"""YouTube's numbers, stored and shown, and kept out of the loop.

The third platform to reach the `insights` table and the first with any
retention at all, which is exactly what makes it the dangerous one. TikTok
could never contaminate the feedback loop because it reports nothing about
watching; YouTube reports `averageViewPercentage`, which looks close enough to
`skip_rate` to be substituted by somebody in a hurry and is a different
measurement. That one scores the whole video, `skip_rate` scores the first
three seconds, and the loop is a claim about openings.

So the split is the same one TikTok drew and the assertions are about both
halves:

- **Storage.** Four counts, `averageViewDuration` into `avg_watch_ms`,
  `estimatedMinutesWatched` into `total_watch_ms` and `averageViewPercentage`
  into a column of its own. `reach`, `saved` and `skip_rate` stay 0 and mean
  "not measured on this platform".
- **The loop.** `/api/results` keeps reading Instagram alone.

The shape TikTok needed is absent here: a YouTube upload hands back its video
id at publish, so there is nothing to resolve and nothing to match on a title.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import analysis, db, insights
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, API_TOKEN, CHANNEL, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/DietrichGebert/ponytail"
TITLE = "Ponytail makes your coding agent stop and ask"

# One video's report, in the metric names the Analytics API uses.
REPORT = {
    "views": 1614,
    "likes": 58,
    "comments": 4,
    "shares": 9,
    "estimatedMinutesWatched": 210,
    "averageViewDuration": 8,
    "averageViewPercentage": 31.25,
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
        connection, account_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
    )
    await db.upsert_youtube_credentials(
        connection,
        channel_id=CHANNEL,
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
    )
    yield connection
    await connection.close()


async def publish(conn, *, video_id: str = "yt-video-1", title: str = TITLE) -> int:
    """A YouTube row as the scheduler leaves it: the real video id, and a URL."""
    queued_id = await db.enqueue_post(
        conn, account_id=CHANNEL, video_name="a.mp4", cover_name=None,
        caption="c", keyword="X", link=LINK, repo_full_name="a/b",
        approved=True, title=title,
    )
    await db.mark_queue_published(
        conn,
        queued_id,
        media_id=video_id,
        permalink=f"https://www.youtube.com/watch?v={video_id}",
    )
    return queued_id


async def sweep(conn, meta, cfg, metrics) -> int:
    async with meta.client() as http:
        return await insights.refresh_youtube_account(
            conn, GraphClient(http, cfg), cfg, metrics,
            await db.get_account(conn, CHANNEL),
        )


# --- Reading them -------------------------------------------------------------


async def test_a_published_short_gets_a_reading(conn, meta, cfg, metrics):
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    assert await sweep(conn, meta, cfg, metrics) == 1

    reading = (await db.latest_insights(conn, CHANNEL))["yt-video-1"]
    assert (reading["views"], reading["likes"], reading["comments"]) == (1614, 58, 4)
    assert reading["shares"] == 9


async def test_the_id_is_the_one_the_upload_returned(conn, meta, cfg, metrics):
    """The whole reason this sweep is one call where TikTok's is two. A publish
    id that is not a video id is a shape Google never had."""
    await publish(conn, video_id="yt-video-2")
    meta.youtube.stats = {"yt-video-2": REPORT}

    await sweep(conn, meta, cfg, metrics)

    assert meta.youtube.reports[0]["filters"] == "video==yt-video-2"
    assert list(await db.latest_insights(conn, CHANNEL)) == ["yt-video-2"]


async def test_the_whole_batch_is_one_request(conn, meta, cfg, metrics):
    """The report is dimensioned by video and the quota is per request, so a
    month of posting is one call rather than thirty."""
    for index in range(3):
        await publish(conn, video_id=f"yt-{index}", title=f"{TITLE} {index}")
    meta.youtube.stats = {f"yt-{index}": REPORT for index in range(3)}

    assert await sweep(conn, meta, cfg, metrics) == 3

    assert len(meta.youtube.reports) == 1


async def test_the_range_covers_the_oldest_post_in_the_batch(conn, meta, cfg, metrics):
    """A later start date would report each video's numbers since then, which
    is not what a running total means."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)

    asked = meta.youtube.reports[0]
    assert asked["startDate"] < asked["endDate"]
    assert asked["ids"] == "channel==MINE"


