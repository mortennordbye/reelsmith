"""The control panel, served by the same process as everything else.

Server-rendered Jinja over the tables this service already owns. No frontend
build, no bundler, no API layer between the page and the query.

**Organised by brand, not by table.** It was eight tabs, each stacking a board
per destination, which read fine for one identity on one platform and produced
a 32,000 pixel Posts page at two identities on seven. The shape now is:

- the portfolio: Today, Calendar, Destinations and System, one row per brand or
  per destination, which is what stays a page at fifty identities
- one brand: Overview, Schedule, Library, Performance, Subjects and Setup, at
  `/admin/b/<brand>/...`, where a video is one row and a destination is a chip

The brand is part of the path rather than a query string, so building a link
with `url_for` cannot silently drop it. What each page shows is computed in
`panel.py`; the routes here stay one call and a template.

**Plain forms, no HTMX.** The plan said HTMX and it would work, but every
control here is a state change followed by a full reload, which is what a form
POST already is. Adding a script tag would mean either a CDN fetch this
container cannot make or a vendored copy to keep updated, in exchange for
nothing the eye can see. Menus are `<details>` elements for the same reason.

**Authentication is this router's own problem, not only the ingress's.** The
homelab pattern is Authentik forward-auth at Traefik, and that is still the
intended front door, but this service is publicly reachable by necessity: Meta
fetches `/media/*` and posts to `/webhook` from its own servers, so there is no
network boundary to hide behind. A panel that can publish to a real account
cannot rely on an ingress rule someone might reorder.

So there are three states and no fourth:

- off (`GATEWAY_ADMIN_ENABLED=false`), the default
- on with `GATEWAY_ADMIN_TOKEN`, which this module checks itself
- on with `GATEWAY_ADMIN_TRUST_PROXY_AUTH=true`, an explicit statement that
  something in front is doing it

Enabled with neither is refused at startup by `config.require_admin_auth`,
because "I thought the ingress was handling it" is how these get exposed.

The token is held in a cookie that is HttpOnly, SameSite=Strict and Secure on
https. SameSite is the primary CSRF defence, since every control here is a form
POST; the Origin check in `_same_origin` is the belt to that pair of braces.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gateway import analysis, db, panel, schedule, scheduler

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Images that ship with the image rather than arriving at runtime, which is what
# separates them from `covers_dir`. An allowlist rather than a cleaned filename,
# because the set is two files and never grows from user input, so there is no
# traversal to defend against in the first place.
_ASSETS = Path(__file__).parent / "assets"
_ASSET_NAMES = frozenset({"boss-room.jpg", "boss-avatar.jpg"})

_COOKIE = "reelsmith_admin"
_FAILED_LOGIN_DELAY_S = 0.5

# What the Manager says. Picked by the date rather than at random, so a reload
# does not reshuffle it and two tabs open on the same morning agree.
_GREETINGS = (
    "Ready to contribute to the dead internet theory?",
    "The machine ran all night. Nobody watched a second of it.",
    "Another day, another repository explained to strangers.",
    "Everything is queued. Nothing is reviewed. This is fine.",
    "Four feeds, one voice, zero human oversight.",
    "The algorithm and I have an understanding.",
    "It is my turn on the Xbox, but I made the videos first.",
)


def _authenticated(request: Request) -> bool:
    cfg = request.app.state.cfg
    if cfg.admin_trust_proxy_auth:
        # Something in front vouched for this request. Said explicitly in
        # config, never inferred from a header, because every one of those is
        # attacker-settable on a service this exposed.
        return True
    presented = request.cookies.get(_COOKIE, "")
    if not presented:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        presented = value if scheme.lower() == "bearer" else ""
    return bool(presented) and hmac.compare_digest(presented, cfg.admin_token)


async def require_admin(request: Request) -> None:
    """Gate every page and every control.

    A browser gets the login page; anything else gets a 401. Redirecting an
    API caller to a form is how a broken script looks like a working one.
    """
    if _authenticated(request):
        return
    accepts = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accepts:
        raise HTTPException(
            status_code=303, headers={"location": str(request.url_for("login_page"))}
        )
    raise HTTPException(status_code=401, detail="admin authentication required")


def _same_origin(request: Request) -> bool:
    """Is this POST from our own page?

    SameSite=Strict already stops a cross-site form from carrying the cookie.
    This is the second lock, and it is the one that still works if a browser
    ever disagrees about what counts as same-site.
    """
    origin = request.headers.get("origin")
    if origin is None:
        # No Origin at all is a same-origin form post in some browsers, and a
        # curl call in the rest. The cookie check has already run either way.
        return True
    base = str(request.base_url).rstrip("/")
    configured = str(request.app.state.cfg.public_base_url).rstrip("/")
    return origin.rstrip("/") in {base, configured}


async def require_csrf(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


# Every route on `router` is authenticated and CSRF-checked by construction. A
# control added later inherits both without anyone remembering to ask, which is
# the only way this stays true as the panel grows.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin), Depends(require_csrf)])
# The login form is the one thing that cannot require a login.
public = APIRouter(prefix="/admin")


@public.get("/login", response_class=HTMLResponse, name="login_page")
async def login_page(request: Request) -> Any:
    if _authenticated(request):
        return RedirectResponse(str(request.url_for("dashboard_page")), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"cfg": request.app.state.cfg, "page": "login", "failed": False}
    )


@public.post("/login", response_class=HTMLResponse)
async def do_login(request: Request, token: Annotated[str, Form()] = "") -> Any:
    cfg = request.app.state.cfg
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request refused")

    if not cfg.admin_token or not hmac.compare_digest(token, cfg.admin_token):
        # Deliberately says nothing about which part was wrong, and is logged
        # so a stream of these is visible in the same place as everything else.
        log.warning("Failed admin login from %s", request.client.host if request.client else "?")
        # Not a lockout, which would let anyone shut the owner out of their own
        # panel. Just enough delay that guessing a 24 character token online is
        # not a strategy, and cheap enough that a flood of these costs a
        # coroutine rather than a thread.
        await asyncio.sleep(_FAILED_LOGIN_DELAY_S)
        return templates.TemplateResponse(
            request, "login.html", {"cfg": cfg, "page": "login", "failed": True},
            status_code=401,
        )

    response = RedirectResponse(str(request.url_for("dashboard_page")), status_code=303)
    response.set_cookie(
        _COOKIE,
        cfg.admin_token,
        max_age=cfg.admin_session_hours * 3600,
        httponly=True,
        # SameSite=Strict is the primary CSRF defence: a cross-site form POST
        # never carries this cookie, so every control below is unreachable from
        # another origin even before the Origin check runs.
        samesite="strict",
        secure=str(cfg.public_base_url).startswith("https://"),
        path="/admin",
    )
    return response


@public.post("/logout")
async def logout(request: Request) -> Any:
    response = RedirectResponse(str(request.url_for("login_page")), status_code=303)
    response.delete_cookie(_COOKIE, path="/admin")
    return response


def _fmt(when: datetime | None, tz: str = "UTC") -> str:
    if when is None:
        return "unscheduled"
    return when.astimezone(schedule.zone_or_utc(tz)).strftime("%a %d %b %H:%M")


def _ago(when: datetime | None) -> str:
    """Relative time, because "4 minutes ago" answers the question and a
    timestamp makes the reader do subtraction."""
    if when is None:
        return "never"
    seconds = (db.now() - when).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172_800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


def _until(when: datetime | None) -> str:
    """How long until something that has not happened yet.

    `ago` counts the other way and reports a future time as a negative number of
    seconds, which is how the dashboard first shipped a countdown reading
    "-56934s from now". Same wording as `ago` so the two read as a pair.
    """
    if when is None:
        return "not scheduled"
    seconds = (when - db.now()).total_seconds()
    if seconds <= 0:
        return "due now"
    if seconds < 5400:
        return f"in {int(seconds // 60)}m"
    if seconds < 172_800:
        hours, minutes = divmod(int(seconds // 60), 60)
        return f"in {hours}h {minutes}m"
    return f"in {int(seconds // 86_400)}d"


def _clock(when: datetime | None, tz: str = "UTC") -> str:
    """Just the time, for the places that set it large or inside a chip."""
    if when is None:
        return "--:--"
    return when.astimezone(schedule.zone_or_utc(tz)).strftime("%H:%M")


# One dry line per page, under its heading. Here rather than in the templates
# so adding a page means adding a line to one dict. Shown only in the loud look.
#
# **Nothing on the Library page may say reach, saves or skipped.** A YouTube
# brand reports none of the three, and `test_gateway_youtube_insights` asserts
# the words appear nowhere on that page, because a zero beside a metric a
# platform does not measure reads as a result. A joke does not earn an exception.
_QUIPS = {
    "today": "Everything is queued. Nothing is reviewed. This is fine.",
    "calendar": "Seven days of openings nobody has read yet.",
    "destinations": "Four platforms, one voice, zero human oversight.",
    "system": "Green means the machine is fine. It says nothing about the videos.",
    "schedule": "Cancelling one of these before its slot fires is the entire review process.",
    "library": "Every one of these was written, voiced, rendered and published while you slept.",
    "performance": "The format averages 30 to 40 percent. We are working on it.",
    "subjects": "Thirty days is how long a subject gets to forget about us.",
    "setup": "The jitter is derived, never rolled, so nothing here fires twice.",
}


def _quip_for(page: str) -> str:
    return _QUIPS.get(page, "")


# The two looks this panel comes in. `plain` is the one it is built around: the
# structure, the spacing and the tokens. `loud` is the same panel with the joke
# added on top, the wordmark in its frame, the room behind it, the manager and
# the quips, and it may only swap tokens and add decoration, never move a thing.
#
# A cookie rather than a setting on the service, because it is a preference of
# whoever is looking rather than a fact about the deployment, and two people
# can hold different ones. It is also the reason it needs no migration and
# cannot break publishing: the worst a bad value does is fall back to `loud`.
SKINS = ("loud", "plain")
SKIN_COOKIE = "skin"


def _skin_of(request: Request) -> str:
    """Which look this viewer has chosen, validated rather than trusted.

    Straight into a class name on `body`, so an unchecked cookie would be an
    attacker-controlled string in the markup. Anything unrecognised is `loud`,
    which is also what a first visit gets.
    """
    chosen = request.cookies.get(SKIN_COOKIE, "")
    return chosen if chosen in SKINS else SKINS[0]


templates.env.filters["fmt"] = _fmt
templates.env.filters["ago"] = _ago
templates.env.filters["until"] = _until
templates.env.filters["clock"] = _clock
templates.env.globals["quip_for"] = _quip_for
# Takes the request because a cookie is per viewer, unlike the other globals
# here, which are facts about the service.
templates.env.globals["skin_of"] = _skin_of
templates.env.globals["platform_list"] = panel.names
templates.env.globals["measured_columns"] = analysis.measured_columns
templates.env.globals["platforms"] = db.PLATFORMS

# Which route resolves an issue, keyed by the page an `Issue` names.
templates.env.globals["issue_routes"] = {
    "overview": "brand_page",
    "schedule": "schedule_page",
    "library": "library_page",
    "performance": "performance_page",
    "subjects": "subjects_page",
    "setup": "setup_page",
}


def _back(request: Request, anchor: str = "") -> RedirectResponse:
    """Post, redirect, get. A reload must not resend the form.

    The Referer is attacker-controlled, so it is only honoured when it points
    back at this service. Sending a 303 to whatever a header said would make
    every control here an open redirect, which is a phishing primitive on a
    hostname the account's own audience is being asked to trust.
    """
    fallback = str(request.url_for("dashboard_page"))
    referer = request.headers.get("referer") or ""
    allowed = (str(request.base_url).rstrip("/"), str(request.app.state.cfg.public_base_url))
    target = referer if referer.startswith(tuple(f"{a}/" for a in allowed)) else fallback
    return RedirectResponse(f"{target}{anchor}", status_code=303)


@router.get("/assets/{name}", name="asset")
async def serve_asset(name: str) -> FileResponse:
    """The panel's own images, behind the same login as the panel.

    Inlining them as data URIs was the alternative and it is the wrong trade:
    the room photo is 210 KB and `base.html` renders on every page, so every
    request would carry it and no cache would ever help. Behind `router`, so it
    inherits the login rather than being one unauthenticated path on a service
    that is publicly reachable by necessity.
    """
    if name not in _ASSET_NAMES:
        raise HTTPException(status_code=404, detail="no such asset")
    # Immutable: the filename changes when the picture does, which is the same
    # bargain `covers_dir` makes and the reason a year is safe here.
    return FileResponse(
        _ASSETS / name,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


async def _shell(request: Request) -> tuple[dict[str, Any], datetime]:
    moment = db.now()
    shell = await panel.shell(request.app.state.db, request.app.state.cfg, moment=moment)
    return shell, moment


def _render(
    request: Request, template: str, page: str, shell: dict[str, Any], **context: Any
) -> Any:
    return templates.TemplateResponse(
        request,
        template,
        {"cfg": request.app.state.cfg, "page": page, "shell": shell, "brand": None, **context},
    )


def _home(request: Request) -> RedirectResponse:
    """Where a link to a brand that no longer exists lands.

    A bookmark that outlived its identity should open the panel, not a 404.
    """
    return RedirectResponse(str(request.url_for("dashboard_page")), status_code=303)


def _platform_filter(request: Request, brand: panel.Brand) -> str:
    wanted = request.query_params.get("platform") or ""
    return wanted if wanted in brand.platforms else ""


# --------------------------------------------------------------------------
# Portfolio pages
# --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, name="dashboard_page")
async def dashboard_page(request: Request) -> Any:
    """Today: what needs a person across every brand, then one row per brand.

    It used to sum destinations, which answered "is the machine running" for
    one identity and mixed two identities into one runway, one next post and
    one retention chart the moment there were two.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, moment = await _shell(request)
    states = shell["picker"]
    metrics = request.app.state.metrics
    return _render(
        request,
        "today.html",
        "today",
        shell,
        states=states,
        issues=panel.group_across_brands(
            panel.merge_issues([issue for state in states for issue in state.issues])
        ),
        next_outs={
            state.brand.name: await panel.next_out(conn, cfg, state, moment=moment)
            for state in states
        },
        greeting=_GREETINGS[moment.date().toordinal() % len(_GREETINGS)],
        stuck=sum(d.stale for d in shell["destinations"].values()),
        poller_last=_gauge_time(metrics.poller_last_success),
        scheduler_last=_gauge_time(metrics.scheduler_last_success),
        insights_last=_gauge_time(metrics.insights_last_success),
    )


