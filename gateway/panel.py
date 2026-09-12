"""What each page of the panel is about, worked out before a template sees it.

The panel used to be eight tabs of data types, and every one of them looped the
destinations and rendered a board per account. That was the right shape for one
identity on one platform. At two identities on seven destinations it made a
32,000 pixel Posts page, the same video four times on the queue, and the same
paragraph seven times on Slots. At fifty it is not a page.

So the unit here is what the person running this thinks in:

- **A brand**, which is one identity and whose feed, cooldown list and schedule
  a page is about. `accounts.brand` is the grouping and it already existed.
- **A video**, which is one render fanned out to a queue row per platform. Rows
  that share `video_name` are one video, and a destination is a chip on it.
- **A destination**, which is a connection that is healthy or not and a place a
  video lands with its own numbers.

Nothing here writes. Every builder takes the connection and returns plain data,
so the routes in `admin.py` stay one call each and the arithmetic stays out of
Jinja.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote

from gateway import analysis, db, schedule, scheduler

PLATFORM_NAMES = {
    db.PLATFORM_INSTAGRAM: "Instagram",
    db.PLATFORM_YOUTUBE: "YouTube",
    db.PLATFORM_TIKTOK: "TikTok",
    db.PLATFORM_FACEBOOK: "Facebook",
}


@dataclass(frozen=True)
class Caps:
    """What one platform supports, in one place.

    Decided in six places before this: `measured_columns`, `has_funnel` on the
    Posts page, `comparable` on Insights, and `if platform ==` branches in three
    templates. Health missed the rule the other two followed and showed DM
    switches on YouTube. A table the templates read cannot miss it.
    """

    name: str
    # The column that scores one post on this platform, and what to call it.
    # None where the platform reports nothing that does.
    headline: str | None
    headline_label: str
    reach: bool
    watch: bool
    # The comment keyword and private reply mechanic, which is Instagram's.
    dm: bool
    # Where the credential's expiry lives: on the account row, in the TikTok
    # credentials table, or nowhere because it does not expire.
    token_clock: str


CAPS = {
    db.PLATFORM_INSTAGRAM: Caps("Instagram", "skip_rate", "skip", True, True, True, "account"),
    db.PLATFORM_YOUTUBE: Caps("YouTube", "avg_view_pct", "viewed", False, True, False, "none"),
    db.PLATFORM_TIKTOK: Caps("TikTok", None, "", False, False, False, "tiktok"),
    # Reach is not a fixed capability since Meta retired it for Reels; see
    # `analysis._MEASURED`.
    db.PLATFORM_FACEBOOK: Caps("Facebook", None, "", False, True, False, "none"),
}


def caps(platform: str | None) -> Caps:
    # Instagram for anything untaught, for the reason the account readers
    # default to it: an unknown platform renders what always rendered.
    return CAPS.get(str(platform or db.PLATFORM_INSTAGRAM), CAPS[db.PLATFORM_INSTAGRAM])


def names(platforms: list[str]) -> str:
    """"Instagram", "Instagram and YouTube", "Instagram, YouTube and TikTok"."""
    words = [PLATFORM_NAMES.get(p, p) for p in platforms]
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


# An Instagram token is refreshed by the gateway once it is inside
# `token_refresh_margin_days`, so days left above that margin are normal and a
# token more than a day under it is a refresh that is failing. Under this many
# days that failure is urgent.
TOKEN_BAD_DAYS_LEFT = 7
# TikTok's refresh token lasts a year and cannot be renewed without consent.
TIKTOK_WARN_DAYS = 30
# A destination that has published this many posts and stored no reading for
# any of them has a sweep that is not working, rather than posts that are new.
NO_READINGS_AFTER = 3
# How long a post may go without a reading before its absence is a gap rather
# than something still arriving.
PENDING_FOR = timedelta(hours=48)
# YouTube Analytics reports a day or two behind, so a Short can be three days
# old and still have nothing to report. Calling that a gap sends someone to
# debug a sweep that is fine.
PENDING_BY_PLATFORM = {db.PLATFORM_YOUTUBE: timedelta(hours=96)}
# A render host that has not rendered for this long, on a brand whose queue is
# not stocked, has probably stopped. Only with a thin queue, because the
# `--max-queue` ceiling stopping a batch is the normal outcome and a full queue
# renders nothing on purpose.
RENDER_WARN_AFTER = timedelta(hours=30)
RENDER_BAD_AFTER = timedelta(hours=54)
RENDER_STOCKED_DAYS = 2.0
PER_PAGE = 30

LEVELS = ("bad", "warn", "info")


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else len(LEVELS)


def display_tz(cfg: Any, slots: list[Any]) -> str:
    """The zone the schedule is written in, so a time reads as the audience's."""
    return str(slots[0]["tz"]) if slots else cfg.default_timezone


def claim_is_stale(row: Any, *, moment: datetime | None = None) -> bool:
    """Whether a `claimed` row has been held past any real publish attempt.

    Mirrors `db.stale_claims` so the button and the gauge agree. `claimed_at` is
    null before schema 18, so `created_at` stands in, the same fallback.
    """
    held = db.parse_iso(row["claimed_at"] or row["created_at"])
    if held is None:
        return False
    return (moment or db.now()) - held >= db.CLAIM_STALE_AFTER


_DIGEST = re.compile(r"-[0-9a-f]{6,}$")
_DEAD_UPLOAD = re.compile(r"\bContainer \S+ is (ERROR|EXPIRED)\b")


def upload_is_dead(row: Any) -> bool:
    """Whether a failed row's upload is one Meta will never publish.

    Read from the failure text, because rows that failed before the gateway
    said so in words carry only `Container <id> is ERROR`, and those are exactly
    the ones waiting for a decision.
    """
    return (
        str(row["state"]) == db.QUEUE_FAILED
        and bool(row["container_id"])
        and bool(_DEAD_UPLOAD.search(str(row["failure"] or "")))
    )


def subject_of(row: Any) -> str:
    """What a video is about, in words.

    The repository where there is one. The second brand's subjects are people
    and books, and its rows showed `james-cook-ad5bc1d6a00e.mp4` as a title, so
    the file name loses its digest and extension instead.
    """
    repo = str(row["repo_full_name"] or "")
    if repo:
        return repo
    stem = Path(str(row["video_name"] or "")).stem
    return _DIGEST.sub("", stem) or stem or "untitled"


