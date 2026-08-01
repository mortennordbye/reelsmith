"""SQLite, one file, no ORM.

At this scale the honest storage is a single file on a PVC. Postgres is the
migration path the day this needs a second replica, and nothing here makes that
hard: the queries are plain SQL and the only SQLite-specific thing is the
`user_version` pragma used for migrations.

Two decisions worth knowing about:

- **Times are ISO 8601 strings in UTC**, not SQLite timestamps. They survive a
  dump, they sort correctly as text, and they come back as the same string that
  went in, which matters when the thing you are debugging is a 24 hour window.
- **`comments_handled` is a claim table, not a log.** A row appears *before*
  the private reply is sent. See `conversations.claim_comment` for why that
  direction is the safe one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

# One statement block per version. To change the schema, append a new entry and
# bump SCHEMA_VERSION; never edit an entry that has shipped.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE accounts (
        ig_user_id        TEXT PRIMARY KEY,
        username          TEXT NOT NULL DEFAULT '',
        access_token      TEXT NOT NULL,
        token_expires_at  TEXT,
        token_refreshed_at TEXT,
        -- active gates the poller. dm_enabled gates every outbound message and
        -- is the kill switch: flipping it stops the account talking without
        -- touching the webhook subscription, so nothing has to be re-approved
        -- to turn it back on.
        active            INTEGER NOT NULL DEFAULT 1,
        dm_enabled        INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL
    );

    CREATE TABLE posts (
        media_id      TEXT PRIMARY KEY,
        ig_user_id    TEXT NOT NULL,
        keyword       TEXT NOT NULL,
        link          TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        last_polled_at TEXT
    );
    CREATE INDEX posts_by_account ON posts (ig_user_id, registered_at);

    CREATE TABLE comments_handled (
        comment_id  TEXT PRIMARY KEY,
        media_id    TEXT NOT NULL,
        ig_user_id  TEXT NOT NULL,
        igsid       TEXT,
        claimed_at  TEXT NOT NULL,
        replied_at  TEXT,
        failure     TEXT
    );

    CREATE TABLE conversations (
        igsid           TEXT NOT NULL,
        ig_user_id      TEXT NOT NULL,
        media_id        TEXT,
        state           TEXT NOT NULL,
        last_inbound_at TEXT,
        link_sent_at    TEXT,
        nudges_sent     INTEGER NOT NULL DEFAULT 0,
        follow_checks   INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (igsid, ig_user_id)
    );
    """,
    # v2. Conversion was a property of the person, so `converted` was a dead
    # end: a returning commenter got the private reply and then silence,
    # because one conversation row per person cannot represent "converted on
    # post 1, still waiting on post 2". Which failed the most engaged part of
    # the audience while every counter looked healthy.
    #
    # A delivery is now per person and per post. `conversations` keeps only the
    # live state.
    """
    CREATE TABLE deliveries (
        igsid      TEXT NOT NULL,
        ig_user_id TEXT NOT NULL,
        media_id   TEXT NOT NULL,
        sent_at    TEXT NOT NULL,
        PRIMARY KEY (igsid, ig_user_id, media_id)
    );

    -- Backfill, so nobody who already has a link is sent it a second time the
    -- moment this ships.
    INSERT OR IGNORE INTO deliveries (igsid, ig_user_id, media_id, sent_at)
    SELECT igsid, ig_user_id, media_id, COALESCE(link_sent_at, updated_at)
    FROM conversations
    WHERE link_sent_at IS NOT NULL AND media_id IS NOT NULL;
    """,
    # v3. Meta's one-reply-per-comment rule is per comment, not per person, so
    # someone who writes "SEND" and then "send pls" on the same Reel was owed
    # two private replies and got two identical DMs. The commenter's id lets
    # the second one be declined.
    #
    # Kept separate from the IGSID on the same row on purpose: they are
    # different id spaces. `author_id` is who wrote the comment,
    # `igsid` is who the DM went to, and only Meta knows they are the same
    # person.
    """
    ALTER TABLE comments_handled ADD COLUMN author_id TEXT;
    CREATE INDEX comments_by_author ON comments_handled (media_id, author_id);
    """,
    # v4. The scheduled queue. The Mac renders a batch and pushes it here; this
    # service publishes them on a schedule, so the laptop is only needed while
    # rendering.
    #
    # `queued_posts.state` is a claim, in the same sense `comments_handled` is:
    # the move to `claimed` is committed before the first Graph call, so a crash
    # mid-publish leaves a row that is visibly stuck rather than one a retry
    # turns into a second identical Reel.
    #
    # `slot_fires` is the other half of that. A slot fires at most once per
    # local date, and the primary key is what enforces it across restarts,
    # concurrent ticks and a clock that steps backwards.
    """
    CREATE TABLE queued_posts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ig_user_id     TEXT NOT NULL,
        state          TEXT NOT NULL,
        -- Filenames under covers_dir rather than URLs: the public base URL can
        -- change (it did once already) and a stored absolute URL would rot.
        video_name     TEXT NOT NULL,
        cover_name     TEXT,
        caption        TEXT NOT NULL DEFAULT '',
        repo_full_name TEXT,
        keyword        TEXT NOT NULL,
        link           TEXT NOT NULL,
        -- Set to pin one post to a wall-clock time instead of the next slot.
        slot_override  TEXT,
        position       INTEGER NOT NULL DEFAULT 0,
        container_id   TEXT,
        media_id       TEXT,
        permalink      TEXT,
        attempts       INTEGER NOT NULL DEFAULT 0,
        failure        TEXT,
        created_at     TEXT NOT NULL,
        published_at   TEXT
    );
    CREATE INDEX queued_by_state ON queued_posts (ig_user_id, state, position, id);

    CREATE TABLE schedule_slots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ig_user_id TEXT NOT NULL,
        -- Local wall-clock time plus an IANA zone, not a UTC offset. Storing
        -- the offset would move the slot by an hour every DST change.
        hour       INTEGER NOT NULL,
        minute     INTEGER NOT NULL,
        tz         TEXT NOT NULL DEFAULT 'UTC',
        -- Plus or minus this many minutes, so the account does not post at
        -- exactly the same second every day. Derived per date, never rolled.
        jitter_minutes INTEGER NOT NULL DEFAULT 0,
        -- Comma separated ISO weekdays (1=Monday). Empty means every day.
        days       TEXT NOT NULL DEFAULT '',
        active     INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE INDEX slots_by_account ON schedule_slots (ig_user_id, active);

    CREATE TABLE slot_fires (
        slot_id    INTEGER NOT NULL,
        local_date TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        queued_id  INTEGER,
        PRIMARY KEY (slot_id, local_date)
    );
    """,
    # v5. Where a slot came from. Config-declared slots are replaced wholesale
    # on every boot, so the UI has to know not to offer edits that the next
    # rollout would silently undo.
    """
    ALTER TABLE schedule_slots ADD COLUMN source TEXT NOT NULL DEFAULT 'ui';
    """,
)

