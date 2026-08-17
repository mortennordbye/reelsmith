"""Candidate selection: relevance, README quality, scoring, cooldown."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from conftest import candidate

from pipeline import scraper
from pipeline.scraper import (
    _DEFAULT_ENRICH_TOP,
    ThemeSaturation,
    UsedRepos,
    _cold_start_velocity,
    _is_relevant,
    _readme_quality,
    find_trending_repos,
    score_candidates,
)

TODAY = date(2026, 7, 30)


# --------------------------------------------------------------------------
# _is_relevant
# --------------------------------------------------------------------------


def item(**kwargs) -> dict:
    return {"name": "thing", "full_name": "owner/thing", "description": "", **kwargs}


def test_declared_topic_is_enough():
    assert _is_relevant(item(topics=["rust", "cli"]))


def test_blocklisted_topic_wins_over_a_relevant_one():
    assert not _is_relevant(item(topics=["ai", "awesome-list"]))


def test_blocklisted_name_is_rejected_even_with_good_topics():
    assert not _is_relevant(
        item(full_name="sindresorhus/awesome-llm", name="awesome-llm", topics=["llm"])
    )


def test_topicless_repos_fall_back_to_keywords():
    assert _is_relevant(item(description="A fast local inference server"))


def test_keywords_are_word_boundary_matched():
    # The reason this matters: substring matching makes "airflow" an AI repo,
    # and then half the feed is data pipelines.
    assert not _is_relevant(item(name="airflow", description="scheduler for batch jobs"))


def test_hyphenated_keywords_survive_tokenization():
    assert _is_relevant(item(description="A self-hosted dashboard"))


def test_an_unrelated_repo_is_rejected():
    assert not _is_relevant(item(name="recipes", description="My grandmother's cooking"))


# --------------------------------------------------------------------------
# _readme_quality
# --------------------------------------------------------------------------

PROSE = " ".join(["word"] * 200)


def test_missing_readme_scores_zero():
    assert _readme_quality("") == 0.0


def test_a_complete_readme_scores_full_marks():
    readme = (
        f"# Tool\n\n{PROSE}\n\n"
        "## Install\n\n```bash\npip install tool\n```\n\n"
        "```python\nimport tool\n```\n"
    )
    assert _readme_quality(readme) == 1.0


def test_one_code_block_scores_less_than_several():
    one = f"{PROSE}\n\n```bash\nx\n```\n"
    several = f"{PROSE}\n\n```bash\nx\n```\n\n```python\ny\n```\n"
    assert _readme_quality(one) < _readme_quality(several)


def test_badge_walls_do_not_count_as_prose():
    # Without badge stripping these 300 lines read as 300 words and earn the
    # length bonus, which is exactly backwards -- this README says nothing.
    badges = "\n".join(["[![Build](https://img.shields.io/b)](https://ci)"] * 300)
    assert _readme_quality(badges) == 0.0


def test_an_install_section_is_worth_something():
    plain = PROSE
    with_install = f"{PROSE}\n\n## Getting Started\n\nRun it.\n"
    assert _readme_quality(with_install) > _readme_quality(plain)


def test_quality_never_exceeds_one():
    readme = (
        f"# Tool\n\n## Install\n\n{PROSE}\n"
        + "\n".join(f"```bash\ncmd{i}\n```" for i in range(10))
    )
    assert _readme_quality(readme) == 1.0


# --------------------------------------------------------------------------
# _cold_start_velocity
# --------------------------------------------------------------------------


def test_young_repos_are_not_damped():
    assert _cold_start_velocity(stars=900, age_days=30) == 30.0


def test_old_repos_are_damped_below_a_real_breakout():
    # The failure this prevents: a 5-year-old 50k-star repo scoring ~27
    # stars/day and outranking a project that gained 800 stars yesterday.
    established = _cold_start_velocity(stars=50_000, age_days=1825)
    breakout = _cold_start_velocity(stars=2_000, age_days=60)
    assert established < breakout


# --------------------------------------------------------------------------
# score_candidates
# --------------------------------------------------------------------------


def used_repos(tmp_path, **marked) -> UsedRepos:
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    for full_name, on in marked.items():
        store.mark_used(full_name.replace("__", "/"), on)
    return store


def test_empty_pool_scores_nothing(tmp_path):
    assert score_candidates([], used_repos(tmp_path)) == []


def test_velocity_dominates_raw_star_count(tmp_path):
    fast = candidate("new/rocket", stars=1_000, velocity=800.0)
    big = candidate("old/giant", stars=200_000, velocity=40.0)

    ranked = score_candidates([big, fast], used_repos(tmp_path), TODAY)

    assert [c.full_name for c in ranked] == ["new/rocket", "old/giant"]


def test_cooldown_zeroes_a_candidate_however_good_it_is(tmp_path):
    fast = candidate("new/rocket", stars=1_000, velocity=800.0)
    other = candidate("old/giant", stars=200_000, velocity=40.0)
    store = used_repos(tmp_path, new__rocket=TODAY)

    ranked = score_candidates([fast, other], store, TODAY)

    assert ranked[0].full_name == "old/giant"
    assert ranked[-1].full_name == "new/rocket"
    assert ranked[-1].score == 0.0


def test_the_breakdown_explains_the_score(tmp_path):
    cand = candidate("a/b", stars=10_000, velocity=100.0, hn_points=600)

    [scored] = score_candidates([cand], used_repos(tmp_path), TODAY)

    multipliers = {"cooldown_multiplier", "theme_multiplier"}
    parts = {"velocity", "stars", "hackernews", "readme"}
    assert set(scored.score_breakdown) == parts | multipliers
    components = {k: v for k, v in scored.score_breakdown.items() if k not in multipliers}
    assert scored.score == sum(components.values())
    # Hacker News saturates at 300 points; 600 must not count double.
    assert scored.score_breakdown["hackernews"] == 0.15


def test_a_single_candidate_is_normalised_against_itself(tmp_path):
    [scored] = score_candidates([candidate("a/b", velocity=7.0)], used_repos(tmp_path), TODAY)
    assert scored.score_breakdown["velocity"] == 0.55


def test_a_pool_with_no_velocity_at_all_does_not_divide_by_zero(tmp_path):
    cands = [candidate("a/b", velocity=0.0), candidate("c/d", velocity=0.0)]
    ranked = score_candidates(cands, used_repos(tmp_path), TODAY)
    assert all(c.score_breakdown["velocity"] == 0.0 for c in ranked)


# --------------------------------------------------------------------------
# UsedRepos
# --------------------------------------------------------------------------


def test_an_unseen_repo_is_free_to_use(tmp_path):
    assert UsedRepos(tmp_path / "used.json").penalty("a/b", TODAY) == 1.0


def test_a_repo_used_today_is_blocked(tmp_path):
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("a/b", TODAY)
    assert store.penalty("a/b", TODAY) == 0.0


def test_the_cooldown_expires_on_the_boundary_day(tmp_path):
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("a/b", TODAY - timedelta(days=30))
    assert store.penalty("a/b", TODAY) == 1.0

    store.mark_used("c/d", TODAY - timedelta(days=29))
    assert store.penalty("c/d", TODAY) == 0.0


def test_marks_survive_a_reload(tmp_path):
    path = tmp_path / "used.json"
    UsedRepos(path, cooldown_days=30).mark_used("a/b", TODAY)
    assert UsedRepos(path, cooldown_days=30).penalty("a/b", TODAY) == 0.0


def test_clearing_a_mark_frees_the_repo(tmp_path):
    path = tmp_path / "used.json"
    store = UsedRepos(path, cooldown_days=30)
    store.mark_used("a/b", TODAY)

    assert store.clear("a/b") == TODAY.isoformat()
    assert store.penalty("a/b", TODAY) == 1.0
    assert UsedRepos(path).used_on("a/b") is None
    # Clearing something that was never marked is a no-op, not an error.
    assert store.clear("a/b") is None


def test_a_corrupt_store_degrades_instead_of_crashing(tmp_path):
    path = tmp_path / "used.json"
    path.write_text("{ this is not json")
    assert UsedRepos(path).penalty("a/b", TODAY) == 1.0


# --------------------------------------------------------------------------
# merge -- the gateway's copy folded back in
# --------------------------------------------------------------------------


def test_merge_recovers_a_repo_this_machine_never_knew(tmp_path):
    path = tmp_path / "used.json"
    store = UsedRepos(path, cooldown_days=30)

    assert store.merge({"a/b": TODAY.isoformat()}) == ["a/b"]
    assert store.penalty("a/b", TODAY) == 0.0
    # Written through, so the next run does not need the gateway to agree.
    assert UsedRepos(path).used_on("a/b") == TODAY.isoformat()


def test_merge_never_drops_a_local_only_repo(tmp_path):
    """`--posted` marks repos the gateway is never told about. A merge that
    replaced would hand them straight back to discovery."""
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("hand/posted", TODAY)

    store.merge({"a/b": TODAY.isoformat()})
    assert store.used_on("hand/posted") == TODAY.isoformat()


def test_the_earlier_date_wins_so_a_cooldown_is_never_extended(tmp_path):
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    earlier = (TODAY - timedelta(days=10)).isoformat()
    store.mark_used("a/b", TODAY)

    assert store.merge({"a/b": earlier}) == ["a/b"]
    assert store.used_on("a/b") == earlier


def test_a_later_remote_date_is_ignored(tmp_path):
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    earlier = TODAY - timedelta(days=10)
    store.mark_used("a/b", earlier)

    assert store.merge({"a/b": TODAY.isoformat()}) == []
    assert store.used_on("a/b") == earlier.isoformat()


def test_merging_nothing_does_not_rewrite_the_file(tmp_path):
    """A gateway that agrees is the normal case on the machine that enqueued."""
    path = tmp_path / "used.json"
    store = UsedRepos(path, cooldown_days=30)
    store.mark_used("a/b", TODAY)
    before = path.stat().st_mtime_ns

    assert store.merge({"a/b": TODAY.isoformat()}) == []
    assert path.stat().st_mtime_ns == before


def test_an_empty_remote_is_the_documented_failure(tmp_path):
    """fetch_covered returns {} for every failure, so this is the down path."""
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("a/b", TODAY)

    assert store.merge({}) == []
    assert store.penalty("a/b", TODAY) == 0.0


# --------------------------------------------------------------------------
# is_covered -- the discovery-time filter, which is what stops the re-scrape
# --------------------------------------------------------------------------


def test_a_covered_repo_is_recognised_before_it_is_scored(tmp_path):
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("a/b", TODAY)

    assert store.is_covered("a/b", TODAY) is True
    assert store.is_covered("c/d", TODAY) is False


def test_is_covered_and_the_penalty_cannot_disagree(tmp_path):
    """Both express one rule. Two answers to "may we use this" is a bug waiting."""
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("fresh/repo", TODAY)
    store.mark_used("edge/repo", TODAY - timedelta(days=30))
    store.mark_used("inside/repo", TODAY - timedelta(days=29))

    for name in ("fresh/repo", "edge/repo", "inside/repo", "never/seen"):
        blocked = store.is_covered(name, TODAY)
        assert store.penalty(name, TODAY) == (0.0 if blocked else 1.0), name


def test_covered_lists_everything_ever_made_newest_first(tmp_path):
    """The record outlives the cooldown: "have we done this" is not "may we do it"."""
    store = UsedRepos(tmp_path / "used.json", cooldown_days=30)
    store.mark_used("old/one", TODAY - timedelta(days=200))
    store.mark_used("new/one", TODAY)

    assert [name for name, _ in store.covered()] == ["new/one", "old/one"]
    # The old one is off cooldown and still on the list.
    assert store.is_covered("old/one", TODAY) is False


# --------------------------------------------------------------------------
# find_trending_repos -- enrichment has to cover the whole batch
# --------------------------------------------------------------------------


def _spy_collect(monkeypatch, pool):
    """Capture the enrich_top find_trending_repos asks for."""
    seen = {}

    def fake(cfg, *, enrich_top=_DEFAULT_ENRICH_TOP, on=None):
        seen["enrich_top"] = enrich_top
        return pool

    monkeypatch.setattr(scraper, "collect_candidates", fake)
    return seen


def test_a_batch_larger_than_the_default_enriches_the_whole_batch(monkeypatch):
    """The README shortlist is picked by velocity and the winners by score, so
    an unenriched repo can place inside the top N and be scripted from its one
    line description. Certain at --batch 15 against a constant 12."""
    pool = [candidate(full_name=f"o/r{i}", score=1.0) for i in range(20)]
    seen = _spy_collect(monkeypatch, pool)

    find_trending_repos(cfg=object(), count=15)
    assert seen["enrich_top"] == 15


def test_a_small_batch_does_not_lower_the_ceiling(monkeypatch):
    """Enrichment costs a request each, so one video still shortlists 12 and
    picks the best of them rather than enriching exactly one."""
    pool = [candidate(full_name=f"o/r{i}", score=1.0) for i in range(20)]
    seen = _spy_collect(monkeypatch, pool)

    find_trending_repos(cfg=object(), count=1)
    assert seen["enrich_top"] == _DEFAULT_ENRICH_TOP


# --------------------------------------------------------------------------
# Theme spreading
# --------------------------------------------------------------------------


def test_a_batch_does_not_repeat_a_theme():
    """Three videos from one batch reach one audience inside one day, and the
    account's first 52 repos carried `ai-agents` eleven times."""
    ranked = [
        candidate("o/agent-a", score=0.9, topics=["ai-agents", "llm"]),
        candidate("o/agent-b", score=0.8, topics=["ai-agents", "claude-code"]),
        candidate("o/db", score=0.7, topics=["database", "sql"]),
    ]
    picked = scraper._spread_themes(ranked, 2)

    assert [c.full_name for c in picked] == ["o/agent-a", "o/db"]


