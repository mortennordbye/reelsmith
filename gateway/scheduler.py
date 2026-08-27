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
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite

from gateway import db, facebook, publisher, schedule, tiktok, youtube
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
    """Publish one already-claimed row, wherever it is going.

    Returns `(published, safe_to_retry)`. The caller uses the second value to
    decide whether the slot gets its turn back; see the module docstring.
    """
    platform = str(account["platform"] or "")
    publisher_for = {
        db.PLATFORM_INSTAGRAM: publish_queued_instagram,
        db.PLATFORM_YOUTUBE: publish_queued_youtube,
        db.PLATFORM_TIKTOK: publish_queued_tiktok,
        db.PLATFORM_FACEBOOK: publish_queued_facebook,
    }.get(platform)

    if publisher_for is None:
        # **Instagram used to be the fallthrough of this branch**, which meant a
        # row for a platform with no publisher was handed to Meta: a Reels
        # container created against a TikTok open id with an empty access
        # token, against a live account. That is the opposite of the fail safe
        # design in `db.active_accounts`, where the readers default to
        # Instagram precisely so a missed call site is inert. Here the same
        # default was fail open. F1.
        #
        # Failing the row rather than the tick, so one misconfigured account
        # cannot stop the other two publishing.
        await db.set_queue_state(
            conn,
            int(queued["id"]),
            db.QUEUE_FAILED,
            failure=f"no publisher for platform {platform!r}",
        )
        metrics.publish_failures.labels(platform=platform or "unknown").inc()
        log.error(
            "Queue %d: account %s is on platform %r, which has no publisher. "
            "The row is failed rather than sent to another platform's API.",
            int(queued["id"]), account["account_id"], platform,
        )
        return False, False

    return await publisher_for(conn, graph, cfg, metrics, account=account, queued=queued)


async def publish_queued_youtube(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    queued: Any,
) -> tuple[bool, bool]:
    """Upload one claimed row to YouTube.

    The same two-step shape as the Instagram path and for the same reason.
    `youtube.upload` would do the whole sequence in one call, but then a
    process that died mid-transfer would leave a row in `claimed` with nothing
    recorded, and the only question worth answering afterwards is whether
    anything was created. So the session URI is opened and **committed** first,
    into the same `container_id` column and meaning the same thing, and only
    then do the bytes go up.
    """
    queued_id = int(queued["id"])
    channel_id = account["account_id"]

    credentials = await db.youtube_credentials(conn, channel_id)
    if credentials is None:
        # An account row with no credentials cannot be fixed by trying again.
        await db.set_queue_state(
            conn, queued_id, db.QUEUE_FAILED, failure="no YouTube credentials stored"
        )
        metrics.publish_failures.labels(platform=db.PLATFORM_YOUTUBE).inc()
        log.error("Queue %d: channel %s has no stored credentials", queued_id, channel_id)
        return False, False

    video_path = Path(cfg.covers_dir) / str(queued["video_name"] or "")
    if not queued["video_name"] or not video_path.is_file():
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no video file")
        metrics.publish_failures.labels(platform=db.PLATFORM_YOUTUBE).inc()
        log.error("Queue %d: no video at %s", queued_id, video_path)
        return False, False

    title = str(queued["title"] or "").strip()
    if not title:
        # YouTube rejects an empty title, and a video called after its filename
        # is worse than one that waits for a person.
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no title")
        metrics.publish_failures.labels(platform=db.PLATFORM_YOUTUBE).inc()
        log.error("Queue %d: a YouTube row needs a title", queued_id)
        return False, False

    size_bytes = video_path.stat().st_size
    try:
        token = await youtube.access_token(
            graph.http,
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            refresh_token=credentials["refresh_token"],
        )
        session_uri = await youtube.start_session(
            graph.http,
            token=token,
            title=title,
            description=str(queued["caption"] or ""),
            size_bytes=size_bytes,
            privacy_status=cfg.youtube_privacy_status,
            contains_synthetic_media=cfg.youtube_synthetic_media,
        )
    except youtube.UploadError as exc:
        # No session exists, so Google was never asked to make anything and
        # this is the one failure worth retrying.
        attempts = int(queued["attempts"] or 0)
        exhausted = attempts >= cfg.max_publish_attempts
        state = db.QUEUE_FAILED if exhausted else db.QUEUE_APPROVED
        await db.set_queue_state(conn, queued_id, state, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_YOUTUBE).inc()
        log.warning(
            "Queue %d: no upload session (attempt %d/%d): %s",
            queued_id, attempts, cfg.max_publish_attempts, exc,
        )
        return False, not exhausted

    await db.set_container(conn, queued_id, session_uri)

    try:
        result = await youtube.push_bytes(
            graph.http,
            session_uri=session_uri,
            video_path=video_path,
            size_bytes=size_bytes,
            timeout_s=cfg.youtube_upload_timeout_s,
        )
    except youtube.UploadError as exc:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_YOUTUBE).inc()
        log.error("Queue %d: upload failed after the session existed: %s", queued_id, exc)
        return False, False

    await db.mark_queue_published(
        conn, queued_id, media_id=result.video_id, permalink=result.url
    )
    metrics.posts_published.labels(platform=db.PLATFORM_YOUTUBE).inc()

    # No `register_post` here, unlike the Instagram path. That call arms the
    # comment poller and the insights sweep, both of which are Meta-only, and a
    # YouTube video id sitting in `posts` is exactly the row those loops must
    # never pick up.
    log.info(
        "Uploaded queue %d to %s as %s (%s)",
        queued_id, channel_id, result.video_id, result.privacy_status,
    )
    if result.privacy_status != cfg.youtube_privacy_status:
        # Measured as not applying to this project on 2026-08-16, so if it ever
        # starts, that is news and worth a line naming the likely cause rather
        # than a silent downgrade. Compares against what was asked for rather
        # than against "private", so any downgrade is caught and not just the
        # one failure mode that was expected.
        log.warning(
            "Queue %d asked for %s and got %s. The usual cause is the audit "
            "restriction on an API project, which lives on the project rather "
            "than the video, so nothing here or in Studio would change it.",
            queued_id, cfg.youtube_privacy_status, result.privacy_status,
        )
    return True, False


