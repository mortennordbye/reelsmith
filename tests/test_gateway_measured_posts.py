"""Registering a post to be measured without answering its comments.

Until schema v8 the two were the same act, which is fine while every post is
registered seconds after it goes out. A backfill registers one that is days old,
and the difference between "read its numbers" and "reply to its comments" stops
being academic: the second one DMs people who commented on Saturday about a
video they have long since scrolled past.

The dangerous case is the second run. The backfill re-sends everything it can
match, so if a re-registration carried the flag it would disarm the poller on
every live post the moment somebody ran it twice, and the only symptom would be
a mechanic that quietly stopped working.
"""

from __future__ import annotations

import pytest

from gateway import db
from tests.gateway_harness import ACCOUNT, settings

LINK = "https://github.com/dietrichgebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    yield connection
    await connection.close()


async def test_a_measured_post_is_never_polled(conn):
    await db.register_post(
        conn, media_id="live", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    await db.register_post(
        conn,
        media_id="backfilled",
        ig_user_id=ACCOUNT,
        keyword="send",
        link=LINK,
        poll_comments=False,
    )

    pollable = await db.pollable_posts(conn, ACCOUNT, ttl_days=7)

    assert [row["media_id"] for row in pollable] == ["live"]


async def test_re_registering_never_disarms_a_live_post(conn):
    """The whole reason the flag is decided on the way in and never updated."""
    await db.register_post(
        conn, media_id="live", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    await db.register_post(
        conn,
        media_id="live",
        ig_user_id=ACCOUNT,
        keyword="PONYTAIL",
        link=LINK,
        poll_comments=False,
    )

    pollable = await db.pollable_posts(conn, ACCOUNT, ttl_days=7)

    assert [row["media_id"] for row in pollable] == ["live"]
    # The typo fix still lands. That is what an update is for.
    assert pollable[0]["keyword"] == "PONYTAIL"


async def test_a_measured_post_stays_measured_if_it_is_re_sent(conn):
    for _ in range(2):
        await db.register_post(
            conn,
            media_id="backfilled",
            ig_user_id=ACCOUNT,
            keyword="send",
            link=LINK,
            poll_comments=False,
        )

    assert await db.pollable_posts(conn, ACCOUNT, ttl_days=7) == []


async def test_the_post_is_listed_under_the_date_it_went_out(conn):
    """Not the date somebody noticed it was missing.

    `registered_at` stood in for the publish date, and for a backfill that is
    today, which would sort a Reel from July above one published this morning
    and make the Posts page lie about when the account was active.
    """
    await db.register_post(
        conn,
        media_id="backfilled",
        ig_user_id=ACCOUNT,
        keyword="send",
        link=LINK,
        published_at="2026-07-31T18:04:11+00:00",
        poll_comments=False,
    )

    rows = await db.published_media(conn, ACCOUNT)

    assert [row["media_id"] for row in rows] == ["backfilled"]
    assert rows[0]["published_at"] == "2026-07-31T18:04:11+00:00"
    assert rows[0]["repo_full_name"] == "dietrichgebert/ponytail"


async def test_a_post_registered_without_a_date_still_has_one(conn):
    """Every row written before v8, and every one the publisher writes."""
    await db.register_post(
        conn, media_id="live", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )

    rows = await db.published_media(conn, ACCOUNT)

    assert rows[0]["published_at"], "registered_at has to stand in when nothing better exists"


async def test_a_publish_date_is_filled_in_but_never_cleared(conn):
    await db.register_post(
        conn,
        media_id="m",
        ig_user_id=ACCOUNT,
        keyword="send",
        link=LINK,
        published_at="2026-07-31T18:04:11+00:00",
    )
    await db.register_post(
        conn, media_id="m", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )

    rows = await db.published_media(conn, ACCOUNT)

    assert rows[0]["published_at"] == "2026-07-31T18:04:11+00:00"


async def test_every_post_that_predates_the_flag_keeps_being_polled(cfg):
    """The upgrade must not silence the poller on posts that are already live.

    `DEFAULT 1` is what guarantees it, and it is worth pinning rather than
    trusting, because the failure is invisible: no error, no empty table, just
    an account that quietly stops answering comments.
    """
    import aiosqlite

    from gateway.db import _MIGRATIONS

    path = cfg.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    try:
        # A database exactly as v7 left it, with a live post in it.
        for statements in _MIGRATIONS[:7]:
            await connection.executescript(statements)
        await connection.execute("PRAGMA user_version=7")
        await connection.execute(
            "INSERT INTO posts (media_id, ig_user_id, keyword, link, registered_at) "
            "VALUES ('live', ?, 'send', ?, ?)",
            (ACCOUNT, LINK, db.iso(db.now())),
        )
        await connection.commit()

        assert await db.migrate(connection) == db.SCHEMA_VERSION

        pollable = await db.pollable_posts(connection, ACCOUNT, ttl_days=7)
        assert [row["media_id"] for row in pollable] == ["live"]
    finally:
        await connection.close()


async def test_a_backfilled_post_is_swept_for_insights(conn):
    """The point of registering it. Bounded on `registered_at`, so a Reel
    published long ago still gets one window in which to be read."""
    await db.register_post(
        conn,
        media_id="backfilled",
        ig_user_id=ACCOUNT,
        keyword="send",
        link=LINK,
        published_at="2026-06-01T10:00:00+00:00",
        poll_comments=False,
    )

    stale = await db.insights_stale_media(conn, ig_user_id=ACCOUNT, on="2026-08-02")

    assert [row["media_id"] for row in stale] == ["backfilled"]
