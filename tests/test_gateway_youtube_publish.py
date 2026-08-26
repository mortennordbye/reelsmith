"""The scheduler putting a queued row on YouTube instead of Instagram.

The queue, the slots and the two claims are shared and already tested. What is
new here is one branch and one ordering, and the ordering is the part worth
guarding: the session URI is committed before any bytes move, so that a process
which dies mid-transfer leaves a row that can answer the only question that
matters afterwards, which is whether anything was created.
"""

from __future__ import annotations

import pytest

from gateway import db, scheduler
from gateway.config import GatewaySettings
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, CHANNEL, FakeMeta, settings

LINK = "https://github.com/DietrichGebert/ponytail"
TITLE = "TensorFlow is removing the part that runs on phones"
DESCRIPTION = "tf.lite is going away.\n\nRepo: https://github.com/tensorflow/tensorflow"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"0" * 4096


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


def _video(cfg, name: str = "short-abc123.mp4") -> str:
    directory = cfg.covers_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(VIDEO)
    return name


async def _queue(conn, cfg, **overrides) -> int:
    fields = {
        "account_id": CHANNEL,
        "video_name": _video(cfg),
        "cover_name": None,
        "caption": DESCRIPTION,
        "keyword": "send",
        "link": LINK,
        "title": TITLE,
        "approved": True,
        **overrides,
    }
    queued_id = await db.enqueue_post(conn, **fields)
    await db.claim_queued(conn, queued_id)
    return queued_id


async def _publish(conn, graph, cfg, metrics, queued_id):
    account = await db.get_account(conn, CHANNEL)
    queued = await db.get_queued(conn, queued_id)
    return await scheduler.publish_queued(
        conn, graph, cfg, metrics, account=account, queued=queued
    )


# --- The happy path ---------------------------------------------------------


async def test_a_queued_short_is_uploaded_and_recorded(conn, graph, cfg, metrics, meta):
    queued_id = await _queue(conn, cfg)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (True, False)
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_PUBLISHED
    assert row["media_id"] == "yt-video-1"
    assert row["permalink"] == "https://www.youtube.com/watch?v=yt-video-1"
    assert meta.youtube.uploads == [len(VIDEO)], "the whole file, exactly once"


async def test_the_metadata_is_what_the_mac_stored(conn, graph, cfg, metrics, meta):
    """Title and description are carried, not rebuilt. A gateway that
    reconstructed either would be a second place for the copy to drift."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn, cfg))

    snippet = meta.youtube.sessions[0]["snippet"]
    status = meta.youtube.sessions[0]["status"]
    assert snippet["title"] == TITLE
    assert snippet["description"] == DESCRIPTION
    assert status["privacyStatus"] == "private"
    assert status["selfDeclaredMadeForKids"] is False
    assert status["containsSyntheticMedia"] is False


async def test_nothing_is_registered_for_the_comment_poller(conn, graph, cfg, metrics):
    """`register_post` arms the comment sweep and the insights sweep, both of
    which are Meta-only. A YouTube video id in `posts` is precisely the row
    those loops must never pick up."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn, cfg))

    assert await db.pollable_posts(conn, CHANNEL, ttl_days=30) == []


# --- The ordering that makes a restart safe ---------------------------------


async def test_the_session_uri_is_committed_before_the_bytes_go_up(
    conn, graph, cfg, metrics, meta
):
    """The whole reason the scheduler does not call `youtube.upload`.

    If the process dies mid-transfer, the only question worth answering is
    whether anything was created, and the answer has to survive in the database
    rather than in a stack frame.
    """
    meta.youtube.upload_status = 500
    queued_id = await _queue(conn, cfg)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    row = await db.get_queued(conn, queued_id)
    assert row["container_id"] == meta.youtube.session_uri
    assert row["state"] == db.QUEUE_FAILED


async def test_a_refused_session_hands_the_slot_back(conn, graph, cfg, metrics, meta):
    """Nothing was created, so this is the one failure worth retrying."""
    meta.youtube.session_status = 503
    queued_id = await _queue(conn, cfg)

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, True)
    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_APPROVED
    assert row["container_id"] is None
    assert meta.youtube.uploads == []


