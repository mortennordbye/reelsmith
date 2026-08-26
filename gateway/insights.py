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

from gateway import db
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


async def refresh_once(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
) -> int:
    """One sweep across every active account."""
    total = 0
    for account in await db.all_accounts(conn):
        if not account["active"]:
            continue
        total += await refresh_account(conn, graph, cfg, metrics, account)
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
