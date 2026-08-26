"""Talking to the DM gateway from the Mac.

Two calls at publish time. The cover goes up first, because Meta fetches
`cover_url` when the container is created and cannot read a local path. The post
is registered afterwards, because the media id does not exist until the publish
succeeds.

**Everything here is best effort and returns rather than raises.** That is the
same rule `render_covers` and `copy_to_clipboard` follow, and the opposite of
`publish_reel`, which raises because a half-finished upload is worth stopping
on. A cluster that is down must never fail a publish that already produced a
video: the cover falls back to `thumb_offset`, and a post that failed to
register can be registered by hand later, while the comment is still inside
Meta's seven day reply window.

Configuring nothing disables all of it. An empty `GATEWAY_URL` is the normal
state for anyone who cloned this repo and does not run the gateway.
"""

from __future__ import annotations

import logging
import re
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from pathlib import Path

import httpx

from config import Settings
from pipeline.models import VisualCue

log = logging.getLogger(__name__)

# The scriptwriter is told the caption gets a call to action appended, but it
# writes its own anyway often enough to matter, and neither its wording nor its
# placement is predictable. Two shapes, both seen for real in one evening:
#
#   a line of its own       "Comment ADHD and I will send you the link."
#   the tail of a paragraph "...patched only afterwards. Comment REVIEW for the link."
_CTA_LINE_RE = re.compile(
    r"^\s*(?:comment\s+[A-Za-z0-9]{2,}|follow\s+(?:for|to|so|us|me|this|the\s+account))\b.*$",
    re.IGNORECASE,
)
# Inside a paragraph the keyword must be capitalised to count, which every
# instance of the ask is. That keeps ordinary prose such as "Comment on the
# issue and the maintainer replies" from being mistaken for one.
#
# The follow half needs the same care for the same reason. "Follow the migration
# guide" and "follow the symlink" are ordinary sentences in this niche, so only
# the handful of words that can only be a request count, and "follow the" is
# deliberately not among them.
_CTA_SENTENCE_RE = re.compile(
    r"\s*(?:Comment\s+[A-Z][A-Z0-9]+|Follow\s+(?:for|so|us|me|this account|the account))"
    r"\b[^.!?\n]*[.!?]"
)

# A run of hashtags closing the last line of prose, so it can be lifted onto a
# line of its own. Anchored at the end, so tags mentioned mid-sentence stay put.
_TRAILING_TAGS_RE = re.compile(r"(\s+#[^\s#]+(?:\s+#[^\s#]+)*)\s*$")

# Words too ordinary to use as the ask. `comment_matches` in the gateway tests
# whole words against the comment text, so a keyword of OPEN on a video about
# an open source reviewer fires on "open source is great" and DMs a link to
# someone who never asked for one. That is the one thing the gateway must not
# do. These are the segment names common enough in repo names to be a real
# risk; the check is only ever applied to the first segment, so a repo actually
# called `open` still gets a usable keyword from its full name.
_TOO_COMMON = {
    "agent", "agents", "ai", "api", "app", "auto", "awesome", "build", "chat",
    "cli", "cloud", "code", "core", "data", "deep", "dev", "docs", "engine",
    "fast", "free", "go", "hub", "kit", "lab", "lite", "live", "llm", "main",
    "mini", "ml", "net", "new", "next", "node", "one", "open", "pro", "py",
    "run", "self", "server", "smart", "stack", "super", "test", "the", "tiny",
    "tool", "tools", "ultra", "web", "zero",
}

# Short. Nothing here is worth making a publish wait, and the publish itself
# already has a much longer clock running.
_TIMEOUT = 20.0


def _configured(cfg: Settings) -> bool:
    return bool(cfg.gateway_url and cfg.gateway_token)


