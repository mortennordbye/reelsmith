"""The tick that turns a queued Reel into a published one.

This is the part that removes the laptop from posting, and it is also the part
where a mistake is expensive: the failure modes are "posted nothing" and "posted
twice", and only one of them can be undone quietly.

**Two claims, in this order.** The slot fire is claimed first, keyed on
(slot, local date), so a restart mid-tick cannot make one evening's slot fire
twice. The post is claimed second, by compare and swap on `state = approved`, so
two ticks racing cannot take the same post. Both are committed before any call
to Meta.

**A failure is only retried when a retry is provably safe.** The dividing line
is whether a container exists. Before that, Meta has not been asked to make
anything, so the fire is handed back and the next tick tries again, which is
what keeps a single dropped connection from costing a day. Once a container id
exists the Reel may already be live, and no amount of error text proves
otherwise, so the row stops in `failed` and waits for a human. This is the same
trade `conversations.claim_comment` makes and for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiosqlite

from gateway import db, publisher, schedule
from gateway.config import GatewaySettings
from gateway.graph import GraphClient
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


def media_url(cfg: GatewaySettings, name: str | None) -> str | None:
    """Where Meta should fetch a queued file from.

    Built at publish time rather than stored, so moving the service to another
    hostname does not strand everything already in the queue.

    Percent-encoded because the name reaches here from a request body. The
    model already refuses path separators, so this is about a name that is
    merely awkward rather than hostile.
    """
    return f"{cfg.public_base_url}/media/{quote(name, safe='')}" if name else None


async def publish_queued(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    queued: Any,
) -> tuple[bool, bool]:
    """Publish one already-claimed row.

    Returns `(published, safe_to_retry)`. The caller uses the second value to
    decide whether the slot gets its turn back; see the module docstring.
    """
    queued_id = int(queued["id"])
    token = account["access_token"]
    ig_user_id = account["ig_user_id"]

    video = media_url(cfg, queued["video_name"])
    if not video:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no video file")
        # Counted like every other failure. Without this the one path that
        # never asks Meta anything was also the one path Prometheus could not
        # see, so a post that died for want of a file was invisible to
        # ReelsmithPublishFailing while every other failure fired it.
        metrics.publish_failures.inc()
        log.error("Queue %d: no video file, nothing to publish", queued_id)
        return False, False

    try:
        container_id = await publisher.create_container(
            graph,
            cfg,
            ig_user_id=ig_user_id,
            token=token,
            video_url=video,
            caption=queued["caption"] or "",
            cover_url=media_url(cfg, queued["cover_name"]),
        )
    except publisher.PublishError as exc:
        # Nothing was created, so this is the one failure worth retrying.
        attempts = int(queued["attempts"] or 0)
        exhausted = attempts >= cfg.max_publish_attempts
        state = db.QUEUE_FAILED if exhausted else db.QUEUE_APPROVED
        await db.set_queue_state(conn, queued_id, state, failure=str(exc))
        metrics.publish_failures.inc()
        log.warning(
            "Queue %d: container creation failed (attempt %d/%d): %s",
            queued_id, attempts, cfg.max_publish_attempts, exc,
        )
        return False, not exhausted

    await db.set_container(conn, queued_id, container_id)

    try:
        await publisher.await_container(graph, cfg, container_id=container_id, token=token)
        result = await publisher.publish_container(
            graph, cfg, ig_user_id=ig_user_id, token=token, container_id=container_id
        )
    except publisher.PublishError as exc:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
        metrics.publish_failures.inc()
        log.error("Queue %d: publish failed after the container existed: %s", queued_id, exc)
        return False, False

    await db.mark_queue_published(
        conn, queued_id, media_id=result.media_id, permalink=result.permalink
    )
    metrics.posts_published.inc()

    # Registering the post is what makes the keyword mechanic work on it. It
    # happens here rather than as a separate call from the Mac because the media
    # id only exists now, and because the Mac may well be asleep.
    await db.register_post(
        conn,
        media_id=result.media_id,
        ig_user_id=ig_user_id,
        keyword=queued["keyword"],
        link=queued["link"],
    )
    log.info(
        "Published queue %d as %s (%s), watching for %r",
        queued_id, result.media_id, result.permalink or "no permalink", queued["keyword"],
    )
    return True, False


async def run_slot(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    slot: schedule.Slot,
    local_day: Any,
) -> bool:
    """Fire one due slot. Returns whether something was published."""
    day_key = local_day.isoformat()
    if not await db.claim_slot_fire(conn, slot_id=slot.id, local_date=day_key):
        return False

    queued = await db.next_approved(conn, account["ig_user_id"])
    if queued is None:
        # An empty queue is normal, not an error. The fire stays claimed so a
        # post approved later today waits for tomorrow's slot rather than going
        # out the moment it is armed, which would be a surprise.
        metrics.slots_starved.inc()
        log.info("Slot %d was due and the queue is empty", slot.id)
        return False

    queued_id = int(queued["id"])
    if not await db.claim_queued(conn, queued_id):
        log.info("Queue %d was taken by another tick", queued_id)
        return False

    await db.attach_fire(conn, slot_id=slot.id, local_date=day_key, queued_id=queued_id)
    # Re-read so `attempts`, just incremented by the claim, is what the publish
    # path sees when it decides whether the retry budget is spent.
    claimed = await db.get_queued(conn, queued_id)
    assert claimed is not None

    published, retry = await publish_queued(
        conn, graph, cfg, metrics, account=account, queued=claimed
    )
    if retry:
        await db.release_slot_fire(conn, slot_id=slot.id, local_date=day_key)
    return published


async def tick_once(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> int:
    """One pass over every account's slots. Returns how many posts went out."""
    moment = db.now()
    published = 0
    # Every platform, not just Instagram. This is the one loop that is about
    # the queue rather than about Meta, and a destination missing from here is
    # a destination that silently stops posting.
    for account in await db.active_accounts(conn, platform=None):
        for row in await db.active_slots(conn, account["ig_user_id"]):
            slot = schedule.Slot.from_row(row)
            local_day = schedule.due(
                slot, moment, grace_minutes=cfg.scheduler_grace_minutes
            )
            if local_day is None:
                continue
            if await run_slot(
                conn, graph, cfg, metrics,
                account=account, slot=slot, local_day=local_day,
            ):
                published += 1
    metrics.scheduler_last_success.set(moment.timestamp())
    return published