def test_a_language_in_common_is_not_a_theme_in_common():
    """Two Rust CLIs are not the same video. Only what a repo is *about*
    collides, so build-and-packaging tags never block a pick."""
    ranked = [
        candidate("o/one", score=0.9, language="Rust", topics=["rust", "cli", "search"]),
        candidate("o/two", score=0.8, language="Rust", topics=["rust", "cli", "terminal"]),
    ]
    picked = scraper._spread_themes(ranked, 2)

    assert len(picked) == 2


def test_a_single_theme_pool_still_fills_the_batch():
    """A night where everything trending is agents is a real Tuesday. A batch
    of one is worse than a batch of three that overlap, so it backfills on
    score rather than returning short."""
    ranked = [
        candidate(f"o/agent-{i}", score=1.0 - i / 10, topics=["ai-agents"]) for i in range(4)
    ]
    picked = scraper._spread_themes(ranked, 3)

    assert [c.full_name for c in picked] == ["o/agent-0", "o/agent-1", "o/agent-2"]


def test_an_untagged_repo_never_blocks_another():
    """No topics means no theme to collide on, not a theme that matches
    everything."""
    ranked = [
        candidate("o/one", score=0.9, topics=[]),
        candidate("o/two", score=0.8, topics=[]),
    ]
    picked = scraper._spread_themes(ranked, 2)

    assert len(picked) == 2


