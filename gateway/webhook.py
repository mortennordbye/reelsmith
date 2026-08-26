"""Meta's side of the door.

Two routes. `GET /webhook` is the subscription handshake, which Meta calls once
when the callback URL is saved in the dashboard and echoes a challenge string.
`POST /webhook` is every inbound event after that.

Three things about the POST route are deliberate:

- **The signature is verified against the raw body, before parsing.** Any
  re-serialisation changes bytes and breaks the HMAC, so the handler reads
  `await request.body()` and hands the same bytes to both steps.
- **It answers 200 even when handling failed.** Meta never replays a missed
  event, so a non-200 buys nothing and only teaches the delivery system this
  endpoint is unhealthy. The comment poller is the safety net that makes this
  acceptable: comments are re-read for seven days.
- **Echoes are ignored.** The account's own outgoing messages arrive here too,
  and treating one as an inbound message would have the service answering
  itself.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from gateway import conversations

log = logging.getLogger(__name__)

router = APIRouter()


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Constant-time check of Meta's `X-Hub-Signature-256`.

    Absent header, wrong prefix and wrong digest are all one answer: no.
    """
    if not header or not app_secret:
        return False
    prefix, _, digest = header.partition("=")
    if prefix != "sha256" or not digest:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def sign(raw_body: bytes, app_secret: str) -> str:
    """The header Meta would send. Used by the tests and by hand with curl."""
    return "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def inbound_messages(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Flatten a delivery into (account_ig_id, sender_igsid, text).

    One POST batches up to 1000 entries, and each entry batches events, so the
    nesting is real rather than defensive.
    """
    out: list[tuple[str, str, str]] = []
    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            message = event.get("message") or {}
            if message.get("is_echo"):
                continue
            text = message.get("text")
            if not text:
                # Reactions, attachments and read receipts all land here. Any of
                # them still counts as the person writing back, but there is
                # nothing to read, so the state machine handles them the same
                # way as a text message and the text itself is unused.
                text = ""
            sender = (event.get("sender") or {}).get("id")
            recipient = (event.get("recipient") or {}).get("id") or entry.get("id")
            if sender and recipient:
                out.append((str(recipient), str(sender), str(text)))
    return out


@router.get("/webhook", response_class=PlainTextResponse)
async def verify(request: Request) -> Response:
    cfg = request.app.state.cfg
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get(
        "hub.verify_token"
    ) == cfg.verify_token:
        return PlainTextResponse(params.get("hub.challenge", ""))
    log.warning("Webhook handshake rejected")
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/webhook")
async def receive(request: Request) -> Response:
    app = request.app
    cfg = app.state.cfg
    raw = await request.body()

    if not verify_signature(raw, request.headers.get("x-hub-signature-256"), cfg.app_secret):
        app.state.metrics.webhook_signature_failures.inc()
        log.warning("Rejected an unsigned or wrongly signed webhook POST")
        return Response(status_code=403)

    try:
        payload = await request.json()
    except ValueError:
        log.warning("Webhook POST carried no JSON")
        return Response(status_code=200)

    for account_id, igsid, _text in inbound_messages(payload):
        try:
            await conversations.handle_inbound_message(
                app.state.db,
                app.state.graph,
                cfg,
                app.state.metrics,
                igsid=igsid,
                account_id=account_id,
            )
        except Exception:
            log.exception("Handling a message from %s failed", igsid)

    return Response(status_code=200)
