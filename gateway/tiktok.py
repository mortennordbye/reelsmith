"""Publishing to TikTok, on either side of an audit that may never come.

Three shapes of destination now. **Meta fetches the video** from a public URL,
which is why this service hosts `/media/*` at all. **YouTube takes pushed
bytes.** TikTok fetches, like Meta, so nothing new gets hosted and the seam
already exists.

**The two paths differ by one field, one endpoint and one success state**, and
building for both is the whole hedge in `docs/tiktok-api-setup.md`:

|  | Direct Post | Inbox |
|---|---|---|
| endpoint | `/v2/post/publish/video/init/` | `/v2/post/publish/inbox/video/init/` |
| body | carries `post_info` | no `post_info` |
| done at | `PUBLISH_COMPLETE` | `SEND_TO_USER_INBOX` |
| audited | yes, or everything is `SELF_ONLY` | never needed |

The audit is a UX review of a posting screen this repo does not have, and the
content sharing guidelines say in terms that unattended posting is not allowed.
So a refusal is the expected outcome and it has to cost a config flag rather
than a rewrite. That is the only reason `direct_post=False` exists as a
parameter rather than as a fork somebody writes later.

**`SEND_TO_USER_INBOX` is a success on one path and an intermediate state
nowhere else.** A publisher that waits for `PUBLISH_COMPLETE` on the inbox path
polls until it times out on a video that is sitting in the creator's drafts,
which is why `_DONE` is chosen from the path rather than from a constant.

**`publish_id` is this file's container id.** `publisher.PublishError` divides
failures by whether a container exists, because past that point a post may be
live and no error text proves otherwise. The same line lives here: before
`video/init/` returns, TikTok was never asked to make anything and the slot can
be handed back; after it, only a person should decide.

Raw httpx rather than a vendor SDK, following `gateway/youtube.py`: four
documented calls do not justify a synchronous client library inside a service
that is async end to end.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BASE = "https://open.tiktokapis.com"
TOKEN_URL = f"{BASE}/v2/oauth/token/"
CREATOR_INFO_URL = f"{BASE}/v2/post/publish/creator_info/query/"
DIRECT_INIT_URL = f"{BASE}/v2/post/publish/video/init/"
INBOX_INIT_URL = f"{BASE}/v2/post/publish/inbox/video/init/"
STATUS_URL = f"{BASE}/v2/post/publish/status/fetch/"
VIDEO_LIST_URL = f"{BASE}/v2/video/list/"
VIDEO_QUERY_URL = f"{BASE}/v2/video/query/"

# One `title` field carrying the ask, the link and the hashtags together. Not
# Instagram's caption and not YouTube's title plus description, which is why
# `pipeline/gateway.py` builds a third shape.
MAX_TITLE = 2_200

# What `status/fetch` can say. `FAILED` is terminal; the two PROCESSING states
# are not; the last two are each the end of exactly one path.
STATUS_INBOX_DONE = "SEND_TO_USER_INBOX"
STATUS_PUBLISHED = "PUBLISH_COMPLETE"
STATUS_FAILED = "FAILED"

# The documented 403 that is the audit, and the one worth naming in a log. It
# is also the proof that everything else is wired correctly, so it reads as a
# milestone rather than only as an error.
ERROR_UNAUDITED = "unaudited_client_can_only_post_to_private_accounts"
# The DNS record nobody did. It names the URL rather than the missing record,
# and it fails at init rather than at download, so it looks like a bad media
# URL.
ERROR_URL_UNVERIFIED = "url_ownership_unverified"


class PublishError(RuntimeError):
    """A post that did not complete.

    `publish_started` is the field the caller acts on, not the message. False
    means TikTok was never asked to make anything and the slot can be handed
    back. True means a post may exist, and only a human should decide.
    """

    def __init__(self, message: str, *, publish_started: bool = False, code: str = ""):
        super().__init__(message)
        self.publish_started = publish_started
        self.code = code


@dataclass(frozen=True)
class Credentials:
    """What one account needs, and what a refresh hands back.

    `refresh_token` is here rather than looked up per call because the caller
    has to persist whatever the refresh returned, and passing it through a
    result object is what makes forgetting that write hard.
    """

    open_id: str
    client_key: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True)
class RefreshedToken:
    """What a refresh hands back, including a refresh token that must be stored.

    `refresh_token` is on here rather than left in the response body because
    the caller has to persist it, and a result object it has to unpack is the
    cheapest way to make forgetting that write hard.
    """

    access_token: str
    refresh_token: str
    expires_in: int = 0
    refresh_expires_in: int = 0

    def rotated_from(self, previous: str) -> bool:
        """Whether TikTok handed back a different token than the one sent.

        Not a condition to branch the write on. The write happens either way,
        because "it looked the same" is not a guarantee TikTok offers. It is
        here so a log line can say which happened, since a rotation that was
        never persisted is an account lost a year later with nothing left to
        explain it.
        """
        return self.refresh_token != previous


@dataclass(frozen=True)
class CreatorInfo:
    """What this account may currently be asked to post.

    Not decoration. `privacy_level` has to come from `privacy_level_options`
    or the post fails `privacy_level_option_mismatch`, which reads like a bad
    constant and is actually a stale read.
    """

    username: str = ""
    privacy_level_options: list[str] = field(default_factory=list)
    comment_disabled: bool = False
    duet_disabled: bool = False
    stitch_disabled: bool = False
    max_video_post_duration_sec: int = 0


@dataclass(frozen=True)
class PublishResult:
    publish_id: str
    status: str

    @property
    def in_inbox(self) -> bool:
        return self.status == STATUS_INBOX_DONE


def _error(payload: dict) -> tuple[str, str]:
    """TikTok's envelope, which reports failure with HTTP 200 as readily as not.

    Every response carries `error.code`, and `ok` is the success value. Reading
    the status code alone would treat a `scope_not_authorized` as a successful
    post.
    """
    err = payload.get("error") or {}
    code = str(err.get("code") or "")
    if code and code != "ok":
        return code, str(err.get("message") or "")
    return "", ""


async def refresh_access_token(
    http: httpx.AsyncClient, *, credentials: Credentials
) -> RefreshedToken:
    """Trade the refresh token for an access token, and for a new refresh token.

    **The returned refresh token must be persisted before anything else
    happens.** TikTok rotates it on every use and the one that was just spent
    is dead. Instagram's token is refreshed by a job whose failure costs a day;
    Google's has no clock at all. This one, dropped, costs the account.
    """
    try:
        response = await http.post(
            TOKEN_URL,
            data={
                "client_key": credentials.client_key,
                "client_secret": credentials.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not reach TikTok to refresh a token: {exc}") from exc

    if response.status_code != 200:
        raise PublishError(
            f"Token refresh failed ({response.status_code}): {response.text}"
        )

    payload = response.json()
    # The token endpoint reports failure in `error` rather than `error.code`,
    # unlike every other call here.
    if payload.get("error"):
        raise PublishError(
            f"Token refresh refused: {payload.get('error')} "
            f"{payload.get('error_description') or ''}".strip(),
            code=str(payload.get("error")),
        )

    access = str(payload.get("access_token") or "")
    refreshed = str(payload.get("refresh_token") or "")
    if not access:
        raise PublishError("Token refresh returned no access token")
    if not refreshed:
        # Not survivable by carrying the old one forward: if TikTok rotated and
        # the response was misread, the old token is already dead and using it
        # burns the next attempt too.
        raise PublishError("Token refresh returned no refresh token")

    return RefreshedToken(
        access_token=access,
        refresh_token=refreshed,
        expires_in=int(payload.get("expires_in") or 0),
        refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
    )


async def creator_info(http: httpx.AsyncClient, *, token: str) -> CreatorInfo:
    """Ask what this account may be asked to post, before asking it to.

    Mandatory rather than advisory, and TikTok checks that it was used. It is
    also the only authoritative answer to how long a video may be, and the only
    way to tell a missing scope from an app misconfiguration.
    """
    try:
        response = await http.post(
            CREATOR_INFO_URL,
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=UTF-8",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not reach TikTok for creator info: {exc}") from exc

    payload = response.json() if response.content else {}
    code, message = _error(payload)
    if code:
        raise PublishError(f"Creator info refused: {code} {message}".strip(), code=code)

    data = payload.get("data") or {}
    return CreatorInfo(
        username=str(data.get("creator_username") or ""),
        privacy_level_options=list(data.get("privacy_level_options") or []),
        comment_disabled=bool(data.get("comment_disabled")),
        duet_disabled=bool(data.get("duet_disabled")),
        stitch_disabled=bool(data.get("stitch_disabled")),
        max_video_post_duration_sec=int(data.get("max_video_post_duration_sec") or 0),
    )


def _clean_title(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > MAX_TITLE:
        log.warning("Truncating title from %d to %d characters", len(cleaned), MAX_TITLE)
        cleaned = cleaned[:MAX_TITLE].rstrip()
    return cleaned


async def start_publish(
    http: httpx.AsyncClient,
    *,
    token: str,
    video_url: str,
    title: str,
    direct_post: bool,
    privacy_level: str = "",
    is_aigc: bool = False,
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    cover_timestamp_ms: int = 0,
) -> str:
    """Ask TikTok to fetch the video. Returns the `publish_id`.

    Nothing exists on TikTok before this returns, so every failure up to here
    is safe to retry. After it, a post may exist.

    `post_info` is what separates the two paths, and it is left off entirely
    rather than sent empty: the inbox endpoint does not take it and sending one
    is a different request, not a harmless extra.
    """
    body: dict = {
        "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
    }
    if direct_post:
        post_info: dict = {
            "title": _clean_title(title),
            # Required, and it has to be one of the options `creator_info`
            # returned, or the post fails `privacy_level_option_mismatch`.
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            # The same question `containsSyntheticMedia` asks on YouTube. It is
            # decided in config and travels from there, so the two platforms
            # cannot answer it differently by accident.
            "is_aigc": is_aigc,
        }
        if cover_timestamp_ms:
            # The same moment `pipeline/publisher.py` already computes for
            # Meta's `thumb_offset`, which is COVER_FRAME / fps.
            post_info["video_cover_timestamp_ms"] = cover_timestamp_ms
        body["post_info"] = post_info

    url = DIRECT_INIT_URL if direct_post else INBOX_INIT_URL
    try:
        response = await http.post(
            url,
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=UTF-8",
            },
            json=body,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not open a TikTok publish: {exc}") from exc

    payload = response.json() if response.content else {}
    code, message = _error(payload)
    if code:
        if code == ERROR_UNAUDITED:
            log.error(
                "TikTok refused the post because the client is unaudited. Everything "
                "else is wired correctly; set the privacy level to SELF_ONLY or use "
                "the inbox path until the audit lands."
            )
        elif code == ERROR_URL_UNVERIFIED:
            log.error(
                "TikTok will not fetch %s: the domain is not verified in the app's "
                "URL properties. This is a DNS record, not a bad media URL.",
                video_url,
            )
        raise PublishError(f"Publish refused: {code} {message}".strip(), code=code)

    publish_id = str((payload.get("data") or {}).get("publish_id") or "")
    if not publish_id:
        raise PublishError(f"Publish returned no publish_id: {payload}")
    return publish_id


async def fetch_status(
    http: httpx.AsyncClient, *, token: str, publish_id: str
) -> tuple[str, str]:
    """One poll. Returns `(status, failure_reason)`."""
    try:
        response = await http.post(
            STATUS_URL,
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(
            f"Could not read the publish status: {exc}", publish_started=True
        ) from exc

    payload = response.json() if response.content else {}
    code, message = _error(payload)
    if code:
        raise PublishError(
            f"Status check refused: {code} {message}".strip(),
            publish_started=True,
            code=code,
        )

    data = payload.get("data") or {}
    return str(data.get("status") or ""), str(data.get("fail_reason") or "")


async def await_publish(
    http: httpx.AsyncClient,
    *,
    token: str,
    publish_id: str,
    direct_post: bool,
    poll_interval_s: float = 5,
    timeout_s: float = 300,
) -> PublishResult:
    """Poll until the post is done, on whichever path it is on.

    The terminal state is chosen from the path rather than fixed, because
    `SEND_TO_USER_INBOX` is the finish line on one path and a stage on the
    other. Waiting for `PUBLISH_COMPLETE` on the inbox path times out on a
    video that is already sitting in the creator's drafts, and reports a
    failure for something that worked.
    """
    done = STATUS_PUBLISHED if direct_post else STATUS_INBOX_DONE
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        last, reason = await fetch_status(http, token=token, publish_id=publish_id)
        if last == done:
            return PublishResult(publish_id=publish_id, status=last)
        if last == STATUS_FAILED:
            raise PublishError(
                f"TikTok failed the post: {reason or 'no reason given'}",
                publish_started=True,
            )
        # On the direct path the inbox state cannot appear, and on the inbox
        # path nothing follows it, so anything else is genuinely in progress.
        await asyncio.sleep(poll_interval_s)

    raise PublishError(
        f"Gave up waiting for {publish_id} after {timeout_s:.0f}s, last status "
        f"{last or 'unknown'}",
        publish_started=True,
    )


# --- What came back, which is less than either other platform gives ----------
#
# There is no retention metric and no substitute for one. `/v2/video/query/`
# returns view, like, comment and share counts and nothing about watch time,
# completion or anything a three second skip could be computed from. So the
# rule `PLAN.md` H6 set for YouTube holds here without argument: these numbers
# are worth storing and showing, and are never fed to the prompt that writes
# tomorrow's script.
#
# One shape TikTok adds that Meta never needed: **the `publish_id` returned at
# post time is not a video id.** Getting from one to the other means listing
# the account's recent videos and matching, which is what `list_videos` is for.

VIDEO_FIELDS = "id,create_time,title,share_url,view_count,like_count,comment_count,share_count"


@dataclass(frozen=True)
class Video:
    """One of the account's own posts, as `/v2/video/list/` describes it."""

    video_id: str
    title: str = ""
    share_url: str = ""
    create_time: int = 0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0

    @classmethod
    def parse(cls, row: dict) -> Video:
        return cls(
            video_id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            share_url=str(row.get("share_url") or ""),
            create_time=int(row.get("create_time") or 0),
            views=int(row.get("view_count") or 0),
            likes=int(row.get("like_count") or 0),
            comments=int(row.get("comment_count") or 0),
            shares=int(row.get("share_count") or 0),
        )