async def scheduler_loop(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> None:
    while True:
        try:
            await tick_once(conn, graph, cfg, metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Same rule as the comment poller: a scheduler that throws must not
            # take the webhook receiver down with it.
            log.exception("Scheduler tick failed, continuing")
        await asyncio.sleep(cfg.scheduler_interval_s)


async def upcoming(
    conn: aiosqlite.Connection, cfg: GatewaySettings, ig_user_id: str, *, moment: datetime | None
) -> list[tuple[Any, datetime | None]]:
    """Queued posts paired with when each is expected to go out.

    Pinned posts keep their own time. Everything else is matched against the
    projected slot firings in queue order, which is what makes the queue page
    show "Thursday 18:04" instead of "position 3".
    """
    moment = moment or db.now()
    rows = await db.queued_posts(
        conn, ig_user_id=ig_user_id, states=(db.QUEUE_DRAFT, db.QUEUE_APPROVED)
    )
    slots = [schedule.Slot.from_row(r) for r in await db.active_slots(conn, ig_user_id)]

    # Only armed posts consume a slot. A draft sitting at the head of the queue
    # is not what goes out on Thursday, and showing it that way would be a lie
    # the moment someone forgot to approve it.
    armed = [r for r in rows if r["state"] == db.QUEUE_APPROVED and not r["slot_override"]]
    times = schedule.projected_times(slots, moment, len(armed))

    projected: dict[int, datetime | None] = {}
    for index, row in enumerate(armed):
        projected[int(row["id"])] = times[index] if index < len(times) else None

    out: list[tuple[Any, datetime | None]] = []
    for row in rows:
        if row["slot_override"]:
            out.append((row, db.parse_iso(row["slot_override"])))
        else:
            out.append((row, projected.get(int(row["id"]))))
    return out
