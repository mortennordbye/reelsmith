"""The control panel, served by the same process as everything else.

Server-rendered Jinja over the tables this service already owns. No frontend
build, no bundler, no API layer between the page and the query.

**Plain forms, no HTMX.** The plan said HTMX and it would work, but every
control here is a state change followed by a full reload, which is what a form
POST already is. Adding a script tag would mean either a CDN fetch this
container cannot make or a vendored copy to keep updated, in exchange for
nothing the eye can see. Pages that want to feel live use a meta refresh.

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
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gateway import analysis, db, schedule, scheduler

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
    """Just the time, for the one place that sets it at 58 pixels. The date
    goes on the line underneath, where it does not have to compete."""
    if when is None:
        return "--:--"
    return when.astimezone(schedule.zone_or_utc(tz)).strftime("%H:%M")


# One dry line per page, under its heading. Here rather than in six templates
# so adding a page means adding a line to one dict, and the pages call
# `quip_for(page)` instead of each one carrying its own copy.
#
# **Nothing on the Posts page may say reach, saves or skipped.** A YouTube
# account reports none of the three, and `test_gateway_youtube_insights` asserts
# the words appear nowhere on that page, because a zero beside a metric a
# platform does not measure reads as a result. A joke does not earn an exception.
_QUIPS = {
    "queue": "Cancelling one of these before its slot fires is the entire review process.",
    "posts": "Every one of these was written, voiced, rendered and published while you slept.",
    "insights": "The format averages 30 to 40 percent. We are working on it.",
    "repos": "Thirty days is how long a repository gets to forget about us.",
    "slots": "The jitter is derived, never rolled, so nothing here fires twice.",
    "health": "Green means the machine is fine. It says nothing about the videos.",
}


def _quip_for(page: str) -> str:
    return _QUIPS.get(page, "")


templates.env.filters["fmt"] = _fmt
templates.env.filters["ago"] = _ago
templates.env.filters["until"] = _until
templates.env.filters["clock"] = _clock
templates.env.globals["quip_for"] = _quip_for


def _display_tz(cfg: Any, slots: list[Any]) -> str:
    """Show times in the zone the schedule is written in, not in UTC.

    Whoever is reading this wants to know if a post goes out at a reasonable
    hour where the audience is, and the slot already says where that is.
    """
    return str(slots[0]["tz"]) if slots else cfg.default_timezone


async def _accounts(request: Request) -> list[Any]:
    # Every platform. The panel is where a person goes to see what is queued
    # and what failed, and a destination it cannot show is a destination
    # nobody is watching.
    return await db.all_accounts(request.app.state.db, platform=None)


async def _scope(request: Request) -> dict[str, Any]:
    """What the page is about: everything, one original account, or one of its
    destinations.

    Every page used to stack one board per account down the screen, which reads
    fine at one account and becomes a scroll at four. The scope narrows the
    page instead, and `visible` is what a page iterates in every mode, so a
    page never has to care which one it is in.

    **Two levels, because an identity is not a destination.** One original
    account posts to three platforms and is three `accounts` rows, so a flat
    picker offered the same handle three times and would offer it nine times at
    three identities. `?brand=` is the identity and `?account=` is one of its
    rows, and the narrower one wins when both are given.

    An unknown or removed value falls back to showing everything rather than
    404ing. A bookmark that outlived its account should still show the panel.
    """
    accounts = await db.all_accounts(request.app.state.db, platform=None)

    # Grouped in the order the rows come back, which `db` already sorts by
    # brand and then by platform, so the switcher and the boards agree without
    # either of them sorting again.
    groups: dict[str, dict[str, Any]] = {}
    for account in accounts:
        brand = account["brand"] or db.brand_of(account["username"], account["account_id"])
        groups.setdefault(brand, {"name": brand, "accounts": []})["accounts"].append(account)
    brands = list(groups.values())

    wanted = request.query_params.get("account") or ""
    selected = next((a for a in accounts if a["account_id"] == wanted), None)
    wanted_brand = request.query_params.get("brand") or ""
    # The destination wins. Both arriving together means a stale link rather
    # than a contradiction, and the narrower answer is the one a person clicked.
    brand = groups.get(selected["brand"], {}).get("name", "") if selected else ""
    if not selected and wanted_brand in groups:
        brand = wanted_brand

    if selected:
        visible, query = [selected], f"?account={selected['account_id']}"
    elif brand:
        visible, query = list(groups[brand]["accounts"]), f"?brand={quote(brand)}"
    else:
        visible, query = list(accounts), ""

    return {
        "accounts": accounts,
        "brands": brands,
        "selected": selected,
        # Which identity the page is inside, whether that came from a brand
        # link or from picking one of its destinations. The switcher lights the
        # chip either way, so a YouTube board never looks like it belongs to
        # nobody.
        "brand": brand,
        "visible": visible,
        # Appended to every in-panel link so the choice survives navigation.
        "query": query,
    }


def _back(request: Request, anchor: str = "") -> RedirectResponse:
    """Post, redirect, get. A reload must not resend the form.

    The Referer is attacker-controlled, so it is only honoured when it points
    back at this service. Sending a 303 to whatever a header said would make
    every control here an open redirect, which is a phishing primitive on a
    hostname the account's own audience is being asked to trust.
    """
    fallback = str(request.url_for("queue_page"))
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
# Pages
# --------------------------------------------------------------------------


async def _rendered_at(conn: Any, rows: list[Any]) -> dict[int, datetime | None]:
    """When each queued post's video was made, keyed by queue id.

    `created_at` answers a different question. It is when a video reached the
    gateway, and the two dates come apart by days in the ordinary case: a batch
    renders at 02:00 and the post waits its turn in a line three days deep, and
    a run moved aside by hand can be queued a week after it was built.
    `rendered_repos` is the only record of when the video itself was made, so
    the panel reads it rather than inferring it from the queue row.
    """
    stamps = await db.rendered_at_for(conn, [row["repo_full_name"] for row in rows])
    return {
        row["id"]: db.parse_iso(stamps.get(row["repo_full_name"] or ""))
        for row in rows
    }


@router.get("/", response_class=HTMLResponse, name="dashboard_page")
async def dashboard_page(request: Request) -> Any:
    """Everything at a glance, which is the one question the six list pages
    could not answer between them.

    Queue and Posts list things and Insights compares them; none of them says
    "is the machine running and what has it done". This does, and it adds no
    query of its own: every number below is a call one of those pages already
    makes, so the dashboard cannot disagree with the page it summarises.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)
    moment = db.now()
    metrics = request.app.state.metrics

    # Two calls for every account, rather than two per account.
    depths = await db.queue_depth_by_account(conn)
    stale = await db.stale_claims_by_account(conn)

    boards: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for account in scope["visible"]:
        account_id = account["account_id"]
        slots = await db.active_slots(conn, account_id)
        approved = depths.get((account_id, db.QUEUE_APPROVED), 0)
        rows = await scheduler.upcoming(conn, cfg, account_id, moment=moment)
        boards.append(
            {
                "row": account,
                "approved": approved,
                "draft": depths.get((account_id, db.QUEUE_DRAFT), 0),
                "failed": depths.get((account_id, db.QUEUE_FAILED), 0),
                "stale": stale.get(account_id, 0),
                "slots_per_day": len(slots),
                # Days of posting the queue can still cover, which is the number
                # that says whether to go and render. `health_page` computes it
                # the same way.
                "runway_days": (approved / len(slots)) if slots else None,
                "tz": _display_tz(cfg, slots),
                "days_left": _token_days(account, moment),
            }
        )
        for row, when in rows:
            if when is not None:
                # The slot's own zone travels with the row, because whoever is
                # reading this wants to know if a post goes out at a reasonable
                # hour where the audience is, and UTC does not answer that.
                upcoming.append(
                    {"account": account, "row": row, "when": when,
                     "tz": _display_tz(cfg, slots)}
                )

    # The soonest post across every visible destination. Sorted here rather than
    # per board, because "what goes out next" is one answer and not four.
    upcoming.sort(key=lambda item: item["when"])
    next_out = upcoming[0] if upcoming else None
    made = await _rendered_at(conn, [next_out["row"]] if next_out else [])

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "boards": boards,
            "cfg": cfg,
            "page": "dashboard",
            "scope": scope,
            "greeting": _GREETINGS[moment.date().toordinal() % len(_GREETINGS)],
            "next_out": next_out,
            "made": made,
            "upcoming_count": len(upcoming),
            "machine": await _machine(conn, scope),
            "retention": await _retention(conn, scope),
            "poller_last": _gauge_time(metrics.poller_last_success),
            "scheduler_last": _gauge_time(metrics.scheduler_last_success),
            "insights_last": _gauge_time(metrics.insights_last_success),
        },
    )