def keyword_for(repo_name: str, cfg: Settings) -> str:
    """The word this post asks people to comment, derived from the repo.

    Not required for correctness: the gateway maps a comment to its post and the
    post to its link, so one shared keyword would already return the right URL
    per Reel. It is worth doing anyway because "Comment GROK" reads as specific
    to this video where "Comment SEND" reads as a template, and this audience
    can tell the difference.

    The first segment of the repo name, so `grok-build` becomes GROK rather than
    GROKBUILD. Short enough to type from memory, which is the whole point of
    asking for it.

    The first segment is skipped when it is an ordinary word, because the ask
    has to be something nobody types by accident. `open-code-review` gives
    OPENCODEREVIEW rather than OPEN, which is longer to type and worth it: OPEN
    fires on "open source is great" and sends a link to someone who was only
    talking.
    """
    bare = repo_name.rsplit("/", 1)[-1]
    first = "".join(c for c in bare.split("-")[0].split("_")[0] if c.isalnum())
    whole = "".join(c for c in bare if c.isalnum())

    if first.lower() in _TOO_COMMON:
        first = ""

    for candidate in (first, whole):
        # Two letters is not a word anyone will type deliberately, and it
        # collides with ordinary comment text.
        if len(candidate) >= 3:
            return candidate.upper()[:14]
    return cfg.gateway_keyword.upper()


def strip_written_cta(text: str) -> str:
    """Remove any ask the model wrote itself, so ours is the only one.

    The prompt tells the scriptwriter that the call to action is appended
    afterwards. It writes one anyway, often enough that both places this text
    reaches a viewer have to defend against it, and in wording and placement
    that vary.

    Heard in a rendered voiceover on alibaba/open-code-review, which read
    "Comment REVIEW and I will send the link. Comment OPENCODEREVIEW if you
    want the link." back to back, asking for two different words in the same
    breath. The caption had the same pair. Fixing one channel and not the
    other is how that shipped, so both now call this.
    """
    # Our own caption ask is two sentences on one line, so the line rule does
    # not see it and the sentence rule would take only its second half. Matched
    # whole and dropped first, which is what keeps `add_caption_cta` idempotent.
    kept = [line for line in text.splitlines() if line.strip() != CAPTION_CTA]
    cleaned = "\n".join(
        _CTA_SENTENCE_RE.sub("", line).rstrip() for line in kept if not _CTA_LINE_RE.match(line)
    )
    return cleaned.strip()


def youtube_description(caption: str, link: str) -> str:
    """The Instagram caption, rewritten for a surface with no DMs.

    The ask does not port, for a different reason than it used to. It was that
    a description saying "comment TENSORFLOW if you want the link" promised a
    private reply YouTube has no way to send. Now the ask is a follow, which
    that surface calls subscribing, so the wording is still wrong even though
    the action exists. Either way the written ask comes out and the repo URL
    goes in directly.

    Here rather than in the gateway, because this is where `strip_written_cta`
    and the wording rules already live, and a gateway that rebuilt the copy
    would be a second place for it to drift. That drift is the exact failure
    `strip_written_cta` was written for.

    The hashtags stay, and stay last. YouTube reads the first three hashtags in
    a description as the ones it shows above the title, and they are the same
    topic words either way.
    """
    lines = [line for line in strip_written_cta(caption).splitlines() if line.strip()]
    tags = lines.pop() if lines and lines[-1].lstrip().startswith("#") else ""
    prose = "\n".join(lines).strip()
    return "\n\n".join(part for part in (prose, f"Repo: {link}", tags) if part)


