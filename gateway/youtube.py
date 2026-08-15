"""Publishing a Short, which is the opposite shape to publishing a Reel.

`gateway/publisher.py` exists because **Meta fetches the video**: the container
is handed a public URL and Meta pulls the bytes from its own servers, which is
the whole reason this service hosts `/media/*` at all. **YouTube takes pushed
bytes.** Three calls, no public URL, no hosting, and the file goes up from disk.

Every docstring in this repo that says the video is fetched and never pushed is
describing Meta specifically.

**The session URI is this file's container id.** `publisher.PublishError`
divides failures by whether a container exists, because after that point a Reel
may be live and no error text proves otherwise. The same line lives here and in
the same place: before a resumable session exists Google was never asked to make
anything, so a retry is provably safe. Once it does, a video may exist, and the
only safe move is to stop and let a person look.

Raw httpx rather than `google-api-python-client`, which is synchronous and would
need a thread pool inside a service that is async end to end, in exchange for
wrapping three documented calls. `scripts/youtube_authorise.py` makes the
opposite call for the browser consent flow, and for the opposite reason: that
one runs once per channel on a laptop, and hand-rolling a loopback listener with
PKCE would be reimplementing a solved problem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
WATCH_URL = "https://www.youtube.com/watch?v="

# YouTube's own limits. Both are enforced here rather than trusted, because the
# API rejects the whole upload for either and the bytes are already on the wire
# by then.
MAX_TITLE = 100
MAX_DESCRIPTION = 5000

# Science & Technology. Category is required and there is no "unset".
CATEGORY_SCIENCE_AND_TECHNOLOGY = "28"


class UploadError(RuntimeError):
    """An upload that did not complete.

    `session_created` is the field the caller acts on, not the message. False
    means Google was never asked to make anything and the slot can be handed
    back. True means a video may exist, and only a human should decide.
    """

    def __init__(self, message: str, *, session_created: bool = False):
        super().__init__(message)
        self.session_created = session_created


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    privacy_status: str
    session_uri: str | None = None

    @property
    def url(self) -> str:
        return f"{WATCH_URL}{self.video_id}"


def _clean(text: str, *, limit: int, field: str) -> str:
    """Trim to the limit and drop the two characters YouTube refuses.

    Angle brackets fail the whole insert with `invalidVideoMetadata`, which
    names neither the field nor the character. Truncating rather than raising
    because the hook is already capped well inside the title limit upstream, so
    reaching either bound means something odd rather than something fatal.
    """
    cleaned = text.replace("<", "").replace(">", "").strip()
    if len(cleaned) > limit:
        log.warning("Truncating %s from %d to %d characters", field, len(cleaned), limit)
        cleaned = cleaned[:limit].rstrip()
    return cleaned


async def access_token(
    http: httpx.AsyncClient, *, client_id: str, client_secret: str, refresh_token: str
) -> str:
    """Mint an access token from the refresh token.

    Minted per publish rather than cached. Access tokens last an hour, a slot
    fires a few times a day, and a cache would be two more columns plus a
    staleness check to save one HTTP call. Google refresh tokens do not expire
    on a clock, so unlike the Meta side there is no margin job behind this.
    """
    try:
        response = await http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise UploadError(f"Could not reach Google to mint a token: {exc}") from exc

    if response.status_code != 200:
        # `invalid_grant` here means the refresh token is dead: revoked, or
        # issued while the consent screen was still in Testing. Neither is
        # retryable and both need a person in a browser.
        raise UploadError(f"Token refresh failed ({response.status_code}): {response.text}")

    token = str(response.json().get("access_token") or "")
    if not token:
        raise UploadError("Token refresh returned no access token")
    return token


async def start_session(
    http: httpx.AsyncClient,
    *,
    token: str,
    title: str,
    description: str,
    size_bytes: int,
    privacy_status: str,
    contains_synthetic_media: bool,
    tags: list[str] | None = None,
    category_id: str = CATEGORY_SCIENCE_AND_TECHNOLOGY,
) -> str:
    """Ask for a resumable session. Returns the session URI.

    Nothing exists on YouTube after this call except an intent to receive
    bytes, so every failure up to here is safe to retry.
    """
    body = {
        "snippet": {
            "title": _clean(title, limit=MAX_TITLE, field="title"),
            "description": _clean(description, limit=MAX_DESCRIPTION, field="description"),
            "categoryId": category_id,
            "tags": tags or [],
        },
        "status": {
            "privacyStatus": privacy_status,
            # Required, and there is no safe default to leave it at. False is
            # the honest answer for a channel about developer tooling.
            "selfDeclaredMadeForKids": False,
            # A decision, not a default. See PROFILE.md: the voice is a clone of
            # a real person reading a script that person commissioned.
            "containsSyntheticMedia": contains_synthetic_media,
        },
    }
    try:
        response = await http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "authorization": f"Bearer {token}",
                "x-upload-content-length": str(size_bytes),
                "x-upload-content-type": "video/mp4",
            },
            json=body,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise UploadError(f"Could not open an upload session: {exc}") from exc

    if response.status_code != 200:
        raise UploadError(
            f"Upload session refused ({response.status_code}): {response.text}"
        )

    session_uri = response.headers.get("location", "")
    if not session_uri:
        raise UploadError("Upload session returned no Location header")
    return session_uri


async def _chunks(path: Path, size: int = 1024 * 1024):
    """Feed the file to httpx a megabyte at a time.

    An async client refuses a plain file handle, and reading the whole video
    into memory would hold up to the queue's 300 MB cap in a pod sized for a
    SQLite database.

    The reads themselves are blocking, which is a real if small cost: a
    megabyte off local disk is sub-millisecond, this runs a few times a day,
    and the alternative is another dependency to make the read async.
    """
    with path.open("rb") as handle:
        while chunk := handle.read(size):
            yield chunk


async def push_bytes(
    http: httpx.AsyncClient,
    *,
    session_uri: str,
    video_path: Path,
    size_bytes: int,
    timeout_s: float,
) -> UploadResult:
    """Send the file and read back the video resource.

    Streamed from disk rather than read into memory: the queue accepts files up
    to 300 MB and this runs in a pod sized for a SQLite database.

    Deliberately one PUT with no resume-from-offset. The protocol supports
    asking a half-finished session how many bytes it holds, but a partial upload
    and a completed-but-unacknowledged one are worth the same here: both mean a
    video may exist, and the rule this service runs on is that a human decides
    what happens next.
    """
    try:
        response = await http.put(
            session_uri,
            content=_chunks(video_path),
            headers={
                # Explicit, because httpx would otherwise send an async
                # iterator as chunked transfer encoding, and a resumable
                # upload done in one request needs a length.
                "content-type": "video/mp4",
                "content-length": str(size_bytes),
            },
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise UploadError(f"Upload failed mid-transfer: {exc}", session_created=True) from exc

    if response.status_code not in (200, 201):
        raise UploadError(
            f"Upload rejected ({response.status_code}): {response.text}",
            session_created=True,
        )

    data = response.json()
    video_id = str(data.get("id") or "")
    if not video_id:
        raise UploadError(f"Upload returned no video id: {data}", session_created=True)

    status = data.get("status") or {}
    return UploadResult(
        video_id=video_id,
        privacy_status=str(status.get("privacyStatus") or "unknown"),
        session_uri=session_uri,
    )


async def upload(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    video_path: Path,
    title: str,
    description: str,
    privacy_status: str,
    contains_synthetic_media: bool,
    tags: list[str] | None = None,
    timeout_s: float = 600,
) -> UploadResult:
    """The whole sequence: mint, open a session, push the bytes."""
    size_bytes = video_path.stat().st_size
    token = await access_token(
        http,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    session_uri = await start_session(
        http,
        token=token,
        title=title,
        description=description,
        size_bytes=size_bytes,
        privacy_status=privacy_status,
        contains_synthetic_media=contains_synthetic_media,
        tags=tags,
    )
    log.info("Upload session open for %s (%d bytes)", video_path.name, size_bytes)
    result = await push_bytes(
        http,
        session_uri=session_uri,
        video_path=video_path,
        size_bytes=size_bytes,
        timeout_s=timeout_s,
    )
    log.info("Uploaded %s as %s (%s)", video_path.name, result.video_id, result.privacy_status)
    return result
