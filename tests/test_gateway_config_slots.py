"""What the config-declared schedule does when it cannot tell whose it is.

`GATEWAY_SLOTS` is applied at boot and config is the truth for those rows, so
`_apply_config_slots` deletes as readily as it inserts. That is correct and it
is why a line that names no account is dangerous: the code has to decide
between "nobody declared slots for this account" and "I could not work out who
declared these", and those want opposite actions.

It got that wrong, and the failure was silent. Registering a second Instagram
account takes the resolve-by-count past one, the unnamed lines are dropped, and
then the sweep that exists to retire a deleted account's slots visits account 1
with an empty list and deletes a working schedule. The pod boots healthy and
the feed stops. Reproduced 2026-08-26, F0 in docs/multi-destination-audit.md.
"""

from __future__ import annotations

import pytest

from gateway import db
from gateway.app import create_app
from tests.gateway_harness import ACCOUNT, CHANNEL, FakeMeta, settings

SECOND = "17841400000000001"
THREE_UNNAMED = "06:00 UTC\n10:00 UTC\n17:00 UTC"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path, slots=THREE_UNNAMED)


@pytest.fixture
def meta():
    return FakeMeta()


async def boot(cfg, meta):
    """One lifespan, which is the only thing that applies config slots."""
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            return await db.all_slots(app.state.db, ACCOUNT)


async def test_registering_a_second_instagram_account_keeps_the_first_ones_slots(cfg, meta):
    """F0, and the whole point of this file.

    Nothing about account 1 changed between the two boots. A row was added for
    a different account and that was enough to delete account 1's schedule.
    """
    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
    finally:
        await conn.close()

    assert len(await boot(cfg, meta)) == 3

    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, account_id=SECOND, access_token="tok")
    finally:
        await conn.close()

    assert len(await boot(cfg, meta)) == 3


async def test_an_unresolved_line_freezes_the_sweep_for_every_account(cfg, meta):
    """The conservative half of the trade, stated so it is chosen rather than
    discovered.

    A line was genuinely deleted from the config here, and the account keeps
    its slot anyway, because another line in the same config could not be
    resolved and the sweep cannot tell the two situations apart. The cost is
    an account that keeps posting until the config is fixed, which is
    recoverable and visible on the Queue page. The other direction deletes a
    working schedule at boot and announces it as "Applied 0".
    """
    directory = cfg.db_path.parent
    before = settings(
        directory, slots=f"06:00 UTC\n19:30 UTC account={CHANNEL}"
    )
    conn = await db.connect(before.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        await db.upsert_account(
            conn, account_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
        )
    finally:
        await conn.close()

    async with meta.client() as fake_meta:
        app = create_app(before, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            assert len(await db.all_slots(app.state.db, CHANNEL)) == 1

    # A second Instagram account arrives, so the unnamed line stops resolving.
    conn = await db.connect(before.db_path)
    try:
        await db.upsert_account(conn, account_id=SECOND, access_token="tok")
    finally:
        await conn.close()

    # ... and the channel's line is deleted in the same edit.
    after = settings(directory, slots="06:00 UTC")
    async with meta.client() as fake_meta:
        app = create_app(after, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            assert len(await db.all_slots(app.state.db, CHANNEL)) == 1
            assert len(await db.all_slots(app.state.db, ACCOUNT)) == 1


async def test_naming_the_account_survives_a_second_one_being_registered(cfg, meta):
    """The cheap half of the defence, and the one to apply to the ConfigMap.

    A line carrying `account=` is never ambiguous, so none of the above
    applies to it. This is what every GATEWAY_SLOTS line should look like
    before a second account exists anywhere.
    """
    named = settings(
        cfg.db_path.parent,
        slots=f"06:00 UTC account={ACCOUNT}\n10:00 UTC account={ACCOUNT}",
    )
    conn = await db.connect(named.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        await db.upsert_account(conn, account_id=SECOND, access_token="tok")
    finally:
        await conn.close()

    assert len(await boot(named, meta)) == 2


async def test_a_line_actually_deleted_from_config_still_removes_its_slots(cfg, meta):
    """The behaviour the freeze must not cost when nothing is ambiguous.

    Config is the truth for these rows and a channel deleted from it that kept
    publishing on a schedule nobody can read is the worse failure. With every
    line naming its account there is no ambiguity, so the sweep runs.
    """
    both = settings(
        cfg.db_path.parent,
        slots=f"06:00 UTC account={ACCOUNT}\n19:30 UTC account={CHANNEL}",
    )
    conn = await db.connect(both.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        await db.upsert_account(
            conn, account_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
        )
    finally:
        await conn.close()

    async with meta.client() as fake_meta:
        app = create_app(both, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            assert len(await db.all_slots(app.state.db, CHANNEL)) == 1

        without = settings(both.db_path.parent, slots=f"06:00 UTC account={ACCOUNT}")
        app = create_app(without, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            assert await db.all_slots(app.state.db, CHANNEL) == []
            assert len(await db.all_slots(app.state.db, ACCOUNT)) == 1


async def test_the_deletion_says_what_it_deleted(cfg, meta, caplog, monkeypatch):
    """It said "Applied 0 slot(s)" at INFO, next to a warning describing a
    different symptom, which is how a fortnight of it went unread.

    `_configure_logging` is stubbed out because it calls `basicConfig(force=True)`,
    which removes the handler caplog installed. That is correct in the app, where
    uvicorn has already claimed the root logger, and it makes the log unreadable
    from a test unless it is skipped.
    """
    monkeypatch.setattr("gateway.app._configure_logging", lambda _cfg: None)
    named = settings(cfg.db_path.parent, slots=f"06:00 UTC account={ACCOUNT}")
    conn = await db.connect(named.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
    finally:
        await conn.close()

    await boot(named, meta)

    emptied = settings(named.db_path.parent, slots="0:00 UTC account=" + CHANNEL)
    with caplog.at_level("WARNING"):
        async with meta.client() as fake_meta:
            app = create_app(emptied, http=fake_meta, background=False)
            async with app.router.lifespan_context(app):
                pass

    assert any("Removed 1 config slot" in r.getMessage() for r in caplog.records)
