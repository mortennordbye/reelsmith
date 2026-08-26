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
from pipeline import results, scriptwriter
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


def test_the_recipe_moves_when_the_hook_specification_changes(cfg, monkeypatch):
    """The test the defect would have failed.

    `recipe()` hashed `SYSTEM_PROMPT` and nothing else until 2026-08-26, so
    every rule in `_build_prompt` -- the research rule, the whole hook
    specification, the caption rules -- could be rewritten without moving the
    fingerprint. An uncommitted edit to the half of the prompt that gets
    edited most left two runs looking comparable, which is the one claim a
    recipe makes.
    """
    before = results.recipe(cfg)

    def _rewritten(repo, cfg, past=None):
        return "a completely different set of rules"

    monkeypatch.setattr(scriptwriter, "_build_prompt", _rewritten)

    assert results.recipe(cfg) != before


def test_the_recipe_moves_when_the_benchmark_changes(cfg, monkeypatch):
    """`_results_block` states the benchmark and derives its verdict from it,
    and CLAUDE.md calls that verdict load bearing: it decides whether the
    model is told to copy the top of the list or to disregard all of it."""
    before = results.recipe(cfg)

    def _rewritten(past):
        return "## Ignore everything this account has ever done"

    monkeypatch.setattr(scriptwriter, "_results_block", _rewritten)

    assert results.recipe(cfg) != before


def test_the_recipe_does_not_move_when_only_the_past_numbers_change(cfg):
    """The source is the rule and the numbers are the data.

    `_results_block` renders this account's own skip rates, which change every
    night without any rule changing. Hashing what it returns rather than what
    it is would put every run in a cohort of one and the recipe would compare
    nothing to nothing.
    """
    lean = scriptwriter._results_block([])
    fed = scriptwriter._results_block(
        [
            results.PastPost(
                repo_full_name="a/one", hook="a hook", skip_rate=40.0,
                avg_watch_s=3.0, views=900,
            )
        ]
    )

    assert lean != fed
    assert results.recipe(cfg) == results.recipe(cfg)


def test_the_prompt_digest_covers_what_the_model_is_actually_shown(cfg):
    """A guard on the seam rather than on the digest.

    The failure this file exists to prevent is prompt text living somewhere
    `recipe()` does not read, and a hash is opaque about which. Naming three
    sentences that reach the model, one from each source, fails loudly and
    says where when one of them stops being covered.
    """
    covered = scriptwriter.prompt_source()

    assert scriptwriter.SYSTEM_PROMPT in covered
    assert "would the hook still be true if" in covered
    assert "Educational videos in this format" in covered


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


def test_the_gateway_hook_beats_the_one_found_on_this_disk(cfg, build, monkeypatch):
    """The hook that was on the video wins over the one lying next to its name.

    The local lookup is by repo name against a build folder, and a repo usually
    has more than one. On the machine that did not render it that answers with
    a run which was never on a video: the Mac holds a `claw-code` script written
    eleven days before the pod built and queued that repo, and the loop was told
    its hook scored 63.6 percent.

    Worse than the same mistake on the recipe, which only misleads somebody
    reading a table. This one is fed into the prompt that writes tomorrow, so
    the loop argues from evidence that does not exist.
    """
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="never shipped")
    row = result("a/one", 64.2) | {"hook": "the one that actually ran"}
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [row])

    assert results.past_posts(cfg, build_dir=build)[0].hook == "the one that actually ran"


def test_a_post_with_no_run_folder_here_still_reaches_the_loop(cfg, build, monkeypatch):
    """Rendered on the pod, measured on the gateway, invisible on the laptop.

    Requiring a local folder to produce a hook dropped every Reel this machine
    did not render, which was thirteen of fifty eight. They were the most recent
    thirteen, so the loop was blind to exactly the posts worth learning from.
    """
    build.mkdir(parents=True)
    row = result("a/one", 64.2) | {"hook": "made somewhere else"}
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [row])

    got = results.past_posts(cfg, build_dir=build)

    assert [p.hook for p in got] == ["made somewhere else"]


