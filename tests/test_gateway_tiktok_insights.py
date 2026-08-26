"""TikTok's four numbers, stored and shown, and kept out of the loop.

F5. `/api/results` structurally could not return a non Instagram post, because
it omits any row with no `skip_rate` and nothing else can produce one. That was
the right call for its own purpose and it is why this splits in two:

- **Storage.** The numbers are worth having. `views`, `likes`, `comments` and
  `shares` fit the columns that exist; `reach`, `saved`, `avg_watch_ms`,
  `total_watch_ms` and `skip_rate` stay 0 and mean "not measured on this
  platform", which the `platform` column is what distinguishes.
- **The loop.** `_results_block` keeps reading Instagram alone. TikTok exposes
  no retention, watch time or completion metric of any kind, so there is no
  three second equivalent to substitute and feeding it anything else would
  corrupt the one measurement everything else is argued from.

One shape TikTok adds that Meta never needed: the `publish_id` a post is
published under is not a video id, so the sweep has to list and match first.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db, insights
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, API_TOKEN, OPEN_ID, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"
TITLE = "Ponytail makes your coding agent stop and ask"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path, tiktok_enabled=True)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, account_id=ACCOUNT, access_token="tok")
    await db.upsert_account(
        connection, account_id=OPEN_ID, access_token="", platform=db.PLATFORM_TIKTOK
    )
    await db.upsert_tiktok_credentials(
        connection, open_id=OPEN_ID, client_key="k", client_secret="s",
        refresh_token="rft.original", refresh_expires_in=31_536_000,
    )
    yield connection
    await connection.close()


async def publish(conn, *, title: str = TITLE, publish_id: str = "tt-publish-1") -> int:
    """A TikTok row as the scheduler leaves it: media id is the publish id."""
    queued_id = await db.enqueue_post(
        conn, account_id=OPEN_ID, video_name="a.mp4", cover_name=None,
        caption="c", keyword="X", link=LINK, repo_full_name="a/b",
        approved=True, title=title,
    )
    await db.set_container(conn, queued_id, publish_id)
    await db.mark_queue_published(conn, queued_id, media_id=publish_id, permalink="")
    return queued_id


def videos(meta, *rows):
    meta.tiktok.videos = list(rows)


# --- The list and match step -------------------------------------------------


async def test_a_publish_id_is_resolved_to_a_video_id(conn, meta, cfg, metrics):
    """The step Meta never needed. `status/fetch` reports the post finished and
    hands back neither an id nor a URL, so the row carries its `publish_id`
    until this works out which video it became."""
    queued_id = await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "share_url": "https://tiktok/v-777",
                  "view_count": 900, "like_count": 40, "comment_count": 3, "share_count": 7})

    async with meta.client() as http:
        stored = await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )

    assert stored == 1
    row = await db.get_queued(conn, queued_id)
    assert row["media_id"] == "v-777"
    assert row["permalink"] == "https://tiktok/v-777"
    # The publish id is not lost, and it was written before the publish was
    # even attempted.
    assert row["container_id"] == "tt-publish-1"


async def test_matching_is_on_the_title_this_service_wrote(conn, meta, cfg, metrics):
    """A string this service sent and TikTok echoed back identifies a post
    exactly. A timestamp has to be compared with a tolerance, and two posts an
    hour apart on a busy day would be a coin toss."""
    await publish(conn, title="A hook about Ponytail")
    videos(
        meta,
        {"id": "v-1", "title": "Something else entirely", "view_count": 10},
        {"id": "v-2", "title": "A hook about Ponytail", "view_count": 900},
    )

    async with meta.client() as http:
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )

    readings = await db.latest_insights(conn, OPEN_ID)
    assert list(readings) == ["v-2"]
    assert readings["v-2"]["views"] == 900


async def test_a_post_not_on_the_first_page_yet_is_not_an_error(conn, meta, cfg, metrics):
    """Either it has fallen off, in which case its id was resolved on an
    earlier sweep, or it is not there yet. Neither is worth a log line every
    six hours and neither should stop the sweep."""
    await publish(conn)
    videos(meta)

    async with meta.client() as http:
        assert await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        ) == 0


async def test_an_already_resolved_row_keeps_being_read(conn, meta, cfg, metrics):
    """The numbers climb for days, so resolving once and never reading again
    would store one reading and call it the answer."""
    queued_id = await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "view_count": 100})

    async with meta.client() as http:
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )
        videos(meta, {"id": "v-777", "title": TITLE, "view_count": 1614})
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID), on="2026-08-27",
        )

    assert (await db.get_queued(conn, queued_id))["media_id"] == "v-777"
    assert (await db.latest_insights(conn, OPEN_ID))["v-777"]["views"] == 1614


# --- What the numbers mean ----------------------------------------------------


async def test_the_unmeasured_columns_are_marked_by_platform_not_by_zero(
    conn, meta, cfg, metrics
):
    """A zero and an absence are different claims. The column is what tells
    them apart, and it is why storing these was worth a migration."""
    await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "view_count": 900,
                  "like_count": 40, "comment_count": 3, "share_count": 7})

    async with meta.client() as http:
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )

    reading = (await db.latest_insights(conn, OPEN_ID))["v-777"]
    assert reading["platform"] == db.PLATFORM_TIKTOK
    assert (reading["views"], reading["likes"], reading["shares"]) == (900, 40, 7)
    assert (reading["reach"], reading["saved"], reading["skip_rate"]) == (0, 0, 0.0)


async def test_an_instagram_reading_is_still_marked_instagram(conn):
    """Defaulted rather than backfilled, because every row that already existed
    could only have come from one place."""
    await db.record_insights(
        conn, media_id="m1", account_id=ACCOUNT, metrics={"views": 5, "skip_rate": 70.0}
    )

    assert (await db.latest_insights(conn, ACCOUNT))["m1"]["platform"] == "instagram"


async def test_the_sweep_rotates_the_refresh_token_before_it_reads(conn, meta, cfg, metrics):
    """The same rule as the publisher. The token just spent is dead, and a
    sweep that throws must not take the new one with it."""
    await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "view_count": 1})

    async with meta.client() as http:
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )

    assert (await db.tiktok_credentials(conn, OPEN_ID))["refresh_token"] == "rft.rotated"


# --- And what stays out of the loop -------------------------------------------


async def test_the_results_api_cannot_return_a_tiktok_post(conn, meta, cfg):
    """The rule the whole feedback loop rests on. `skip_rate` is the one number
    it turns on, and there is no TikTok equivalent to substitute."""
    await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "view_count": 900})

    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            await insights.refresh_tiktok_account(
                app.state.db, GraphClient(fake_meta, cfg), cfg, Metrics(),
                await db.get_account(app.state.db, OPEN_ID),
            )
            body = (await http.get("/api/results", headers=AUTH)).json()

    assert body["results"] == []


async def test_a_tiktok_reading_cannot_be_read_as_instagrams(conn, meta, cfg, metrics):
    """Scoped by platform rather than by "nothing else fills that column",
    which is a rule that holds by accident until it does not."""
    await publish(conn)
    videos(meta, {"id": "v-777", "title": TITLE, "view_count": 900})

    async with meta.client() as http:
        await insights.refresh_tiktok_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, OPEN_ID),
        )

    assert await db.latest_insights(conn, platform=db.PLATFORM_INSTAGRAM) == {}
    assert list(await db.latest_insights(conn, platform=db.PLATFORM_TIKTOK)) == ["v-777"]


def test_a_platform_reports_only_the_numbers_it_has():
    """A TikTok row rendered with Instagram's column set is a post that got
    zero reach and zero saves, which is a claim rather than an absence."""
    from gateway import analysis

    assert "reach" not in analysis.measured_columns("tiktok")
    assert "saved" not in analysis.measured_columns("tiktok")
    assert "reach" in analysis.measured_columns("instagram")
    # A platform nobody has taught it about renders what has always been
    # rendered, rather than an empty board.
    assert analysis.measured_columns("myspace") == analysis.measured_columns("instagram")
