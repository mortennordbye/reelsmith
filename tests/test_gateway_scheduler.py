"""The queue, the scheduler, and the two ways this can go wrong.

The failure modes worth testing are "posted nothing" and "posted twice", and
only one of them can be undone quietly. Most of what follows is about the
second.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from gateway import db, schedule, scheduler
from gateway.config import GatewaySettings
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, FakeMeta, settings


class PublishingMeta(FakeMeta):
    """FakeMeta plus the three publish calls.

    `container_status` and the two failure switches are what let a test choose
    exactly where in the sequence things break, which is the only thing that
    decides whether a retry is safe.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.container_status = "FINISHED"
        self.fail_container = False
        self.fail_publish = False
        self.published: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")

        if path.endswith("/media") and request.method == "POST":
            if self.fail_container:
                return httpx.Response(400, json={"error": {"message": "no", "code": 1}})
            return httpx.Response(200, json={"id": "container-1"})
        if path.endswith("/media_publish"):
            if self.fail_publish:
                return httpx.Response(400, json={"error": {"message": "nope", "code": 1}})
            self.published.append("container-1")
            return httpx.Response(200, json={"id": "media-999"})
        if path.endswith("/container-1"):
            return httpx.Response(200, json={"status_code": self.container_status})
        if path.endswith("/media-999"):
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/A/"})
        return super()._handle(request)


@pytest.fixture
def cfg(tmp_path) -> GatewaySettings:
    return settings(
        tmp_path,
        scheduler_enabled=True,
        publish_poll_interval_s=0.001,
        publish_timeout_s=2,
        public_base_url="https://gate.example.test",
    )


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    yield connection
    await connection.close()


def graph_for(meta: PublishingMeta, cfg: GatewaySettings) -> GraphClient:
    return GraphClient(meta.client(), cfg)


async def queue_one(conn, *, approved: bool = True, name: str = "a.mp4") -> int:
    return await db.enqueue_post(
        conn,
        ig_user_id=ACCOUNT,
        video_name=name,
        cover_name=None,
        caption="hi",
        keyword="UV",
        link="https://github.com/astral-sh/uv",
        repo_full_name="astral-sh/uv",
        approved=approved,
    )


async def due_slot(conn, *, jitter: int = 0) -> schedule.Slot:
    """A slot whose time is right now."""
    local = db.now()
    await db.add_slot(
        conn, ig_user_id=ACCOUNT, hour=local.hour, minute=local.minute,
        tz="UTC", jitter_minutes=jitter,
    )
    return schedule.Slot.from_row((await db.all_slots(conn, ACCOUNT))[0])


# --- The happy path -------------------------------------------------------


async def test_a_due_slot_publishes_and_registers_the_post(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn)
    await due_slot(conn)

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 1

    row = (await db.queued_posts(conn))[0]
    assert row["state"] == db.QUEUE_PUBLISHED
    assert row["media_id"] == "media-999"
    assert row["permalink"] == "https://instagram.com/reel/A/"
    # The keyword mechanic has to be armed by the same act that published it,
    # because the Mac is not here to do it afterwards.
    post = await db.get_post(conn, "media-999")
    assert post["keyword"] == "UV"


async def test_meta_is_told_to_fetch_the_video_from_this_service(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, name="reel-abc.mp4")
    await due_slot(conn)
    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    assert scheduler.media_url(cfg, "reel-abc.mp4") == (
        "https://gate.example.test/media/reel-abc.mp4"
    )


async def test_a_draft_is_never_published(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, approved=False)
    await due_slot(conn)

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 0
    assert (await db.queued_posts(conn))[0]["state"] == db.QUEUE_DRAFT
    assert meta.published == []


async def test_a_slot_that_is_not_due_publishes_nothing(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn)
    # Two hours from now, well outside the grace window.
    later = db.now() + timedelta(hours=2)
    await db.add_slot(conn, ig_user_id=ACCOUNT, hour=later.hour, minute=later.minute, tz="UTC")

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 0


# --- Not posting twice ----------------------------------------------------


async def test_a_second_tick_does_not_publish_again(conn, cfg):
    """The single most expensive mistake this code could make."""
    meta = PublishingMeta()
    await queue_one(conn)
    await due_slot(conn)

    first = await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    second = await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    assert (first, second) == (1, 0)
    assert len(meta.published) == 1


