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

Both loops swallow their exceptions on purpose. A poll that throws must not take
the webhook receiver down with it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiosqlite

from gateway import conversations, db
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
