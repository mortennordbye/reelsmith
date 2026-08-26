"""A third destination, and the one whose token can be lost for good.

Two things here are unlike either platform already wired.

**The refresh token rotates.** Instagram's refresh rides on the render host's
`--snapshot` job and Google's refresh token has no clock at all, so neither has
ever needed a loop in this service. TikTok's access token lasts 24 hours and the
refresh token is rewritten on every use, so the token just spent is dead the
moment the response arrives. A dropped write is not a bad day, it is an account
recoverable only by a person in a browser. Most of this file is about that
write.

**There are two publish paths and they end differently.** `SEND_TO_USER_INBOX`
is the finish line on the unaudited path and an intermediate state on the
audited one, so a publisher that waits for `PUBLISH_COMPLETE` on the inbox path
polls until it times out on a video that already worked.
"""

from __future__ import annotations

import pytest

from gateway import db, poller, tiktok
from gateway.metrics import Metrics
from tests.gateway_harness import OPEN_ID, FakeMeta, settings

VIDEO_URL = "https://gate.example.test/media/abc.mp4"
TITLE = "Ponytail makes your coding agent stop and ask"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(
        connection, account_id=OPEN_ID, access_token="", platform=db.PLATFORM_TIKTOK
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


def credentials(refresh_token: str = "rft.original") -> tiktok.Credentials:
    return tiktok.Credentials(
        open_id=OPEN_ID,
        client_key="key",
        client_secret="secret",
        refresh_token=refresh_token,
    )


# --- The rotating refresh token ---------------------------------------------


async def test_a_refresh_hands_back_a_different_token(meta):
    async with meta.client() as http:
        fresh = await tiktok.refresh_access_token(http, credentials=credentials())

    assert fresh.access_token == meta.tiktok.access_token
    assert fresh.refresh_token == "rft.rotated"
    assert fresh.rotated_from("rft.original") is True


async def test_the_rotated_token_is_what_gets_stored(conn, meta):
    """The whole point of the loop. Storing what was sent instead of what came
    back works until the first rotation and then dies with no clock to explain
    it."""
    metrics = Metrics()

    async with meta.client() as http:
        assert await poller.refresh_tiktok_once(conn, http, metrics) == 1

    stored = await db.tiktok_credentials(conn, OPEN_ID)
    assert stored["refresh_token"] == "rft.rotated"
    assert meta.tiktok.refresh_tokens_seen == ["rft.original"]


async def test_a_second_pass_spends_the_token_the_first_one_stored(conn, meta):
    """A pass that read the original again would be spending a dead token, and
    the symptom would be a working integration that stops after a day."""
    metrics = Metrics()
    meta.tiktok.refresh_token = "rft.second"

    async with meta.client() as http:
        await poller.refresh_tiktok_once(conn, http, metrics)
        meta.tiktok.refresh_token = "rft.third"
        await poller.refresh_tiktok_once(conn, http, metrics)

    assert meta.tiktok.refresh_tokens_seen == ["rft.original", "rft.second"]
    stored = await db.tiktok_credentials(conn, OPEN_ID)
    assert stored["refresh_token"] == "rft.third"


async def test_a_refresh_returning_no_new_token_fails_rather_than_reusing_the_old(meta):
    """Carrying the old one forward is not a recovery. If TikTok rotated and the
    response was misread, the old token is already dead and reusing it burns the
    next attempt too."""
    meta.tiktok.refresh_token = ""

    async with meta.client() as http:
        with pytest.raises(tiktok.PublishError, match="no refresh token"):
            await tiktok.refresh_access_token(http, credentials=credentials())


async def test_a_failed_refresh_leaves_the_stored_token_alone(conn, meta):
    """The stored token is still good; only the attempt failed. Overwriting it
    with anything would turn a retryable pass into a lost account."""
    metrics = Metrics()
    meta.tiktok.token_error = "invalid_grant"

    async with meta.client() as http:
        assert await poller.refresh_tiktok_once(conn, http, metrics) == 0

    stored = await db.tiktok_credentials(conn, OPEN_ID)
    assert stored["refresh_token"] == "rft.original"


async def test_an_account_with_no_credentials_row_is_skipped_not_crashed(conn, meta):
    await db.upsert_account(
        conn, account_id="_another_open_id", access_token="", platform=db.PLATFORM_TIKTOK
    )
    metrics = Metrics()

    async with meta.client() as http:
        assert await poller.refresh_tiktok_once(conn, http, metrics) == 1


async def test_the_meta_refresher_does_not_see_a_tiktok_account(conn, meta):
    """The same claim the YouTube rows rest on: every loop that talks to Meta
    reads only the Instagram accounts. A TikTok open id reaching
    graph.instagram.com with an empty token is not a test failure, it is a live
    account being handed to the wrong API."""
    assert await db.all_accounts(conn) == []
    assert len(await db.all_accounts(conn, platform=db.PLATFORM_TIKTOK)) == 1


# --- Two paths that end differently -----------------------------------------


async def test_the_inbox_path_sends_no_post_info(meta):
    """Left off entirely rather than sent empty. The inbox endpoint does not
    take it, so an empty one is a different request rather than a harmless
    extra."""
    async with meta.client() as http:
        await tiktok.start_publish(
            http, token="act", video_url=VIDEO_URL, title=TITLE, direct_post=False
        )

    assert meta.tiktok.init_urls == [tiktok.INBOX_INIT_URL]
    assert "post_info" not in meta.tiktok.inits[0]
    assert meta.tiktok.inits[0]["source_info"]["source"] == "PULL_FROM_URL"


async def test_the_direct_path_carries_the_privacy_level_it_was_given(meta):
    """It has to be one of the options `creator_info` returned, or the post
    fails `privacy_level_option_mismatch`, which reads like a bad constant and
    is actually a stale read."""
    async with meta.client() as http:
        await tiktok.start_publish(
            http,
            token="act",
            video_url=VIDEO_URL,
            title=TITLE,
            direct_post=True,
            privacy_level="SELF_ONLY",
            is_aigc=False,
            cover_timestamp_ms=3000,
        )

    assert meta.tiktok.init_urls == [tiktok.DIRECT_INIT_URL]
    post_info = meta.tiktok.inits[0]["post_info"]
    assert post_info["privacy_level"] == "SELF_ONLY"
    assert post_info["is_aigc"] is False
    assert post_info["video_cover_timestamp_ms"] == 3000


async def test_the_inbox_path_finishes_at_send_to_user_inbox(meta):
    """A publisher waiting for PUBLISH_COMPLETE here polls until it times out on
    a video already sitting in the creator's drafts, and reports a failure for
    something that worked."""
    meta.tiktok.statuses = ["PROCESSING_DOWNLOAD", "SEND_TO_USER_INBOX"]

    async with meta.client() as http:
        result = await tiktok.await_publish(
            http,
            token="act",
            publish_id="tt-publish-1",
            direct_post=False,
            poll_interval_s=0,
        )

    assert result.in_inbox is True
    assert result.status == tiktok.STATUS_INBOX_DONE


async def test_the_direct_path_waits_past_the_inbox_state(meta):
    meta.tiktok.statuses = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]

    async with meta.client() as http:
        result = await tiktok.await_publish(
            http, token="act", publish_id="p", direct_post=True, poll_interval_s=0
        )

    assert result.status == tiktok.STATUS_PUBLISHED
    assert result.in_inbox is False


