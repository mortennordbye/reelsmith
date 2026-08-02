"""Reading how the published Reels are doing, and reporting it per post.

Two things matter here. That a sweep survives the normal case of a Reel Meta
has no numbers for yet, because that is every post for its first hours and a
sweep that dies on the newest one never reaches the rest. And that the per-post
funnel attributes to the right video, since the whole point of the page is
deciding what to make more of.
"""

from __future__ import annotations

import pytest

from gateway import db, insights
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, FakeMeta, settings

OTHER_ACCOUNT = "17841400000000001"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    yield connection
    await connection.close()


async def publish_one(conn, *, media_id: str, repo: str = "astral-sh/uv", account=ACCOUNT) -> int:
    queued_id = await db.enqueue_post(
        conn,
        ig_user_id=account,
        video_name=f"{media_id}.mp4",
        cover_name=None,
        caption="hi",
        keyword="UV",
        link="https://github.com/astral-sh/uv",
        repo_full_name=repo,
        approved=True,
    )
    await db.mark_queue_published(
        conn, queued_id, media_id=media_id, permalink=f"https://ig/{media_id}"
    )
    return queued_id


def sweep(meta: FakeMeta, cfg):
    return GraphClient(meta.client(), cfg), Metrics()


# --- The sweep --------------------------------------------------------------


async def test_a_reading_is_stored_for_a_published_reel(conn, cfg):
    await publish_one(conn, media_id="media-1")
    meta = FakeMeta(insights={"media-1": {"views": 1500, "reach": 1173, "likes": 23,
                                          "comments": 0, "saved": 20, "shares": 9}})
    graph, metrics = sweep(meta, cfg)

    stored = await insights.refresh_once(conn, graph, cfg, metrics)

    assert stored == 1
    latest = await db.latest_insights(conn, ACCOUNT)
    assert latest["media-1"]["views"] == 1500
    assert latest["media-1"]["saved"] == 20


async def test_a_reel_with_no_numbers_yet_is_skipped_not_fatal(conn, cfg):
    """Every post looks like this for its first hours.

    A sweep that raised here would never reach the older posts behind it, which
    are the ones that actually have numbers.
    """
    await publish_one(conn, media_id="too-young")
    await publish_one(conn, media_id="has-numbers", repo="other/repo")
    meta = FakeMeta(insights={"has-numbers": {"views": 10, "reach": 8, "likes": 1,
                                              "comments": 0, "saved": 0, "shares": 0}})
    graph, metrics = sweep(meta, cfg)

    stored = await insights.refresh_once(conn, graph, cfg, metrics)

    assert stored == 1, "the young post is skipped and the older one still read"
    assert "too-young" not in await db.latest_insights(conn, ACCOUNT)


async def test_a_second_sweep_the_same_day_updates_rather_than_duplicates(conn, cfg):
    """A Reel climbs all day. Two readings on one date would be a lie about which."""
    await publish_one(conn, media_id="media-1")
    meta = FakeMeta(insights={"media-1": {"views": 100, "reach": 90, "likes": 1,
                                          "comments": 0, "saved": 0, "shares": 0}})
    graph, metrics = sweep(meta, cfg)
    await insights.refresh_once(conn, graph, cfg, metrics)

    meta.insights["media-1"]["views"] = 250
    # Nothing is stale now, so ask for the same date explicitly the way a
    # manual refresh would.
    account = await db.get_account(conn, ACCOUNT)
    await db.record_insights(
        conn, media_id="media-1", ig_user_id=ACCOUNT,
        metrics={"views": 250, "reach": 90, "likes": 1, "comments": 0, "saved": 0, "shares": 0},
    )
    assert account is not None

    rows = await db._all(conn, "SELECT * FROM insights WHERE media_id = 'media-1'")
    assert len(rows) == 1, "one row per media per day"
    assert rows[0]["views"] == 250


