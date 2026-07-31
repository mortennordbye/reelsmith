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
from pathlib import Path

import httpx

from config import Settings

log = logging.getLogger(__name__)

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
    """
    bare = repo_name.rsplit("/", 1)[-1]
    first = "".join(c for c in bare.split("-")[0].split("_")[0] if c.isalnum())
    whole = "".join(c for c in bare if c.isalnum())

    for candidate in (first, whole):
        # Two letters is not a word anyone will type deliberately, and it
        # collides with ordinary comment text.
        if len(candidate) >= 3:
            return candidate.upper()[:14]
    return cfg.gateway_keyword.upper()


def add_caption_cta(caption: str, cfg: Settings, *, keyword: str | None = None) -> str:
    """Insert the "comment the keyword" line, above the hashtags.

    Only when the gateway is configured. Telling people to comment a word that
    nothing is listening for is a promise the account cannot keep, and it is the
    kind of thing that reads as a template rather than as a person.

    It goes above the hashtags rather than at the very top, because the first
    line of a caption competes with the hook for the one line Instagram shows
    before "more", and the hook earns that space.

    Obeys the same text rules as everything else: no colons, no dashes, no
    hype.
    """
    if not _configured(cfg):
        return caption

    word = (keyword or cfg.gateway_keyword).strip().upper()
    cta = f"Comment {word} and I will send you the link."
    if cta in caption:
        return caption

    lines = caption.rstrip().splitlines()
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


def register_post(
    media_id: str,
    link: str,
    cfg: Settings,
    *,
    keyword: str | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Tell the gateway to watch this post's comments. True if it took.

    Failing here costs a day of the keyword mechanic on one post, not the post
    itself, and it is recoverable by hand for seven days.
    """
    if not _configured(cfg):
        return False

    url = f"{cfg.gateway_url.rstrip('/')}/api/posts"
    payload = {
        "media_id": media_id,
        "ig_user_id": cfg.ig_user_id,
        "link": link,
        "keyword": keyword or cfg.gateway_keyword,
    }
    try:
        with client or httpx.Client(timeout=_TIMEOUT) as http:
            response = http.post(url, headers=_headers(cfg), json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "Registering %s with the gateway failed: %s. "
            "Comments on it will not be answered until it is registered by hand.",
            media_id,
            exc,
        )
        return False

    log.info("Gateway is watching %s for %r", media_id, payload["keyword"])
    return True
