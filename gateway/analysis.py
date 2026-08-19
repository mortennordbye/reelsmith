"""Turning the published Reels into groups that can be compared.

The Posts page lists posts. Listing is not comparing, and every comparison this
account has made was worked out by hand in a session and pasted into a notes
file, where it goes stale silently and the only symptom is a worse decision
weeks later.

Two groupings, because they are the two questions worth asking and neither
could be answered from anything this service printed.

`recipe` is "did the change work". Three posts a day go out while the prompt is
edited daily, so rows are comparable when their recipes match and not when they
do not. Publish dates cannot stand in: the queue runs days deep by design, so a
change lands in the middle of a line of older videos and the two are
indistinguishable by date.

`slot` is "does it matter when it goes out", and it is close to a natural
experiment, which almost nothing here is. Which slot a post lands in comes from
its queue position rather than from anything about the video.

Pure functions on plain rows, with no database and no FastAPI, because the
arithmetic is the part worth testing and the part most likely to be quietly
wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from statistics import median
from typing import Any

# The threshold everything is judged against. Measured on this account over 58
# posts: those under it took a median 909 views against 122 for those over 70
# percent. It is not a benchmark from anywhere else and it is not a round
# number chosen for looking tidy.
SKIP_THRESHOLD = 60.0

# What counts as a post that actually reached anybody. Eight of the first 58
# carried a third of all views, so the median describes the post that failed
# and the tail is where the account's value is. A cohort's median can be
# identical to another's while this differs fourfold.
BREAKOUT_VIEWS = 500

# Posts per step of the trend line. Small enough to move inside a fortnight of
# posting, wide enough that one breakout does not become a trend.
TREND_WINDOW = 7


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC)
    except ValueError:
        return None


def slot_of(row: Any) -> str:
    """The hour a post went out, not the minute.

    The scheduler jitters every slot by an offset derived from its id and the
    local date, precisely so a restart cannot fire the same slot twice. The
    side effect is that no two posts share a publish time, and grouping on the
    exact stamp gave 43 cohorts of one out of 58 posts.

    A slot that straddles an hour boundary splits into two rows. That is
    visible and honest; clustering nearby times would be guessing at the
    schedule from its own output.
    """
    when = _parse(row.get("published_at"))
    return f"{when:%H}:00 UTC" if when else "unknown"


def recipe_of(row: Any) -> str:
    """The checkout and settings that wrote the script, or a name for having none.

    Empty is a claim rather than a gap: nothing recorded what made this video.
    It is comparable to nothing, and saying so keeps it out of a cohort it does
    not belong in.
    """
    return str(row.get("recipe") or "") or "before recipes"


def cohorts(
    rows: Iterable[Any], *, key: Callable[[Any], str], now: datetime | None = None
) -> list[dict]:
    """Group rows and describe each group.

    Ages are carried because a young cohort has had less time to accumulate
    views, which flatters its skip rate and punishes its views. A comparison
    across cohorts of very different age is not a comparison, and the only way
    for a reader to know is for the number to be on the page.
    """
    now = now or datetime.now(UTC)
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        # A zero skip rate is not a perfect opening, it is a post too young to
        # have a reading. The newest post always has one, and counting it would
        # drag every cohort it lands in toward a number nobody earned.
        if not row.get("skip_rate"):
            continue
        grouped.setdefault(key(row), []).append(row)

    out = []
    for name, group in grouped.items():
        skips = [float(r["skip_rate"]) for r in group]
        views = [int(r.get("views") or 0) for r in group]
        ages = [(now - w).days for w in (_parse(r.get("published_at")) for r in group) if w]
        good = sum(1 for s in skips if s < SKIP_THRESHOLD)
        big = sum(1 for v in views if v > BREAKOUT_VIEWS)
        out.append(
            {
                "name": name,
                "n": len(group),
                "skip": median(skips),
                "views": median(views),
                "best": max(views),
                "breakouts": big,
                "breakout_share": 100 * big / len(group),
                "under_threshold": good,
                "under_share": 100 * good / len(group),
                "age_days": median(ages) if ages else None,
            }
        )
    return out


def trend(values: Sequence[float], window: int = TREND_WINDOW) -> list[float | None]:
    """A trailing median over `window` posts, None until there are enough.

    Trailing rather than centred, because "the median of the last seven" is a
    sentence somebody can check against the list underneath it. None rather
    than a partial window at the start, because a median of two posts drawn on
    the same line as a median of seven reads as the same kind of evidence.
    """
    return [
        median(values[i - window + 1 : i + 1]) if i >= window - 1 else None
        for i in range(len(values))
    ]


# Chart geometry. Computed here rather than in the template, because it is
# arithmetic and arithmetic is testable; the template only draws what it is
# handed. The viewBox is fixed and the SVG scales to its container, so there is
# no measurement step and nothing to go wrong on a phone.
CHART = {"w": 720, "h": 240, "left": 34, "right": 14, "top": 12, "bottom": 24}


def skip_chart(rows: Sequence[Any], *, window: int = TREND_WINDOW) -> dict | None:
    """Skip rate per post over time, with a trailing median through it.

    One measure on one axis. Views deliberately does not share this chart: two
    scales on one plot is the most common way a chart lies, and views here runs
    from 86 to 1614, so a linear axis holding both would be a flat line with one
    spike. Views lives in the cohort tables underneath, as medians and as a
    count of how many cleared 500, which is what the shape of that distribution
    can honestly support.

    The axis runs the full 0 to 100. Skip rate is a percentage where both ends
    mean something, and cropping it to the range the data happens to occupy
    would make a two point drift look like a collapse.

    Every post is a dot and the line is the same series smoothed, not a second
    one, so both wear the same hue and differ by weight. Two steps of one hue
    used as two series is exactly the palette that fails a separation check.
    """
    points = [
        (when, float(r["skip_rate"]), r)
        for r in rows
        if r.get("skip_rate") and (when := _parse(r.get("published_at")))
    ]
    if len(points) < 2:
        return None
    points.sort(key=lambda p: p[0])

    first, last = points[0][0], points[-1][0]
    span = (last - first).total_seconds() or 1.0
    plot_w = CHART["w"] - CHART["left"] - CHART["right"]
    plot_h = CHART["h"] - CHART["top"] - CHART["bottom"]

    def x_of(when: datetime) -> float:
        return CHART["left"] + plot_w * (when - first).total_seconds() / span

    def y_of(skip: float) -> float:
        return CHART["top"] + plot_h * (1 - skip / 100)

    smoothed = trend([p[1] for p in points], window)
    dots = [
        {
            "x": round(x_of(when), 1),
            "y": round(y_of(skip), 1),
            "skip": skip,
            "repo": row.get("repo_full_name") or "",
            "hook": row.get("hook") or "",
            "views": int(row.get("views") or 0),
            "on": f"{when:%Y-%m-%d %H:%M} UTC",
            # Under the threshold is the outcome worth spotting in a field of
            # dots, and it is marked by a ring rather than by a second colour so
            # the distinction survives being printed or read without colour.
            "good": skip < SKIP_THRESHOLD,
        }
        for (when, skip, row) in points
    ]
    return {
        **CHART,
        "dots": dots,
        "trend": " ".join(
            f"{round(x_of(w), 1)},{round(y_of(v), 1)}"
            for (w, _, _), v in zip(points, smoothed, strict=True)
            if v is not None
        ),
        "window": window,
        "threshold": SKIP_THRESHOLD,
        "threshold_y": round(y_of(SKIP_THRESHOLD), 1),
        # The label sits on the plot, because a reference line nobody can name
        # is a line nobody can use, and it carries its own background chip
        # because the dots and the trend run straight through where it goes.
        # Width is computed rather than measured: the face is monospace at 10px,
        # so a character is very close to 6px and there is nothing to measure.
        "threshold_label": f"under {SKIP_THRESHOLD:.0f}% is the target",
        "threshold_label_w": len(f"under {SKIP_THRESHOLD:.0f}% is the target") * 6 + 10,
        "baseline_y": round(y_of(0), 1),
        "grid": [
            {"y": round(y_of(v), 1), "label": f"{v:.0f}%"} for v in (0, 20, 40, 60, 80, 100)
        ],
        # Three ticks only. The x axis is here to say "this is roughly three
        # weeks", not to let anyone read a date off a dot; the dot's own tooltip
        # does that exactly.
        # Anchored inward at the ends, or half of the first and last labels sits
        # outside the viewBox and is clipped by the container.
        "x_ticks": [
            {"x": round(x_of(w), 1), "label": f"{w:%d %b}", "anchor": anchor}
            for w, anchor in (
                (first, "start"),
                (first + (last - first) / 2, "middle"),
                (last, "end"),
            )
        ],
    }
