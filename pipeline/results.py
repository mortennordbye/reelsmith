"""What this account's own videos did, joined to the hook that was on them.

The pipeline could generate, publish and, since the insights sweep, measure.
What it could not do was learn: every script was written as though it were the
first, because the numbers reached a web page and stopped there. This is the
join that closes the loop.

It takes two halves neither side holds alone. The gateway knows the media id,
the repo and how the post did, and has never seen a hook. The run folder on
this machine holds the hook and has never heard of a media id, because a queued
post is published days later by a service on another continent. `repo_full_name`
is what they have in common, and the 30 day cooldown makes it unique enough:
the same repo cannot appear twice inside the window this ever looks at.

Everything here degrades to an empty list. A missing gateway, an unreadable
run folder or a repo whose build directory was moved aside all mean the same
thing, which is that this run writes its script the way every run before it
did. That is worth no log line above debug and certainly not a failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from pipeline import gateway

log = logging.getLogger(__name__)

# How many past posts to carry into a prompt. Enough to show a pattern, few
# enough that the model weighs the account's own evidence rather than drowning
# in it, and few enough that the block stays readable when a human reads the
# prompt back to work out why a script came out the way it did.
MAX_RESULTS = 10


@dataclass(frozen=True)
class PastPost:
    """One published Reel, with the opening that earned or lost its audience."""

    repo_full_name: str
    hook: str
    skip_rate: float
    avg_watch_s: float
    views: int

    @property
    def line(self) -> str:
        return f"{self.skip_rate:4.1f}%  {self.hook}"


def _hooks_by_repo(build_dir: Path) -> dict[str, str]:
    """The hook that shipped, per repo.

    A repo usually has more than one run folder, because the documented way to
    force a regeneration is to move the old one aside rather than delete it.
    `dietrichgebert-ponytail` has three: the one that published, a `.prev` and
    a `.v2`, and the `.v2` hook was a draft that was rejected for naming YAGNI
    without explaining it.

    So the folders are ranked rather than iterated, because the first version
    of this took the last one in sorted order and confidently reported a
    rejected draft as the hook that scored 79.5 percent. A feedback loop
    carrying the wrong evidence is worse than no feedback loop, and nothing
    downstream could have caught it.

    Ranked on, in order:

    - **An unsuffixed folder name.** `RepoCandidate.slug` turns every dot into
      a hyphen, so a dot in a directory name can only have come from a human
      renaming it. This is the strongest signal available and it stays correct
      when the current run is set aside and re-rendered, because the new run is
      the unsuffixed one.
    - **A publish receipt**, which is proof it went out. Second rather than
      first because a set-aside folder keeps the receipt it had:
      `xai-org-grok-build.v1-posted` still holds one for a media that was
      deleted.
    - **The later date**, for an honest re-render on a later day.
    """
    hooks: dict[str, tuple[tuple[bool, bool, str], str]] = {}
    if not build_dir.exists():
        return hooks

    for day in sorted(p for p in build_dir.iterdir() if p.is_dir()):
        for run in sorted(p for p in day.iterdir() if p.is_dir()):
            script, repo = run / "script.json", run / "repo.json"
            if not (script.exists() and repo.exists()):
                continue
            try:
                full_name = json.loads(repo.read_text())["full_name"]
                hook = json.loads(script.read_text())["hook"]
            except (OSError, ValueError, KeyError):
                continue
            if not (full_name and hook):
                continue

            rank = (
                "." not in run.name,
                (run / "published.json").exists() or (run / "queued.json").exists(),
                day.name,
            )
            if full_name not in hooks or rank > hooks[full_name][0]:
                hooks[full_name] = (rank, hook)

    return {repo: hook for repo, (_, hook) in hooks.items()}


def past_posts(
    cfg: Settings, *, build_dir: Path | None = None, limit: int = MAX_RESULTS
) -> list[PastPost]:
    """The account's own results, best opening first.

    Sorted by skip rate rather than by date, because the question the caller is
    about to ask is "what worked", and a date order answers "what happened".

    `build_dir` is injected the same way the http clients in this package are,
    and for the same reason: `Settings.build_dir` is a property rooted at the
    repo, so without a seam every test here would read, and create, the real
    one.
    """
    readings = gateway.fetch_results(cfg)
    if not readings:
        return []

    hooks = _hooks_by_repo(build_dir or cfg.build_dir)
    out = []
    for row in readings:
        hook = hooks.get(row.get("repo_full_name") or "")
        skip = row.get("skip_rate")
        if not hook or not skip:
            continue
        out.append(
            PastPost(
                repo_full_name=row["repo_full_name"],
                hook=hook,
                skip_rate=float(skip),
                avg_watch_s=float(row.get("avg_watch_ms") or 0) / 1000,
                views=int(row.get("views") or 0),
            )
        )

    out.sort(key=lambda p: p.skip_rate)
    return out[:limit]
