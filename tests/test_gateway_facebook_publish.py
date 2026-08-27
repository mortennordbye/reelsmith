"""The scheduler putting a queued row on a Facebook Page.

The fourth destination and the one with the least new machinery, which is worth
asserting rather than assuming. **A Page access token is Meta's credential
shape**, so there is no credentials table, no token mint and no refresher: what
is on the account row is what publishes. Most of this file is about the two
things that are genuinely new.

**Three calls across two hosts.** `start` and `finish` are Graph; the upload in
between is `rupload.facebook.com` and takes the video as a `file_url` *header*,
so Meta fetches and this service sends no bytes.

**The retry line sits one step earlier than the API's own.** `start` publishes
nothing, so in principle a failed upload could be retried. It is terminal
anyway, because a retry restarts at `start` and cannot tell a `finish` that
never landed from one whose response was lost. The second of those posts the
Reel twice, which is the only failure here that cannot be undone quietly.
"""

from __future__ import annotations

import pytest

from gateway import db, facebook, scheduler
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import PAGE_ID, FakeMeta, settings

LINK = "https://github.com/DietrichGebert/ponytail"
CAPTION = "Ponytail makes your coding agent stop and ask.\n\nFollow for one a day.\n\n#devtools"


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
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(
        connection,
        account_id=PAGE_ID,
        access_token="page-token",
        username="The Nightly Build",
        platform=db.PLATFORM_FACEBOOK,
    )
    yield connection
    await connection.close()


async def _queue(conn, **overrides) -> int:
    fields = {
        "account_id": PAGE_ID,
        "video_name": "reel-abc123.mp4",
        "cover_name": None,
        "caption": CAPTION,
        "keyword": "send",
        "link": LINK,
        "approved": True,
        **overrides,
    }
    queued_id = await db.enqueue_post(conn, **fields)
    await db.claim_queued(conn, queued_id)
    return queued_id


async def _publish(conn, graph, cfg, metrics, queued_id):
    account = await db.get_account(conn, PAGE_ID)
    queued = await db.get_queued(conn, queued_id)
    return await scheduler.publish_queued(
        conn, graph, cfg, metrics, account=account, queued=queued
    )


# --- The happy path ---------------------------------------------------------


async def test_a_queued_reel_is_published_and_recorded(conn, graph, cfg, metrics, meta):
    queued_id = await _queue(conn)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (True, False)
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_PUBLISHED
    assert row["media_id"] == "fb-video-1"
    assert meta.facebook.phases == ["start", "upload", "finish", "status"]


async def test_meta_fetches_the_video_rather_than_being_handed_it(
    conn, graph, cfg, metrics, meta
):
    """The seam this service already provides for Instagram and TikTok. The URL
    is built at publish time rather than stored, so moving the service to
    another hostname does not strand the queue."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    assert meta.facebook.fetched == ["https://gate.example.test/media/reel-abc123.mp4"]


async def test_the_caption_is_carried_rather_than_rebuilt(conn, graph, cfg, metrics, meta):
    """Facebook gets the Instagram caption unchanged, ask included, because a
    Page has followers and the word is the same word. A gateway that rebuilt
    the copy would be a second place for it to drift."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    assert meta.facebook.finishes[0]["description"] == CAPTION
    assert meta.facebook.finishes[0]["video_state"] == "PUBLISHED"


async def test_the_permalink_is_absolute(conn, graph, cfg, metrics):
    """Meta returns `permalink_url` as a site-relative path on a video node.
    Stored raw, the panel renders it as an href that resolves against the
    gateway's own host, which is a link to a 404 that looks like a link to the
    post."""
    queued_id = await _queue(conn)

    await _publish(conn, graph, cfg, metrics, queued_id)

    row = await db.get_queued(conn, queued_id)
    assert row["permalink"] == "https://www.facebook.com/thenightlybuild/videos/1234567890/"


async def test_nothing_is_registered_for_the_comment_poller(conn, graph, cfg, metrics):
    """`register_post` arms the comment sweep, which works through
    graph.instagram.com on an Instagram media id. A Page video id in `posts` is
    exactly the row that loop must never pick up."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    assert await db.pollable_posts(conn, PAGE_ID, ttl_days=30) == []


async def test_the_publish_is_counted_under_its_own_platform(conn, graph, cfg, metrics):
    """Unlabelled, `ReelsmithPublishFailing` fires without saying where, and a
    destination that has stopped publishing looks like one that never did."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    counter = metrics.posts_published.labels(platform=db.PLATFORM_FACEBOOK)
    assert counter._value.get() == 1


async def test_the_token_never_reaches_a_url(conn, graph, cfg, metrics, meta):
    """The rule `test_gateway_graph.py` pins over the Instagram surface, held
    here too so there is one story rather than two. Meta documents most of
    these calls with `access_token` as a parameter and the header form works
    on both Graph hosts; a URL is the copy that reaches logs and referrers.

    The upload phase is the one call that is not `Bearer`. That endpoint takes
    `Authorization: OAuth <token>`, and written the ordinary way it fails as a
    401 that reads like a bad token.
    """
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    sent = [r for r in meta.requests if "facebook.com" in str(r.url)]
    assert len(sent) == 4
    for request in sent:
        assert "page-token" not in str(request.url), request.url.path
        assert request.headers["Authorization"] in (
            "Bearer page-token",
            "OAuth page-token",
        )
    assert sent[1].headers["Authorization"] == "OAuth page-token", "the upload phase"


