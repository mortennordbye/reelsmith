"""One nightly for every account.

What is worth pinning: each brand runs the pipeline its settings name, in the
order the old prompts ran it; one account failing never stops the next;
recovery runs whatever happened before it; a full queue pays for nothing; and
`--plan` reads and spends nothing.

Nothing renders and nothing reaches a gateway: the stages are stubbed, so what
is asserted is the decision to call them.
"""

from __future__ import annotations

import pytest
import typer

import main
from config import Settings

ACCOUNTS = {"reels": "reelbrand", "quotes": "quotebrand"}


def _cfg(brand: str) -> Settings:
    return Settings(
        github_token="x",
        brand=brand,
        gateway_url="https://gate.example.test",
        gateway_token="t",
        _env_file=None,
    )


@pytest.fixture
def world(monkeypatch):
    rows = {
        "reelbrand": {"settings": {"pipeline": "reel", "batch": 3, "max_queue": 6}, "version": 1},
        "quotebrand": {
            "settings": {"pipeline": "episode", "batch": 1, "max_queue": 3},
            "version": 1,
        },
    }
    calls: list[tuple] = []
    reports: list[tuple] = []

    def report(cfg, kind, **kw):
        reports.append((cfg.brand, kind, kw.get("outcome", "running")))
        return 5

    monkeypatch.setattr(main, "available_accounts", lambda: list(ACCOUNTS))
    monkeypatch.setattr(main, "select_account", lambda name: _cfg(ACCOUNTS[name]))
    monkeypatch.setattr(main.gateway, "fetch_brand_settings", lambda cfg: rows.get(cfg.brand))
    monkeypatch.setattr(main.gateway, "fetch_pending_count", lambda cfg: 1)
    monkeypatch.setattr(main.gateway, "report_run", report)
    monkeypatch.setattr(main, "_preflight", lambda **kw: None)
    monkeypatch.setattr(
        main.scraper, "snapshot_stars", lambda cfg: calls.append(("snapshot", cfg.brand)) or 10
    )
    monkeypatch.setattr(main.publisher, "refresh_token_if_due", lambda cfg: None)
    monkeypatch.setattr(
        main,
        "_run_batch",
        lambda cfg, count, **kw: calls.append(("batch", cfg.brand, count, kw["max_queue"])) or [],
    )
    monkeypatch.setattr(
        main, "_write_episode", lambda cfg, name, render: calls.append(("episode", cfg.brand))
    )
    monkeypatch.setattr(
        main,
        "_recover",
        lambda cfg, approve, max_queue: calls.append(("recover", cfg.brand, approve, max_queue))
        or [],
    )
    return rows, calls, reports


def test_each_brand_runs_the_pipeline_its_settings_name(world):
    _, calls, reports = world

    main._all_accounts(plan=False)

    assert calls == [
        ("snapshot", "reelbrand"),
        ("batch", "reelbrand", 3, 6),
        ("recover", "reelbrand", True, 6),
        ("episode", "quotebrand"),
        ("recover", "quotebrand", True, 3),
    ]
    assert reports == [
        ("reelbrand", "batch", "running"),
        ("reelbrand", "batch", "ok"),
        ("quotebrand", "episode", "running"),
        ("quotebrand", "episode", "ok"),
    ]


def test_plan_reads_and_spends_nothing(world, monkeypatch):
    def spend(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("--plan spent something")

    for name in ("_run_batch", "_write_episode", "_recover"):
        monkeypatch.setattr(main, name, spend)
    monkeypatch.setattr(main.scraper, "snapshot_stars", spend)
    monkeypatch.setattr(main.gateway, "report_run", spend)

    lines = [main._night_for(name, plan=True) for name in ACCOUNTS]

    assert all(line.startswith("plan:") for line in lines)
    assert "reel" in lines[0] and "1 waiting" in lines[0]
    assert "episode" in lines[1]


def test_one_account_failing_does_not_stop_the_next(world):
    rows, calls, _ = world
    rows.pop("reelbrand")

    with pytest.raises(typer.Exit):
        main._all_accounts(plan=False)

    assert ("episode", "quotebrand") in calls
    assert not any(call[0] == "batch" for call in calls)


def test_a_failed_batch_still_recovers_and_reports_the_failure(world, monkeypatch):
    _, calls, reports = world

    def explode(cfg, count, **kw):
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(main, "_run_batch", explode)

    line = main._night_for("reels", plan=False)

    assert line.startswith("failed") and "chromium missing" in line
    assert ("recover", "reelbrand", True, 6) in calls
    assert reports[-1] == ("reelbrand", "batch", "failed")


def test_a_full_queue_pays_for_no_episode(world, monkeypatch):
    _, calls, _ = world
    monkeypatch.setattr(main.gateway, "fetch_pending_count", lambda cfg: 3)

    main._night_for("quotes", plan=False)

    assert ("episode", "quotebrand") not in calls
    assert ("recover", "quotebrand", True, 3) in calls


def test_an_account_with_no_brand_is_skipped_not_failed(world, monkeypatch):
    _, calls, _ = world
    monkeypatch.setattr(main, "select_account", lambda name: _cfg(""))

    main._all_accounts(plan=False)

    assert calls == []
