"""Every number a platform will give, stored and shown, and the audience too.

The nine `insights` columns were chosen one metric at a time, and every
platform reports more than they hold. What this file pins is the shape that
lets the rest arrive without a migration each:

- **Anything else lands in `extra`**, in the platform's own names, and a metric
  a platform refuses costs that metric and never the core reading beside it.
  That is the promise the skip rate the feedback loop reads depends on.
- **A refusal is remembered and a refusal of everything is not**, so a retired
  name is asked for once a process and a bad minute switches nothing off.
- **The audience is a series of its own**, one follower count per destination
  per day, taken whether or not anything has been published.
- **The page shows a column only once a post has a value for it**, so a metric
  nobody sends is an absent column rather than a row of zeroes.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from gateway import analysis, db, insights, probe
from gateway.app import create_app
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, CHANNEL, PAGE_ID, FakeMeta, settings

REEL = {
    "views": 1500, "reach": 1173, "likes": 23, "comments": 0, "saved": 20, "shares": 9,
    "ig_reels_avg_watch_time": 8370, "ig_reels_video_view_total_time": 10_622_235,
    "reels_skip_rate": 64.2,
}

SHORT = {
    "views": 1614, "likes": 58, "comments": 4, "shares": 9,
    "estimatedMinutesWatched": 210, "averageViewDuration": 8, "averageViewPercentage": 31.25,
    "engagedViews": 1200, "subscribersGained": 7, "subscribersLost": 1,
}

# The hundred points the retention report returns, falling steadily.
CURVE = [[i / 100, round(1.2 - i / 100, 3), 0.4] for i in range(1, 101)]


@pytest.fixture(autouse=True)
def _nothing_refused_carries_over():
    """What a platform refused is remembered per process, and tests share one."""
    probe.forget_all()
    yield
    probe.forget_all()


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, account_id=ACCOUNT, access_token="tok")
    yield connection
    await connection.close()


async def publish(
    conn, *, account_id: str = ACCOUNT, media_id: str = "media-1", title: str = ""
) -> None:
    queued_id = await db.enqueue_post(
        conn, account_id=account_id, video_name=f"{media_id}.mp4", cover_name=None,
        caption="c", keyword="X", link="https://github.com/astral-sh/uv",
        repo_full_name=f"astral-sh/{media_id}", approved=True, hook="A hook",
        title=title or media_id,
    )
    await db.mark_queue_published(conn, queued_id, media_id=media_id, permalink=f"https://x/{media_id}")


async def youtube_channel(conn):
    await db.upsert_account(conn, account_id=CHANNEL, access_token="", platform=db.PLATFORM_YOUTUBE)
    await db.upsert_youtube_credentials(
        conn, channel_id=CHANNEL, client_id="c", client_secret="s", refresh_token="r"
    )
    return await db.get_account(conn, CHANNEL)


async def facebook_page(conn):
    await db.upsert_account(
        conn, account_id=PAGE_ID, access_token="page-token", platform=db.PLATFORM_FACEBOOK
    )
    return await db.get_account(conn, PAGE_ID)


# --- The probe ------------------------------------------------------------------


async def test_a_refused_metric_is_dropped_named_and_not_asked_for_again(caplog):
    refusals = probe.Refusals("Somewhere")
    asked: list[list[str]] = []

    async def fetch(names):
        asked.append(list(names))
        return "no" if "gone" in names else "ok"

    with caplog.at_level("WARNING"):
        assert await refusals.read(["a", "gone", "b"], fetch, lambda o: o == "no") == "ok"
    assert asked[-1] == ["a", "b"]
    assert "gone" in caplog.text

    asked.clear()
    await refusals.read(["a", "gone", "b"], fetch, lambda o: o == "no")
    assert asked == [["a", "b"]]


async def test_a_refusal_of_everything_is_not_remembered():
    """An endpoint having a bad minute refuses each metric alone as well, and
    remembering that would switch the read off until the next rollout."""
    refusals = probe.Refusals("Somewhere")

    async def fetch(names):
        return "no"

    assert await refusals.read(["a", "b"], fetch, lambda o: o == "no") == "no"
    assert refusals.names == set()


# --- Instagram ------------------------------------------------------------------


async def ig_sweep(conn, meta, cfg, metrics) -> int:
    async with meta.client() as http:
        return await insights.refresh_account(
            conn, GraphClient(http, cfg), cfg, metrics, await db.get_account(conn, ACCOUNT)
        )


async def test_everything_else_a_reel_reports_is_stored(conn, cfg, metrics):
    await publish(conn)
    meta = FakeMeta(insights={"media-1": {
        **REEL, "reposts": 4, "facebook_views": 310, "total_interactions": 61,
    }})

    assert await ig_sweep(conn, meta, cfg, metrics) == 1

    row = (await db.latest_insights(conn, ACCOUNT))["media-1"]
    assert db.extra_of(row) == {"reposts": 4, "facebook_views": 310, "total_interactions": 61}
    assert row["skip_rate"] == pytest.approx(64.2)


async def test_a_metric_instagram_refuses_costs_that_metric_and_nothing_else(
    conn, cfg, metrics, caplog
):
    """Meta fails the whole call and says only "An unknown error has occurred",
    so without the probe one retired name would cost every extra, and folded
    into the core request it would cost the skip rate."""
    await publish(conn, media_id="media-1")
    await publish(conn, media_id="media-2")
    reel = {**REEL, "reposts": 4}
    meta = FakeMeta(
        insights={"media-1": reel, "media-2": reel}, refused_metrics={"crossposted_views"}
    )

    with caplog.at_level("WARNING"):
        assert await ig_sweep(conn, meta, cfg, metrics) == 2

    latest = await db.latest_insights(conn, ACCOUNT)
    for media_id in ("media-1", "media-2"):
        assert latest[media_id]["skip_rate"] == pytest.approx(64.2)
        assert db.extra_of(latest[media_id])["reposts"] == 4
    assert "crossposted_views" in caplog.text
    # Once in the full request and once alone, for the whole sweep.
    naming = [
        r for r in meta.requests
        if "crossposted_views" in str(r.url.params.get("metric") or "")
    ]
    assert len(naming) == 2


async def test_a_row_read_before_extras_existed_is_read_again_once(conn, cfg, metrics):
    """What the first sweep after this deploy meets: every post already has
    today's row, written without extras."""
    await publish(conn)
    await db.record_insights(
        conn, media_id="media-1", account_id=ACCOUNT, metrics={"views": 1500, "skip_rate": 64.2}
    )
    meta = FakeMeta(insights={"media-1": {**REEL, "reposts": 4}})

    assert await ig_sweep(conn, meta, cfg, metrics) == 1
    assert await ig_sweep(conn, meta, cfg, metrics) == 0

    assert db.extra_of((await db.latest_insights(conn, ACCOUNT))["media-1"])["reposts"] == 4