@router.get("/calendar", response_class=HTMLResponse, name="calendar_page")
async def calendar_page(request: Request) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, moment = await _shell(request)
    days = await panel.calendar(
        conn, cfg, shell["brands"], shell["destinations"], moment=moment
    )
    return _render(request, "calendar.html", "calendar", shell, days=days)


@router.get("/destinations", response_class=HTMLResponse, name="destinations_page")
async def destinations_page(request: Request) -> Any:
    """Brands down, platforms across. The seven stacked Health boards, as one grid."""
    shell, _ = await _shell(request)
    show = request.query_params.get("show") or ""
    states = list(shell["states"].values())
    if show == "attention":
        states = [
            s for s in states if any(d.level in ("bad", "warn") for d in s.destinations)
        ]
    return _render(request, "destinations.html", "destinations", shell, states=states, show=show)


@router.get("/system", response_class=HTMLResponse, name="system_page")
async def system_page(request: Request) -> Any:
    """The process, labelled as the process.

    Health led with counters that reset at every restart and read "4 published"
    on a service holding 150 posts. Every number here says since when.
    """
    conn = request.app.state.db
    shell, _ = await _shell(request)
    metrics = request.app.state.metrics
    return _render(
        request,
        "system.html",
        "system",
        shell,
        started_at=getattr(request.app.state, "started_at", None),
        poller_last=_gauge_time(metrics.poller_last_success),
        scheduler_last=_gauge_time(metrics.scheduler_last_success),
        insights_last=_gauge_time(metrics.insights_last_success),
        backup_last=_gauge_time(metrics.backup_last_success),
        published=_by_label(metrics.posts_published),
        failures=_by_label(metrics.publish_failures),
        counters={
            "published": _counter(metrics.posts_published),
            "publish_failures": _counter(metrics.publish_failures),
            "slots_starved": _counter(metrics.slots_starved),
            "graph_errors": _counter(metrics.graph_errors),
            "signature_failures": _counter(metrics.webhook_signature_failures),
            "insights_fetched": _counter(metrics.insights_fetched),
        },
        stale=await db.stale_claims(conn),
        funnel=await db.funnel(conn),
    )