# --------------------------------------------------------------------------
# Brands and destinations
# --------------------------------------------------------------------------


@dataclass
class Brand:
    name: str
    accounts: list[Any] = field(default_factory=list)

    @property
    def path(self) -> str:
        return quote(self.name, safe="")

    @property
    def monogram(self) -> str:
        # Letters rather than a colour, because the four platform hues are
        # reserved and every other hue here already means a state.
        stem = re.sub(r"^the(?=[a-z0-9])", "", self.name.lower())
        letters = re.sub(r"[^a-z0-9]", "", stem) or self.name
        return letters[:2].upper()

    @property
    def platforms(self) -> list[str]:
        return [str(a["platform"] or db.PLATFORM_INSTAGRAM) for a in self.accounts]

    def on(self, platform: str) -> Any | None:
        return next(
            (a for a in self.accounts if str(a["platform"] or db.PLATFORM_INSTAGRAM) == platform),
            None,
        )


def brand_name(row: Any) -> str:
    return str(row["brand"] or "") or db.brand_of(
        str(row["username"] or ""), str(row["account_id"])
    )


async def load_brands(conn: Any) -> list[Brand]:
    """Every identity, in the order `db` sorts accounts: brand, then platform."""
    grouped: dict[str, Brand] = {}
    for row in await db.all_accounts(conn, platform=None):
        grouped.setdefault(brand_name(row), Brand(brand_name(row))).accounts.append(row)
    return list(grouped.values())


def find_brand(brands: list[Brand], name: str) -> Brand | None:
    return next((b for b in brands if b.name == name), None)


def owner_of_unowned(brands: list[Brand]) -> str:
    """Which brand a render with no account belongs to.

    Blank rows predate `--account`, so they belong to whichever identity was
    registered first. Handing them to every brand is what put a repository the
    second brand never touched on its page.
    """
    stamps = [(str(a["created_at"] or ""), b.name) for b in brands for a in b.accounts]
    return min(stamps)[1] if stamps else ""


@dataclass
class Issue:
    """Something that needs a person, phrased for the inbox.

    `lead` carries `{where}` so the same problem on three destinations is one
    line naming three platforms rather than three lines.
    """

    level: str
    tag: str
    lead: str
    rest: str
    brand: str
    page: str
    platforms: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return self.lead.format(where=names(self.platforms))

    @property
    def short(self) -> str:
        """The headline without the platform, for a cell already under its name."""
        text = re.sub(r"\s+on$", "", self.lead.format(where="").strip()).strip()
        return text[:1].upper() + text[1:]


def merge_issues(issues: list[Issue]) -> list[Issue]:
    merged: dict[tuple[str, ...], Issue] = {}
    for issue in issues:
        key = (issue.brand, issue.level, issue.tag, issue.lead, issue.rest, issue.page)
        if key in merged:
            have = merged[key].platforms
            have.extend(p for p in issue.platforms if p not in have)
        else:
            merged[key] = Issue(**{**issue.__dict__, "platforms": list(issue.platforms)})
    return sorted(merged.values(), key=lambda i: (level_rank(i.level), i.brand, i.tag))


def group_across_brands(issues: list[Issue]) -> list[dict[str, Any]]:
    """The same problem on several brands as one line naming them.

    At fifty identities a batch of tokens expiring in the same week is fourteen
    identical rows, and the inbox stops being readable exactly when it matters.
    """
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for issue in issues:
        key = (issue.level, issue.tag, issue.headline, issue.rest, issue.page)
        entry = grouped.setdefault(key, {"issue": issue, "brands": []})
        if issue.brand not in entry["brands"]:
            entry["brands"].append(issue.brand)
    return sorted(
        grouped.values(),
        key=lambda e: (level_rank(e["issue"].level), -len(e["brands"]), e["issue"].tag),
    )


@dataclass
class Destination:
    row: Any
    brand: str
    armed: int = 0
    draft: int = 0
    failed: int = 0
    stale: int = 0
    slots_per_day: int = 0
    published: int = 0
    last_published: datetime | None = None
    readings: int = 0
    last_reading: datetime | None = None
    token_days: float | None = None
    # Whole days since the oldest failed post failed, for "9 days ago".
    failed_days: int | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def platform(self) -> str:
        return str(self.row["platform"] or db.PLATFORM_INSTAGRAM)

    @property
    def account_id(self) -> str:
        return str(self.row["account_id"])

    @property
    def handle(self) -> str:
        return str(self.row["username"] or "") or self.account_id

    @property
    def active(self) -> bool:
        return bool(self.row["active"])

    @property
    def caps(self) -> Caps:
        return caps(self.platform)

    @property
    def expires(self) -> bool:
        return self.caps.token_clock != "none"

    @property
    def runway(self) -> float | None:
        return self.armed / self.slots_per_day if self.slots_per_day else None

    @property
    def level(self) -> str:
        return min((i.level for i in self.issues), key=level_rank, default="ok")

    @property
    def token_level(self) -> str:
        return next((i.level for i in self.issues if i.tag == "Token"), "")

    @property
    def connection(self) -> tuple[str, str]:
        """The connection's own health, apart from anything wrong with a post.

        A destination whose posts failed is still connected, and showing
        "Failed" as its state read as a broken account.
        """
        if not self.active:
            return "Paused", "off"
        if self.token_days is not None and self.token_days < 0:
            return "Token expired", "bad"
        if self.token_level:
            return "Token needs attention", self.token_level
        return "Connected", "ok"

    @property
    def problems(self) -> list[Issue]:
        """Everything wrong that is not the connection itself."""
        return [i for i in self.issues if i.tag not in ("Paused", "Token")]


def no_readings_reason(platform: str, cfg: Any) -> str:
    """Why a destination has published and stored nothing, as far as it is known.

    TikTok's is known exactly. The sweep matches a TikTok video to its queue row
    by the title this service wrote, and the inbox upload sends no `post_info`,
    so on that path there is no title and no row is ever matched.
    """
    if platform == db.PLATFORM_TIKTOK:
        if not getattr(cfg, "tiktok_direct_post", False):
            return (
                "Inbox uploads carry no title, and the sweep matches TikTok videos to "
                "posts by title, so no post is ever matched."
            )
        return "Check that GATEWAY_TIKTOK_ENABLED is on and read the gateway log."
    if platform == db.PLATFORM_FACEBOOK:
        return (
            "The Page token needs the read_insights permission, which the consent trip did "
            "not ask for before 2026-09-11. Authorise the Page again with scripts/authorise.py."
        )
    if platform == db.PLATFORM_YOUTUBE:
        return (
            "Check that GATEWAY_YOUTUBE_INSIGHTS_ENABLED is on and that the channel's "
            "refresh token still mints."
        )
    return "Check that the insights sweep is on and that the token still reads insights."


