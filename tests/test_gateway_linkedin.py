"""The scheduler putting a queued row on a personal LinkedIn profile.

The first destination that is not video, so most of this is about the shapes
a text row can take and about the retry line. The thumbnail upload creates
nothing anybody can see and is retried; `POST /rest/posts` is the commitment,
and anything that goes wrong after it has been sent stops the row, because a
second copy of a post under somebody's own name cannot be undone quietly.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from gateway import db, linkedin, panel, scheduler
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import settings

PERSON = "abc123XYZ"
BLOG = "https://blog.nordbye.it/blog/headroom/"
POST_URN = "urn:li:share:7380000000000000001"
IMAGE_URN = "urn:li:image:C4E10AQFoyyAjHPMQuQ"
UPLOAD_URL = "https://www.linkedin.com/dms-uploads/C4E10AQ/uploaded-image/0"


class FakeLinkedIn:
    """Answers the three calls the publisher makes, and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.posts: list[dict] = []
        self.uploads: list[bytes] = []
        self.post_status = 201
        self.post_raises: Exception | None = None
        self.init_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if "initializeUpload" in url:
            if self.init_status >= 400:
                return httpx.Response(self.init_status, json={"message": "nope"})
            return httpx.Response(
                200, json={"value": {"uploadUrl": UPLOAD_URL, "image": IMAGE_URN}}
            )
        if url == UPLOAD_URL:
            self.uploads.append(request.content)
            return httpx.Response(201)
        if url.endswith("/rest/posts"):
            if self.post_raises is not None:
                raise self.post_raises
            self.posts.append(json.loads(request.content))
            if self.post_status >= 400:
                return httpx.Response(self.post_status, json={"message": "refused"})
            return httpx.Response(201, headers={"x-restli-id": POST_URN})
        return httpx.Response(404)


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def li():
    return FakeLinkedIn()


@pytest.fixture
async def graph(li, cfg):
    async with httpx.AsyncClient(transport=httpx.MockTransport(li.handler)) as client:
        yield GraphClient(client, cfg)


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(
        connection,
        account_id=PERSON,
        access_token="li-token",
        username="Somebody",
        expires_at=db.now() + timedelta(days=50),
        platform=db.PLATFORM_LINKEDIN,
        brand="me",
    )
    yield connection
    await connection.close()


async def _queue(conn, **overrides) -> int:
    fields = {
        "account_id": PERSON,
        "video_name": "",
        "cover_name": None,
        "caption": "We keep saying school should teach finance (it never did).\n\n#Finance",
        "keyword": "send",
        "link": "",
        "approved": True,
        **overrides,
    }
    queued_id = await db.enqueue_post(conn, **fields)
    await db.claim_queued(conn, queued_id)
    return queued_id


async def _publish(conn, graph, cfg, metrics, queued_id):
    account = await db.get_account(conn, PERSON)
    queued = await db.get_queued(conn, queued_id)
    return await scheduler.publish_queued(
        conn, graph, cfg, metrics, account=account, queued=queued
    )


def _thumbnail(cfg, name="thumb.png") -> str:
    cfg.covers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.covers_dir / name).write_bytes(b"\x89PNG fake")
    return name


# --- The shapes ---------------------------------------------------------------


async def test_a_text_post_is_published_and_recorded(conn, graph, cfg, metrics, li):
    queued_id = await _queue(conn)

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (True, False)

    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_PUBLISHED
    assert row["media_id"] == POST_URN
    assert row["container_id"] == POST_URN
    assert row["permalink"] == f"https://www.linkedin.com/feed/update/{POST_URN}/"
    body = li.posts[0]
    assert body["author"] == f"urn:li:person:{PERSON}"
    assert "content" not in body


