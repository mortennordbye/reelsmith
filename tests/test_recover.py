"""The recovery sweep.

`--recover` exists because the batch runs inside a container that can die
mid-render, and a session that dies cannot pick itself up. Everything worth
testing here is what it refuses to touch: the build tree also holds rejected
renders, and a sweep that queues one of those publishes a video a person
already said no to.

No rendering and no gateway: `_render_one` and `_enqueue_run` are stubbed, so
what is asserted is the decision to call them.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import candidate, script

import main
from config import Settings


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "build_dir", property(lambda self: tmp_path))
    return Settings(gateway_url="https://gate.example", gateway_token="t")


@pytest.fixture
def calls(monkeypatch) -> dict[str, list[str]]:
    """Record what the sweep decided to do, and fake the effects."""
    seen: dict[str, list[str]] = {"rendered": [], "enqueued": [], "approved": []}

    def fake_render(cfg, repo, run_dir, *, stop_after=None):
        seen["rendered"].append(run_dir.name)
        (run_dir / "out.mp4").write_bytes(b"video")
        return script("a clip")

    def fake_enqueue(cfg, run_dir, *, approved):
        seen["enqueued"].append(run_dir.name)
        if approved:
            seen["approved"].append(run_dir.name)
        (run_dir / "queued.json").write_text(json.dumps({"id": 1}))

    monkeypatch.setattr(main, "_render_one", fake_render)
    monkeypatch.setattr(main, "_enqueue_run", fake_enqueue)
    monkeypatch.setattr(main.scraper, "covered_repos", lambda cfg: [])
    return seen


def run_dir(cfg, name: str, *, day: date | None = None, **files) -> Path:
    """A build folder holding exactly the artifacts named."""
    stamp = (day or date.today()).isoformat()
    d = cfg.build_dir / stamp / name
    d.mkdir(parents=True)
    owner, _, repo = name.partition("-")
    (d / "repo.json").write_text(candidate(f"{owner}/{repo}").model_dump_json())
    for filename, body in files.items():
        (d / filename.replace("_", ".")).write_text(body)
    return d


# --- What it finishes ------------------------------------------------------


def test_a_run_with_a_script_and_no_video_is_rendered_then_queued(cfg, calls):
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["rendered"] == ["acme-tool"]
    assert calls["enqueued"] == ["acme-tool"]


def test_a_finished_video_is_queued_without_rendering_again(cfg, calls):
    d = run_dir(cfg, "acme-tool", script_json="{}")
    (d / "out.mp4").write_bytes(b"video")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["rendered"] == []
    assert calls["enqueued"] == ["acme-tool"]


def test_approve_is_passed_through(cfg, calls):
    """The nightly arms what it recovers, for the same reason it arms a batch."""
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["approved"] == ["acme-tool"]


def test_yesterday_is_in_the_window(cfg, calls):
    """A batch that died at 02:33 is recovered by a job that runs the next day."""
    run_dir(cfg, "acme-tool", day=date.today() - timedelta(days=1), script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == ["acme-tool"]


# --- What it refuses to touch ---------------------------------------------


def test_a_committed_run_is_left_alone(cfg, calls):
    """`queued.json` is the same duplicate guard --enqueue reads."""
    run_dir(cfg, "acme-tool", script_json="{}", queued_json='{"id": 7}')

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == []


def test_a_published_run_is_left_alone(cfg, calls):
    run_dir(cfg, "acme-tool", script_json="{}", published_json='{"id": "7"}')

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == []


def test_a_folder_moved_aside_by_hand_is_left_alone(cfg, calls):
    """A dot means a person rejected this render.

    `RepoCandidate.slug` turns every dot into a hyphen, so `.prev` and `.v2`
    can only have come from someone moving the folder to force a regeneration.
    Queueing one publishes the take that was already said no to.
    """
    run_dir(cfg, "acme-tool.prev", script_json="{}")
    run_dir(cfg, "acme-tool.v2", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["rendered"] == []
    assert calls["enqueued"] == []


def test_a_repo_already_on_cooldown_is_skipped(cfg, calls, monkeypatch):
    """The receipt is per folder; a sibling that shipped is invisible to it."""
    monkeypatch.setattr(
        main.scraper, "covered_repos", lambda cfg: [("acme/tool", "2026-08-14")]
    )
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == []


def test_two_folders_for_one_repo_only_queue_once(cfg, calls):
    """Recovering the first covers the repo, so the second stops being eligible."""
    run_dir(cfg, "acme-tool", script_json="{}")
    run_dir(cfg, "acme-tool", day=date.today() - timedelta(days=1), script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == ["acme-tool"]


def test_a_run_with_no_script_is_reported_not_written(cfg, calls):
    """Paying Claude for a script is discovery's decision, never recovery's."""
    run_dir(cfg, "acme-tool")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["rendered"] == []
    assert calls["enqueued"] == []


def test_an_old_run_is_out_of_the_window(cfg, calls):
    """A two week old "trending" repo is worse to post than to drop."""
    run_dir(cfg, "acme-tool", day=date.today() - timedelta(days=14), script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == []


def test_one_bad_run_does_not_end_the_sweep(cfg, calls, monkeypatch):
    def explode(cfg, repo, run_dir, *, stop_after=None):
        if run_dir.name == "acme-bad":
            raise RuntimeError("no voice")
        (run_dir / "out.mp4").write_bytes(b"video")
        return script("a clip")

    monkeypatch.setattr(main, "_render_one", explode)
    run_dir(cfg, "acme-bad", script_json="{}")
    run_dir(cfg, "acme-good", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == ["acme-good"]


# --- The ceiling -----------------------------------------------------------


def test_the_ceiling_clamps_how_many_are_recovered(cfg, calls, monkeypatch):
    monkeypatch.setattr(main.gateway, "fetch_pending_count", lambda cfg: 9)
    run_dir(cfg, "acme-one", script_json="{}")
    run_dir(cfg, "acme-two", script_json="{}")

    main._recover(cfg, approve=True, max_queue=10)

    assert calls["enqueued"] == ["acme-one"]


def test_a_full_queue_recovers_nothing(cfg, calls, monkeypatch):
    monkeypatch.setattr(main.gateway, "fetch_pending_count", lambda cfg: 10)
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=10)

    assert calls["rendered"] == []
    assert calls["enqueued"] == []


def test_an_unreachable_gateway_is_refusal_not_zero(cfg, calls, monkeypatch):
    """Same trade as --max-queue on a batch: guessing costs a video either way."""
    monkeypatch.setattr(main.gateway, "fetch_pending_count", lambda cfg: None)
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=10)

    assert calls["rendered"] == []
    assert calls["enqueued"] == []


def test_no_ceiling_means_no_gateway_call(cfg, calls, monkeypatch):
    """Called by hand, --recover should not need the queue to be readable."""
    def fail(cfg):
        raise AssertionError("asked the gateway without --max-queue")

    monkeypatch.setattr(main.gateway, "fetch_pending_count", fail)
    run_dir(cfg, "acme-tool", script_json="{}")

    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == ["acme-tool"]


def test_an_empty_build_tree_is_not_an_error(cfg, calls):
    main._recover(cfg, approve=True, max_queue=None)

    assert calls["enqueued"] == []
