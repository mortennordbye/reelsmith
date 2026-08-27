"""Publishing a Reel to a Facebook Page, and reading what it did.

The fourth destination and the cheapest one, for a reason worth stating rather
than rediscovering: **a Page access token is Meta's credential shape**, which
is the shape `accounts` has held since the first migration. YouTube needed a
client pair plus a refresh token and TikTok needed a rotating one, so each got
a table. This needs neither, and there is no `facebook_credentials` anywhere.
The token and its expiry live on the account row exactly as Instagram's do.

**Meta fetches the video**, which is the seam this service already provides for
Instagram and TikTok, so nothing new is hosted and no bytes leave the cluster.
The upload step takes a `file_url` header and Meta pulls from it.

Three calls, and the middle one is not on `graph.facebook.com`:

| phase | endpoint |
|---|---|
| `start` | `POST /{page-id}/video_reels` |
| upload | `POST rupload.facebook.com/video-upload/{version}/{video-id}` |
| `finish` | `POST /{page-id}/video_reels`, `video_state=PUBLISHED` |

**The line between a retry that is safe and one that is not sits at `start`,
not at `finish`.** That is one step earlier than the API's own irreversibility:
`start` creates an unpublished video id and publishes nothing, so in principle
a failed upload could be retried. It is treated as terminal anyway, because a
retry restarts from `start` and cannot tell "the finish call never landed" from
"the finish call landed and the response was lost". The second case posts the
Reel twice, which is the one failure here that cannot be undone quietly. Same
line as `publisher.create_container` and for the same reason.

**This is not the Instagram Login path.** `gateway/publisher.py` talks to
`graph.instagram.com` with a token that cannot see a Page, and this talks to
`graph.facebook.com` with a Page token that cannot see the Instagram account.
The hosts are constants here rather than `cfg.graph_host`, following
`youtube.py` and `tiktok.py`, because one shared host setting serving two
different login paths is a setting that is wrong for one of them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
# The upload host, which is the same one `pipeline/publisher.py` names in its
# note about the resumable path. Only the upload phase goes here.
RUPLOAD = "https://rupload.facebook.com"

# Reels are 3 to 90 seconds. Ours run 30 to 45, so this is a guard against a
# format change rather than against today's videos, and it is checked nowhere:
# the renderer decides length and a number in this file cannot enforce it. It
# is here so the constraint is written down next to the thing it constrains.
MIN_SECONDS = 3
MAX_SECONDS = 90

# What `status.video_status` can say. `ready` is the finish line; the two
# failures are terminal and everything else is still moving.
STATUS_READY = "ready"
STATUS_UPLOAD_FAILED = "upload_failed"
STATUS_EXPIRED = "expired"

# What `status.publishing_phase.publish_status` can say. `published` is the one
# that means a person can see it.
PUBLISH_PUBLISHED = "published"
PUBLISH_ERROR = "error"


class PublishError(RuntimeError):
    """A publish that did not complete.

    `video_created` is the field the caller acts on, not the message. False
    means Meta was never asked to make anything and a retry is provably safe.
    True means a Reel may exist, and the only safe move is to stop and let a
    person look. The same division `publisher.PublishError` makes with
    `container_created`.
    """

    def __init__(self, message: str, *, video_created: bool = False, code: str = ""):
        super().__init__(message)
        self.video_created = video_created
        self.code = code


@dataclass(frozen=True)
class PublishResult:
    video_id: str
    permalink: str | None = None


def _api_error(response: httpx.Response) -> tuple[str, str]:
    """Meta's error envelope, which is JSON even when the status code is not 200.

    Returns `(code, message)`, both empty when nothing went wrong. Read from
    the body rather than from the status code because a Graph error carries the
    subcode that says which of several things went wrong, and the status code
    alone turns "this Page has not accepted the terms" into "400".
    """
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        code = str(err.get("code") or "")
        subcode = str(err.get("error_subcode") or "")
        return f"{code}/{subcode}" if subcode else code, str(err.get("message") or "")
    if response.status_code >= 400:
        return str(response.status_code), response.text[:400]
    return "", ""


def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _auth(token: str) -> dict[str, str]:
    """The token in a header, never in a URL or a body.

    The same rule `gateway/graph.py` follows and `test_gateway_graph.py` pins
    over its whole surface, applied here so there is one story rather than two.
    Meta documents most of these calls with `access_token` as a parameter, and
    the header form works on both Graph hosts; a URL is the copy that reaches
    logs, referrers and shell history.

    The upload phase is the exception and it is not this function: that
    endpoint takes `Authorization: OAuth <token>`, not `Bearer`, and writing it
    the ordinary way fails as a 401.
    """
    return {"Authorization": f"Bearer {token}"}


async def start_upload(
    http: httpx.AsyncClient,
    *,
    page_id: str,
    token: str,
    api_version: str,
) -> str:
    """Open an upload session. Returns the `video_id`.

    Nothing exists on the Page before this returns and nothing is visible after
    it either, but the id it hands back is durable, and it is what every later
    step is keyed on. So this is the moment the caller commits, and the moment
    after which a failure stops rather than retries.
    """
    try:
        response = await http.post(
            f"{GRAPH}/{api_version}/{page_id}/video_reels",
            data={"upload_phase": "start"},
            headers=_auth(token),
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not reach Meta to start a Reel upload: {exc}") from exc

    code, message = _api_error(response)
    if code:
        raise PublishError(f"Reel upload refused: {code} {message}".strip(), code=code)

    video_id = str(_json(response).get("video_id") or "")
    if not video_id:
        raise PublishError(f"The upload start returned no video_id: {response.text[:400]}")
    return video_id


async def upload_hosted(
    http: httpx.AsyncClient,
    *,
    video_id: str,
    token: str,
    video_url: str,
    api_version: str,
    timeout_s: float = 600,
) -> None:
    """Point Meta at the video and wait while it fetches.

    The hosted form, so the file is pulled rather than pushed and this service
    sends no bytes. `file_url` is a *header*, not a form field, which is the
    one surprising thing about this call and the reason it is worth a function
    of its own rather than a line in the caller.

    `Authorization: OAuth <token>` rather than `Bearer`, which is what this
    endpoint takes and what silently fails as a 401 if it is written the
    ordinary way.
    """
    try:
        response = await http.post(
            f"{RUPLOAD}/video-upload/{api_version}/{video_id}",
            headers={"Authorization": f"OAuth {token}", "file_url": video_url},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise PublishError(
            f"Meta could not be asked to fetch {video_url}: {exc}", video_created=True
        ) from exc

    code, message = _api_error(response)
    if code:
        raise PublishError(
            f"Meta refused to fetch the video: {code} {message}".strip(),
            video_created=True,
            code=code,
        )
    if not _json(response).get("success"):
        raise PublishError(
            f"Meta did not confirm the fetch: {response.text[:400]}", video_created=True
        )


async def finish_upload(
    http: httpx.AsyncClient,
    *,
    page_id: str,
    video_id: str,
    token: str,
    description: str,
    api_version: str,
) -> None:
    """Publish the uploaded video as a Reel.

    `video_state=PUBLISHED` is what separates this from a draft. The other
    states are not offered as a parameter, because a draft nobody publishes is
    the same outcome as a queue row that failed, and it would be invisible
    where a failure is not.
    """
    try:
        response = await http.post(
            f"{GRAPH}/{api_version}/{page_id}/video_reels",
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": description,
            },
            headers=_auth(token),
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise PublishError(
            f"Could not reach Meta to publish the Reel: {exc}", video_created=True
        ) from exc

    code, message = _api_error(response)
    if code:
        raise PublishError(
            f"Publish refused: {code} {message}".strip(), video_created=True, code=code
        )
    if not _json(response).get("success"):
        raise PublishError(
            f"Meta did not confirm the publish: {response.text[:400]}", video_created=True
        )


async def video_status(
    http: httpx.AsyncClient, *, video_id: str, token: str, api_version: str
) -> tuple[str, str, str]:
    """One poll. Returns `(video_status, publish_status, permalink)`.

    The permalink comes back from the same call because it costs nothing to ask
    for and the alternative is a second request in the window where the row is
    already published and nothing else is going to fetch it.
    """
    try:
        response = await http.get(
            f"{GRAPH}/{api_version}/{video_id}",
            params={"fields": "status,permalink_url"},
            headers=_auth(token),
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(
            f"Could not read the Reel's status: {exc}", video_created=True
        ) from exc

    code, message = _api_error(response)
    if code:
        raise PublishError(
            f"Status check refused: {code} {message}".strip(), video_created=True, code=code
        )

    payload = _json(response)
    status = payload.get("status") or {}
    publishing = status.get("publishing_phase") or {} if isinstance(status, dict) else {}
    return (
        str(status.get("video_status") or "") if isinstance(status, dict) else "",
        str(publishing.get("publish_status") or ""),
        permalink_of(str(payload.get("permalink_url") or "")),
    )


def permalink_of(raw: str) -> str:
    """Meta returns `permalink_url` as a site-relative path on a video node.

    Prefixed here rather than stored raw, because the panel renders it as an
    href and `/thenightlybuild/videos/123/` in an href resolves against the
    gateway's own host, which is a link to a 404 that looks like a link to the
    post.
    """
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://www.facebook.com{raw}"


async def await_published(
    http: httpx.AsyncClient,
    *,
    video_id: str,
    token: str,
    api_version: str,
    poll_interval_s: float = 5,
    timeout_s: float = 300,
) -> PublishResult:
    """Poll until the Reel is actually live, or until it is not going to be.

    `finish_upload` returning `{"success": true}` means the request was
    accepted, not that anybody can see a Reel: transcoding happens afterwards
    and can fail on its own. Every other publisher here waits for the platform
    to say the post exists, and one that reported success on an accepted
    request would be the only one whose "published" meant something weaker.

    A timeout is terminal rather than retried. The video may be seconds from
    going live and starting again would publish it twice.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        last, publish_state, permalink = await video_status(
            http, video_id=video_id, token=token, api_version=api_version
        )
        if publish_state == PUBLISH_PUBLISHED:
            return PublishResult(video_id=video_id, permalink=permalink or None)
        if publish_state == PUBLISH_ERROR:
            raise PublishError(
                f"Meta failed to publish {video_id}", video_created=True
            )
        if last in (STATUS_UPLOAD_FAILED, STATUS_EXPIRED):
            raise PublishError(
                f"Meta failed the upload for {video_id}: {last}", video_created=True
            )
        await asyncio.sleep(poll_interval_s)

    raise PublishError(
        f"Gave up waiting for {video_id} after {timeout_s:.0f}s, last status "
        f"{last or 'unknown'}",
        video_created=True,
    )


