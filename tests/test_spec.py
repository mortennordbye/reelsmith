"""Scene timing: the part of the pipeline that decides when each cut lands.

Both functions here are pure arithmetic over a script and a transcript, and
both have failure modes you cannot see by watching one video -- a scene half a
second late reads as "the edit feels slightly off", not as a bug.
"""

from __future__ import annotations

from conftest import captions_from, script

from pipeline.spec import MIN_SCENE_SECONDS, _align_to_captions, _allocate_frames

FPS = 30
MIN_FRAMES = int(MIN_SCENE_SECONDS * FPS)  # 54


# --------------------------------------------------------------------------
# _align_to_captions
# --------------------------------------------------------------------------


def test_cuts_land_on_the_frame_the_next_cue_is_spoken():
    spoken = script("hello world this is", "the second beat here")
    caps = captions_from("hello world this is the second beat here")  # 500ms per word

    got = _align_to_captions(spoken, caps, total_frames=300, fps=FPS, start_frame=0)

    # "the" is word 5, so it starts at 2000ms -> frame 60.
    assert got == [(0, 60), (60, 240)]


def test_alignment_respects_the_intro_offset():
    spoken = script("hello world this is", "the second beat here")
    caps = captions_from("hello world this is the second beat here")

    got = _align_to_captions(spoken, caps, total_frames=300, fps=FPS, start_frame=90)

    # The cues own [90, 390). The second cue is spoken at frame 60, which is
    # already behind the intro, so it gets pushed to the first legal cut --
    # this is the screenshot intro deliberately overlapping the narration.
    assert got == [(90, MIN_FRAMES), (144, 246)]


def test_punctuation_and_casing_do_not_break_matching():
    spoken = script("Ruff replaces flake8", "It runs, in Rust.")
    caps = captions_from("ruff replaces flake8 it runs in rust")

    got = _align_to_captions(spoken, caps, total_frames=600, fps=FPS, start_frame=0)

    assert got is not None
    # "it" is word 4 -> 1500ms -> frame 45, floored to the minimum scene length.
    assert got[0] == (0, MIN_FRAMES)


def test_returns_none_when_the_transcript_diverged():
    spoken = script("hello world this is", "completely different words entirely")
    caps = captions_from("hello world this is something else that was heard")

    assert _align_to_captions(spoken, caps, 300, FPS, 0) is None


def test_returns_none_when_a_cue_has_too_little_to_match_on():
    spoken = script("hello world this is", "yes")
    caps = captions_from("hello world this is yes")

    assert _align_to_captions(spoken, caps, 300, FPS, 0) is None


def test_returns_none_when_the_last_scene_is_squeezed_past_the_end():
    spoken = script("one two three four", "five six seven eight")
    caps = captions_from("one two three four five six seven eight")

    # "five" lands at 2000ms -> frame 60, leaving 10 frames for the last scene.
    assert _align_to_captions(spoken, caps, total_frames=70, fps=FPS, start_frame=0) is None


def test_boundaries_stay_monotonic_when_cues_are_spoken_close_together():
    spoken = script("alpha beta", "gamma delta", "epsilon zeta")
    caps = captions_from("alpha beta gamma delta epsilon zeta", word_ms=100.0)

    got = _align_to_captions(spoken, caps, total_frames=600, fps=FPS, start_frame=0)

    assert got is not None
    starts = [start for start, _ in got]
    assert starts == sorted(starts)
    assert all(duration >= MIN_FRAMES for _, duration in got[:-1])


def test_no_cues_or_no_captions_falls_back():
    assert _align_to_captions(script(), [], 300, FPS, 0) is None
    assert _align_to_captions(script("a b c"), [], 300, FPS, 0) is None


# --------------------------------------------------------------------------
# _allocate_frames
# --------------------------------------------------------------------------


def test_rounding_drift_lands_on_the_longest_scene():
    spoken = script("a b c", "a b c d e", "a b c d e f g")  # weights 3, 5, 7

    got = _allocate_frames(spoken, total_frames=1000, fps=FPS)

    # Raw shares are 200 / 333.33 / 466.67; the 1 leftover frame goes to the
    # longest scene, never to the first one, so the total is exact.
    assert got == [(0, 200), (200, 333), (533, 467)]
    assert sum(duration for _, duration in got) == 1000


def test_short_scenes_get_the_minimum_and_the_long_one_pays_for_it():
    spoken = script("one", " ".join(["word"] * 99))

    got = _allocate_frames(spoken, total_frames=1000, fps=FPS)

    assert got[0] == (0, MIN_FRAMES)
    assert sum(duration for _, duration in got) == 1000


def test_the_floor_collapses_rather_than_overrunning_the_audio():
    spoken = script(*["one two"] * 10)

    # 10 scenes x 54 frames would need 540; only 300 are available.
    got = _allocate_frames(spoken, total_frames=300, fps=FPS)

    assert [duration for _, duration in got] == [30] * 10
    assert sum(duration for _, duration in got) == 300


def test_a_cue_with_no_excerpt_still_gets_screen_time():
    spoken = script("", "a b c d")

    got = _allocate_frames(spoken, total_frames=600, fps=FPS)

    assert got[0][1] >= MIN_FRAMES
    assert sum(duration for _, duration in got) == 600


def test_allocation_starts_where_the_intro_ends():
    spoken = script("a b", "c d")

    got = _allocate_frames(spoken, total_frames=300, fps=FPS, start_frame=210)

    assert got[0][0] == 210
    assert got[-1][0] + got[-1][1] == 510


def test_no_cues_allocates_nothing():
    assert _allocate_frames(script(), 300, FPS) == []
