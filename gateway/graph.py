"""Every call this service makes to Meta, and nothing else.

Same shape as `pipeline/publisher.py`: the HTTP client is injected rather than
constructed, which is what lets the whole thing be tested against
`httpx.MockTransport` without a network. The difference is that this one is
async, because it shares a process with a webhook handler that must answer fast.

Errors from Meta arrive as a JSON `error` object with a numeric code. Two of
those codes decide behaviour rather than just wording, so they are named.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.config import GatewaySettings

log = logging.getLogger(__name__)

# The token is gone and no retry fixes it. Someone has to re-authorise in a
# browser, so this is worth surfacing loudly rather than counting as a blip.
OAUTH_ERROR_CODE = 190
# "user consent is required". Expected, not exceptional: profile fields stay
# unreadable until the person has messaged the account, which is exactly why the
# follow check happens at message time and never at comment time.
CONSENT_ERROR_CODES = frozenset({10, 200, 230})


class GraphError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @property
    def is_auth(self) -> bool:
        return self.code == OAUTH_ERROR_CODE

    @property
    def is_consent(self) -> bool:
        return self.code in CONSENT_ERROR_CODES


@dataclass(frozen=True)
class SendResult:
    """What Meta returns from a send.

    `recipient_id` is the load bearing field. On a private reply it is the
    commenter's IGSID, and it is the only place the person who commented and the
    person who will answer in the DM are connected.
    """

    recipient_id: str | None
    message_id: str | None


@dataclass(frozen=True)
class Comment:
    id: str
    text: str
    author_id: str | None
    timestamp: str | None


@dataclass(frozen=True)
class Profile:
    igsid: str
    username: str | None
    follows_us: bool | None  # None means Meta would not say


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, cfg: GatewaySettings):
        self._http = http
        self._cfg = cfg

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        params["access_token"] = token
        response = await self._http.request(
            method, url, params=params, json=json_body, timeout=self._cfg.graph_timeout_s
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"] or {}
            raise GraphError(
                str(err.get("message", "Graph API error")),
                code=err.get("code"),
                subcode=err.get("error_subcode"),
            )
        if response.status_code >= 400:
            raise GraphError(f"HTTP {response.status_code} from {url}")
        return payload if isinstance(payload, dict) else {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The same call `_request` makes, for callers outside this class.

        `publisher.py` drives a three-call sequence that is nobody's business
        but its own, so it gets the transport rather than three thin wrappers
        here. Error translation and the token parameter stay in one place.
        """
        return await self._request(
            method, url, token=token, params=params, json_body=json_body
        )

    # --- Messaging ---------------------------------------------------------

    async def send_private_reply(
        self, *, ig_user_id: str, token: str, comment_id: str, text: str
    ) -> SendResult:
        """The one reply Meta allows per comment, within seven days of it."""
        return await self._send(
            ig_user_id=ig_user_id,
            token=token,
            recipient={"comment_id": comment_id},
            text=text,
        )

    async def send_message(
        self, *, ig_user_id: str, token: str, igsid: str, text: str
    ) -> SendResult:
        """A direct message. Only legal inside the 24 hour window.

        The window is enforced by the caller rather than here, because the only
        thing that knows when the person last wrote is the conversation row.
        """
        return await self._send(
            ig_user_id=ig_user_id, token=token, recipient={"id": igsid}, text=text
        )

    async def _send(
        self, *, ig_user_id: str, token: str, recipient: dict[str, str], text: str
    ) -> SendResult:
        data = await self._request(
            "POST",
            f"{self._cfg.graph_base}/{ig_user_id}/messages",
            token=token,
            json_body={"recipient": recipient, "message": {"text": text}},
        )
        return SendResult(
            recipient_id=data.get("recipient_id"), message_id=data.get("message_id")
        )

    # --- Reading -----------------------------------------------------------

    async def list_comments(self, *, media_id: str, token: str, limit: int = 50) -> list[Comment]:
        data = await self._request(
            "GET",
            f"{self._cfg.graph_base}/{media_id}/comments",
            token=token,
            params={"fields": "id,text,timestamp,from", "limit": limit},
        )
        out: list[Comment] = []
        for item in data.get("data") or []:
            author = item.get("from") or {}
            out.append(
                Comment(
                    id=str(item.get("id", "")),
                    text=str(item.get("text") or ""),
                    author_id=str(author.get("id")) if author.get("id") else None,
                    timestamp=item.get("timestamp"),
                )
            )
        return [c for c in out if c.id]

    async def get_profile(self, *, igsid: str, token: str) -> Profile:
        """Whether this person follows the account.

        Readable only after they have messaged us. A consent error is not a bug
        and is reported as an unknown follow state, which the state machine
        treats as "not yet".
        """
        try:
            data = await self._request(
                "GET",
                f"{self._cfg.graph_base}/{igsid}",
                token=token,
                params={"fields": "username,is_user_follow_business"},
            )
        except GraphError as exc:
            if exc.is_consent:
                log.info("Profile for %s not readable yet (%s)", igsid, exc)
                return Profile(igsid=igsid, username=None, follows_us=None)
            raise
        raw = data.get("is_user_follow_business")
        return Profile(
            igsid=igsid,
            username=data.get("username"),
            follows_us=bool(raw) if raw is not None else None,
        )

    # --- Account plumbing --------------------------------------------------

    async def subscribe_messages(self, *, token: str) -> bool:
        """Turn on webhook delivery for this account. Idempotent."""
        data = await self._request(
            "POST",
            f"{self._cfg.graph_base}/me/subscribed_apps",
            token=token,
            params={"subscribed_fields": "messages"},
        )
        return bool(data.get("success", True))

    async def me(self, *, token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{self._cfg.graph_base}/me",
            token=token,
            params={"fields": "user_id,username"},
        )

    async def refresh_token(self, *, token: str) -> tuple[str, int | None]:
        """Extend a long-lived token by another 60 days.

        Meta refuses if the token is under 24 hours old, which is not worth
        acting on: a token that new has 59 days left.
        """
        data = await self._request(
            "GET",
            f"{self._cfg.graph_host.rstrip('/')}/refresh_access_token",
            token=token,
            params={"grant_type": "ig_refresh_token"},
        )
        fresh = data.get("access_token")
        if not fresh:
            raise GraphError(f"Refresh returned no access_token: {data}")
        return fresh, data.get("expires_in")