async def publish_queued_tiktok(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    queued: Any,
) -> tuple[bool, bool]:
    """Publish one claimed row to TikTok, on whichever path is configured.

    The same two-step shape as the other two, and the same line drawn in the
    same place: `publish_id` is this path's container id. Before
    `video/init/` returns, TikTok was never asked to make anything and the slot
    gets its turn back. After it, a post may exist and only a person decides.

    **The media is fetched, not pushed**, so this is Meta's shape rather than
    YouTube's and `media_url` is the same seam. What is different is the token:
    a TikTok access token lasts 24 hours, so it is minted per publish out of a
    refresh token the refresher loop keeps alive, and the rotated refresh token
    is persisted before the publish is attempted.
    """
    queued_id = int(queued["id"])
    open_id = account["account_id"]

    if not cfg.tiktok_enabled:
        # The same decision `scheduler_enabled` makes one level up: publishing
        # to a third real account is a choice rather than something gained by
        # upgrading. Failing the row rather than retrying it, because a flag
        # that is off is not a transient condition and a row retrying against
        # it forever would look like an API problem.
        await db.set_queue_state(
            conn, queued_id, db.QUEUE_FAILED,
            failure="TikTok publishing is off (GATEWAY_TIKTOK_ENABLED)",
        )
        metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
        log.error(
            "Queue %d is for TikTok and GATEWAY_TIKTOK_ENABLED is false. "
            "Nothing was sent. Turn it on, or cancel the row.",
            queued_id,
        )
        return False, False

    stored = await db.tiktok_credentials(conn, open_id)
    if stored is None:
        # An account row with no credentials cannot be fixed by trying again.
        await db.set_queue_state(
            conn, queued_id, db.QUEUE_FAILED, failure="no TikTok credentials stored"
        )
        metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
        log.error("Queue %d: TikTok account %s has no stored credentials", queued_id, open_id)
        return False, False

    video = media_url(cfg, queued["video_name"])
    if not video:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no video file")
        metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
        log.error("Queue %d: no video file, nothing to publish", queued_id)
        return False, False

    credentials = tiktok.Credentials(
        open_id=open_id,
        client_key=stored["client_key"],
        client_secret=stored["client_secret"],
        refresh_token=stored["refresh_token"],
    )
    try:
        fresh = await tiktok.refresh_access_token(graph.http, credentials=credentials)
        # Before the publish, not after. The refresh token just spent is dead
        # and a publish that throws must not take the new one with it.
        await db.save_tiktok_refresh(
            conn, open_id, fresh.refresh_token, fresh.refresh_expires_in
        )

        privacy = cfg.tiktok_privacy_level
        if cfg.tiktok_direct_post:
            # Mandatory rather than advisory, and the privacy level has to come
            # from what it returns or the post fails
            # `privacy_level_option_mismatch`, which reads like a bad constant
            # and is actually a stale read.
            info = await tiktok.creator_info(graph.http, token=fresh.access_token)
            if privacy not in info.privacy_level_options:
                await db.set_queue_state(
                    conn,
                    queued_id,
                    db.QUEUE_FAILED,
                    failure=(
                        f"{privacy} is not offered by this account; it allows "
                        f"{', '.join(info.privacy_level_options) or 'nothing'}"
                    ),
                )
                metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
                log.error(
                    "Queue %d: %s asked for %s and the account offers %s",
                    queued_id, open_id, privacy, info.privacy_level_options,
                )
                return False, False

        publish_id = await tiktok.start_publish(
            graph.http,
            token=fresh.access_token,
            video_url=video,
            title=str(queued["title"] or queued["caption"] or ""),
            direct_post=cfg.tiktok_direct_post,
            privacy_level=privacy,
            is_aigc=cfg.tiktok_is_aigc,
        )
    except tiktok.PublishError as exc:
        if exc.publish_started:
            await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
            metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
            log.error("Queue %d: TikTok failed after a publish existed: %s", queued_id, exc)
            return False, False
        attempts = int(queued["attempts"] or 0)
        exhausted = attempts >= cfg.max_publish_attempts
        state = db.QUEUE_FAILED if exhausted else db.QUEUE_APPROVED
        await db.set_queue_state(conn, queued_id, state, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
        log.warning(
            "Queue %d: TikTok refused before anything existed (attempt %d/%d): %s",
            queued_id, attempts, cfg.max_publish_attempts, exc,
        )
        return False, not exhausted

    await db.set_container(conn, queued_id, publish_id)

    try:
        result = await tiktok.await_publish(
            graph.http,
            token=fresh.access_token,
            publish_id=publish_id,
            direct_post=cfg.tiktok_direct_post,
            poll_interval_s=cfg.tiktok_poll_interval_s,
            timeout_s=cfg.tiktok_publish_timeout_s,
        )
    except tiktok.PublishError as exc:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_TIKTOK).inc()
        log.error("Queue %d: TikTok publish did not finish: %s", queued_id, exc)
        return False, False

    # No permalink. `status/fetch` reports the post completed and hands back no
    # id and no URL, and the video id the insights sweep needs is not the
    # publish id, so finding it means listing the account's recent videos and
    # matching. That is phase 7 and it does not belong in the publish path.
    await db.mark_queue_published(conn, queued_id, media_id=publish_id, permalink="")
    metrics.posts_published.labels(platform=db.PLATFORM_TIKTOK).inc()

    # No `register_post`, for the same reason the YouTube path has none: that
    # call arms the comment poller and the insights sweep, both Meta-only.
    log.info(
        "Published queue %d to TikTok %s as %s (%s)",
        queued_id, open_id, publish_id, result.status,
    )
    if result.in_inbox:
        log.info(
            "Queue %d is in the creator's TikTok inbox and needs one tap in the app. "
            "This is the unaudited path, which is what GATEWAY_TIKTOK_DIRECT_POST "
            "being off means.",
            queued_id,
        )
    return True, False


