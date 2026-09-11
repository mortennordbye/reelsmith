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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import timedelta
from typing import Any

import aiosqlite

from gateway import db, facebook, tiktok, youtube
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

    token = account["access_token"]

    pending = await db.insights_stale_media(
        conn, account_id=account_id, on=on, within_days=cfg.insights_max_age_days
    )
    stored = 0
    for row in pending:
        media_id = row["media_id"]
        try:
            reading = await graph.media_insights(media_id=media_id, token=token)
            extra = (
                await graph.media_extras(media_id=media_id, token=token)
                if reading is not None
                else None
            )
        except GraphError as exc:
            # An auth failure will hit every remaining media the same way, so
            # stop rather than spend the rest of the sweep proving it. That
            # includes the audience read below.
            metrics.graph_errors.inc()
            log.warning("Insights for %s failed: %s", media_id, exc)
            if exc.is_auth:
                log.error("Stopping the insights sweep for %s, the token is bad", account_id)
                return stored
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
            extra=extra,
        )
        metrics.insights_fetched.inc()
        stored += 1

    async def audience() -> tuple[int | None, dict[str, Any]]:
        reading = await graph.account_reading(
            ig_user_id=account_id, token=token, day=_yesterday(moment)
        )
        return reading.followers, reading.extra

    await _record_audience(
        conn, metrics, account_id, db.PLATFORM_INSTAGRAM, audience, on=on, moment=moment
    )
    return stored


def _yesterday(moment: Any) -> Any:
    """The last whole UTC day, which is the one every platform's totals settle on."""
    return (moment - timedelta(days=1)).date()


async def _record_audience(
    conn: aiosqlite.Connection,
    metrics: Metrics,
    account_id: str,
    platform: str,
    read: Callable[[], Awaitable[tuple[int | None, dict[str, Any]]]],
    *,
    on: str,
    moment: Any,
) -> None:
    """Store one destination's follower count and day totals, if it gave any.

    **Separate from the per post readings and never allowed to fail them.** A
    post's numbers say how it did and cannot say whether the account is
    growing; a follower count read once a day is the only series that can, and
    none of the four platforms offers one retrospectively, so a day not read is
    a day missing from the chart forever. That is also why it runs on a
    destination with nothing published.
    """
    try:
        followers, extra = await read()
    except (GraphError, youtube.AnalyticsError, facebook.InsightsError) as exc:
        metrics.graph_errors.inc()
        log.warning("Audience for %s %s failed: %s", platform, account_id, exc)
        return
    if followers is None and not extra:
        return
    await db.record_account_insights(
        conn,
        account_id=account_id,
        platform=platform,
        followers=followers,
        extra=extra,
        on=on,
        moment=moment,
    )


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


