"""Contracts that used to be able to drift.

The hook limit lived as one number in the prompt and a different one in the
validator; the staged-asset directory had no rule about what it was allowed to
delete. Both are cheap to pin down and expensive to notice by eye.
"""

from __future__ import annotations

import pytest
from conftest import candidate
from pydantic import ValidationError

from config import get_settings
from pipeline.models import MAX_HOOK_CHARS, VideoScript
from pipeline.renderer import prune_staged_assets, stage_asset
from pipeline.scriptwriter import _build_prompt


def make_script(hook: str) -> VideoScript:
    return VideoScript(hook=hook, spoken_script="Words.", visual_cues=[])


# --------------------------------------------------------------------------
# Hook length
# --------------------------------------------------------------------------


def test_the_validator_uses_the_configured_limit():
    assert get_settings().max_hook_chars == MAX_HOOK_CHARS


def test_a_hook_at_the_limit_is_accepted():
    assert make_script("x" * MAX_HOOK_CHARS).hook == "x" * MAX_HOOK_CHARS


def test_a_hook_over_the_limit_is_rejected():
    with pytest.raises(ValidationError, match=str(MAX_HOOK_CHARS)):
        make_script("x" * (MAX_HOOK_CHARS + 1))


def test_the_prompt_schema_states_the_same_limit():
    # This is the description handed to `claude --json-schema`, so if it says a
    # different number than the validator, Claude is being asked to fail.
    description = VideoScript.model_json_schema()["properties"]["hook"]["description"]
    assert f"Max {MAX_HOOK_CHARS} characters" in description


def test_a_trailing_period_is_stripped_before_measuring():
    hook = "x" * MAX_HOOK_CHARS + "."
    assert make_script(hook).hook == "x" * MAX_HOOK_CHARS


# --------------------------------------------------------------------------
# Script length
#
# The same drift, found later: the schema description said a flat 80 words
# while the prompt interpolated the setting, so raising the setting sent Claude
# one request asking for two different lengths.
# --------------------------------------------------------------------------


def test_the_schema_asks_for_the_configured_word_budget():
    description = VideoScript.model_json_schema()["properties"]["spoken_script"][
        "description"
    ]
    assert f"Under {get_settings().max_script_words} words" in description


def test_the_prompt_asks_for_the_configured_word_budget():
    cfg = get_settings()
    prompt = _build_prompt(candidate("just-vugg/colibri"), cfg)
    assert f"{cfg.max_script_words}-words-or-fewer" in prompt


def test_the_prompt_states_the_length_the_budget_actually_produces():
    """The prompt tells the model what the budget becomes in seconds, and that
    sentence has been wrong in both directions.

    It claimed 30 to 45 seconds while the budget capped the video near 31,
    which is the observation that led to raising the budget to reach the
    stated target. That was backwards: retention data showed the average
    viewer leaving at five seconds, so the target was the wrong number rather
    than the budget. Pinned here so the next person to change one changes the
    other.
    """
    cfg = get_settings()
    # The cloned voice reads ~170 wpm and the appended ask adds ~7 words.
    seconds = (cfg.max_script_words + 7) / 170 * 60
    prompt = _build_prompt(candidate("just-vugg/colibri"), cfg)

    low, high = 25, 32
    assert f"{low} to {high} seconds" in prompt
    assert low <= seconds <= high, (
        f"{cfg.max_script_words} words is {seconds:.0f}s, outside the {low} to "
        f"{high} seconds the prompt promises"
    )


# --------------------------------------------------------------------------
# Staged assets
# --------------------------------------------------------------------------


def video_dir(tmp_path, *names):
    public = tmp_path / "public"
    public.mkdir()
    for name in names:
        (public / name).write_text("x")
    return tmp_path


def test_other_runs_assets_are_pruned(tmp_path):
    root = video_dir(tmp_path, "old-repo-voice.mp3", "old-repo-repo.png", "new-repo-voice.wav")

    assert prune_staged_assets(root, "new-repo") == 2
    assert {p.name for p in (root / "public").iterdir()} == {"new-repo-voice.wav"}


def test_the_current_runs_assets_survive(tmp_path):
    root = video_dir(tmp_path, "a-b-voice.wav", "a-b-repo.png")

    assert prune_staged_assets(root, "a-b") == 0
    assert len(list((root / "public").iterdir())) == 2


def test_files_we_did_not_stage_are_never_touched(tmp_path):
    # public/ is ours to manage, but not to the point of deleting a font or a
    # .gitkeep someone put there on purpose.
    root = video_dir(tmp_path, ".gitkeep", "logo.svg", "notes.txt", "old-voice.mp3")

    prune_staged_assets(root, "current")

    assert {p.name for p in (root / "public").iterdir()} == {".gitkeep", "logo.svg", "notes.txt"}


def test_pruning_a_missing_public_dir_is_a_no_op(tmp_path):
    assert prune_staged_assets(tmp_path, "anything") == 0


def test_staging_produces_a_name_pruning_recognises(tmp_path):
    # The two halves of the contract: whatever stage_asset writes must be
    # something prune_staged_assets is willing to clean up later.
    source = tmp_path / "voice.wav"
    source.write_text("audio")
    root = tmp_path / "video"
    (root / "public").mkdir(parents=True)

    staged = stage_asset(source, root, "owner-repo")

    assert staged == "owner-repo-voice.wav"
    assert prune_staged_assets(root, "some-other-slug") == 1


# --- The research audit signal ----------------------------------------------


def test_web_searches_are_counted_from_the_model_that_made_them():
    """A search is run by a cheaper model, so the top-level counter stays 0.

    Reading `usage.server_tool_use.web_search_requests` made every run since the
    first report "0 web searches" while research was in fact happening, which
    quietly disabled the one check CLAUDE.md names for auditing it.
    """
    from pipeline.scriptwriter import web_search_count

    envelope = {
        "usage": {"server_tool_use": {"web_search_requests": 0}},
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"webSearchRequests": 3, "costUSD": 0.09},
            "claude-opus-5": {"webSearchRequests": 0, "costUSD": 0.81},
        },
    }
    assert web_search_count(envelope) == 3


def test_an_envelope_without_model_usage_counts_zero_rather_than_raising():
    from pipeline.scriptwriter import web_search_count

    assert web_search_count({}) == 0
    assert web_search_count({"modelUsage": None}) == 0