async def test_a_dead_refresh_token_is_retried_then_given_up_on(
    conn, graph, cfg, metrics, meta
):
    """`invalid_grant` is not transient, but it fails before a session exists,
    so it takes the retryable path until the budget is spent rather than being
    special-cased on an error string."""
    meta.youtube.token_status = 400
    # Two, because claiming a row is what increments `attempts`, so the budget
    # is already at one by the time the first publish is attempted.
    cfg = settings(cfg.db_path.parent, max_publish_attempts=2)
    queued_id = await _queue(conn, cfg)

    _, retry = await _publish(conn, graph, cfg, metrics, queued_id)
    assert retry is True

    await db.claim_queued(conn, queued_id)
    _, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert retry is False
    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_FAILED


# --- Refusals that a retry cannot fix ---------------------------------------


async def test_a_channel_with_no_credentials_fails_without_asking_google(
    conn, graph, cfg, metrics, meta
):
    await db.upsert_account(
        conn, account_id="UCorphaned000000000000000", access_token="",
        platform=db.PLATFORM_YOUTUBE,
    )
    queued_id = await _queue(conn, cfg, account_id="UCorphaned000000000000000")

    account = await db.get_account(conn, "UCorphaned000000000000000")
    queued = await db.get_queued(conn, queued_id)
    published, retry = await scheduler.publish_queued(
        conn, graph, cfg, metrics, account=account, queued=queued
    )

    assert (published, retry) == (False, False)
    assert meta.youtube.sessions == []


async def test_a_row_with_no_title_fails_rather_than_inventing_one(
    conn, graph, cfg, metrics, meta
):
    """YouTube rejects an empty title, and a video named after its own file is
    worse than one that waits for a person."""
    queued_id = await _queue(conn, cfg, title="")

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    assert meta.youtube.sessions == []


async def test_a_missing_video_file_fails_before_the_network(
    conn, graph, cfg, metrics, meta
):
    queued_id = await _queue(conn, cfg, video_name="gone.mp4")

    published, retry = await _publish(conn, graph, cfg, metrics, queued_id)

    assert (published, retry) == (False, False)
    assert meta.youtube.sessions == []


# --- The audit lock ---------------------------------------------------------


async def test_a_downgraded_privacy_status_is_reported(
    conn, graph, cfg, metrics, meta, caplog
):
    """An upload that succeeds but comes back less public than it asked for.

    The known cause is the audit restriction on an API project, which was
    measured as not applying here on 2026-08-16. That makes this more worth
    keeping rather than less: if it ever starts, the upload still succeeds and
    the only sign is a value that quietly differs from the one requested.
    """
    cfg = settings(cfg.db_path.parent, youtube_privacy_status="public")
    queued_id = await _queue(conn, cfg)

    with caplog.at_level("WARNING"):
        published, _ = await _publish(conn, graph, cfg, metrics, queued_id)

    assert published is True
    assert "asked for public and got private" in caplog.text


async def test_a_privacy_status_that_sticks_says_nothing(
    conn, graph, cfg, metrics, meta, caplog
):
    """The normal case, and the one that would have been noise if the check
    compared against the word "private" instead of against what was asked."""
    cfg = settings(cfg.db_path.parent, youtube_privacy_status="public")
    meta.youtube.privacy_status = "public"
    queued_id = await _queue(conn, cfg)

    with caplog.at_level("WARNING"):
        published, _ = await _publish(conn, graph, cfg, metrics, queued_id)

    assert published is True
    assert "asked for" not in caplog.text


# --- The other platform is untouched ----------------------------------------


async def test_an_instagram_row_still_takes_the_meta_path(cfg, graph, metrics, meta):
    """The dispatch must be the only thing that changed for Instagram."""
    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        queued_id = await db.enqueue_post(
            conn, account_id=ACCOUNT, video_name=_video(cfg), cover_name=None,
            caption="a caption", keyword="send", link=LINK, approved=True,
        )
        await db.claim_queued(conn, queued_id)
        account = await db.get_account(conn, ACCOUNT)
        queued = await db.get_queued(conn, queued_id)

        await scheduler.publish_queued(
            conn, graph, cfg, metrics, account=account, queued=queued
        )

        assert meta.youtube.sessions == [], "no Google call for an Instagram row"
        assert any("/media" in call for call in meta.calls)
    finally:
        await conn.close()


def test_the_privacy_default_is_private(tmp_path):
    """Not a preference. An unaudited project produces private videos whatever
    this says, so anything else is a confusing log line waiting to happen."""
    cfg = GatewaySettings(app_secret="x", verify_token="y", api_token="z", _env_file=None)
    assert cfg.youtube_privacy_status == "private"