@router.get("/search", response_class=HTMLResponse, name="search_page")
async def search_page(request: Request) -> Any:
    shell, _ = await _shell(request)
    query = (request.query_params.get("q") or "").strip()
    results = await panel.search(request.app.state.db, shell["brands"], query)
    return _render(request, "search.html", "search", shell, query=query, results=results)


@router.get("/settings", response_class=HTMLResponse, name="settings_page")
async def settings_page(request: Request) -> Any:
    """What this viewer can change, which is currently one thing.

    Deliberately not a page of service configuration. Everything that decides
    what publishes lives in the ConfigMap and is applied at boot, where it is
    reviewable and survives a pod being replaced; a panel that could change it
    would be a second source of truth for the schedule. This holds preferences
    of whoever is looking, and nothing here reaches a post.
    """
    shell, _ = await _shell(request)
    return _render(request, "settings.html", "settings", shell)


@router.post("/settings/skin")
async def set_skin(request: Request) -> Any:
    """Store the chosen look for a year, or fall back to the loud one.

    A year rather than a session, because the alternative is a panel that
    changes its appearance every time the login expires, which reads as a bug
    rather than as a default.
    """
    form = await request.form()
    chosen = str(form.get("skin") or "")
    response = RedirectResponse(request.url_for("settings_page"), status_code=303)
    response.set_cookie(
        SKIN_COOKIE,
        chosen if chosen in SKINS else SKINS[0],
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/admin",
    )
    return response