async def test_history_is_kept_across_days(conn, cfg):
    """Without this, "did the evening slot beat the morning one" is unanswerable."""
    await publish_one(conn, media_id="media-1")
    for day, views in (("2026-08-01", 100), ("2026-08-02", 400)):
        await db.record_insights(
            conn, media_id="media-1", ig_user_id=ACCOUNT, on=day,
            metrics={"views": views, "reach": views, "likes": 0, "comments": 0,
                     "saved": 0, "shares": 0},
        )

    rows = await db._all(conn, "SELECT * FROM insights ORDER BY fetched_on")
    assert [r["views"] for r in rows] == [100, 400]
    # Latest is the honest answer to "how is it doing".
    assert (await db.latest_insights(conn, ACCOUNT))["media-1"]["views"] == 400


async def test_an_expired_token_stops_the_sweep_rather_than_hammering_meta(conn, cfg):
    for i in range(3):
        await publish_one(conn, media_id=f"media-{i}", repo=f"o/r{i}")
    meta = FakeMeta(insights_error={"message": "Session expired", "code": 190})
    graph, metrics = sweep(meta, cfg)

    stored = await insights.refresh_once(conn, graph, cfg, metrics)

    assert stored == 0
    calls = [c for c in meta.calls if "insights" in c]
    assert len(calls) == 1, "an auth failure applies to every media, so ask once"


async def test_posts_older_than_the_window_are_left_alone(conn, cfg):
    """A Reel stops moving long before it stops existing."""
    await publish_one(conn, media_id="ancient")
    await conn.execute(
        "UPDATE queued_posts SET published_at = ? WHERE media_id = 'ancient'",
        ("2020-01-01T00:00:00+00:00",),
    )
    await conn.commit()
    meta = FakeMeta(insights={"ancient": {"views": 5, "reach": 5, "likes": 0,
                                          "comments": 0, "saved": 0, "shares": 0}})
    graph, metrics = sweep(meta, cfg)

    assert await insights.refresh_once(conn, graph, cfg, metrics) == 0


# --- Retention --------------------------------------------------------------
#
# Views cannot tell a viewer who left after half a second from one who watched
# to the end, which is how seven posts averaging under six seconds of watch
# time looked fine on the page for a week.


REELS = {
    "views": 1500, "reach": 1173, "likes": 23, "comments": 0, "saved": 20,
    "shares": 9, "ig_reels_avg_watch_time": 8370,
    "ig_reels_video_view_total_time": 10_622_235, "reels_skip_rate": 64.2,
}


async def test_the_retention_metrics_are_stored(conn, cfg):
    await publish_one(conn, media_id="media-1")
    graph, metrics = sweep(FakeMeta(insights={"media-1": REELS}), cfg)

    await insights.refresh_once(conn, graph, cfg, metrics)

    row = (await db.latest_insights(conn, ACCOUNT))["media-1"]
    assert row["avg_watch_ms"] == 8370
    assert row["total_watch_ms"] == 10_622_235
    assert row["skip_rate"] == pytest.approx(64.2)


async def test_the_skip_rate_keeps_its_decimal(conn, cfg):
    """Rounding 64.2 to 64 loses the digit a hook change first shows up in."""
    await publish_one(conn, media_id="media-1")
    graph, metrics = sweep(FakeMeta(insights={"media-1": {**REELS, "reels_skip_rate": 71.4}}), cfg)

    await insights.refresh_once(conn, graph, cfg, metrics)

    assert (await db.latest_insights(conn, ACCOUNT))["media-1"]["skip_rate"] == pytest.approx(71.4)


async def test_a_media_that_is_not_a_reel_still_gets_the_other_numbers(conn, cfg):
    """Meta fails the whole request rather than dropping one metric from it.

    So asking for the Reels-only metrics on an image post would lose views and
    reach as well, on a post that was never going to have retention.
    """
    await publish_one(conn, media_id="photo-1")
    meta = FakeMeta(
        insights={"photo-1": {"views": 40, "reach": 38, "likes": 2, "comments": 0,
                              "saved": 1, "shares": 0}},
        not_a_reel={"photo-1"},
    )
    graph, metrics = sweep(meta, cfg)

    assert await insights.refresh_once(conn, graph, cfg, metrics) == 1
    row = (await db.latest_insights(conn, ACCOUNT))["photo-1"]
    assert row["views"] == 40
    assert row["skip_rate"] == 0


