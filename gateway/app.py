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

from gateway import (
    admin,
    api,
    backup,
    db,
    insights,
    pages,
    poller,
    schedule,
    scheduler,
    webhook,
)
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
    # A line may name its own destination, which is how one config holds
    # several channels. Lines that do not belong to GATEWAY_SLOTS_ACCOUNT, or
    # to the single registered Instagram account when that is unambiguous.
    by_account: dict[str, list[schedule.SlotSpec]] = {}
    for spec in (s for s in specs if s.account):
        by_account.setdefault(spec.account, []).append(spec)

    unnamed = [s for s in specs if not s.account]
    unresolved = False
    if unnamed:
        account = cfg.slots_account.strip()
        if not account:
            # Instagram rows only. Registering a channel would otherwise take
            # the count past one and stop applying a schedule that had worked
            # for months, which is the failure this function exists to prevent.
            accounts = await db.all_accounts(conn, platform=db.PLATFORM_INSTAGRAM)
            if len(accounts) == 1:
                account = str(accounts[0]["account_id"])
        if account:
            by_account.setdefault(account, []).extend(unnamed)
        else:
            # Only the ambiguous lines are dropped. The ones naming an account
            # are not ambiguous and applying them is strictly better than
            # applying nothing.
            unresolved = True
            log.error(
                "%d slot line(s) name no account and it could not be resolved, so "
                "the schedule is frozen at whatever is already in the database. "
                "Set GATEWAY_SLOTS_ACCOUNT, or add account=<id> to those lines.",
                len(unnamed),
            )

    # An account whose lines were all removed from config keeps its rows unless
    # it is visited with an empty list, and would go on publishing on a
    # schedule nobody can see any more.
    #
    # **Not while a line is unresolved.** The sweep reads an account's absence
    # from the config as an instruction to delete its slots, and an unresolved
    # line is an account this function could not name rather than an account
    # nobody named. Registering a second Instagram account is enough to make
    # the difference: the resolve-by-count above stops firing, the unnamed
    # lines are dropped, and then account 1 is visited with an empty list and
    # its three slots are deleted at boot. The pod comes up healthy and the
    # feed stops, which is the loudest thing here failing the quietest way.
    # Reproduced on 2026-08-26 and written up as F0 in
    # docs/multi-destination-audit.md.
    #
    # Freezing is the conservative half of the trade and it is not free: an
    # account whose lines really were deleted keeps posting until the config is
    # fixed. That is recoverable and visible in the panel, where deleting a
    # working schedule is neither.
    if not unresolved:
        for account in await db.config_slot_accounts(conn):
            by_account.setdefault(account, [])

    for account, group in by_account.items():
        applied, removed = await db.sync_config_slots(conn, account, group)
        # The deletion said "Applied 0 slot(s)" at INFO, next to a warning
        # describing a different symptom, which is how it went unnoticed. It
        # says what it did now.
        if removed:
            log.warning(
                "Removed %d config slot(s) for %s, which GATEWAY_SLOTS no longer declares.",
                removed, account,
            )
        log.info("Applied %d slot(s) from config for %s", applied, account)


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

            # A third refresher, because TikTok is the only platform here whose
            # refresh token rotates. Instagram's rides on the render host's
            # --snapshot job and Google's has no clock at all. Gated on the same
            # flag as publishing, since a loop calling TikTok on a deployment
            # that has no TikTok account is a log line a day saying nothing.
            if cfg.tiktok_enabled:
                tasks.append(
                    asyncio.create_task(
                        poller.tiktok_refresher_loop(
                            conn, client, cfg, app.state.metrics
                        ),
                        name="tiktok-refresher",
                    )
                )
                log.info(
                    "TikTok on, refreshing every %ds, %s path",
                    cfg.tiktok_refresh_interval_s,
                    "direct post" if cfg.tiktok_direct_post else "inbox",
                )

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

            # Also on by default, and also because it only reads. What it
            # protects is `comments_handled`, which cannot be rebuilt and whose
            # loss means re-replying to every comment still inside Meta's seven
            # day window.
            if cfg.backup_enabled:
                tasks.append(
                    asyncio.create_task(
                        backup.backup_loop(cfg, app.state.metrics),
                        name="backup",
                    )
                )
                log.info(
                    "Backups on, every %ds to %s, keeping %d",
                    cfg.backup_interval_s, cfg.backup_dir, cfg.backup_keep,
                )

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
    # Unconditional, unlike the admin panel below: these are the URLs the
    # Instagram, YouTube and TikTok app records point at, and they must not
    # depend on whether the panel happens to be switched on.
    app.include_router(pages.router)
    if cfg.admin_enabled:
        # `require_admin_auth` has already refused to get here without either a
        # token or an explicit statement that forward-auth is in front.
        require_admin_auth(cfg)
        app.include_router(admin.public)
        app.include_router(admin.router)
    return app


app = create_app(check_secrets=False)