async def test_a_short_with_no_data_yet_is_not_an_error(conn, meta, cfg, metrics):
    """YouTube omits a video it has nothing for, which is what a Short
    published an hour ago looks like. Nothing is stored, rather than zeroes."""
    await publish(conn)
    meta.youtube.stats = {}

    assert await sweep(conn, meta, cfg, metrics) == 0
    assert await db.latest_insights(conn, CHANNEL) == {}


async def test_a_reading_is_rewritten_rather_than_doubled(conn, meta, cfg, metrics):
    """One row per media per day. A manual refresh an hour after the sweep
    updates today's row instead of inventing a second reading for it."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)
    meta.youtube.stats = {"yt-video-1": {**REPORT, "views": 1700}}
    await sweep(conn, meta, cfg, metrics)

    assert (await db.reading_counts(conn, CHANNEL))["yt-video-1"] == 1
    assert (await db.latest_insights(conn, CHANNEL))["yt-video-1"]["views"] == 1700


# --- What the numbers mean ----------------------------------------------------


async def test_watch_time_arrives_in_the_units_the_table_keeps(conn, meta, cfg, metrics):
    """YouTube reports seconds and minutes; every column here is
    milliseconds."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)

    reading = (await db.latest_insights(conn, CHANNEL))["yt-video-1"]
    assert reading["avg_watch_ms"] == 8_000
    assert reading["total_watch_ms"] == 210 * 60_000


async def test_the_unmeasured_columns_are_marked_by_platform_not_by_zero(
    conn, meta, cfg, metrics
):
    """A zero and an absence are different claims, and `skip_rate` is the one
    that matters: YouTube reports nothing about the first three seconds."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)

    reading = (await db.latest_insights(conn, CHANNEL))["yt-video-1"]
    assert reading["platform"] == db.PLATFORM_YOUTUBE
    assert (reading["reach"], reading["saved"], reading["skip_rate"]) == (0, 0, 0.0)


async def test_the_share_of_the_video_watched_is_not_a_skip_rate(
    conn, meta, cfg, metrics
):
    """The substitution this whole file exists to prevent. It scores the whole
    video where `skip_rate` scores the opening, so it gets a column of its own
    and that one stays 0."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)

    reading = (await db.latest_insights(conn, CHANNEL))["yt-video-1"]
    assert reading["avg_view_pct"] == pytest.approx(31.2, abs=0.1)
    assert reading["skip_rate"] == 0.0


async def test_an_instagram_reading_has_no_view_percentage(conn):
    """Meta reports no such figure, so the new column is an absence there in
    exactly the way `skip_rate` is one on the other two platforms."""
    await db.record_insights(
        conn, media_id="m1", account_id=ACCOUNT, metrics={"views": 5, "skip_rate": 70.0}
    )

    assert (await db.latest_insights(conn, ACCOUNT))["m1"]["avg_view_pct"] == 0.0


def test_a_platform_reports_only_the_numbers_it_has():
    """YouTube's report has the same four counts as TikTok's and neither reach
    nor saves, so a board rendered with Instagram's set claims two zeroes."""
    assert "reach" not in analysis.measured_columns("youtube")
    assert "saved" not in analysis.measured_columns("youtube")
    assert "shares" in analysis.measured_columns("youtube")


# --- When it cannot read ------------------------------------------------------