# Conversation states. `replied` means the private reply went out and we are
# waiting for the commenter to open the messaging window by saying anything at
# all; only then does Meta let us read whether they follow.
STATE_REPLIED = "replied"
STATE_AWAITING_FOLLOW = "awaiting_follow"
STATE_CONVERTED = "converted"

# Queue states.
#
# `draft` is the default on arrival and never publishes. Arming is a separate
# act because the failure mode of the other default is a bad video posting
# itself while nobody is watching.
#
# `claimed` is held only for the length of one publish attempt. A row sitting in
# it means the process died mid-publish, and it is deliberately not swept back
# to `approved` by anything automatic: Meta may well have accepted the post.
QUEUE_DRAFT = "draft"
QUEUE_APPROVED = "approved"
QUEUE_CLAIMED = "claimed"
QUEUE_PUBLISHED = "published"
QUEUE_FAILED = "failed"
QUEUE_CANCELLED = "cancelled"

# The states whose media files must survive the retention sweep, and which the
# admin UI shows as still in the line.
QUEUE_LIVE_STATES = (QUEUE_DRAFT, QUEUE_APPROVED, QUEUE_CLAIMED, QUEUE_FAILED)


def now() -> datetime:
    return datetime.now(UTC)


def iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def connect(path: Path) -> aiosqlite.Connection:
    """Open the database and bring the schema up to date."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    # WAL so the poller writing does not block the webhook reading. Foreign keys
    # stay off deliberately: a post can outlive the account row being edited by
    # hand, and losing a comment to a constraint is worse than an orphan row.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await migrate(conn)
    return conn


async def migrate(conn: aiosqlite.Connection) -> int:
    """Apply every migration the file has not seen. Returns the new version."""
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    version = int(row[0]) if row else 0

    for index in range(version, len(_MIGRATIONS)):
        await conn.executescript(_MIGRATIONS[index])
        log.info("Applied gateway schema migration %d", index + 1)

    if version < len(_MIGRATIONS):
        # Pragmas take no parameters, and the value is an int from a range, so
        # there is nothing here for an injection to attach to.
        await conn.execute(f"PRAGMA user_version={len(_MIGRATIONS)}")
        await conn.commit()
    return len(_MIGRATIONS)


@asynccontextmanager
async def open_db(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(path)
    try:
        yield conn
    finally:
        await conn.close()


async def _all(conn: aiosqlite.Connection, sql: str, args: Iterable[Any] = ()) -> list[Any]:
    async with conn.execute(sql, tuple(args)) as cur:
        return list(await cur.fetchall())


async def _one(conn: aiosqlite.Connection, sql: str, args: Iterable[Any] = ()) -> Any | None:
    async with conn.execute(sql, tuple(args)) as cur:
        return await cur.fetchone()


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


async def upsert_account(
    conn: aiosqlite.Connection,
    *,
    ig_user_id: str,
    access_token: str,
    username: str = "",
    expires_at: datetime | None = None,
) -> None:
    """Add or re-authorise an account, preserving its switches.

    Re-running OAuth must not silently re-enable an account someone paused, so
    `active` and `dm_enabled` are left alone on conflict.
    """
    stamp = iso(now())
    await conn.execute(
        """
        INSERT INTO accounts (ig_user_id, username, access_token, token_expires_at,
                              token_refreshed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ig_user_id) DO UPDATE SET
            username = excluded.username,
            access_token = excluded.access_token,
            token_expires_at = excluded.token_expires_at,
            token_refreshed_at = excluded.token_refreshed_at
        """,
        (ig_user_id, username, access_token, iso(expires_at), stamp, stamp),
    )
    await conn.commit()


async def get_account(conn: aiosqlite.Connection, ig_user_id: str) -> Mapping[str, Any] | None:
    return await _one(conn, "SELECT * FROM accounts WHERE ig_user_id = ?", (ig_user_id,))


async def active_accounts(conn: aiosqlite.Connection) -> list[Any]:
    return await _all(conn, "SELECT * FROM accounts WHERE active = 1")


async def all_accounts(conn: aiosqlite.Connection) -> list[Any]:
    return await _all(conn, "SELECT * FROM accounts ORDER BY created_at")


async def set_account_flags(
    conn: aiosqlite.Connection,
    ig_user_id: str,
    *,
    active: bool | None = None,
    dm_enabled: bool | None = None,
) -> None:
    sets, args = [], []
    if active is not None:
        sets.append("active = ?")
        args.append(int(active))
    if dm_enabled is not None:
        sets.append("dm_enabled = ?")
        args.append(int(dm_enabled))
    if not sets:
        return
    args.append(ig_user_id)
    await conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE ig_user_id = ?", args)
    await conn.commit()


async def save_account_token(
    conn: aiosqlite.Connection,
    ig_user_id: str,
    token: str,
    expires_in_s: int | None,
) -> None:
    moment = now()
    expires = moment + timedelta(seconds=expires_in_s) if expires_in_s else None
    await conn.execute(
        """
        UPDATE accounts SET access_token = ?, token_expires_at = ?, token_refreshed_at = ?
        WHERE ig_user_id = ?
        """,
        (token, iso(expires), iso(moment), ig_user_id),
    )
    await conn.commit()


# --------------------------------------------------------------------------
# Posts
# --------------------------------------------------------------------------


async def register_post(
    conn: aiosqlite.Connection,
    *,
    media_id: str,
    ig_user_id: str,
    keyword: str,
    link: str,
) -> None:
    """Record a published Reel so the poller starts watching its comments.

    Re-registering updates the keyword and link, which is how a typo in either
    gets fixed after the post is already live.
    """
    await conn.execute(
        """
        INSERT INTO posts (media_id, ig_user_id, keyword, link, registered_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(media_id) DO UPDATE SET
            keyword = excluded.keyword,
            link = excluded.link
        """,
        (media_id, ig_user_id, keyword, link, iso(now())),
    )
    await conn.commit()


async def get_post(conn: aiosqlite.Connection, media_id: str) -> Mapping[str, Any] | None:
    return await _one(conn, "SELECT * FROM posts WHERE media_id = ?", (media_id,))


async def pollable_posts(
    conn: aiosqlite.Connection, ig_user_id: str, *, ttl_days: int
) -> list[Any]:
    """Posts still inside the seven day private reply window."""
    cutoff = iso(now() - timedelta(days=ttl_days))
    return await _all(
        conn,
        "SELECT * FROM posts WHERE ig_user_id = ? AND registered_at >= ? ORDER BY registered_at",
        (ig_user_id, cutoff),
    )


async def mark_polled(conn: aiosqlite.Connection, media_id: str) -> None:
    await conn.execute(
        "UPDATE posts SET last_polled_at = ? WHERE media_id = ?", (iso(now()), media_id)
    )
    await conn.commit()


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------


async def already_replied_to_author(
    conn: aiosqlite.Connection, *, media_id: str, author_id: str
) -> bool:
    """Has this person already had a private reply on this post?

    Meta's one-per-comment rule does not stop two comments by the same person
    producing two identical DMs, which reads as a broken bot rather than a
    prompt one.
    """
    row = await _one(
        conn,
        """
        SELECT 1 FROM comments_handled
        WHERE media_id = ? AND author_id = ? AND replied_at IS NOT NULL
        LIMIT 1
        """,
        (media_id, author_id),
    )
    return row is not None


async def claim_comment(
    conn: aiosqlite.Connection,
    *,
    comment_id: str,
    media_id: str,
    ig_user_id: str,
    author_id: str | None = None,
) -> bool:
    """Take exclusive ownership of a comment. True means this caller may reply.

    Meta allows exactly one private reply per comment, ever. The primary key
    does the enforcing: a second caller, a second poll cycle, or a restart
    mid-send all lose the insert and back off.
    """
    async with conn.execute(
        """
        INSERT OR IGNORE INTO comments_handled
            (comment_id, media_id, ig_user_id, author_id, claimed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (comment_id, media_id, ig_user_id, author_id, iso(now())),
    ) as cur:
        won = cur.rowcount == 1
    await conn.commit()
    return won


async def mark_comment_replied(
    conn: aiosqlite.Connection, comment_id: str, *, igsid: str | None
) -> None:
    await conn.execute(
        "UPDATE comments_handled SET replied_at = ?, igsid = ? WHERE comment_id = ?",
        (iso(now()), igsid, comment_id),
    )
    await conn.commit()


async def mark_comment_failed(conn: aiosqlite.Connection, comment_id: str, reason: str) -> None:
    """Record why a claimed comment never got its reply.

    The claim is deliberately not released. Meta may well have accepted the send
    before the failure surfaced, and a duplicate private reply is worse than a
    missing one.
    """
    await conn.execute(
        "UPDATE comments_handled SET failure = ? WHERE comment_id = ?", (reason[:500], comment_id)
    )
    await conn.commit()


async def comment_row(conn: aiosqlite.Connection, comment_id: str) -> Mapping[str, Any] | None:
    return await _one(conn, "SELECT * FROM comments_handled WHERE comment_id = ?", (comment_id,))


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------


async def start_conversation(
    conn: aiosqlite.Connection, *, igsid: str, ig_user_id: str, media_id: str
) -> None:
    """Open, or reopen, a conversation at private reply time.

    The send response carries the commenter's IGSID, which is the only moment
    the link they asked for can be tied to the person who will answer.

    A new comment always reopens. This used to refuse to touch a `converted`
    row, on the theory that a repeat commenter should not be walked back through
    the funnel, which sounded reasonable and was wrong: it meant someone who
    converted on one post could never receive any later post's link. The state
    is about the current ask, not about the person's history, and `deliveries`
    is what stops a link being sent twice.

    The nudge count resets too. It bounds reminders for one ask, not for a
    lifetime.
    """
    stamp = iso(now())
    await conn.execute(
        """
        INSERT INTO conversations (igsid, ig_user_id, media_id, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(igsid, ig_user_id) DO UPDATE SET
            media_id = excluded.media_id,
            state = excluded.state,
            nudges_sent = 0,
            updated_at = excluded.updated_at
        """,
        (igsid, ig_user_id, media_id, STATE_REPLIED, stamp, stamp),
    )
    await conn.commit()


async def record_delivery(
    conn: aiosqlite.Connection, *, igsid: str, ig_user_id: str, media_id: str
) -> None:
    """Remember that this person has this post's link, so it is never resent."""
    await conn.execute(
        """
        INSERT OR IGNORE INTO deliveries (igsid, ig_user_id, media_id, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (igsid, ig_user_id, media_id, iso(now())),
    )
    await conn.commit()


async def pending_ask(
    conn: aiosqlite.Connection, *, igsid: str, ig_user_id: str
) -> str | None:
    """The most recent post this person asked about and has not been sent.

    An "ask" is a comment we sent a private reply to. That is what separates
    someone waiting for a link from someone just talking, and answering the
    second with "you need to follow" would be obnoxious.

    Most recent first, because if several are outstanding the one they just
    commented on is the one they mean.
    """
    row = await _one(
        conn,
        """
        SELECT c.media_id
        FROM comments_handled c
        WHERE c.igsid = ?
          AND c.ig_user_id = ?
          AND c.replied_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM deliveries d
              WHERE d.igsid = c.igsid
                AND d.ig_user_id = c.ig_user_id
                AND d.media_id = c.media_id
          )
        ORDER BY c.claimed_at DESC
        LIMIT 1
        """,
        (igsid, ig_user_id),
    )
    return row[0] if row else None


async def get_conversation(
    conn: aiosqlite.Connection, *, igsid: str, ig_user_id: str
) -> Mapping[str, Any] | None:
    return await _one(
        conn,
        "SELECT * FROM conversations WHERE igsid = ? AND ig_user_id = ?",
        (igsid, ig_user_id),
    )


async def record_inbound(conn: aiosqlite.Connection, *, igsid: str, ig_user_id: str) -> None:
    """Stamp the message that opens a fresh 24 hour window."""
    stamp = iso(now())
    await conn.execute(
        """
        UPDATE conversations SET last_inbound_at = ?, updated_at = ?
        WHERE igsid = ? AND ig_user_id = ?
        """,
        (stamp, stamp, igsid, ig_user_id),
    )
    await conn.commit()


async def update_conversation(
    conn: aiosqlite.Connection,
    *,
    igsid: str,
    ig_user_id: str,
    state: str | None = None,
    link_sent: bool = False,
    bump_nudges: bool = False,
    bump_follow_checks: bool = False,
) -> None:
    sets = ["updated_at = ?"]
    args: list[Any] = [iso(now())]
    if state:
        sets.append("state = ?")
        args.append(state)
    if link_sent:
        sets.append("link_sent_at = ?")
        args.append(iso(now()))
    if bump_nudges:
        sets.append("nudges_sent = nudges_sent + 1")
    if bump_follow_checks:
        sets.append("follow_checks = follow_checks + 1")
    args.extend([igsid, ig_user_id])
    await conn.execute(
        f"UPDATE conversations SET {', '.join(sets)} WHERE igsid = ? AND ig_user_id = ?", args
    )
    await conn.commit()


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------


async def enqueue_post(
    conn: aiosqlite.Connection,
    *,
    ig_user_id: str,
    video_name: str,
    cover_name: str | None,
    caption: str,
    keyword: str,
    link: str,
    repo_full_name: str | None = None,
    approved: bool = False,
    slot_override: datetime | None = None,
) -> int:
    """Put a rendered Reel in the line. Returns its queue id."""
    row = await _one(
        conn, "SELECT COALESCE(MAX(position), 0) + 1 FROM queued_posts WHERE ig_user_id = ?",
        (ig_user_id,),
    )
    position = int(row[0]) if row else 1
    async with conn.execute(
        """
        INSERT INTO queued_posts
            (ig_user_id, state, video_name, cover_name, caption, repo_full_name,
             keyword, link, slot_override, position, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ig_user_id,
            QUEUE_APPROVED if approved else QUEUE_DRAFT,
            video_name,
            cover_name,
            caption,
            repo_full_name,
            keyword,
            link,
            iso(slot_override),
            position,
            iso(now()),
        ),
    ) as cur:
        queued_id = int(cur.lastrowid or 0)
    await conn.commit()
    return queued_id


