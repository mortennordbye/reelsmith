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

import contextlib
import json
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from statistics import median
from typing import Any

# The threshold everything is judged against. Measured on this account over 58
# posts: those under it took a median 909 views against 122 for those over 70
# percent. It is not a benchmark from anywhere else and it is not a round
# number chosen for looking tidy.
# Which numbers a platform actually reports, in the order they are shown.
#
# Instagram's set is Meta's REELS metrics. TikTok's `/v2/video/query/` returns
# four counts and nothing else: no reach, no saves, and nothing about watch
# time, completion or anything a three second skip could be computed from. A
# TikTok row rendered with Instagram's set is a post that got zero reach and
# zero saves, which is a claim rather than an absence. F5.
#
# YouTube's Analytics report has the same four counts and neither reach nor
# saves. What it does have, and TikTok has not, is watch time, which is why the
# retention tiles below are keyed on the reading rather than on the platform.
_MEASURED = {
    "instagram": ("views", "reach", "likes", "comments", "saved", "shares"),
    "youtube": ("views", "likes", "comments", "shares"),
    "tiktok": ("views", "likes", "comments", "shares"),
}


def measured_columns(platform: str | None) -> tuple[str, ...]:
    """The metrics one platform has, defaulting to Instagram's.

    Defaulting rather than raising, for the same reason the account readers
    default to Instagram: a platform nobody has taught this function about
    should render the set that has always been rendered, not an empty board.
    """
    return _MEASURED.get(str(platform or ""), _MEASURED["instagram"])


SKIP_THRESHOLD = 60.0

# What counts as a post that actually reached anybody. Eight of the first 58
# carried a third of all views, so the median describes the post that failed
# and the tail is where the account's value is. A cohort's median can be
# identical to another's while this differs fourfold.
BREAKOUT_VIEWS = 500

# Posts per step of the trend line. Small enough to move inside a fortnight of
# posting, wide enough that one breakout does not become a trend.
TREND_WINDOW = 7


# How many daily readings a post needs before its numbers can be compared with
# an older post's. Derived rather than chosen: `maturity` recomputes the curve
# from this account's own history every time the page is drawn, and on 59 posts
# it read 71 percent of final views at the first reading, 91 at the second and
# 99 at the third. Three is where the remaining error drops under the noise in
# everything else on the page.
#
# Not written into prose anywhere, because a fact about the numbers goes stale
# silently. The page prints the curve it actually measured next to the rule it
# applied.
SETTLED_READINGS = 3