async def refresh_youtube_account(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    account: Mapping[str, Any],
    *,
    on: str | None = None,
) -> int:
    """Fetch today's numbers for one YouTube channel. Returns how many stored.

    **The shape TikTok needed is not needed here.** A YouTube upload returns its
    video id and its URL at publish, both of which the scheduler writes to the
    row, so there is nothing to resolve and nothing to match on a title. The
    whole sweep is a token and one report.

    **One request for the batch, not one per video.** The Analytics report is
    dimensioned by video and filtered to the ids being asked about, so a month
    of posting is a single call rather than thirty.

    **Six of the seven metrics land in columns that already exist.**
    `averageViewPercentage` gets `avg_view_pct`, added for it. `reach` and
    `saved` stay 0 because YouTube reports neither, and so does `skip_rate`:
    that column is the share who left inside three seconds, and the nearest
    thing here scores the whole video, so filling it would put two different
    measurements in one column and the feedback loop reads that column.
    """
    moment = db.now()
    on = on or moment.date().isoformat()
    channel_id = account["account_id"]

    stored = await db.youtube_credentials(conn, channel_id)
    if not stored:
        log.warning("YouTube channel %s has no credentials; skipping insights", channel_id)
        return 0

    # The same window the Meta sweep uses, and applied here rather than in SQL
    # because `published_on` is shared with TikTok, which pages by count.
    #
    # `created_at` stands in where a row has no publish date. It is never later
    # than the publish, so it can only widen the window, and widening costs
    # nothing where a start date past the publish would report each video's
    # numbers since then and call it a total.
    def when(row: Any) -> Any:
        return db.parse_iso(row["published_at"]) or db.parse_iso(row["created_at"]) or moment

    cutoff = moment - timedelta(days=cfg.insights_max_age_days)
    rows = [
        row
        for row in await db.published_on(conn, channel_id)
        if str(row["media_id"] or "") and when(row) >= cutoff
    ]

    try:
        token = await youtube.access_token(
            graph.http,
            client_id=stored["client_id"],
            client_secret=stored["client_secret"],
            refresh_token=stored["refresh_token"],
        )
    except youtube.UploadError as exc:
        # `invalid_grant` here means the refresh token is dead, which every
        # video in the batch would hit the same way. One line, not thirty.
        metrics.graph_errors.inc()
        log.warning("YouTube insights for %s failed to mint a token: %s", channel_id, exc)
        return 0

    async def audience() -> tuple[int | None, dict[str, Any]]:
        counters = await youtube.channel_statistics(graph.http, token=token)
        return counters.pop("subscriberCount", None), counters

    # Before the reports, so a channel with nothing published yet still gets
    # its subscriber count, which is exactly when the count is the only number.
    await _record_audience(
        conn, metrics, channel_id, db.PLATFORM_YOUTUBE, audience, on=on, moment=moment
    )
    if not rows:
        return 0

    # From the oldest post in the batch, so one range covers every video's whole
    # life. One request for the batch is what makes that the right trade: a
    # per video range would be a call per video to save nothing, since the
    # report is cumulative either way.
    start_date = (min(when(row) for row in rows) - timedelta(days=1)).date().isoformat()
    end_date = moment.date().isoformat()

    written = 0
    # Switched off for the rest of the sweep by the first refusal, since a
    # report the channel cannot have fails the same way for every video.
    curves = True
    ids = [str(row["media_id"]) for row in rows]
    for offset in range(0, len(ids), youtube.ANALYTICS_BATCH):
        batch = ids[offset : offset + youtube.ANALYTICS_BATCH]
        try:
            stats = await youtube.analytics(
                graph.http,
                token=token,
                video_ids=batch,
                start_date=start_date,
                end_date=end_date,
            )
        except youtube.AnalyticsError as exc:
            metrics.graph_errors.inc()
            log.warning("YouTube insights for %s failed: %s", channel_id, exc)
            return written

        # The extras and the curves only add to a reading, so a refusal of
        # either costs its own numbers and never the core ones above.
        try:
            extras = await youtube.analytics_extras(
                graph.http,
                token=token,
                video_ids=batch,
                start_date=start_date,
                end_date=end_date,
            )
        except youtube.AnalyticsError as exc:
            metrics.graph_errors.inc()
            log.warning("YouTube extra metrics for %s failed: %s", channel_id, exc)
            extras = {}

        for video_id, reading in stats.items():
            extra: dict[str, Any] = dict(extras.get(video_id, {}))
            if curves:
                try:
                    curve = await youtube.retention(
                        graph.http,
                        token=token,
                        video_id=video_id,
                        start_date=start_date,
                        end_date=end_date,
                    )
                except youtube.AnalyticsError as exc:
                    metrics.graph_errors.inc()
                    log.warning("YouTube retention for %s failed: %s", channel_id, exc)
                    curves = False
                    curve = []
                if curve:
                    extra["retention"] = curve

            await db.record_insights(
                conn,
                media_id=video_id,
                account_id=channel_id,
                metrics={
                    "views": reading.views,
                    "likes": reading.likes,
                    "comments": reading.comments,
                    "shares": reading.shares,
                    "avg_watch_ms": reading.avg_watch_ms,
                    "total_watch_ms": reading.total_watch_ms,
                    "avg_view_pct": reading.avg_view_pct,
                },
                on=on,
                moment=moment,
                platform=db.PLATFORM_YOUTUBE,
                extra=extra,
            )
            metrics.insights_fetched.inc()
            written += 1

    return written


