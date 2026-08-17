"""The feedback loop: what the last videos scored, reaching the next prompt.

Until this existed the pipeline generated, published and measured, and the
measurement reached a web page and stopped. Nothing that decided what to say
had ever seen what happened last time.

The join is the fragile part. The gateway knows the media id, the repo and the
numbers and has never seen a hook; the run folder holds the hook and has never
heard of a media id. `repo_full_name` is all they share.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import candidate

from config import Settings
from pipeline import results
from pipeline.results import PastPost
from pipeline.scriptwriter import _build_prompt, _results_block


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        github_token="x",
        gateway_url="https://gate.example.test",
        gateway_token="test-token",
        _env_file=None,
    )


# `Settings.build_dir` is a property rooted at the repo, so it cannot be
# overridden through the constructor. The seam is the argument, and using it is
# the difference between these tests and a suite that writes into the real
# build/ directory of whoever runs it.
@pytest.fixture
def build(tmp_path):
    return tmp_path / "build"


def run_folder(build, day: str, slug: str, *, repo: str, hook: str) -> None:
    folder = build / day / slug
    folder.mkdir(parents=True)
    (folder / "repo.json").write_text(json.dumps({"full_name": repo}))
    (folder / "script.json").write_text(json.dumps({"hook": hook}))


def gateway_returning(payload, status: int = 200) -> httpx.Client:
    """Unopened on purpose. `fetch_results` opens what it is handed, and a
    client opened twice raises."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def result(repo: str, skip: float, *, watch_ms: int = 8000, views: int = 100) -> dict:
    return {
        "media_id": f"m-{repo}",
        "repo_full_name": repo,
        "published_at": "2026-08-01T09:00:00+00:00",
        "views": views,
        "reach": views - 10,
        "skip_rate": skip,
        "avg_watch_ms": watch_ms,
    }


# --- The join ---------------------------------------------------------------


def test_a_result_is_matched_to_the_hook_that_ran_on_it(cfg, build, monkeypatch):
    run_folder(build, "2026-07-31", "justvugg-colibri", repo="JustVugg/colibri",
               hook="744 billion parameters on a 25 gigabyte machine")
    monkeypatch.setattr(
        results.gateway, "fetch_results", lambda _cfg: [result("JustVugg/colibri", 64.2)]
    )

    got = results.past_posts(cfg, build_dir=build)

    assert len(got) == 1
    assert got[0].hook == "744 billion parameters on a 25 gigabyte machine"
    assert got[0].skip_rate == pytest.approx(64.2)
    assert got[0].avg_watch_s == pytest.approx(8.0)


def test_the_best_opening_comes_first(cfg, build, monkeypatch):
    """Sorted by what worked, because "what worked" is the question being asked."""
    for slug, repo, hook in (
        ("a-one", "a/one", "worst"),
        ("b-two", "b/two", "best"),
        ("c-three", "c/three", "middle"),
    ):
        run_folder(build, "2026-08-01", slug, repo=repo, hook=hook)
    monkeypatch.setattr(
        results.gateway,
        "fetch_results",
        lambda _cfg: [result("a/one", 80.4), result("b/two", 64.2), result("c/three", 75.8)],
    )

    got = results.past_posts(cfg, build_dir=build)

    assert [p.hook for p in got] == ["best", "middle", "worst"]


def test_a_rerendered_repo_reports_the_hook_that_shipped(cfg, build, monkeypatch):
    """The later run replaced the earlier one, so its hook is the live one."""
    run_folder(build, "2026-07-31", "a-one", repo="a/one", hook="the old hook")
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="the hook that shipped")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build)[0].hook == "the hook that shipped"