async def test_a_failed_status_stops_rather_than_polling_on(meta):
    meta.tiktok.statuses = ["FAILED"]
    meta.tiktok.fail_reason = "video_format_check_failed"

    async with meta.client() as http:
        with pytest.raises(tiktok.PublishError, match="video_format_check_failed") as exc:
            await tiktok.await_publish(
                http, token="act", publish_id="p", direct_post=True, poll_interval_s=0
            )

    assert exc.value.publish_started is True


# --- Failure on the right side of the line ----------------------------------


async def test_a_refusal_at_init_is_retryable(meta):
    """Nothing exists on TikTok before `video/init/` returns, so the slot gets
    its turn back. This is the same line `publisher.PublishError` draws for
    Meta and `youtube.UploadError` for Google."""
    meta.tiktok.error_code = tiktok.ERROR_UNAUDITED

    async with meta.client() as http:
        with pytest.raises(tiktok.PublishError) as exc:
            await tiktok.start_publish(
                http, token="act", video_url=VIDEO_URL, title=TITLE, direct_post=True,
                privacy_level="PUBLIC_TO_EVERYONE",
            )

    assert exc.value.publish_started is False
    assert exc.value.code == tiktok.ERROR_UNAUDITED


async def test_a_failure_after_the_publish_id_is_not(meta):
    """A post may exist and no error text proves otherwise, so it stops and
    waits for a person rather than taking its slot back."""
    meta.tiktok.statuses = ["FAILED"]

    async with meta.client() as http:
        with pytest.raises(tiktok.PublishError) as exc:
            await tiktok.await_publish(
                http, token="act", publish_id="p", direct_post=True, poll_interval_s=0
            )

    assert exc.value.publish_started is True


async def test_an_error_inside_a_200_is_still_an_error(meta):
    """TikTok reports failure with HTTP 200 as readily as with a status code.
    Reading the status alone would treat a refusal as a successful post."""
    meta.tiktok.error_code = "scope_not_authorized"

    async with meta.client() as http:
        with pytest.raises(tiktok.PublishError, match="scope_not_authorized"):
            await tiktok.creator_info(http, token="act")


async def test_creator_info_reports_what_may_be_asked_for(meta):
    """Mandatory rather than advisory, and the only authoritative answer to how
    long a video may be."""
    async with meta.client() as http:
        info = await tiktok.creator_info(http, token="act")

    assert "SELF_ONLY" in info.privacy_level_options
    assert info.max_video_post_duration_sec == 600
    assert info.username == "nightlybuild"


async def test_a_title_past_the_limit_is_trimmed_rather_than_refused(meta):
    """2,200 UTF-16 runes carrying the ask, the link and the hashtags together.
    The hook is capped well inside it upstream, so reaching this bound means
    something odd rather than something fatal."""
    async with meta.client() as http:
        await tiktok.start_publish(
            http,
            token="act",
            video_url=VIDEO_URL,
            title="x" * (tiktok.MAX_TITLE + 50),
            direct_post=True,
            privacy_level="SELF_ONLY",
        )

    assert len(meta.tiktok.inits[0]["post_info"]["title"]) == tiktok.MAX_TITLE