async def refresh_facebook_account(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    account: Mapping[str, Any],
    *,
    on: str | None = None,
) -> int:
    """Fetch today's numbers for one Facebook Page. Returns how many stored.

    **Nothing to resolve and nothing to mint.** The publish returns the video
    id and the row already carries it, so this is neither TikTok's title match
    nor YouTube's token dance. One request per Reel, and the Page token on the
    account row is what makes it.

    **One request rather than two**, because the comment count comes from the
    node and the rest from its insights edge, and field expansion asks for both
    at once. Splitting them would double a sweep's calls to answer one
    question.

    **Reach is a second request, to the Reel's Page post.** Meta retired Reels
    reach from the video node on 2026-06-15, so `facebook.read_post_reach` asks
    the post for unique views. Where that is refused, `reach` stays 0 and
    `extra` carries no reach, which is what the page reads to leave the column
    out.

    **The absences are deliberate.** Meta reports watch time here as YouTube
    does, so this board has more than TikTok's. What it has not got is a three
    second skip, and
    `post_video_avg_time_watched` scores the whole Reel including replays, so
    writing it into `skip_rate` inverted would put two different measurements
    in the one column the feedback loop reads. `saved` and `shares` stay 0 and
    mean "not measured here": shares because Meta reports them fused to the
    comment count in `post_video_social_actions` and splitting that would be
    arithmetic on two different definitions.
    """
    moment = db.now()
    on = on or moment.date().isoformat()
    page_id = account["account_id"]
    token = account["access_token"]
    if not token:
        log.warning("Facebook Page %s has no token; skipping insights", page_id)
        return 0

    # The same window the Meta sweep uses. `created_at` stands in where a row
    # has no publish date, exactly as on the YouTube path: it is never later
    # than the publish, so it can only widen the window.
    def when(row: Any) -> Any:
        return db.parse_iso(row["published_at"]) or db.parse_iso(row["created_at"]) or moment

    cutoff = moment - timedelta(days=cfg.insights_max_age_days)
    rows = [
        row
        for row in await db.published_on(conn, page_id)
        if str(row["media_id"] or "") and when(row) >= cutoff
    ]

    written = 0
    for row in rows:
        video_id = str(row["media_id"])
        try:
            reading = await facebook.read_insights(
                graph.http, video_id=video_id, token=token, api_version=cfg.api_version
            )
            reach = await facebook.read_post_reach(
                graph.http,
                page_id=page_id,
                video_id=video_id,
                token=token,
                api_version=cfg.api_version,
            )
        except facebook.InsightsError as exc:
            metrics.graph_errors.inc()
            log.warning("Facebook insights for %s failed: %s", video_id, exc)
            # An auth failure hits every remaining Reel the same way, so stop
            # rather than spend the rest of the sweep proving it. The same
            # decision `refresh_account` makes on the Instagram path.
            if exc.is_auth:
                log.error("Stopping the Facebook sweep for %s, the token is bad", page_id)
                return written
            # A missing scope fails every Reel too, and was sixteen identical
            # warnings a sweep until this. The Page node still reads.
            if exc.is_permission:
                log.error(
                    "Stopping the Facebook Reel sweep for %s, the token lacks a "
                    "permission; authorise the Page again",
                    page_id,
                )
                break
            continue

        # The permalink is not known at publish on every path: a Reel that was
        # still transcoding when the publisher gave up waiting has a row with
        # none, and this is the first call afterwards that would know. The id
        # is unchanged, so this only ever fills a blank.
        if reading.permalink and not str(row["permalink"] or ""):
            await db.resolve_media_id(
                conn, int(row["id"]), media_id=video_id, permalink=reading.permalink
            )

        extra = dict(reading.extra)
        if reach is not None:
            extra[facebook.POST_REACH_METRIC] = reach
        await db.record_insights(
            conn,
            media_id=video_id,
            account_id=page_id,
            metrics={
                "views": reading.views,
                "reach": reach or 0,
                "likes": reading.likes,
                "comments": reading.comments,
                "avg_watch_ms": reading.avg_watch_ms,
                "total_watch_ms": reading.total_watch_ms,
            },
            on=on,
            moment=moment,
            platform=db.PLATFORM_FACEBOOK,
            extra=extra,
        )
        metrics.insights_fetched.inc()
        written += 1

    async def audience() -> tuple[int | None, dict[str, Any]]:
        reading = await facebook.read_page(
            graph.http,
            page_id=page_id,
            token=token,
            api_version=cfg.api_version,
            day=_yesterday(moment),
        )
        return reading.followers, reading.extra

    await _record_audience(
        conn, metrics, page_id, db.PLATFORM_FACEBOOK, audience, on=on, moment=moment
    )
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

    Four platforms, four shapes, and one rule they share: what is stored and
    what the scriptwriter is shown are different questions. Everything here
    writes to `insights` with its `platform` set, and `/api/results` still
    filters to Instagram, because `skip_rate` is the only measurement the loop
    turns on and only one platform reports it. Facebook is the one worth
    re-reading that rule for: it reports reach and watch time, which look close
    enough to Instagram's to be mistaken for them, and it reports no skip rate
    at all.
    """
    total = 0
    for account in await db.all_accounts(conn):
        if not account["active"]:
            continue
        total += await refresh_account(conn, graph, cfg, metrics, account)

    if cfg.youtube_insights_enabled:
        for account in await db.all_accounts(conn, platform=db.PLATFORM_YOUTUBE):
            if not account["active"]:
                continue
            total += await refresh_youtube_account(conn, graph, cfg, metrics, account)

    if cfg.tiktok_enabled:
        for account in await db.all_accounts(conn, platform=db.PLATFORM_TIKTOK):
            if not account["active"]:
                continue
            total += await refresh_tiktok_account(conn, graph, cfg, metrics, account)

    if cfg.facebook_insights_enabled:
        for account in await db.all_accounts(conn, platform=db.PLATFORM_FACEBOOK):
            if not account["active"]:
                continue
            total += await refresh_facebook_account(conn, graph, cfg, metrics, account)

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