async def publish_queued_instagram(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    queued: Any,
) -> tuple[bool, bool]:
    """Publish one already-claimed row to Instagram."""
    queued_id = int(queued["id"])
    token = account["access_token"]
    account_id = account["account_id"]

    video = media_url(cfg, queued["video_name"])
    if not video:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no video file")
        # Counted like every other failure. Without this the one path that
        # never asks Meta anything was also the one path Prometheus could not
        # see, so a post that died for want of a file was invisible to
        # ReelsmithPublishFailing while every other failure fired it.
        metrics.publish_failures.labels(platform=db.PLATFORM_INSTAGRAM).inc()
        log.error("Queue %d: no video file, nothing to publish", queued_id)
        return False, False

    try:
        container_id = await publisher.create_container(
            graph,
            cfg,
            ig_user_id=account_id,
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
        metrics.publish_failures.labels(platform=db.PLATFORM_INSTAGRAM).inc()
        log.warning(
            "Queue %d: container creation failed (attempt %d/%d): %s",
            queued_id, attempts, cfg.max_publish_attempts, exc,
        )
        return False, not exhausted

    await db.set_container(conn, queued_id, container_id)

    try:
        await publisher.await_container(graph, cfg, container_id=container_id, token=token)
        result = await publisher.publish_container(
            graph, cfg, ig_user_id=account_id, token=token, container_id=container_id
        )
    except publisher.PublishError as exc:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_INSTAGRAM).inc()
        log.error("Queue %d: publish failed after the container existed: %s", queued_id, exc)
        return False, False

    await db.mark_queue_published(
        conn, queued_id, media_id=result.media_id, permalink=result.permalink
    )
    metrics.posts_published.labels(platform=db.PLATFORM_INSTAGRAM).inc()

    # Registering the post is what makes the keyword mechanic work on it. It
    # happens here rather than as a separate call from the Mac because the media
    # id only exists now, and because the Mac may well be asleep.
    await db.register_post(
        conn,
        media_id=result.media_id,
        account_id=account_id,
        keyword=queued["keyword"],
        link=queued["link"],
    )
    log.info(
        "Published queue %d as %s (%s), watching for %r",
        queued_id, result.media_id, result.permalink or "no permalink", queued["keyword"],
    )
    return True, False