async def test_a_row_written_before_retention_existed_is_read_again(conn, cfg):
    """What upgrading actually looked like.

    Every post already had a row for the day, written by the previous image
    without the retention columns, so the first sweep on the new one skipped
    all of them and the feedback loop stayed empty until the next day. Any
    metric added later would land the same way.
    """
    await publish_one(conn, media_id="media-1")
    await db.record_insights(
        conn, media_id="media-1", ig_user_id=ACCOUNT,
        metrics={"views": 1500, "reach": 1173, "likes": 23, "comments": 0,
                 "saved": 20, "shares": 9},  # the old image's shape
    )
    graph, metrics = sweep(FakeMeta(insights={"media-1": REELS}), cfg)

    assert await insights.refresh_once(conn, graph, cfg, metrics) == 1
    assert (await db.latest_insights(conn, ACCOUNT))["media-1"]["skip_rate"] == pytest.approx(64.2)


async def test_a_row_that_already_has_retention_is_left_alone(conn, cfg):
    """The other half: this must not re-read every post on every sweep."""
    await publish_one(conn, media_id="media-1")
    graph, metrics = sweep(FakeMeta(insights={"media-1": REELS}), cfg)
    assert await insights.refresh_once(conn, graph, cfg, metrics) == 1

    assert await insights.refresh_once(conn, graph, cfg, metrics) == 0


async def test_a_reel_meta_has_no_retention_for_yet_reads_zero_not_missing(conn, cfg):
    await publish_one(conn, media_id="media-1")
    core = {k: v for k, v in REELS.items() if not k.startswith(("ig_reels", "reels_"))}
    graph, metrics = sweep(FakeMeta(insights={"media-1": core}), cfg)

    await insights.refresh_once(conn, graph, cfg, metrics)

    row = (await db.latest_insights(conn, ACCOUNT))["media-1"]
    assert row["views"] == 1500
    assert row["avg_watch_ms"] == 0


# --- Per-post attribution ---------------------------------------------------


async def test_the_funnel_is_attributed_to_the_reel_that_produced_it(conn, cfg):
    """The account-wide total cannot say which video converted, which is the
    question that decides what to make more of."""
    await publish_one(conn, media_id="good")
    await publish_one(conn, media_id="quiet", repo="other/repo")

    for i in range(3):
        await db.claim_comment(
            conn, comment_id=f"c{i}", media_id="good", ig_user_id=ACCOUNT,
            author_id=f"a{i}",
        )
        await db.mark_comment_replied(conn, f"c{i}", igsid=f"a{i}")
        await db.record_delivery(conn, igsid=f"a{i}", ig_user_id=ACCOUNT, media_id="good")
    await db.claim_comment(
        conn, comment_id="c9", media_id="quiet", ig_user_id=ACCOUNT, author_id="a9"
    )

    per_post = await db.per_post_funnel(conn, ACCOUNT)

    assert per_post["good"] == {"comments_seen": 3, "private_replies": 3, "links_sent": 3}
    assert per_post["quiet"] == {"comments_seen": 1, "private_replies": 0, "links_sent": 0}


