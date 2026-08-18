"""GitHub Search API client plus a local star-velocity store.

Why this file exists in the shape it does:

GitHub has no trending API. github.com/trending is a server-rendered HTML page
with nothing behind it, and scraping it breaks whenever the markup changes.
The Search API *can* sort by total stars but cannot sort by stars-gained-today,
which is the signal we actually want.

So we compute velocity ourselves: every run snapshots the star count of every
repo it sees into data/star_history.json. From the second run onward we have
real deltas. On a cold start we fall back to a damped stars-per-day proxy.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# The Search API caps out here regardless of what you ask for.
MAX_PER_PAGE = 100
# And it refuses `page * per_page > 1000` outright, whatever total_count says,
# so this is how deep any single query can ever be read. Getting past it needs
# a different query, not another page.
SEARCH_RESULT_CAP = 1000


class RateLimited(Exception):
    """Raised when GitHub tells us to back off. Retried by tenacity."""


class GitHubError(RuntimeError):
    """A non-retryable API failure."""


# --------------------------------------------------------------------------
# Star history / velocity
# --------------------------------------------------------------------------


class StarHistory:
    """Persistent {full_name: {iso_date: stars}} snapshot store.

    Small enough to keep as one JSON file: even tracking 200 repos a day for a
    year is well under a megabyte, and it stays trivially inspectable by hand.
    """

    def __init__(self, path: Path, keep_days: int = 120):
        self.path = path
        self.keep_days = keep_days
        self._data: dict[str, dict[str, int]] = self._load()

    def _load(self) -> dict[str, dict[str, int]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt history file should degrade us to cold-start behaviour,
            # not crash the pipeline.
            log.warning("Could not read %s (%s); starting fresh.", self.path, exc)
            return {}

    def record(self, full_name: str, stars: int, on: date | None = None) -> None:
        day = (on or date.today()).isoformat()
        self._data.setdefault(full_name, {})[day] = stars

    def gained_today(self, full_name: str, stars_now: int, on: date | None = None) -> int | None:
        """Stars gained since the most recent *earlier* snapshot.

        Returns None when we have never seen this repo before, which the caller
        uses to decide between a measured velocity and the cold-start proxy.
        """
        today = on or date.today()
        history = self._data.get(full_name, {})
        earlier = [d for d in history if d < today.isoformat()]
        if not earlier:
            return None

        prev_day = max(earlier)
        prev_stars = history[prev_day]
        elapsed_days = max((today - date.fromisoformat(prev_day)).days, 1)
        # Normalise to a per-day figure so a 3-day gap doesn't look like a spike.
        return round((stars_now - prev_stars) / elapsed_days)

    def prune(self, on: date | None = None) -> None:
        cutoff = ((on or date.today()) - timedelta(days=self.keep_days)).isoformat()
        for name in list(self._data):
            kept = {d: s for d, s in self._data[name].items() if d >= cutoff}
            if kept:
                self._data[name] = kept
            else:
                del self._data[name]

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)  # atomic; a crash mid-write can't corrupt history


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class GitHubClient:
    def __init__(self, token: str = "", timeout: float = 20.0):
        """A token is optional but strongly recommended.

        Discovery needs one -- unauthenticated search allows 10 requests/minute
        and 60 core requests/hour. But fetching a single named repo fits
        comfortably inside the anonymous budget, so `--repo` works without one.
        """
        self._last_remaining = "?"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "reelsmith-pipeline",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=API_ROOT,
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- low level ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (RateLimited, httpx.TimeoutException, httpx.TransportError)
        ),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.get(path, **kwargs)

        # GitHub signals secondary/abuse rate limits with 403, not 429, and
        # only sometimes sets Retry-After. Treat both as retryable and sleep
        # for whatever the headers tell us.
        if resp.status_code in (403, 429):
            if wait_s := _retry_delay(resp):
                log.warning("GitHub rate limited; sleeping %.0fs", wait_s)
                time.sleep(min(wait_s, 120))
                raise RateLimited(f"{resp.status_code} on {path}")
            # A 403 with quota remaining is a real permission error, not a limit.
            raise GitHubError(f"403 from GitHub on {path}: {resp.text[:300]}")

        if resp.status_code == 422:
            raise GitHubError(f"GitHub rejected the query on {path}: {resp.text[:300]}")

        resp.raise_for_status()
        return resp

    @property
    def rate_limit_remaining(self) -> str:
        """Last-seen remaining quota, for logging. '?' before the first call."""
        return self._last_remaining

    # -- public ------------------------------------------------------------

    def iter_repositories(
        self, query: str, *, max_results: int = SEARCH_RESULT_CAP
    ) -> Iterator[dict[str, Any]]:
        """Yield one Search API query's results best-first, a page at a time.

        A generator rather than a list because the caller is the only one who
        knows when it has enough. Results are sorted by stars descending and
        most of them get filtered out client-side, so "how many results" is the
        wrong unit to ask for: a caller that needs 50 *usable* repos may have
        to look at 300 to find them on one night and 60 on the next. A page
        nobody asks for is a search request never spent.

        Note the qualifier trap this deliberately avoids: GitHub ANDs repeated
        qualifiers, so `license:mit license:apache-2.0` silently matches
        *nothing*. All license and topic filtering happens client-side in
        pipeline/scraper.py against the response body instead.
        """
        max_results = min(max_results, SEARCH_RESULT_CAP)
        fetched = 0
        page = 1
        while fetched < max_results:
            per_page = min(MAX_PER_PAGE, max_results - fetched)
            resp = self._get(
                "/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            self._last_remaining = resp.headers.get("x-ratelimit-remaining", "?")
            items = resp.json().get("items", [])
            if not items:
                return
            yield from items
            fetched += len(items)
            if len(items) < per_page:
                return
            page += 1
        log.debug(
            "query %r exhausted %d results (quota left: %s)",
            query, fetched, self._last_remaining,
        )

    def search_repositories(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """The first `limit` results of one query, as a list.

        The eager form of `iter_repositories`, for callers that want a fixed
        window rather than a supply they stop drawing from.
        """
        out = list(islice(self.iter_repositories(query, max_results=limit), limit))
        log.debug("query %r -> %d repos (quota left: %s)", query, len(out), self._last_remaining)
        return out

    def fetch_repo(self, full_name: str) -> dict[str, Any]:
        """Fetch one repo by name via the core API.

        Used by --repo. Deliberately not the Search API: this endpoint costs a
        single core request instead of a search request, so it works without a
        token and returns the same fields.
        """
        resp = self._get(f"/repos/{full_name}")
        self._last_remaining = resp.headers.get("x-ratelimit-remaining", "?")
        return resp.json()

    def fetch_readme(self, full_name: str, char_budget: int = 12_000) -> str:
        """Fetch a repo's README as raw markdown.

        Returns "" rather than raising when a repo has no README -- that is a
        normal condition and only affects the repo's quality score.
        """
        try:
            resp = self._get(
                f"/repos/{full_name}/readme",
                headers={"Accept": "application/vnd.github.raw"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ""
            raise
        except GitHubError:
            return ""

        text = resp.text
        if len(text) > char_budget:
            # Truncate on a line boundary so we never hand Claude a snippet cut
            # mid-code-fence.
            text = text[:char_budget].rsplit("\n", 1)[0] + "\n\n[README truncated]"
        return text


def _retry_delay(resp: httpx.Response) -> float | None:
    """Seconds to wait, or None if this response is not a rate limit."""
    if retry_after := resp.headers.get("retry-after"):
        try:
            return float(retry_after)
        except ValueError:
            pass

    if resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            try:
                delta = int(reset) - int(datetime.now(UTC).timestamp())
                return max(delta, 1)
            except ValueError:
                pass
        return 60.0

    return None