async def test_the_account_is_read_whether_or_not_anything_was_published(conn, cfg, metrics):
    meta = FakeMeta(
        account_fields={"followers_count": 812, "follows_count": 3, "media_count": 97},
        account_insights={"reach": 2400, "views": 5100, "accounts_engaged": 41},
    )

    assert await ig_sweep(conn, meta, cfg, metrics) == 0

    [row] = await db.account_insights_series(conn, ACCOUNT)
    assert (row["followers"], row["platform"]) == (812, db.PLATFORM_INSTAGRAM)
    extra = db.extra_of(row)
    assert extra["media_count"] == 97
    assert extra["day"]["reach"] == 2400
    # Yesterday's totals, since today's are still moving.
    assert extra["day"]["on"] < row["fetched_on"]


async def test_a_follower_count_nobody_gave_is_null_rather_than_zero(conn, cfg, metrics):
    meta = FakeMeta(account_insights={"reach": 10})

    await ig_sweep(conn, meta, cfg, metrics)

    [row] = await db.account_insights_series(conn, ACCOUNT)
    assert row["followers"] is None
    assert db.extra_of(row)["day"]["reach"] == 10


async def test_an_expired_token_takes_no_audience_reading(conn, cfg, metrics):
    await publish(conn)
    meta = FakeMeta(
        insights_error={"message": "Session expired", "code": 190},
        account_fields={"followers_count": 812},
    )

    await ig_sweep(conn, meta, cfg, metrics)

    assert await db.account_insights_series(conn, ACCOUNT) == []


