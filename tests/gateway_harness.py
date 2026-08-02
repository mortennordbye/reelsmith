"""A gateway wired to a fake Meta.

Same trade as `tests/test_publisher.py`: `httpx.MockTransport` answers every
Graph call in-process, so the sequence and the failure handling can be asserted
without a network and without a token. The fake records what it was asked to
send, because in this service the thing worth testing is nearly always "did it
send exactly one of these".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.config import GatewaySettings

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
API_TOKEN = "test-api-token"
ACCOUNT = "17841400000000000"
IGSID = "9876543210"


@dataclass
class FakeMeta:
    """Stands in for graph.instagram.com.

    Every send is recorded. `follows` decides what the profile call answers, and
    flipping it mid-test is how the follow gate gets exercised.
    """

    follows: bool | None = False
    comments: list[dict[str, Any]] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    fail_sends_with: dict[str, Any] | None = None
    profile_error: dict[str, Any] | None = None
    calls: list[str] = field(default_factory=list)
    # Every request exactly as it went out. `calls` is the readable summary;
    # this is what lets a test look at the headers and the full URL, which is
    # how "the token is never in a URL" gets asserted over the whole surface
    # rather than over the one call somebody remembered.
    requests: list[httpx.Request] = field(default_factory=list)
    recipient_id: str | None = IGSID
    # Per media id. A media absent from here answers the way Meta does for a
    # Reel too young to have numbers, which is an error rather than zeroes.
    insights: dict[str, dict[str, int]] = field(default_factory=dict)
    insights_error: dict[str, Any] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    @property
    def texts(self) -> list[str]:
        return [s["message"]["text"] for s in self.sends]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        self.requests.append(request)

        if path.endswith("/messages") and request.method == "POST":
            if self.fail_sends_with:
                return httpx.Response(400, json={"error": self.fail_sends_with})
            body = json.loads(request.content)
            self.sends.append(body)
            return httpx.Response(
                200, json={"recipient_id": self.recipient_id, "message_id": "mid.1"}
            )

        if path.endswith("/comments"):
            return httpx.Response(200, json={"data": self.comments})

        if path.endswith("/insights"):
            if self.insights_error:
                return httpx.Response(400, json={"error": self.insights_error})
            media_id = path.rstrip("/").split("/")[-2]
            values = self.insights.get(media_id)
            if values is None:
                # What Meta says for a Reel with nothing yet.
                return httpx.Response(
                    400,
                    json={"error": {"message": "Insights are not available", "code": 100}},
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": name, "values": [{"value": n}]}
                        for name, n in values.items()
                    ]
                },
            )

        if path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})

        if path.endswith("/refresh_access_token"):
            return httpx.Response(200, json={"access_token": "fresher", "expires_in": 5_184_000})

        # Anything left is the profile read, which is a bare node id.
        if self.profile_error:
            return httpx.Response(400, json={"error": self.profile_error})
        payload: dict[str, Any] = {"username": "commenter"}
        if self.follows is not None:
            payload["is_user_follow_business"] = self.follows
        return httpx.Response(200, json=payload)


def settings(tmp_path, **overrides: Any) -> GatewaySettings:
    base = {
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
        "api_token": API_TOKEN,
        "db_path": tmp_path / "gateway.sqlite3",
        "covers_dir": tmp_path / "covers",
        "public_base_url": "https://gate.example.test",
        # A dotenv on the developer's machine must not reach into a test run.
        "_env_file": None,
    }
    return GatewaySettings(**{**base, **overrides})


def comment(cid: str, text: str, *, author: str | None = "commenter-1") -> dict[str, Any]:
    return {
        "id": cid,
        "text": text,
        "timestamp": "2026-07-31T10:00:00+0000",
        "from": {"id": author},
    }


def message_event(text: str = "ok", *, sender: str = IGSID, recipient: str = ACCOUNT) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": recipient,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": recipient},
                        "message": {"mid": "mid.in", "text": text},
                    }
                ],
            }
        ],
    }
