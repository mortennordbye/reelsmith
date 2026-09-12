"""Per brand settings, the table a render host reads before it renders.

Three claims are worth pinning. Only allowlisted, non-secret keys can be
stored, because one bearer token writes every brand. A write is pinned to the
version it read, so two edits cannot silently overwrite each other. And nothing
here touches `accounts.brand`, which is what groups the live boards.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db
from gateway.app import create_app
from tests.gateway_harness import ACCOUNT, API_TOKEN, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
async def client(cfg):
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            yield http, app


async def put(http, brand: str, body: dict) -> httpx.Response:
    return await http.put(f"/api/brands/{brand}", json=body, headers=AUTH)


async def test_every_brand_route_needs_the_bearer_token(client):
    http, _ = client
    assert (await http.get("/api/brands")).status_code == 401
    assert (await http.get("/api/brands/thenightlybuild")).status_code == 401
    assert (await http.put("/api/brands/x", json={"settings": {}})).status_code == 401


async def test_a_brand_nobody_seeded_is_a_404_not_an_empty_row(client):
    """A render host treats a failed read as a reason to refuse the run. An
    empty 200 would read as "use every default", which is the soft failure the
    restructure was told to design against."""
    http, _ = client
    assert (await http.get("/api/brands/thenightlybuild", headers=AUTH)).status_code == 404


async def test_creating_then_reading_a_brand(client):
    http, _ = client
    created = await put(http, "thenightlybuild", {"settings": {"pipeline": "reel", "batch": 3}})

    assert created.status_code == 200, created.text
    assert created.json()["version"] == 1

    body = (await http.get("/api/brands/thenightlybuild", headers=AUTH)).json()
    assert body["brand"] == "thenightlybuild"
    assert body["settings"] == {"pipeline": "reel", "batch": 3}
    assert body["version"] == 1
    listed = (await http.get("/api/brands", headers=AUTH)).json()["brands"]
    assert [row["brand"] for row in listed] == ["thenightlybuild"]


async def test_a_key_outside_the_allowlist_is_refused(client):
    """The one way a token ends up in this table is somebody pasting an .env."""
    http, _ = client
    response = await put(http, "b", {"settings": {"batch": 1, "IG_ACCESS_TOKEN": "EAAG"}})

    assert response.status_code == 422
    assert (await http.get("/api/brands/b", headers=AUTH)).status_code == 404


@pytest.mark.parametrize(
    "settings_",
    [{"pipeline": "podcast"}, {"batch": 400}, {"max_queue": -1}, {"episode_crf": 99}],
)
async def test_a_value_outside_its_range_is_refused(client, settings_):
    http, _ = client
    assert (await put(http, "b", {"settings": settings_})).status_code == 422


async def test_a_brand_name_that_is_not_a_label_is_refused(client):
    http, _ = client
    assert (await put(http, "Not A Label", {"settings": {}})).status_code == 422


async def test_an_update_must_name_the_version_it_read(client):
    """Without it, the second of two edits silently wins."""
    http, _ = client
    await put(http, "b", {"settings": {"batch": 3}})

    unpinned = await put(http, "b", {"settings": {"batch": 4}})
    stale = await put(http, "b", {"settings": {"batch": 4}, "if_version": 7})
    pinned = await put(http, "b", {"settings": {"batch": 4}, "if_version": 1})

    assert unpinned.status_code == 409
    assert stale.status_code == 409
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["version"] == 2
    assert (await http.get("/api/brands/b", headers=AUTH)).json()["settings"] == {"batch": 4}


async def test_creating_twice_without_a_version_is_a_conflict(client):
    http, _ = client
    await put(http, "b", {"settings": {"batch": 3}})
    assert (await put(http, "b", {"settings": {"batch": 5}, "if_version": 0})).status_code == 409


async def test_a_put_replaces_rather_than_merges(client):
    """A key left out means the pipeline's default, so leaving one out has to
    clear it rather than keep a value somebody meant to remove."""
    http, _ = client
    await put(http, "b", {"settings": {"batch": 3, "max_queue": 6}})
    await put(http, "b", {"settings": {"batch": 3}, "if_version": 1})

    assert (await http.get("/api/brands/b", headers=AUTH)).json()["settings"] == {"batch": 3}


async def test_writing_settings_never_touches_the_account_grouping(client):
    """`accounts.brand` groups the live boards. The settings table is keyed on
    the same word and must never be the thing that moves it."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id=ACCOUNT, access_token="tok", username="nightly",
        brand="thenightlybuild",
    )

    await put(http, "thenightlybuild", {"settings": {"batch": 3}})
    await put(http, "nightly", {"settings": {"batch": 1}})

    row = await db.get_account(app.state.db, ACCOUNT)
    assert row["brand"] == "thenightlybuild"


async def test_the_migration_leaves_existing_brands_alone(cfg):
    """Built at 21 with a grouped account, migrated to 22: the label is intact
    and the new table is empty."""
    import sqlite3

    raw = sqlite3.connect(cfg.db_path)
    for script in db._MIGRATIONS[:21]:
        raw.executescript(script)
    raw.execute(
        "INSERT INTO accounts (account_id, username, access_token, created_at, brand) "
        "VALUES ('1', 'nightly', 'tok', '2026-09-01T00:00:00+00:00', 'thenightlybuild')"
    )
    raw.execute("PRAGMA user_version=21")
    raw.commit()
    raw.close()

    conn = await db.connect(cfg.db_path)
    try:
        assert (await db.get_account(conn, "1"))["brand"] == "thenightlybuild"
        assert await db.list_brands(conn) == []
    finally:
        await conn.close()