# --------------------------------------------------------------------------
# Brand pages
# --------------------------------------------------------------------------


@router.get("/b/{brand}/", response_class=HTMLResponse, name="brand_page")
async def brand_page(request: Request, brand: str) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, moment = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    state = shell["states"][found.name]
    everything = await panel.library(
        conn, cfg, found, shell["destinations"], moment=moment, per_page=10_000
    )
    coming = [
        plan for plan in await panel.plans(conn, cfg, found, shell["destinations"], moment=moment)
        if plan.first
    ][:3]
    return _render(
        request,
        "brand.html",
        "overview",
        shell,
        brand=found,
        state=state,
        next=await panel.next_out(conn, cfg, state, moment=moment),
        compare=panel.views_compare(everything["all"]),
        coming=coming,
        rules=await panel.slot_rules(conn, found),
    )


@router.get("/b/{brand}/schedule", response_class=HTMLResponse, name="schedule_page")
async def schedule_page(request: Request, brand: str) -> Any:
    """Queue and Slots in one page, one row per video."""
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, moment = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    platform = _platform_filter(request, found)
    plans = await panel.plans(
        conn, cfg, found, shell["destinations"], moment=moment, platform=platform
    )
    decisions = await panel.decisions(conn, found, shell["destinations"], moment=moment)
    return _render(
        request,
        "schedule.html",
        "schedule",
        shell,
        brand=found,
        platform=platform,
        days=panel.by_day(plans, moment),
        plan_count=len(plans),
        row_count=sum(len(p.chips) for p in plans),
        decisions=[d for d in decisions if not platform or d["platform"] == platform],
        rules=await panel.slot_rules(conn, found),
    )


@router.get("/b/{brand}/library", response_class=HTMLResponse, name="library_page")
async def library_page(request: Request, brand: str) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, moment = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    platform = _platform_filter(request, found)
    sort = "views" if request.query_params.get("sort") == "views" else "published"
    try:
        page = int(request.query_params.get("page") or 1)
    except ValueError:
        page = 1
    lib = await panel.library(
        conn, cfg, found, shell["destinations"], moment=moment,
        platform=platform, sort=sort, page=page,
    )
    return _render(
        request, "library.html", "library", shell,
        brand=found, lib=lib, platform=platform, sort=sort,
    )