# --- YouTube --------------------------------------------------------------------


async def yt_sweep(conn, meta, cfg, metrics, account) -> int:
    async with meta.client() as http:
        return await insights.refresh_youtube_account(
            conn, GraphClient(http, cfg), cfg, metrics, account
        )


async def test_engaged_views_subscribers_and_the_curve_are_stored(conn, cfg, metrics):
    account = await youtube_channel(conn)
    await publish(conn, account_id=CHANNEL, media_id="yt-1")
    meta = FakeMeta()
    meta.youtube.stats = {"yt-1": SHORT}
    meta.youtube.retention = {"yt-1": CURVE}

    assert await yt_sweep(conn, meta, cfg, metrics, account) == 1

    reading = (await db.latest_insights(conn, CHANNEL))["yt-1"]
    extra = db.extra_of(reading)
    assert extra["engagedViews"] == 1200
    assert (extra["subscribersGained"], extra["subscribersLost"]) == (7, 1)
    # One point in five, and the half way point is the one the page reads.
    assert len(extra["retention"]) == 20
    assert extra["retention"][9] == [0.5, 0.7, 0.4]
    assert reading["avg_view_pct"] == pytest.approx(31.2, abs=0.1)


async def test_a_metric_the_analytics_api_does_not_know_costs_only_itself(conn, cfg, metrics):
    account = await youtube_channel(conn)
    await publish(conn, account_id=CHANNEL, media_id="yt-1")
    meta = FakeMeta()
    meta.youtube.stats = {"yt-1": SHORT}
    meta.youtube.rejected_metrics = {"engagedViews"}

    assert await yt_sweep(conn, meta, cfg, metrics, account) == 1

    reading = (await db.latest_insights(conn, CHANNEL))["yt-1"]
    assert reading["views"] == 1614
    extra = db.extra_of(reading)
    assert "engagedViews" not in extra
    assert extra["subscribersGained"] == 7


async def test_the_subscriber_count_is_read_before_anything_is_published(conn, cfg, metrics):
    account = await youtube_channel(conn)
    meta = FakeMeta()
    meta.youtube.channel = {
        "subscriberCount": "132", "viewCount": "48210", "videoCount": "26",
        "hiddenSubscriberCount": False,
    }

    assert await yt_sweep(conn, meta, cfg, metrics, account) == 0

    assert meta.youtube.reports == []
    [row] = await db.account_insights_series(conn, CHANNEL)
    assert row["followers"] == 132
    assert db.extra_of(row) == {"viewCount": 48210, "videoCount": 26}


async def test_a_refused_curve_is_not_asked_for_again_that_sweep(conn, cfg, metrics):
    """A report the channel cannot have fails the same way for every video."""
    account = await youtube_channel(conn)
    await publish(conn, account_id=CHANNEL, media_id="yt-1")
    await publish(conn, account_id=CHANNEL, media_id="yt-2")
    meta = FakeMeta()
    meta.youtube.stats = {"yt-1": SHORT, "yt-2": SHORT}
    meta.youtube.retention_status = 400

    assert await yt_sweep(conn, meta, cfg, metrics, account) == 2

    curves = [r for r in meta.youtube.reports if r.get("dimensions") == "elapsedVideoTimeRatio"]
    assert len(curves) == 1
    assert metrics.graph_errors._value.get() == 1


# --- Facebook -------------------------------------------------------------------


async def fb_sweep(conn, meta, cfg, metrics, account) -> int:
    async with meta.client() as http:
        return await insights.refresh_facebook_account(
            conn, GraphClient(http, cfg), cfg, metrics, account
        )


async def test_a_reels_replays_follows_and_retention_are_stored(conn, cfg, metrics):
    account = await facebook_page(conn)
    await publish(conn, account_id=PAGE_ID, media_id="fb-1")
    meta = FakeMeta()
    meta.facebook.insights = {"fb-1": {
        "blue_reels_play_count": 900,
        "post_video_avg_time_watched": 7000,
        "fb_reels_replay_count": 140,
        "post_video_followers": 3,
        "post_video_retention_graph": {"0": 1.0, "1": 0.6, "2": 0.4},
        "post_video_likes_by_reaction_type": {"like": 5, "love": 2},
    }}

    assert await fb_sweep(conn, meta, cfg, metrics, account) == 1

    reading = (await db.latest_insights(conn, PAGE_ID))["fb-1"]
    assert (reading["views"], reading["likes"]) == (900, 7)
    extra = db.extra_of(reading)
    assert (extra["fb_reels_replay_count"], extra["post_video_followers"]) == (140, 3)
    assert extra["post_video_retention_graph"] == {"0": 1.0, "1": 0.6, "2": 0.4}
    # The sum is the column and the breakdown is the information.
    assert extra["post_video_likes_by_reaction_type"] == {"like": 5, "love": 2}
    assert "blue_reels_play_count" not in extra


