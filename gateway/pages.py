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

`/tiktok/callback` and `/facebook/callback` sit here for the same reason the
other three do, which is that they have to answer whether or not the admin
panel is on. They are the OAuth redirect targets `scripts/tiktok_authorise.py`
and `scripts/facebook_authorise.py` send a browser to, and they exist because
**neither platform will register a redirect URI that is not https**, so a
loopback listener has nowhere to listen. They render the authorisation code and
nothing else happens here; the exchange is done by the script, with the client
secret, on the operator's own machine.

**Two routes and one template.** The wording is all that differs between them,
so it is a parameter rather than a second page drifting from the first. Two
routes rather than one shared `/oauth/callback`, because each platform has its
own URL on file in its own developer portal, and a URL a platform holds is not
something to consolidate later.
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


def _callback(request: Request, *, platform: str, script: str) -> Any:
    """Show the authorisation code so it can be pasted back into the terminal.

    **It deliberately does not exchange the code.** The exchange needs the
    client secret, and this service is never told it until the account is
    registered, which is the last step of the trip this page is in the middle
    of. Handing the secret to the gateway early to save one paste would put it
    in a second place for the sake of a one-off.

    A stranger reaching either URL sees a page saying there is nothing here.
    The code in the query string is the visitor's own, so rendering it back
    tells nobody anything they did not already have.
    """
    query = request.query_params
    return templates.TemplateResponse(
        request,
        "oauth_callback.html",
        {
            "platform": platform,
            "script": script,
            "code": query.get("code", ""),
            "state": query.get("state", ""),
            "error": query.get("error", ""),
            "error_description": query.get("error_description", ""),
        },
    )


@router.get("/tiktok/callback", response_class=HTMLResponse, name="tiktok_callback")
async def tiktok_callback(request: Request) -> Any:
    return _callback(
        request, platform="TikTok", script="scripts/tiktok_authorise.py"
    )


@router.get("/facebook/callback", response_class=HTMLResponse, name="facebook_callback")
async def facebook_callback(request: Request) -> Any:
    """The Page consent trip's landing page.

    Facebook Login does allow a `localhost` redirect while an app is in
    development, and this deliberately does not rely on that: the app that
    publishes the Reels is live, the allowance is one Meta has narrowed before,
    and the page next door already exists.
    """
    return _callback(
        request, platform="Facebook", script="scripts/facebook_authorise.py"
    )
