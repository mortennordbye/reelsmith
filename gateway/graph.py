"""Every call this service makes to Meta, and nothing else.

Same shape as `pipeline/publisher.py`: the HTTP client is injected rather than
constructed, which is what lets the whole thing be tested against
`httpx.MockTransport` without a network. The difference is that this one is
async, because it shares a process with a webhook handler that must answer fast.

Errors from Meta arrive as a JSON `error` object with a numeric code. Two of
those codes decide behaviour rather than just wording, so they are named.

**The token rides in an `Authorization` header, not the query string.** A token
in the query string is in every URL, and a URL is the thing every layer feels
free to write down: httpx logs one per request at INFO, proxies keep access
logs, and a `GraphError` naming a URL could carry it into an alert. Turning
this package's logging up once put a live token with publishing rights into the
pod log. Setting the httpx logger to WARNING hid that instance; the header is
what stops the next one, in a layer nobody here configures.

`refresh_access_token` is the one exception and stays on the query string,
because Meta documents no header form for it. It fires only inside the refresh
margin rather than on every call, and `pipeline/publisher.py` carries the same
exception for the same reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from gateway import probe
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


# The metrics asked for on a Reel. `views` replaced `plays` and `impressions`
# for this media type, and asking for a retired one fails the whole call rather
# than omitting that metric, which is why this list is fixed here rather than
# assembled per request.
MEDIA_METRICS = ("views", "reach", "likes", "comments", "saved", "shares")

# What the six above cannot tell you: whether anyone watched. Views count a
# viewer who left after half a second the same as one who watched to the end,
# so an account can read its own numbers for a week and never learn that the
# average viewer leaves at five seconds of a twenty six second video. Measured
# by hand on 2026-08-02 across seven posts, that was exactly the case, and skip
# rate ran 64 to 80 percent against a 30 to 40 percent benchmark for
# educational Reels. Nothing here was asking.
#
# `reels_skip_rate` is the share who scrolled past inside the first three
# seconds, so it scores the hook alone. `ig_reels_avg_watch_time` is
# milliseconds per initial view.
#
# **Reels only.** Meta fails the whole request when a metric does not apply to
# the media product type, so these cannot simply join the tuple above: one
# image post in the account would take the other six numbers down with it. The
# request asks for everything and falls back to the core six, which also tells
# the two cases apart. A media that fails both is too young to have numbers; a
# media that fails only the first is not a Reel.
REELS_METRICS = (
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
    "reels_skip_rate",
)

# Everything else a Reel reports, stored in `insights.extra` under these names.
#
# **A request of its own**, because Meta fails a whole call over one metric it
# will not give and says only "An unknown error has occurred". Folded into the
# request above, a retired name here would cost the skip rate the feedback loop
# turns on. `probe.Refusals` drops whatever is refused and names it.
#
# `follows`, `profile_visits` and `profile_activity` are the ones worth having
# and the documentation lists them for feed posts and stories only. They are
# left out rather than probed, since a refusal known in advance is only noise.
MEDIA_EXTRA_METRICS = ("total_interactions", "reposts", "facebook_views", "crossposted_views")

# The account itself, read once a sweep. The follower count is a field on the
# user node; the day totals come from its insights edge as `total_value` for
# yesterday, since today's is a number still moving.
ACCOUNT_FIELDS = "followers_count,follows_count,media_count"
ACCOUNT_METRICS = (
    "reach",
    "views",
    "accounts_engaged",
    "total_interactions",
    "likes",
    "comments",
    "shares",
    "saves",
    "replies",
    "reposts",
    "profile_links_taps",
)

_MEDIA_EXTRAS = probe.Refusals("Instagram media insights")
_ACCOUNT_INSIGHTS = probe.Refusals("Instagram account insights")


@dataclass(frozen=True)
class AccountReading:
    """One account's audience: the follower count and yesterday's totals."""

    followers: int | None
    extra: dict[str, Any] = field(default_factory=dict)


def _refused(outcome: Any) -> bool:
    """A Graph refusal that is about the request rather than the token."""
    return isinstance(outcome, GraphError) and not outcome.is_auth


# Meta's name -> ours. Theirs carry the product and the unit, which is useful
# in a request and noise in a column heading.
_RETENTION_FIELDS = {
    "ig_reels_avg_watch_time": "avg_watch_ms",
    "ig_reels_video_view_total_time": "total_watch_ms",
    "reels_skip_rate": "skip_rate",
}


@dataclass(frozen=True)
class MediaInsights:
    media_id: str
    views: int
    reach: int
    likes: int
    comments: int
    saved: int
    shares: int
    # Zero when the media is not a Reel, or when Meta had no retention numbers
    # for it yet. Zero is also a legitimate reading, so treat a whole row of
    # zeroes as "not measured" rather than "nobody watched".
    avg_watch_ms: int = 0
    total_watch_ms: int = 0
    skip_rate: float = 0.0

    @property
    def interactions(self) -> int:
        """Everything a viewer had to choose to do."""
        return self.likes + self.comments + self.saved + self.shares

    @property
    def avg_watch_seconds(self) -> float:
        return self.avg_watch_ms / 1000


def _first_value(item: Any) -> float:
    """Pull the number out of Meta's {values: [{value: n}]} wrapper.

    Float rather than int because `reels_skip_rate` comes back as 64.2, and
    rounding the one metric that scores the hook to 64 loses the only digit
    that would show a change working.
    """
    values = item.get("values") or []
    if not values:
        return 0.0
    try:
        return float(values[0].get("value") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _total_value(item: Any) -> float:
    """The number out of an account metric asked for as `total_value`.

    A different envelope from the media one, `{total_value: {value: n}}`, and
    falls back to that one because a metric asked for without a breakdown has
    been seen in both.
    """
    total = item.get("total_value") if isinstance(item, dict) else None
    if isinstance(total, dict) and isinstance(total.get("value"), (int, float)):
        return float(total["value"])
    return _first_value(item) if isinstance(item, dict) else 0.0


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, cfg: GatewaySettings):
        self._http = http
        self._cfg = cfg

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying client, for callers that talk to somebody else.

        `gateway/youtube.py` needs an async client and has nothing to do with
        the Graph API. Sharing this one rather than opening a second pool,
        since every URL here is absolute and there is no base_url to collide
        over.
        """
        return self._http

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        token_in_query: bool = False,
    ) -> dict[str, Any]:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if token_in_query:
            params["access_token"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
        response = await self._http.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self._cfg.graph_timeout_s,
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

    async def media_insights(self, *, media_id: str, token: str) -> MediaInsights | None:
        """How one published Reel is doing, or None if Meta will not say yet.

        Returns None rather than raising for the two normal cases: a Reel
        published minutes ago has no insights row yet, and a media id that has
        been deleted from the account is gone for good. Neither is a fault
        worth failing a refresh sweep over, and a sweep that dies on the first
        young post never reaches the older ones behind it.

        **An auth failure is re-raised.** It is not a property of this media,
        it will be true of every one behind it, and swallowing it would leave a
        sweep reading nothing at all while reporting no errors, which is the
        worst of the available outcomes.
        """
        values = await self._insight_values(
            media_id=media_id, token=token, metrics=MEDIA_METRICS + REELS_METRICS
        )
        if values is None:
            # Either the media is too young to have numbers or it is not a
            # Reel, and the retention metrics are what a non Reel rejects.
            # Asking again without them separates the two at the cost of one
            # request on media that were never going to have retention.
            values = await self._insight_values(
                media_id=media_id, token=token, metrics=MEDIA_METRICS
            )
        if values is None:
            return None

        return MediaInsights(
            media_id=media_id,
            **{name: int(values.get(name, 0)) for name in MEDIA_METRICS},
            **{
                ours: (
                    values.get(theirs, 0.0)
                    if ours == "skip_rate"
                    else int(values.get(theirs, 0))
                )
                for theirs, ours in _RETENTION_FIELDS.items()
            },
        )

    async def media_extras(self, *, media_id: str, token: str) -> dict[str, float]:
        """Every other metric a Reel will answer for, keyed by Meta's name.

        Called only after the core read for the same media worked, so a
        refusal here is about a metric rather than about a post too young to
        have numbers. Empty when nothing could be read. An auth failure is
        re-raised, for the reason `media_insights` gives.
        """

        async def fetch(metrics: list[str]) -> Any:
            try:
                return await self._request(
                    "GET",
                    f"{self._cfg.graph_base}/{media_id}/insights",
                    token=token,
                    params={"metric": ",".join(metrics)},
                )
            except GraphError as exc:
                return exc

        outcome = await _MEDIA_EXTRAS.read(MEDIA_EXTRA_METRICS, fetch, _refused)
        if isinstance(outcome, GraphError):
            if outcome.is_auth:
                raise outcome
            log.info("No extra insights for %s (%s)", media_id, outcome)
            return {}
        return {
            str(item.get("name")): _first_value(item)
            for item in ((outcome or {}).get("data") or [])
        }

    async def account_reading(
        self, *, ig_user_id: str, token: str, day: date
    ) -> AccountReading:
        """The follower count now, and the account's totals for `day`.

        Two reads, and either may fail without the other: the node answers on
        the basic scope, the insights edge wants the insights one and has a
        100 follower floor on some metrics. An auth failure is re-raised.
        """
        extra: dict[str, Any] = {}
        followers: int | None = None
        try:
            node = await self._request(
                "GET",
                f"{self._cfg.graph_base}/{ig_user_id}",
                token=token,
                params={"fields": ACCOUNT_FIELDS},
            )
        except GraphError as exc:
            if exc.is_auth:
                raise
            log.warning("Account fields for %s failed: %s", ig_user_id, exc)
            node = {}
        if isinstance(node.get("followers_count"), (int, float)):
            followers = int(node["followers_count"])
        for name in ("follows_count", "media_count"):
            if isinstance(node.get(name), (int, float)):
                extra[name] = int(node[name])

        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        window = {
            "period": "day",
            "metric_type": "total_value",
            "since": int(start.timestamp()),
            "until": int((start + timedelta(days=1)).timestamp()),
        }

        async def fetch(metrics: list[str]) -> Any:
            try:
                return await self._request(
                    "GET",
                    f"{self._cfg.graph_base}/{ig_user_id}/insights",
                    token=token,
                    params={"metric": ",".join(metrics), **window},
                )
            except GraphError as exc:
                return exc

        outcome = await _ACCOUNT_INSIGHTS.read(ACCOUNT_METRICS, fetch, _refused)
        if isinstance(outcome, GraphError):
            if outcome.is_auth:
                raise outcome
            log.warning("Account insights for %s failed: %s", ig_user_id, outcome)
        elif outcome:
            totals = {
                str(item.get("name")): _total_value(item)
                for item in (outcome.get("data") or [])
            }
            if totals:
                extra["day"] = {"on": day.isoformat(), **totals}
        return AccountReading(followers=followers, extra=extra)

    async def _insight_values(
        self, *, media_id: str, token: str, metrics: tuple[str, ...]
    ) -> dict[str, float] | None:
        """One insights read, or None if Meta would not answer for these metrics."""
        try:
            data = await self._request(
                "GET",
                f"{self._cfg.graph_base}/{media_id}/insights",
                token=token,
                params={"metric": ",".join(metrics)},
            )
        except GraphError as exc:
            if exc.is_auth:
                raise
            log.info("No insights for %s yet (%s)", media_id, exc)
            return None

        # Meta returns a list of {name, values: [{value}]}, and omits a metric
        # entirely rather than reporting zero when it has nothing.
        return {
            str(item.get("name")): _first_value(item)
            for item in (data.get("data") or [])
        }

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

        The only call that puts the token in the query string. Meta documents
        this endpoint with `access_token` as a required query parameter and no
        header form, and a token nobody could refresh is a browser trip, so it
        is not the place to find out whether an undocumented form works.
        """
        data = await self._request(
            "GET",
            f"{self._cfg.graph_host.rstrip('/')}/refresh_access_token",
            token=token,
            params={"grant_type": "ig_refresh_token"},
            token_in_query=True,
        )
        fresh = data.get("access_token")
        if not fresh:
            raise GraphError(f"Refresh returned no access_token: {data}")
        return fresh, data.get("expires_in")