async def get_queued(conn: aiosqlite.Connection, queued_id: int) -> Mapping[str, Any] | None:
    return await _one(conn, "SELECT * FROM queued_posts WHERE id = ?", (queued_id,))


async def queued_posts(
    conn: aiosqlite.Connection,
    *,
    ig_user_id: str | None = None,
    states: Iterable[str] | None = None,
    limit: int = 200,
) -> list[Any]:
    where, args = [], []
    if ig_user_id:
        where.append("ig_user_id = ?")
        args.append(ig_user_id)
    states = tuple(states) if states else ()
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.append(limit)
    return await _all(
        conn,
        f"SELECT * FROM queued_posts {clause} ORDER BY position, id LIMIT ?",
        args,
    )


async def live_media_names(conn: aiosqlite.Connection) -> set[str]:
    """Every file the queue still needs.

    The retention sweep in `api._prune_media` deletes by age, which on its own
    would eat the back of a ten-post queue three days before its turn and then
    fail the publish with a 404 from Meta's fetcher. This is the exemption list.
    """
    rows = await _all(
        conn,
        f"""
        SELECT video_name, cover_name FROM queued_posts
        WHERE state IN ({','.join('?' * len(QUEUE_LIVE_STATES))})
        """,
        QUEUE_LIVE_STATES,
    )
    names: set[str] = set()
    for row in rows:
        names.update(name for name in (row["video_name"], row["cover_name"]) if name)
    return names


