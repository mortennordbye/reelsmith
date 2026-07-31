"""The DM state machine, and the three rules Meta will not bend on.

    comment "SEND"
      └─► private reply, one per comment ever
          └─► they reply anything            (opens the 24h window, and only
              │                               then can we read their profile)
              └─► do they follow?
                  ├─ yes ─► send the link, mark converted
                  └─ no  ─► nudge, re-check on the next message

The rules, and where each is enforced:

1. **One private reply per comment, ever.** `db.claim_comment` inserts the
   comment id before the send. A crash between the insert and the send loses
   that one reply, which is the correct direction to fail: Meta may already
   have accepted it, and a person receiving the same automated message twice is
   worse than one who receives it never.
2. **Follower data is consent gated.** `_follow_state` is only ever called from
   the inbound message path. Asking at comment time returns a consent error,
   which is why the gate lives here and not in the poller.
3. **The 24 hour window.** Every outbound DM checks `last_inbound_at` first. A
   lapsed window is not an error; the person gets the link the next time they
   write, whenever that is.

On top of Meta's rules there is one of ours: `accounts.dm_enabled` is checked
before every outbound message. That is the kill switch, and it has to sit below
the state machine so that nothing can route around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from gateway import copy, db
from gateway.config import GatewaySettings
from gateway.graph import GraphClient, GraphError
from gateway.metrics import Metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """What happened, in terms the funnel counts."""

    action: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.action not in {"skipped", "failed"}


SKIPPED = Outcome("skipped")


def _dm_allowed(account: Any) -> bool:
    return bool(account["dm_enabled"]) and bool(account["active"])


def window_is_open(conversation: Any, *, window_s: int) -> bool:
    """True while Meta still allows an outbound message on this conversation."""
    last = db.parse_iso(conversation["last_inbound_at"])
    if last is None:
        return False
    return (db.now() - last).total_seconds() < window_s


# --------------------------------------------------------------------------
# Comment side
# --------------------------------------------------------------------------


def comment_matches(text: str, keyword: str) -> bool:
    """Whole-word, case-insensitive match.

    Substring matching would fire on "sending" and on "resend", and the private
    reply it wastes cannot be taken back.
    """
    if not keyword:
        return False
    words = {"".join(c for c in w if c.isalnum()).lower() for w in text.split()}
    return keyword.strip().lower() in words


async def handle_comment(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    account: Any,
    post: Any,
    comment_id: str,
    author_id: str | None,
) -> Outcome:
    """Send the one private reply this comment gets, and open a conversation."""
    ig_user_id = account["ig_user_id"]

    # Our own replies come back from the comments endpoint too, and replying to
    # ourselves would be a loop with a rate limit at the end of it.
    if author_id and author_id == ig_user_id:
        return SKIPPED

    if not await db.claim_comment(
        conn, comment_id=comment_id, media_id=post["media_id"], ig_user_id=ig_user_id
    ):
        return SKIPPED

    metrics.comments_matched.inc()

    if not _dm_allowed(account):
        await db.mark_comment_failed(conn, comment_id, "dm_enabled is off")
        return Outcome("skipped", "kill switch")

    try:
        result = await graph.send_private_reply(
            ig_user_id=ig_user_id,
            token=account["access_token"],
            comment_id=comment_id,
            text=copy.PRIVATE_REPLY,
        )
    except GraphError as exc:
        log.warning("Private reply to %s failed: %s", comment_id, exc)
        metrics.graph_errors.inc()
        await db.mark_comment_failed(conn, comment_id, str(exc))
        return Outcome("failed", str(exc))

    await db.mark_comment_replied(conn, comment_id, igsid=result.recipient_id)
    metrics.private_replies.inc()

    # The send response is the only place the commenter's IGSID appears before
    # they write to us. Without it the conversation cannot be tied to the link
    # they asked for, so a missing one is worth a log line.
    if result.recipient_id:
        await db.start_conversation(
            conn,
            igsid=result.recipient_id,
            ig_user_id=ig_user_id,
            media_id=post["media_id"],
        )
    else:
        log.warning("Private reply to %s returned no recipient_id", comment_id)

    return Outcome("replied", comment_id)


# --------------------------------------------------------------------------
# Message side
# --------------------------------------------------------------------------


async def _follow_state(
    graph: GraphClient, metrics: Metrics, *, igsid: str, token: str
) -> bool | None:
    metrics.follow_checks.inc()
    try:
        profile = await graph.get_profile(igsid=igsid, token=token)
    except GraphError as exc:
        # Anything other than the expected consent error is a real failure, and
        # the safe reading of "we do not know" is "not yet". Sending the link on
        # an unknown state would make the follow gate decorative.
        log.warning("Follow check for %s failed: %s", igsid, exc)
        metrics.graph_errors.inc()
        return None
    if profile.follows_us:
        metrics.follows_confirmed.inc()
    return profile.follows_us


async def handle_inbound_message(
    conn: aiosqlite.Connection,
    graph: GraphClient,
    cfg: GatewaySettings,
    metrics: Metrics,
    *,
    igsid: str,
    ig_user_id: str,
) -> Outcome:
    """Someone wrote to the account. Decide whether they get the link."""
    metrics.inbound_messages.inc()

    account = await db.get_account(conn, ig_user_id)
    if account is None:
        log.warning("Message for unknown account %s", ig_user_id)
        return Outcome("skipped", "unknown account")

    conversation = await db.get_conversation(conn, igsid=igsid, ig_user_id=ig_user_id)
    if conversation is None:
        # Nobody who was sent a private reply can land here, because that path
        # creates the row. This is a cold DM, and answering one with an
        # automated follow gate is both noise and a worse policy position than
        # staying quiet.
        log.info("Inbound from %s with no conversation, ignoring", igsid)
        return Outcome("skipped", "no conversation")

    await db.record_inbound(conn, igsid=igsid, ig_user_id=ig_user_id)
    conversation = await db.get_conversation(conn, igsid=igsid, ig_user_id=ig_user_id)
    assert conversation is not None

    if conversation["state"] == db.STATE_CONVERTED:
        return Outcome("skipped", "already converted")

    if not _dm_allowed(account):
        return Outcome("skipped", "kill switch")

    post = await db.get_post(conn, conversation["media_id"]) if conversation["media_id"] else None
    if post is None:
        log.warning("Conversation with %s points at no post", igsid)
        return Outcome("skipped", "no post")

    follows = await _follow_state(
        graph, metrics, igsid=igsid, token=account["access_token"]
    )
    await db.update_conversation(
        conn, igsid=igsid, ig_user_id=ig_user_id, bump_follow_checks=True
    )

    # Re-read: recording the inbound above is what opened the window, so this is
    # the state the send has to be judged against.
    conversation = await db.get_conversation(conn, igsid=igsid, ig_user_id=ig_user_id)
    assert conversation is not None
    if not window_is_open(conversation, window_s=cfg.message_window_s):
        metrics.window_lapsed.inc()
        return Outcome("skipped", "outside the 24 hour window")

    if follows:
        late = int(conversation["nudges_sent"]) > 0
        text = copy.link_message(post["link"], late=late)
        try:
            await graph.send_message(
                ig_user_id=ig_user_id,
                token=account["access_token"],
                igsid=igsid,
                text=text,
            )
        except GraphError as exc:
            log.warning("Link send to %s failed: %s", igsid, exc)
            metrics.graph_errors.inc()
            return Outcome("failed", str(exc))

        await db.update_conversation(
            conn,
            igsid=igsid,
            ig_user_id=ig_user_id,
            state=db.STATE_CONVERTED,
            link_sent=True,
        )
        metrics.links_sent.inc()
        return Outcome("link_sent", igsid)

    if int(conversation["nudges_sent"]) >= cfg.max_nudges:
        # Out of reminders. The conversation stays open and the next message
        # still gets a follow check, it just stops being told about it.
        return Outcome("skipped", "nudge cap reached")

    try:
        await graph.send_message(
            ig_user_id=ig_user_id,
            token=account["access_token"],
            igsid=igsid,
            text=copy.NUDGE,
        )
    except GraphError as exc:
        log.warning("Nudge to %s failed: %s", igsid, exc)
        metrics.graph_errors.inc()
        return Outcome("failed", str(exc))

    await db.update_conversation(
        conn,
        igsid=igsid,
        ig_user_id=ig_user_id,
        state=db.STATE_AWAITING_FOLLOW,
        bump_nudges=True,
    )
    metrics.nudges_sent.inc()
    return Outcome("nudged", igsid)
