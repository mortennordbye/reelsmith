"""Hacker News lookup, used as a corroboration signal.

Star velocity alone can be gamed or noisy. A repo that is *also* being
discussed on HN right now is far more likely to be genuinely interesting to a
developer audience, so we use HN points as a bonus term in the score rather
than as a discovery source of its own.

Uses the Algolia HN Search API (no auth, no key, generous limits) instead of
the official Firebase API, because Firebase has no search -- you would have to
walk item IDs to find stories mentioning a repo.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"


class HackerNewsClient:
    def __init__(self, timeout: float = 10.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "tech-ig-pipeline"},
        )

    def __enter__(self) -> HackerNewsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def top_story_for_repo(
        self, full_name: str, within_hours: int = 72
    ) -> tuple[int, str] | None:
        """Highest-scoring recent HN story about this repo.

        Returns (points, story_url) or None. Never raises: HN being down must
        not take out the pipeline, since this is only a scoring bonus.
        """
        since = datetime.now(UTC) - timedelta(hours=within_hours)
        try:
            resp = self._client.get(
                ALGOLIA_SEARCH,
                params={
                    # Searching the full "owner/name" is precise enough to avoid
                    # matching unrelated stories about a common repo name.
                    "query": full_name,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{int(since.timestamp())}",
                    "hitsPerPage": 10,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("HN lookup failed for %s: %s", full_name, exc)
            return None

        best: tuple[int, str] | None = None
        for hit in hits:
            points = hit.get("points") or 0
            if points < 20:  # below this it is noise, not a signal
                continue
            # Require the repo to actually be referenced, not just fuzzily matched.
            haystack = f"{hit.get('title', '')} {hit.get('url') or ''}".lower()
            if full_name.lower() not in haystack:
                continue
            story_url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            if best is None or points > best[0]:
                best = (points, story_url)
        return best