def _ago_phrase(days: int | None) -> str:
    if days is None:
        return ""
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _token_issue(dest: Destination, cfg: Any) -> Issue | None:
    days = dest.token_days
    if days is None:
        return None
    where, brand = [dest.platform], dest.brand
    command = f"uv run python scripts/authorise.py {dest.platform} --account <name>"
    if days < 0:
        return Issue("bad", "Token", "The {where} token has expired",
                     f"An expired token cannot be refreshed. Authorise again: {command}",
                     brand, "setup", where)
    if dest.caps.token_clock == "account":
        margin = int(getattr(cfg, "token_refresh_margin_days", 15))
        if days >= margin - 1:
            return None
        return Issue(
            "bad" if days < TOKEN_BAD_DAYS_LEFT else "warn", "Token",
            f"{{where}} token was not refreshed, {int(days)} days left",
            f"The gateway refreshes it once it is inside {margin} days, so that refresh is "
            f"failing and the gateway log says why. If the token was revoked, authorise "
            f"again: {command}",
            brand, "setup", where,
        )
    if days < TIKTOK_WARN_DAYS:
        return Issue(
            "bad" if days < TOKEN_BAD_DAYS_LEFT else "warn", "Token",
            f"{{where}} authorisation lapses in {int(days)} days",
            f"It cannot be renewed without consent. Authorise again before it does: {command}",
            brand, "setup", where,
        )
    return None


def _issues(dest: Destination, cfg: Any) -> list[Issue]:
    """What is wrong with one destination, each with how to fix it.

    The `rest` of every issue is the fix, in the words of the page that holds
    it, because a problem the panel names and does not explain is a problem
    someone has to go and research.
    """
    where = [dest.platform]
    brand = dest.brand
    if not dest.active:
        return [
            Issue("info", "Paused", "{where} is paused",
                  "Nothing polls or publishes there until it is resumed on Setup.",
                  brand, "setup", where)
        ]
    found: list[Issue] = []
    if dest.failed:
        when = _ago_phrase(dest.failed_days)
        if dest.failed == 1:
            rest = f"It failed {when}. " if when else ""
            rest += "Open Schedule to see why, then retry it or give up on it."
        else:
            rest = f"The oldest failed {when}. " if when else ""
            rest += "Open Schedule to see why, then retry or give up on each."
        found.append(Issue(
            "bad", "Failed post",
            f"{dest.failed} post{'s' if dest.failed != 1 else ''} failed to publish on {{where}}",
            rest, brand, "schedule", where,
        ))
    if dest.stale:
        plural = "s" if dest.stale != 1 else ""
        found.append(Issue(
            "bad", "Stuck", f"{dest.stale} post{plural} on {{where}} never finished publishing",
            "Look for the post on the account, then give up on it on Schedule.",
            brand, "schedule", where,
        ))
    token = _token_issue(dest, cfg)
    if token is not None:
        found.append(token)
    if not dest.slots_per_day:
        found.append(Issue(
            "info", "No slot", "No active slot on {where}",
            "Nothing goes out there on its own. Add a GATEWAY_SLOTS line for the brand, "
            "or a slot on Setup.",
            brand, "setup", where,
        ))
    elif dest.armed == 0:
        found.append(Issue(
            "bad", "Empty", "Nothing armed on {where}",
            "The next slot fires into an empty queue. Render or approve a video for this brand.",
            brand, "schedule", where,
        ))
    elif dest.runway is not None and dest.runway <= 1:
        found.append(Issue(
            "warn", "Runway", f"{dest.runway:.1f} days of posts left on {{where}}",
            "Render or approve more before the queue runs dry.",
            brand, "schedule", where,
        ))
    if dest.published >= NO_READINGS_AFTER and dest.readings == 0:
        found.append(Issue(
            "warn", "No data", f"No readings stored for {dest.published} posts on {{where}}",
            no_readings_reason(dest.platform, cfg), brand, "performance", where,
        ))
    return found


