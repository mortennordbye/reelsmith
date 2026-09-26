"""Publishing a Reel, from the cluster instead of the laptop.

An async port of the sequence in `pipeline/publisher.py`, which stays where it
is and still works: `--post` and `--publish` are the reviewed path and are not
going anywhere. This is the same three calls for the unattended one.

The one fact that shapes everything: **Meta fetches the video, it is never
pushed.** `upload_type=resumable` takes raw bytes only on the Facebook Login for
Business path; on Instagram Login `graph.instagram.com` answers "The parameter
video_url is required". So a public URL has to exist before the container is
created, which this service was already providing for the laptop. Publishing
from here just removes the round trip.

Split from `graph.py` on purpose. That file is the DM mechanic and is called on
every webhook; this one is called once a day and blocks for half a minute while
Meta transcodes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from gateway.config import GatewaySettings
from gateway.graph import GraphClient, GraphError

log = logging.getLogger(__name__)

# Terminal states from GET /<container-id>?fields=status_code.
_STATUS_DONE = "FINISHED"
_STATUS_WAIT = "IN_PROGRESS"
_STATUS_PUBLISHED = "PUBLISHED"

# How many times to ask a FINISHED container whether it has since been
# published, after `media_publish` failed. Meta can publish in the same second
# the call times out, so the first answer may not be the last.
_PUBLISHED_RECHECKS = 3
# How far back in the account's media to look for the one a container became.
_RECENT_MEDIA = 10


class PublishError(RuntimeError):
    """A publish that did not complete.

    `container_created` is the field the caller acts on, not the message. False
    means Meta was never asked to make anything and a retry is provably safe.
    True means a Reel may exist, and the only safe move is to stop and let a
    human look.
    """

    def __init__(self, message: str, *, container_created: bool = False):
        super().__init__(message)
        self.container_created = container_created


@dataclass(frozen=True)
class PublishResult:
    media_id: str
    permalink: str | None = None
    container_id: str | None = None


async def create_container(
    graph: GraphClient,
    cfg: GatewaySettings,
    *,
    ig_user_id: str,
    token: str,
    video_url: str,
    caption: str,
    cover_url: str | None = None,
) -> str:
    """Ask Meta to fetch the video. Returns the container id."""
    params: dict[str, str] = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption.strip(),
    }
    if cover_url:
        params["cover_url"] = cover_url
    else:
        params["thumb_offset"] = str(cfg.cover_thumb_offset_ms)

    try:
        data = await graph.request(
            "POST", f"{cfg.graph_base}/{ig_user_id}/media", token=token, params=params
        )
    except GraphError as exc:
        raise PublishError(f"Container creation failed: {exc}", container_created=False) from exc

    container_id = str(data.get("id") or "")
    if not container_id:
        raise PublishError(
            f"Container creation returned no id: {data}", container_created=False
        )
    return container_id


async def await_container(
    graph: GraphClient, cfg: GatewaySettings, *, container_id: str, token: str
) -> None:
    """Poll until Meta has finished transcoding, or give up loudly.

    Every failure past this point reports `container_created=True`. The
    container itself stays valid for 24 hours, so a timeout is recoverable by
    hand rather than lost.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.publish_timeout_s
    seen = ""
    while loop.time() < deadline:
        try:
            data = await graph.request(
                "GET",
                f"{cfg.graph_base}/{container_id}",
                token=token,
                params={"fields": "status_code,status"},
            )
        except GraphError as exc:
            raise PublishError(
                f"Could not read container {container_id}: {exc}", container_created=True
            ) from exc

        status = str(data.get("status_code") or "")
        if status != seen:
            log.info("Container %s: %s", container_id, status or "?")
            seen = status
        if status == _STATUS_DONE:
            return
        if status and status != _STATUS_WAIT:
            # ERROR and EXPIRED both land here; `status` carries the prose
            # reason, which is the only thing that says which one it was.
            detail = f"Container {container_id} is {status}: {data.get('status', '')}"
            if status in ("ERROR", "EXPIRED"):
                # Terminal at Meta: a container in either state is never
                # published, and `media_publish` was never called for it. The
                # sentence is what the panel reads to offer a clean retry.
                detail += (
                    ". Meta rejected the upload while processing it, so nothing was published."
                )
            raise PublishError(detail, container_created=True)
        await asyncio.sleep(cfg.publish_poll_interval_s)

    raise PublishError(
        f"Container {container_id} was still processing after {cfg.publish_timeout_s}s. "
        f"It stays valid for 24 hours, so it can still be published by hand.",
        container_created=True,
    )


