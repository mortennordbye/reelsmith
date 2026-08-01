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
            raise PublishError(
                f"Container {container_id} is {status}: {data.get('status', '')}",
                container_created=True,
            )
        await asyncio.sleep(cfg.publish_poll_interval_s)

    raise PublishError(
        f"Container {container_id} was still processing after {cfg.publish_timeout_s}s. "
        f"It stays valid for 24 hours, so it can still be published by hand.",
        container_created=True,
    )


async def publish_container(
    graph: GraphClient, cfg: GatewaySettings, *, ig_user_id: str, token: str, container_id: str
) -> PublishResult:
    try:
        data = await graph.request(
            "POST",
            f"{cfg.graph_base}/{ig_user_id}/media_publish",
            token=token,
            params={"creation_id": container_id},
        )
    except GraphError as exc:
        raise PublishError(f"media_publish failed: {exc}", container_created=True) from exc

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
