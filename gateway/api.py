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
from datetime import timedelta
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