# --------------------------------------------------------------------------
# Theme saturation across days
# --------------------------------------------------------------------------


def _built(tmp_path, day: str, name: str, topics: list[str]):
    run = tmp_path / day / name
    run.mkdir(parents=True)
    (run / "repo.json").write_text(
        json.dumps({"full_name": f"o/{name}", "name": name, "owner": "o",
                    "url": "u", "stars": 1, "topics": topics})
    )


def test_a_theme_the_last_few_days_were_all_about_is_penalised(tmp_path):
    """Three a day on a velocity ranking means the same subject every day, and
    within-batch spreading cannot see yesterday."""
    for i, day in enumerate(["2026-08-14", "2026-08-15", "2026-08-16"]):
        _built(tmp_path, day, f"agent{i}", ["ai-agents"])
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))

    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == pytest.approx(0.3)
    assert sat.penalty(candidate("o/other", topics=["database"])) == 1.0


def test_saturation_only_looks_at_the_recent_window(tmp_path):
    """A theme covered a fortnight ago is not saturation, it is history. The
    30 day repo cooldown is what stops the repo itself repeating."""
    _built(tmp_path, "2026-07-20", "old", ["ai-agents"])
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))

    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == 1.0


def test_a_partly_saturated_theme_is_only_partly_penalised(tmp_path):
    """It has to lose to a comparable candidate on a fresh theme, not to every
    candidate."""
    _built(tmp_path, "2026-08-16", "a", ["ai-agents"])
    _built(tmp_path, "2026-08-16", "b", ["database"])
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))

    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == pytest.approx(0.65)


