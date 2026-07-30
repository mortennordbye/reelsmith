"""The star-velocity store.

This is the file the whole ranking rests on: velocity is 55% of the score, and
it is only *measured* when a snapshot exists from an earlier day.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sources.github import StarHistory

TODAY = date(2026, 7, 30)


def test_a_repo_we_have_never_seen_has_no_measured_velocity(tmp_path):
    history = StarHistory(tmp_path / "stars.json")
    assert history.gained_today("a/b", 5_000, TODAY) is None


def test_todays_snapshot_alone_is_not_a_measurement(tmp_path):
    # The cold-start trap: record() runs before gained_today() in the same
    # pass, so "we have an entry for this repo" must not mean "we have history".
    history = StarHistory(tmp_path / "stars.json")
    history.record("a/b", 5_000, TODAY)
    assert history.gained_today("a/b", 5_000, TODAY) is None


def test_yesterdays_snapshot_gives_a_real_delta(tmp_path):
    history = StarHistory(tmp_path / "stars.json")
    history.record("a/b", 5_000, TODAY - timedelta(days=1))
    assert history.gained_today("a/b", 5_150, TODAY) == 150


def test_a_multi_day_gap_is_normalised_to_per_day(tmp_path):
    # Without this a laptop that slept for three days would report a 3x spike
    # and hand the video to whatever repo happened to be biggest.
    history = StarHistory(tmp_path / "stars.json")
    history.record("a/b", 1_000, TODAY - timedelta(days=3))
    assert history.gained_today("a/b", 1_600, TODAY) == 200


def test_the_most_recent_earlier_snapshot_wins(tmp_path):
    history = StarHistory(tmp_path / "stars.json")
    history.record("a/b", 1_000, TODAY - timedelta(days=10))
    history.record("a/b", 1_500, TODAY - timedelta(days=1))
    assert history.gained_today("a/b", 1_550, TODAY) == 50


def test_a_repo_losing_stars_reports_a_negative_delta(tmp_path):
    history = StarHistory(tmp_path / "stars.json")
    history.record("a/b", 1_000, TODAY - timedelta(days=1))
    assert history.gained_today("a/b", 900, TODAY) == -100


def test_snapshots_survive_a_reload(tmp_path):
    path = tmp_path / "stars.json"
    first = StarHistory(path)
    first.record("a/b", 1_000, TODAY - timedelta(days=1))
    first.save()

    assert StarHistory(path).gained_today("a/b", 1_100, TODAY) == 100


def test_saving_prunes_snapshots_past_the_retention_window(tmp_path):
    path = tmp_path / "stars.json"
    history = StarHistory(path, keep_days=120)
    history.record("a/b", 100, TODAY - timedelta(days=200))
    history.record("a/b", 900, TODAY - timedelta(days=1))
    history.prune(TODAY)

    kept = history._data["a/b"]
    assert list(kept) == [(TODAY - timedelta(days=1)).isoformat()]


def test_a_repo_with_only_stale_snapshots_is_dropped_entirely(tmp_path):
    history = StarHistory(tmp_path / "stars.json", keep_days=120)
    history.record("gone/repo", 100, TODAY - timedelta(days=200))
    history.record("live/repo", 100, TODAY)
    history.prune(TODAY)

    assert set(history._data) == {"live/repo"}


def test_writes_are_atomic_and_leave_no_temp_file(tmp_path):
    path = tmp_path / "stars.json"
    history = StarHistory(path)
    history.record("a/b", 1_000, TODAY)
    history.save()

    assert json.loads(path.read_text()) == {"a/b": {TODAY.isoformat(): 1_000}}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_corrupt_history_degrades_to_cold_start(tmp_path):
    path = tmp_path / "stars.json"
    path.write_text("{ truncated mid-writ")
    assert StarHistory(path).gained_today("a/b", 1_000, TODAY) is None