async def publish_container(
    graph: GraphClient,
    cfg: GatewaySettings,
    *,
    ig_user_id: str,
    token: str,
    container_id: str,
    caption: str = "",
) -> PublishResult:
    """Publish a finished container.

    A failed `media_publish` is not proof that nothing went out. On
    2026-09-22 the call timed out and Meta published the Reel in the same
    second, so before reporting a failure this asks the container whether it
    was published and, if it was, finds the media by its caption.
    """
    try:
        data = await graph.request(
            "POST",
            f"{cfg.graph_base}/{ig_user_id}/media_publish",
            token=token,
            params={"creation_id": container_id},
        )
    except GraphError as exc:
        status, found = await _published_anyway(
            graph, cfg, ig_user_id=ig_user_id, token=token,
            container_id=container_id, caption=caption,
        )
        if found is not None:
            log.warning(
                "media_publish for container %s failed (%s) but Meta published it as %s",
                container_id, exc, found,
            )
            return PublishResult(
                media_id=found,
                permalink=await permalink(graph, cfg, media_id=found, token=token),
                container_id=container_id,
            )
        detail = f"media_publish failed: {exc}"
        if status == _STATUS_PUBLISHED:
            # Live, and nothing here knows its media id. Saying so is what
            # stops somebody retrying it into a duplicate.
            detail += (
                f". Meta reports container {container_id} as PUBLISHED, so the Reel"
                " is live; find it on the account rather than retrying."
            )
        raise PublishError(detail, container_created=True) from exc

    media_id = str(data.get("id") or "")
    if not media_id:
        raise PublishError(
            f"media_publish returned no id: {data}", container_created=True
        )
    return PublishResult(
        media_id=media_id,
        permalink=await permalink(graph, cfg, media_id=media_id, token=token),
        container_id=container_id,
    )


async def permalink(
    graph: GraphClient, cfg: GatewaySettings, *, media_id: str, token: str
) -> str | None:
    """The post's URL, for the log line and the admin UI.

    Never worth failing a publish over: by the time this runs the Reel is
    already live.
    """
    try:
        data = await graph.request(
            "GET",
            f"{cfg.graph_base}/{media_id}",
            token=token,
            params={"fields": "permalink"},
        )
    except GraphError as exc:
        log.debug("Could not read permalink for %s (%s)", media_id, exc)
        return None
    return data.get("permalink")


async def _published_anyway(
    graph: GraphClient,
    cfg: GatewaySettings,
    *,
    ig_user_id: str,
    token: str,
    container_id: str,
    caption: str,
) -> tuple[str, str | None]:
    """The container's status, and the media id it became if it can be shown.

    No media id whenever it cannot be shown, which leaves the caller on the old path:
    the row fails with its container recorded and a human decides. A container
    does not name the media it became, so the media is found by its caption,
    which is the one thing this service wrote and Meta stores verbatim.
    """
    status = ""
    for attempt in range(_PUBLISHED_RECHECKS):
        if attempt:
            await asyncio.sleep(cfg.publish_poll_interval_s)
        try:
            data = await graph.request(
                "GET",
                f"{cfg.graph_base}/{container_id}",
                token=token,
                params={"fields": "status_code"},
            )
        except GraphError as exc:
            log.warning("Could not re-read container %s: %s", container_id, exc)
            return status, None
        status = str(data.get("status_code") or "")
        if status != _STATUS_DONE:
            break
    if status != _STATUS_PUBLISHED:
        return status, None

    wanted = caption.strip()
    if not wanted:
        log.error("Container %s is published but has no caption to find it by", container_id)
        return status, None
    try:
        data = await graph.request(
            "GET",
            f"{cfg.graph_base}/{ig_user_id}/media",
            token=token,
            params={"fields": "id,caption", "limit": str(_RECENT_MEDIA)},
        )
    except GraphError as exc:
        log.error("Container %s is published but its media could not be listed: %s",
                  container_id, exc)
        return status, None
    # Newest first, so a repeated caption resolves to the post just made.
    for item in data.get("data") or []:
        if str(item.get("caption") or "").strip() == wanted and item.get("id"):
            return status, str(item["id"])
    log.error("Container %s is published but no recent media carries its caption",
              container_id)
    return status, None
