"""Grouping the published Reels by something, and comparing the groups.

Every number in `IDEAS.md` was worked out by hand in a session and pasted in,
which `CLAUDE.md` already names as the failure it is: a fact about the numbers
goes stale silently and the only symptom is worse decisions. This is the part
that recomputes, so the tests are about the two ways a grouping lies.
"""

from __future__ import annotations

import pytest
import typer

import main
from config import Settings


@pytest.fixture
def cfg() -> Settings:
    return Settings(gateway_url="https://gate.example", gateway_token="t", _env_file=None)


def reading(
    *, published_at: str, skip: float = 70.0, views: int = 100, recipe: str = "",
    readings: int = 5,
):
    return {
        "repo_full_name": f"a/{published_at}",
        "published_at": published_at,
        "skip_rate": skip,
        "views": views,
        "recipe": recipe,
        "readings": readings,
    }


@pytest.fixture
def results(monkeypatch):
    def use(rows):
        monkeypatch.setattr(main.gateway, "fetch_results", lambda _cfg: rows)

    return use


def rendered(capsys) -> str:
    return capsys.readouterr().out


def test_a_slot_is_the_hour_not_the_minute(cfg, results, capsys):
    """The scheduler jitters every slot by an offset derived from its id and the
    date, so no two posts share a publish time. Grouping on the exact stamp gave
    43 cohorts of one out of 58 posts, which is a table that cannot be read and
    a comparison that cannot be made."""
    results([
        reading(published_at="2026-08-01T05:58:00+00:00"),
        reading(published_at="2026-08-02T06:26:00+00:00"),
        reading(published_at="2026-08-03T17:07:00+00:00"),
    ])

    main._show_cohorts(cfg, "slot")
    out = rendered(capsys)

    assert "05:00 UTC" in out
    assert "06:00 UTC" in out
    assert "17:00 UTC" in out


def test_slots_read_in_time_order_and_recipes_by_size(cfg, results, capsys):
    """A slot table is about the shape of the day, so time order is the only
    order that shows it. Recipes have no order, so the biggest cohort leads."""
    results([
        reading(published_at="2026-08-01T17:00:00+00:00"),
        reading(published_at="2026-08-02T06:00:00+00:00"),
        reading(published_at="2026-08-03T06:10:00+00:00"),
    ])

    main._show_cohorts(cfg, "slot")
    out = rendered(capsys)

    assert out.index("06:00 UTC") < out.index("17:00 UTC")


def test_a_post_with_no_recipe_is_named_rather_than_dropped(cfg, results, capsys):
    """Empty means "before recipes" and is comparable to nothing. Dropping those
    rows would hide the entire history the account has so far, and hiding is
    what a missing column already did once."""
    results([reading(published_at="2026-08-01T06:00:00+00:00")])

    main._show_cohorts(cfg, "recipe")

    assert "before recipes" in rendered(capsys)


def test_two_recipes_are_two_cohorts(cfg, results, capsys):
    """The whole point. Rows are comparable when their recipes match and not
    when they do not, and publish dates cannot stand in for that: the queue runs
    days deep, so a change lands in the middle of a queue full of older videos.
    """
    results([
        reading(published_at="2026-08-01T06:00:00+00:00", recipe="old1234.aaaaaaaa"),
        reading(published_at="2026-08-02T06:00:00+00:00", recipe="new5678.bbbbbbbb"),
    ])

    main._show_cohorts(cfg, "recipe")
    out = rendered(capsys)

    assert "old1234.aaaaaaaa" in out
    assert "new5678.bbbbbbbb" in out


def test_a_post_with_no_reading_is_not_counted_as_a_perfect_one(cfg, results, capsys):
    """A zero skip rate reads as an opening nobody scrolled past, and the newest
    post always has one. Same call `/api/results` and the Posts page make."""
    results([
        reading(published_at="2026-08-01T06:00:00+00:00", skip=64.0),
        reading(published_at="2026-08-02T06:00:00+00:00", skip=0.0),
    ])

    main._show_cohorts(cfg, "slot")

    assert "(1 posts with readings)" in rendered(capsys).replace("\n", "")


def test_an_unreachable_gateway_says_so_rather_than_printing_an_empty_table(
    cfg, results, capsys
):
    """The same shrug every other reader of the gateway makes. An empty table
    reads as "nothing worked", which is a different claim from "nobody asked"."""
    results([])

    main._show_cohorts(cfg, "slot")

    assert "No results yet" in rendered(capsys)


def test_a_post_still_arriving_is_held_back_and_said_so(cfg, results, capsys):
    """The panel holds these back too, and the two disagreeing about the same
    question would be worse than either being wrong. A Reel reaches about 99
    percent of its final views by its third reading."""
    results([
        reading(published_at="2026-08-01T06:00:00+00:00", views=900),
        reading(published_at="2026-08-19T06:00:00+00:00", views=40, readings=1),
    ])

    main._show_cohorts(cfg, "slot")
    out = rendered(capsys)

    assert "(1 posts with readings)" in out.replace("\n", "")
    assert "1 post(s) held back" in out.replace("\n", " ")


def test_a_gateway_too_old_to_count_readings_keeps_every_post(cfg, results, capsys):
    """No history is not evidence that a post is unsettled, and silently
    emptying the table would read as an account with no posts."""
    row = reading(published_at="2026-08-01T06:00:00+00:00")
    del row["readings"]
    results([row])

    main._show_cohorts(cfg, "slot")

    assert "held back" not in rendered(capsys)


def test_an_unknown_dimension_is_refused_by_name(cfg, results, capsys):
    """Rather than silently grouping everything into one row, which looks like a
    finished comparison."""
    results([reading(published_at="2026-08-01T06:00:00+00:00")])

    with pytest.raises(typer.Exit):
        main._show_cohorts(cfg, "vibes")

    assert "vibes" in rendered(capsys)
