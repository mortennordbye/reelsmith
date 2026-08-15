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
# A YouTube channel id: `UC` and 22 more characters. Registered as an account
# row alongside the Instagram one, which is the shape the cluster runs.
CHANNEL = "UCq0Ff3lJ7dK2sWnEv8mXtLp"


@dataclass
class FakeYouTube:
    """Stands in for oauth2.googleapis.com and the resumable upload endpoint.

    Three calls, and the interesting assertions are about the boundary between
    the second and the third: a failure before the session URI exists is
    retryable and one after it is not, which is the same line
    `publisher.PublishError` draws and the reason the queue can be restarted
    safely.
    """

    video_id: str = "yt-video-1"
    session_uri: str = "https://upload.googleapis.com/resumable/session-1"
    # What the video resource reports back. Setting this to "private" while the
    # caller asked for "public" is the unaudited project lock.
    privacy_status: str = "private"
    token_status: int = 200
    session_status: int = 200
    upload_status: int = 200
    # Every resumable init body, so a test can assert what the metadata said.
    sessions: list[dict[str, Any]] = field(default_factory=list)
    # Byte counts of each completed PUT.
    uploads: list[int] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response | None:
        """Answer if this is ours, otherwise None so Meta gets a look."""
        url = str(request.url)

        if url.startswith("https://oauth2.googleapis.com/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ya29.fake", "expires_in": 3599})

        if url.startswith("https://www.googleapis.com/upload/youtube/v3/videos"):
            if self.session_status != 200:
                return httpx.Response(self.session_status, json={"error": "nope"})
            self.sessions.append(json.loads(request.content))
            return httpx.Response(200, headers={"location": self.session_uri})

        if url == self.session_uri:
            if self.upload_status not in (200, 201):
                return httpx.Response(self.upload_status, text="upload rejected")
            self.uploads.append(len(request.content))
            return httpx.Response(
                200,
                json={"id": self.video_id, "status": {"privacyStatus": self.privacy_status}},
            )

        return None


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
    insights: dict[str, dict[str, float]] = field(default_factory=dict)
    insights_error: dict[str, Any] | None = None
    # Media ids that answer the core metrics but reject the Reels-only
    # retention ones, which is how Meta treats an image post. The client is
    # expected to notice and ask again without them.
    not_a_reel: set[str] = field(default_factory=set)
    # The gateway hands one httpx client to both upstreams, so one transport
    # has to answer for both. Google gets first refusal and falls through.
    youtube: FakeYouTube = field(default_factory=FakeYouTube)

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

        google = self.youtube.handle(request)
        if google is not None:
            return google

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
            asked = (request.url.params.get("metric") or "").split(",")
            if media_id in self.not_a_reel and any(m.startswith("ig_reels") for m in asked):
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": (
                                "The Media Insights API does not support the "
                                "ig_reels_avg_watch_time metric for this media "
                                "product type."
                            ),
                            "code": 100,
                        }
                    },
                )
            values = {k: v for k, v in values.items() if k in asked}
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