def test_an_unreadable_run_folder_does_not_fail_a_run(tmp_path):
    """A half written folder is a normal thing to find in build/."""
    (tmp_path / "2026-08-16" / "broken").mkdir(parents=True)
    (tmp_path / "2026-08-16" / "broken" / "repo.json").write_text("{not json")
    _built(tmp_path, "2026-08-16", "ok", ["ai-agents"])
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))

    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == pytest.approx(0.3)


def test_an_empty_build_dir_scores_exactly_as_before(tmp_path):
    """A fresh checkout has no history to be saturated by."""
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))
    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == 1.0


def test_saturation_is_optional_in_scoring(tmp_path):
    """Absent means no penalty, so a caller with no build directory to read
    scores exactly as it did before."""
    cands = [candidate("o/a", velocity=5.0, topics=["ai-agents"])]
    [scored] = score_candidates(cands, used_repos(tmp_path), TODAY)

    assert scored.score_breakdown["theme_multiplier"] == 1.0


def test_a_run_folder_from_an_older_version_has_no_theme(tmp_path):
    """`repo.json` is read back with `model_construct`, which skips validation.
    A folder written before topics were recorded must read as "no theme" rather
    than raising in the middle of discovery."""
    run = tmp_path / "2026-08-16" / "old"
    run.mkdir(parents=True)
    run.joinpath("repo.json").write_text(
        json.dumps({"full_name": "o/old", "name": "old", "owner": "o", "url": "u", "stars": 1})
    )
    sat = ThemeSaturation.from_build_dir(tmp_path, date(2026, 8, 17))

    assert sat.penalty(candidate("o/new", topics=["ai-agents"])) == 1.0