async def publish_queued_facebook(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    queued: Any,
) -> tuple[bool, bool]:
    """Publish one already-claimed row as a Reel on a Facebook Page.

    **The cheapest of the four, and the reason is the credential.** A Page
    access token is Meta's shape, which is the shape `accounts` has always
    held, so there is no credentials table to read and no token to mint. What
    is on the row is what publishes.

    The same two-commit shape as the other three. `video_id` goes into
    `container_id` before a single byte is fetched, and every failure past that
    point is terminal: a retry restarts at `upload_phase=start` and cannot tell
    a `finish` that never landed from one whose response was lost, and the
    second of those posts the Reel twice.

    **Meta fetches the video**, so `media_url` is the same seam Instagram and
    TikTok use and nothing is pushed from here.
    """
    queued_id = int(queued["id"])
    page_id = account["account_id"]
    token = account["access_token"]

    if not token:
        # An account row with an empty token is a registration that never
        # finished, and trying again cannot fix it. Failed rather than retried,
        # like the missing credentials on the other two paths.
        await db.set_queue_state(
            conn, queued_id, db.QUEUE_FAILED, failure="no Facebook Page token stored"
        )
        metrics.publish_failures.labels(platform=db.PLATFORM_FACEBOOK).inc()
        log.error("Queue %d: Facebook Page %s has no stored token", queued_id, page_id)
        return False, False

    video = media_url(cfg, queued["video_name"])
    if not video:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure="no video file")
        metrics.publish_failures.labels(platform=db.PLATFORM_FACEBOOK).inc()
        log.error("Queue %d: no video file, nothing to publish", queued_id)
        return False, False

    try:
        video_id = await facebook.start_upload(
            graph.http, page_id=page_id, token=token, api_version=cfg.api_version
        )
    except facebook.PublishError as exc:
        # Nothing was created, so this is the one failure worth retrying.
        attempts = int(queued["attempts"] or 0)
        exhausted = attempts >= cfg.max_publish_attempts
        state = db.QUEUE_FAILED if exhausted else db.QUEUE_APPROVED
        await db.set_queue_state(conn, queued_id, state, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_FACEBOOK).inc()
        log.warning(
            "Queue %d: no Facebook upload session (attempt %d/%d): %s",
            queued_id, attempts, cfg.max_publish_attempts, exc,
        )
        return False, not exhausted

    await db.set_container(conn, queued_id, video_id)

    try:
        await facebook.upload_hosted(
            graph.http,
            video_id=video_id,
            token=token,
            video_url=video,
            api_version=cfg.api_version,
            timeout_s=cfg.facebook_upload_timeout_s,
        )
        await facebook.finish_upload(
            graph.http,
            page_id=page_id,
            video_id=video_id,
            token=token,
            description=str(queued["caption"] or ""),
            api_version=cfg.api_version,
        )
        result = await facebook.await_published(
            graph.http,
            video_id=video_id,
            token=token,
            api_version=cfg.api_version,
            poll_interval_s=cfg.facebook_poll_interval_s,
            timeout_s=cfg.facebook_publish_timeout_s,
        )
    except facebook.PublishError as exc:
        await db.set_queue_state(conn, queued_id, db.QUEUE_FAILED, failure=str(exc))
        metrics.publish_failures.labels(platform=db.PLATFORM_FACEBOOK).inc()
        log.error("Queue %d: Facebook failed after the video existed: %s", queued_id, exc)
        return False, False

    await db.mark_queue_published(
        conn, queued_id, media_id=result.video_id, permalink=result.permalink
    )
    metrics.posts_published.labels(platform=db.PLATFORM_FACEBOOK).inc()

    # No `register_post`, for the reason the YouTube and TikTok paths have
    # none: that call arms the comment poller, which works through
    # `graph.instagram.com` on an Instagram media id. A Page video id sitting
    # in `posts` is exactly the row that loop must never pick up.
    log.info(
        "Published queue %d to Facebook Page %s as %s (%s)",
        queued_id, page_id, result.video_id, result.permalink or "no permalink",
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

    queued = await db.next_approved(conn, account["account_id"])
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
        for row in await db.active_slots(conn, account["account_id"]):
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
    conn: aiosqlite.Connection, cfg: GatewaySettings, account_id: str, *, moment: datetime | None
) -> list[tuple[Any, datetime | None]]:
    """Queued posts paired with when each is expected to go out.

    Pinned posts keep their own time. Everything else is matched against the
    projected slot firings in queue order, which is what makes the queue page
    show "Thursday 18:04" instead of "position 3".
    """
    moment = moment or db.now()
    rows = await db.queued_posts(
        conn, account_id=account_id, states=(db.QUEUE_DRAFT, db.QUEUE_APPROVED)
    )
    slots = [schedule.Slot.from_row(r) for r in await db.active_slots(conn, account_id)]

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