async def test_the_slot_fire_is_claimed_per_local_date(conn, cfg):
    slot = await due_slot(conn)
    day = db.now().date().isoformat()
    assert await db.claim_slot_fire(conn, slot_id=slot.id, local_date=day) is True
    assert await db.claim_slot_fire(conn, slot_id=slot.id, local_date=day) is False
    # Tomorrow is a different claim.
    assert await db.claim_slot_fire(conn, slot_id=slot.id, local_date="2099-01-01") is True


async def test_claiming_a_post_is_compare_and_swap(conn):
    queued_id = await queue_one(conn)
    assert await db.claim_queued(conn, queued_id) is True
    # A second tick racing for the same row loses rather than double publishing.
    assert await db.claim_queued(conn, queued_id) is False


async def test_two_slots_due_together_take_different_posts(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, name="first.mp4")
    await queue_one(conn, name="second.mp4")
    local = db.now()
    await db.add_slot(conn, ig_user_id=ACCOUNT, hour=local.hour, minute=local.minute, tz="UTC")
    await db.add_slot(conn, ig_user_id=ACCOUNT, hour=local.hour, minute=local.minute, tz="UTC")

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 2
    names = {r["video_name"] for r in await db.queued_posts(conn)}
    assert names == {"first.mp4", "second.mp4"}


# --- Failure, and whether a retry is safe ---------------------------------


async def test_a_failure_before_the_container_is_retried(conn, cfg):
    """A dropped connection on container creation must not cost the day.

    Nothing was created, so the slot gets its turn back and the post goes to
    the back of `approved` rather than to `failed`.
    """
    meta = PublishingMeta()
    meta.fail_container = True
    queued_id = await queue_one(conn)
    slot = await due_slot(conn)
    metrics = Metrics()

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics) == 0
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_APPROVED
    assert "Container creation failed" in row["failure"]
    # The fire was released, so the next tick tries again.
    day = db.now().date().isoformat()
    assert await db.claim_slot_fire(conn, slot_id=slot.id, local_date=day) is True


async def test_the_retry_actually_succeeds_on_the_next_tick(conn, cfg):
    meta = PublishingMeta()
    meta.fail_container = True
    await queue_one(conn)
    await due_slot(conn)
    metrics = Metrics()

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics) == 0
    meta.fail_container = False
    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics) == 1
    assert (await db.queued_posts(conn))[0]["state"] == db.QUEUE_PUBLISHED


async def test_a_failure_after_the_container_stops_and_waits(conn, cfg):
    """Meta may have accepted the post, and no error text proves otherwise.

    So the row stops in `failed` and the slot stays spent. One missing post is
    recoverable; an unknown number of duplicates is not.
    """
    meta = PublishingMeta()
    meta.fail_publish = True
    queued_id = await queue_one(conn)
    slot = await due_slot(conn)

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 0
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_FAILED
    assert row["container_id"] == "container-1"
    day = db.now().date().isoformat()
    assert await db.claim_slot_fire(conn, slot_id=slot.id, local_date=day) is False


async def test_a_container_that_errors_is_not_retried_either(conn, cfg):
    meta = PublishingMeta()
    meta.container_status = "ERROR"
    queued_id = await queue_one(conn)
    await due_slot(conn)

    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_FAILED


async def test_retries_are_bounded(conn, cfg):
    """A structural failure should stop asking rather than retry forever."""
    meta = PublishingMeta()
    meta.fail_container = True
    queued_id = await queue_one(conn)
    metrics = Metrics()

    for _ in range(cfg.max_publish_attempts + 2):
        await db.set_slot_active(conn, 1, False)
        await conn.execute("DELETE FROM schedule_slots")
        await conn.execute("DELETE FROM slot_fires")
        await conn.commit()
        await due_slot(conn)
        await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics)

    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_FAILED
    assert row["attempts"] >= cfg.max_publish_attempts


async def test_an_empty_queue_is_counted_not_crashed(conn, cfg):
    """A slot firing into nothing is the shape of "the account went quiet"."""
    meta = PublishingMeta()
    await due_slot(conn)
    metrics = Metrics()

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics) == 0
    assert metrics.slots_starved._value.get() == 1


async def test_a_paused_account_publishes_nothing(conn, cfg):
    """The kill switch has to stop the scheduler too, not just the DMs."""
    meta = PublishingMeta()
    await queue_one(conn)
    await due_slot(conn)
    await db.set_account_flags(conn, ACCOUNT, active=False)

    assert await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics()) == 0
    assert meta.published == []


# --- Ordering and pinning -------------------------------------------------