async def set_queue_state(
    conn: aiosqlite.Connection,
    queued_id: int,
    state: str,
    *,
    failure: str | None = None,
    bump_attempts: bool = False,
    reset_attempts: bool = False,
) -> None:
    sets = ["state = ?"]
    args: list[Any] = [state]
    # Cleared rather than left behind, so a row that failed and was re-approved
    # does not still show yesterday's reason in the UI.
    sets.append("failure = ?")
    args.append(failure[:500] if failure else None)
    if bump_attempts:
        sets.append("attempts = attempts + 1")
    if reset_attempts:
        # A person who looked at the failure and chose to retry is a better
        # signal than the counter that gave up, so they get a fresh budget.
        sets.append("attempts = 0")
    args.append(queued_id)
    await conn.execute(f"UPDATE queued_posts SET {', '.join(sets)} WHERE id = ?", args)
    await conn.commit()


async def claim_queued(conn: aiosqlite.Connection, queued_id: int) -> bool:
    """Move an approved post to `claimed`. True means this caller may publish.

    The `state = approved` predicate is the whole point: it is a compare and
    swap, so two ticks racing for the same row produce one publish and one
    caller that quietly does nothing.
    """
    async with conn.execute(
        "UPDATE queued_posts SET state = ?, attempts = attempts + 1 WHERE id = ? AND state = ?",
        (QUEUE_CLAIMED, queued_id, QUEUE_APPROVED),
    ) as cur:
        won = cur.rowcount == 1
    await conn.commit()
    return won