def test_a_set_aside_draft_never_beats_the_run_that_shipped(cfg, build, monkeypatch):
    """The real shape of `dietrichgebert-ponytail`, which has three folders.

    Sorting by name puts `.v2` last, which is how the first version of this
    reported a rejected draft as the hook that scored 79.5 percent. Moving a
    run aside is the documented way to force a regeneration, so this is the
    normal case rather than a corner.
    """
    run_folder(build, "2026-07-31", "a-one", repo="a/one", hook="the hook that shipped")
    run_folder(build, "2026-07-31", "a-one.prev", repo="a/one", hook="an early draft")
    run_folder(build, "2026-07-31", "a-one.v2", repo="a/one", hook="a rejected draft")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 79.5)])

    assert results.past_posts(cfg, build_dir=build)[0].hook == "the hook that shipped"


def test_a_set_aside_folder_keeps_its_old_receipt_and_still_loses(cfg, build, monkeypatch):
    """`xai-org-grok-build.v1-posted` holds a receipt for a deleted media."""
    run_folder(build, "2026-07-31", "a-one", repo="a/one", hook="the hook that shipped")
    (build / "2026-07-31" / "a-one" / "published.json").write_text("{}")
    run_folder(build, "2026-07-31", "a-one.v1-posted", repo="a/one", hook="an older post")
    (build / "2026-07-31" / "a-one.v1-posted" / "published.json").write_text("{}")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 75.8)])

    assert results.past_posts(cfg, build_dir=build)[0].hook == "the hook that shipped"


def test_a_receipt_breaks_a_tie_between_two_plain_folders(cfg, build, monkeypatch):
    run_folder(build, "2026-07-31", "a-one", repo="a/one", hook="published")
    (build / "2026-07-31" / "a-one" / "queued.json").write_text("{}")
    run_folder(build, "2026-07-31", "b-one", repo="a/one", hook="never went out")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build)[0].hook == "published"


def test_a_post_whose_run_folder_is_gone_is_dropped(cfg, build, monkeypatch):
    """Moving a run folder aside is the documented way to force a re-render."""
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build) == []


def test_a_run_that_never_produced_a_script_is_dropped(cfg, build, monkeypatch):
    folder = build / "2026-08-01" / "a-one"
    folder.mkdir(parents=True)
    (folder / "repo.json").write_text(json.dumps({"full_name": "a/one"}))
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build) == []


def test_the_list_is_capped(cfg, build, monkeypatch):
    rows = []
    for i in range(results.MAX_RESULTS + 5):
        run_folder(build, "2026-08-01", f"r-{i}", repo=f"r/{i}", hook=f"hook {i}")
        rows.append(result(f"r/{i}", 60.0 + i))
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: rows)

    assert len(results.past_posts(cfg, build_dir=build)) == results.MAX_RESULTS


# --- What produced a run ----------------------------------------------------
#
# Three posts a day go out while the prompt is being edited daily, so without a
# recipe "did the hook change work" is unanswerable after the fact.


def test_the_recipe_changes_when_a_setting_that_changes_the_output_changes(cfg):
    louder = cfg.model_copy(update={"max_script_words": cfg.max_script_words + 10})

    assert results.recipe(cfg) != results.recipe(louder)


def test_the_recipe_is_stable_across_calls(cfg):
    assert results.recipe(cfg) == results.recipe(cfg)


def test_a_setting_that_cannot_change_the_script_does_not_change_the_recipe(cfg):
    """Otherwise every unrelated edit invalidates the comparison."""
    elsewhere = cfg.model_copy(update={"repo_cooldown_days": 99})

    assert results.recipe(cfg) == results.recipe(elsewhere)


def test_the_recipe_is_written_next_to_what_it_produced(cfg, tmp_path):
    written = results.write_recipe(tmp_path, cfg)

    stored = json.loads((tmp_path / "recipe.json").read_text())
    assert stored["recipe"] == written
    assert stored["max_script_words"] == cfg.max_script_words


def test_a_post_carries_the_recipe_that_wrote_it(cfg, build, monkeypatch):
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="a hook")
    (build / "2026-08-01" / "a-one" / "recipe.json").write_text(
        json.dumps({"recipe": "abc1234.deadbeef"})
    )
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build)[0].recipe == "abc1234.deadbeef"


