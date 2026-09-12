"""The cooldown as a table of its own, per brand, keyed on a subject.

What is worth pinning: a merge never moves a commitment later, a brand's
commitment does not cover another brand, `GET /api/covered` keeps its shape
and still includes what the queue says, and the migration backfills the queue
without touching `accounts.brand`.
"""

from __future__ import annotations

import sqlite3

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
            await db.upsert_account(
                app.state.db, account_id=ACCOUNT, access_token="tok", username="nightly",
                brand="thenightlybuild",
            )
            yield http, app


async def post(http, **body) -> httpx.Response:
    return await http.post("/api/covered", json=body, headers=AUTH)


async def covered(http, **params) -> dict[str, str]:
    response = await http.get("/api/covered", params=params, headers=AUTH)
    assert response.status_code == 200, response.text
    return {row["repo_full_name"]: row["covered_at"] for row in response.json()["covered"]}


async def test_the_write_routes_need_the_bearer_token(client):
    http, _ = client
    body = {"brand": "b", "subject_key": "repo:a/b"}
    assert (await http.post("/api/covered", json=body)).status_code == 401
    assert (await http.delete("/api/covered/repo:a/b?brand=b")).status_code == 401


async def test_a_posted_repo_the_queue_never_saw_is_covered(client):
    """The case the table exists for: `--posted` marks a repo by hand."""
    http, _ = client
    response = await post(
        http, brand="thenightlybuild", subject_key="repo:DietrichGebert/ponytail",
        committed_at="2026-08-10T00:00:00+00:00", source="posted",
    )

    assert response.status_code == 200, response.text
    assert "DietrichGebert/ponytail" in await covered(http, account_id=ACCOUNT)
    assert "DietrichGebert/ponytail" in await covered(http)


async def test_a_later_date_never_moves_a_commitment(client):
    """Taking the later one would extend the cooldown by the disagreement."""
    http, app = client
    await post(http, brand="b", subject_key="repo:a/b", committed_at="2026-08-01T00:00:00+00:00")
    later = await post(
        http, brand="b", subject_key="repo:a/b", committed_at="2026-09-01T00:00:00+00:00"
    )
    earlier = await post(
        http, brand="b", subject_key="repo:a/b", committed_at="2026-07-01T00:00:00+00:00"
    )

    assert "already covered" in later.json()["detail"]
    assert "covered for b" in earlier.json()["detail"]
    rows = await db.covered_subjects(app.state.db, "b")
    assert [row["committed_at"][:10] for row in rows] == ["2026-07-01"]


async def test_one_brands_commitment_does_not_cover_another(client):
    """A cooldown is a fact about one audience (F9)."""
    http, _ = client
    await post(http, brand="thewholequote", subject_key="repo:a/b")

    assert "a/b" not in await covered(http, account_id=ACCOUNT)


async def test_a_person_is_a_subject_too(client):
    """Episodes had no cooldown at all, because nothing was keyed on anything
    but a repo. Listed per brand; not mixed into the repo shaped list."""
    http, app = client
    response = await post(http, brand="thewholequote", subject_key="person:Q3057690")
    assert response.status_code == 200

    rows = await db.covered_subjects(app.state.db, "thewholequote")
    assert [row["subject_key"] for row in rows] == ["person:Q3057690"]
    assert await covered(http) == {}


@pytest.mark.parametrize(
    "body",
    [
        {"brand": "b", "subject_key": "a/b"},
        {"brand": "b", "subject_key": "person:Montaigne"},
        {"brand": "Not A Brand", "subject_key": "repo:a/b"},
        {"brand": "b", "subject_key": "repo:a/b", "extra": 1},
    ],
)
async def test_a_malformed_commitment_is_refused(client, body):
    http, _ = client
    assert (await http.post("/api/covered", json=body, headers=AUTH)).status_code == 422


async def test_forgetting_needs_the_brand_and_is_idempotent(client):
    http, _ = client
    await post(http, brand="thenightlybuild", subject_key="repo:a/b")
    await post(http, brand="thewholequote", subject_key="repo:a/b")

    scope = {"brand": "thenightlybuild"}
    first = await http.delete("/api/covered/repo:a/b", params=scope, headers=AUTH)
    again = await http.delete("/api/covered/repo:a/b", params=scope, headers=AUTH)

    assert "no longer covered" in first.json()["detail"]
    assert "was not covered" in again.json()["detail"]
    assert "a/b" not in await covered(http, account_id=ACCOUNT)
    assert "a/b" in await covered(http)


async def test_the_migration_backfills_the_queue_by_brand(cfg):
    """Every non-cancelled queue row with a repo, per brand, earliest first,
    and the account grouping left exactly as it was."""
    raw = sqlite3.connect(cfg.db_path)
    for script in db._MIGRATIONS[:22]:
        raw.executescript(script)
    raw.execute(
        "INSERT INTO accounts (account_id, username, access_token, created_at, brand) "
        "VALUES ('1', 'nightly', 'tok', '2026-09-01T00:00:00+00:00', 'thenightlybuild')"
    )
    columns = {row[1] for row in raw.execute("PRAGMA table_info(queued_posts)")}
    base = {
        "account_id": "1", "video_name": "v.mp4", "keyword": "K", "link": "",
        "position": 0,
    }
    rows = [
        ("astral-sh/uv", "published", "2026-08-02T00:00:00+00:00"),
        ("astral-sh/uv", "draft", "2026-08-01T00:00:00+00:00"),
        ("gone/away", "cancelled", "2026-08-03T00:00:00+00:00"),
    ]
    for repo, state, created in rows:
        values = {**base, "repo_full_name": repo, "state": state, "created_at": created}
        values = {k: v for k, v in values.items() if k in columns}
        marks = ",".join("?" * len(values))
        raw.execute(
            f"INSERT INTO queued_posts ({','.join(values)}) VALUES ({marks})", list(values.values())
        )
    raw.execute("PRAGMA user_version=22")
    raw.commit()
    raw.close()

    conn = await db.connect(cfg.db_path)
    try:
        subjects = await db.covered_subjects(conn, "thenightlybuild")
        assert [(r["subject_key"], r["committed_at"][:10]) for r in subjects] == [
            ("repo:astral-sh/uv", "2026-08-01"),
        ]
        assert (await db.get_account(conn, "1"))["brand"] == "thenightlybuild"
    finally:
        await conn.close()
