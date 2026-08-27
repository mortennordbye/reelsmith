"""Facebook's numbers, stored and shown, and kept out of the loop.

The fourth platform to reach the `insights` table and the one that looks most
like Instagram without being it, which is what makes it the dangerous one.
TikTok could never contaminate the feedback loop because it reports nothing
about watching. YouTube reports a percentage of the video watched. Facebook
reports **reach**, which no other platform here does, and an average time
watched that includes replays, so it looks like Instagram's board and scores
something else. `skip_rate` is the share who left inside three seconds, and
this platform cannot answer that at all.

So the assertions are about both halves:

- **Storage.** Plays into `views`, unique impressions into `reach`, the
  reaction breakdown summed into `likes`, and the two watch time metrics into
  their own columns. `saved`, `shares`, `skip_rate` and `avg_view_pct` stay 0
  and mean "not measured here".
- **The loop.** `/api/results` keeps reading Instagram alone.

**Shares are an absence, not a zero.** Meta reports comments and shares fused
into one number in `post_video_social_actions`, and splitting it by subtracting
a separately fetched comment count would be arithmetic on two definitions. The
comment count comes from the node's own edge, which is exact.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import analysis, db, insights
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, API_TOKEN, PAGE_ID, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"
HOOK = "Ponytail makes your coding agent stop and ask"

# One Reel's insights, in the metric names Meta uses on this edge.
READING = {
    "blue_reels_play_count": 1614,
    "post_impressions_unique": 1180,
    "post_video_avg_time_watched": 8_200,
    "post_video_view_time": 210_000,
    # A breakdown by reaction type rather than a count, which is the one entry
    # in this set that is not a number.
    "post_video_likes_by_reaction_type": {"like": 41, "love": 12, "wow": 5},
}


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


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
        connection,
        account_id=PAGE_ID,
        access_token="page-token",
        platform=db.PLATFORM_FACEBOOK,
    )
    yield connection
    await connection.close()


async def publish(conn, *, video_id: str = "fb-video-1", permalink: str = "") -> int:
    """A Facebook row as the scheduler leaves it: the real video id, and a URL."""
    queued_id = await db.enqueue_post(
        conn, account_id=PAGE_ID, video_name="a.mp4", cover_name=None,
        caption="c", keyword="X", link=LINK, repo_full_name="a/b",
        approved=True, hook=HOOK,
    )
    await db.mark_queue_published(
        conn,
        queued_id,
        media_id=video_id,
        permalink=permalink or f"https://www.facebook.com/x/videos/{video_id}/",
    )
    return queued_id


async def sweep(conn, meta, cfg, metrics) -> int:
    async with meta.client() as http:
        return await insights.refresh_facebook_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, PAGE_ID),
        )


# --- Reading them -------------------------------------------------------------


async def test_a_published_reel_gets_a_reading(conn, meta, cfg, metrics):
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}
    meta.facebook.comment_counts = {"fb-video-1": 4}

    assert await sweep(conn, meta, cfg, metrics) == 1

    reading = (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]
    assert reading["views"] == 1614
    assert reading["comments"] == 4


async def test_the_reaction_breakdown_is_summed_into_likes(conn, meta, cfg, metrics):
    """The one metric in the set that comes back as a dict rather than a count.
    Read as a number it is zero, which looks like a Reel nobody reacted to."""
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    assert (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]["likes"] == 58


async def test_reach_is_stored_because_this_platform_actually_reports_it(
    conn, meta, cfg, metrics
):
    """The column YouTube and TikTok leave at zero. Both Meta surfaces fill it,
    which is why the Facebook board carries it and the other two do not."""
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    assert (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]["reach"] == 1180


async def test_watch_time_lands_in_the_columns_that_already_exist(conn, meta, cfg, metrics):
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    reading = (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]
    assert reading["avg_watch_ms"] == 8_200
    assert reading["total_watch_ms"] == 210_000


async def test_the_id_is_the_one_the_publish_returned(conn, meta, cfg, metrics):
    """No resolution step, unlike TikTok. A publish id that is not a video id
    is a shape Meta never had, on either of its surfaces."""
    await publish(conn, video_id="fb-video-9")
    meta.facebook.insights = {"fb-video-9": READING}

    await sweep(conn, meta, cfg, metrics)

    assert list(await db.latest_insights(conn, PAGE_ID)) == ["fb-video-9"]


async def test_a_reel_with_no_numbers_yet_is_not_an_error(conn, meta, cfg, metrics):
    """Meta has nothing for a Reel published minutes ago, and a sweep that died
    on the newest post would never reach the older ones behind it."""
    await publish(conn)

    assert await sweep(conn, meta, cfg, metrics) == 1

    assert (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]["views"] == 0


async def test_a_response_with_no_insights_key_is_not_read_as_zeroes(
    conn, meta, cfg, metrics
):
    """An empty insights list is a Reel with nothing yet. A *missing* key is
    what a renamed metric looks like, and storing it as zeroes is a column that
    quietly stops meaning anything."""
    await publish(conn)
    meta.facebook.no_insights_key = {"fb-video-1"}

    assert await sweep(conn, meta, cfg, metrics) == 0
    assert await db.latest_insights(conn, PAGE_ID) == {}


async def test_a_bad_token_stops_the_sweep_rather_than_failing_every_reel(
    conn, meta, cfg, metrics
):
    """Code 190 is every expired, revoked and invalidated token, and it will
    fail the same way for every remaining Reel."""
    for index in range(3):
        await publish(conn, video_id=f"fb-{index}")
    meta.facebook.insights_error = {"message": "Session has expired", "code": 190}

    assert await sweep(conn, meta, cfg, metrics) == 0

    assert len([p for p in meta.facebook.phases if p == "insights"]) == 1


async def test_a_missing_permalink_is_filled_in_and_the_id_is_left_alone(
    conn, meta, cfg, metrics
):
    """A Reel still transcoding when the publisher gave up waiting has a row
    with no URL, and this is the first call afterwards that would know one."""
    queued_id = await publish(conn, permalink="")
    await db.resolve_media_id(conn, queued_id, media_id="fb-video-1", permalink="")
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    row = await db.get_queued(conn, queued_id)
    assert row["media_id"] == "fb-video-1"
    assert row["permalink"] == "https://www.facebook.com/thenightlybuild/videos/1234567890/"


# --- Keeping it out of the loop ----------------------------------------------


async def test_the_reading_is_stored_under_its_own_platform(conn, meta, cfg, metrics):
    """The column that tells an absence from a result. Without it a Facebook
    row is a post that got zero saves and zero shares."""
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    rows = await db.latest_insights(conn, PAGE_ID, platform=db.PLATFORM_FACEBOOK)
    assert list(rows) == ["fb-video-1"]
    assert await db.latest_insights(conn, PAGE_ID, platform=db.PLATFORM_INSTAGRAM) == {}


async def test_nothing_is_written_into_skip_rate(conn, meta, cfg, metrics):
    """`post_video_avg_time_watched` scores the whole Reel, replays included.
    Inverted into `skip_rate` it would put two different measurements in the
    one column the feedback loop turns on."""
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    await sweep(conn, meta, cfg, metrics)

    reading = (await db.latest_insights(conn, PAGE_ID))["fb-video-1"]
    assert reading["skip_rate"] == 0
    assert reading["avg_view_pct"] == 0


async def test_the_results_api_cannot_return_a_facebook_post(conn, meta, cfg):
    """The rule the whole feedback loop rests on. This is the platform that
    comes closest to Instagram's board and it still cannot answer the one
    question the loop asks."""
    await publish(conn)
    meta.facebook.insights = {"fb-video-1": READING}

    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            await insights.refresh_facebook_account(
                app.state.db, GraphClient(fake_meta, cfg), cfg, Metrics(),
                await db.get_account(app.state.db, PAGE_ID),
            )
            body = (await http.get("/api/results", headers=AUTH)).json()

    assert body["results"] == []


# --- What the board may show --------------------------------------------------


def test_the_board_shows_reach_and_leaves_shares_out():
    """Facebook is the only platform here besides Instagram that reports reach,
    and the only one that reports no share count at all."""
    columns = analysis.measured_columns("facebook")

    assert "reach" in columns
    assert "shares" not in columns
    assert "saved" not in columns