def test_a_run_made_before_recipes_existed_says_nothing_rather_than_guessing(
    cfg, build, monkeypatch
):
    """An invented version string would make two incomparable runs look
    comparable, which is the one thing this exists to prevent."""
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="a hook")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 70.0)])

    assert results.past_posts(cfg, build_dir=build)[0].recipe == ""


# --- Nothing here may break a run -------------------------------------------


def test_a_gateway_that_is_down_costs_hindsight_and_nothing_else(cfg):
    from pipeline import gateway as pipeline_gateway

    def dead(request):
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(dead))
    assert pipeline_gateway.fetch_results(cfg, client=client) == []


def test_a_500_from_the_gateway_is_not_fatal(cfg):
    from pipeline import gateway as pipeline_gateway

    client = gateway_returning({"detail": "boom"}, status=500)
    assert pipeline_gateway.fetch_results(cfg, client=client) == []


def test_no_gateway_configured_means_no_results():
    from pipeline import gateway as pipeline_gateway

    off = Settings(github_token="x", _env_file=None)
    assert pipeline_gateway.fetch_results(off) == []


def test_results_are_read_from_the_results_route(cfg):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"results": [result("a/one", 70.0)]})

    from pipeline import gateway as pipeline_gateway

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(pipeline_gateway.fetch_results(cfg, client=client)) == 1
    assert seen["url"].endswith("/api/results")
    assert seen["auth"], "the route is behind the bearer token"


# --- What the prompt says ---------------------------------------------------


def past(*pairs) -> list[PastPost]:
    return [
        PastPost(repo_full_name=f"r/{i}", hook=hook, skip_rate=skip,
                 avg_watch_s=5.0, views=100)
        for i, (hook, skip) in enumerate(pairs)
    ]


def test_no_results_adds_nothing_to_the_prompt(cfg):
    assert _results_block([]) == ""
    assert "own videos did" not in _build_prompt(candidate("a/one"), cfg, [])


def test_the_block_carries_the_hooks_and_their_numbers(cfg):
    block = _results_block(past(("a concrete number", 64.2), ("a formula", 80.4)))

    assert "a concrete number" in block
    assert "64.2%" in block
    assert "80.4%" in block


def test_a_list_that_is_all_bad_is_labelled_as_all_bad():
    """Otherwise the model copies the top of a sorted list of failures. When
    this was written every published hook skipped 64 to 80 percent against a 30
    to 40 percent average, so the best line was still a bad one."""
    block = _results_block(past(("the least bad one", 64.2)))

    assert "30 to 40 percent" in block
    assert "underperforming" in block


def test_a_list_with_a_real_winner_in_it_is_not(cfg):
    """By 53 posts the best hook had reached 45.8 percent, which is near the
    benchmark rather than far below it. Hardcoded, the "all underperforming"
    line had become an instruction to ignore the account's own best work."""
    block = _results_block(past(("the one that worked", 45.8), ("a formula", 80.4)))

    assert "underperforming" not in block
    assert "evidence of what works here" in block


def test_the_verdict_switches_on_the_best_score_not_the_worst():
    """A single strong hook is enough to make the list worth learning from,
    however bad the rest of it is."""
    all_bad = _results_block(past(("a", 60.1), ("b", 89.0)))
    one_good = _results_block(past(("a", 49.9), ("b", 89.0)))

    assert "underperforming" in all_bad
    assert "underperforming" not in one_good


def test_the_block_asks_for_a_shape_that_is_not_in_it():
    """Five of the first seven hooks were the same shape, which is what a rule
    read as a template produces. Naming a replacement template buys the same
    thing in a different costume."""
    block = _results_block(past(("Your coding agent does a bad thing", 80.4)))

    assert "Do not write a variation on any shape above" in block


def test_the_block_reaches_the_prompt(cfg):
    prompt = _build_prompt(candidate("a/one"), cfg, past(("a real hook", 64.2)))

    assert "a real hook" in prompt
    assert "64.2%" in prompt