async def test_one_account_never_sees_another_accounts_numbers(conn, cfg):
    """The panel is about to have more than one account on it."""
    await db.upsert_account(conn, ig_user_id=OTHER_ACCOUNT, access_token="tok2")
    await publish_one(conn, media_id="mine")
    await publish_one(conn, media_id="theirs", repo="them/repo", account=OTHER_ACCOUNT)
    for media_id, account in (("mine", ACCOUNT), ("theirs", OTHER_ACCOUNT)):
        await db.record_insights(
            conn, media_id=media_id, ig_user_id=account,
            metrics={"views": 1, "reach": 1, "likes": 0, "comments": 0, "saved": 0, "shares": 0},
        )

    assert set(await db.latest_insights(conn, ACCOUNT)) == {"mine"}
    assert set(await db.latest_insights(conn, OTHER_ACCOUNT)) == {"theirs"}
    assert [r["media_id"] for r in await db.published_media(conn, ACCOUNT)] == ["mine"]


async def test_the_account_funnel_counts_a_repeat_converter_twice(conn, cfg):
    """Someone who asks on two Reels converted twice, not once.

    `conversations` holds one row per person and its link_sent_at is
    overwritten, so counting it there reported the most engaged part of the
    audience as a single conversion. The per-post page and the account total
    have to agree, or one of them is lying.
    """
    await publish_one(conn, media_id="post-a")
    await publish_one(conn, media_id="post-b", repo="other/repo")
    await db.start_conversation(conn, igsid="fan", ig_user_id=ACCOUNT, media_id="post-a")
    for media_id in ("post-a", "post-b"):
        await db.claim_comment(
            conn, comment_id=f"c-{media_id}", media_id=media_id,
            ig_user_id=ACCOUNT, author_id="fan",
        )
        await db.record_delivery(conn, igsid="fan", ig_user_id=ACCOUNT, media_id=media_id)
        await db.update_conversation(
            conn, igsid="fan", ig_user_id=ACCOUNT, link_sent=True
        )

    account_wide = await db.funnel(conn, ACCOUNT)
    per_post = await db.per_post_funnel(conn, ACCOUNT)

    assert account_wide["links_sent"] == 2
    assert sum(p["links_sent"] for p in per_post.values()) == account_wide["links_sent"]


# --- Both publish paths -----------------------------------------------------


async def test_a_reel_published_by_hand_is_not_invisible(conn, cfg):
    """`--publish` from the Mac writes `posts` and never touches the queue.

    Reading only the queue hid three of the first five Reels ever made,
    including the best performing one, on the page built to compare them.
    """
    await publish_one(conn, media_id="from-queue")
    await db.register_post(
        conn, media_id="by-hand", ig_user_id=ACCOUNT,
        keyword="SKILLS", link="https://github.com/mattpocock/skills",
    )

    listed = await db.published_media(conn, ACCOUNT)

    assert {r["media_id"] for r in listed} == {"from-queue", "by-hand"}
    by_hand = next(r for r in listed if r["media_id"] == "by-hand")
    # No queue row means no stored repo name, so it comes from the link.
    assert by_hand["repo_full_name"] == "mattpocock/skills"
    assert by_hand["source"] == "direct"


async def test_a_hand_published_reel_is_swept_for_insights_too(conn, cfg):
    await db.register_post(
        conn, media_id="by-hand", ig_user_id=ACCOUNT,
        keyword="SKILLS", link="https://github.com/mattpocock/skills",
    )
    meta = FakeMeta(insights={"by-hand": {"views": 1500, "reach": 1173, "likes": 23,
                                          "comments": 0, "saved": 20, "shares": 9}})
    graph, metrics = sweep(meta, cfg)

    assert await insights.refresh_once(conn, graph, cfg, metrics) == 1
    assert (await db.latest_insights(conn, ACCOUNT))["by-hand"]["views"] == 1500


async def test_a_reel_in_both_tables_is_listed_once(conn, cfg):
    """The publish path registers the post as well as marking the queue row."""
    await publish_one(conn, media_id="media-1")
    await db.register_post(
        conn, media_id="media-1", ig_user_id=ACCOUNT,
        keyword="UV", link="https://github.com/astral-sh/uv",
    )

    listed = await db.published_media(conn, ACCOUNT)

    assert [r["media_id"] for r in listed] == ["media-1"]
    assert listed[0]["repo_full_name"] == "astral-sh/uv", "the queue row wins"
