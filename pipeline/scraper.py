"""Step 1 -- find today's most interesting trending AI/dev repository.

Pipeline:

    two Search API queries  ->  hard filters  ->  enrich  ->  score  ->  winner

The two queries are complementary on purpose. One finds *breakouts* (young
projects climbing fast), the other finds *established but active* projects.
Running only the first gives you a feed of week-old toys; running only the
second gives you React and VS Code every single day.

Run standalone to inspect the ranking without spending anything downstream:

    python -m pipeline.scraper
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import Settings, get_settings, require_github_token
from pipeline import gateway
from pipeline.models import RepoCandidate
from sources.github import GitHubClient, StarHistory
from sources.hackernews import HackerNewsClient

log = logging.getLogger(__name__)

# How many candidates get a README and a Hacker News lookup. Each one costs a
# request, so this is the spend cap on enrichment rather than a ranking rule.
# `find_trending_repos` raises it to the batch size when that is larger.
_DEFAULT_ENRICH_TOP = 12

# Licenses permissive enough to feature without caveats. MPL-2.0 is weak
# copyleft but file-level only, which is fine for "here's a cool tool".
PERMISSIVE_LICENSES = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MPL-2.0",
    "Unlicense",
    "0BSD",
}

# Matched against the repo's declared topics.
RELEVANT_TOPICS = {
    "ai", "artificial-intelligence", "machine-learning", "deep-learning",
    "llm", "llms", "large-language-models", "genai", "generative-ai",
    "agents", "ai-agents", "agentic", "rag", "embeddings", "vector-database",
    "mcp", "openai", "anthropic", "transformers", "nlp",
    "developer-tools", "devtools", "cli", "developer-experience",
    "devops", "infrastructure", "kubernetes", "observability",
    "typescript", "rust", "golang", "python",
    "database", "api", "framework", "self-hosted", "open-source-alternative",
}

# Fallback for repos whose maintainers never set topics -- matched against
# name + description. Word-boundary matched so "airflow" doesn't match "ai".
RELEVANT_KEYWORDS = {
    # AI / ML
    "ai", "llm", "agent", "agents", "agentic", "rag", "gpt", "claude",
    "copilot", "inference", "embedding", "embeddings", "transformer",
    "prompt", "mcp", "neural", "model", "models", "multimodal",
    "ocr", "vision", "diffusion", "speech", "voice", "tts", "stt",
    "quantization", "finetune", "finetuning", "tokenizer", "reranker",
    "eval", "evals", "benchmark", "guardrails",
    # Developer tooling
    "cli", "devtool", "devtools", "sdk", "framework", "runtime",
    "compiler", "database", "self-hosted", "kubernetes", "observability",
    "terminal", "editor", "debugger", "linter", "formatter", "profiler",
    "tracing", "orchestration", "workflow", "automation", "scraper",
    "proxy", "gateway", "wasm", "webassembly", "typescript", "rust",
}

# Repos we never want to feature: too big to be news, or not really software.
TOPIC_BLOCKLIST = {
    "awesome", "awesome-list", "roadmap", "interview", "interview-questions",
    "tutorial", "course", "book", "books", "cheatsheet", "free",
    "study-plan", "computer-science", "coding-interview",
}
NAME_BLOCKLIST_RE = re.compile(
    r"awesome|roadmap|interview|cheat[- ]?sheet|tutorial|course|free-|-book|30-days|100-days",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Cooldown store
# --------------------------------------------------------------------------


class UsedRepos:
    """Remembers which repos we've already featured.

    Without this, the same three repos win every day for a month -- star
    velocity is sticky, and a repo trending today is usually still trending
    tomorrow.
    """

    def __init__(self, path: Path, cooldown_days: int = 30):
        self.path = path
        self.cooldown_days = cooldown_days
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s (%s); starting fresh.", self.path, exc)
            return {}

    def is_covered(self, full_name: str, on: date | None = None) -> bool:
        """True if we already made a video about this and it is still inside the window.

        Checked during discovery, before enrichment, so a repo we have covered
        costs no README fetch and no Hacker News lookup. The scoring penalty
        below is the same rule applied later; keeping both means a caller that
        scores a pool it assembled itself still cannot pick a repeat.
        """
        used_on = self._data.get(full_name)
        if not used_on:
            return False
        today = on or date.today()
        return (today - date.fromisoformat(used_on)).days < self.cooldown_days

    def penalty(self, full_name: str, on: date | None = None) -> float:
        """1.0 if free to use, 0.0 if inside the cooldown window."""
        return 0.0 if self.is_covered(full_name, on) else 1.0

    def covered(self) -> list[tuple[str, str]]:
        """Everything we have ever covered, newest first. The record, not the filter.

        Includes repos whose cooldown has long expired, because the question
        this answers is "have we done this one" rather than "may we do it now".
        """
        return sorted(self._data.items(), key=lambda kv: kv[1], reverse=True)

    def mark_used(self, full_name: str, on: date | None = None) -> None:
        self._data[full_name] = (on or date.today()).isoformat()
        self.save()

    def clear(self, full_name: str) -> str | None:
        """Drop a repo's cooldown. Returns the date it was set, or None."""
        used_on = self._data.pop(full_name, None)
        if used_on is not None:
            self.save()
        return used_on

    def used_on(self, full_name: str) -> str | None:
        return self._data.get(full_name)

    def merge(self, remote: dict[str, str]) -> list[str]:
        """Fold the gateway's record in. Returns the repos this added or moved.

        **Merge, never replace.** `main.py --posted` marks a repo the account
        published by hand, and the gateway is never told, so it holds a subset
        rather than the truth. Replacing would un-cover exactly the posts that
        exist nowhere else and hand them straight back to discovery.

        The earlier date wins on a conflict, because the cooldown starts at the
        first commitment and taking the later one would extend it by however
        long the two records disagree.

        Saves only when something changed, so a run against a gateway that
        agrees does not rewrite the file.
        """
        changed = []
        for full_name, covered_at in remote.items():
            mine = self._data.get(full_name)
            if mine is not None and mine <= covered_at:
                continue
            self._data[full_name] = covered_at
            changed.append(full_name)
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def _is_relevant(item: dict[str, Any]) -> bool:
    topics = {t.lower() for t in item.get("topics") or []}
    if topics & TOPIC_BLOCKLIST:
        return False

    name = (item.get("name") or "").lower()
    full_name = (item.get("full_name") or "").lower()
    if NAME_BLOCKLIST_RE.search(full_name):
        return False

    if topics & RELEVANT_TOPICS:
        return True

    # Topic-less repos are common and often the most interesting, so fall back
    # to word-boundary keyword matching on name + description.
    haystack = f"{name} {(item.get('description') or '').lower()}"
    words = set(re.findall(r"[a-z0-9+#-]+", haystack))
    return bool(words & RELEVANT_KEYWORDS)


