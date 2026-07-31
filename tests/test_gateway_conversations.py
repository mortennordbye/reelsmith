"""The state machine, which is where every rule Meta enforces lives.

If one test in this file matters more than the others it is the duplicate
private reply one. Meta allows exactly one per comment, ever, and there is no
way to take a second one back.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gateway import conversations, copy, db
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, IGSID, FakeMeta, comment, settings

LINK = "https://github.com/DietrichGebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok", username="reels")
    await db.register_post(
        connection, media_id="media-1", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    yield connection
    await connection.close()


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


async def account_row(conn):
    return await db.get_account(conn, ACCOUNT)


async def post_row(conn):
    return await db.get_post(conn, "media-1")


async def reply_to(conn, graph, cfg, metrics, cid="c1"):
    return await conversations.handle_comment(
        conn,
        graph,
        cfg,
        metrics,
        account=await account_row(conn),
        post=await post_row(conn),
        comment_id=cid,
        author_id="commenter-1",
    )


async def inbound(conn, graph, cfg, metrics, igsid=IGSID):
    return await conversations.handle_inbound_message(
        conn, graph, cfg, metrics, igsid=igsid, ig_user_id=ACCOUNT
    )


# --- Keyword matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SEND", True),
        ("send", True),
        ("please send!", True),
        ("Send me the link", True),
        ("sending", False),
        ("resend", False),
        ("", False),
    ],
)
def test_keyword_matches_whole_words_only(text, expected):
    # "sending" costing a private reply that cannot be retracted is the reason
    # this is not a substring check.
    assert conversations.comment_matches(text, "send") is expected


# --- The one private reply --------------------------------------------------


async def test_a_matching_comment_gets_one_private_reply(conn, graph, cfg, metrics, meta):
    outcome = await reply_to(conn, graph, cfg, metrics)

    assert outcome.action == "replied"
    assert meta.texts == [copy.PRIVATE_REPLY]
    assert meta.sends[0]["recipient"] == {"comment_id": "c1"}


async def test_the_same_comment_is_never_replied_to_twice(conn, graph, cfg, metrics, meta):
    first = await reply_to(conn, graph, cfg, metrics)
    second = await reply_to(conn, graph, cfg, metrics)

    assert first.action == "replied"
    assert second.action == "skipped"
    assert len(meta.sends) == 1


async def test_a_crash_after_claiming_still_blocks_a_retry(conn, graph, cfg, metrics, meta):
    """The claim is the whole point: it survives the process that made it.

    Losing one reply is the deliberate trade. Meta may have accepted the send
    before the crash, and a duplicate is worse than a miss.
    """
    await db.claim_comment(conn, comment_id="c9", media_id="media-1", ig_user_id=ACCOUNT)

    outcome = await reply_to(conn, graph, cfg, metrics, cid="c9")

    assert outcome.action == "skipped"
    assert meta.sends == []


async def test_a_failed_send_keeps_the_claim_and_records_why(conn, graph, cfg, metrics, meta):
    meta.fail_sends_with = {"message": "Too many private replies", "code": 613}

    outcome = await reply_to(conn, graph, cfg, metrics)

    assert outcome.action == "failed"
    row = await db.comment_row(conn, "c1")
    assert row["replied_at"] is None
    assert "private replies" in row["failure"]


async def test_the_account_never_replies_to_itself(conn, graph, cfg, metrics, meta):
    outcome = await conversations.handle_comment(
        conn,
        graph,
        cfg,
        metrics,
        account=await account_row(conn),
        post=await post_row(conn),
        comment_id="c-own",
        author_id=ACCOUNT,
    )

    assert outcome.action == "skipped"
    assert meta.sends == []
    # And it did not burn the comment id either, so nothing is left half-claimed.
    assert await db.comment_row(conn, "c-own") is None


async def test_the_private_reply_opens_a_conversation_from_the_recipient_id(
    conn, graph, cfg, metrics
):
    await reply_to(conn, graph, cfg, metrics)

    conversation = await db.get_conversation(conn, igsid=IGSID, ig_user_id=ACCOUNT)
    assert conversation is not None
    assert conversation["state"] == db.STATE_REPLIED
    assert conversation["media_id"] == "media-1"


# --- The follow gate --------------------------------------------------------


async def test_a_reply_without_a_follow_gets_a_nudge_not_the_link(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "nudged"
    assert meta.texts == [copy.NUDGE]
    assert LINK not in "".join(meta.texts)


async def test_the_link_arrives_once_the_follow_shows_up(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    await inbound(conn, graph, cfg, metrics)  # not following yet
    meta.follows = True
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "link_sent"
    assert LINK in meta.texts[0]
    conversation = await db.get_conversation(conn, igsid=IGSID, ig_user_id=ACCOUNT)
    assert conversation["state"] == db.STATE_CONVERTED
    assert conversation["link_sent_at"] is not None


async def test_the_link_is_sent_exactly_once(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    meta.follows = True
    await inbound(conn, graph, cfg, metrics)
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert meta.sends == []


async def test_an_unreadable_profile_counts_as_not_following(conn, graph, cfg, metrics, meta):
    """Consent errors are the expected state before someone has written to us.

    Treating an unknown answer as a follow would make the gate decorative.
    """
    await reply_to(conn, graph, cfg, metrics)
    meta.profile_error = {"message": "user consent is required", "code": 10}
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "nudged"


async def test_nudges_stop_at_the_cap(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    for _ in range(cfg.max_nudges):
        await inbound(conn, graph, cfg, metrics)
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert meta.sends == []


async def test_a_follow_after_the_cap_still_gets_the_link(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    for _ in range(cfg.max_nudges + 2):
        await inbound(conn, graph, cfg, metrics)
    meta.follows = True
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "link_sent"
    assert LINK in meta.texts[0]


# --- The 24 hour window -----------------------------------------------------


async def test_nothing_goes_out_after_the_window_closes(
    conn, graph, cfg, metrics, meta, monkeypatch
):
    await reply_to(conn, graph, cfg, metrics)
    meta.follows = True
    meta.sends.clear()

    # Backdate the inbound message past the window. The handler stamps a fresh
    # one, so the backdating has to happen to the row it will read.
    stale = db.iso(db.now() - timedelta(hours=cfg.message_window_h + 1))
    await conn.execute(
        "UPDATE conversations SET last_inbound_at = ? WHERE igsid = ?", (stale, IGSID)
    )
    await conn.commit()

    async def frozen(*_args, **_kwargs):
        return None

    # record_inbound is what would re-open the window, so silence it to model a
    # send attempted from a stale conversation rather than from a live message.
    monkeypatch.setattr(db, "record_inbound", frozen)
    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert "24 hour window" in outcome.detail
    assert meta.sends == []


def test_the_window_is_closed_until_someone_writes():
    assert not conversations.window_is_open({"last_inbound_at": None}, window_s=86_400)


# --- The kill switch --------------------------------------------------------


async def test_the_kill_switch_stops_outbound_dms(conn, graph, cfg, metrics, meta):
    await reply_to(conn, graph, cfg, metrics)
    meta.follows = True
    meta.sends.clear()
    await db.set_account_flags(conn, ACCOUNT, dm_enabled=False)

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert outcome.detail == "kill switch"
    assert meta.sends == []


async def test_the_kill_switch_stops_private_replies_too(conn, graph, cfg, metrics, meta):
    await db.set_account_flags(conn, ACCOUNT, dm_enabled=False)

    outcome = await reply_to(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert meta.sends == []


# --- Unknown senders --------------------------------------------------------


async def test_a_cold_dm_is_recorded_and_ignored(conn, graph, cfg, metrics, meta):
    """Nobody who was sent a private reply can land here.

    Answering a cold DM with an automated follow gate is noise, and a worse
    policy position than staying quiet.
    """
    outcome = await inbound(conn, graph, cfg, metrics, igsid="stranger")

    assert outcome.action == "skipped"
    assert meta.sends == []


# --- The copy ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(copy.TEMPLATES))
def test_the_dm_copy_obeys_the_repo_text_rules(name):
    assert copy.check_copy(copy.TEMPLATES[name]) == []


def test_the_first_message_discloses_the_automation():
    """Meta's messaging policy asks for this where law requires it, naming
    California and Germany. It is one clause and easy to lose to a copy edit
    that is only thinking about tone."""
    assert "automated" in copy.PRIVATE_REPLY.lower()


def test_the_copy_never_pretends_to_be_a_person():
    """The other half of the same policy line, and the one a well-meaning copy
    edit is most likely to break."""
    for name, template in copy.TEMPLATES.items():
        lowered = template.lower()
        for pretence in ("i am typing", "one moment", "let me check", "hang on", "brb"):
            assert pretence not in lowered, f"{name} implies a human is answering"


def test_a_link_with_a_hyphen_survives_the_template():
    rendered = copy.link_message("https://github.com/some-owner/some-repo")
    assert "some-owner/some-repo" in rendered


def test_the_harness_comment_helper_is_shaped_like_metas():
    assert comment("c1", "send")["from"]["id"] == "commenter-1"


# --- Returning commenters ---------------------------------------------------
#
# Conversion used to be a property of the person, which made `converted` a dead
# end: someone who got one link could never receive another, and nothing logged
# an error. It failed the most engaged part of the audience while every counter
# looked healthy. A delivery is now per person and per post.


LINK2 = "https://github.com/xai-org/grok-build"


async def _second_post(conn):
    await db.register_post(
        conn, media_id="media-2", ig_user_id=ACCOUNT, keyword="grok", link=LINK2
    )
    return await db.get_post(conn, "media-2")


async def test_a_returning_commenter_gets_the_second_posts_link(
    conn, graph, cfg, metrics, meta
):
    """The bug this file exists to prevent coming back."""
    meta.follows = True
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    await inbound(conn, graph, cfg, metrics)
    assert LINK in meta.texts[-1]

    post2 = await _second_post(conn)
    meta.sends.clear()
    await conversations.handle_comment(
        conn, graph, cfg, metrics,
        account=await account_row(conn), post=post2,
        comment_id="c2", author_id="commenter-1",
    )
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "link_sent"
    assert LINK2 in meta.texts[0], "the second post's link, not the first"


async def test_a_link_is_never_sent_twice_for_the_same_post(conn, graph, cfg, metrics, meta):
    meta.follows = True
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    await inbound(conn, graph, cfg, metrics)
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert meta.sends == []


async def test_someone_who_unfollows_and_asks_again_is_told_to_follow(
    conn, graph, cfg, metrics, meta
):
    """The gate has to hold both ways, or the follow it bought is not kept."""
    meta.follows = True
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    await inbound(conn, graph, cfg, metrics)

    meta.follows = False  # they unfollow
    post2 = await _second_post(conn)
    await conversations.handle_comment(
        conn, graph, cfg, metrics,
        account=await account_row(conn), post=post2,
        comment_id="c2", author_id="commenter-1",
    )
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "nudged"
    assert LINK2 not in "".join(meta.texts)


async def test_chatting_with_nothing_outstanding_gets_no_reply(conn, graph, cfg, metrics, meta):
    """Answering a plain thank-you with "you need to follow" would be obnoxious,
    and the account would deserve the unfollow."""
    meta.follows = True
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    await inbound(conn, graph, cfg, metrics)

    meta.follows = False  # unfollowed, but has not asked for anything new
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "skipped"
    assert outcome.detail == "nothing outstanding"
    assert meta.sends == []


async def test_the_nudge_budget_resets_for_a_new_ask(conn, graph, cfg, metrics, meta):
    """The cap bounds reminders for one ask, not for a lifetime. Without the
    reset, someone who ignored the first post could never be nudged again."""
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    for _ in range(cfg.max_nudges + 1):
        await inbound(conn, graph, cfg, metrics)

    post2 = await _second_post(conn)
    await conversations.handle_comment(
        conn, graph, cfg, metrics,
        account=await account_row(conn), post=post2,
        comment_id="c2", author_id="commenter-1",
    )
    meta.sends.clear()

    outcome = await inbound(conn, graph, cfg, metrics)

    assert outcome.action == "nudged"


async def test_the_backfill_does_not_resend_to_someone_already_converted(cfg):
    """Shipping this must not blast a link at everyone who already has one."""
    conn = await db.connect(cfg.db_path)
    try:
        await db.upsert_account(conn, ig_user_id=ACCOUNT, access_token="tok")
        await db.register_post(
            conn, media_id="media-1", ig_user_id=ACCOUNT, keyword="send", link=LINK
        )
        await db.claim_comment(
            conn, comment_id="c1", media_id="media-1", ig_user_id=ACCOUNT
        )
        await db.mark_comment_replied(conn, "c1", igsid=IGSID)
        await db.start_conversation(
            conn, igsid=IGSID, ig_user_id=ACCOUNT, media_id="media-1"
        )
        await db.record_delivery(
            conn, igsid=IGSID, ig_user_id=ACCOUNT, media_id="media-1"
        )

        assert await db.pending_ask(conn, igsid=IGSID, ig_user_id=ACCOUNT) is None
    finally:
        await conn.close()


async def test_two_comments_by_one_person_earn_one_dm(conn, graph, cfg, metrics, meta):
    """Meta's one-reply-per-comment rule is per comment, not per person. Two
    identical DMs about the same Reel reads as a broken bot."""
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    meta.sends.clear()

    outcome = await reply_to(conn, graph, cfg, metrics, cid="c2")

    assert outcome.action == "skipped"
    assert "duplicate" in outcome.detail
    assert meta.sends == []
    # The claim is kept, or the poller reconsiders it every sweep.
    assert await db.comment_row(conn, "c2") is not None


async def test_two_different_people_both_get_a_reply(conn, graph, cfg, metrics, meta):
    await conversations.handle_comment(
        conn, graph, cfg, metrics, account=await account_row(conn),
        post=await post_row(conn), comment_id="c1", author_id="person-a",
    )
    await conversations.handle_comment(
        conn, graph, cfg, metrics, account=await account_row(conn),
        post=await post_row(conn), comment_id="c2", author_id="person-b",
    )

    assert len(meta.sends) == 2


async def test_the_same_person_commenting_on_a_second_post_still_gets_a_reply(
    conn, graph, cfg, metrics, meta
):
    """The guard is per post. A new Reel is a new ask."""
    await reply_to(conn, graph, cfg, metrics, cid="c1")
    await db.register_post(
        conn, media_id="media-2", ig_user_id=ACCOUNT, keyword="grok", link=LINK2
    )
    meta.sends.clear()

    outcome = await conversations.handle_comment(
        conn, graph, cfg, metrics, account=await account_row(conn),
        post=await db.get_post(conn, "media-2"), comment_id="c9", author_id="commenter-1",
    )

    assert outcome.action == "replied"
    assert len(meta.sends) == 1