async def test_a_page_missing_read_insights_stops_once_and_still_counts_followers(
    conn, cfg, metrics
):
    """Measured on production 2026-09-11: sixteen identical warnings a sweep,
    one per Reel. The Page node reads on a scope every Page has granted."""
    account = await facebook_page(conn)
    await publish(conn, account_id=PAGE_ID, media_id="fb-1")
    await publish(conn, account_id=PAGE_ID, media_id="fb-2")
    missing = {"code": 200, "message": "(#200) read_insights permission missing"}
    meta = FakeMeta()
    meta.facebook.insights_error = missing
    meta.facebook.page_insights_error = missing
    meta.facebook.page_fields = {"followers_count": 41, "fan_count": 38}

    assert await fb_sweep(conn, meta, cfg, metrics, account) == 0

    assert len([p for p in meta.facebook.phases if p == "insights"]) == 1
    [row] = await db.account_insights_series(conn, PAGE_ID)
    assert row["followers"] == 41
    assert db.extra_of(row) == {"fan_count": 38}


async def test_a_pages_day_totals_are_stored(conn, cfg, metrics):
    account = await facebook_page(conn)
    meta = FakeMeta()
    meta.facebook.page_fields = {"followers_count": 41}
    meta.facebook.page_insights = {"page_daily_follows_unique": 2, "page_media_view": 880}

    await fb_sweep(conn, meta, cfg, metrics, account)

    [row] = await db.account_insights_series(conn, PAGE_ID)
    assert db.extra_of(row)["day"]["page_media_view"] == 880


# --- What the page makes of it --------------------------------------------------


def test_a_change_needs_a_reading_that_old():
    """A destination first read on Monday has no weekly change on Wednesday,
    which is different from a week in which nobody followed."""
    series = [
        {"fetched_on": "2026-09-01", "followers": 100},
        {"fetched_on": "2026-09-05", "followers": 110},
        {"fetched_on": "2026-09-11", "followers": 125},
        {"fetched_on": "2026-09-12", "followers": None},
    ]

    summary = analysis.audience(series)

    assert summary is not None
    assert (summary["followers"], summary["week"], summary["month"]) == (125, 25, None)
    assert summary["since"] == 25
    assert analysis.audience([{"fetched_on": "2026-09-01", "followers": None}]) is None


async def test_the_page_shows_the_audience_and_only_the_columns_a_post_has(tmp_path, metrics):
    cfg = settings(tmp_path, admin_enabled=True, admin_trust_proxy_auth=True)
    meta = FakeMeta(
        insights={"media-1": {**REEL, "reposts": 4}},
        account_fields={"followers_count": 812},
        account_insights={"reach": 2400},
    )

    async with meta.client() as fake:
        app = create_app(cfg, http=fake, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            conn = app.state.db
            await db.upsert_account(
                conn, account_id=ACCOUNT, access_token="tok", username="nightly"
            )
            await db.record_account_insights(
                conn, account_id=ACCOUNT, platform=db.PLATFORM_INSTAGRAM, followers=790,
                on=(db.now() - timedelta(days=40)).date().isoformat(),
            )
            await publish(conn)
            await insights.refresh_account(
                conn, GraphClient(fake, cfg), cfg, metrics, await db.get_account(conn, ACCOUNT)
            )
            body = " ".join((await http.get("/admin/b/nightly/performance")).text.split())

    assert "Audience" in body
    assert "812" in body
    assert "+22" in body
    assert "2,400 reached" in body
    assert "Reposts" in body
    # Nothing sent Facebook views, so there is no column claiming none.
    assert "On Facebook" not in body
