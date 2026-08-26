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

import contextlib
import hashlib
import json
import logging
import subprocess
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
    recipe: str = ""
    # Already collected by the insights sweep and never sent until now. Skip
    # rate scores the opening; nothing scored whether the video was worth
    # passing on, which is the input a third party claims outweighs a like
    # several times over. None rather than 0 for a gateway too old to send
    # them, because a zero share count reads as a video nobody passed on and
    # "not measured" is a different claim.
    saved: int | None = None
    shares: int | None = None

    @property
    def line(self) -> str:
        return f"{self.skip_rate:4.1f}%  {self.hook}"


def recipe(cfg: Settings) -> str:
    """A short fingerprint of everything that decides how a script comes out.

    Three posts a day go out while the prompt is being edited daily, so without
    this "did the hook change work" is unanswerable after the fact: the numbers
    are attached to a video and nothing records which version of the rules
    wrote it. Two runs a week apart with the same fingerprint are comparable
    and two with different ones are not, which is the whole claim.

    The commit is most of it, because the prompt lives in git. The digest
    covers what git cannot see: an uncommitted edit mid-session, a checkout
    where `git rev-parse` finds nothing, and the `.env` knobs that change the
    output without changing a tracked file.

    **The digest reads `scriptwriter.prompt_source`, not `SYSTEM_PROMPT`.**
    It hashed the system prompt alone until 2026-08-26, which is roughly a
    third of what the model is shown: the research rule, the whole hook
    specification, the caption rules and the benchmark the results block
    argues from are all in `_build_prompt` and `_results_block`, and editing
    any of them left the fingerprint unchanged. That is the half most often
    edited, so the digest was blind in exactly the direction it was built to
    see. Every post published before this carries `0ec3237b` and is comparable
    to nothing written after it, which is the honest answer rather than a
    regression.
    """
    knobs = "|".join(
        str(x)
        for x in (
            cfg.max_script_words,
            cfg.max_hook_chars,
            cfg.claude_model,
            cfg.claude_effort,
            cfg.claude_research,
            cfg.tts_backend,
        )
    )
    from pipeline import scriptwriter

    prompt = scriptwriter.prompt_source()
    digest = hashlib.sha256(f"{prompt}\n{knobs}".encode()).hexdigest()[:8]
    return f"{_git_commit()}.{digest}"


def _git_commit() -> str:
    """The checkout a run came from, or `nogit` outside one.

    `dirty` on the end rather than a different hash, because an uncommitted
    tree is not a version anyone can go back to and saying so is more useful
    than pretending it is one.
    """
    try:
        sha = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True, text=True, timeout=5, cwd=Path(__file__).parent.parent,
        )
        if sha.returncode != 0:
            return "nogit"
        status = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "status", "--porcelain"],  # noqa: S607
            capture_output=True, text=True, timeout=5, cwd=Path(__file__).parent.parent,
        )
        suffix = "-dirty" if status.stdout.strip() else ""
        return f"{sha.stdout.strip()}{suffix}"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def write_recipe(run_dir: Path, cfg: Settings) -> str:
    """Record what produced this run, next to what it produced."""
    value = recipe(cfg)
    (run_dir / "recipe.json").write_text(
        json.dumps(
            {
                "recipe": value,
                "max_script_words": cfg.max_script_words,
                "max_hook_chars": cfg.max_hook_chars,
                "claude_model": cfg.claude_model,
                "claude_effort": cfg.claude_effort,
                "claude_research": cfg.claude_research,
                "tts_backend": cfg.tts_backend,
            },
            indent=2,
        )
        + "\n"
    )
    return value


def read_recipe(run_dir: Path) -> str:
    """The recipe recorded when this run was made, or empty if none was.

    Read rather than recomputed. `--enqueue` can run days after the render and
    `--recover` sweeps two days of folders, so calling `recipe()` here would
    stamp the video with the checkout that queued it instead of the one that
    wrote it, which is the exact confusion this whole fingerprint exists to
    remove. A folder made before recipes existed has no file and gets "".
    """
    path = run_dir / "recipe.json"
    if not path.exists():
        return ""
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return str(json.loads(path.read_text()).get("recipe") or "")
    return ""


def _runs_by_repo(build_dir: Path) -> dict[str, tuple[str, str]]:
    """The hook that shipped and the recipe that wrote it, per repo.

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
    found: dict[str, tuple[tuple[bool, bool, str], str, str]] = {}
    if not build_dir.exists():
        return {}

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

            # Absent on every run made before recipes existed, which is all of
            # the first seven. Blank rather than a guess: an invented version
            # string would make two incomparable runs look comparable, which is
            # the one thing this is for.
            made_with = ""
            with contextlib.suppress(OSError, ValueError, KeyError):
                made_with = json.loads((run / "recipe.json").read_text())["recipe"]

            rank = (
                "." not in run.name,
                (run / "published.json").exists() or (run / "queued.json").exists(),
                day.name,
            )
            if full_name not in found or rank > found[full_name][0]:
                found[full_name] = (rank, hook, made_with)

    return {repo: (hook, made_with) for repo, (_, hook, made_with) in found.items()}


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

    runs = _runs_by_repo(build_dir or cfg.build_dir)
    out = []
    for row in readings:
        run = runs.get(row.get("repo_full_name") or "")
        skip = row.get("skip_rate")
        if not skip:
            continue
        # What the gateway sends was recorded by the machine that rendered, so
        # it wins over anything found here. The local lookup is by repo name
        # against a build folder, which on a machine that did not render this
        # video answers with a run that was never on it, confidently: the hook
        # it returned for `ultraworkers/claw-code` came from a script written
        # eleven days earlier that was never rendered at all.
        #
        # It is also what lets a post reach the loop from anywhere. Requiring a
        # local folder to produce a hook silently dropped every Reel rendered on
        # the pod, which on this laptop was thirteen of them.
        hook = str(row.get("hook") or "") or (run[0] if run else "")
        made_with = str(row.get("recipe") or "") or (run[1] if run else "")
        if not hook:
            continue
        out.append(
            PastPost(
                repo_full_name=row["repo_full_name"],
                hook=hook,
                skip_rate=float(skip),
                avg_watch_s=float(row.get("avg_watch_ms") or 0) / 1000,
                views=int(row.get("views") or 0),
                recipe=made_with,
                saved=None if row.get("saved") is None else int(row["saved"]),
                shares=None if row.get("shares") is None else int(row["shares"]),
            )
        )

    out.sort(key=lambda p: p.skip_rate)
    return out[:limit]
