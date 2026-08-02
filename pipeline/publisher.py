"""Step 6 -- get the finished video onto Instagram.

Two paths out of here, and the difference is whether a human looks at the video
first.

**Automatic.** `publish_reel()` publishes `out.mp4` to the account. Three facts
shape it:

- **Meta fetches the video, it is never pushed.** The container needs a public
  `video_url`. This file used to claim `upload_type=resumable` let the MP4 go
  up as raw bytes, which is true only for Facebook Login for Business; on the
  Instagram Login path the API answers "The parameter video_url is required".
  The claim survived because the first posts were made by hand, so the code
  had never actually run. The gateway is what hosts the file.
- App Review is only for Advanced Access, meaning acting on accounts you do not
  own. Standard Access is automatic and covers any account holding a role on
  your app, so publishing to your own account needs an app in development mode
  and nothing else.
- The rate limit is 100 published posts per rolling 24 hours. One a day is not
  near it.

**Manual.** `copy_to_clipboard()` and `reveal()` put the caption on the
clipboard and open the run folder, leaving a drag and a paste. This is still the
default, because posting automatically also removes the only step where anyone
reads the script before it is public.

Everything in the manual path is best-effort: a failed clipboard write must
never fail a run that already produced a video. Everything in the automatic path
raises, because a publish that half-worked is worth stopping on.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from config import Settings
from pipeline.renderer import COVER_FRAME

log = logging.getLogger(__name__)

# Terminal states from GET /<container-id>?fields=status_code. IN_PROGRESS is
# the only one worth waiting on.
_STATUS_DONE = "FINISHED"
_STATUS_WAIT = "IN_PROGRESS"

# Meta's code for "this token is no longer valid", which is the one failure
# here that a retry can never fix and a human has to go and undo in a browser.
_OAUTH_ERROR_CODE = 190


class PublishError(RuntimeError):
    """A publish that did not complete. The message is meant to be shown as-is."""


@dataclass(frozen=True)
class PublishResult:
    media_id: str
    permalink: str | None = None


# ---------------------------------------------------------------------------
# Token storage
#
# Long-lived tokens last 60 days and are refreshed, not reissued, so the current
# one has to be written back somewhere. That somewhere is data/ig_token.json
# rather than .env: a cron job that rewrites a hand-edited dotenv will eventually
# eat a comment or a line someone cared about.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenState:
    access_token: str
    expires_at: datetime | None
    refreshed_at: datetime | None

    @property
    def days_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(UTC)).total_seconds() / 86_400


def load_token(cfg: Settings) -> TokenState:
    """The token to use now: the stored one, or the .env seed on first run."""
    path = cfg.ig_token_path
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise PublishError(f"{path} is unreadable ({exc}). Delete it and re-seed.") from exc
        token = raw.get("access_token", "")
        if token:
            return TokenState(
                access_token=token,
                expires_at=_parse_dt(raw.get("expires_at")),
                refreshed_at=_parse_dt(raw.get("refreshed_at")),
            )

    if not cfg.ig_access_token:
        raise PublishError(
            "No Instagram token. Put a long-lived token in IG_ACCESS_TOKEN, then run "
            "`python main.py --refresh-token` to move it into data/ig_token.json."
        )
    # Seeded from .env, so the expiry is whatever Meta issued it with and we
    # have no way to know. The first refresh fills it in.
    return TokenState(access_token=cfg.ig_access_token, expires_at=None, refreshed_at=None)


def save_token(cfg: Settings, token: str, expires_in_s: int | None) -> TokenState:
    now = datetime.now(UTC)
    state = TokenState(
        access_token=token,
        expires_at=now + timedelta(seconds=expires_in_s) if expires_in_s else None,
        refreshed_at=now,
    )
    payload = {
        "access_token": state.access_token,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        "refreshed_at": now.isoformat(),
    }
    path = cfg.ig_token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    # Not a secret-manager, but no reason for it to be world-readable either.
    path.chmod(0o600)
    return state


def refresh_token(cfg: Settings, *, client: httpx.Client | None = None) -> TokenState:
    """Exchange the current long-lived token for a fresh 60 days.

    Refusable by Meta if the token is under 24 hours old, which is not an error
    worth acting on -- a token that new has 59 days left.
    """
    current = load_token(cfg)
    with _client(client, timeout=30) as http:
        data = _graph_json(
            http,
            "GET",
            f"{cfg.ig_graph_host.rstrip('/')}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": current.access_token},
        )

    token = data.get("access_token")
    if not token:
        raise PublishError(f"Refresh returned no access_token: {data}")
    state = save_token(cfg, token, data.get("expires_in"))
    log.info(
        "Instagram token refreshed, %s days left",
        f"{state.days_left:.0f}" if state.days_left is not None else "?",
    )
    return state


def refresh_token_if_due(cfg: Settings) -> TokenState | None:
    """Refresh only when the token is inside the margin. Returns None if it is not.

    Meant for the daily job, where a no-op most days is the point. An unknown
    expiry counts as due: that is the state a freshly seeded token is in, and
    refreshing it is how it gets a known one.
    """
    if not cfg.ig_user_id and not cfg.ig_token_path.exists():
        return None
    try:
        current = load_token(cfg)
    except PublishError:
        return None

    left = current.days_left
    if left is not None and left > cfg.ig_refresh_margin_days:
        log.debug("Instagram token has %.0f days left, not refreshing", left)
        return None
    if left is not None and left <= 0:
        raise PublishError(
            "The Instagram token has expired and can no longer be refreshed. "
            "Re-authorise in the Meta app dashboard and re-seed IG_ACCESS_TOKEN."
        )
    return refresh_token(cfg)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def publish_reel(
    video_path: Path,
    caption: str,
    cfg: Settings,
    *,
    video_url: str | None = None,
    cover_url: str | None = None,
    client: httpx.Client | None = None,
) -> PublishResult:
    """Publish `video_path` as a Reel. Returns the published media.

    Three calls: create a container pointing at a public `video_url`, wait for
    Meta to fetch and transcode it, publish. The wait is the slow part and
    there is no callback, so it is a poll.

    **Meta pulls the file, this never pushes it.** `upload_type=resumable`,
    which does take raw bytes, is documented as Facebook Login for Business
    only; on the Instagram Login path `graph.instagram.com` answers "The
    parameter video_url is required". This file used to claim otherwise and was
    wrong, which went unnoticed because the first posts were made by hand.

    So both the video and the cover need somewhere public to live, and that is
    what the gateway is for. Without a `cover_url` the thumbnail falls back to
    `thumb_offset`; without a `video_url` there is no publish at all.
    """
    if not video_path.exists():
        raise PublishError(f"No video at {video_path}")
    if not cfg.ig_user_id:
        raise PublishError("IG_USER_ID is not set.")

    size = video_path.stat().st_size
    token = load_token(cfg).access_token
    base = f"{cfg.ig_graph_base}/{cfg.ig_user_id}"

    if not video_url:
        raise PublishError(
            "Publishing a Reel needs a public video_url, and none was provided.\n"
            "Meta fetches the file from its own servers on this API path: "
            "upload_type=resumable is documented as Facebook Login for Business only, "
            "and graph.instagram.com rejects it with 'The parameter video_url is required'.\n"
            "Set GATEWAY_URL and GATEWAY_TOKEN so the pipeline can host the MP4, "
            "or pass a URL yourself."
        )

    params: dict[str, str] = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption.strip(),
    }
    if cover_url:
        params["cover_url"] = cover_url
    else:
        params["thumb_offset"] = str(round(COVER_FRAME / cfg.fps * 1000))

    with _client(client, timeout=cfg.ig_upload_timeout_s) as http:
        log.info("Creating Reels container (%.1f MB, fetched from %s)", size / 1_048_576, video_url)
        created = _graph_json(http, "POST", f"{base}/media", token=token, data=params)
        container_id = created.get("id")
        if not container_id:
            raise PublishError(f"Container creation returned no id: {created}")

        _await_container(http, cfg, container_id, token)

        log.info("Publishing container %s", container_id)
        published = _graph_json(
            http, "POST", f"{base}/media_publish", token=token, data={"creation_id": container_id}
        )
        media_id = published.get("id")
        if not media_id:
            raise PublishError(f"media_publish returned no id: {published}")

        return PublishResult(media_id=media_id, permalink=_permalink(http, cfg, media_id, token))




def _await_container(http: httpx.Client, cfg: Settings, container_id: str, token: str) -> None:
    """Poll until Meta has finished transcoding, or give up loudly."""
    deadline = time.monotonic() + cfg.ig_publish_timeout_s
    seen = ""
    while time.monotonic() < deadline:
        data = _graph_json(
            http,
            "GET",
            f"{cfg.ig_graph_base}/{container_id}",
            token=token,
            params={"fields": "status_code,status"},
        )
        status = data.get("status_code", "")
        if status != seen:
            log.info("Container %s: %s", container_id, status or "?")
            seen = status
        if status == _STATUS_DONE:
            return
        if status and status != _STATUS_WAIT:
            # ERROR and EXPIRED both land here. `status` carries Meta's prose
            # reason, which is the only thing that says which one it was.
            raise PublishError(f"Container {container_id} is {status}: {data.get('status', '')}")
        time.sleep(cfg.ig_poll_interval_s)

    raise PublishError(
        f"Container {container_id} was still processing after {cfg.ig_publish_timeout_s}s. "
        f"It stays valid for 24 hours, so `--publish` on the same run will pick it up."
    )


def list_media(cfg: Settings, *, limit: int = 50, client: httpx.Client | None = None) -> list[dict]:
    """Everything live on the account, newest first.

    The one call that does not start from something this machine already knows.
    Every other path here works forwards, from a run folder to a media id; this
    one works backwards, because a Reel posted by hand through the app left no
    run folder pointing at it and Meta is the only party that remembers both.

    The caption comes along because it is the join. The gateway holds media ids
    and numbers, the build folder holds hooks, and the caption is the only
    string that was ever written into both.
    """
    if not cfg.ig_user_id:
        raise PublishError("IG_USER_ID is not set.")

    token = load_token(cfg).access_token
    with _client(client, timeout=30) as http:
        data = _graph_json(
            http,
            "GET",
            f"{cfg.ig_graph_base}/{cfg.ig_user_id}/media",
            token=token,
            params={
                "fields": "id,caption,timestamp,permalink,media_product_type",
                "limit": str(limit),
            },
        )
    items = data.get("data")
    return list(items) if isinstance(items, list) else []


def _permalink(http: httpx.Client, cfg: Settings, media_id: str, token: str) -> str | None:
    """The post's URL, for the log line. Never worth failing a publish over."""
    try:
        data = _graph_json(
            http,
            "GET",
            f"{cfg.ig_graph_base}/{media_id}",
            token=token,
            params={"fields": "permalink"},
        )
    except PublishError as exc:
        log.debug("Could not read permalink (%s)", exc)
        return None
    return data.get("permalink")


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _client(
    existing: httpx.Client | None, *, timeout: float
) -> AbstractContextManager[httpx.Client]:
    """Use the caller's client if given, otherwise own one and close it.

    nullcontext is what keeps a borrowed client open: closing someone else's
    client on the way out would break the second call that shares it. The
    injection point is also what lets the tests drive this over a mock
    transport instead of the network.
    """
    if existing is not None:
        return nullcontext(existing)
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _graph_json(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = http.request(method, url, headers=headers, params=params, data=data)
    except httpx.HTTPError as exc:
        raise PublishError(f"{method} {url} failed: {exc}") from exc

    if resp.status_code >= 400:
        raise PublishError(_error_message(resp))
    try:
        body = resp.json()
    except ValueError as exc:
        raise PublishError(f"{method} {url} returned non-JSON: {resp.text[:300]}") from exc
    return body if isinstance(body, dict) else {"data": body}


def _error_message(resp: httpx.Response) -> str:
    """Turn a Graph error envelope into one readable line."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"

    if not err:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"

    message = err.get("message", "unknown error")
    if err.get("code") == _OAUTH_ERROR_CODE:
        return (
            f"{message}\n"
            "The token is no longer valid. Long-lived tokens die after 60 days without a "
            "refresh, and an expired one cannot be refreshed -- re-authorise in the Meta "
            "app dashboard and re-seed IG_ACCESS_TOKEN."
        )
    parts = [message]
    if sub := err.get("error_user_msg"):
        parts.append(sub)
    if trace := err.get("fbtrace_id"):
        parts.append(f"(fbtrace_id {trace})")
    return " ".join(parts)


def _parse_dt(value: str | None) -> datetime | None:
    """Read a stored timestamp, tolerating one someone edited by hand.

    Forced to UTC because a naive value would raise on the first comparison
    against `now`, and the file is plain JSON that invites editing.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# The manual path: caption on the clipboard, folder open, drag the file.
# ---------------------------------------------------------------------------


def copy_to_clipboard(text: str) -> bool:
    """Put `text` on the system clipboard. Returns whether it worked."""
    if not text:
        return False

    system = platform.system()
    if system == "Darwin":
        cmd = ["pbcopy"]
    elif system == "Linux":
        # Wayland first: on a Wayland session xclip talks to an X server that
        # may not exist, and fails in a way that is confusing to debug.
        cmd = _first_available(["wl-copy"], ["xclip", "-selection", "clipboard"])
        if cmd is None:
            log.debug("No clipboard tool found (tried wl-copy, xclip).")
            return False
    else:
        log.debug("No clipboard support for platform %s.", system)
        return False

    try:
        subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, input=text, text=True, check=True, timeout=10, capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Clipboard copy failed (%s)", exc)
        return False
    return True


def reveal(path: Path) -> bool:
    """Open `path` in the desktop file manager. Returns whether it worked.

    Given a file, reveal it selected inside its folder rather than opening it.
    The next action is dragging the MP4 into Instagram, so a Finder window with
    the file already highlighted saves a step; `open` without -R would launch
    QuickTime instead, which is not what anyone wants here.
    """
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]
    elif system == "Linux":
        # xdg-open has no reveal equivalent, so fall back to the parent folder.
        target = path.parent if path.is_file() else path
        cmd = ["xdg-open", str(target)]
    elif system == "Windows":
        cmd = ["explorer", f"/select,{path}"] if path.is_file() else ["explorer", str(path)]
    else:
        return False

    try:
        subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, check=True, timeout=10, capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Could not open %s (%s)", path, exc)
        return False
    return True


def _first_available(*candidates: list[str]) -> list[str] | None:
    from shutil import which

    return next((c for c in candidates if which(c[0])), None)
