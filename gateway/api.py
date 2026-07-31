"""What the Mac talks to, plus the two routes that answer to nobody.

The pipeline calls `/api/covers` and `/api/posts` at publish time and both are
best effort on its side: a gateway that is down must never fail a publish that
already produced a video.

`/covers/<name>.png` is the only reason this service serves files. Meta fetches
`cover_url` once, at container creation, from its own servers, which is why a
local path cannot work and why the route needs no auth. `/healthz` and
`/metrics` are for the cluster.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response

from gateway import db
from gateway.graph import GraphError
from gateway.models import AccountRegistration, CoverUploaded, PostRegistration, Registered

log = logging.getLogger(__name__)

router = APIRouter()

# Anything outside this cannot become part of a path on disk.
_SAFE_NAME = re.compile(r"[^a-z0-9-]+")
_MAX_COVER_BYTES = 8 * 1024 * 1024

# Video is the reason this service hosts anything at all beyond covers.
# `upload_type=resumable` is documented as Facebook Login only, so on the
# Instagram Login path Meta will not take the MP4 as bytes: it fetches
# `video_url` from a public server. This is that server.
_MAX_VIDEO_BYTES = 300 * 1024 * 1024
_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4"}

# Meta fetches the file once, while the container is being created. Keeping it
# much beyond that only fills the volume, which is 1Gi and shared with the
# database. A week is generous for retries and a republish.
_MEDIA_TTL_DAYS = 7


async def require_token(request: Request) -> None:
    """Bearer auth for everything the Mac calls.

    Compared in constant time out of habit rather than need; the cost is nil and
    the alternative is a timing oracle on a shared secret.
    """
    expected = request.app.state.cfg.api_token
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


def safe_cover_name(slug: str, payload: bytes) -> str:
    """`slug-<digest>.png`, with the digest making a re-upload idempotent.

    The digest also means two runs of the same repo on different days do not
    fight over one filename, and a cover already fetched by Meta is never
    overwritten under it.
    """
    cleaned = _SAFE_NAME.sub("-", slug.lower()).strip("-") or "cover"
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"{cleaned[:60]}-{digest}.png"


@router.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    return Response(
        content=request.app.state.metrics.export(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.post("/api/posts", response_model=Registered, dependencies=[Depends(require_token)])
async def register_post(request: Request, body: PostRegistration) -> Registered:
    await db.register_post(
        request.app.state.db,
        media_id=body.media_id,
        ig_user_id=body.ig_user_id,
        keyword=body.keyword,
        link=body.link,
    )
    log.info("Watching %s for the keyword %r", body.media_id, body.keyword)
    return Registered(detail=f"watching {body.media_id}")


@router.post("/api/accounts", response_model=Registered, dependencies=[Depends(require_token)])
async def register_account(request: Request, body: AccountRegistration) -> Registered:
    app = request.app
    expires_at = db.now() + timedelta(seconds=body.expires_in) if body.expires_in else None
    await db.upsert_account(
        app.state.db,
        ig_user_id=body.ig_user_id,
        access_token=body.access_token,
        username=body.username,
        expires_at=expires_at,
    )

    detail = "stored"
    if body.subscribe:
        # Without this the account produces no webhooks at all, and the failure
        # looks exactly like "nobody is messaging us".
        try:
            await app.state.graph.subscribe_messages(token=body.access_token)
            detail = "stored and subscribed to messages"
        except GraphError as exc:
            log.warning("subscribed_apps failed for %s: %s", body.ig_user_id, exc)
            detail = f"stored, but the messages subscription failed ({exc})"
    return Registered(detail=detail)


@router.post("/api/covers", response_model=CoverUploaded, dependencies=[Depends(require_token)])
async def upload_cover(
    request: Request,
    file: Annotated[UploadFile, File()],
    slug: Annotated[str, Form()] = "cover",
) -> CoverUploaded:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(payload) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="cover is too large")

    cfg = request.app.state.cfg
    name = safe_cover_name(slug, payload)
    covers_dir = Path(cfg.covers_dir)
    covers_dir.mkdir(parents=True, exist_ok=True)
    (covers_dir / name).write_bytes(payload)
    return CoverUploaded(name=name, url=f"{cfg.public_base_url}/covers/{name}")


@router.get("/covers/{name}")
async def serve_cover(request: Request, name: str) -> FileResponse:
    # Rebuilt from the basename rather than trusted, so no combination of dots
    # and slashes reaches a parent directory.
    cleaned = Path(name).name
    path = Path(request.app.state.cfg.covers_dir) / cleaned
    if not cleaned.endswith(".png") or not path.is_file():
        raise HTTPException(status_code=404, detail="no such cover")
    return FileResponse(path, media_type="image/png")


def _prune_media(directory: Path) -> int:
    """Drop anything older than the TTL. Returns how many went.

    Called on upload rather than on a timer: uploads are the only thing that
    grows this directory, so that is exactly when it needs bounding, and it
    saves a background task that could fail silently.
    """
    cutoff = db.now() - timedelta(days=_MEDIA_TTL_DAYS)
    removed = 0
    for path in directory.glob("*"):
        try:
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # a file vanishing under us is not an error
            log.debug("Could not prune %s: %s", path, exc)
    return removed


@router.post("/api/media", response_model=CoverUploaded, dependencies=[Depends(require_token)])
async def upload_media(
    request: Request,
    file: Annotated[UploadFile, File()],
    slug: Annotated[str, Form()] = "media",
) -> CoverUploaded:
    """Host a file Meta has to fetch, and hand back the URL it should fetch.

    Covers and videos both come through here. The video is the load bearing
    one: without a public URL there is no way to publish a Reel on the
    Instagram Login path at all.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _MEDIA_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported type {suffix!r}, expected one of {sorted(_MEDIA_TYPES)}",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty upload")
    limit = _MAX_VIDEO_BYTES if suffix == ".mp4" else _MAX_COVER_BYTES
    if len(payload) > limit:
        raise HTTPException(status_code=413, detail=f"{suffix} larger than {limit} bytes")

    cfg = request.app.state.cfg
    media_dir = Path(cfg.covers_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    pruned = _prune_media(media_dir)
    if pruned:
        log.info("Pruned %d media files past %d days", pruned, _MEDIA_TTL_DAYS)

    name = safe_cover_name(slug, payload).removesuffix(".png") + suffix
    (media_dir / name).write_bytes(payload)
    log.info("Hosting %s (%.1f MB)", name, len(payload) / 1_048_576)
    return CoverUploaded(name=name, url=f"{cfg.public_base_url}/media/{name}")


@router.get("/media/{name}")
async def serve_media(request: Request, name: str) -> FileResponse:
    cleaned = Path(name).name
    media_type = _MEDIA_TYPES.get(Path(cleaned).suffix.lower())
    path = Path(request.app.state.cfg.covers_dir) / cleaned
    if media_type is None or not path.is_file():
        raise HTTPException(status_code=404, detail="no such media")
    return FileResponse(path, media_type=media_type)