def _token_days(account: Any, moment: datetime) -> float | None:
    expires = db.parse_iso(account["token_expires_at"])
    return (expires - moment).total_seconds() / 86_400 if expires else None


async def _machine(conn: Any, scope: dict[str, Any]) -> dict[str, Any]:
    """What the pipeline has moved, counted from what this service can see.

    Deliberately shorter than the real pipeline. Discovery matches thousands of
    repositories and ranks them down to a handful, and both of those numbers
    live on the machine that renders and never reach here, so the strip starts
    at the first step the gateway has a record of. Inventing the rest would put
    a number on this page that nothing could check.
    """
    ids = [account["account_id"] for account in scope["visible"]]
    single = ids[0] if len(ids) == 1 else None

    covered = await db.covered_repos(conn, single, limit=10_000)
    rendered = await db.rendered_repos_list(conn, single, limit=10_000)
    published = await db.published_media(conn, single, limit=10_000)
    depth = await db.queue_depth(conn, single)

    # Nights that produced something, which is the closest honest reading of
    # "how long has this run on its own". One night can render several.
    nights = {stamp[:10] for row in rendered if (stamp := row["rendered_at"] or "")}
    return {
        "covered": len(covered),
        "rendered": len(rendered),
        "queued": sum(depth.get(state, 0) for state in db.QUEUE_LIVE_STATES),
        "published": len(published),
        "destinations": len(scope["visible"]),
        "nights": len(nights),
    }