# --- The ordering that makes a restart safe ---------------------------------


async def test_the_video_id_is_committed_before_the_video_is_fetched(
    conn, graph, cfg, metrics, meta
):
    """A process dying mid-fetch has to leave a row that can answer the only
    question worth asking afterwards, which is whether Meta was ever asked to
    make anything."""
    meta.facebook.upload_error = {"message": "gone", "code": 1}
    queued_id = await _queue(conn)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    row = await db.get_queued(conn, queued_id)
    assert row["container_id"] == "fb-video-1", "committed before the bytes were asked for"
    assert row["state"] == db.QUEUE_FAILED


async def test_a_refusal_at_start_hands_the_slot_back(conn, graph, cfg, metrics, meta):
    """Nothing exists before `start` returns, so this is the one failure worth
    retrying. The same line `publisher.PublishError` draws for Instagram and
    `youtube.UploadError` for Google."""
    meta.facebook.start_error = {"message": "temporarily unavailable", "code": 2}
    queued_id = await _queue(conn)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, True)
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_APPROVED, "back in the queue for the next tick"
    assert row["container_id"] is None


async def test_a_failure_at_finish_stops_rather_than_retrying(conn, graph, cfg, metrics, meta):
    """A retry restarts at `start` and would publish a second Reel if the
    finish had actually landed. No error text proves it did not."""
    meta.facebook.finish_error = {"message": "unknown error", "code": 1}
    queued_id = await _queue(conn)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_FAILED


async def test_an_account_with_no_token_fails_rather_than_retrying(
    conn, graph, cfg, metrics, meta
):
    """A registration that never finished cannot be fixed by trying again."""
    await db.upsert_account(
        conn, account_id=PAGE_ID, access_token="", platform=db.PLATFORM_FACEBOOK
    )
    queued_id = await _queue(conn)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    assert meta.facebook.phases == [], "nothing was sent"
    assert "no Facebook Page token" in (await db.get_queued(conn, queued_id))["failure"]


async def test_a_row_with_no_video_is_counted_like_every_other_failure(
    conn, graph, cfg, metrics
):
    """The one path that never asks Meta for anything, and for a while the only
    failure Prometheus could not see."""
    queued_id = await _queue(conn, video_name="")

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    counter = metrics.publish_failures.labels(platform=db.PLATFORM_FACEBOOK)
    assert counter._value.get() == 1


# --- Waiting for the Reel to actually exist ---------------------------------


async def test_success_at_finish_is_not_treated_as_published(meta):
    """`finish` returning `{"success": true}` means the request was accepted.
    Transcoding follows and can fail on its own, so a publisher that stopped
    there would be the only one here whose "published" meant something
    weaker."""
    meta.facebook.publish_states = ["draft", "draft", "published"]

    async with meta.client() as http:
        result = await facebook.await_published(
            http,
            video_id="fb-video-1",
            token="page-token",
            api_version="v23.0",
            poll_interval_s=0,
        )

    assert result.video_id == "fb-video-1"
    assert meta.facebook.polls == 3


async def test_a_publishing_error_stops_rather_than_polling_on(meta):
    meta.facebook.publish_states = ["error"]

    async with meta.client() as http:
        with pytest.raises(facebook.PublishError) as exc:
            await facebook.await_published(
                http,
                video_id="fb-video-1",
                token="page-token",
                api_version="v23.0",
                poll_interval_s=0,
            )

    assert exc.value.video_created is True


async def test_a_failed_upload_status_is_terminal(meta):
    meta.facebook.publish_states = ["draft"]
    meta.facebook.video_states = ["upload_failed"]

    async with meta.client() as http:
        with pytest.raises(facebook.PublishError, match="upload_failed") as exc:
            await facebook.await_published(
                http,
                video_id="fb-video-1",
                token="page-token",
                api_version="v23.0",
                poll_interval_s=0,
            )

    assert exc.value.video_created is True


async def test_a_timeout_does_not_hand_the_slot_back(meta):
    """The Reel may be seconds from going live, and starting again would
    publish it twice."""
    meta.facebook.publish_states = ["draft"]

    async with meta.client() as http:
        with pytest.raises(facebook.PublishError) as exc:
            await facebook.await_published(
                http,
                video_id="fb-video-1",
                token="page-token",
                api_version="v23.0",
                poll_interval_s=0,
                timeout_s=0,
            )

    assert exc.value.video_created is True


# --- The registration, which is one write ------------------------------------


async def test_the_page_row_carries_its_own_token(conn):
    """No `facebook_credentials` table anywhere. A Page access token is a token
    plus an expiry, which is what `accounts` has always held, and that is the
    whole reason this destination cost less than the other two."""
    account = await db.get_account(conn, PAGE_ID)

    assert account["platform"] == db.PLATFORM_FACEBOOK
    assert account["access_token"] == "page-token"


async def test_a_page_does_not_answer_the_instagram_readers(conn):
    """The account readers default to Instagram so that a Meta-only loop which
    was never taught about a fourth destination ignores it rather than sending
    a Page id to graph.instagram.com."""
    assert await db.active_accounts(conn) == []
    assert len(await db.active_accounts(conn, platform=db.PLATFORM_FACEBOOK)) == 1