@router.get("/b/{brand}/performance", response_class=HTMLResponse, name="performance_page")
async def performance_page(request: Request, brand: str) -> Any:
    """Each platform in its own terms.

    Insights refused every platform but Instagram, correctly, because every
    table on it was built on skip rate. The rule is that skip rate never shares
    a table with anything else, not that the others go unshown.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    shell, _ = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    sections = await panel.performance(conn, cfg, found, shell["destinations"])
    return _render(
        request, "performance.html", "performance", shell, brand=found, sections=sections
    )


@router.get("/b/{brand}/subjects", response_class=HTMLResponse, name="subjects_page")
async def subjects_page(request: Request, brand: str) -> Any:
    """The cooldown list, once per brand, which is whose list it is."""
    shell, _ = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    status = request.query_params.get("status") or ""
    data = await panel.subjects(request.app.state.db, found, owner=shell["owner"], status=status)
    return _render(request, "subjects.html", "subjects", shell, brand=found, data=data)


@router.get("/b/{brand}/setup", response_class=HTMLResponse, name="setup_page")
async def setup_page(request: Request, brand: str) -> Any:
    shell, _ = await _shell(request)
    found = panel.find_brand(shell["brands"], brand)
    if found is None:
        return _home(request)
    entries = await panel.setup(request.app.state.db, found, shell["destinations"])
    return _render(request, "setup.html", "setup", shell, brand=found, entries=entries)


# --------------------------------------------------------------------------
# The old addresses
# --------------------------------------------------------------------------
#
# Every page used to be `/admin/<table>?brand=` or `?account=`, and those links
# are in bookmarks and in this repo's own notes. Each one lands on the page that
# answers the same question now: inside the brand it named, narrowed to the
# platform of the destination it named, or on the portfolio page when it named
# neither.

_LEGACY = {
    "queue": ("schedule_page", "calendar_page"),
    "posts": ("library_page", "dashboard_page"),
    "insights": ("performance_page", "dashboard_page"),
    "repos": ("subjects_page", "dashboard_page"),
    "slots": ("setup_page", "destinations_page"),
    "health": ("setup_page", "system_page"),
}


async def _legacy(request: Request, section: str) -> RedirectResponse:
    target, fallback = _LEGACY[section]
    brands = await panel.load_brands(request.app.state.db)
    account = request.query_params.get("account") or ""
    for brand in brands:
        for row in brand.accounts:
            if account and str(row["account_id"]) == account:
                url = str(request.url_for(target, brand=brand.path))
                if target in ("schedule_page", "library_page"):
                    url += f"?platform={row['platform'] or db.PLATFORM_INSTAGRAM}"
                return RedirectResponse(url, status_code=303)
    found = panel.find_brand(brands, request.query_params.get("brand") or "")
    if found is not None:
        return RedirectResponse(str(request.url_for(target, brand=found.path)), status_code=303)
    return RedirectResponse(str(request.url_for(fallback)), status_code=303)


@router.get("/queue", name="queue_page")
async def queue_page(request: Request) -> Any:
    return await _legacy(request, "queue")


@router.get("/posts", name="posts_page")
async def posts_page(request: Request) -> Any:
    return await _legacy(request, "posts")


@router.get("/insights", name="insights_page")
async def insights_page(request: Request) -> Any:
    return await _legacy(request, "insights")


@router.get("/repos", name="repos_page")
async def repos_page(request: Request) -> Any:
    return await _legacy(request, "repos")


@router.get("/slots", name="slots_page")
async def slots_page(request: Request) -> Any:
    return await _legacy(request, "slots")


@router.get("/health", name="health_page")
async def health_page(request: Request) -> Any:
    return await _legacy(request, "health")


def _counter(metric: Any) -> int:
    """Read a counter, labelled or not.

    **A labelled parent has no `_value`.** `prometheus_client` only runs
    `_metric_init` on an unlabelled metric, so `posts_published` and
    `publish_failures` stopped being readable here the day they gained a
    `platform` label, the `AttributeError` was swallowed, and the Health page
    reported zero posts published on a service that had published dozens. The
    children hold the numbers, so sum them.
    """
    children = getattr(metric, "_metrics", None)  # noqa: SLF001 - no public read
    if children:
        return sum(_counter(child) for child in list(children.values()))
    try:
        return int(metric._value.get())  # noqa: SLF001 - prometheus_client has no public read
    except (AttributeError, TypeError):
        return 0


def _by_label(metric: Any) -> dict[str, int]:
    """A labelled counter's children, keyed by the first label's value.

    The platform label was added so a platform that stopped publishing could not
    hide behind the others, and summing it back into one tile undid that.
    """
    children = getattr(metric, "_metrics", None) or {}  # noqa: SLF001 - no public read
    return {str(key[0]): _counter(child) for key, child in list(children.items())}


def _gauge_time(metric: Any) -> datetime | None:
    """A zero gauge means "has not run yet", not 1970."""
    value = _counter(metric)
    return datetime.fromtimestamp(value, tz=db.now().tzinfo) if value else None


# --------------------------------------------------------------------------
# Queue controls
# --------------------------------------------------------------------------


# Publishes started by hand, held only so the event loop cannot collect a task
# nobody is awaiting. Discarded on completion; the row's state is the record.
_in_flight: set[asyncio.Task] = set()


async def _require_row(request: Request, queued_id: int) -> Any:
    row = await db.get_queued(request.app.state.db, queued_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such queued post")
    return row


async def _approve_row(conn: Any, row: Any, *, bulk: bool = False) -> None:
    if row["state"] not in (db.QUEUE_DRAFT, db.QUEUE_FAILED):
        return
    queued_id = int(row["id"])
    retrying = row["state"] == db.QUEUE_FAILED
    if retrying and row["container_id"] and panel.upload_is_dead(row):
        # Meta reported this upload ERROR or EXPIRED, states it never publishes,
        # so nothing is live and the dead id only stands in the way of a clean
        # retry. Allowed in bulk too, since there is no duplicate to fear.
        await db.clear_container(conn, queued_id)
        log.info(
            "Queue %d: cleared rejected upload %s before retrying", queued_id, row["container_id"]
        )
    elif retrying and row["container_id"]:
        if bulk:
            # One click arming a whole video must not also make the decision a
            # person makes only after reading a failure with a container behind
            # it. That one stays on its own row.
            log.info("Queue %d has container %s, so arming the video skipped it",
                     queued_id, row["container_id"])
            return
        # The scheduler refuses this case on its own, because a container that
        # existed may already have become a Reel. A person who has read the
        # failure and clicked anyway is making a different decision, so it is
        # allowed here and only here, and it is worth a loud line in the log.
        log.warning(
            "Queue %d retried by hand although container %s existed; "
            "check the account for a duplicate",
            queued_id, row["container_id"],
        )
    # Arming a failed row clears the reason as well as the state, so the next
    # attempt is not read through the last one's error.
    await db.set_queue_state(conn, queued_id, db.QUEUE_APPROVED, reset_attempts=retrying)
    log.info("Queue %d armed from the admin UI", queued_id)


async def _hold_row(conn: Any, row: Any, *, bulk: bool = False) -> None:
    if row["state"] == db.QUEUE_APPROVED:
        await db.set_queue_state(conn, int(row["id"]), db.QUEUE_DRAFT)


async def _cancel_row(conn: Any, row: Any, *, bulk: bool = False) -> None:
    if row["state"] == db.QUEUE_PUBLISHED:
        # Cancelling a post that went out would only make the record wrong.
        return
    if row["state"] == db.QUEUE_CLAIMED and not panel.claim_is_stale(row):
        # A fresh claim is mid-flight and cancelling it races the publish. An
        # old one is a process that died holding it, and refusing that left
        # row 55 stuck for nine days with no way to resolve it from here.
        return
    await db.set_queue_state(conn, int(row["id"]), db.QUEUE_CANCELLED)
    log.info("Queue %d cancelled from the admin UI", row["id"])


@router.post("/queue/{queued_id}/approve", name="approve")
async def approve(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    await _approve_row(request.app.state.db, row)
    return _back(request)


@router.post("/queue/{queued_id}/publish", name="publish_now")
async def publish_now(request: Request, queued_id: int) -> Any:
    """Send this post now, without waiting for its slot.

    **The control that was missing.** Everything else here decides what the
    scheduler will do later; there was no way to say "go", so a row whose
    failure you had just fixed still cost a day to find out about, and the only
    lever was deleting its `slot_fires` row by hand against the live database.
    That is how 2026-08-28 was spent: TikTok refused the first Reel ever queued
    to it, the cause was fixed within the hour, and nothing in the panel could
    ask it to try again.

    **It does not touch the slot.** Today's fire stays claimed and tomorrow's
    fires normally, because this answers "publish this row" rather than
    "pretend the slot has not run". A row sent this way and then published by
    its slot as well is the one thing worth being careful about, and claiming
    the row is what stops it: `claim_queued` moves it out of `approved` before
    anything is sent, so a tick landing mid-flight finds nothing to take.

    **It returns before the post exists.** An upload plus a status poll runs to
    minutes and a request held that long is a proxy timeout wearing a failure's
    clothes, so the work goes to a task and the panel is told to come back. The
    row's own state is the progress bar: `claimed` while it runs, then
    `published` or `failed` with the reason attached, which is the same thing a
    slot-fired publish leaves behind.
    """
    row = await _require_row(request, queued_id)
    if row["state"] not in (db.QUEUE_DRAFT, db.QUEUE_APPROVED, db.QUEUE_FAILED):
        # Published is done, claimed is either in flight or stale, and both are
        # decisions this button must not make. `cancel` is where a stale claim
        # is resolved, by a person who has read it.
        return _back(request)
    if row["container_id"] and panel.upload_is_dead(row):
        # The one container that is provably not live: Meta reported it ERROR
        # or EXPIRED. Cleared so the publish below makes a fresh one.
        await db.clear_container(request.app.state.db, queued_id)
        log.info(
            "Queue %d: cleared rejected upload %s to send it now", queued_id, row["container_id"]
        )
        row = await _require_row(request, queued_id)
    elif row["container_id"]:
        # The same line the scheduler draws. Something exists at the platform
        # and may already be live, so re-sending risks a duplicate that nothing
        # here could detect afterwards.
        log.warning(
            "Queue %d has container %s, so publish now was refused", queued_id, row["container_id"]
        )
        return _back(request)

    account = await db.get_account(request.app.state.db, str(row["account_id"]))
    if account is None:
        log.error("Queue %d names account %s, which is gone", queued_id, row["account_id"])
        return _back(request)

    # Armed first, because `claim_queued` only takes a row that is approved,
    # and a draft or a failed row reaching here is one a person just asked to
    # send. Attempts are reset for the same reason `approve` resets them: this
    # is a new decision rather than a continuation of the last one's budget.
    if row["state"] != db.QUEUE_APPROVED:
        await db.set_queue_state(
            request.app.state.db, queued_id, db.QUEUE_APPROVED, reset_attempts=True
        )
    if not await db.claim_queued(request.app.state.db, queued_id):
        log.info("Queue %d was taken by a tick before publish now could claim it", queued_id)
        return _back(request)

    claimed = await db.get_queued(request.app.state.db, queued_id)
    log.info("Queue %d published by hand from the admin UI", queued_id)
    task = asyncio.create_task(
        scheduler.publish_queued(
            request.app.state.db,
            request.app.state.graph,
            request.app.state.cfg,
            request.app.state.metrics,
            account=account,
            queued=claimed,
        )
    )
    # Held so the loop cannot collect it mid-publish, and discarded when it is
    # done. Without the reference this is a task that can vanish between the
    # upload and the status poll.
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)
    return _back(request)


@router.post("/queue/publish-all", name="publish_all_now")
async def publish_all_now(request: Request) -> Any:
    """Publish what the next slot fire would publish, on every destination, now.

    **A rehearsal of the schedule rather than a way around it.** A new identity
    is registered, credentialled and queued, and the only thing that proves any
    of it works is a post coming out the other end. Waiting for 08:10 to find
    out that a token was minted wrong, or a Page id is the one from a URL,
    costs a day per attempt, and the first attempt is exactly when something is
    most likely to be wrong.

    Each destination contributes the row `next_approved` would hand a due slot,
    which is what makes this a rehearsal: the same row, chosen the same way,
    published by the same function. A destination with nothing armed
    contributes nothing rather than reaching for a draft, because a draft is
    deliberately not armed and this is not the control that decides that.

    **It does not touch the slots**, exactly as `publish_now` does not, and here
    that is the feature rather than a caveat. Today's fire is still due, so the
    schedule still gets to prove itself on its own timetable while this proves
    the publishing path immediately.

    **It refuses to run unscoped.** It takes `?brand=` or `?account=` and
    nothing else, because one misread click must not fire every identity at
    once. The Schedule page is the only place it is offered, and that page is
    always inside a brand.

    Every row is claimed before anything is sent, so a tick landing mid-flight
    finds nothing to take, and every row with a container is skipped, which is
    the same line the scheduler and `cancel` draw. It returns before the posts
    exist, for the reason `publish_now` does: the rows' own states are the
    progress bar.
    """
    conn = request.app.state.db
    brands = await panel.load_brands(conn)
    account_id = request.query_params.get("account") or ""
    visible = [
        row for brand in brands for row in brand.accounts
        if account_id and str(row["account_id"]) == account_id
    ]
    label = account_id
    if not visible:
        found = panel.find_brand(brands, request.query_params.get("brand") or "")
        if found is not None:
            visible, label = list(found.accounts), found.name
    if not visible:
        log.warning("Publish all was refused because no identity is selected")
        return _back(request)

    started: list[str] = []
    for account in visible:
        row = await db.next_approved(conn, str(account["account_id"]))
        if row is None:
            log.info("Publish all: %s has nothing armed", account["account_id"])
            continue
        if row["container_id"]:
            log.warning(
                "Publish all: queue %d has container %s, so it was skipped",
                row["id"], row["container_id"],
            )
            continue
        if not await db.claim_queued(conn, int(row["id"])):
            log.info("Publish all: queue %d was taken by a tick first", row["id"])
            continue

        claimed = await db.get_queued(conn, int(row["id"]))
        task = asyncio.create_task(
            scheduler.publish_queued(
                conn,
                request.app.state.graph,
                request.app.state.cfg,
                request.app.state.metrics,
                account=account,
                queued=claimed,
            )
        )
        # Held for the reason `publish_now` holds its one: without a reference
        # this is a task that can vanish between the upload and the status poll.
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)
        started.append(f"{account['platform']}:{row['id']}")

    log.info(
        "Publish all for %r started %d of %d destination(s): %s",
        label, len(started), len(visible), ", ".join(started) or "none",
    )
    return _back(request)


@router.post("/queue/{queued_id}/hold", name="hold")
async def hold(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    await _hold_row(request.app.state.db, row)
    return _back(request)


@router.post("/queue/{queued_id}/cancel", name="cancel")
async def cancel(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    await _cancel_row(request.app.state.db, row)
    return _back(request)


_VIDEO_ACTIONS = {"approve": _approve_row, "hold": _hold_row, "cancel": _cancel_row}


@router.post("/b/{brand}/videos/{action}", name="video_action")
async def video_action(
    request: Request,
    brand: str,
    action: str,
    ids: Annotated[list[int] | None, Form()] = None,
) -> Any:
    """Approve, hold or cancel one video on every destination it is queued to.

    A video is one render and a row per platform, so reviewing it used to mean
    the same decision four times on four cards. Each row still goes through the
    single-row rule, and only rows belonging to this brand are touched, so a
    forged list of ids cannot reach another identity's queue.
    """
    if action not in _VIDEO_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    conn = request.app.state.db
    found = panel.find_brand(await panel.load_brands(conn), brand)
    if found is None:
        raise HTTPException(status_code=404, detail="no such brand")
    allowed = {str(row["account_id"]) for row in found.accounts}
    for queued_id in ids or []:
        row = await db.get_queued(conn, queued_id)
        if row is None or str(row["account_id"]) not in allowed:
            continue
        await _VIDEO_ACTIONS[action](conn, row, bulk=True)
    return _back(request)


@router.post("/queue/{queued_id}/move", name="move")
async def move(request: Request, queued_id: int, direction: Annotated[str, Form()]) -> Any:
    """Swap this post with its neighbour in the line."""
    conn = request.app.state.db
    row = await _require_row(request, queued_id)
    siblings = await db.queued_posts(
        conn, account_id=row["account_id"], states=(db.QUEUE_DRAFT, db.QUEUE_APPROVED)
    )
    ids = [int(r["id"]) for r in siblings]
    if queued_id not in ids:
        return _back(request)
    index = ids.index(queued_id)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(ids):
        return _back(request)

    # Positions are rewritten wholesale rather than swapped, because rows that
    # arrived before this feature all share position 0 and a swap between two
    # zeroes changes nothing.
    ids[index], ids[target] = ids[target], ids[index]
    for position, ident in enumerate(ids, start=1):
        await db.update_queued(conn, ident, position=position)
    return _back(request)


@router.post("/queue/{queued_id}/edit", name="edit")
async def edit(
    request: Request,
    queued_id: int,
    caption: Annotated[str, Form()] = "",
    keyword: Annotated[str, Form()] = "",
    link: Annotated[str, Form()] = "",
    pin: Annotated[str, Form()] = "",
) -> Any:
    row = await _require_row(request, queued_id)
    if row["state"] in (db.QUEUE_PUBLISHED, db.QUEUE_CLAIMED):
        return _back(request)

    override, clear = None, False
    if pin.strip():
        try:
            override = datetime.fromisoformat(pin.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="pin must be an ISO timestamp") from None
        if override.tzinfo is None:
            override = override.replace(tzinfo=db.now().tzinfo)
    else:
        clear = True

    await db.update_queued(
        request.app.state.db,
        queued_id,
        caption=caption,
        keyword=keyword.strip() or None,
        link=link.strip() or None,
        slot_override=override,
        clear_override=clear,
    )
    return _back(request)


# --------------------------------------------------------------------------
# Slot and account controls
# --------------------------------------------------------------------------


@router.post("/slots/add", name="add_slot")
async def add_slot(
    request: Request,
    account_id: Annotated[str, Form()],
    hour: Annotated[int, Form()],
    minute: Annotated[int, Form()] = 0,
    tz: Annotated[str, Form()] = "UTC",
    jitter_minutes: Annotated[int, Form()] = 0,
    days: Annotated[list[str] | None, Form()] = None,
) -> Any:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise HTTPException(status_code=400, detail="hour or minute out of range")
    await db.add_slot(
        request.app.state.db,
        account_id=account_id,
        hour=hour,
        minute=minute,
        tz=tz.strip() or "UTC",
        jitter_minutes=max(0, jitter_minutes),
        days=schedule.format_days({int(d) for d in (days or []) if d.isdigit()}),
    )
    return _back(request)


@router.post("/slots/{slot_id}/toggle", name="toggle_slot")
async def toggle_slot(
    request: Request, slot_id: int, active: Annotated[str, Form()] = "0"
) -> Any:
    await db.set_slot_active(request.app.state.db, slot_id, active == "1")
    return _back(request)


@router.post("/slots/{slot_id}/delete", name="remove_slot")
async def remove_slot(request: Request, slot_id: int) -> Any:
    await db.delete_slot(request.app.state.db, slot_id)
    return _back(request)


@router.post("/accounts/{account_id}/flags", name="set_flags")
async def set_flags(
    request: Request,
    account_id: str,
    field: Annotated[str, Form()],
    value: Annotated[str, Form()] = "0",
) -> Any:
    """The per-destination kill switch, and the poller's on/off.

    `active` gates the comment poller and the scheduler both, which is what
    makes it a real stop rather than a partial one: pausing a destination that
    keeps publishing would be a worse surprise than either behaviour alone.
    """
    if field not in ("active", "dm_enabled"):
        raise HTTPException(status_code=400, detail="unknown flag")
    await db.set_account_flags(
        request.app.state.db, account_id, **{field: value == "1"}
    )
    log.warning("Account %s: %s set to %s from the admin UI", account_id, field, value)
    return _back(request)
