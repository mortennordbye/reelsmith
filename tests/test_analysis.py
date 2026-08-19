"""The arithmetic behind the Insights page.

Pure functions on plain rows, tested apart from the route, because the numbers
are the part that can be quietly wrong. A page that renders is not a page that
is right, and a cohort table is exactly the kind of thing nobody re-derives by
hand once it looks plausible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gateway import analysis

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def post(*, at: datetime, skip: float = 70.0, views: int = 100, recipe: str = "", hook: str = ""):
    return {
        "published_at": at.isoformat(),
        "skip_rate": skip,
        "views": views,
        "recipe": recipe,
        "hook": hook,
        "repo_full_name": "a/b",
    }


# --- Grouping ---------------------------------------------------------------


def test_a_slot_is_the_hour_not_the_minute():
    """The scheduler jitters every slot by an offset derived from its id and the
    date, precisely so a restart cannot fire one twice. The side effect is that
    no two posts share a publish time, and grouping on the stamp gave 43 cohorts
    of one out of 58 posts."""
    early = post(at=NOW.replace(hour=5, minute=58))
    late = post(at=NOW.replace(hour=5, minute=59))

    assert analysis.slot_of(early) == analysis.slot_of(late) == "05:00 UTC"


def test_a_post_with_no_recipe_is_named_rather_than_grouped_with_the_rest():
    """Empty is a claim, that nothing recorded what made this video, and it is
    comparable to nothing. Folding it in with a real recipe would put posts from
    an unknown checkout inside a cohort measuring a known one."""
    assert analysis.recipe_of({"recipe": ""}) == "before recipes"
    assert analysis.recipe_of({"recipe": "abc1234.deadbeef"}) == "abc1234.deadbeef"


def test_a_post_with_no_reading_is_not_counted_as_a_perfect_one():
    """A zero skip rate reads as an opening nobody scrolled past, and the newest
    post always has one."""
    rows = [post(at=NOW, skip=64.0), post(at=NOW, skip=0.0)]

    (cohort,) = analysis.cohorts(rows, key=analysis.slot_of, now=NOW)["groups"]

    assert cohort["n"] == 1


def test_a_cohort_reports_breakouts_separately_from_its_median():
    """Eight of the first 58 posts carried a third of all views, so the median
    describes the post that failed. Two cohorts can share a median and differ
    fourfold in what they were actually worth, which is only visible as a
    count."""
    rows = [post(at=NOW, views=100) for _ in range(4)] + [post(at=NOW, views=1400)]

    (cohort,) = analysis.cohorts(rows, key=analysis.slot_of, now=NOW)["groups"]

    assert cohort["views"] == 100
    assert cohort["best"] == 1400
    assert cohort["breakouts"] == 1
    assert cohort["breakout_share"] == pytest.approx(20.0)


def test_a_cohort_carries_its_age():
    """A young cohort has had less time to gather views, which flatters its skip
    rate and punishes its views. Without the number on the page a reader has no
    way to know two rows are not comparable."""
    rows = [post(at=NOW - timedelta(days=d)) for d in (8, 10, 12)]

    (cohort,) = analysis.cohorts(rows, key=analysis.slot_of, now=NOW)["groups"]

    assert cohort["age_days"] == 10


def test_the_threshold_count_uses_the_accounts_own_number():
    """60 percent is where this account's median views jumped several fold. It
    is not a benchmark from elsewhere and not a round number chosen for looking
    tidy, so it is asserted rather than left to drift."""
    rows = [post(at=NOW, skip=s) for s in (55.0, 59.9, 60.0, 72.0)]

    (cohort,) = analysis.cohorts(rows, key=analysis.slot_of, now=NOW)["groups"]

    assert analysis.SKIP_THRESHOLD == 60.0
    assert cohort["under_threshold"] == 2


# --- The trend line ---------------------------------------------------------


def test_the_trend_is_the_median_of_the_last_n():
    got = analysis.trend([10, 20, 30, 40, 50], window=3)

    assert got == [None, None, 20, 30, 40]


def test_the_trend_stays_empty_until_the_window_is_full():
    """A median of two posts drawn on the same line as a median of seven reads
    as the same kind of evidence, and it is not."""
    assert analysis.trend([10, 20], window=7) == [None, None]


def test_the_trend_resists_one_breakout():
    """A median rather than a mean, because this account's distribution is a
    long flat run with occasional outliers, and a mean turns one of those into a
    trend that was never there."""
    assert analysis.trend([70, 70, 70, 70, 20], window=5) == [None, None, None, None, 70]


# --- Chart geometry ---------------------------------------------------------


def test_the_chart_needs_two_points_to_be_a_chart():
    assert analysis.skip_chart([post(at=NOW)]) is None
    assert analysis.skip_chart([]) is None


def test_a_worse_skip_rate_sits_higher_on_the_plot():
    """SVG y grows downward, so this is the one place the arithmetic can be
    upside down and still look like a chart.

    The axis is not inverted. Skip rate reads like every other percentage, with
    100 at the top, which means better is *down* and the good band sits along
    the bottom. Inverting so that better is up reads correctly for one second
    and wrongly for every second after, because the number would then not mean
    what the axis says it means.
    """
    rows = [post(at=NOW - timedelta(days=1), skip=40.0), post(at=NOW, skip=80.0)]

    chart = analysis.skip_chart(rows)

    good, bad = chart["dots"]
    assert bad["y"] < good["y"], "80% skip is worse, and sits higher up"
    # And the shaded band runs from the threshold down to the baseline, so the
    # good region is the one under the line rather than over it.
    assert chart["threshold_y"] < chart["baseline_y"]


def test_the_axis_runs_the_full_range_rather_than_the_data():
    """Skip rate is a percentage where both ends mean something. Cropping to the
    range the data happens to occupy would make a two point drift look like a
    collapse."""
    chart = analysis.skip_chart(
        [post(at=NOW - timedelta(days=1), skip=70.0), post(at=NOW, skip=72.0)]
    )

    assert [g["label"] for g in chart["grid"]] == ["0%", "20%", "40%", "60%", "80%", "100%"]


def test_the_end_labels_are_anchored_inward():
    """Centred, half of the first and last date sits outside the viewBox and is
    clipped by the container. Caught by looking at it, not by a test that only
    checked the numbers."""
    chart = analysis.skip_chart([post(at=NOW - timedelta(days=1)), post(at=NOW)])

    assert [t["anchor"] for t in chart["x_ticks"]] == ["start", "middle", "end"]


def test_a_dot_carries_what_its_tooltip_needs():
    """Including the hook, which is the whole reason a dot on this chart is
    worth hovering: the point of the page is to connect an opening to what it
    scored."""
    rows = [
        post(at=NOW - timedelta(days=1), skip=55.0, hook="It reads 40 pages in one pass"),
        post(at=NOW, skip=80.0),
    ]

    dot = analysis.skip_chart(rows)["dots"][0]

    assert dot["hook"] == "It reads 40 pages in one pass"
    assert dot["good"] is True, "under the threshold, so it gets the ring"


def test_dots_are_ordered_by_time_whatever_order_they_arrive_in():
    """`published_media` returns newest first, and a trend line drawn through
    that is the same data read backwards."""
    rows = [post(at=NOW, skip=80.0), post(at=NOW - timedelta(days=2), skip=40.0)]

    dots = analysis.skip_chart(rows)["dots"]

    assert [d["skip"] for d in dots] == [40.0, 80.0]


# --- The repo list ----------------------------------------------------------


def covered_row(name: str, at: str):
    return {"repo_full_name": name, "covered_at": f"{at}T02:00:00+00:00"}


def rendered_row(name: str, at: str, *, score: float = 0.0, breakdown: str = ""):
    return {
        "repo_full_name": name,
        "rendered_at": f"{at}T02:00:00+00:00",
        "run_folder": f"{at}/{name.replace('/', '-')}",
        "score": score,
        "score_breakdown": breakdown,
    }


def published_row(name: str, at: str, *, media_id: str = "m1", hook: str = ""):
    return {
        "repo_full_name": name,
        "published_at": f"{at}T06:00:00+00:00",
        "media_id": media_id,
        "hook": hook,
        "permalink": "https://ig/x",
    }


def history(**kw):
    kw.setdefault("covered", [])
    kw.setdefault("rendered", [])
    kw.setdefault("published", [])
    kw.setdefault("readings", {})
    return analysis.repo_history(now=NOW, **kw)


def test_the_three_records_stay_distinguishable():
    """Covered is a commitment and blocks discovery for a month; rendered only
    means a video exists and blocks nothing. Flattening them into one date would
    turn "I built this and have not watched it" into a 30 day block, which is
    the opposite of rendering being free to throw away."""
    (row,) = history(
        covered=[covered_row("a/b", "2026-08-10")],
        rendered=[rendered_row("a/b", "2026-08-09")],
        published=[published_row("a/b", "2026-08-11")],
    )

    assert row["covered_at"] == "2026-08-10"
    assert row["rendered_at"] == "2026-08-09"
    assert row["published_at"] == "2026-08-11"


def test_a_render_nobody_committed_to_is_flagged():
    """The row the page exists to surface: a finished video that cost a script,
    a voiceover and a render, which nothing will bring up again."""
    (row,) = history(rendered=[rendered_row("a/b", "2026-08-18")])

    assert row["stranded"] is True
    assert row["days_left"] is None, "nothing is blocking it"


def test_a_covered_repo_is_never_stranded():
    """It has a commitment, so the render is accounted for."""
    (row,) = history(
        covered=[covered_row("a/b", "2026-08-18")],
        rendered=[rendered_row("a/b", "2026-08-18")],
    )

    assert row["stranded"] is False


def test_the_cooldown_counts_down_and_then_frees_the_repo():
    """30 days from the commitment, mirroring `REPO_COOLDOWN_DAYS` on the Mac.
    Discovery reads its own copy of that number, so this one only decides a word
    on a page, but a page that said "free again" a week early would be read as
    permission."""
    rows = {
        r["repo"]: r
        for r in history(
            covered=[
                covered_row("a/old", "2026-07-01"),
                covered_row("a/recent", "2026-08-18"),
            ]
        )
    }

    assert rows["a/recent"]["days_left"] == 29
    assert rows["a/old"]["days_left"] < 0, "past the window, so free again"


def test_the_earlier_commitment_wins_on_a_conflict():
    """Matching the Mac's own merge. Taking the later date would extend the
    cooldown by however long the two records disagree."""
    (row,) = history(
        covered=[covered_row("a/b", "2026-08-14"), covered_row("a/b", "2026-08-10")]
    )

    assert row["covered_at"] == "2026-08-10"


def test_the_last_publish_wins_when_a_repo_has_two():
    """The cooldown stops two inside its window, but `--unmark` and a re-post
    can put one either side of it, and the question is when it last went out."""
    (row,) = history(
        published=[
            published_row("a/b", "2026-07-20", media_id="old"),
            published_row("a/b", "2026-08-12", media_id="new"),
        ],
        readings={"new": {"views": 900, "skip_rate": 55.0}},
    )

    assert row["published_at"] == "2026-08-12"
    assert row["views"] == 900


def test_most_recently_touched_leads():
    rows = history(
        covered=[covered_row("a/old", "2026-08-01"), covered_row("a/new", "2026-08-18")]
    )

    assert [r["repo"] for r in rows] == ["a/new", "a/old"]


# --- When a reading can be trusted ------------------------------------------


def reading(media: str, day: int, views: int):
    return {"media_id": media, "fetched_on": f"2026-08-{day:02d}", "views": views}


def series(media: str, views: list[int], *, start: int = 1):
    return {media: [reading(media, start + i, v) for i, v in enumerate(views)]}


def test_the_maturity_curve_is_measured_not_asserted():
    """Every comparison here was hedged with "a post from this morning is not
    comparable with one from last week", written from intuition. The table knew
    the answer all along and nothing had ever read it: a Reel arrives at about
    71 percent of its final views on the first reading and 99 by the third.
    """
    # Nine readings, so it clears the window `maturity` needs, climbing to 100
    # and then flat, which is the real shape.
    got = analysis.maturity(series("m", [50, 90, 99, 100, 100, 100, 100, 100, 100]))

    assert [p["reading"] for p in got["curve"]] == [0, 1, 2, 3]
    assert [round(p["share"]) for p in got["curve"]] == [50, 90, 99, 100]
    assert got["n"] == 1


def test_a_post_read_enough_times_is_settled():
    """Counted in readings rather than days, because the sweep is what produces
    a reading and a sweep that did not run leaves a post younger than the
    calendar says it is."""
    got = analysis.maturity(
        {**series("old", [10, 20, 30]), **series("new", [10, 20])}
    )

    assert "old" in got["settled"]
    assert "new" not in got["settled"], "two readings is not enough"


def test_a_post_still_arriving_is_held_back_rather_than_averaged_in():
    """A Reel has 71 percent of its final views at its first reading, so a
    cohort holding yesterday's post is not reporting a worse slot, it is
    reporting a post that has not finished arriving."""
    rows = [
        {"media_id": "settled", "published_at": "2026-08-10T06:00:00+00:00",
         "skip_rate": 60.0, "views": 900},
        {"media_id": "fresh", "published_at": "2026-08-19T06:00:00+00:00",
         "skip_rate": 60.0, "views": 40},
    ]

    got = analysis.cohorts(rows, key=analysis.slot_of, settled={"settled"}, now=NOW)

    assert got["held_back"] == 1
    assert [c["n"] for c in got["groups"]] == [1]
    assert got["groups"][0]["views"] == 900, "the unsettled post did not drag it down"


def test_no_reading_history_at_all_keeps_every_post():
    """A caller with no history is not evidence that a post is unsettled, and
    silently emptying the table would read as an account with no posts."""
    rows = [
        {"media_id": "a", "published_at": "2026-08-10T06:00:00+00:00",
         "skip_rate": 60.0, "views": 100},
    ]

    got = analysis.cohorts(rows, key=analysis.slot_of, now=NOW)

    assert got["held_back"] == 0
    assert got["groups"][0]["n"] == 1


def test_a_post_measured_too_late_does_not_flatten_the_curve():
    """A post first read days after it published shows no climb, because the
    climb already happened. Counting it would report the curve as flatter than
    it is and settle posts sooner than the evidence allows."""
    late = series("late", [100] * 9)
    early = series("early", [50, 90, 99, 100, 100, 100, 100, 100, 100])

    both = analysis.maturity({**late, **early})

    # Both are tracked, so the median at the first reading sits between the two
    # rather than being dragged to 100 by the late one alone.
    assert both["n"] == 2
    assert 50 <= both["curve"][0]["share"] <= 100


def test_a_post_with_no_views_cannot_divide_the_curve():
    """A Reel that never got a view has no final total to be a share of, and it
    is the newest post that most often looks like that."""
    got = analysis.maturity(series("dead", [0] * 9))

    assert got["curve"] == []


def test_why_a_repo_was_picked_travels_with_the_render():
    """The only answer available to "why does discovery keep landing on the same
    corner of GitHub". `score_candidates` splits the score four ways and writes
    it into `repo.json`, where it never left the machine that ranked."""
    (row,) = history(
        rendered=[
            rendered_row(
                "a/b", "2026-08-18", score=0.81,
                breakdown='{"velocity": 0.44, "stars": 0.12, "readme": 0.09}',
            )
        ]
    )

    assert row["score"] == 0.81
    assert row["breakdown"]["velocity"] == 0.44
    assert row["run_folder"] == "2026-08-18/a-b"


def test_a_render_from_before_the_score_travelled_has_no_breakdown():
    """Empty rather than zero. A repo that scored nothing and a repo nobody
    recorded a score for are different claims, and only one of them is about
    the repo."""
    (row,) = history(rendered=[rendered_row("a/b", "2026-08-18")])

    assert row["breakdown"] == {}
    assert not row["score"]


def test_a_corrupt_breakdown_costs_the_chips_and_not_the_page():
    """It arrives over the network and is stored as it came. A row nobody can
    parse must not take out the list that decides what gets made tonight."""
    (row,) = history(rendered=[rendered_row("a/b", "2026-08-18", breakdown="{oh no")])

    assert row["breakdown"] == {}
