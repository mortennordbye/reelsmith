"""The public pages: a description of the account, a privacy policy, terms.

Three static pages, and they exist because every platform this service posts
to demands a privacy policy URL, a terms of service URL and an official website
before it will accept an application. TikTok adds a condition the others did
not: the URLs must sit on a domain proved by DNS record, so a GitHub blob URL
cannot be used however readable it is. `gate.nordbye.it` is already the host
TikTok pulls media from, so serving them here means one verification covers the
media and the documents together.

**Deliberately on a router of its own, mounted unconditionally.** The obvious
place was `admin.public`, which already serves the one page that cannot require
a login. But that router is only included when `GATEWAY_ADMIN_ENABLED` is on,
and turning the panel off would then quietly 404 the URLs a platform has on
file for the app. A legal page that disappears with a feature flag is worse
than one nobody reads.

**The templates are the only copy.** These began as `docs/privacy.md` and
`docs/terms.md`, which the image cannot see because the Dockerfile copies
`gateway/` alone. Rendering markdown at runtime would mean a parser in an image
that deliberately carries no dependency it does not import, and keeping both
would mean two versions of a privacy policy drifting apart. Those two files now
point here instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

# Its own Jinja environment rather than importing `admin.templates`, so these
# pages do not depend on a module that may not be mounted.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse, name="index_page")
async def index(request: Request) -> Any:
    """What this account is, for somebody who arrived from an app listing."""
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/privacy", response_class=HTMLResponse, name="privacy_page")
async def privacy(request: Request) -> Any:
    return templates.TemplateResponse(request, "privacy.html", {})


@router.get("/terms", response_class=HTMLResponse, name="terms_page")
async def terms(request: Request) -> Any:
    return templates.TemplateResponse(request, "terms.html", {})