async def next_approved(
    conn: aiosqlite.Connection, ig_user_id: str, *, before: datetime | None = None
) -> Mapping[str, Any] | None:
    """The post a due slot should take.

    A pinned post wins if its time has come, because pinning is an explicit
    instruction and the queue order is only a default. Otherwise it is the head
    of the line.
    """
    moment = iso(before or now())
    pinned = await _one(
        conn,
        """
        SELECT * FROM queued_posts
        WHERE ig_user_id = ? AND state = ? AND slot_override IS NOT NULL
          AND slot_override <= ?
        ORDER BY slot_override
        LIMIT 1
        """,
        (ig_user_id, QUEUE_APPROVED, moment),
    )
    if pinned is not None:
        return pinned
    return await _one(
        conn,
        """
        SELECT * FROM queued_posts
        WHERE ig_user_id = ? AND state = ? AND slot_override IS NULL
        ORDER BY position, id
        LIMIT 1
        """,
        (ig_user_id, QUEUE_APPROVED),
    )


async def mark_queue_published(
    conn: aiosqlite.Connection, queued_id: int, *, media_id: str, permalink: str | None
) -> None:
    await conn.execute(
        """
        UPDATE queued_posts
        SET state = ?, media_id = ?, permalink = ?, published_at = ?, failure = NULL
        WHERE id = ?
        """,
        (QUEUE_PUBLISHED, media_id, permalink, iso(now()), queued_id),
    )
    await conn.commit()


