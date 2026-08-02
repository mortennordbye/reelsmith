"""Register a Reel that was published outside the pipeline.

Three of the first Reels this account put out were posted by hand from the
phone, before `--publish` existed. Two of them were later registered with the
gateway and one was not, so the feedback loop reads six posts where the account
has seven, and the missing one is `dietrichgebert/ponytail`: one of only two
hooks so far that was not the "Your coding agent does X" formula the loop exists
to argue the next script out of. The evidence that matters most is exactly the
evidence that went missing, which is not a coincidence so much as a reminder
that the unusual post is the one that took an unusual route.

Nothing on this machine can point at that Reel. The run folder holds the hook,
the caption and the repo, and has never heard of a media id, because the media
id was created by the Instagram app on a phone. Meta holds the media id, the
numbers and the caption. **The caption is the only string both sides have
seen**, so it is the join, the same way `repo_full_name` is the join in
`results.py` and for the same reason: use what both parties independently wrote
down, not what one of them can be asked to remember.

Two rules keep the join honest.

- **The first paragraph only.** The gateway inserts the comment ask above the
  hashtag block, so the tail of a caption differs depending on which path
  published it. The body paragraph is written once, by the scriptwriter, and is
  never touched again.
- **An ambiguous body matches nothing.** A repo that was re-rendered has several
  run folders and they often share a caption. Registering the wrong one would
  attribute a rejected draft's hook to a real post's numbers, which is the bug
  `_runs_by_repo` already exists to prevent, arriving by a different door.

A backfilled post is registered for measurement and not for polling. It is days
old by definition, and a private reply to a comment whose author has moved on
reads as a bot rather than as an answer.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from config import Settings
from pipeline import gateway, publisher

log = logging.getLogger(__name__)

# Meta returns the newest first. Fifty is well past the point where a post is
# still worth measuring and keeps this to one page, so there is no paging to get
# wrong on a path that runs by hand a few times a year.
MEDIA_PAGE = 50

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Match:
    """A live Reel and the run folder that made it."""

    media_id: str
    run: Path
    repo_full_name: str
    link: str
    keyword: str
    published_at: datetime
    permalink: str | None = None
    hook: str = ""


@dataclass(frozen=True)
class Plan:
    """What a backfill would do, before it does any of it."""

    matched: tuple[Match, ...] = ()
    # Live media no run folder explains. Usually nothing, but a caption edited
    # in the app lands here and is worth printing rather than swallowing: the
    # alternative is a post that silently never joins and nobody knows why.
    unmatched: tuple[dict, ...] = ()
    # A body paragraph that more than one run folder claims. Named so the fix is
    # obvious, which is to say which folder shipped.
    ambiguous: tuple[str, ...] = ()


def body_key(caption: str) -> str:
    """The first paragraph, normalised, as written by whoever wrote the script.

    Case and whitespace are levelled because a caption makes a round trip
    through a phone keyboard on this path, and neither survives that reliably
    enough to be part of an identity.
    """
    first = caption.strip().split("\n\n", 1)[0]
    return _WHITESPACE.sub(" ", first).strip().casefold()


def index_runs(build_dir: Path) -> tuple[dict[str, Path], set[str]]:
    """Every run folder that has a caption, keyed by its body paragraph.

    Returns the unambiguous ones and, separately, the keys thrown out for being
    claimed twice. A caller that silently dropped the second half would report
    "no match" for a post whose folder is sitting right there.

    Takes the directory rather than the settings for the reason `results.py`
    does: `Settings.build_dir` is a property rooted at the repo, so without a
    seam every test here reads the real one.
    """
    runs: dict[str, Path] = {}
    clashes: set[str] = set()
    build = build_dir
    if not build.exists():
        return runs, clashes

    for day in sorted(p for p in build.iterdir() if p.is_dir()):
        for run in sorted(p for p in day.iterdir() if p.is_dir()):
            caption = run / "caption.txt"
            if not caption.exists():
                continue
            try:
                key = body_key(caption.read_text())
            except OSError:
                continue
            if not key:
                continue
            if key in runs and runs[key] != run:
                clashes.add(key)
                continue
            runs[key] = run
    for key in clashes:
        runs.pop(key, None)
    return runs, clashes


def _match(run: Path, media: dict, cfg: Settings) -> Match | None:
    """Everything the gateway needs about one post, read off the run folder."""
    try:
        repo = json.loads((run / "repo.json").read_text())
    except (OSError, ValueError):
        log.debug("%s has no readable repo.json", run)
        return None

    full_name, link = repo.get("full_name"), repo.get("url")
    if not (full_name and link):
        return None

    # Only for the table a person reads before saying yes. A run whose script
    # is unreadable is still worth registering, because the numbers are what
    # the loop needs and `results.py` reads the hook again on its own.
    hook = ""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        hook = json.loads((run / "script.json").read_text()).get("hook", "")

    try:
        published_at = datetime.fromisoformat(media["timestamp"].replace("+0000", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        log.debug("%s has no readable timestamp", media.get("id"))
        return None

    return Match(
        media_id=str(media["id"]),
        run=run,
        repo_full_name=full_name,
        link=link,
        # Deterministic from the repo name, so this reproduces the keyword the
        # post would have been registered with had it gone out the normal way.
        # Inert either way while `poll_comments` is off, and correct if the flag
        # is ever turned back on by hand.
        keyword=gateway.keyword_for(full_name, cfg),
        published_at=published_at,
        permalink=media.get("permalink"),
        hook=hook,
    )


def plan(
    cfg: Settings,
    *,
    build_dir: Path | None = None,
    media: list[dict] | None = None,
    client: httpx.Client | None = None,
) -> Plan:
    """What could be registered, without registering anything.

    `media` is injectable so this can be replayed against a saved page from
    Meta, which is the only way to test a join whose whole difficulty is real
    captions that have been through a phone.
    """
    items = publisher.list_media(cfg, limit=MEDIA_PAGE, client=client) if media is None else media
    runs, clashes = index_runs(build_dir or cfg.build_dir)

    matched: list[Match] = []
    unmatched: list[dict] = []
    ambiguous: set[str] = set()

    for item in items:
        # Everything else on the account is a photo or a story and has no hook,
        # no run folder and no skip rate.
        if item.get("media_product_type") not in (None, "REELS"):
            continue
        key = body_key(item.get("caption") or "")
        if key and key in clashes:
            ambiguous.add(key)
            continue
        run = runs.get(key) if key else None
        if run is None:
            unmatched.append(item)
            continue
        found = _match(run, item, cfg)
        if found is None:
            unmatched.append(item)
            continue
        matched.append(found)

    return Plan(
        matched=tuple(matched),
        unmatched=tuple(unmatched),
        ambiguous=tuple(sorted(ambiguous)),
    )


def apply(
    cfg: Settings,
    matches: tuple[Match, ...] | list[Match],
    *,
    client: httpx.Client | None = None,
) -> list[Match]:
    """Register each match for measurement. Returns the ones that took.

    Safe to run twice. The gateway updates the keyword, the link and a missing
    publish date on a post it already has, and never re-decides whether to poll
    its comments, so a second run cannot disarm the mechanic on a live post.

    **The first refusal stops the rest.** A gateway too old to know about
    `poll_comments` arms its poller instead of ignoring the request, and
    `register_post` can only detect that from the reply, which arrives after the
    row is already written. Stopping there is the difference between one post
    wrongly armed and every post in the account, and the log line says which one
    to go and look at.
    """
    done: list[Match] = []
    for match in matches:
        ok = gateway.register_post(
            match.media_id,
            match.link,
            cfg,
            keyword=match.keyword,
            published_at=match.published_at,
            poll_comments=False,
            client=client,
        )
        if not ok:
            log.error("Stopped after %s. %d left untouched.", match.media_id,
                      len(matches) - len(done) - 1)
            break
        done.append(match)
    if done:
        log.info("Registered %d post(s) for measurement", len(done))
    return done