# --- What came back ----------------------------------------------------------
#
# **No retention metric, and one absence that is not obvious.** Facebook
# reports average time watched and total view time, so this board has watch
# time where TikTok has none. What it does not have is a three second skip, and
# `post_video_avg_time_watched` scores the whole Reel including replays, which
# is YouTube's `averageViewPercentage` problem in different units. So it is
# stored, shown, and never fed to the prompt that writes tomorrow's script.
#
# **Comments and shares arrive as one number** and are therefore not stored as
# either. `post_video_social_actions` is documented as comments plus shares
# together, and splitting it by subtracting a separately fetched comment count
# would be arithmetic on two differently defined metrics. The comment count is
# read from the node's own edge, which is exact, and shares stay 0 and mean
# "not measured here", the way `reach` does on a TikTok row.

# Requested by name. A metric Meta drops fails the whole call rather than
# returning fewer rows, so this list is short and every entry is one the docs
# name as supported on Reels.
INSIGHT_METRICS = (
    # Plays after an impression is already counted, excluding replays. The
    # nearest thing to a view, and the number the Page's own UI shows.
    "blue_reels_play_count",
    # People who saw it at least once, whether or not they played it. Meta is
    # the only platform of the four that reports this, on both its surfaces.
    "post_impressions_unique",
    # Milliseconds, and it includes replays, so it can exceed the video length
    # exactly as a looping Short does on YouTube.
    "post_video_avg_time_watched",
    "post_video_view_time",
    # A breakdown by reaction type rather than a count, which is why it is
    # summed below instead of read.
    "post_video_likes_by_reaction_type",
)


