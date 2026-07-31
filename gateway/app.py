"""The FastAPI app, and the one place the pieces are wired together.

Everything the routes need hangs off `app.state`, which is what makes the whole
service testable: a test builds an app with its own SQLite file and an
`httpx.MockTransport` standing in for Meta, and nothing else changes.

Background tasks are started by the lifespan and cancelled by it. They are off
by default in tests, because a poller racing an assertion is a flaky test with a
plausible-looking cause.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI

from gateway import api, db, poller, webhook
from gateway.config import GatewaySettings, get_gateway_settings, require_secrets
from gateway.graph import GraphClient
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


def create_app(
    cfg: GatewaySettings | None = None,
    *,
    http: httpx.AsyncClient | None = None,
    background: bool = True,
    check_secrets: bool = True,
) -> FastAPI:
    cfg = cfg or get_gateway_settings()
    if check_secrets:
        require_secrets(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or httpx.AsyncClient()
        conn = await db.connect(cfg.db_path)

        app.state.cfg = cfg
        app.state.db = conn
        app.state.http = client
        app.state.graph = GraphClient(client, cfg)
        app.state.metrics = app.state.metrics or Metrics()

        tasks: list[asyncio.Task[None]] = []
        if background:
            tasks = [
                asyncio.create_task(
                    poller.poller_loop(conn, app.state.graph, cfg, app.state.metrics),
                    name="comment-poller",
                ),
                asyncio.create_task(
                    poller.refresher_loop(conn, app.state.graph, cfg, app.state.metrics),
                    name="token-refresher",
                ),
            ]
            log.info("Polling comments every %ds", cfg.poll_interval_s)

        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await conn.close()
            # An injected client belongs to whoever injected it.
            if http is None:
                await client.aclose()

    app = FastAPI(title="reelsmith gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Built before the lifespan runs so a metrics object survives an app that is
    # never started, which is the shape most unit tests want.
    app.state.metrics = Metrics()
    app.include_router(webhook.router)
    app.include_router(api.router)
    return app


app = create_app(check_secrets=False)