def strip_cta_cues(cues: list[VisualCue]) -> list[VisualCue]:
    """Take the model's own ask out of the visual cues, the third channel.

    `strip_written_cta` defends the voiceover and the caption. A cue excerpt is
    neither, so it kept the sentence after the other two dropped it, and the
    cost is not cosmetic. An excerpt is matched against the transcript to place
    the scene cut, a stripped sentence is never spoken, and `_align_to_captions`
    abandons the whole video when one cue cannot be found. So a single stray ask
    costs every scene its word level timing and falls back to a proportional
    guess, silently, on a video that looks fine until the cuts drift.

    Seen on `alibaba/open-code-review`, the same run named above, which is why
    this reuses those regexes rather than matching the ask its own way. Fixing
    two channels of three is how that shipped the first time.

    A cue that is nothing but the ask is dropped rather than blanked, because
    the spoken ask already gets a scene of its own in `build_spec`. A cue that
    never had an excerpt keeps its place; `_allocate_frames` gives it a floor
    weight on purpose.
    """
    kept: list[VisualCue] = []
    for cue in cues:
        excerpt = strip_written_cta(cue.spoken_excerpt)
        if excerpt or not cue.spoken_excerpt.strip():
            kept.append(cue.model_copy(update={"spoken_excerpt": excerpt}))
    # Every cue being an ask means the match is wrong rather than the script;
    # a video with no beats at all is worse than one that is timed by guess.
    return kept or cues


# The ask, in the one wording all three channels use. The voice reads it, the
# end card shows it and the caption carries it, so it lives here once.
#
# **It asks for a follow, and it used to ask for a comment.** "Comment SEND if
# you want the link" ran for the account's first 53 posts and drew two comments,
# both from people who unfollowed once the DM arrived. It could not have done
# much else: the thing being traded is a public GitHub URL, which a developer
# can find faster than they can comment and wait, and gating it behind a follow
# selects precisely the follower who leaves when they have it. A tap is the
# cheapest action a viewer can take and the account was never asking for it.
#
# The nightly cadence is the reason given, because the brand name already
# promises it and a reason to come back is what a follow actually is.
SPOKEN_CTA = "Follow for a new one every night."
CAPTION_CTA = "New one every night. Follow so tomorrow's finds you."


def spoken_cta(cfg: Settings) -> str | None:
    """The sentence the voice reads at the end.

    Appended after the scriptwriter rather than requested from it, for two
    reasons. It is the same every time, so spending prompt budget and a
    validation round trip on it buys nothing. And leaving it to the model means
    it is occasionally missing, which quietly breaks the mechanic on that video
    with no error anywhere.

    It lands after the `max_script_words` check on purpose: the limit exists to
    keep Claude terse, and this is not Claude's text.

    **No longer gated on the gateway being configured.** It was, because a video
    telling people to comment a word nothing listens for is a promise the
    account cannot keep. A follow needs nothing listening, so a checkout with no
    gateway now gets the ask too. It still returns `str | None` because
    `pipeline/spec.py` reads None as "no outro scene", and that is the seam for
    turning the ask off.
    """
    return SPOKEN_CTA


def add_caption_cta(caption: str, cfg: Settings) -> str:
    """Insert the follow ask, above the hashtags.

    It goes above the hashtags rather than at the very top, because the first
    line of a caption competes with the hook for the one line Instagram shows
    before "more", and the hook earns that space.

    **Any call to action the model wrote is removed first**, not just a byte
    identical one. The scriptwriter writes its own often enough to matter, in
    its own wording and its own placement, and two asks in one caption is worse
    than either alone.

    Obeys the same text rules as everything else: no colons, no dashes, no
    hype.
    """
    cta = CAPTION_CTA

    lines = strip_written_cta(caption).splitlines()
    # Hashtags are asked for as a trailing block, and the model routinely ends
    # the last sentence with them instead. Line-anchored, that block is
    # invisible, the ask lands underneath it, and the caption reads exactly the
    # way this function exists to prevent. Split them onto a line of their own
    # first, so the ask goes above them and the tags still end the caption.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and not lines[-1].lstrip().startswith("#"):
        run = _TRAILING_TAGS_RE.search(lines[-1])
        if run:
            lines[-1] = lines[-1][: run.start()].rstrip()
            lines.append(run.group(1).strip())
    # Hashtags live in a trailing block. Find where it starts so the call to
    # action lands in the prose rather than after a wall of tags nobody reads.
    first_tag = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("#")), len(lines)
    )
    head, tail = lines[:first_tag], lines[first_tag:]
    while head and not head[-1].strip():
        head.pop()

    return "\n".join([*head, "", cta, *(["", *tail] if tail else [])]).strip() + "\n"


