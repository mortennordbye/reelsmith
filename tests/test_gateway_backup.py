"""Point in time copies of the state.

The database is on NAS backed NFS, so it already survives losing a node. This
is about the failures that leaves: a bad migration, a mistaken delete, a corrupt
page. One table makes it worth doing rather than merely tidy. `comments_handled`
is the claim enforcing one private reply per comment, Meta allows that reply for
seven days, and a poller starting again with an empty one re-replies to every
comment still inside the window.

So the test that matters is not "a file appeared". It is that the copy is a real
database that still knows what has been answered.
"""

from __future__ import annotations

import aiosqlite
import pytest

from gateway import backup, db
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, IGSID, settings


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    yield connection
    await connection.close()


@pytest.fixture
def metrics():
    return Metrics()


async def test_the_copy_is_a_database_that_remembers_what_was_answered(conn, cfg, metrics):
    """The reason this exists, tested the way it will be used."""
    await db.claim_comment(
        conn, comment_id="c1", media_id="media-1", ig_user_id=ACCOUNT, author_id=IGSID
    )

    path = await backup.backup_once(cfg, metrics)

    assert path is not None
    restored = await aiosqlite.connect(path)
    restored.row_factory = aiosqlite.Row
    async with restored.execute("SELECT comment_id FROM comments_handled") as cur:
        rows = await cur.fetchall()
    await restored.close()
    assert [r["comment_id"] for r in rows] == ["c1"]


async def test_a_claim_made_after_the_copy_is_not_in_it(conn, cfg, metrics):
    """A point in time copy, not a live mirror. Says what it is."""
    path = await backup.backup_once(cfg, metrics)
    await db.claim_comment(
        conn, comment_id="later", media_id="media-1", ig_user_id=ACCOUNT, author_id=IGSID
    )

    restored = await aiosqlite.connect(path)
    async with restored.execute("SELECT COUNT(*) FROM comments_handled") as cur:
        (count,) = await cur.fetchone()
    await restored.close()
    assert count == 0


async def test_the_backup_directory_is_created(conn, cfg, metrics):
    assert not cfg.backup_dir.exists()

    await backup.backup_once(cfg, metrics)

    assert cfg.backup_dir.is_dir()


async def test_the_success_gauge_moves(conn, cfg, metrics):
    """What the staleness alert reads. A backup nobody notices stopping is not
    a backup."""
    assert metrics.backup_last_success._value.get() == 0

    await backup.backup_once(cfg, metrics)

    assert metrics.backup_last_success._value.get() > 0


async def test_it_works_while_the_service_connection_is_mid_transaction(conn, cfg, metrics):
    """The condition every test here used to miss.

    The gateway shares one connection across the poller, the scheduler and the
    insights sweep, and SQLite refuses to vacuum a connection with statements
    in progress. The first deploy raised `cannot VACUUM - SQL statements in
    progress` every six hours while the whole suite stayed green, because a
    test connection is idle at the moment it is asked and a live one is not.
    """
    await conn.execute(
        "INSERT INTO comments_handled (comment_id, media_id, ig_user_id, claimed_at) "
        "VALUES ('open', 'media-1', ?, '2026-08-02T00:00:00+00:00')",
        (ACCOUNT,),
    )  # deliberately not committed, so the write transaction stays open

    path = await backup.backup_once(cfg, metrics)

    assert path is not None and path.exists()


async def test_a_second_copy_in_the_same_second_is_not_an_error(conn, cfg, metrics):
    """What a restart loop looks like. VACUUM INTO refuses an existing file."""
    first = await backup.backup_once(cfg, metrics)

    assert first is not None
    assert await backup.backup_once(cfg, metrics) is None


# --- Pruning ----------------------------------------------------------------


def make_copies(directory, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"state-2026080{i // 9}T00000{i % 9}.sqlite3").write_text("x")


def test_only_the_newest_are_kept(cfg):
    make_copies(cfg.backup_dir, 20)

    assert backup.prune(cfg.backup_dir, keep=5) == 15
    assert len(list(cfg.backup_dir.glob("state-*.sqlite3"))) == 5


def test_pruning_keeps_the_newest_by_name(cfg):
    """The name is a timestamp, so sorting it is sorting by age."""
    make_copies(cfg.backup_dir, 3)

    backup.prune(cfg.backup_dir, keep=1)

    assert [p.name for p in cfg.backup_dir.iterdir()] == ["state-20260800T000002.sqlite3"]


def test_fewer_copies_than_the_limit_prunes_nothing(cfg):
    make_copies(cfg.backup_dir, 2)

    assert backup.prune(cfg.backup_dir, keep=14) == 0


def test_files_this_did_not_write_are_never_touched(cfg):
    """The same rule `renderer.prune_staged_assets` follows. A sweep that
    deletes what it did not create eventually deletes something deliberate."""
    make_copies(cfg.backup_dir, 3)
    (cfg.backup_dir / "keep-me.sqlite3").write_text("x")
    (cfg.backup_dir / "notes.txt").write_text("x")

    backup.prune(cfg.backup_dir, keep=1)

    survivors = {p.name for p in cfg.backup_dir.iterdir()}
    assert "keep-me.sqlite3" in survivors
    assert "notes.txt" in survivors


async def test_the_loop_keeps_going_after_a_failure(conn, cfg, metrics, monkeypatch):
    """A gateway that stops answering Meta because a disk filled has turned a
    recoverable problem into an outage."""
    calls = []

    async def boom(*_args, **_kwargs):
        calls.append(1)
        raise OSError("no space left on device")

    monkeypatch.setattr(backup, "backup_once", boom)
    monkeypatch.setattr(cfg, "backup_interval_s", 0)

    import asyncio

    task = asyncio.create_task(backup.backup_loop(cfg, metrics))
    while len(calls) < 3:
        await asyncio.sleep(0)
    task.cancel()

    assert len(calls) >= 3
