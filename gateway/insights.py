"""How the published Reels are actually doing.

The service could always say a post went out and nothing about whether it
worked, so judging a video meant opening the Instagram app and reading numbers
that nothing kept. This sweep stores them.

**Read only.** It creates nothing, publishes nothing and messages nobody. That
is why it is on by default where the scheduler is not: the worst outcome of a
bug here is a stale number on a page.

Two things worth knowing:

- **A reading is per media per day**, not per media. A Reel keeps climbing for
  days after it publishes, so one mutable row would answer "how is it doing"
  while making "did the evening slot beat the morning one" unanswerable
  forever.
- **A media with no insights yet is normal, not an error.** Meta has nothing
  for a Reel published minutes ago. `graph.media_insights` returns None for
  that case rather than raising, because a sweep that dies on the newest post
  never reaches the older ones behind it.
- **Retention is the half worth reading.** A view counts a viewer who left
  after half a second exactly like one who watched to the end, so the first
  six columns can look healthy while nobody is watching. On the first seven
  posts here the average viewer left before six seconds of a twenty six second
  video, and `skip_rate` ran 64 to 80 percent against a 30 to 40 percent
  benchmark for educational Reels. None of that was visible from views.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import aiosqlite

from gateway import db, tiktok
from gateway.config import GatewaySettings
from gateway.graph import GraphClient, GraphError
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


async def refresh_account(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    account: Mapping[str, Any],
    *,
    on: str | None = None,
) -> int:
    """Fetch today's missing readings for one account. Returns how many stored."""
    moment = db.now()
    on = on or moment.date().isoformat()
    account_id = account["account_id"]

    pending = await db.insights_stale_media(
        conn, account_id=account_id, on=on, within_days=cfg.insights_max_age_days
    )
    stored = 0
    for row in pending:
        media_id = row["media_id"]
        try:
            reading = await graph.media_insights(
                media_id=media_id, token=account["access_token"]
            )
        except GraphError as exc:
            # An auth failure will hit every remaining media the same way, so
            # stop rather than spend the rest of the sweep proving it.
            metrics.graph_errors.inc()
            log.warning("Insights for %s failed: %s", media_id, exc)
            if exc.is_auth:
                log.error("Stopping the insights sweep for %s, the token is bad", account_id)
                break
            continue

        if reading is None:
            continue

        values = asdict(reading)
        values.pop("media_id", None)
        await db.record_insights(
            conn,
            media_id=media_id,
            account_id=account_id,
            metrics=values,
            on=on,
            moment=moment,
        )
        metrics.insights_fetched.inc()
        stored += 1

    return stored


async def refresh_tiktok_account(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    account: Mapping[str, Any],
    *,
    on: str | None = None,
) -> int:
    """Fetch today's counts for one TikTok account. Returns how many stored.

    **Two steps rather than one, and the first is what Meta never needed.** The
    `publish_id` a post is published under is not a video id, so a row that has
    never been resolved is matched against the account's recent videos by title
    and rewritten to carry the real id. `container_id` keeps the publish id, so
    nothing is lost by the swap.

    Matched on title rather than on time. The title is a string this service
    wrote and TikTok echoes back, so it identifies a post exactly, where a
    timestamp has to be compared with a tolerance and two posts an hour apart
    on a busy day would be a coin toss.

    **What comes back is four numbers.** There is no retention metric of any
    kind, so `reach`, `saved`, `avg_watch_ms`, `total_watch_ms` and `skip_rate`
    stay 0 and mean "not measured on this platform". The `platform` column is
    what tells them apart from a real zero.
    """
    moment = db.now()
    on = on or moment.date().isoformat()
    account_id = account["account_id"]

    stored = await db.tiktok_credentials(conn, account_id)
    if not stored:
        log.warning("TikTok account %s has no credentials; skipping insights", account_id)
        return 0

    try:
        fresh = await tiktok.refresh_access_token(
            graph.http,
            credentials=tiktok.Credentials(
                open_id=account_id,
                client_key=stored["client_key"],
                client_secret=stored["client_secret"],
                refresh_token=stored["refresh_token"],
            ),
        )
        # Before anything is read with it. The token just spent is dead, and a
        # sweep that throws must not take the new one with it.
        await db.save_tiktok_refresh(
            conn, account_id, fresh.refresh_token, fresh.refresh_expires_in
        )
        recent = await tiktok.list_videos(graph.http, token=fresh.access_token)
    except tiktok.PublishError as exc:
        metrics.graph_errors.inc()
        log.warning("TikTok insights for %s failed: %s", account_id, exc)
        return 0

    by_title = {video.title.strip(): video for video in recent if video.title.strip()}
    rows = await db.published_on(conn, account_id)

    written = 0
    for row in rows:
        video = by_title.get(str(row["title"] or "").strip())
        if video is None:
            # Either it has fallen off the first page, in which case its id was
            # resolved on an earlier sweep, or it is not there yet. Neither is
            # an error and neither is worth a log line every six hours.
            video = _known(recent, str(row["media_id"] or ""))
        if video is None:
            continue

        if str(row["media_id"] or "") != video.video_id:
            await db.resolve_media_id(
                conn, int(row["id"]), media_id=video.video_id, permalink=video.share_url
            )
            log.info(
                "Queue %d is TikTok video %s (published as %s)",
                int(row["id"]), video.video_id, row["media_id"],
            )

        await db.record_insights(
            conn,
            media_id=video.video_id,
            account_id=account_id,
            metrics={
                "views": video.views,
                "likes": video.likes,
                "comments": video.comments,
                "shares": video.shares,
            },
            on=on,
            moment=moment,
            platform=db.PLATFORM_TIKTOK,
        )
        metrics.insights_fetched.inc()
        written += 1

    return written


def _known(recent: list[Any], media_id: str) -> Any:
    """The video a row already resolved to, if it is still on the first page."""
    if not media_id:
        return None
    return next((video for video in recent if video.video_id == media_id), None)


async def refresh_once(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
) -> int:
    """One sweep across every active account, on every platform that has numbers.

    YouTube is deliberately absent. Its `averageViewPercentage` scores a whole
    video rather than the opening, so it belongs in this table no more than
    TikTok's four counts belong in `_results_block`, and nothing has asked for
    it yet.
    """
    total = 0
    for account in await db.all_accounts(conn):
        if not account["active"]:
            continue
        total += await refresh_account(conn, graph, cfg, metrics, account)

    if cfg.tiktok_enabled:
        for account in await db.all_accounts(conn, platform=db.PLATFORM_TIKTOK):
            if not account["active"]:
                continue
            total += await refresh_tiktok_account(conn, graph, cfg, metrics, account)

    metrics.insights_last_success.set(db.now().timestamp())
    return total


async def insights_loop(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
) -> None:
    while True:
        try:
            stored = await refresh_once(conn, graph, cfg, metrics)
            if stored:
                log.info("Stored %d insight readings", stored)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Insights sweep failed, continuing")
        await asyncio.sleep(cfg.insights_interval_s)
