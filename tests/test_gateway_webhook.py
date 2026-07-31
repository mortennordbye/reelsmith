"""The webhook route, signature first.

The signature check is the only thing standing between a public URL and anyone
who can POST to it, so it gets tested harder than the happy path does.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway import db, webhook
from gateway.app import create_app
from tests.gateway_harness import (
    ACCOUNT,
    APP_SECRET,
    IGSID,
    VERIFY_TOKEN,
    FakeMeta,
    message_event,
    settings,
)

LINK = "https://github.com/DietrichGebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
async def client(cfg, meta):
    """The app, started for real, with Meta faked and the pollers off.

    Background loops are off because a sweep landing in the middle of an
    assertion is a flaky test with a very plausible-looking cause.
    """
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        # ASGITransport does not run the lifespan, and the lifespan is what
        # opens the database, so it is entered by hand.
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as http,
        ):
            yield http, app


async def seed(app) -> None:
    """A conversation as it really arises: a comment, a private reply, then a
    row keyed to the person who will answer.

    The claimed and replied comment is not decoration. An inbound message is
    only answered when there is an outstanding ask behind it, which is what
    keeps the follow gate from replying to someone who is just talking.
    """
    conn = app.state.db
    await db.upsert_account(conn, ig_user_id=ACCOUNT, access_token="tok")
    await db.register_post(
        conn, media_id="media-1", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    await db.claim_comment(
        conn, comment_id="c1", media_id="media-1", ig_user_id=ACCOUNT, author_id="commenter-1"
    )
    await db.mark_comment_replied(conn, "c1", igsid=IGSID)
    await db.start_conversation(conn, igsid=IGSID, ig_user_id=ACCOUNT, media_id="media-1")


# --- Signatures -------------------------------------------------------------


def test_a_signature_matches_only_the_exact_bytes():
    body = b'{"a": 1}'
    header = webhook.sign(body, APP_SECRET)

    assert webhook.verify_signature(body, header, APP_SECRET)
    # Re-serialising JSON changes the bytes, which is why the handler never
    # parses before it verifies.
    assert not webhook.verify_signature(b'{"a":1}', header, APP_SECRET)


@pytest.mark.parametrize(
    "header",
    [None, "", "sha256=", "deadbeef", "sha1=abc", "sha256=not-the-digest"],
)
def test_bad_signature_headers_are_all_rejected(header):
    assert not webhook.verify_signature(b"{}", header, APP_SECRET)


def test_an_empty_app_secret_never_verifies():
    # Otherwise a misconfigured deployment would accept everything rather than
    # nothing, which is the wrong way round to fail.
    body = b"{}"
    assert not webhook.verify_signature(body, webhook.sign(body, ""), "")


# --- The handshake ----------------------------------------------------------


async def test_the_handshake_echoes_the_challenge(client):
    http, _app = client
    response = await http.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_the_handshake_refuses_a_wrong_verify_token(client):
    http, _app = client
    response = await http.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )

    assert response.status_code == 403
    assert "x" not in response.text


# --- Delivery ---------------------------------------------------------------


async def test_an_unsigned_post_is_rejected_before_anything_happens(client, meta):
    http, app = client
    await seed(app)

    response = await http.post("/webhook", json=message_event())

    assert response.status_code == 403
    assert meta.calls == []
    registry = app.state.metrics.registry
    assert registry.get_sample_value("reelsmith_webhook_signature_failures_total") == 1


async def test_a_signed_message_runs_the_state_machine(client, meta):
    http, app = client
    await seed(app)
    body = json.dumps(message_event()).encode()

    response = await http.post(
        "/webhook",
        content=body,
        headers={
            "x-hub-signature-256": webhook.sign(body, APP_SECRET),
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200
    assert len(meta.sends) == 1  # the nudge, since the fake does not follow


async def test_the_accounts_own_echo_is_ignored(client, meta):
    http, app = client
    await seed(app)
    event = message_event()
    event["entry"][0]["messaging"][0]["message"]["is_echo"] = True
    body = json.dumps(event).encode()

    response = await http.post(
        "/webhook",
        content=body,
        headers={
            "x-hub-signature-256": webhook.sign(body, APP_SECRET),
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200
    assert meta.sends == []


async def test_a_handler_failure_still_answers_200(client, meta, caplog):
    """Meta never replays a missed event, so a 500 buys nothing.

    The comment poller is the safety net that makes this acceptable.
    """
    http, app = client
    await seed(app)
    meta.fail_sends_with = {"message": "nope", "code": 1}
    body = json.dumps(message_event()).encode()

    response = await http.post(
        "/webhook",
        content=body,
        headers={
            "x-hub-signature-256": webhook.sign(body, APP_SECRET),
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200


def test_a_batched_delivery_is_flattened():
    payload = {
        "entry": [
            {
                "id": ACCOUNT,
                "messaging": [
                    {"sender": {"id": "a"}, "recipient": {"id": ACCOUNT}, "message": {"text": "1"}},
                    {"sender": {"id": "b"}, "recipient": {"id": ACCOUNT}, "message": {"text": "2"}},
                ],
            },
            {
                "id": ACCOUNT,
                "messaging": [
                    {"sender": {"id": "c"}, "recipient": {"id": ACCOUNT}, "message": {"text": "3"}}
                ],
            },
        ]
    }

    assert [s for _r, s, _t in webhook.inbound_messages(payload)] == ["a", "b", "c"]


def test_an_attachment_with_no_text_still_counts_as_writing_back():
    # It opens the 24 hour window exactly like a text does, and the state
    # machine never reads the text anyway.
    payload = {
        "entry": [
            {
                "id": ACCOUNT,
                "messaging": [
                    {
                        "sender": {"id": IGSID},
                        "recipient": {"id": ACCOUNT},
                        "message": {"attachments": [{"type": "image"}]},
                    }
                ],
            }
        ]
    }

    assert webhook.inbound_messages(payload) == [(ACCOUNT, IGSID, "")]