async def _retention(conn: Any, scope: dict[str, Any]) -> dict[str, Any] | None:
    """The opening scores, and only Instagram's.

    `skip_rate` is the one number the loop turns on and the only platform that
    reports it is Instagram. Every other platform stores 0, which is an absence
    rather than a perfect score, so a board mixing them in would report an
    opening that was never measured as one nobody skipped.
    """
    rows = [
        account for account in scope["visible"]
        if (account["platform"] or db.PLATFORM_INSTAGRAM) == db.PLATFORM_INSTAGRAM
    ]
    if not rows:
        return None

    # The keys `skip_chart` reads, so the chart here and the chart on Insights
    # are the same function over the same shape rather than two joins that agree
    # until one of them is edited.
    merged: list[dict[str, Any]] = []
    for account in rows:
        account_id = account["account_id"]
        readings = await db.latest_insights(conn, account_id, platform=db.PLATFORM_INSTAGRAM)
        for post in await db.published_media(conn, account_id, limit=10_000):
            reading = readings.get(post["media_id"])
            # A post with no hook is skipped for the reason the loop skips it:
            # the opening is what the score is about, and a row without one
            # cannot be attributed to anything.
            if reading is None or not post["hook"] or not reading["skip_rate"]:
                continue
            merged.append(
                {
                    "hook": post["hook"],
                    "repo_full_name": post["repo_full_name"],
                    "published_at": post["published_at"],
                    "skip_rate": float(reading["skip_rate"]),
                    "views": int(reading["views"] or 0),
                }
            )
    if not merged:
        return None

    return {
        "best": sorted(merged, key=lambda post: post["skip_rate"])[:4],
        "n": len(merged),
        "chart": analysis.skip_chart(merged),
        "median_skip": _median([post["skip_rate"] for post in merged]),
        "median_views": _median([float(post["views"]) for post in merged]),
        "threshold": analysis.SKIP_THRESHOLD,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


@router.get("/queue", response_class=HTMLResponse, name="queue_page")
async def queue_page(request: Request) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)
    moment = db.now()

    boards = []
    for account in scope["visible"]:
        slots = await db.active_slots(conn, account["account_id"])
        rows = await scheduler.upcoming(
            conn, cfg, account["account_id"], moment=moment
        )
        recent = await db.queued_posts(
            conn,
            account_id=account["account_id"],
            states=(db.QUEUE_PUBLISHED, db.QUEUE_FAILED, db.QUEUE_CLAIMED, db.QUEUE_CANCELLED),
            limit=15,
        )
        boards.append(
            {
                "account": account,
                "tz": _display_tz(cfg, slots),
                "upcoming": rows,
                "recent": list(reversed(recent)),
                "made": await _rendered_at(conn, [row for row, _ in rows] + recent),
                "has_slots": bool(slots),
                # A claim nothing finished needs the same decision a failure
                # does, so the row has to say so rather than looking ordinary.
                "stale": {int(r["id"]) for r in recent if r["state"] == db.QUEUE_CLAIMED
                          and _claim_is_stale(r, moment=moment)},
            }
        )

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "boards": boards,
            "cfg": cfg,
            "page": "queue",
            "scope": scope,
            "states": db,
        },
    )