def _passes_hard_filters(item: dict[str, Any]) -> bool:
    if item.get("fork") or item.get("archived") or item.get("disabled"):
        return False
    if item.get("is_template"):
        return False
    spdx = (item.get("license") or {}).get("spdx_id")
    # Client-side, deliberately: GitHub ANDs repeated `license:` qualifiers, so
    # putting this allowlist in the query string would match nothing at all.
    if spdx not in PERMISSIVE_LICENSES:
        return False
    return _is_relevant(item)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _cold_start_velocity(stars: int, age_days: float) -> float:
    """Stars/day proxy, damped for old repos.

    Without damping a five-year-old repo with 50k stars scores ~27 stars/day
    and outranks a genuine breakout that gained 800 stars yesterday.
    """
    raw = stars / max(age_days, 1.0)
    if age_days > 180:
        raw *= 180.0 / age_days
    return raw


def _readme_quality(readme: str) -> float:
    """0..1. Rewards a README that can actually source a script."""
    if not readme:
        return 0.0
    score = 0.0

    # Code fences mean we can build a real code card, which is the whole point.
    fences = readme.count("```")
    if fences >= 2:
        score += 0.5 if fences >= 4 else 0.35

    # Strip badge lines before counting -- some READMEs are 90% shields.io.
    prose = "\n".join(
        line for line in readme.splitlines() if not re.match(r"^\s*[\[!]*\[!\[", line)
    )
    words = len(prose.split())
    if 150 <= words <= 8000:
        score += 0.35
    elif words > 8000:
        score += 0.2
    elif words >= 60:
        score += 0.15

    # An explicit install/usage section is a strong signal of a usable tool.
    if re.search(r"^#{1,3}\s*(install|usage|quick ?start|getting started)", readme,
                 re.IGNORECASE | re.MULTILINE):
        score += 0.15

    return min(score, 1.0)