@dataclass(frozen=True)
class Reading:
    """One Reel's numbers, in the columns `insights` already has."""

    video_id: str
    views: int = 0
    reach: int = 0
    likes: int = 0
    comments: int = 0
    avg_watch_ms: int = 0
    total_watch_ms: int = 0
    permalink: str = ""
    # Every metric Meta answered with, before mapping. Kept for the log line
    # that says a metric stopped being returned, which is otherwise a column
    # that quietly goes to zero.
    raw: dict[str, Any] = field(default_factory=dict)


def _metric_value(item: Any) -> Any:
    """The first value out of Meta's insights envelope.

    Every metric comes back as `{"name": ..., "values": [{"value": ...}]}`,
    where the value is a number for most and a dict keyed by reaction type for
    one of them. Returned as it is and interpreted by the caller, because
    flattening the dict here would hide which metric is the odd one.
    """
    values = item.get("values") if isinstance(item, dict) else None
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return first.get("value")
    return None


def parse_reading(video_id: str, payload: dict) -> Reading:
    """Turn one node response into a row, or raise if it is not one.

    Missing insights are normal and are not an error: Meta has nothing for a
    Reel published minutes ago, exactly as `graph.media_insights` finds for an
    Instagram one. A *missing insights key* is different from an empty one and
    is not treated as a zero, because that is what a renamed field looks like
    and a column that silently goes to zero is the failure this whole file is
    written to avoid.
    """
    if "video_insights" not in payload:
        raise PublishError(
            f"{video_id}: the response carried no video_insights. Either the "
            f"field expansion was rejected or the metric names have moved."
        )

    rows = (payload.get("video_insights") or {}).get("data") or []
    by_name = {str(item.get("name") or ""): _metric_value(item) for item in rows}

    def count(name: str) -> int:
        value = by_name.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    reactions = by_name.get("post_video_likes_by_reaction_type")
    likes = (
        sum(int(v) for v in reactions.values() if isinstance(v, (int, float)))
        if isinstance(reactions, dict)
        else count("post_video_likes_by_reaction_type")
    )

    comments = ((payload.get("comments") or {}).get("summary") or {}).get("total_count")

    return Reading(
        video_id=video_id,
        views=count("blue_reels_play_count"),
        reach=count("post_impressions_unique"),
        likes=likes,
        comments=int(comments) if isinstance(comments, (int, float)) else 0,
        avg_watch_ms=count("post_video_avg_time_watched"),
        total_watch_ms=count("post_video_view_time"),
        permalink=permalink_of(str(payload.get("permalink_url") or "")),
        raw=by_name,
    )