@router.get("/slots", response_class=HTMLResponse, name="slots_page")
async def slots_page(request: Request) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)
    moment = db.now()
    boards = []
    for account in scope["visible"]:
        rows = await db.all_slots(conn, account["account_id"])
        boards.append(
            {
                "account": account,
                "slots": [
                    {
                        "row": row,
                        "slot": schedule.Slot.from_row(row),
                        "next": schedule.next_fire(schedule.Slot.from_row(row), moment)
                        if row["active"]
                        else None,
                    }
                    for row in rows
                ],
            }
        )
    return templates.TemplateResponse(
        request,
        "slots.html",
        {"boards": boards, "cfg": cfg, "page": "slots", "scope": scope},
    )


@router.get("/posts", response_class=HTMLResponse, name="posts_page")
async def posts_page(request: Request) -> Any:
    """How the published Reels are doing, and which of them converted.

    Two sources joined by media id and neither of them new: Meta's numbers from
    the insights sweep, and the DM funnel this service has been recording since
    the first post. The funnel was only ever shown as an account-wide total,
    which cannot answer the question worth asking, which is which video worked.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)

    boards = []
    for account in scope["visible"]:
        account_id = account["account_id"]
        rows = await db.published_media(conn, account_id)
        readings = await db.latest_insights(conn, account_id)
        funnels = await db.per_post_funnel(conn, account_id)

        # Which numbers this platform actually has. A TikTok row rendered with
        # Instagram's column set is a post that got zero reach and zero saves,
        # which is a claim rather than an absence.
        columns = analysis.measured_columns(account["platform"])
        posts, totals = [], dict.fromkeys((*columns, "comments_seen", "links_sent"), 0)
        for row in rows:
            seen = funnels.get(row["media_id"], {})
            reading = readings.get(row["media_id"])
            posts.append(
                {
                    "row": row,
                    "insights": reading,
                    "funnel": seen,
                    # Of the people who asked, how many got the link. The
                    # denominator is comments we matched, not total comments,
                    # because the rest were never asking for anything.
                    "conversion": (
                        seen.get("links_sent", 0) / seen["comments_seen"]
                        if seen.get("comments_seen")
                        else None
                    ),
                }
            )
            for key in columns:
                totals[key] += int(reading[key]) if reading else 0
            for key in ("comments_seen", "links_sent"):
                totals[key] += seen.get(key, 0)

        # Retention is averaged rather than summed, and only over the posts
        # that have a reading. A total watch time would be a number that only
        # goes up, and dividing by every post would report a hook getting
        # better every time an unmeasured one drops off the window.
        #
        # Each figure over its own population rather than all of them over the
        # posts that have a `skip_rate`. Only Instagram reports that one, so
        # tying the watch time to it hid the number YouTube does report behind
        # the one it does not.
        scored: list[Any] = [p["insights"] for p in posts if p["insights"]]

        def mean(key: str, rows: list[Any] = scored) -> float | None:
            measured = [float(r[key]) for r in rows if r[key]]
            return sum(measured) / len(measured) if measured else None

        avg_watch_ms = mean("avg_watch_ms")
        boards.append(
            {
                "account": account,
                "posts": posts,
                "totals": totals,
                "columns": columns,
                # Whether a DM funnel can exist here at all. The keyword
                # mechanic is comments and private replies, which is one
                # platform, so showing the row everywhere reported a mechanic
                # that was never available as one that converted nobody.
                "has_funnel": account["platform"] == db.PLATFORM_INSTAGRAM,
                # An average is the only fair way to compare a Reel published
                # this morning with one from last week.
                "avg_views": totals["views"] // len(posts) if posts else 0,
                "measured": sum(1 for p in posts if p["insights"]),
                # The one number that scores the hook on its own. Educational
                # Reels benchmark at 30 to 40 percent; the first seven here ran
                # 64 to 80, which is why it is on the board rather than buried
                # in a per post row.
                "avg_skip": mean("skip_rate"),
                # Milliseconds in the column and seconds on the page, the same
                # conversion the per post tile makes.
                "avg_watch_s": avg_watch_ms / 1000 if avg_watch_ms else None,
                # YouTube's share of the video watched. Meta reports no such
                # figure and TikTok reports nothing about watching at all, so
                # this tile appears on one board of three.
                "avg_viewed": mean("avg_view_pct"),
            }
        )

    return templates.TemplateResponse(
        request,
        "posts.html",
        {"boards": boards, "cfg": cfg, "page": "posts", "scope": scope},
    )


@router.get("/insights", response_class=HTMLResponse, name="insights_page")
async def insights_page(request: Request) -> Any:
    """The comparisons, as against the list of posts on the Posts page.

    Listing is not comparing. Every number the account has acted on was worked
    out by hand in a session and pasted into a notes file, which goes stale
    silently, and the only place any of it could be recomputed was a terminal on
    one laptop. This is the panel, which is what can be opened from a phone at
    seven in the morning after the nightly did something at two.

    Two cohort tables and one chart. The chart is skip rate because that is the
    metric the whole pipeline is tuned on; the tables carry views, because views
    are too skewed to plot honestly on the same axis and a median plus a count
    of breakouts is what that distribution can support.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)

    boards = []
    for account in scope["visible"]:
        # **Instagram only, and structurally.** Every comparison on this page
        # is built on `skip_rate`, which is the share who scrolled past inside
        # three seconds. YouTube's `averageViewPercentage` scores a whole video
        # and TikTok exposes no retention metric at all, so a board for either
        # would be a page of tables built on a column those platforms leave at
        # zero. The filters below say so rather than relying on that zero, which
        # is a rule that holds by accident. F5.
        #
        # It said so by rendering nothing at all until now, so selecting the
        # YouTube account gave a page with a heading and empty space under it,
        # which reads as a broken page rather than as a question this data
        # cannot answer. The board is built either way and carries whether it
        # can be compared; the template is where that becomes a sentence.
        if account["platform"] != db.PLATFORM_INSTAGRAM:
            boards.append({"account": account, "comparable": False})
            continue
        account_id = account["account_id"]
        rows = await db.published_media(conn, account_id)
        readings = await db.latest_insights(
            conn, account_id, platform=db.PLATFORM_INSTAGRAM
        )
        # One flat row per post, so the analysis never has to know that the
        # numbers and the hook arrive from two different tables.
        merged = [
            {**dict(row), **{k: reading[k] for k in ("views", "reach", "skip_rate")}}
            for row in rows
            if (reading := readings.get(row["media_id"]))
        ]
        measured = [r for r in merged if r["skip_rate"]]
        # How long a Reel takes to stop moving, recomputed from this account's
        # own history rather than asserted. It decides which posts the cohorts
        # may count, so it is measured on the same page that applies it.
        settling = analysis.maturity(
            await db.insights_series(conn, account_id, platform=db.PLATFORM_INSTAGRAM)
        )
        by_slot = analysis.cohorts(
            merged, key=analysis.slot_of, settled=settling["settled"]
        )
        by_recipe = analysis.cohorts(
            merged, key=analysis.recipe_of, settled=settling["settled"]
        )
        boards.append(
            {
                "account": account,
                "comparable": True,
                "measured": len(measured),
                "total": len(rows),
                # The chart keeps every post. Skip rate settles at the second
                # reading and drifts a median 1.3 points afterwards, so holding
                # back the newest dots would hide the most recent evidence to
                # avoid an error smaller than the marker.
                "chart": analysis.skip_chart(merged),
                "settling": settling,
                "held_back": by_slot["held_back"],
                # Slots read in time order, because the question is the shape of
                # the day. Recipes have no order, so the biggest cohort leads.
                "by_slot": sorted(by_slot["groups"], key=lambda c: c["name"]),
                "by_recipe": sorted(
                    by_recipe["groups"], key=lambda c: (-c["n"], c["name"])
                ),
                "threshold": analysis.SKIP_THRESHOLD,
                "breakout": analysis.BREAKOUT_VIEWS,
            }
        )

    return templates.TemplateResponse(
        request,
        "insights.html",
        {"boards": boards, "cfg": cfg, "page": "insights", "scope": scope},
    )


