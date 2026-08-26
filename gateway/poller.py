"""Two background tasks in the same process as the webhook handler.

**The comment poller** is here because real-time `comments` webhooks need
Advanced Access, meaning App Review plus Business Verification. Polling needs
neither, and at one sweep a minute over a handful of posts it is nowhere near
any limit. It is also the more reliable of the two: Meta never replays a missed
webhook, while a poll that failed simply happens again in sixty seconds, and the
reply window is seven days wide.

**The token refresher** matters more than it looks. A long-lived token lasts 60
days, is refreshed rather than reissued, and once expired cannot be refreshed at
all. Nothing else in this service notices that happening.

**The TikTok refresher is a third loop and a genuinely different mechanism.**
Instagram's refresh rides on the render host's `--snapshot` job and YouTube
needs none, because Google's refresh token has no clock. TikTok's access token
lasts 24 hours and its refresh token **rotates on every use**, so the token just
spent is dead the moment the response arrives. A missed Meta refresh costs a
day. A dropped TikTok write costs the account, recoverable only by a person in
a browser. That is why the write happens before anything is done with the access
token it came back with, and why it is not conditional on the token looking
different.

All three loops swallow their exceptions on purpose. A poll that throws must not
take the webhook receiver down with it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiosqlite
import httpx

from gateway import conversations, db, tiktok
from gateway.config import GatewaySettings
from gateway.graph import GraphClient, GraphError
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


async def poll_account(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    account: Any,
) -> int:
    """One sweep of one account's recent posts. Returns replies sent."""
    sent = 0
    for post in await db.pollable_posts(conn, account["account_id"], ttl_days=cfg.post_ttl_days):
        try:
            comments = await graph.list_comments(
                media_id=post["media_id"], token=account["access_token"]
            )
        except GraphError as exc:
            # One unreadable post (deleted, or comments turned off) must not
            # stop the sweep for the others.
            log.warning("Comments for %s unreadable: %s", post["media_id"], exc)
            metrics.graph_errors.inc()
            continue

        for comment in comments:
            if not conversations.comment_matches(comment.text, post["keyword"]):
                continue
            outcome = await conversations.handle_comment(
                conn,
                graph,
                cfg,
                metrics,
                account=account,
                post=post,
                comment_id=comment.id,
                author_id=comment.author_id,
            )
            if outcome.action == "replied":
                sent += 1

        await db.mark_polled(conn, post["media_id"])
    return sent


async def poll_once(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> int:
    total = 0
    for account in await db.active_accounts(conn):
        total += await poll_account(conn, graph, cfg, metrics, account)
    metrics.poll_cycles.inc()
    metrics.poller_last_success.set(db.now().timestamp())
    return total


async def poller_loop(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> None:
    while True:
        try:
            sent = await poll_once(conn, graph, cfg, metrics)
            if sent:
                log.info("Comment sweep sent %d private replies", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Comment sweep failed, continuing")
        await asyncio.sleep(cfg.poll_interval_s)


async def refresh_tokens_once(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> int:
    """Refresh every account token inside the margin. Returns how many moved."""
    refreshed = 0
    for account in await db.all_accounts(conn):
        expires = db.parse_iso(account["token_expires_at"])
        days_left = (expires - db.now()).total_seconds() / 86_400 if expires else None
        if days_left is not None:
            metrics.token_days_left.labels(ig_user_id=account["account_id"]).set(days_left)
            if days_left > cfg.token_refresh_margin_days:
                continue
            if days_left <= 0:
                # Past the point a refresh can help. Someone has to re-authorise
                # in a browser, so say so rather than retrying daily forever.
                log.error(
                    "Token for %s has expired and cannot be refreshed. Re-authorise it.",
                    account["account_id"],
                )
                continue

        try:
            token, expires_in = await graph.refresh_token(token=account["access_token"])
        except GraphError as exc:
            log.warning("Token refresh for %s failed: %s", account["account_id"], exc)
            metrics.graph_errors.inc()
            continue

        await db.save_account_token(conn, account["account_id"], token, expires_in)
        if expires_in:
            metrics.token_days_left.labels(ig_user_id=account["account_id"]).set(
                expires_in / 86_400
            )
        refreshed += 1
        log.info("Refreshed the token for %s", account["account_id"])
    return refreshed


async def refresher_loop(
    conn: aiosqlite.Connection, graph: GraphClient, cfg: GatewaySettings, metrics: Metrics
) -> None:
    while True:
        try:
            await refresh_tokens_once(conn, graph, cfg, metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Token refresh pass failed, continuing")
        await asyncio.sleep(cfg.token_refresh_interval_s)


async def refresh_tiktok_once(
    conn: aiosqlite.Connection, http: httpx.AsyncClient, metrics: Metrics
) -> int:
    """Rotate every TikTok account's refresh token. Returns how many moved.

    Unconditional rather than margin-based, unlike the Meta pass above. That one
    skips an account with 40 days left because a refresh costs a call and buys
    nothing. Here the access token lasts 24 hours, so a daily pass is the
    minimum that keeps one alive at all, and the refresh token's own year is
    extended as a side effect rather than as the goal.

    **The write comes first.** `save_tiktok_refresh` commits the token TikTok
    just handed back before this function does anything else with the access
    token, because the one that was spent is already dead. Failing after the
    write costs a day; failing before it costs the account.
    """
    rotated = 0
    for account in await db.all_accounts(conn, platform=db.PLATFORM_TIKTOK):
        open_id = account["account_id"]
        stored = await db.tiktok_credentials(conn, open_id)
        if not stored:
            log.warning("TikTok account %s has no credentials row; skipping", open_id)
            continue

        expires = db.parse_iso(stored["refresh_expires_at"])
        if expires:
            days_left = (expires - db.now()).total_seconds() / 86_400
            metrics.token_days_left.labels(ig_user_id=open_id).set(days_left)
            if days_left <= 0:
                # A year of daily refreshes should make this unreachable, so
                # arriving here means the loop stopped and nobody noticed.
                log.error(
                    "TikTok refresh token for %s has expired. Re-authorise it in a "
                    "browser; nothing here can recover it.",
                    open_id,
                )
                continue

        credentials = tiktok.Credentials(
            open_id=open_id,
            client_key=stored["client_key"],
            client_secret=stored["client_secret"],
            refresh_token=stored["refresh_token"],
        )
        try:
            fresh = await tiktok.refresh_access_token(http, credentials=credentials)
        except tiktok.PublishError as exc:
            log.warning("TikTok token refresh for %s failed: %s", open_id, exc)
            metrics.graph_errors.inc()
            continue

        await db.save_tiktok_refresh(
            conn, open_id, fresh.refresh_token, fresh.refresh_expires_in
        )
        rotated += 1
        log.info(
            "Refreshed TikTok token for %s (refresh token %s)",
            open_id,
            "rotated" if fresh.rotated_from(credentials.refresh_token) else "unchanged",
        )
    return rotated


async def tiktok_refresher_loop(
    conn: aiosqlite.Connection, http: httpx.AsyncClient, cfg: GatewaySettings, metrics: Metrics
) -> None:
    while True:
        try:
            await refresh_tiktok_once(conn, http, metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TikTok token refresh pass failed, continuing")
        await asyncio.sleep(cfg.tiktok_refresh_interval_s)