def test_a_post_nobody_can_name_a_hook_for_is_left_out(cfg, build, monkeypatch):
    """A row with no hook on either side carries no evidence. Counting it would
    put an empty opening in the block as though it were one that scored."""
    build.mkdir(parents=True)
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 64.2)])

    assert results.past_posts(cfg, build_dir=build) == []


def test_saves_and_shares_come_through_when_the_gateway_sends_them(
    cfg, build, monkeypatch
):
    """Collected since the insights sweep existed and never sent, so every
    analysis here optimised the one metric that happened to be exposed."""
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="h")
    row = result("a/one", 64.2) | {"saved": 20, "shares": 9}
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [row])

    post = results.past_posts(cfg, build_dir=build)[0]

    assert (post.saved, post.shares) == (20, 9)


def test_an_older_gateway_leaves_saves_and_shares_unmeasured_not_zero(
    cfg, build, monkeypatch
):
    """Zero shares reads as a video nobody passed on. "Not measured" is a
    different claim and the only true one against a gateway that cannot send
    the field."""
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="h")
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 64.2)])

    post = results.past_posts(cfg, build_dir=build)[0]

    assert post.saved is None
    assert post.shares is None


def test_the_gateway_recipe_beats_the_one_found_on_this_disk(cfg, build, monkeypatch):
    """The recorded recipe wins over the guessed one, because it was recorded by
    the machine that rendered.

    The local join is by repo name against a build folder. On a machine that did
    not make this video that answers with a checkout which never wrote it, and
    it answers confidently: the Mac holds an old run for a repo the render host
    rebuilt and queued days later. Wrong is worse than missing here, since the
    whole point of the fingerprint is deciding which rows are comparable.
    """
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="h")
    (build / "2026-08-01" / "a-one" / "recipe.json").write_text(
        json.dumps({"recipe": "staleaa.11111111"})
    )
    row = result("a/one", 64.2) | {"recipe": "fresh99.22222222"}
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [row])

    assert results.past_posts(cfg, build_dir=build)[0].recipe == "fresh99.22222222"


def test_a_gateway_too_old_to_send_a_recipe_falls_back_to_this_disk(
    cfg, build, monkeypatch
):
    """An older gateway sends no `recipe` key at all, which must cost the local
    answer nothing. Same defensiveness as `PastPost` reading a missing metric as
    absent rather than as zero."""
    run_folder(build, "2026-08-01", "a-one", repo="a/one", hook="h")
    (build / "2026-08-01" / "a-one" / "recipe.json").write_text(
        json.dumps({"recipe": "local11.33333333"})
    )
    monkeypatch.setattr(results.gateway, "fetch_results", lambda _cfg: [result("a/one", 64.2)])

    assert results.past_posts(cfg, build_dir=build)[0].recipe == "local11.33333333"


def test_the_recipe_sent_at_enqueue_is_the_one_the_render_recorded(cfg, tmp_path):
    """Read from the folder, never recomputed.

    `--enqueue` can run days after the render and `--recover` sweeps two days of
    folders, so recomputing here would stamp the video with the checkout that
    queued it rather than the one that wrote it. That is precisely the confusion
    the fingerprint exists to remove, so it would be the worst possible place to
    reintroduce it.
    """
    (tmp_path / "recipe.json").write_text(json.dumps({"recipe": "older11.44444444"}))

    assert results.read_recipe(tmp_path) == "older11.44444444"
    assert results.read_recipe(tmp_path / "nothing-here") == ""


def test_an_unreadable_recipe_file_is_no_recipe_rather_than_a_crash(tmp_path):
    """A truncated write must cost the stamp and not the enqueue. The video is
    already rendered and uploaded by then, and refusing to queue it over a
    metadata file would trade a Reel for a label."""
    (tmp_path / "recipe.json").write_text("{not json")

    assert results.read_recipe(tmp_path) == ""


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
