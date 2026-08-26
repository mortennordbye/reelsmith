"""`ig_user_id` becomes `account_id`, and nothing else changes.

F10. The column was named when there was one platform and every row held a Meta
user id. It holds a YouTube channel id on some rows now and will hold a TikTok
open id on others, so the name is wrong on two thirds of what it is about to
carry and reads as a bug to anyone who has not been told otherwise.

A rename with no behaviour is proved by the suite passing, which is most of this
directory. What the suite cannot prove on its own is the three places the rename
had to stop, and those are what this file is for: a database with rows in it, a
render host that has not been pulled yet, and a Prometheus label the alert rules
in another repo read by name.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from gateway import db
from gateway.app import create_app
from gateway.metrics import Metrics
from gateway.models import PostRegistration, QueueSubmission, RenderedRepo
from tests.gateway_harness import ACCOUNT, API_TOKEN, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"
OTHER = "17841400000000009"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


# --- A database that already has rows in it ---------------------------------


async def test_the_rename_keeps_the_rows_it_renames(tmp_path):
    """The live database is not empty and the migration is not reversible.

    A v14 file is built with the stdlib driver and then opened through the real
    `db.connect`, which is what runs migration 15 in production. Building it
    with `db.connect` is not possible, because that migrates to the newest
    version before there is anything to migrate.
    """
    path = tmp_path / "v14.sqlite3"
    plain = sqlite3.connect(path)
    for statement in db._MIGRATIONS[:14]:
        plain.executescript(statement)
    plain.execute("PRAGMA user_version=14")
    plain.execute(
        "INSERT INTO accounts (ig_user_id, access_token, username, created_at) "
        "VALUES (?, ?, ?, '2026-08-01T00:00:00+00:00')",
        (ACCOUNT, "tok", "nightly"),
    )
    plain.execute(
        "INSERT INTO posts (media_id, ig_user_id, keyword, link, registered_at) "
        "VALUES ('m1', ?, 'send', ?, '2026-08-01T00:00:00+00:00')",
        (ACCOUNT, LINK),
    )
    plain.commit()
    plain.close()

    conn = await db.connect(path)
    try:
        account = await db.get_account(conn, ACCOUNT)
        posts = await db.pollable_posts(conn, ACCOUNT, ttl_days=365_000)
    finally:
        await conn.close()

    assert account is not None
    assert account["account_id"] == ACCOUNT
    assert account["username"] == "nightly"
    assert [p["media_id"] for p in posts] == ["m1"]


async def test_the_indexes_follow_the_column(tmp_path):
    """SQLite's RENAME COLUMN rewrites them, and an index left naming a column
    that no longer exists would fail at the next query rather than here."""
    conn = await db.connect(tmp_path / "fresh.sqlite3")
    async with conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ) as cur:
        sql = " ".join(row[0] for row in await cur.fetchall())
    await conn.close()

    assert "ig_user_id" not in sql
    assert "account_id" in sql


# --- A render host that has not been pulled yet -----------------------------


@pytest.mark.parametrize(
    "model",
    [PostRegistration, QueueSubmission, RenderedRepo],
)
def test_a_body_still_saying_ig_user_id_is_accepted(model):
    """The gateway image deploys itself and the render host is pulled by hand,
    so the side that lags is always the one sending the old name. Refusing it
    would turn a rename with no behaviour into a broken publish."""
    fields = {
        PostRegistration: {"media_id": "m", "link": LINK},
        QueueSubmission: {"video_name": "a.mp4", "link": LINK},
        RenderedRepo: {"repo_full_name": "a/b"},
    }[model]

    parsed = model(ig_user_id=ACCOUNT, **fields)

    assert parsed.account_id == ACCOUNT


def test_the_new_name_is_what_a_body_is_written_with():
    assert PostRegistration(account_id=ACCOUNT, media_id="m", link=LINK).account_id == ACCOUNT


async def test_a_read_still_saying_ig_user_id_is_scoped_the_same_way(cfg, meta):
    """The dangerous direction. An ignored parameter is not an error, it is
    every account's commitments answered as one account's, which is the F8
    failure this rename must not reintroduce."""
    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        await db.record_rendered(conn, repo_full_name="a/one", account_id=ACCOUNT)
        await db.record_rendered(conn, repo_full_name="b/two", account_id=OTHER)
    finally:
        await conn.close()

    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            old = await http.get(f"/api/rendered?ig_user_id={ACCOUNT}", headers=AUTH)
            new = await http.get(f"/api/rendered?account_id={ACCOUNT}", headers=AUTH)
            everything = await http.get("/api/rendered", headers=AUTH)

    assert [r["repo_full_name"] for r in old.json()["rendered"]] == ["a/one"]
    assert old.json() == new.json()
    # The failure an ignored parameter would look like, so the test would still
    # fail if the alias were dropped and the read silently widened.
    assert len(everything.json()["rendered"]) == 2


# --- A label the alert rules read by name -----------------------------------


def test_the_token_gauge_keeps_its_old_label():
    """A Prometheus label is part of a series' identity, and the rules that read
    it live in the homelab repo. It moves in a homelab PR or not at all, and a
    rename with no behaviour in it is not the change that should break paging."""
    metrics = Metrics()
    metrics.token_days_left.labels(ig_user_id=ACCOUNT).set(30)

    assert 'ig_user_id="17841400000000000"' in metrics.export().decode()
