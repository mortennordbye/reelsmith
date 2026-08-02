"""The routes the Mac calls, and the two the cluster calls.

The cover route is the one with teeth: it is unauthenticated by necessity,
because Meta fetches it from its own servers, so the filename it serves has to
be impossible to steer.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gateway import api, db
from gateway.app import create_app
from tests.gateway_harness import ACCOUNT, API_TOKEN, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
async def client(cfg, meta):
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            yield http, app


# --- Liveness and metrics ---------------------------------------------------


async def test_healthz_needs_no_auth(client):
    http, _ = client
    assert (await http.get("/healthz")).text == "ok"


async def test_metrics_serves_prometheus_text(client):
    http, _ = client
    response = await http.get("/metrics")

    assert response.status_code == 200
    assert "reelsmith_links_sent_total" in response.text


# --- Auth -------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer wrong"}, {"authorization": API_TOKEN}],
)
async def test_the_pipeline_routes_refuse_a_bad_token(client, headers):
    http, _ = client
    response = await http.post(
        "/api/posts",
        json={"media_id": "m", "ig_user_id": ACCOUNT, "link": LINK},
        headers=headers,
    )

    assert response.status_code == 401


# --- Post registration ------------------------------------------------------


async def test_registering_a_post_starts_the_poller_watching_it(client):
    http, app = client

    response = await http.post(
        "/api/posts",
        json={"media_id": "media-1", "ig_user_id": ACCOUNT, "link": LINK, "keyword": "SEND"},
        headers=AUTH,
    )

    assert response.status_code == 200
    row = await db.get_post(app.state.db, "media-1")
    assert row["link"] == LINK
    assert row["keyword"] == "SEND"


async def test_a_measure_only_registration_says_so_in_its_reply(client):
    """The wording is a contract, not a log line.

    A gateway older than schema v8 drops `poll_comments` silently, because
    pydantic ignores a field its model does not declare, and arms the poller
    instead. `pipeline/gateway.register_post` reads this reply to tell the two
    apart, so changing "measuring" to anything else turns a caught deploy skew
    back into a DM going out about a Reel from last week.
    """
    http, app = client

    response = await http.post(
        "/api/posts",
        json={
            "media_id": "media-1",
            "ig_user_id": ACCOUNT,
            "link": LINK,
            "poll_comments": False,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["detail"].startswith("measuring")
    assert await db.pollable_posts(app.state.db, ACCOUNT, ttl_days=7) == []


async def test_a_normal_registration_still_says_watching(client):
    http, app = client

    response = await http.post(
        "/api/posts",
        json={"media_id": "media-1", "ig_user_id": ACCOUNT, "link": LINK},
        headers=AUTH,
    )

    assert response.json()["detail"].startswith("watching")


async def test_re_registering_fixes_a_wrong_link(client):
    http, app = client
    payload = {"media_id": "media-1", "ig_user_id": ACCOUNT, "link": "https://wrong.example"}
    await http.post("/api/posts", json=payload, headers=AUTH)

    await http.post("/api/posts", json={**payload, "link": LINK}, headers=AUTH)

    row = await db.get_post(app.state.db, "media-1")
    assert row["link"] == LINK


@pytest.mark.parametrize(
    "bad",
    [
        {"link": "github.com/no/scheme"},
        {"keyword": "two words"},
        {"keyword": ""},
        {"media_id": ""},
    ],
)
async def test_a_malformed_registration_is_refused_with_the_field_named(client, bad):
    http, _ = client
    payload = {"media_id": "m", "ig_user_id": ACCOUNT, "link": LINK, **bad}

    response = await http.post("/api/posts", json=payload, headers=AUTH)

    assert response.status_code == 422


# --- Accounts ---------------------------------------------------------------


async def test_registering_an_account_subscribes_it_to_messages(client, meta):
    """Without the subscription the account produces no webhooks at all.

    That failure looks exactly like "nobody is messaging us", so it happens on
    registration rather than being left to a checklist.
    """
    http, app = client

    response = await http.post(
        "/api/accounts",
        json={"ig_user_id": ACCOUNT, "access_token": "tok", "expires_in": 5_184_000},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert any("subscribed_apps" in call for call in meta.calls)
    row = await db.get_account(app.state.db, ACCOUNT)
    assert row["token_expires_at"] is not None


async def test_re_authorising_does_not_un_pause_an_account(client):
    http, app = client
    await http.post(
        "/api/accounts",
        json={"ig_user_id": ACCOUNT, "access_token": "tok", "subscribe": False},
        headers=AUTH,
    )
    await db.set_account_flags(app.state.db, ACCOUNT, active=False, dm_enabled=False)

    await http.post(
        "/api/accounts",
        json={"ig_user_id": ACCOUNT, "access_token": "fresher", "subscribe": False},
        headers=AUTH,
    )

    row = await db.get_account(app.state.db, ACCOUNT)
    assert row["access_token"] == "fresher"
    assert row["active"] == 0
    assert row["dm_enabled"] == 0


# --- Covers -----------------------------------------------------------------


async def test_a_cover_round_trips_from_upload_to_the_url_meta_fetches(client, cfg):
    http, _ = client

    upload = await http.post(
        "/api/covers",
        files={"file": ("cover.png", PNG, "image/png")},
        data={"slug": "DietrichGebert/ponytail"},
        headers=AUTH,
    )

    assert upload.status_code == 200
    url = upload.json()["url"]
    assert url.startswith("https://gate.example.test/covers/")

    fetched = await http.get(httpx.URL(url).path)
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert fetched.headers["content-type"] == "image/png"


async def test_the_same_cover_uploaded_twice_keeps_one_name(client):
    http, _ = client
    files = {"file": ("cover.png", PNG, "image/png")}

    first = await http.post("/api/covers", files=files, data={"slug": "x"}, headers=AUTH)
    second = await http.post("/api/covers", files=files, data={"slug": "x"}, headers=AUTH)

    assert first.json()["name"] == second.json()["name"]


def test_a_cover_name_can_never_climb_out_of_the_directory():
    name = api.safe_cover_name("../../etc/passwd", PNG)

    assert "/" not in name
    assert ".." not in name
    assert name.endswith(".png")


@pytest.mark.parametrize("name", ["../../../etc/passwd", "..%2Fsecret.png", "nope.txt"])
async def test_the_cover_route_serves_nothing_but_covers(client, name):
    http, _ = client
    response = await http.get(f"/covers/{name}")

    assert response.status_code == 404


# --- Media, which is why publishing works at all -----------------------------

MP4 = b"\x00\x00\x00\x20ftypisom" + b"0" * 512


async def test_a_video_round_trips_and_is_served_as_video(client):
    """Meta fetches the MP4 from a public URL on the Instagram Login path.
    `upload_type=resumable` is Facebook Login only, so without this there is no
    publish at all."""
    http, _ = client

    up = await http.post(
        "/api/media", files={"file": ("out.mp4", MP4, "video/mp4")},
        data={"slug": "xai-org-grok-build"}, headers=AUTH,
    )

    assert up.status_code == 200
    url = up.json()["url"]
    assert "/media/" in url and url.endswith(".mp4")

    fetched = await http.get(httpx.URL(url).path)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "video/mp4"
    assert fetched.content == MP4


async def test_media_refuses_a_type_meta_would_not_take(client):
    http, _ = client
    r = await http.post(
        "/api/media", files={"file": ("notes.txt", b"hello", "text/plain")}, headers=AUTH
    )
    assert r.status_code == 415


async def test_media_needs_the_bearer_token(client):
    http, _ = client
    r = await http.post("/api/media", files={"file": ("out.mp4", MP4, "video/mp4")})
    assert r.status_code == 401


async def test_old_media_is_pruned_on_upload(client, cfg):
    """The volume is 1Gi and shared with the database. Meta fetches the file
    once, at container creation, so nothing needs to live long."""
    import os
    import time

    http, _ = client
    stale = Path(cfg.covers_dir)
    stale.mkdir(parents=True, exist_ok=True)
    old = stale / "old-deadbeef.mp4"
    old.write_bytes(b"x")
    ancient = time.time() - 30 * 86_400
    os.utime(old, (ancient, ancient))

    await http.post(
        "/api/media", files={"file": ("out.mp4", MP4, "video/mp4")}, headers=AUTH
    )

    assert not old.exists(), "a month-old video should not survive an upload"


async def test_pruning_only_ever_touches_media(client, cfg):
    """The guard that matters. If covers_dir were ever pointed at /state, an
    age sweep would delete gateway.sqlite3 and every conversation in it."""
    import os
    import time

    http, _ = client
    directory = Path(cfg.covers_dir)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "gateway.sqlite3"
    database.write_bytes(b"pretend database")
    ancient = time.time() - 365 * 86_400
    os.utime(database, (ancient, ancient))

    await http.post(
        "/api/media", files={"file": ("out.mp4", MP4, "video/mp4")}, headers=AUTH
    )

    assert database.exists(), "a year-old database must survive a media prune"


async def test_an_empty_upload_is_refused(client):
    http, _ = client
    response = await http.post(
        "/api/covers", files={"file": ("cover.png", b"", "image/png")}, headers=AUTH
    )

    assert response.status_code == 400


# --- The queue gauge ------------------------------------------------------
#
# ReelsmithPostStuck alerts on reelsmith_queue_depth{state="failed"}. The gauge
# was declared for months and never written to, so the alert it exists for was
# not expressible.


async def test_the_metrics_endpoint_publishes_the_queue_depth(client):
    http, app = client
    await db.enqueue_post(
        app.state.db, ig_user_id=ACCOUNT, video_name="a.mp4", cover_name=None,
        caption="", keyword="UV", link=LINK, repo_full_name="astral-sh/uv",
        approved=False,
    )

    body = (await http.get("/metrics")).text

    assert 'reelsmith_queue_depth{state="draft"} 1.0' in body


async def test_every_state_is_published_even_at_zero(client):
    """A gauge that only reports what exists leaves a series at its last
    non-zero value forever, so the alert that fired never resolves."""
    http, _ = client

    body = (await http.get("/metrics")).text

    for state in db.QUEUE_STATES:
        assert f'reelsmith_queue_depth{{state="{state}"}} 0.0' in body


async def test_a_row_leaving_failed_takes_the_gauge_back_down(client):
    http, app = client
    queued_id = await db.enqueue_post(
        app.state.db, ig_user_id=ACCOUNT, video_name="a.mp4", cover_name=None,
        caption="", keyword="UV", link=LINK, repo_full_name="astral-sh/uv",
        approved=False,
    )
    await db.set_queue_state(app.state.db, queued_id, db.QUEUE_FAILED)
    assert 'reelsmith_queue_depth{state="failed"} 1.0' in (await http.get("/metrics")).text

    await db.set_queue_state(app.state.db, queued_id, db.QUEUE_CANCELLED)
    assert 'reelsmith_queue_depth{state="failed"} 0.0' in (await http.get("/metrics")).text