@router.get("/repos", response_class=HTMLResponse, name="repos_page")
async def repos_page(request: Request) -> Any:
    """Which repos are spent, which are half spent, and which cost a render for
    nothing.

    This is the list that decides whether a video gets made tonight. Discovery
    reads `data/used_repos.json` on the machine that renders, which is a single
    JSON file on one laptop, outside git and outside every backup this project
    has; these tables are the durable copy on a volume that gets `VACUUM INTO`
    every six hours, and the Mac merges them back in before its first Search
    call. Until now nothing displayed either, so "have we already done this one"
    was a question you answered by running a command on the right machine.
    """
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)

    boards = []
    for account in scope["visible"]:
        account_id = account["account_id"]
        repos = analysis.repo_history(
            covered=await db.covered_repos(conn, account_id),
            rendered=await db.rendered_repos_list(conn, account_id),
            published=await db.published_media(conn, account_id),
            readings=await db.latest_insights(conn, account_id),
        )
        boards.append(
            {
                "account": account,
                "repos": repos,
                "blocked": sum(1 for r in repos if (r["days_left"] or 0) > 0),
                "stranded": sum(1 for r in repos if r["stranded"]),
                "cooldown": analysis.REPO_COOLDOWN_DAYS,
            }
        )

    return templates.TemplateResponse(
        request,
        "repos.html",
        {"boards": boards, "cfg": cfg, "page": "repos", "scope": scope},
    )