def _headers(cfg: Settings) -> dict[str, str]:
    return {"authorization": f"Bearer {cfg.gateway_token}"}


def _scope(cfg: Settings) -> dict[str, str]:
    """Which account is asking, for every read that can answer for one.

    Left off, the gateway answers for all of them, and until a second account
    existed that was harmless: the Instagram row and the YouTube row are the
    same video for the same repo. With two accounts it is wrong in the
    expensive direction. Account 2's discovery would read account 1's
    commitments as its own and drop those repos, and the two would starve each
    other out of the top of a stars-sorted result set, which is the failure
    that killed two nights of batches in August with only one account doing it
    to itself. F8.

    Empty when no account is selected, which sends no parameter and asks the
    same question this code asked before, so a checkout mid migration is not a
    third behaviour to reason about.
    """
    return {"ig_user_id": cfg.ig_user_id} if cfg.ig_user_id else {}


def _borrow(existing: httpx.Client | None) -> AbstractContextManager[httpx.Client]:
    """Use the caller's client without closing it, or own one and close it.

    The same seam `publisher._client` opens, and here for the reason it warns
    about: everything else in this module makes one call and `with client or
    httpx.Client(...)` closes a borrowed client on the way out, which nothing
    noticed until the backfill became the first caller to register more than
    one post through the same client.
    """
    if existing is not None:
        return nullcontext(existing)
    return httpx.Client(timeout=_TIMEOUT)


def upload_cover(
    cover_path: Path, slug: str, cfg: Settings, *, client: httpx.Client | None = None
) -> str | None:
    """Upload cover.png and return the public URL Meta can fetch, or None.

    None is a normal answer, not an error: it means the caller should let
    `thumb_offset` pick the frame instead, which is the same moment cover.png is
    rendered from, minus the hook band.
    """
    if not _configured(cfg):
        return None
    if not cover_path.exists():
        log.debug("No cover at %s, nothing to upload", cover_path)
        return None

    url = f"{cfg.gateway_url.rstrip('/')}/api/covers"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.post(
                url,
                headers=_headers(cfg),
                files={"file": (cover_path.name, cover_path.read_bytes(), "image/png")},
                data={"slug": slug},
            )
            response.raise_for_status()
            public_url = response.json().get("url")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        log.warning("Cover upload failed, falling back to a video frame: %s", exc)
        return None

    if not public_url:
        log.warning("Cover upload returned no url")
        return None
    log.info("Cover hosted at %s", public_url)
    return public_url


def upload_media(
    path: Path, slug: str, cfg: Settings, *, client: httpx.Client | None = None
) -> str | None:
    """Host any file Meta has to fetch, and return the public URL.

    Unlike the cover, the video is **not** optional. Meta pulls the MP4 from a
    public URL on this API path, so if this returns None there is no publish at
    all. The caller decides how loudly to fail: `_publish_run` raises for the
    video and shrugs for the cover.
    """
    if not _configured(cfg):
        return None
    if not path.exists():
        log.debug("Nothing to upload at %s", path)
        return None

    url = f"{cfg.gateway_url.rstrip('/')}/api/media"
    mime = "video/mp4" if path.suffix.lower() == ".mp4" else "image/png"
    try:
        # Videos are tens of MB and the upload crosses the internet to the
        # cluster, so this gets its own, longer clock.
        with client or httpx.Client(timeout=cfg.ig_upload_timeout_s) as http:
            response = http.post(
                url,
                headers=_headers(cfg),
                files={"file": (path.name, path.read_bytes(), mime)},
                data={"slug": slug},
            )
            response.raise_for_status()
            public_url = response.json().get("url")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        log.warning("Hosting %s failed: %s", path.name, exc)
        return None

    if public_url:
        log.info("Hosting %s at %s", path.name, public_url)
    return public_url