class InsightsError(RuntimeError):
    """A read that failed. Separate from `PublishError` because nothing was
    being created and the caller's only decision is whether to keep sweeping."""

    def __init__(self, message: str, *, is_auth: bool = False):
        super().__init__(message)
        self.is_auth = is_auth


async def read_insights(
    http: httpx.AsyncClient,
    *,
    video_id: str,
    token: str,
    api_version: str,
) -> Reading:
    """One Reel's numbers, in one request.

    Field expansion rather than the `/video_insights` edge plus a second call
    for the comment count, because the two halves are always wanted together
    and a sweep that made two requests per post would double the calls to
    answer one question.
    """
    fields = (
        "permalink_url,"
        "comments.summary(true).limit(0),"
        f"video_insights.metric({','.join(INSIGHT_METRICS)})"
    )
    try:
        response = await http.get(
            f"{GRAPH}/{api_version}/{video_id}",
            params={"fields": fields},
            headers=_auth(token),
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise InsightsError(f"Could not read insights for {video_id}: {exc}") from exc

    code, message = _api_error(response)
    if code:
        # 190 is every expired, revoked and invalidated token. It will fail the
        # same way for every remaining post, so the caller stops rather than
        # spending the rest of the sweep proving it.
        raise InsightsError(
            f"Insights for {video_id} refused: {code} {message}".strip(),
            is_auth=code.split("/")[0] == "190",
        )

    try:
        return parse_reading(video_id, _json(response))
    except PublishError as exc:
        raise InsightsError(str(exc)) from exc