def _normalise(values: list[float]) -> list[float]:
    """Scale to 0..1 against the pool max. Empty/flat pools score 0."""
    hi = max(values, default=0.0)
    if hi <= 0:
        return [0.0] * len(values)
    return [v / hi for v in values]


def score_candidates(
    candidates: list[RepoCandidate], used: UsedRepos, on: date | None = None
) -> list[RepoCandidate]:
    """Assign scores in place and return the list sorted best-first."""
    if not candidates:
        return []

    velocities = _normalise([c.velocity for c in candidates])
    star_scores = _normalise([math.log10(max(c.stars, 1)) for c in candidates])

    for cand, vel_n, star_n in zip(candidates, velocities, star_scores, strict=True):
        hn = min((cand.hn_points or 0) / 300.0, 1.0)
        readme = _readme_quality(cand.readme)

        breakdown = {
            "velocity": 0.55 * vel_n,
            "stars": 0.20 * star_n,
            "hackernews": 0.15 * hn,
            "readme": 0.10 * readme,
        }
        penalty = used.penalty(cand.full_name, on)
        cand.score_breakdown = {**breakdown, "cooldown_multiplier": penalty}
        cand.score = sum(breakdown.values()) * penalty

    return sorted(candidates, key=lambda c: c.score, reverse=True)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _build_queries(cfg: Settings, today: date) -> list[str]:
    breakout_since = (today - timedelta(days=cfg.breakout_window_days)).isoformat()
    pushed_since = (today - timedelta(days=cfg.pushed_window_days)).isoformat()
    return [
        # A. Recent breakouts -- young projects climbing fast.
        f"created:>{breakout_since} stars:>{cfg.min_stars_breakout}",
        # B. Established but still shipping.
        f"pushed:>{pushed_since} stars:>{cfg.min_stars_established}",
    ]


