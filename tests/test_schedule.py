"""Slot arithmetic: when a slot is due, and where the jitter puts it.

Every case here runs against a clock passed in, so none of it waits for a
Tuesday or patches `datetime.now`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway import schedule


def slot(**overrides) -> schedule.Slot:
    base = {
        "id": 1,
        "account_id": "acct",
        "hour": 18,
        "minute": 0,
        "tz": "UTC",
        "jitter_minutes": 0,
        "days": frozenset(),
    }
    return schedule.Slot(**{**base, **overrides})


def at(y: int, m: int, d: int, hh: int, mm: int = 0, tz: str = "UTC") -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz))


# --- Jitter ---------------------------------------------------------------


def test_jitter_is_stable_for_a_given_day():
    """The property the whole design rests on.

    A rolled offset would differ between these two calls, and a slot judged
    not-yet-due before a restart could come due before its earlier time after
    one, publishing twice or skipping a day.
    """
    first = schedule.jitter_offset(7, date(2026, 8, 1), 20)
    second = schedule.jitter_offset(7, date(2026, 8, 1), 20)
    assert first == second


def test_jitter_differs_by_day_and_by_slot():
    offsets = {schedule.jitter_offset(1, date(2026, 8, d), 20) for d in range(1, 15)}
    # Not all identical: the point is that the account does not post at the
    # same second every evening.
    assert len(offsets) > 1
    assert schedule.jitter_offset(1, date(2026, 8, 1), 20) != schedule.jitter_offset(
        2, date(2026, 8, 1), 20
    )


def test_jitter_stays_inside_the_window():
    for day in range(1, 60):
        offset = schedule.jitter_offset(3, date(2026, 8, 1) + timedelta(days=day), 20)
        assert timedelta(minutes=-20) <= offset <= timedelta(minutes=20)


def test_no_jitter_means_exactly_the_slot_time():
    assert schedule.jitter_offset(1, date(2026, 8, 1), 0) == timedelta()
    assert schedule.fire_time(slot(), date(2026, 8, 1)) == at(2026, 8, 1, 18)


# --- Due ------------------------------------------------------------------


def test_due_at_the_slot_time():
    assert schedule.due(slot(), at(2026, 8, 1, 18)) == date(2026, 8, 1)


def test_not_due_before():
    assert schedule.due(slot(), at(2026, 8, 1, 17, 59)) is None


def test_due_inside_the_grace_window_but_not_after():
    assert schedule.due(slot(), at(2026, 8, 1, 19), grace_minutes=90) == date(2026, 8, 1)
    # A pod that was down all night must not publish at breakfast.
    assert schedule.due(slot(), at(2026, 8, 2, 8), grace_minutes=90) is None


def test_day_restriction():
    # 2026-08-01 is a Saturday, so a weekdays-only slot owes nothing.
    weekdays = slot(days=frozenset({1, 2, 3, 4, 5}))
    assert schedule.due(weekdays, at(2026, 8, 1, 18)) is None
    assert schedule.due(weekdays, at(2026, 8, 3, 18)) == date(2026, 8, 3)


def test_late_evening_slot_is_still_yesterdays_business_after_midnight():
    """A slot at 23:50 checked at 00:05 owes the previous local date.

    Only looking at today would drop it, and the drop would be invisible.
    """
    late = slot(hour=23, minute=50)
    assert schedule.due(late, at(2026, 8, 2, 0, 5), grace_minutes=90) == date(2026, 8, 1)


# --- Timezones ------------------------------------------------------------


def test_local_wall_clock_is_honoured_across_dst():
    """18:00 Oslo is 16:00 UTC in summer and 17:00 UTC in winter.

    Storing an offset instead of a zone would move every post by an hour twice
    a year, in the direction nobody notices for months.
    """
    oslo = slot(tz="Europe/Oslo")
    assert schedule.fire_time(oslo, date(2026, 7, 1)) == datetime(2026, 7, 1, 16, tzinfo=UTC)
    assert schedule.fire_time(oslo, date(2026, 12, 1)) == datetime(2026, 12, 1, 17, tzinfo=UTC)


def test_jitter_is_applied_in_utc_not_wall_clock():
    """Adding a timedelta to a zoned datetime is wall-clock arithmetic in
    Python, which skips or repeats an hour on a DST boundary. The offset has to
    land on the instant."""
    # 2026-10-25 is the European autumn transition.
    late = slot(tz="Europe/Oslo", hour=2, minute=30, jitter_minutes=45)
    fired = schedule.fire_time(late, date(2026, 10, 25))
    base = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Oslo")).astimezone(UTC)
    assert abs(fired - base) <= timedelta(minutes=45)


def test_a_bad_timezone_falls_back_rather_than_raising():
    """One typo must not take the scheduler down for every account."""
    assert schedule.zone_or_utc("Mars/Olympus") == ZoneInfo("UTC")
    assert schedule.due(slot(tz="Mars/Olympus"), at(2026, 8, 1, 18)) == date(2026, 8, 1)


# --- Projection -----------------------------------------------------------


def test_next_fire_skips_to_a_day_the_slot_runs():
    weekend = slot(days=frozenset({6, 7}))
    # Wednesday, so the next firing is Saturday.
    assert schedule.next_fire(weekend, at(2026, 8, 5, 12)).date() == date(2026, 8, 8)


def test_projected_times_are_ordered_and_bounded():
    times = schedule.projected_times([slot()], at(2026, 8, 1, 12), 5)
    assert len(times) == 5
    assert times == sorted(times)
    assert times[0] == at(2026, 8, 1, 18)


def test_projected_times_interleave_two_slots():
    morning = slot(id=1, hour=9)
    evening = slot(id=2, hour=18)
    times = schedule.projected_times([morning, evening], at(2026, 8, 1, 0), 4)
    assert [t.hour for t in times] == [9, 18, 9, 18]


# --- Not looking like a cron entry ----------------------------------------


def test_the_fire_time_never_lands_on_a_round_minute():
    """The whole point of the jitter. A column of 18:00:00 rows is the
    cheapest automation tell there is, and :15, :30 and :45 are barely
    better."""
    jittered = slot(jitter_minutes=15)
    for day in range(1, 200):
        fired = schedule.fire_time(jittered, date(2026, 1, 1) + timedelta(days=day))
        local = fired.astimezone(ZoneInfo("UTC"))
        assert local.minute not in {0, 15, 30, 45}, local


def test_the_fire_time_is_not_on_the_minute_either():
    """Seconds resolution, because posting at exactly HH:MM:00 is still a
    grid, just a coarser one."""
    jittered = slot(jitter_minutes=15)
    seconds = {
        schedule.fire_time(jittered, date(2026, 1, 1) + timedelta(days=d)).second
        for d in range(60)
    }
    assert len(seconds) > 5
    assert seconds != {0}


def test_jitter_still_respects_the_window_with_the_round_rule_on():
    for day in range(1, 120):
        offset = schedule.jitter_offset(3, date(2026, 1, 1) + timedelta(days=day), 15,
                                        base_minute=0)
        assert timedelta(minutes=-15) <= offset <= timedelta(minutes=15)


def test_the_round_minute_rule_does_not_break_stability():
    """Still the same answer twice, which is what a restart depends on."""
    first = schedule.jitter_offset(9, date(2026, 8, 1), 15, base_minute=0)
    second = schedule.jitter_offset(9, date(2026, 8, 1), 15, base_minute=0)
    assert first == second


# --- Declared schedules ---------------------------------------------------


def test_parse_slots_reads_a_configmap_block():
    specs = schedule.parse_slots(
        """
        # the weekday evening slot
        18:00 Europe/Oslo jitter=15
        08:30 Europe/Oslo jitter=20 days=6,7
        12:00
        """,
        default_tz="UTC",
        default_jitter=15,
    )
    assert len(specs) == 3
    assert (specs[0].hour, specs[0].minute, specs[0].tz) == (18, 0, "Europe/Oslo")
    assert specs[0].jitter_minutes == 15
    assert specs[1].days == "6,7"
    # Only the time is required; the rest falls back to the defaults.
    assert (specs[2].tz, specs[2].jitter_minutes, specs[2].days) == ("UTC", 15, "")


def test_parse_slots_accepts_semicolons_for_a_one_line_env_var():
    specs = schedule.parse_slots("18:00 UTC; 09:00 UTC")
    assert [s.hour for s in specs] == [18, 9]


def test_parse_slots_is_empty_for_an_empty_value():
    assert schedule.parse_slots("") == []
    assert schedule.parse_slots("  \n # just a comment\n") == []


@pytest.mark.parametrize(
    "bad",
    [
        "1800 UTC",              # no colon
        "25:00 UTC",             # out of range
        "18:00 Mars/Olympus",    # not a zone
        "18:00 UTC jitter=soon", # not a number
        "18:00 UTC days=banana", # names no weekday
        "18:00 UTC wat=1",       # unknown setting
    ],
)
def test_a_bad_slot_line_raises_rather_than_being_skipped(bad):
    """A schedule that silently drops the line with the typo is a schedule
    that quietly stops posting."""
    with pytest.raises(schedule.SlotSpecError):
        schedule.parse_slots(bad)


def test_day_names_lists_every_day_the_slot_runs():
    """The slots page used to recover this by splitting `describe()` on a
    comma, which showed a Saturday and Sunday slot as Sunday only."""
    assert slot(days=frozenset({6, 7})).day_names == "Sat, Sun"
    assert slot().day_names == "every day"


def test_parse_and_format_days_round_trip():
    assert schedule.parse_days("1,3,5") == frozenset({1, 3, 5})
    assert schedule.parse_days("") == frozenset()
    # Junk is treated as "every day" rather than as an error nobody sees.
    assert schedule.parse_days("banana") == frozenset()
    assert schedule.format_days({5, 1, 3}) == "1,3,5"