async def test_markup_characters_are_escaped_and_hashtags_are_not(
    conn, graph, cfg, metrics, li
):
    """An unescaped parenthesis in LinkedIn's little format can swallow the
    rest of the post, which nobody sees until it is live and half missing."""
    await _publish(conn, graph, cfg, metrics, await _queue(conn))

    commentary = li.posts[0]["commentary"]
    assert "\\(it never did\\)" in commentary
    assert commentary.endswith("#Finance")


async def test_a_link_makes_an_article_card_with_the_uploaded_thumbnail(
    conn, graph, cfg, metrics, li
):
    """The Posts API does not scrape a link card, so the post carries its own
    title and thumbnail."""
    queued_id = await _queue(
        conn, link=BLOG, title="Headroom", cover_name=_thumbnail(cfg)
    )

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (True, False)

    assert li.uploads == [b"\x89PNG fake"]
    assert li.posts[0]["content"] == {
        "article": {"source": BLOG, "title": "Headroom", "thumbnail": IMAGE_URN}
    }


async def test_an_image_without_a_link_is_an_image_post(conn, graph, cfg, metrics, li):
    await _publish(conn, graph, cfg, metrics, await _queue(conn, cover_name=_thumbnail(cfg)))

    assert li.posts[0]["content"] == {"media": {"id": IMAGE_URN}}


# --- The retry line -----------------------------------------------------------


async def test_a_failed_thumbnail_upload_is_retried(conn, graph, cfg, metrics, li):
    """Nothing visible exists yet, so the slot gets its turn back."""
    li.init_status = 500
    queued_id = await _queue(conn, cover_name=_thumbnail(cfg))

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (False, True)

    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_APPROVED
    assert li.posts == []


async def test_a_refused_post_body_is_retried(conn, graph, cfg, metrics, li):
    li.post_status = 422
    queued_id = await _queue(conn)

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (False, True)
    assert (await db.get_queued(conn, queued_id))["state"] == db.QUEUE_APPROVED


@pytest.mark.parametrize(
    "trouble",
    [
        {"post_status": 503},
        {"post_raises": httpx.ReadTimeout("slow")},
    ],
)
async def test_a_post_that_may_exist_stops_the_row(conn, graph, cfg, metrics, li, trouble):
    """A 5xx or a timeout after sending does not say the post was not made."""
    for key, value in trouble.items():
        setattr(li, key, value)
    queued_id = await _queue(conn)

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (False, False)

    row = await db.get_queued(conn, queued_id)
    assert row["state"] == db.QUEUE_FAILED
    assert "may have the post" in row["failure"]


async def test_an_expired_token_fails_with_the_fix(conn, graph, cfg, metrics, li):
    """No refresh token exists, so waiting cannot help and the row says why."""
    await conn.execute(
        "UPDATE accounts SET token_expires_at = ? WHERE account_id = ?",
        (db.iso(db.now() - timedelta(days=1)), PERSON),
    )
    await conn.commit()
    queued_id = await _queue(conn)

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (False, False)
    assert "authorise.py linkedin" in (await db.get_queued(conn, queued_id))["failure"]
    assert li.calls == []


async def test_text_past_linkedins_limit_fails_before_any_call(
    conn, graph, cfg, metrics, li
):
    queued_id = await _queue(conn, caption="x" * (linkedin.MAX_COMMENTARY + 1))

    assert await _publish(conn, graph, cfg, metrics, queued_id) == (False, False)
    assert li.calls == []


# --- The panel ----------------------------------------------------------------


def test_the_panel_does_not_claim_the_gateway_refreshes_a_linkedin_token():
    caps = panel.caps(db.PLATFORM_LINKEDIN)
    assert caps.token_clock == "consent"
    assert not caps.dm


def test_a_lapsing_linkedin_token_says_to_walk_the_trip_again():
    dest = panel.Destination(
        row={"platform": db.PLATFORM_LINKEDIN, "account_id": PERSON}, brand="me",
        token_days=10.0,
    )
    issue = panel._token_issue(dest, cfg=None)
    assert issue is not None
    assert "authorise.py linkedin" in issue.rest
    assert "refreshes" not in issue.rest