@router.get("/health", response_class=HTMLResponse, name="health_page")
async def health_page(request: Request) -> Any:
    conn, cfg = request.app.state.db, request.app.state.cfg
    scope = await _scope(request)
    moment = db.now()

    accounts = []
    for account in scope["visible"]:
        account_id = account["account_id"]
        expires = db.parse_iso(account["token_expires_at"])
        published = await db.published_media(conn, account_id, limit=1)
        slots = await db.active_slots(conn, account_id)
        depth = await db.queue_depth(conn, account_id)
        # Days of posting the queue can still cover. The number that says
        # whether to go and render, and the one a stacked board buried.
        approved = depth.get(db.QUEUE_APPROVED, 0)
        accounts.append(
            {
                "row": account,
                "expires": expires,
                "days_left": (expires - moment).total_seconds() / 86_400 if expires else None,
                "depth": depth,
                "funnel": await db.funnel(conn, account_id),
                "last_published": published[0] if published else None,
                "slots_per_day": len(slots),
                "runway_days": (approved / len(slots)) if slots else None,
                "insights_last": db.parse_iso(await db.last_insight_fetch(conn, account_id)),
            }
        )

    metrics = request.app.state.metrics
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "accounts": accounts,
            "cfg": cfg,
            "page": "health",
            "scope": scope,
            "poller_last": _gauge_time(metrics.poller_last_success),
            "scheduler_last": _gauge_time(metrics.scheduler_last_success),
            "insights_last": _gauge_time(metrics.insights_last_success),
            "counters": {
                "published": _counter(metrics.posts_published),
                "publish_failures": _counter(metrics.publish_failures),
                "slots_starved": _counter(metrics.slots_starved),
                "graph_errors": _counter(metrics.graph_errors),
                "signature_failures": _counter(metrics.webhook_signature_failures),
                "insights_fetched": _counter(metrics.insights_fetched),
            },
        },
    )


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