def maturity(series: Mapping[str, Sequence[Any]]) -> dict:
    """How much of a Reel's final audience has arrived by reading N.

    Every comparison in this project has been hedged with "a post from this
    morning is not comparable with one from last week", which is true and far
    too cautious: the hedge was written from intuition and this is the table
    that knows. Turning it into a number is what lets the cohorts stop printing
    an age column and asking the reader to judge.

    Only posts watched from early enough and for long enough are used. A post
    whose first reading came days after it published has no visible climb, and
    counting it would report the curve as flatter than it is.
    """
    tracked = [rows for rows in series.values() if len(rows) >= SETTLED_READINGS * 3]
    curve = []
    for index in range(SETTLED_READINGS + 1):
        shares = [
            rows[index]["views"] / rows[-1]["views"]
            for rows in tracked
            if len(rows) > index and rows[-1]["views"]
        ]
        if shares:
            curve.append({"reading": index, "share": 100 * median(shares)})
    return {
        "curve": curve,
        "n": len(tracked),
        "settled_after": SETTLED_READINGS,
        # A post is settled once it has been read this many times. Counted
        # rather than aged, because the sweep is what produces a reading and a
        # sweep that did not run leaves a post younger than the calendar says.
        "settled": {
            media for media, rows in series.items() if len(rows) >= SETTLED_READINGS
        },
    }


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
    rows: Iterable[Any],
    *,
    key: Callable[[Any], str],
    settled: Container[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Group rows and describe each group.

    `settled` is the set of media that have been read enough times for their
    numbers to have stopped moving, from `maturity`. Unsettled posts are held
    back rather than shown with a caveat: a Reel reaches 71 percent of its
    final views at its first reading, so a cohort holding yesterday's post is
    not reporting a worse slot, it is reporting a post that has not finished
    arriving. This page previously printed an age column and left the reader to
    make that correction by eye, which is arithmetic dressed up as a warning.

    Passing nothing keeps every post, which is what a caller with no reading
    history should get: fewer posts is better than wrong ones, but no history
    at all is not evidence that a post is unsettled.

    Returns the groups and the number held back together, because a table of
    cohorts that quietly dropped four posts is a table that reads as covering
    everything. The count is one number about the call, not a property of any
    group, so it does not ride on the rows.
    """
    now = now or datetime.now(UTC)
    grouped: dict[str, list[Any]] = {}
    held_back = 0
    for row in rows:
        # A zero skip rate is not a perfect opening, it is a post too young to
        # have a reading. The newest post always has one, and counting it would
        # drag every cohort it lands in toward a number nobody earned.
        if not row.get("skip_rate"):
            continue
        if settled is not None and row.get("media_id") not in settled:
            held_back += 1
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
    return {"groups": out, "held_back": held_back}


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


# Mirrors `REPO_COOLDOWN_DAYS` in the pipeline's `config.py`, which this service
# deliberately cannot import: the gateway holds no pipeline code, which is what
# keeps its image free of the models and the voice. Duplicated rather than
# guessed at, and the consequence of the two drifting is bounded, because
# discovery reads its own value and this one only decides a word on a page.
REPO_COOLDOWN_DAYS = 30


def repo_history(
    *,
    covered: Iterable[Any],
    rendered: Iterable[Any],
    published: Iterable[Any],
    readings: Any,
    now: datetime | None = None,
) -> list[dict]:
    """Every repo this account has touched, and how far each one got.

    The list that decides whether a video gets made tonight, which until now
    existed as `data/used_repos.json` on one laptop and two tables nothing
    displayed. Discovery reads the laptop copy; this is the durable side of it,
    and being able to see it is the difference between "have we done this one"
    being a question and being a lookup.

    Three records of deliberately different strength, kept distinguishable
    rather than flattened into one date:

    - **Covered** is a commitment. It starts the cooldown and is the only one
      of the three that blocks discovery.
    - **Rendered** is only "a video exists". It stops a rebuild for one run and
      starts nothing, so a repo can sit there having cost a script, a voiceover
      and a render and never have gone out.
    - **Posted** is the last time a Reel about it actually published.

    A repo that is rendered and not covered is the row worth looking at: a
    finished video nothing has committed to, which is either waiting to be
    watched or was forgotten.
    """
    now = now or datetime.now(UTC)
    merged: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return merged.setdefault(
            name,
            {
                "repo": name,
                "covered_at": None,
                "rendered_at": None,
                "post": None,
                "run_folder": "",
                "score": None,
                "breakdown": {},
            },
        )

    for row in covered:
        if name := row["repo_full_name"]:
            # Earliest wins, matching the Mac's own merge. Taking the later one
            # would extend the cooldown by however long the two records
            # disagree, which is the wrong direction to be wrong in.
            entry = slot(name)
            when = (row["covered_at"] or "")[:10]
            if when and (not entry["covered_at"] or when < entry["covered_at"]):
                entry["covered_at"] = when
    for row in rendered:
        if name := row["repo_full_name"]:
            entry = slot(name)
            entry["rendered_at"] = (row["rendered_at"] or "")[:10]
            entry["run_folder"] = row["run_folder"] or ""
            entry["score"] = row["score"] or 0.0
            # Written by the pipeline and stored as it arrived. A row from
            # before the score travelled has an empty string here, which is
            # different from a repo that scored zero.
            with contextlib.suppress(ValueError, TypeError):
                entry["breakdown"] = json.loads(row["score_breakdown"] or "{}")
    for row in published:
        name, when = row["repo_full_name"], row["published_at"]
        if not name or not when:
            continue
        entry = slot(name)
        prior = entry["post"]
        if not prior or when > prior["published_at"]:
            entry["post"] = {**dict(row), **(readings.get(row["media_id"]) or {})}

    out = []
    for entry in merged.values():
        post = entry["post"] or {}
        published_on = (post.get("published_at") or "")[:10]
        left = None
        if entry["covered_at"]:
            age = (now.date() - datetime.fromisoformat(entry["covered_at"]).date()).days
            left = REPO_COOLDOWN_DAYS - age
        out.append(
            {
                **entry,
                "published_at": published_on,
                "hook": post.get("hook") or "",
                "views": post.get("views"),
                "skip_rate": post.get("skip_rate"),
                "permalink": post.get("permalink"),
                "days_left": left,
                # A video that exists and was never committed to. Nothing blocks
                # the repo and nothing on this machine will bring it up again.
                "stranded": bool(entry["rendered_at"] and not entry["covered_at"]),
                "latest": max(
                    entry["covered_at"] or "", entry["rendered_at"] or "", published_on
                ),
            }
        )
    # Most recently touched first, and the last publish breaks a tie. A day of
    # commitments all share a covered date, so without the second key the order
    # inside that day is whatever the first query happened to return.
    out.sort(key=lambda r: (r["latest"], r["published_at"], r["repo"]), reverse=True)
    return out