async def load_destinations(
    conn: Any, cfg: Any, brands: list[Brand], *, moment: datetime
) -> dict[str, Destination]:
    """Every destination's state, from six grouped queries whatever the count."""
    depths = await db.queue_depth_by_account(conn)
    stale = await db.stale_claims_by_account(conn, moment=moment)
    slots: dict[str, int] = {}
    for slot in await db.active_slots(conn):
        slots[str(slot["account_id"])] = slots.get(str(slot["account_id"]), 0) + 1
    activity = await db.destination_activity(conn)
    tiktok_expiry = await db.tiktok_refresh_expiries(conn)
    failed_since = await db.failed_since_by_account(conn)

    out: dict[str, Destination] = {}
    for brand in brands:
        for row in brand.accounts:
            account_id = str(row["account_id"])
            seen = activity.get(account_id, {})
            dest = Destination(
                row=row,
                brand=brand.name,
                armed=depths.get((account_id, db.QUEUE_APPROVED), 0),
                draft=depths.get((account_id, db.QUEUE_DRAFT), 0),
                failed=depths.get((account_id, db.QUEUE_FAILED), 0),
                stale=stale.get(account_id, 0),
                slots_per_day=slots.get(account_id, 0),
                published=int(seen.get("published") or 0),
                last_published=db.parse_iso(seen.get("last_published_at")),
                readings=int(seen.get("readings") or 0),
                last_reading=db.parse_iso(seen.get("last_reading_at")),
            )
            clock = dest.caps.token_clock
            expires = None
            if clock == "account":
                expires = db.parse_iso(row["token_expires_at"])
            elif clock == "tiktok":
                expires = db.parse_iso(tiktok_expiry.get(account_id))
            if expires is not None:
                dest.token_days = (expires - moment).total_seconds() / 86_400
            failed_at = db.parse_iso(failed_since.get(account_id))
            if dest.failed and failed_at is not None:
                dest.failed_days = max(0, int((moment - failed_at).total_seconds() // 86_400))
            dest.issues = _issues(dest, cfg)
            out[account_id] = dest
    return out


@dataclass
class BrandState:
    brand: Brand
    destinations: list[Destination]
    issues: list[Issue]
    last_render: datetime | None = None
    # The render host's own report of its last run for this brand. Beside the
    # render time rather than instead of it: a run that rendered nothing
    # because the queue was full is a healthy night with no render.
    last_run_at: datetime | None = None
    last_run_outcome: str = ""
    next_out: dict[str, Any] | None = None

    @property
    def level(self) -> str:
        return min((i.level for i in self.issues), key=level_rank, default="ok")

    @property
    def open(self) -> int:
        return sum(1 for i in self.issues if i.level in ("bad", "warn"))

    @property
    def armed(self) -> int:
        return sum(d.armed for d in self.destinations)

    @property
    def published(self) -> int:
        return sum(d.published for d in self.destinations)

    @property
    def runway(self) -> float | None:
        values = [d.runway for d in self.destinations if d.active and d.runway is not None]
        return min(values) if values else None

    @property
    def thinnest(self) -> list[str]:
        low = self.runway
        return [
            d.platform for d in self.destinations
            if d.active and d.runway is not None and low is not None and d.runway == low
        ]

    @property
    def schedule_badge(self) -> int:
        return sum(d.failed + d.stale for d in self.destinations)


async def brand_state(
    conn: Any, brand: Brand, dests: dict[str, Destination], *, moment: datetime, owner: str
) -> BrandState:
    mine = [dests[str(a["account_id"])] for a in brand.accounts]
    state = BrandState(brand=brand, destinations=mine, issues=[])
    state.last_render = db.parse_iso(
        await db.last_rendered(
            conn, [d.account_id for d in mine], include_unowned=brand.name == owner
        )
    )
    run = await db.latest_run(conn, brand.name)
    if run:
        state.last_run_at = db.parse_iso(run["finished_at"] or run["started_at"])
        state.last_run_outcome = run["outcome"]
    issues = [issue for d in mine for issue in d.issues]
    runway = state.runway
    if (
        state.last_render
        and moment - state.last_render > RENDER_WARN_AFTER
        and (runway is None or runway < RENDER_STOCKED_DAYS)
    ):
        hours = int((moment - state.last_render).total_seconds() // 3600)
        issues.append(Issue(
            "bad" if moment - state.last_render > RENDER_BAD_AFTER else "warn",
            "Render", f"No render in {hours} hours",
            "The queue is thin and the render host may have stopped. Nothing else here would "
            "show it until the queue runs dry.",
            brand.name, "overview",
        ))
    state.issues = merge_issues(issues)
    return state


async def next_out(
    conn: Any, cfg: Any, state: BrandState, *, moment: datetime
) -> dict[str, Any] | None:
    """The soonest armed post across a brand's destinations."""
    soonest: dict[str, Any] | None = None
    for dest in state.destinations:
        if not dest.active or not dest.slots_per_day:
            continue
        tz = display_tz(cfg, await db.active_slots(conn, dest.account_id))
        for row, when in await scheduler.upcoming(conn, cfg, dest.account_id, moment=moment):
            if when is None or row["state"] != db.QUEUE_APPROVED:
                continue
            if soonest is None or when < soonest["when"]:
                soonest = {"row": row, "when": when, "platform": dest.platform, "tz": tz,
                           "subject": subject_of(row)}
    return soonest


async def shell(conn: Any, cfg: Any, *, moment: datetime) -> dict[str, Any]:
    """What every page's sidebar needs: brands, their state and the counts.

    Computed on every page so the counts in the sidebar and the Today inbox are
    one calculation and cannot disagree.
    """
    brands = await load_brands(conn)
    dests = await load_destinations(conn, cfg, brands, moment=moment)
    owner = owner_of_unowned(brands)
    states = [await brand_state(conn, b, dests, moment=moment, owner=owner) for b in brands]
    return {
        "brands": brands,
        "destinations": dests,
        "states": {s.brand.name: s for s in states},
        "owner": owner,
        "open": sum(s.open for s in states),
        "destinations_open": sum(1 for d in dests.values() if d.level in ("bad", "warn")),
        # Attention first, then by name, so the picker's first rows are the
        # brands worth opening. The full list is Today.
        "picker": sorted(states, key=lambda s: (level_rank(s.level), s.brand.name)),
    }


# --------------------------------------------------------------------------
# The schedule: videos rather than queue rows
# --------------------------------------------------------------------------


@dataclass
class Chip:
    """One destination's row for one video."""

    platform: str
    row: Any
    when: datetime | None
    tz: str
    stale: bool = False

    @property
    def state(self) -> str:
        return str(self.row["state"])


@dataclass
class Plan:
    key: str
    hook: str
    subject: str
    repo: str
    video_name: str
    chips: list[Chip] = field(default_factory=list)
    made: datetime | None = None
    # Checked on disk, like the Library's player, so a row whose file is gone
    # gets a line saying so rather than a dead control.
    media: bool = False
    cover: str = ""

    @property
    def first(self) -> datetime | None:
        times = [c.when for c in self.chips if c.when and c.state == db.QUEUE_APPROVED]
        return min(times) if times else None

    @property
    def tz(self) -> str:
        return self.chips[0].tz if self.chips else "UTC"

    def ids(self, *states: str) -> list[int]:
        return [int(c.row["id"]) for c in self.chips if c.state in states]


def video_key(row: Any) -> str:
    return str(row["video_name"] or "") or f"row:{row['id']}"


def _rank(platform: str) -> int:
    return db.PLATFORMS.index(platform) if platform in db.PLATFORMS else len(db.PLATFORMS)


async def plans(
    conn: Any, cfg: Any, brand: Brand, dests: dict[str, Destination], *,
    moment: datetime, platform: str = "",
) -> list[Plan]:
    grouped: dict[str, Plan] = {}

    def plan_for(row: Any) -> Plan:
        key = video_key(row)
        if key not in grouped:
            grouped[key] = Plan(key=key, hook=str(row["hook"] or ""), subject=subject_of(row),
                                repo=str(row["repo_full_name"] or ""),
                                video_name=str(row["video_name"] or ""))
        elif not grouped[key].hook and row["hook"]:
            grouped[key].hook = str(row["hook"])
        return grouped[key]

    zones: dict[str, str] = {}
    for account in brand.accounts:
        dest = dests[str(account["account_id"])]
        if platform and dest.platform != platform:
            continue
        zones[dest.account_id] = display_tz(cfg, await db.active_slots(conn, dest.account_id))
        for row, when in await scheduler.upcoming(conn, cfg, dest.account_id, moment=moment):
            plan_for(row).chips.append(Chip(dest.platform, row, when, zones[dest.account_id]))

    # The rows of the same videos that have already moved on, so a video half
    # out reads as one line with its sent and failed destinations beside it.
    for account_id, tz in zones.items():
        dest = dests[account_id]
        # Newest published first rather than queue order, which puts a
        # destination's whole history ahead of this morning's post.
        moved_on = [
            *await db.published_on(conn, account_id, limit=20),
            *await db.queued_posts(
                conn, account_id=account_id,
                states=(db.QUEUE_FAILED, db.QUEUE_CLAIMED), limit=40,
            ),
        ]
        for row in moved_on:
            key = video_key(row)
            if key in grouped and not any(c.row["id"] == row["id"] for c in grouped[key].chips):
                stale = row["state"] == db.QUEUE_CLAIMED and claim_is_stale(row, moment=moment)
                grouped[key].chips.append(
                    Chip(dest.platform, row, db.parse_iso(row["published_at"]), tz, stale)
                )

    made = await db.rendered_at_for(conn, [p.repo for p in grouped.values()])
    covers = Path(cfg.covers_dir)
    for plan in grouped.values():
        plan.chips.sort(key=lambda c: _rank(c.platform))
        plan.made = db.parse_iso(made.get(plan.repo))
        plan.media = bool(plan.video_name) and (covers / plan.video_name).is_file()
        names = (str(c.row["cover_name"] or "") for c in plan.chips)
        plan.cover = next((n for n in names if n and (covers / n).is_file()), "")
    far = datetime.max.replace(tzinfo=moment.tzinfo)
    return sorted(grouped.values(), key=lambda p: (p.first or far, p.key))


def by_day(plans_: list[Plan], moment: datetime) -> list[tuple[str, list[Plan]]]:
    """Plans under a heading per local day, then the ones nothing has timed."""
    days: dict[str, list[Plan]] = {}
    order: list[str] = []
    for plan in plans_:
        when = plan.first
        if when is None:
            label = "Not scheduled"
        else:
            zone = schedule.zone_or_utc(plan.tz)
            local, today = when.astimezone(zone).date(), moment.astimezone(zone).date()
            stamp = when.astimezone(zone).strftime("%a %d %b")
            label = (
                f"Today · {stamp}" if local == today
                else f"Tomorrow · {stamp}" if local == today + timedelta(days=1)
                else stamp
            )
        if label not in days:
            days[label] = []
            order.append(label)
        days[label].append(plan)
    if "Not scheduled" in order:
        order.remove("Not scheduled")
        order.append("Not scheduled")
    return [(label, days[label]) for label in order]


async def decisions(
    conn: Any, brand: Brand, dests: dict[str, Destination], *, moment: datetime
) -> list[dict[str, Any]]:
    """Failed rows and abandoned claims: the ones waiting on a person.

    Each carries what the template needs to say what it means: how old it is,
    whether the upload is one the platform will never publish, and where else
    the same video already went out.
    """
    platform_of = {
        str(a["account_id"]): str(a["platform"] or db.PLATFORM_INSTAGRAM) for a in brand.accounts
    }
    out: list[dict[str, Any]] = []
    for account in brand.accounts:
        dest = dests[str(account["account_id"])]
        for row in await db.queued_posts(
            conn, account_id=dest.account_id, states=(db.QUEUE_FAILED, db.QUEUE_CLAIMED), limit=30
        ):
            stale = row["state"] == db.QUEUE_CLAIMED and claim_is_stale(row, moment=moment)
            if row["state"] != db.QUEUE_FAILED and not stale:
                continue
            elsewhere: list[str] = []
            if row["video_name"]:
                for other in await db.rows_for_video(conn, str(row["video_name"])):
                    other_platform = platform_of.get(str(other["account_id"]))
                    if (
                        other_platform
                        and other_platform != dest.platform
                        and other["state"] == db.QUEUE_PUBLISHED
                        and other_platform not in elsewhere
                    ):
                        elsewhere.append(other_platform)
            held = db.parse_iso(row["claimed_at"] or row["created_at"])
            out.append({
                "platform": dest.platform,
                "row": row,
                "stale": stale,
                "dead": upload_is_dead(row),
                "subject": subject_of(row),
                "elsewhere": sorted(elsewhere, key=_rank),
                "age_days": (
                    max(0, int((moment - held).total_seconds() // 86_400)) if held else None
                ),
            })
    return out


async def slot_rules(conn: Any, brand: Brand) -> list[dict[str, Any]]:
    """A brand's slots, once per rule rather than once per destination.

    A `brand=` line gives every destination the same times, and the Slots page
    listed them four times with an add form under each.
    """
    rules: dict[tuple[Any, ...], dict[str, Any]] = {}
    for account in brand.accounts:
        platform = str(account["platform"] or db.PLATFORM_INSTAGRAM)
        for row in await db.all_slots(conn, str(account["account_id"])):
            key = (row["hour"], row["minute"], row["tz"], row["jitter_minutes"], row["days"] or "",
                   row["source"])
            rule = rules.setdefault(
                key,
                {"slot": schedule.Slot.from_row(row), "source": row["source"], "members": []},
            )
            rule["members"].append({"platform": platform, "row": row})
    return sorted(rules.values(), key=lambda r: (r["slot"].hour, r["slot"].minute))


# --------------------------------------------------------------------------
# The library: published videos, one row each
# --------------------------------------------------------------------------


@dataclass
class Post:
    platform: str
    row: Any
    reading: Any | None
    funnel: dict[str, int]
    # "pending" is too new to have numbers, "none" is a destination that has
    # stored no readings at all, "missing" is a gap on a destination that has.
    absent: str = ""

    def value(self, key: str) -> Any:
        return self.reading[key] if self.reading is not None else None


@dataclass
class Video:
    key: str
    hook: str
    subject: str
    video_name: str
    recipe: str
    posts: dict[str, Post] = field(default_factory=dict)
    published: datetime | None = None
    media: bool = False

    @property
    def views(self) -> int:
        return sum(
            int(p.reading["views"] or 0) for p in self.posts.values() if p.reading is not None
        )


async def library(
    conn: Any, cfg: Any, brand: Brand, dests: dict[str, Destination], *,
    moment: datetime, platform: str = "", sort: str = "published", page: int = 1,
    per_page: int = PER_PAGE,
) -> dict[str, Any]:
    videos: dict[str, Video] = {}
    for account in brand.accounts:
        dest = dests[str(account["account_id"])]
        if platform and dest.platform != platform:
            continue
        readings = await db.latest_insights(conn, dest.account_id)
        funnels = await db.per_post_funnel(conn, dest.account_id) if dest.caps.dm else {}
        for row in await db.published_media(conn, dest.account_id, limit=10_000):
            key = str(row["video_name"] or "") or f"media:{row['media_id']}"
            video = videos.setdefault(key, Video(
                key=key, hook=str(row["hook"] or ""), subject=subject_of(row),
                video_name=str(row["video_name"] or ""), recipe=str(row["recipe"] or ""),
            ))
            if not video.hook and row["hook"]:
                video.hook = str(row["hook"])
            published = db.parse_iso(row["published_at"])
            reading = readings.get(row["media_id"])
            absent = ""
            if reading is None:
                # Too new first, whatever the destination has stored. A post
                # published an hour ago on a brand-new destination is waiting,
                # not a sweep that has never worked.
                window = PENDING_BY_PLATFORM.get(dest.platform, PENDING_FOR)
                if published and moment - published < window:
                    absent = "pending"
                elif dest.readings == 0:
                    absent = "none"
                else:
                    absent = "missing"
            prior = video.posts.get(dest.platform)
            newer = prior is None or (
                str(row["published_at"] or "") > str(prior.row["published_at"] or "")
            )
            if newer:
                video.posts[dest.platform] = Post(dest.platform, row, reading,
                                                  funnels.get(row["media_id"], {}), absent)
            if published and (video.published is None or published < video.published):
                video.published = published

    epoch = datetime.min.replace(tzinfo=moment.tzinfo)
    ordered = sorted(videos.values(), key=lambda v: v.published or epoch, reverse=True)
    if sort == "views":
        ordered.sort(key=lambda v: v.views, reverse=True)
    pages = max(1, math.ceil(len(ordered) / per_page))
    page = min(max(1, page), pages)
    shown = ordered[(page - 1) * per_page : page * per_page]
    for video in shown:
        # Media is pruned after publish by design, so a player for a file that
        # is gone is 404s and a dead control rather than a preview.
        video.media = bool(video.video_name) and (Path(cfg.covers_dir) / video.video_name).is_file()

    present = [p for p in brand.platforms if not platform or p == platform]

    def read(p: str) -> list[Any]:
        return [
            v.posts[p].reading for v in ordered
            if p in v.posts and v.posts[p].reading is not None
        ]

    def mean(p: str, key: str) -> float | None:
        # Each figure over the posts that have it, so an unmeasured post never
        # pulls an average toward zero.
        values = [float(r[key]) for r in read(p) if r[key]]
        return sum(values) / len(values) if values else None

    summary = {}
    for p in present:
        found = caps(p)
        summary[p] = {
            "views": sum(int(r["views"] or 0) for r in read(p)),
            "read": len(read(p)),
            "posted": sum(1 for v in ordered if p in v.posts),
            "skip": mean(p, "skip_rate") if found.headline == "skip_rate" else None,
            "viewed": mean(p, "avg_view_pct") if found.headline == "avg_view_pct" else None,
            "watch_ms": mean(p, "avg_watch_ms") if found.watch else None,
            "reach": sum(int(r["reach"] or 0) for r in read(p)) if found.reach else None,
            "links_sent": (
                sum(v.posts[p].funnel.get("links_sent", 0) for v in ordered if p in v.posts)
                if found.dm else None
            ),
        }
    return {
        "videos": shown,
        "all": ordered,
        "total": len(ordered),
        "page": page,
        "pages": pages,
        "platforms": present,
        "summary": summary,
    }


def views_compare(videos: list[Video], *, limit: int = 10) -> dict[str, Any] | None:
    """Views per platform for the same renders, which only a video row can show.

    Views and nothing else, because every platform counts a view and none of
    the retention measures mean the same thing twice. Only videos read on at
    least two platforms, so a row never compares a number with an absence.
    """
    rows = []
    for video in videos:
        counts = {
            p: int(post.reading["views"] or 0)
            for p, post in video.posts.items()
            if post.reading is not None and post.reading["views"]
        }
        if len(counts) >= 2:
            rows.append({"subject": video.subject, "counts": counts})
        if len(rows) == limit:
            break
    if not rows:
        return None
    platforms = [p for p in db.PLATFORMS if any(p in r["counts"] for r in rows)]
    top = max(v for r in rows for v in r["counts"].values()) or 1
    for row in rows:
        row["bars"] = [
            {"platform": p, "views": row["counts"][p], "pct": 100 * row["counts"][p] / top}
            for p in platforms if p in row["counts"]
        ]
    totals = {p: sum(r["counts"].get(p, 0) for r in rows) for p in platforms}
    return {"rows": rows, "platforms": platforms, "totals": totals}


# --------------------------------------------------------------------------
# Performance, each platform in its own terms
# --------------------------------------------------------------------------


async def _instagram_board(conn: Any, account_id: str) -> dict[str, Any]:
    rows = await db.published_media(conn, account_id)
    readings = await db.latest_insights(conn, account_id, platform=db.PLATFORM_INSTAGRAM)
    merged = [
        {**dict(row), **{k: reading[k] for k in ("views", "reach", "skip_rate")}}
        for row in rows
        if (reading := readings.get(row["media_id"]))
    ]
    measured = [r for r in merged if r["skip_rate"]]
    settling = analysis.maturity(
        await db.insights_series(conn, account_id, platform=db.PLATFORM_INSTAGRAM)
    )
    by_slot = analysis.cohorts(merged, key=analysis.slot_of, settled=settling["settled"])
    by_recipe = analysis.cohorts(merged, key=analysis.recipe_of, settled=settling["settled"])
    return {
        "measured": len(measured),
        "total": len(rows),
        "chart": analysis.skip_chart(merged),
        "settling": settling,
        "held_back": by_slot["held_back"],
        "by_slot": sorted(by_slot["groups"], key=lambda c: c["name"]),
        "by_recipe": sorted(by_recipe["groups"], key=lambda c: (-c["n"], c["name"])),
        "median_skip": median([float(r["skip_rate"]) for r in measured]) if measured else None,
        "threshold": analysis.SKIP_THRESHOLD,
        "breakout": analysis.BREAKOUT_VIEWS,
    }


def _median_of(posts: list[dict[str, Any]], key: str) -> float | None:
    values = [float(p["reading"][key]) for p in posts if p["reading"][key]]
    return median(values) if values else None


@dataclass(frozen=True)
class Column:
    """One column of a platform's per post table.

    `key` is an `insights` column or a name in that row's `extra`. **A column is
    shown only when some post on the page has a value for it**, so a metric a
    platform refused, or one it has not started sending, is an absent column
    rather than a row of zeroes. That is the rule every board here keeps, made
    automatic for the metrics nobody chose one at a time.
    """

    key: str
    label: str
    # count, ms, pct, per_k (per thousand views), curve_half (YouTube's
    # retention list) or segments_half (a Page's retention graph).
    kind: str = "count"


_CORE_COLUMNS = frozenset({
    "views", "reach", "likes", "comments", "saved", "shares",
    "avg_watch_ms", "total_watch_ms", "skip_rate", "avg_view_pct",
})

# Only the columns each platform measures, for the reason `analysis._MEASURED`
# gives: a core column a platform never fills would render its absence as 0.
POST_COLUMNS: dict[str, tuple[Column, ...]] = {
    db.PLATFORM_INSTAGRAM: (
        Column("views", "Views"),
        Column("reach", "Reach"),
        Column("avg_watch_ms", "Watch", "ms"),
        Column("skip_rate", "Skip", "pct"),
        Column("saved", "Saves/1k", "per_k"),
        Column("shares", "Shares"),
        Column("reposts", "Reposts"),
        Column("total_interactions", "Interactions"),
        Column("facebook_views", "On Facebook"),
    ),
    db.PLATFORM_YOUTUBE: (
        Column("views", "Views"),
        Column("engagedViews", "Engaged"),
        Column("avg_view_pct", "Viewed", "pct"),
        Column("avg_watch_ms", "Watch", "ms"),
        Column("retention", "At half", "curve_half"),
        Column("subscribersGained", "Subs gained"),
        Column("subscribersLost", "Subs lost"),
        Column("likes", "Likes"),
        Column("shares", "Shares"),
    ),
    db.PLATFORM_FACEBOOK: (
        Column("views", "Views"),
        Column("fb_reels_replay_count", "Replays"),
        # From `extra` rather than the `reach` column, which is 0 on a Page
        # Meta gives no post reach for. See `facebook.read_post_reach`.
        Column("post_total_media_view_unique", "Reach"),
        Column("avg_watch_ms", "Watch", "ms"),
        Column("post_video_retention_graph", "At half", "segments_half"),
        Column("post_video_followers", "Follows"),
        Column("likes", "Reactions"),
        Column("comments", "Comments"),
    ),
    db.PLATFORM_TIKTOK: (
        Column("views", "Views"),
        Column("likes", "Likes"),
        Column("comments", "Comments"),
        Column("shares", "Shares"),
    ),
}


def _curve_half(curve: Any) -> float | None:
    """YouTube's watch ratio at the point nearest half way through."""
    if not isinstance(curve, list):
        return None
    points = [p for p in curve if isinstance(p, list) and len(p) >= 2]
    if not points:
        return None
    return float(min(points, key=lambda p: abs(float(p[0]) - 0.5))[1])


def _segments_half(graph: Any) -> float | None:
    """A Page's plays still going at the middle segment, as a share of the first.

    Normalised by the first segment rather than read raw, because Meta
    documents the values as a percentage and has been seen sending fractions,
    and a ratio of two values in the same unit is right either way.
    """
    if not isinstance(graph, dict):
        return None
    try:
        segments = sorted((int(k), float(v)) for k, v in graph.items())
    except (TypeError, ValueError):
        return None
    if not segments or not segments[0][1]:
        return None
    return segments[len(segments) // 2][1] / segments[0][1]


def cell(column: Column, reading: Any, extra: Mapping[str, Any]) -> str | None:
    """One post's value for one column, formatted, or None if it has none."""
    value = reading[column.key] if column.key in _CORE_COLUMNS else extra.get(column.key)
    if value is None:
        return None
    if column.kind == "curve_half":
        share = _curve_half(value)
        return None if share is None else f"{100 * share:.0f}%"
    if column.kind == "segments_half":
        share = _segments_half(value)
        return None if share is None else f"{100 * share:.0f}%"
    if not isinstance(value, (int, float)):
        return None
    if column.kind == "per_k":
        views = float(reading["views"] or 0)
        return f"{1000 * float(value) / views:.1f}" if views else None
    if column.kind == "ms":
        return f"{value / 1000:.0f} s"
    if column.kind == "pct":
        return f"{value:.1f}%"
    return f"{int(value):,}"


def post_table(platform: str, posts: list[dict[str, Any]]) -> dict[str, Any]:
    """The columns some post has a value for, and each post's cells in them."""
    columns = POST_COLUMNS.get(platform, POST_COLUMNS[db.PLATFORM_INSTAGRAM])
    cells = [[cell(c, p["reading"], p["extra"]) for c in columns] for p in posts]
    shown = [i for i in range(len(columns)) if any(row[i] is not None for row in cells)]
    return {
        "columns": [columns[i] for i in shown],
        "rows": [
            {"post": post, "cells": [row[i] for i in shown]}
            for post, row in zip(posts, cells, strict=True)
        ],
    }


# The day totals worth a word each, per platform's own name, in the order shown.
# Anything else in a reading stays stored and unshown.
DAY_LABELS = {
    "views": "views",
    "reach": "reached",
    "accounts_engaged": "engaged",
    "total_interactions": "interactions",
    "profile_links_taps": "link taps",
    "page_media_view": "views",
    "page_total_media_view_unique": "reached",
    "page_post_engagements": "engagements",
    "page_daily_follows_unique": "follows",
    "page_daily_unfollows_unique": "unfollows",
    "page_views_total": "Page views",
}
LIFETIME_LABELS = {"media_count": "posts", "videoCount": "videos", "viewCount": "lifetime views"}


async def audience(conn: Any, dest: Destination) -> dict[str, Any] | None:
    """One destination's followers and their movement, and its last day's totals."""
    series = await db.account_insights_series(conn, dest.account_id)
    if not series:
        return None
    extra = db.extra_of(series[-1])
    day = extra.get("day") if isinstance(extra.get("day"), dict) else {}
    return {
        **(analysis.audience(series) or {"followers": None}),
        "day_on": day.get("on"),
        "totals": [
            (label, int(day[name]))
            for name, label in DAY_LABELS.items()
            if isinstance(day.get(name), (int, float))
        ],
        "lifetime": [
            (label, int(extra[name]))
            for name, label in LIFETIME_LABELS.items()
            if isinstance(extra.get(name), (int, float))
        ],
    }


async def performance(
    conn: Any, cfg: Any, brand: Brand, dests: dict[str, Destination]
) -> list[dict[str, Any]]:
    sections = []
    for account in brand.accounts:
        dest = dests[str(account["account_id"])]
        readings = await db.latest_insights(conn, dest.account_id, platform=dest.platform)
        posts = [
            {
                "row": row,
                "reading": readings[row["media_id"]],
                "extra": db.extra_of(readings[row["media_id"]]),
                "subject": subject_of(row),
            }
            for row in await db.published_media(conn, dest.account_id, limit=60)
            if row["media_id"] in readings
        ][:12]
        section: dict[str, Any] = {
            "platform": dest.platform,
            "dest": dest,
            "audience": await audience(conn, dest),
            "table": post_table(dest.platform, posts),
        }
        if dest.platform == db.PLATFORM_INSTAGRAM:
            section.update(await _instagram_board(conn, dest.account_id))
        else:
            section.update({
                "posts": posts,
                "median_views": _median_of(posts, "views"),
                "median_viewed": _median_of(posts, "avg_view_pct"),
                "median_watch_ms": _median_of(posts, "avg_watch_ms"),
                "median_reach": _median_of(posts, "reach"),
                "reason": (
                    no_readings_reason(dest.platform, cfg)
                    if not posts and dest.published >= NO_READINGS_AFTER
                    else ""
                ),
            })
        sections.append(section)
    return sections


# --------------------------------------------------------------------------
# Subjects, setup, calendar and search
# --------------------------------------------------------------------------


async def subjects(conn: Any, brand: Brand, *, owner: str, status: str = "") -> dict[str, Any]:
    """The cooldown list for one identity, which is per brand rather than per row.

    Posted dates and scores come from Instagram where the brand has it, since
    that is the platform the skip column means anything on, and from every
    destination otherwise.
    """
    covered: list[Any] = []
    rendered: list[Any] = []
    published: list[Any] = []
    readings: dict[str, Any] = {}
    has_instagram = db.PLATFORM_INSTAGRAM in brand.platforms
    for account in brand.accounts:
        account_id = str(account["account_id"])
        platform = str(account["platform"] or db.PLATFORM_INSTAGRAM)
        covered += await db.covered_repos(conn, account_id, limit=10_000)
        rendered += await db.rendered_repos_list(
            conn, account_id, limit=10_000, include_unowned=brand.name == owner
        )
        if not has_instagram or platform == db.PLATFORM_INSTAGRAM:
            published += await db.published_media(conn, account_id, limit=10_000)
        if platform == db.PLATFORM_INSTAGRAM:
            readings.update(await db.latest_insights(conn, account_id, platform=platform))
    repos = analysis.repo_history(
        covered=covered, rendered=rendered, published=published, readings=readings
    )
    counts = {
        "cooldown": sum(1 for r in repos if (r["days_left"] or 0) > 0),
        "stranded": sum(1 for r in repos if r["stranded"]),
    }
    if status == "cooldown":
        repos = [r for r in repos if (r["days_left"] or 0) > 0]
    elif status == "stranded":
        repos = [r for r in repos if r["stranded"]]
    return {"repos": repos, "counts": counts, "cooldown": analysis.REPO_COOLDOWN_DAYS,
            "status": status}


async def setup(conn: Any, brand: Brand, dests: dict[str, Destination]) -> list[dict[str, Any]]:
    registered = {d["account_id"]: d for d in await db.registered_destinations(conn)}
    out = []
    for account in brand.accounts:
        dest = dests[str(account["account_id"])]
        out.append({
            "dest": dest,
            "registration": registered.get(dest.account_id, {}),
            "slots": [
                {"row": row, "slot": schedule.Slot.from_row(row)}
                for row in await db.all_slots(conn, dest.account_id)
            ],
        })
    return out


async def calendar(
    conn: Any, cfg: Any, brands: list[Brand], dests: dict[str, Destination], *,
    moment: datetime, days: int = 7,
) -> list[tuple[str, list[tuple[Brand, Plan]]]]:
    """Every brand's timed videos for the next week, by local day."""
    entries: list[tuple[datetime, Brand, Plan]] = []
    horizon = moment + timedelta(days=days)
    for brand in brands:
        for plan in await plans(conn, cfg, brand, dests, moment=moment):
            if plan.first and plan.first <= horizon:
                entries.append((plan.first, brand, plan))
    entries.sort(key=lambda e: e[0])
    grouped = by_day([plan for _, _, plan in entries], moment)
    owner = {id(plan): brand for _, brand, plan in entries}
    return [(label, [(owner[id(p)], p) for p in group]) for label, group in grouped]


async def search(conn: Any, brands: list[Brand], query: str) -> list[dict[str, Any]]:
    """Videos whose hook, subject or file name matches, grouped by brand and video."""
    query = query.strip()
    if len(query) < 2:
        return []
    where = {
        str(a["account_id"]): (b, str(a["platform"] or db.PLATFORM_INSTAGRAM))
        for b in brands for a in b.accounts
    }
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for row in await db.search_queue(conn, query):
        owner = where.get(str(row["account_id"]))
        if owner is None:
            continue
        brand, platform = owner
        key = (brand.name, video_key(row))
        hit = found.setdefault(key, {"brand": brand, "subject": subject_of(row),
                                     "hook": str(row["hook"] or ""), "rows": []})
        hit["rows"].append({"platform": platform, "row": row})
    for hit in found.values():
        hit["rows"].sort(key=lambda r: _rank(r["platform"]))
    return list(found.values())[:40]