def enqueue(
    video_name: str,
    link: str,
    cfg: Settings,
    *,
    caption: str = "",
    keyword: str | None = None,
    cover_name: str | None = None,
    repo_full_name: str | None = None,
    approved: bool = False,
    client: httpx.Client | None = None,
    account: str | None = None,
    title: str = "",
    recipe: str = "",
    hook: str = "",
) -> dict | None:
    """Hand a rendered video to the gateway's schedule. None means it did not take.

    `account` picks the destination and defaults to the Instagram one. A
    YouTube channel is another account row with its own queue and its own
    slots, so the same video going to both is two calls here rather than one
    call with a list: the two rows are approved, reordered, held and cancelled
    independently, and one failing must not take the other with it.

    `title` is required by YouTube and meaningless on Instagram.

    `recipe` travels with the video because it cannot be reconstructed later.
    It fingerprints the checkout and the settings that wrote the script, and it
    is written into the run folder on whichever machine rendered. The numbers
    live on the gateway, so without sending it the only join is the repo name
    against a build folder on the machine that happens to be asking, which is
    wrong rather than missing once two machines render.

    `hook` travels for the same reason and matters more. It is what the feedback
    loop reads back into the next prompt, so looking it up on the asking machine
    means the loop can argue from an opening that was never on a video.

    The odd one out in this module: everything else here shrugs on failure
    because a publish has already happened and the video exists either way.
    This one *is* the publish, so the caller treats None as a failure and says
    so. It still returns rather than raising, because the caller is the one that
    knows whether the cooldown has been started yet.

    Filenames rather than URLs, because `/api/media` already stored them and a
    URL baked into a row that sits in a queue for a week would rot with the
    hostname.
    """
    if not _configured(cfg):
        return None

    url = f"{cfg.gateway_url.rstrip('/')}/api/queue"
    payload = {
        "ig_user_id": account or cfg.ig_user_id,
        "video_name": video_name,
        "cover_name": cover_name,
        "caption": caption,
        "keyword": keyword or cfg.gateway_keyword,
        "link": link,
        "repo_full_name": repo_full_name,
        "approved": approved,
        "title": title,
        "recipe": recipe,
        "hook": hook,
    }
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.post(url, headers=_headers(cfg), json=payload)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f" ({exc.response.text[:200]})"
        log.warning("Queueing %s with the gateway failed: %s%s", video_name, exc, detail)
        return None


