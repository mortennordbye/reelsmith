"""A second destination sharing the queue without reaching the Meta paths.

The whole design of publishing to YouTube from here rests on one claim: an
account row can say which service it belongs to, and every loop that talks to
Meta reads only the Instagram ones. If that claim is wrong the symptom is not a
test failure, it is `graph.instagram.com` being handed a YouTube channel id and
an empty token against a live account.

So most of this file is about what the Meta loops do *not* see. The other half
is the mirror of it: the scheduler and the admin panel must see everything, or
a destination silently stops posting and nobody is watching the page that would
have said so.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db, insights, poller, scheduler
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, API_TOKEN, CHANNEL, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
async def graph(meta, cfg):
    async with meta.client() as client:
        yield GraphClient(client, cfg)


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    """One account of each kind, which is the shape the cluster will run."""
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    await db.upsert_account(
        connection,
        ig_user_id=CHANNEL,
        access_token="",
        username="@reelsmith",
        platform=db.PLATFORM_YOUTUBE,
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


# --- What the Meta loops must not see ---------------------------------------


async def _publish_to_both(conn) -> None:
    """The state a published Short leaves behind, alongside a published Reel.

    Registering the post is what arms the comment poller and what makes a media
    visible to the insights sweep, and the publish path registers every post it
    puts out. So the moment a Short goes out, a YouTube video id is sitting in
    the same table the Meta loops read. Both tests below need that row to mean
    anything at all: without it the loops find nothing for the channel and
    would pass with no filter in place.
    """
    await db.register_post(
        conn, media_id="media-1", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    await db.register_post(
        conn, media_id="yt-video-1", ig_user_id=CHANNEL, keyword="send", link=LINK
    )


async def test_the_comment_sweep_skips_a_youtube_account(conn, graph, cfg, metrics, meta):
    """A YouTube video id sent to Meta's comments endpoint reads as a deleted
    post: one warning a minute, forever, about a media that was never theirs."""
    await _publish_to_both(conn)

    await poller.poll_once(conn, graph, cfg, metrics)

    assert any("media-1" in call for call in meta.calls), "the Reel is still swept"
    assert not any("yt-video-1" in call for call in meta.calls)


async def test_the_token_refresher_skips_a_youtube_account(conn, graph, cfg, metrics, meta):
    """The sharpest edge of the platform filter.

    A YouTube row has a null `token_expires_at`, which this loop reads as an
    unknown expiry and therefore as due for refresh, and an empty
    `access_token`. Without the filter it would post that empty token to Meta's
    refresh endpoint on every pass, forever.
    """
    refreshed = await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    assert refreshed == 1, "the Instagram account still refreshes"
    assert sum("refresh_access_token" in call for call in meta.calls) == 1


async def test_the_insights_sweep_skips_a_youtube_account(conn, graph, cfg, metrics, meta):
    """Meta has no numbers for a YouTube video, so every sweep would spend a
    Graph call proving it and store nothing."""
    await _publish_to_both(conn)
    meta.insights = {"media-1": {"views": 100, "reach": 90}}

    await insights.refresh_once(conn, graph, cfg, metrics)

    assert any("media-1" in call for call in meta.calls), "the Reel is still read"
    assert not any("yt-video-1" in call for call in meta.calls)


# --- What the queue-shaped paths must see -----------------------------------


async def test_the_scheduler_looks_at_every_platform(conn, graph, cfg, metrics):
    """The one loop that is about the queue rather than about Meta.

    Asserted through `active_slots` rather than a publish, because a YouTube
    publish does not exist yet. What matters here is that the tick reaches the
    channel's slots at all.
    """
    seen = [
        account["ig_user_id"]
        for account in await db.active_accounts(conn, platform=None)
    ]

    assert seen == [ACCOUNT, CHANNEL]
    # And the loop that consumes it is wired to the same call.
    await scheduler.tick_once(conn, graph, cfg, metrics)


async def test_the_admin_panel_lists_every_platform(client):
    http, app = client
    await db.upsert_account(app.state.db, ig_user_id=ACCOUNT, access_token="tok")
    await db.upsert_account(
        app.state.db, ig_user_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
    )

    accounts = await db.all_accounts(app.state.db, platform=None)

    assert {a["ig_user_id"] for a in accounts} == {ACCOUNT, CHANNEL}


async def test_the_default_is_instagram_so_a_missed_call_site_is_inert(conn):
    """Forgetting the argument must cost a no-op rather than a Graph error.

    This is the reason the default is not "everything". A loop that talks to
    Meta and was never updated should ignore a new destination; the opposite
    default would have it send a channel id to graph.instagram.com.
    """
    assert [a["ig_user_id"] for a in await db.all_accounts(conn)] == [ACCOUNT]
    assert [a["ig_user_id"] for a in await db.active_accounts(conn)] == [ACCOUNT]


# --- Registration -----------------------------------------------------------


def _registration(**overrides) -> dict:
    return {
        "channel_id": CHANNEL,
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "username": "@reelsmith",
        **overrides,
    }


async def test_registering_a_channel_stores_the_account_and_the_credentials(client):
    http, app = client

    response = await http.post("/api/accounts/youtube", json=_registration(), headers=AUTH)

    assert response.status_code == 200
    account = await db.get_account(app.state.db, CHANNEL)
    assert account["platform"] == db.PLATFORM_YOUTUBE
    assert account["access_token"] == ""
    assert account["username"] == "@reelsmith"
    credentials = await db.youtube_credentials(app.state.db, CHANNEL)
    assert credentials["refresh_token"] == "refresh"


async def test_registering_a_channel_subscribes_to_nothing(client, meta):
    """`/api/accounts` subscribes the account to messages. There is no such
    thing here, and a stray Graph call would be made with a channel id."""
    http, _ = client

    await http.post("/api/accounts/youtube", json=_registration(), headers=AUTH)

    assert meta.calls == []


async def test_a_handle_is_refused_where_a_channel_id_belongs(client):
    """The likely paste. Accepting it registers cleanly and fails weeks later
    at the first publish, nowhere near the cause."""
    http, _ = client

    response = await http.post(
        "/api/accounts/youtube", json=_registration(channel_id="@reelsmith"), headers=AUTH
    )

    assert response.status_code == 422


async def test_registering_a_channel_needs_the_bearer_token(client):
    http, _ = client
    assert (await http.post("/api/accounts/youtube", json=_registration())).status_code == 401


async def test_re_authorising_a_channel_replaces_the_refresh_token(client):
    http, app = client
    await http.post("/api/accounts/youtube", json=_registration(), headers=AUTH)

    await http.post(
        "/api/accounts/youtube", json=_registration(refresh_token="fresher"), headers=AUTH
    )

    credentials = await db.youtube_credentials(app.state.db, CHANNEL)
    assert credentials["refresh_token"] == "fresher"


async def test_re_authorising_a_channel_does_not_un_pause_it(client):
    """Same rule the Instagram path has. Someone who paused a destination did
    it on purpose, and re-running OAuth is not a request to undo that."""
    http, app = client
    await http.post("/api/accounts/youtube", json=_registration(), headers=AUTH)
    await db.set_account_flags(app.state.db, CHANNEL, active=False)

    await http.post("/api/accounts/youtube", json=_registration(), headers=AUTH)

    assert (await db.get_account(app.state.db, CHANNEL))["active"] == 0


async def test_the_platform_of_an_existing_row_is_not_overwritten(conn):
    """A row changing destination would point a queue full of posts somewhere
    new, which is a mistake rather than a re-authorisation."""
    await db.upsert_account(conn, ig_user_id=CHANNEL, access_token="tok")

    assert (await db.get_account(conn, CHANNEL))["platform"] == db.PLATFORM_YOUTUBE


# --- Config slots -----------------------------------------------------------


async def test_config_slots_still_apply_once_a_channel_is_registered(cfg, meta):
    """The regression this filter exists to prevent.

    `GATEWAY_SLOTS` with no `GATEWAY_SLOTS_ACCOUNT` resolves the account by
    there being exactly one. Counting every platform would take that to two the
    day a channel is registered, and a schedule that had worked for months
    would stop applying with only a warning in the log.
    """
    cfg = settings(cfg.db_path.parent, slots="18:00 Europe/Oslo jitter=15")
    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, ig_user_id=ACCOUNT, access_token="tok")
        await db.upsert_account(
            conn, ig_user_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
        )
    finally:
        await conn.close()

    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with app.router.lifespan_context(app):
            slots = await db.all_slots(app.state.db, ACCOUNT)

    assert len(slots) == 1
    assert slots[0]["hour"] == 18
