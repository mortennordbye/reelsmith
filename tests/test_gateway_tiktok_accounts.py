"""A third destination sharing the queue without reaching the other two.

The claim the whole design rests on is that an account row says which service
it belongs to, and every loop written for one platform reads only its own rows.
If that is wrong the symptom is not a test failure, it is a TikTok open id
being handed to `graph.instagram.com` with an empty access token, against a
live account.

That was not hypothetical. The publish dispatch matched YouTube and **fell
through to Instagram**, so a row for a platform with no branch was handed to
Meta's publisher. It is the opposite of `db.active_accounts`, where the readers
default to Instagram precisely so a missed call site is inert; here the same
default was fail open. F1.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db, insights, poller, scheduler
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, API_TOKEN, OPEN_ID, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    # Zero poll interval and a two second ceiling, so a publisher that waits
    # for the wrong terminal state fails in this file rather than hanging the
    # run. That is not hypothetical: the inbox path is the default and it
    # finishes at SEND_TO_USER_INBOX, so a fake answering PUBLISH_COMPLETE
    # polls until the timeout.
    return settings(
        tmp_path,
        tiktok_enabled=True,
        tiktok_poll_interval_s=0,
        tiktok_publish_timeout_s=2,
    )


@pytest.fixture(autouse=True)
def staged_media(cfg):
    """The file every row in this file queues, on disk where the publisher
    looks for it.

    Autouse, because this file is about routing rather than about media, and
    the file existing is a precondition of publishing rather than anything
    under test. It became one on 2026-08-28: TikTok is pushed rather than
    fetched now, so the publisher reads the bytes instead of naming a URL, and
    a row whose file is missing fails before it reaches any of the assertions
    here.
    """
    cfg.covers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.covers_dir / "a.mp4").write_bytes(b"an mp4, more or less")


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    """One Instagram account and one TikTok account, which is the shape."""
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, account_id=ACCOUNT, access_token="tok")
    await db.upsert_account(
        connection,
        account_id=OPEN_ID,
        access_token="",
        username="@nightlybuild",
        platform=db.PLATFORM_TIKTOK,
    )
    await db.upsert_tiktok_credentials(
        connection,
        open_id=OPEN_ID,
        client_key="key",
        client_secret="secret",
        refresh_token="rft.original",
        refresh_expires_in=31_536_000,
    )
    yield connection
    await connection.close()


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


# --- What the Meta loops do not see -----------------------------------------


async def test_the_comment_poller_never_sees_a_tiktok_account(conn, meta, cfg, metrics):
    """There is no private reply API on TikTok, so `posts`, `comments_handled`,
    `conversations` and `deliveries` stay Instagram only, exactly as they
    already are for YouTube."""
    async with meta.client() as http:
        graph = GraphClient(http, cfg)
        await poller.poll_once(conn, graph, cfg, metrics)

    assert not any("open" in call for call in meta.calls)
    assert OPEN_ID not in " ".join(meta.calls)


async def test_the_meta_token_refresher_never_sees_a_tiktok_account(conn, meta, cfg, metrics):
    """A TikTok row has an empty `access_token` by construction, and handing
    that to Meta's refresh endpoint is an authorisation failure against a live
    app rather than a no-op."""
    async with meta.client() as http:
        graph = GraphClient(http, cfg)
        await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    # The Instagram account is refreshed, which is the loop working. What must
    # not happen is the TikTok open id reaching the same endpoint with the empty
    # token a TikTok row carries by construction.
    refreshes = [str(r.url) for r in meta.requests if "refresh_access_token" in str(r.url)]
    assert refreshes
    assert not [url for url in refreshes if OPEN_ID in url]


async def test_the_insights_sweep_never_sees_a_tiktok_account(conn, meta, cfg, metrics):
    """The `insights` columns are Meta's REELS metric names, `skip_rate`
    included, and TikTok exposes no retention metric of any kind."""
    async with meta.client() as http:
        graph = GraphClient(http, cfg)
        await insights.refresh_once(conn, graph, cfg, metrics)

    assert OPEN_ID not in " ".join(meta.calls)


# --- What the dispatch does ---------------------------------------------------


async def test_a_row_for_an_unknown_platform_fails_its_own_row(conn, meta, cfg, metrics):
    """F1, and the reason this file exists.

    Instagram used to be the fallthrough, so this row would have reached
    `graph.instagram.com`. It fails itself instead, and the tick survives so
    one misconfigured account cannot stop the other two publishing.
    """
    await db.upsert_account(
        conn, account_id="_mystery", access_token="", platform="myspace"
    )
    account = await db.get_account(conn, "_mystery")
    queued_id = await db.enqueue_post(
        conn, account_id="_mystery", video_name="a.mp4", cover_name=None,
        caption="", keyword="X", link=LINK, repo_full_name="a/b", approved=True,
    )
    queued = await db.get_queued(conn, queued_id)

    async with meta.client() as http:
        graph = GraphClient(http, cfg)
        published, retry = await scheduler.publish_queued(
            conn, graph, cfg, metrics, account=account, queued=queued
        )

    assert (published, retry) == (False, False)
    assert meta.calls == []
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_FAILED
    assert "myspace" in row["failure"]


async def test_the_failure_is_counted_under_the_platform_that_caused_it(
    conn, meta, cfg, metrics
):
    """Unlabelled, `ReelsmithPublishFailing` fired without saying where. F7."""
    await db.upsert_account(
        conn, account_id="_mystery", access_token="", platform="myspace"
    )
    account = await db.get_account(conn, "_mystery")
    queued_id = await db.enqueue_post(
        conn, account_id="_mystery", video_name="a.mp4", cover_name=None,
        caption="", keyword="X", link=LINK, repo_full_name="a/b", approved=True,
    )

    async with meta.client() as http:
        await scheduler.publish_queued(
            conn, GraphClient(http, cfg), cfg, metrics,
            account=account, queued=await db.get_queued(conn, queued_id),
        )

    assert metrics.registry.get_sample_value(
        "reelsmith_publish_failures_total", {"platform": "myspace"}
    ) == 1
    assert metrics.registry.get_sample_value(
        "reelsmith_publish_failures_total", {"platform": db.PLATFORM_INSTAGRAM}
    ) == 0


async def test_a_tiktok_row_reaches_tiktok_and_not_meta(conn, meta, cfg, metrics):
    """The other half of the same claim: routed, not merely not-misrouted."""
    # The inbox path is the default, and it finishes here rather than at
    # PUBLISH_COMPLETE.
    meta.tiktok.statuses = ["SEND_TO_USER_INBOX"]
    account = await db.get_account(conn, OPEN_ID)
    queued_id = await db.enqueue_post(
        conn, account_id=OPEN_ID, video_name="a.mp4", cover_name=None,
        caption="a caption", keyword="X", link=LINK, repo_full_name="a/b",
        approved=True,
    )

    async with meta.client() as http:
        published, retry = await scheduler.publish_queued(
            conn, GraphClient(http, cfg), cfg, metrics,
            account=account, queued=await db.get_queued(conn, queued_id),
        )

    assert published is True
    assert meta.tiktok.inits, "the TikTok init was never called"
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_PUBLISHED
    assert row["media_id"] == meta.tiktok.publish_id
    assert metrics.registry.get_sample_value(
        "reelsmith_posts_published_total", {"platform": db.PLATFORM_TIKTOK}
    ) == 1


async def test_publishing_rotates_the_refresh_token_before_it_posts(conn, meta, cfg, metrics):
    """The refresh token just spent is dead, and a publish that throws must not
    take the new one with it."""
    meta.tiktok.statuses = ["FAILED"]
    account = await db.get_account(conn, OPEN_ID)
    queued_id = await db.enqueue_post(
        conn, account_id=OPEN_ID, video_name="a.mp4", cover_name=None,
        caption="c", keyword="X", link=LINK, repo_full_name="a/b", approved=True,
    )

    async with meta.client() as http:
        await scheduler.publish_queued(
            conn, GraphClient(http, cfg), cfg, metrics,
            account=account, queued=await db.get_queued(conn, queued_id),
        )

    stored = await db.tiktok_credentials(conn, OPEN_ID)
    assert stored["refresh_token"] == "rft.rotated"


async def test_the_flag_being_off_stops_a_publish_rather_than_retrying_it(
    conn, meta, cfg, metrics
):
    """The same decision `scheduler_enabled` makes one level up: publishing to
    a third real account is a choice rather than something gained by upgrading.

    Failed rather than approved, because a flag that is off is not a transient
    condition and a row retrying against it forever would look like an API
    problem rather than a configuration one.
    """
    off = settings(cfg.db_path.parent, tiktok_enabled=False)
    account = await db.get_account(conn, OPEN_ID)
    queued_id = await db.enqueue_post(
        conn, account_id=OPEN_ID, video_name="a.mp4", cover_name=None,
        caption="c", keyword="X", link=LINK, repo_full_name="a/b", approved=True,
    )

    async with meta.client() as http:
        published, retry = await scheduler.publish_queued(
            conn, GraphClient(http, off), off, metrics,
            account=account, queued=await db.get_queued(conn, queued_id),
        )

    assert (published, retry) == (False, False)
    assert meta.tiktok.inits == []
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_FAILED
    assert "GATEWAY_TIKTOK_ENABLED" in row["failure"]


# --- Registration -------------------------------------------------------------


async def test_registering_a_tiktok_account_stores_it_apart_from_meta(client):
    http, app = client

    response = await http.post(
        "/api/accounts/tiktok",
        headers=AUTH,
        json={
            "open_id": OPEN_ID,
            "client_key": "key",
            "client_secret": "secret",
            "refresh_token": "rft.seed",
            "refresh_expires_in": 31_536_000,
            "username": "@nightlybuild",
        },
    )

    assert response.status_code == 200
    account = await db.get_account(app.state.db, OPEN_ID)
    assert account["platform"] == db.PLATFORM_TIKTOK
    assert account["access_token"] == ""
    stored = await db.tiktok_credentials(app.state.db, OPEN_ID)
    assert stored["refresh_token"] == "rft.seed"
    # Not visible to any Meta loop, which is the whole reason for the column.
    assert await db.all_accounts(app.state.db) == []


async def test_a_handle_pasted_instead_of_an_open_id_is_refused(client):
    """There is no `UC` prefix to check the way a channel id has, so this
    catches only the obvious mistake. It is still the mistake that would
    register cleanly and fail at the first publish, weeks later."""
    http, _ = client

    response = await http.post(
        "/api/accounts/tiktok",
        headers=AUTH,
        json={
            "open_id": "@nightlybuild",
            "client_key": "k",
            "client_secret": "s",
            "refresh_token": "r",
        },
    )

    assert response.status_code == 422


async def test_the_secret_never_reaches_the_response(client):
    http, _ = client

    response = await http.post(
        "/api/accounts/tiktok",
        headers=AUTH,
        json={
            "open_id": OPEN_ID, "client_key": "key",
            "client_secret": "the-secret", "refresh_token": "rft.seed",
        },
    )

    assert "the-secret" not in response.text
    assert "rft.seed" not in response.text
