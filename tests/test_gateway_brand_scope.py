"""`?brand=` on the reads a render host makes before it renders.

An identity is several destinations, and until this a read was scoped to one
account id or to nothing. An account with no Instagram sent nothing and read
every account's lists. What is worth pinning: a brand reads all of its own
destinations and none of another's, a brand naming nobody reads nothing, and
the old parameters behave exactly as they did.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db
from gateway.app import create_app
from tests.gateway_harness import API_TOKEN, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
IG_ONE, YT_ONE, IG_TWO = "17841400000000001", "UConeoneoneoneoneoneone1", "17841400000000002"


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
            conn = app.state.db
            await db.upsert_account(
                conn, account_id=IG_ONE, access_token="t", username="one", brand="one"
            )
            await db.upsert_account(
                conn, account_id=YT_ONE, access_token="t", username="@one",
                platform=db.PLATFORM_YOUTUBE, brand="one",
            )
            await db.upsert_account(
                conn, account_id=IG_TWO, access_token="t", username="two", brand="two"
            )
            yield http, app


async def get(http, path: str, **params) -> dict:
    response = await http.get(path, params=params, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_brand_reads_every_destination_it_holds_and_no_other(client):
    http, app = client
    conn = app.state.db
    await db.record_rendered(conn, repo_full_name="a/ig", account_id=IG_ONE)
    await db.record_rendered(conn, repo_full_name="a/yt", account_id=YT_ONE)
    await db.record_rendered(conn, repo_full_name="b/ig", account_id=IG_TWO)

    body = await get(http, "/api/rendered", brand="one")
    names = {r["repo_full_name"] for r in body["rendered"]}

    assert names == {"a/ig", "a/yt"}


async def test_a_brand_naming_nobody_reads_nothing_rather_than_everything(client):
    """A misspelt brand that returned the whole database would hand one
    identity every other identity's cooldowns."""
    http, app = client
    await db.record_rendered(app.state.db, repo_full_name="a/ig", account_id=IG_ONE)
    await db.record_covered(
        app.state.db, brand="one", subject_key="repo:a/posted",
        committed_at="2026-08-01T00:00:00+00:00",
    )

    assert (await get(http, "/api/rendered", brand="onee"))["rendered"] == []
    assert (await get(http, "/api/covered", brand="onee"))["covered"] == []
    assert (await get(http, "/api/queue", brand="onee"))["queue"] == []
    assert (await get(http, "/api/results", brand="onee"))["results"] == []


async def test_covered_by_brand_includes_its_table_rows_once(client):
    http, app = client
    await db.record_covered(
        app.state.db, brand="one", subject_key="repo:a/posted",
        committed_at="2026-08-01T00:00:00+00:00",
    )
    await db.record_covered(
        app.state.db, brand="two", subject_key="repo:b/posted",
        committed_at="2026-08-01T00:00:00+00:00",
    )

    rows = (await get(http, "/api/covered", brand="one"))["covered"]

    assert [r["repo_full_name"] for r in rows] == ["a/posted"]


async def test_the_old_parameters_are_unchanged(client):
    """The render host is pulled by hand, so the side that lags keeps sending
    `account_id` or `ig_user_id` and must get the answer it always got."""
    http, app = client
    await db.record_rendered(app.state.db, repo_full_name="a/ig", account_id=IG_ONE)
    await db.record_rendered(app.state.db, repo_full_name="a/yt", account_id=YT_ONE)

    async def names(**params) -> set[str]:
        body = await get(http, "/api/rendered", **params)
        return {r["repo_full_name"] for r in body["rendered"]}

    by_id = await names(account_id=IG_ONE)
    by_old = await names(ig_user_id=IG_ONE)
    unscoped = await names()

    assert by_id == by_old == {"a/ig"}
    assert unscoped == {"a/ig", "a/yt"}


async def test_a_brand_sent_with_an_account_id_wins(client):
    """A render host sends both, so a gateway that predates `brand` still
    scopes by account. If the brand narrowed to that account instead, an up to
    date host would never see its other destinations."""
    http, app = client
    await db.record_rendered(app.state.db, repo_full_name="a/ig", account_id=IG_ONE)
    await db.record_rendered(app.state.db, repo_full_name="a/yt", account_id=YT_ONE)
    await db.record_rendered(app.state.db, repo_full_name="b/ig", account_id=IG_TWO)

    rows = (await get(http, "/api/rendered", brand="one", account_id=IG_ONE))["rendered"]

    assert {r["repo_full_name"] for r in rows} == {"a/ig", "a/yt"}