def fetch_queue(cfg: Settings, *, client: httpx.Client | None = None) -> list[dict] | None:
    """What the gateway already holds, so a second push can be refused.

    None means the question could not be asked, which is different from "the
    queue is empty" and is why the caller must not read it as a green light.
    """
    if not _configured(cfg):
        return None
    url = f"{cfg.gateway_url.rstrip('/')}/api/queue"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.get(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
            return list(response.json().get("queue") or [])
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not read the gateway queue: %s", exc)
        return None


def fetch_results(cfg: Settings, *, client: httpx.Client | None = None) -> list[dict]:
    """How the account's own published Reels did, newest first.

    An empty list for every failure, and for "no gateway configured", because
    the caller is a prompt builder rather than a guard. Nothing here is worth
    stopping a render over: the worst case is the script gets written the way
    every script before it was written, with no idea what happened last time.
    That is a step backwards from good, not a broken run.
    """
    if not _configured(cfg):
        return []
    url = f"{cfg.gateway_url.rstrip('/')}/api/results"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.get(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
            return list(response.json().get("results") or [])
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not read the gateway results: %s", exc)
        return []


def fetch_covered(cfg: Settings, *, client: httpx.Client | None = None) -> dict[str, str]:
    """Repos the gateway has seen committed, as `owner/repo` to an ISO date.

    The dates come back as timestamps and are cut to a date here, because the
    cooldown is counted in days and `UsedRepos` stores `YYYY-MM-DD`.

    An empty dict for every failure, matching `fetch_results`. A gateway that is
    down costs discovery nothing: the local store is the one it already reads,
    and this only ever adds to it.
    """
    if not _configured(cfg):
        return {}
    url = f"{cfg.gateway_url.rstrip('/')}/api/covered"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.get(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
            rows = response.json().get("covered") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not read the gateway's covered repos: %s", exc)
        return {}

    out: dict[str, str] = {}
    for row in rows:
        name, covered_at = row.get("repo_full_name"), row.get("covered_at")
        if not name or not covered_at:
            continue
        out[name] = str(covered_at)[:10]
    return out


def fetch_pending_count(cfg: Settings, *, client: httpx.Client | None = None) -> int | None:
    """How many posts are waiting to go out, drafts and armed together.

    Both count. A draft is a post somebody still has to watch, and the reason
    to stop rendering is that the line is long, not that it is armed.

    None rather than 0 when the gateway cannot be reached, because the two mean
    opposite things to the caller: 0 invites a batch, and unknown must not.

    **Only the Instagram queue counts**, and that is the whole point of the
    filter. One render now makes two rows, so counting every destination would
    halve the ceiling the nightly was calibrated against. Worse, YouTube drains
    one a day against Instagram's three, so its queue grows on purpose and by
    design: counted here it would climb past `--max-queue` on its own and pin
    the batch at zero renders, permanently and with every component reporting
    healthy.

    The ceiling asks "is the feed stocked far enough ahead", and the feed is
    Instagram. A second destination running deliberately deep behind it is not
    a reason to stop making videos.
    """
    if not _configured(cfg):
        return None
    url = f"{cfg.gateway_url.rstrip('/')}/api/queue"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.get(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
            rows = response.json().get("queue") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not read the gateway queue: %s", exc)
        return None
    return sum(1 for row in rows if row.get("state") in {"draft", "approved"})


def fetch_rendered(cfg: Settings, *, client: httpx.Client | None = None) -> dict[str, str]:
    """Repos a Reel has already been built for, as `owner/repo` to an ISO date.

    Read before discovery alongside `fetch_covered`, and kept apart from it on
    purpose. A covered repo is a commitment and gets merged into
    `data/used_repos.json`, which is what the 30 day cooldown is counted from.
    A rendered repo is only "this video already exists", so it is dropped for
    today and never written down; `--unmark` or deleting the row is enough to
    bring it back.

    An empty dict for every failure, matching `fetch_covered`. A gateway that
    is down means a repo might get rendered twice, which is the behaviour this
    project had before the endpoint existed.
    """
    if not _configured(cfg):
        return {}
    url = f"{cfg.gateway_url.rstrip('/')}/api/rendered"
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.get(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
            rows = response.json().get("rendered") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Could not read the gateway's rendered repos: %s", exc)
        return {}

    out: dict[str, str] = {}
    for row in rows:
        name, rendered_at = row.get("repo_full_name"), row.get("rendered_at")
        if not name or not rendered_at:
            continue
        out[name] = str(rendered_at)[:10]
    return out


def register_rendered(
    repo_full_name: str,
    cfg: Settings,
    *,
    run_folder: str = "",
    score: float = 0.0,
    score_breakdown: dict[str, float] | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Tell the gateway a Reel for this repo now exists. True if it took.

    Sent when a render finishes, which is the earliest and weakest thing this
    module reports. It starts no cooldown and takes no slot; it only stops the
    next discovery pass ranking a repo whose video is already on disk.

    The score rides along because this is the only message sent about the pick
    itself. `score_candidates` splits it into velocity, stars, Hacker News and
    README quality and writes the lot into `repo.json`, where it has never left
    the machine that ranked, so nothing anywhere could answer why discovery
    keeps landing on the same corner of GitHub.

    Best effort, like everything else here. A gateway that is down means the
    duplicate guard falls back to the local store, which is exactly how
    discovery behaved before this existed.
    """
    if not _configured(cfg):
        return False

    url = f"{cfg.gateway_url.rstrip('/')}/api/rendered"
    payload = {
        "repo_full_name": repo_full_name,
        "ig_user_id": cfg.ig_user_id or "",
        "run_folder": run_folder,
        "score": score,
        "score_breakdown": score_breakdown or {},
    }
    try:
        with _borrow(client) as http:
            response = http.post(url, headers=_headers(cfg), json=payload)
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        # Debug, not warning. The consequence is one repeated render on a later
        # day, and the run that just succeeded is not the place to shout about
        # it. `--covered` is where a missing record actually shows up.
        log.debug("Could not record the render of %s with the gateway: %s", repo_full_name, exc)
        return False

    log.info("Gateway knows %s is rendered, so discovery will not rebuild it", repo_full_name)
    return True


def forget_rendered(
    repo_full_name: str, cfg: Settings, *, client: httpx.Client | None = None
) -> bool:
    """Take back the render record, so a rejected video stops blocking its repo.

    The other half of `--unmark`. Rendering is the one step meant to be free to
    throw away, so undoing the record of it has to be as cheap as deleting the
    folder.
    """
    if not _configured(cfg):
        return False

    url = f"{cfg.gateway_url.rstrip('/')}/api/rendered/{repo_full_name.strip('/')}"
    try:
        with _borrow(client) as http:
            response = http.delete(url, headers=_headers(cfg), params=_scope(cfg))
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "Could not clear the gateway's render record for %s: %s. "
            "Discovery may keep skipping it; clear it by hand.",
            repo_full_name,
            exc,
        )
        return False
    return True


def register_post(
    media_id: str,
    link: str,
    cfg: Settings,
    *,
    keyword: str | None = None,
    published_at: datetime | None = None,
    poll_comments: bool = True,
    client: httpx.Client | None = None,
) -> bool:
    """Tell the gateway to watch this post's comments. True if it took.

    Failing here costs a day of the keyword mechanic on one post, not the post
    itself, and it is recoverable by hand for seven days.

    `poll_comments=False` registers a post to be measured and never answered,
    which is what a backfill wants: see `pipeline/backfill.py`. `published_at`
    goes with it, because a post registered after the fact would otherwise be
    dated the moment somebody noticed it was missing.
    """
    if not _configured(cfg):
        return False

    url = f"{cfg.gateway_url.rstrip('/')}/api/posts"
    payload: dict[str, object] = {
        "media_id": media_id,
        "ig_user_id": cfg.ig_user_id,
        "link": link,
        "keyword": keyword or cfg.gateway_keyword,
        "poll_comments": poll_comments,
    }
    if published_at is not None:
        payload["published_at"] = published_at.isoformat()
    try:
        with _borrow(client) as http:
            response = http.post(url, headers=_headers(cfg), json=payload)
            response.raise_for_status()
            detail = str((response.json() or {}).get("detail", ""))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "Registering %s with the gateway failed: %s. "
            "Comments on it will not be answered until it is registered by hand.",
            media_id,
            exc,
        )
        return False

    # A gateway older than schema v8 has never heard of `poll_comments`, and
    # pydantic drops a field a model does not declare rather than complaining.
    # So asking an old one to measure a post quietly arms its comment poller
    # instead, and the first anyone would know is a DM going out about a Reel
    # from last week. The reply is the only evidence available, and it names
    # which of the two things it did.
    if not poll_comments and not detail.startswith("measuring"):
        log.error(
            "Asked the gateway to measure %s without polling it, and it answered %r. "
            "That gateway predates the flag and has armed the comment poller instead. "
            "Deploy the current image before backfilling.",
            media_id,
            detail,
        )
        return False

    # Says which of the two things was asked for. The guard above already
    # proved the gateway agreed, and a backfill printing "watching" for eight
    # old posts reads exactly like the failure that guard exists to catch.
    if poll_comments:
        log.info("Gateway is watching %s for %r", media_id, payload["keyword"])
    else:
        log.info("Gateway is measuring %s, not watching its comments", media_id)
    return True