async def test_a_channel_with_no_credentials_is_skipped(conn, meta, cfg, metrics):
    """Registered as an account but never authorised. One warning, no calls."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}
    await conn.execute("DELETE FROM youtube_credentials")
    await conn.commit()

    assert await sweep(conn, meta, cfg, metrics) == 0
    assert meta.youtube.reports == []


async def test_a_dead_refresh_token_costs_one_line_not_thirty(conn, meta, cfg, metrics):
    """`invalid_grant` hits every video in the batch the same way, so the sweep
    stops at the token rather than proving it once per post."""
    for index in range(3):
        await publish(conn, video_id=f"yt-{index}", title=f"{TITLE} {index}")
    meta.youtube.token_status = 400

    assert await sweep(conn, meta, cfg, metrics) == 0
    assert metrics.graph_errors._value.get() == 1


async def test_a_refused_report_is_counted_and_survived(conn, meta, cfg, metrics):
    await publish(conn)
    meta.youtube.analytics_status = 403

    assert await sweep(conn, meta, cfg, metrics) == 0
    assert await db.latest_insights(conn, CHANNEL) == {}
    assert metrics.graph_errors._value.get() == 1


async def test_a_channel_with_nothing_published_asks_google_nothing(
    conn, meta, cfg, metrics
):
    assert await sweep(conn, meta, cfg, metrics) == 0
    assert meta.youtube.reports == []


async def test_the_whole_sweep_reads_youtube_without_being_asked(
    conn, meta, cfg, metrics
):
    """On by default, like the sweep it runs inside. It only reads, and a
    deployment with no YouTube account calls Google nothing."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    async with meta.client() as http:
        await insights.refresh_once(conn, GraphClient(http, cfg), cfg, metrics)

    assert list(await db.latest_insights(conn, CHANNEL)) == ["yt-video-1"]


async def test_the_flag_is_what_stops_the_sweep(conn, meta, cfg, metrics):
    """Off is the switch for a channel whose token has died, and off means the
    whole platform is skipped rather than each read failing."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}
    quiet = settings(cfg.db_path.parent, youtube_insights_enabled=False)

    async with meta.client() as http:
        await insights.refresh_once(conn, GraphClient(http, quiet), quiet, metrics)

    assert meta.youtube.reports == []


# --- And what stays out of the loop -------------------------------------------


async def test_the_results_api_cannot_return_a_youtube_post(conn, meta, cfg):
    """The rule the whole feedback loop rests on. `averageViewPercentage` is
    the closest any platform gets to `skip_rate` and it is still a different
    question."""
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            await insights.refresh_youtube_account(
                app.state.db, GraphClient(fake_meta, cfg), cfg, Metrics(),
                await db.get_account(app.state.db, CHANNEL),
            )
            body = (await http.get("/api/results", headers=AUTH)).json()

    assert body["results"] == []


async def test_a_youtube_reading_cannot_be_read_as_instagrams(conn, meta, cfg, metrics):
    await publish(conn)
    meta.youtube.stats = {"yt-video-1": REPORT}

    await sweep(conn, meta, cfg, metrics)

    assert await db.latest_insights(conn, platform=db.PLATFORM_INSTAGRAM) == {}
    assert list(await db.latest_insights(conn, platform=db.PLATFORM_YOUTUBE)) == [
        "yt-video-1"
    ]


# --- And what the page says ---------------------------------------------------


async def test_the_posts_page_shows_what_youtube_reports(tmp_path, meta, metrics):
    """The column set is the platform's, and the retention tiles are the ones
    the reading earned. A YouTube board rendered with Instagram's set is a post
    that got zero reach and zero saves, which is a claim rather than an
    absence."""
    cfg = settings(tmp_path, admin_enabled=True, admin_trust_proxy_auth=True)
    meta.youtube.stats = {"yt-video-1": REPORT}

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
                conn, account_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE
            )
            await db.upsert_youtube_credentials(
                conn, channel_id=CHANNEL, client_id="client",
                client_secret="secret", refresh_token="refresh",
            )
            await publish(conn)
            await insights.refresh_youtube_account(
                conn, GraphClient(fake_meta, cfg), cfg, metrics,
                await db.get_account(conn, CHANNEL),
            )
            page = (await http.get("/admin/posts")).text

    assert "avg viewed" in page
    assert "avg watch" in page
    # The three that platform does not report, in the two places they would
    # otherwise be rendered as results.
    assert "saves" not in page
    assert "reach" not in page
    assert "skipped" not in page
