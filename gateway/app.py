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

import aiosqlite
import httpx
from fastapi import FastAPI

from gateway import admin, api, db, insights, poller, schedule, scheduler, webhook
from gateway.config import (
    GatewaySettings,
    get_gateway_settings,
    require_admin_auth,
    require_secrets,
)
from gateway.graph import GraphClient
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


async def _apply_config_slots(conn: aiosqlite.Connection, cfg: GatewaySettings) -> None:
    """Bring the declared schedule into the database, or refuse to start.

    A typo in `GATEWAY_SLOTS` fails the boot rather than starting with a
    schedule that quietly lost a line. The pod crashlooping with the offending
    line in the log is a much shorter debug than an account that stopped
    posting on Saturdays for no visible reason.
    """
    if not cfg.slots.strip():
        return

    specs = schedule.parse_slots(
        cfg.slots,
        default_tz=cfg.default_timezone,
        default_jitter=cfg.default_jitter_minutes,
    )
    account = cfg.slots_account.strip()
    if not account:
        # One account is the normal case, so the id does not have to be
        # repeated in config. More than one is ambiguous and says so.
        accounts = await db.all_accounts(conn)
        if len(accounts) != 1:
            log.warning(
                "GATEWAY_SLOTS is set but %d accounts are registered. "
                "Set GATEWAY_SLOTS_ACCOUNT, or declare the slots in the admin UI.",
                len(accounts),
            )
            return
        account = str(accounts[0]["ig_user_id"])

    count = await db.sync_config_slots(conn, account, specs)
    log.info("Applied %d slot(s) from config for %s", count, account)


def _configure_logging(cfg: GatewaySettings) -> None:
    """Make this service's own logging visible.

    uvicorn configures its own loggers and nothing else, so every `log.info` in
    this package was landing on a root logger with no handler and disappearing.
    The symptom was a container whose entire log was a wall of health check
    lines: which slots were applied, what the scheduler did, and why a publish
    failed were all invisible, and a failed admin login left no trace at all.

    `force=True` because uvicorn has already touched the root logger by the
    time this runs.
    """
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        force=True,
    )
    # The access log is the noise this was drowning in. Health checks arrive
    # every ten seconds from the kubelet and say nothing.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs the full request URL at INFO. When every Graph call carried
    # `access_token` as a query parameter, turning this package's logging on
    # wrote a live Instagram token, with publishing and messaging rights, into
    # the pod log every twenty seconds and from there into the log store.
    # `gateway/graph.py` now sends a header instead, so this is a second lock
    # rather than the only one, and it still matters: the token refresh has no
    # header form and its URL carries the token. WARNING keeps the failures and
    # drops the URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(
    cfg: GatewaySettings | None = None,
    *,
    http: httpx.AsyncClient | None = None,
    background: bool = True,
    check_secrets: bool = True,
) -> FastAPI:
    cfg = cfg or get_gateway_settings()
    _configure_logging(cfg)
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
        await _apply_config_slots(conn, cfg)

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

            # On by default, unlike the scheduler. This one only reads: it
            # creates nothing, publishes nothing and messages nobody, so
            # gaining it by upgrading is not a surprise worth guarding.
            if cfg.insights_enabled:
                tasks.append(
                    asyncio.create_task(
                        insights.insights_loop(
                            conn, app.state.graph, cfg, app.state.metrics
                        ),
                        name="insights",
                    )
                )
                log.info("Insights on, refreshing every %ds", cfg.insights_interval_s)

            # Off unless asked for. Publishing to the feed is a bigger power
            # than answering comments, and a gateway that gained it by being
            # upgraded would be a surprise rather than a decision.
            if cfg.scheduler_enabled:
                tasks.append(
                    asyncio.create_task(
                        scheduler.scheduler_loop(
                            conn, app.state.graph, cfg, app.state.metrics
                        ),
                        name="scheduler",
                    )
                )
                log.info("Scheduler on, checking slots every %ds", cfg.scheduler_interval_s)
            else:
                log.info("Scheduler off (GATEWAY_SCHEDULER_ENABLED)")

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
    if cfg.admin_enabled:
        # `require_admin_auth` has already refused to get here without either a
        # token or an explicit statement that forward-auth is in front.
        require_admin_auth(cfg)
        app.include_router(admin.public)
        app.include_router(admin.router)
    return app


app = create_app(check_secrets=False)