async def list_videos(
    http: httpx.AsyncClient, *, token: str, limit: int = 20
) -> list[Video]:
    """The account's own recent videos, newest first.

    Twenty at a time, which is the API's page size and comfortably more than a
    queue that publishes a few a day produces between sweeps. Not paged: a post
    that has fallen off the first page has been live long enough that its
    numbers have settled, and asking for more would spend calls to re-read
    videos nothing is going to look at again.
    """
    try:
        response = await http.post(
            VIDEO_LIST_URL,
            params={"fields": VIDEO_FIELDS},
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=UTF-8",
            },
            json={"max_count": limit},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not list the account's videos: {exc}") from exc

    payload = response.json() if response.content else {}
    code, message = _error(payload)
    if code:
        raise PublishError(f"Video list refused: {code} {message}".strip(), code=code)

    rows = (payload.get("data") or {}).get("videos") or []
    return [Video.parse(row) for row in rows]


async def query_videos(
    http: httpx.AsyncClient, *, token: str, video_ids: list[str]
) -> dict[str, Video]:
    """Counts for videos already known by id, keyed by id.

    Twenty ids per request, which is the documented ceiling. Separate from
    `list_videos` because the two answer different questions: that one finds
    a video, this one re-reads a video already found, and a sweep that only
    listed would lose a post the moment it fell off the first page.
    """
    if not video_ids:
        return {}
    try:
        response = await http.post(
            VIDEO_QUERY_URL,
            params={"fields": VIDEO_FIELDS},
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=UTF-8",
            },
            json={"filters": {"video_ids": video_ids[:20]}},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not query videos: {exc}") from exc

    payload = response.json() if response.content else {}
    code, message = _error(payload)
    if code:
        raise PublishError(f"Video query refused: {code} {message}".strip(), code=code)

    rows = (payload.get("data") or {}).get("videos") or []
    return {video.video_id: video for video in map(Video.parse, rows) if video.video_id}