def _gauge_time(metric: Any) -> datetime | None:
    """A zero gauge means "has not run yet", not 1970."""
    value = _counter(metric)
    return datetime.fromtimestamp(value, tz=db.now().tzinfo) if value else None


# --------------------------------------------------------------------------
# Queue controls
# --------------------------------------------------------------------------


async def _require_row(request: Request, queued_id: int) -> Any:
    row = await db.get_queued(request.app.state.db, queued_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such queued post")
    return row


@router.post("/queue/{queued_id}/approve")
async def approve(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    if row["state"] not in (db.QUEUE_DRAFT, db.QUEUE_FAILED):
        return _back(request)

    retrying = row["state"] == db.QUEUE_FAILED
    if retrying and row["container_id"]:
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
    await db.set_queue_state(
        request.app.state.db, queued_id, db.QUEUE_APPROVED, reset_attempts=retrying
    )
    log.info("Queue %d armed from the admin UI", queued_id)
    return _back(request)


@router.post("/queue/{queued_id}/hold")
async def hold(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    if row["state"] == db.QUEUE_APPROVED:
        await db.set_queue_state(request.app.state.db, queued_id, db.QUEUE_DRAFT)
    return _back(request)


def _claim_is_stale(row: Any, *, moment: Any = None) -> bool:
    """Whether a `claimed` row has been held past any real publish attempt.

    Mirrors `db.stale_claims` so the button and the gauge agree about which
    rows are abandoned. `claimed_at` is null before schema 18, so `created_at`
    stands in, the same fallback and for the same reason.
    """
    held = db.parse_iso(row["claimed_at"] or row["created_at"])
    if held is None:
        return False
    return (moment or db.now()) - held >= db.CLAIM_STALE_AFTER


@router.post("/queue/{queued_id}/cancel")
async def cancel(request: Request, queued_id: int) -> Any:
    row = await _require_row(request, queued_id)
    if row["state"] == db.QUEUE_PUBLISHED:
        # Cancelling a post that went out would only make the record wrong.
        return _back(request)
    if row["state"] == db.QUEUE_CLAIMED and not _claim_is_stale(row):
        # A fresh claim is mid-flight and cancelling it races the publish. An
        # old one is a process that died holding it, and refusing that left
        # row 55 stuck for nine days with no way to resolve it from here.
        return _back(request)
    await db.set_queue_state(request.app.state.db, queued_id, db.QUEUE_CANCELLED)
    log.info("Queue %d cancelled from the admin UI", queued_id)
    return _back(request)


@router.post("/queue/{queued_id}/move")
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


@router.post("/queue/{queued_id}/edit")
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


@router.post("/slots/add")
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


@router.post("/slots/{slot_id}/toggle")
async def toggle_slot(
    request: Request, slot_id: int, active: Annotated[str, Form()] = "0"
) -> Any:
    await db.set_slot_active(request.app.state.db, slot_id, active == "1")
    return _back(request)


@router.post("/slots/{slot_id}/delete")
async def remove_slot(request: Request, slot_id: int) -> Any:
    await db.delete_slot(request.app.state.db, slot_id)
    return _back(request)


@router.post("/accounts/{account_id}/flags")
async def set_flags(
    request: Request,
    account_id: str,
    field: Annotated[str, Form()],
    value: Annotated[str, Form()] = "0",
) -> Any:
    """The kill switch, and the poller's on/off.

    `active` gates the comment poller and the scheduler both, which is what
    makes it a real stop rather than a partial one: pausing an account that
    keeps publishing would be a worse surprise than either behaviour alone.
    """
    if field not in ("active", "dm_enabled"):
        raise HTTPException(status_code=400, detail="unknown flag")
    await db.set_account_flags(
        request.app.state.db, account_id, **{field: value == "1"}
    )
    log.warning("Account %s: %s set to %s from the admin UI", account_id, field, value)
    return _back(request)
