"""The render host's report that a night ran.

Before this, a host that rendered nothing looked the same as one whose queue
ceiling stopped the batch, and the first sign was the feed going dark days
later. What is worth pinning: a start and a finish land on one row, a start
with no finish stays visibly running, a malformed report is refused, and the
per brand gauge exists for every brand at zero before its first run.
"""

from __future__ import annotations

from datetime import UTC, datetime

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


async def report(http, **body) -> httpx.Response:
    return await http.post("/api/runs", json=body, headers=AUTH)


async def test_the_route_needs_the_bearer_token(client):
    http, _ = client
    body = {"brand": "b", "kind": "batch"}
    assert (await http.post("/api/runs", json=body)).status_code == 401


async def test_a_start_and_a_finish_are_one_run(client):
    http, app = client
    started = await report(
        http, brand="thenightlybuild", kind="batch", host="verksted",
        started_at="2026-09-13T00:00:05+00:00",
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["id"]

    finished = await report(
        http, run_id=run_id, brand="thenightlybuild", kind="batch", outcome="ok",
        finished_at="2026-09-13T00:42:00+00:00",
        results=[{"subject": "astral-sh/uv", "outcome": "queued"}],
    )

    assert finished.status_code == 200, finished.text
    runs = await db.latest_runs(app.state.db)
    assert [(r["id"], r["outcome"], r["finished_at"][:16]) for r in runs] == [
        (run_id, "ok", "2026-09-13T00:42"),
    ]


async def test_a_start_with_no_finish_stays_running(client):
    """The failure this exists for: a host that died mid batch."""
    http, app = client
    await report(http, brand="thenightlybuild", kind="batch")

    (run,) = await db.latest_runs(app.state.db)
    assert run["outcome"] == "running"
    assert run["finished_at"] is None


async def test_finishing_a_run_that_does_not_exist_is_a_404(client):
    http, _ = client
    response = await report(http, run_id=999, brand="b", kind="batch", outcome="ok")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"brand": "b", "kind": "lunch"},
        {"brand": "b", "kind": "batch", "outcome": "great"},
        {"brand": "Not A Brand", "kind": "batch"},
        {"brand": "b", "kind": "batch", "token": "x"},
    ],
)
async def test_a_malformed_report_is_refused(client, body):
    http, _ = client
    assert (await http.post("/api/runs", json=body, headers=AUTH)).status_code == 422


async def test_the_gauge_exists_for_a_brand_that_never_ran(client):
    """A series that only appears after the first run cannot alert on a host
    that never started reporting."""
    http, _ = client
    text = (await http.get("/metrics")).text

    assert 'reelsmith_last_run_timestamp{brand="thenightlybuild"} 0.0' in text


async def test_the_gauge_moves_when_a_run_finishes(client):
    http, _ = client
    started = await report(http, brand="thenightlybuild", kind="batch")
    await report(
        http, run_id=started.json()["id"], brand="thenightlybuild", kind="batch",
        outcome="ok", finished_at="2026-09-13T00:42:00+00:00",
    )

    text = (await http.get("/metrics")).text
    line = next(
        row for row in text.splitlines()
        if row.startswith('reelsmith_last_run_timestamp{brand="thenightlybuild"}')
    )

    expected = datetime(2026, 9, 13, 0, 42, tzinfo=UTC).timestamp()
    assert float(line.rsplit(" ", 1)[1]) == pytest.approx(expected)


async def test_the_latest_run_per_brand_is_the_newest_start(client):
    http, app = client
    await report(http, brand="b", kind="batch", started_at="2026-09-11T00:00:00+00:00")
    await report(http, brand="b", kind="recover", started_at="2026-09-12T03:00:00+00:00")
    await report(http, brand="c", kind="episode", started_at="2026-09-12T01:00:00+00:00")

    latest = {r["brand"]: r["kind"] for r in await db.latest_runs(app.state.db)}

    assert latest == {"b": "recover", "c": "episode"}