async def set_container(conn: aiosqlite.Connection, queued_id: int, container_id: str) -> None:
    """Record the container before waiting on it.

    This is what tells a later attempt whether Meta was ever asked to make
    something. A failure with no container id here is safe to retry; one with a
    container id is not, because the publish may have landed.
    """
    await conn.execute(
        "UPDATE queued_posts SET container_id = ? WHERE id = ?", (container_id, queued_id)
    )
    await conn.commit()


async def update_queued(
    conn: aiosqlite.Connection,
    queued_id: int,
    *,
    caption: str | None = None,
    keyword: str | None = None,
    link: str | None = None,
    position: int | None = None,
    slot_override: datetime | None = None,
    clear_override: bool = False,
) -> None:
    sets, args = [], []
    for column, value in (
        ("caption", caption), ("keyword", keyword), ("link", link), ("position", position)
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    if clear_override:
        sets.append("slot_override = NULL")
    elif slot_override is not None:
        sets.append("slot_override = ?")
        args.append(iso(slot_override))
    if not sets:
        return
    args.append(queued_id)
    await conn.execute(f"UPDATE queued_posts SET {', '.join(sets)} WHERE id = ?", args)
    await conn.commit()


async def queue_depth(conn: aiosqlite.Connection, ig_user_id: str | None = None) -> dict[str, int]:
    where, args = ("WHERE ig_user_id = ?", (ig_user_id,)) if ig_user_id else ("", ())
    rows = await _all(
        conn, f"SELECT state, COUNT(*) FROM queued_posts {where} GROUP BY state", args
    )
    return {str(row[0]): int(row[1]) for row in rows}


# --------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------


SLOT_SOURCE_UI = "ui"
SLOT_SOURCE_CONFIG = "config"


async def add_slot(
    conn: aiosqlite.Connection,
    *,
    ig_user_id: str,
    hour: int,
    minute: int,
    tz: str = "UTC",
    jitter_minutes: int = 0,
    days: str = "",
    source: str = SLOT_SOURCE_UI,
) -> int:
    async with conn.execute(
        """
        INSERT INTO schedule_slots
            (ig_user_id, hour, minute, tz, jitter_minutes, days, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ig_user_id, hour, minute, tz, jitter_minutes, days, source, iso(now())),
    ) as cur:
        slot_id = int(cur.lastrowid or 0)
    await conn.commit()
    return slot_id


async def sync_config_slots(
    conn: aiosqlite.Connection, ig_user_id: str, specs: Iterable[Any]
) -> int:
    """Make the config-declared slots for one account match the config exactly.

    Replace rather than merge, because the config file is the truth for these
    and a slot deleted from it has to disappear. Slots added in the UI carry a
    different source and are left alone.

    A slot's id is what seeds its jitter, so rewriting the rows every boot
    would reshuffle the offsets on every restart, which is precisely the
    instability the derived jitter exists to avoid. The id is therefore kept
    for any slot whose definition has not changed.
    """
    specs = list(specs)
    existing = await _all(
        conn,
        "SELECT * FROM schedule_slots WHERE ig_user_id = ? AND source = ?",
        (ig_user_id, SLOT_SOURCE_CONFIG),
    )

    def shape(row: Any) -> tuple:
        return (
            int(row["hour"]), int(row["minute"]), str(row["tz"]),
            int(row["jitter_minutes"]), str(row["days"] or ""),
        )

    wanted = {
        (s.hour, s.minute, s.tz, s.jitter_minutes, s.days): s for s in specs
    }
    keep = {shape(row) for row in existing} & set(wanted)

    for row in existing:
        if shape(row) not in keep:
            await conn.execute("DELETE FROM schedule_slots WHERE id = ?", (row["id"],))
    for key, spec in wanted.items():
        if key in keep:
            continue
        await add_slot(
            conn,
            ig_user_id=ig_user_id,
            hour=spec.hour,
            minute=spec.minute,
            tz=spec.tz,
            jitter_minutes=spec.jitter_minutes,
            days=spec.days,
            source=SLOT_SOURCE_CONFIG,
        )
    await conn.commit()
    return len(specs)


async def active_slots(conn: aiosqlite.Connection, ig_user_id: str | None = None) -> list[Any]:
    where, args = ("AND ig_user_id = ?", (ig_user_id,)) if ig_user_id else ("", ())
    return await _all(
        conn, f"SELECT * FROM schedule_slots WHERE active = 1 {where} ORDER BY hour, minute", args
    )


async def all_slots(conn: aiosqlite.Connection, ig_user_id: str | None = None) -> list[Any]:
    where, args = ("WHERE ig_user_id = ?", (ig_user_id,)) if ig_user_id else ("", ())
    return await _all(
        conn, f"SELECT * FROM schedule_slots {where} ORDER BY hour, minute", args
    )


async def set_slot_active(conn: aiosqlite.Connection, slot_id: int, active: bool) -> None:
    await conn.execute(
        "UPDATE schedule_slots SET active = ? WHERE id = ?", (int(active), slot_id)
    )
    await conn.commit()


async def delete_slot(conn: aiosqlite.Connection, slot_id: int) -> None:
    await conn.execute("DELETE FROM schedule_slots WHERE id = ?", (slot_id,))
    await conn.commit()


async def claim_slot_fire(conn: aiosqlite.Connection, *, slot_id: int, local_date: str) -> bool:
    """Take this slot's turn for this local date. True means it is ours.

    Once claimed it stays claimed, including when the publish that follows
    fails. That is the same trade `claim_comment` makes, for the same reason:
    the alternative to one missed post is an unknown number of duplicate ones.
    The scheduler releases it explicitly in the one case that is provably safe.
    """
    async with conn.execute(
        "INSERT OR IGNORE INTO slot_fires (slot_id, local_date, claimed_at) VALUES (?, ?, ?)",
        (slot_id, local_date, iso(now())),
    ) as cur:
        won = cur.rowcount == 1
    await conn.commit()
    return won


async def release_slot_fire(conn: aiosqlite.Connection, *, slot_id: int, local_date: str) -> None:
    """Give the slot its turn back. Only safe before a container exists."""
    await conn.execute(
        "DELETE FROM slot_fires WHERE slot_id = ? AND local_date = ?", (slot_id, local_date)
    )
    await conn.commit()


async def attach_fire(
    conn: aiosqlite.Connection, *, slot_id: int, local_date: str, queued_id: int
) -> None:
    await conn.execute(
        "UPDATE slot_fires SET queued_id = ? WHERE slot_id = ? AND local_date = ?",
        (queued_id, slot_id, local_date),
    )
    await conn.commit()


async def funnel(conn: aiosqlite.Connection, ig_user_id: str | None = None) -> dict[str, int]:
    """The five numbers the whole mechanic exists to move."""
    where, args = ("WHERE ig_user_id = ?", (ig_user_id,)) if ig_user_id else ("", ())
    counts: dict[str, int] = {}
    for name, sql in (
        ("comments_seen", f"SELECT COUNT(*) FROM comments_handled {where}"),
        (
            "private_replies",
            f"SELECT COUNT(*) FROM comments_handled {where}{' AND' if where else 'WHERE'}"
            " replied_at IS NOT NULL",
        ),
        (
            "replied_in_dm",
            f"SELECT COUNT(*) FROM conversations {where}{' AND' if where else 'WHERE'}"
            " last_inbound_at IS NOT NULL",
        ),
        (
            "links_sent",
            f"SELECT COUNT(*) FROM conversations {where}{' AND' if where else 'WHERE'}"
            " link_sent_at IS NOT NULL",
        ),
    ):
        row = await _one(conn, sql, args)
        counts[name] = int(row[0]) if row else 0
    return counts