def _to_candidate(item: dict[str, Any]) -> RepoCandidate:
    return RepoCandidate(
        full_name=item["full_name"],
        name=item["name"],
        owner=item["owner"]["login"],
        url=item["html_url"],
        description=item.get("description") or "",
        homepage=item.get("homepage") or None,
        stars=item.get("stargazers_count", 0),
        forks=item.get("forks_count", 0),
        language=item.get("language"),
        license_spdx=(item.get("license") or {}).get("spdx_id"),
        topics=item.get("topics") or [],
        created_at=_parse_dt(item.get("created_at")),
        pushed_at=_parse_dt(item.get("pushed_at")),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sync_covered(cfg: Settings, used: UsedRepos) -> None:
    """Fold the gateway's record of committed repos into the local store.

    Runs before the first Search call, so a repo recovered here costs no query
    slot and no README fetch on its way to being dropped.

    Silent when there is nothing to add, which is the normal case on the machine
    that did the enqueueing. It only says anything when the two records
    disagreed, since that is the case worth seeing: this laptop had forgotten a
    repo it has already posted about.
    """
    added = used.merge(gateway.fetch_covered(cfg))
    if added:
        log.info(
            "Recovered %d covered repo(s) from the gateway: %s",
            len(added), ", ".join(sorted(added)),
        )


def collect_candidates(
    cfg: Settings, *, enrich_top: int = _DEFAULT_ENRICH_TOP, on: date | None = None
) -> list[RepoCandidate]:
    """Discover, filter, enrich, and score. Returns best-first."""
    today = on or date.today()
    token = require_github_token(cfg)

    history = StarHistory(cfg.star_history_path)
    used = UsedRepos(cfg.used_repos_path, cfg.repo_cooldown_days)
    _sync_covered(cfg, used)
    # Repos whose video already exists but was never queued. Held apart from
    # `used` rather than merged into it: a commitment is a 30 day cooldown and
    # this is only "do not build that twice", which `--unmark` undoes.
    rendered = gateway.fetch_rendered(cfg)

    seen: dict[str, RepoCandidate] = {}
    covered = 0
    already_built = 0

    with GitHubClient(token) as gh:
        for query in _build_queries(cfg, today):
            for item in gh.search_repositories(query, limit=cfg.candidates_per_query):
                # Snapshot every repo we lay eyes on, even ones we drop below.
                # Tomorrow's velocity is only as good as today's coverage, and
                # that includes repos we have already featured.
                history.record(item["full_name"], item.get("stargazers_count", 0), today)

                if item["full_name"] in seen or not _passes_hard_filters(item):
                    continue
                # Dropped here rather than scored to zero later, so a repo we
                # have already made a video about never reaches the enrichment
                # step. Star velocity is sticky: yesterday's winner is usually
                # still near the top today, and enriching it costs a README
                # fetch and a Hacker News lookup to produce a candidate that
                # cannot win.
                if used.is_covered(item["full_name"], today):
                    covered += 1
                    continue
                # Same reasoning one step weaker: the video exists, so building
                # it again spends a script, a voiceover and a render to land
                # back where we already are.
                if item["full_name"] in rendered:
                    already_built += 1
                    continue
                seen[item["full_name"]] = _to_candidate(item)

        log.info(
            "Found %d candidates after filtering, %d skipped as already covered, "
            "%d already rendered (GitHub quota left: %s)",
            len(seen), covered, already_built, gh.rate_limit_remaining,
        )

        # Velocity for everything -- it's free, it's already in memory.
        for cand in seen.values():
            gained = history.gained_today(cand.full_name, cand.stars, today)
            if gained is not None:
                cand.stars_gained_today = gained
                cand.velocity = float(max(gained, 0))
                cand.velocity_is_measured = True
            else:
                cand.velocity = _cold_start_velocity(cand.stars, cand.age_days)

        # READMEs and HN cost a request each, so only enrich plausible winners.
        # Rank by velocity first to decide who's worth the spend.
        shortlist = sorted(seen.values(), key=lambda c: c.velocity, reverse=True)[:enrich_top]
        with HackerNewsClient() as hn_client:
            for cand in shortlist:
                cand.readme = gh.fetch_readme(cand.full_name, cfg.readme_char_budget)
                if story := hn_client.top_story_for_repo(cand.full_name):
                    cand.hn_points, cand.hn_url = story

    history.save()
    return score_candidates(list(seen.values()), used, today)


def snapshot_stars(cfg: Settings, *, on: date | None = None) -> int:
    """Record today's star counts for every repo the discovery queries return.

    Deliberately search-and-record only: no README fetches, no Hacker News, no
    scoring. Velocity is 55% of the candidate score but it is only *measured*
    when a snapshot exists from an earlier day, so this needs to run daily --
    including on days we never generate a video. Two search requests, a couple
    of seconds, and from the second day onward every ranking uses real deltas
    instead of the damped stars/day proxy.
    """
    today = on or date.today()
    token = require_github_token(cfg)
    history = StarHistory(cfg.star_history_path)

    seen: set[str] = set()
    with GitHubClient(token) as gh:
        for query in _build_queries(cfg, today):
            for item in gh.search_repositories(query, limit=cfg.candidates_per_query):
                history.record(item["full_name"], item.get("stargazers_count", 0), today)
                seen.add(item["full_name"])
        quota = gh.rate_limit_remaining

    history.save()
    log.info(
        "Snapshotted %d repos for %s (GitHub quota left: %s)", len(seen), today.isoformat(), quota
    )
    return len(seen)


def record_snapshot(cfg: Settings, repo: RepoCandidate, *, on: date | None = None) -> None:
    """Snapshot one repo's stars, for paths that skip discovery.

    `--repo` bypasses `collect_candidates` entirely, so without this the repo we
    just spent a whole pipeline run on contributes nothing to tomorrow's
    velocity measurement.
    """
    history = StarHistory(cfg.star_history_path)
    history.record(repo.full_name, repo.stars, on or date.today())
    history.save()


def find_trending_repos(
    cfg: Settings | None = None, *, count: int = 1, on: date | None = None
) -> list[RepoCandidate]:
    """The top `count` distinct repos, best first.

    One discovery pass for the whole batch, which is the only way to get
    distinct winners: nothing marks a repo as taken until it is published or
    queued, so ranking once per video would hand the same winner to every run
    in the batch.

    Returns fewer than `count` rather than raising when the pool is thin. A
    batch of three that only found two good repos should still make two videos,
    and the caller says so out loud.

    **Enrichment has to cover the whole batch.** The shortlist that gets a
    README is picked by velocity, while the final ranking is by score, where the
    README is only a tenth and velocity is over half. So a repo can place inside
    the top `count` by score without ever having been enriched, and the
    scriptwriter then works from the one line description. Invisible at
    `--batch 3` against the default of 12 and certain at `--batch 15`, which is
    why the ceiling follows the batch rather than sitting at a constant.
    """
    cfg = cfg or get_settings()
    ranked = collect_candidates(cfg, enrich_top=max(_DEFAULT_ENRICH_TOP, count), on=on)
    if not ranked:
        raise RuntimeError(
            "No candidate repositories survived filtering. Either everything "
            "trending is already covered (see `--covered`), or the filters are "
            "too tight: try lowering MIN_STARS_BREAKOUT or widening "
            "BREAKOUT_WINDOW_DAYS in .env"
        )
    winners = [c for c in ranked[:count] if c.score > 0]
    if not winners:
        raise RuntimeError(
            "Every candidate scored zero, which means the pool is flat rather "
            "than empty. Widen BREAKOUT_WINDOW_DAYS in .env"
        )
    return winners


def find_trending_repo(cfg: Settings | None = None, *, on: date | None = None) -> RepoCandidate:
    """The single entry point main.py calls for a one-video run."""
    return find_trending_repos(cfg, count=1, on=on)[0]


def mark_featured(cfg: Settings, full_name: str, on: date | None = None) -> None:
    """Record that we *posted* a video about this repo, starting its cooldown.

    Deliberately not called by the render step. A rendered video you looked at
    and rejected should not burn the repo for a month; only a posted one should.
    `main.py --posted owner/repo` is what calls this.
    """
    UsedRepos(cfg.used_repos_path, cfg.repo_cooldown_days).mark_used(full_name, on)


def unmark_featured(cfg: Settings, full_name: str) -> str | None:
    """Escape hatch: clear a repo's cooldown. Returns the date it was set."""
    return UsedRepos(cfg.used_repos_path, cfg.repo_cooldown_days).clear(full_name)


def covered_repos(cfg: Settings) -> list[tuple[str, str]]:
    """Every repo we have committed to a post about, newest first."""
    return UsedRepos(cfg.used_repos_path, cfg.repo_cooldown_days).covered()


# --------------------------------------------------------------------------
# Standalone inspection
# --------------------------------------------------------------------------


def inspect_candidates(cfg: Settings | None = None, *, top: int = 15) -> list[RepoCandidate]:
    """Rank today's repos and print the table. Returns the full ranking.

    Used by `python main.py --candidates` and by `python -m pipeline.scraper`.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    cfg = cfg or get_settings()

    with console.status("Searching GitHub..."):
        ranked = collect_candidates(cfg)

    table = Table(title=f"Top candidates for {date.today().isoformat()}", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Repository", style="cyan", no_wrap=True)
    table.add_column("Stars", justify="right")
    table.add_column("Today", justify="right")
    table.add_column("Lang")
    table.add_column("License")
    table.add_column("HN", justify="right")
    table.add_column("Score", justify="right", style="bold")

    for i, c in enumerate(ranked[:top], 1):
        gained = f"+{c.stars_gained_today}" if c.velocity_is_measured else f"~{c.velocity:.0f}"
        table.add_row(
            str(i),
            c.full_name,
            f"{c.stars:,}",
            gained,
            c.language or "-",
            c.license_spdx or "-",
            str(c.hn_points or "-"),
            f"{c.score:.3f}",
        )

    console.print(table)
    if ranked:
        console.print(
            "\n[dim]'Today' is measured (+N) once history exists, "
            "otherwise a damped stars/day proxy (~N).[/dim]"
        )
        console.print(f"[bold green]Winner:[/] {ranked[0].full_name} — {ranked[0].description}")

    return ranked


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    inspect_candidates()