async def test_the_head_of_the_queue_goes_first(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, name="first.mp4")
    await queue_one(conn, name="second.mp4")
    await due_slot(conn)

    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    published = [r for r in await db.queued_posts(conn) if r["state"] == db.QUEUE_PUBLISHED]
    assert published[0]["video_name"] == "first.mp4"


async def test_a_pinned_post_jumps_the_line_once_its_time_has_come(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, name="ordinary.mp4")
    pinned = await db.enqueue_post(
        conn, ig_user_id=ACCOUNT, video_name="pinned.mp4", cover_name=None,
        caption="", keyword="X", link="https://example.com/x",
        approved=True, slot_override=db.now() - timedelta(minutes=5),
    )
    await due_slot(conn)

    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    assert (await db.get_queued(conn, pinned))["state"] == db.QUEUE_PUBLISHED


async def test_a_pin_in_the_future_does_not_jump(conn, cfg):
    meta = PublishingMeta()
    ordinary = await queue_one(conn, name="ordinary.mp4")
    await db.enqueue_post(
        conn, ig_user_id=ACCOUNT, video_name="pinned.mp4", cover_name=None,
        caption="", keyword="X", link="https://example.com/x",
        approved=True, slot_override=db.now() + timedelta(days=3),
    )
    await due_slot(conn)

    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())
    assert (await db.get_queued(conn, ordinary))["state"] == db.QUEUE_PUBLISHED


# --- Retention ------------------------------------------------------------


async def test_queued_media_is_exempt_from_the_age_sweep(conn):
    """The bug that would otherwise eat the back of a ten post queue.

    Files are pruned by mtime, and a post scheduled eight days out is older
    than the TTL by the time its turn arrives.
    """
    await queue_one(conn, name="scheduled.mp4")
    await db.enqueue_post(
        conn, ig_user_id=ACCOUNT, video_name="cancelled.mp4", cover_name="cover.png",
        caption="", keyword="X", link="https://example.com/x",
    )
    names = await db.live_media_names(conn)
    assert "scheduled.mp4" in names
    # A draft still counts: it is in the line, just not armed.
    assert "cancelled.mp4" in names and "cover.png" in names


async def test_published_media_is_no_longer_exempt(conn, cfg):
    meta = PublishingMeta()
    await queue_one(conn, name="done.mp4")
    await due_slot(conn)
    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, Metrics())

    # Meta has fetched it by now, so it is free to age out.
    assert "done.mp4" not in await db.live_media_names(conn)


# --- What the admin UI shows ----------------------------------------------


async def test_upcoming_pairs_each_armed_post_with_a_real_time(conn, cfg):
    await queue_one(conn, name="one.mp4")
    await queue_one(conn, name="two.mp4")
    await db.add_slot(conn, ig_user_id=ACCOUNT, hour=18, minute=0, tz="UTC")

    rows = await scheduler.upcoming(conn, cfg, ACCOUNT, moment=db.now())
    times = [when for _, when in rows]
    assert all(t is not None for t in times)
    assert times[0] < times[1]


async def test_a_draft_does_not_consume_a_slot_in_the_projection(conn, cfg):
    """Showing a draft a publish time would be a lie the moment someone
    forgot to approve it."""
    await queue_one(conn, name="draft.mp4", approved=False)
    await queue_one(conn, name="armed.mp4")
    await db.add_slot(conn, ig_user_id=ACCOUNT, hour=18, minute=0, tz="UTC")

    rows = {r["video_name"]: when for r, when in await scheduler.upcoming(
        conn, cfg, ACCOUNT, moment=db.now()
    )}
    assert rows["draft.mp4"] is None
    assert rows["armed.mp4"] is not None


# --- What Prometheus can see ----------------------------------------------
#
# These exist because the alerting lives in homelab as PrometheusRules rather
# than in this process. A rule can only fire on a metric that moves, so the
# metric moving is this repo's half of the contract.


async def test_a_missing_video_file_is_counted_as_a_failure(conn, cfg):
    """It is the one failure that never asks Meta for anything, and it was also
    the one Prometheus could not see. ReelsmithPublishFailing alerts on this
    counter, so a post dying for want of a file has to reach it."""
    meta = PublishingMeta()
    queued_id = await queue_one(conn, name="")
    await due_slot(conn)
    metrics = Metrics()

    await scheduler.tick_once(conn, graph_for(meta, cfg), cfg, metrics)

    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_FAILED
    assert metrics.registry.get_sample_value("reelsmith_publish_failures_total") == 1
