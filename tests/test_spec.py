"""Scene timing: the part of the pipeline that decides when each cut lands.

Both functions here are pure arithmetic over a script and a transcript, and
both have failure modes you cannot see by watching one video -- a scene half a
second late reads as "the edit feels slightly off", not as a bug.
"""

from __future__ import annotations

from conftest import candidate, captions_from, script

from config import Settings
from pipeline.models import CueKind
from pipeline.spec import (
    MIN_SCENE_SECONDS,
    _align_to_captions,
    _allocate_frames,
    _cta_start_frame,
    build_spec,
)

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


# --------------------------------------------------------------------------
# The spoken ask
#
# It is appended to the narration after the script is written, so no cue was
# ever written for it. Every one of these is about it not being charged to a
# scene that did not ask for it.
# --------------------------------------------------------------------------

CTA = "Comment COLIBRI if you want the link."


def test_the_ask_is_found_where_it_starts_being_spoken():
    caps = captions_from("one two three comment colibri if you want the link")

    # "comment" is word 3, so 1500ms -> frame 45.
    assert _cta_start_frame(caps, CTA, FPS) == 45


def test_a_keyword_the_transcript_heard_as_words_still_matches():
    """Whisper never returns IHAVEADHD. It returns "I have ADHD"."""
    caps = captions_from("one two three comment i have adhd if you want the link")

    assert _cta_start_frame(caps, "Comment IHAVEADHD if you want the link.", FPS) == 45


def test_the_word_comment_doing_its_ordinary_job_is_not_the_ask():
    caps = captions_from("comment on the issue and the maintainer usually replies within a day")

    assert _cta_start_frame(caps, CTA, FPS) is None


def test_the_last_comment_wins_when_the_script_used_the_word_too():
    caps = captions_from("comment on the issue first comment colibri if you want the link")

    assert _cta_start_frame(caps, CTA, FPS) == 75  # word 5, not word 0


def test_an_ask_nowhere_near_the_end_is_not_believed():
    caps = captions_from(
        "comment colibri if you want the link " + " ".join(["filler"] * 30)
    )

    assert _cta_start_frame(caps, CTA, FPS) is None


def test_no_ask_and_no_captions_are_both_none():
    assert _cta_start_frame([], CTA, FPS) is None
    assert _cta_start_frame(captions_from("one two three"), "", FPS) is None


# Distinct words at 500ms each, so a frame number can be read straight off a
# word index. The narration has to be long enough that the intro is not eating
# a third of it, or the minimum-scene clamp decides the timings instead of the
# transcript and the test stops being about the ask.
NARRATION = " ".join(f"w{i}" for i in range(18))
TRANSCRIPT = f"{NARRATION} comment colibri if you want the link"
SECONDS = 12.5


def _spec_with_cta(*, first_cue_words=14, transcript=TRANSCRIPT, seconds=SECONDS,
                   cta=CTA, shot="hero.png"):
    words = NARRATION.split()
    cfg = Settings(github_token="x", _env_file=None)
    return build_spec(
        candidate("just-vugg/colibri"),
        script(" ".join(words[:first_cue_words]), " ".join(words[first_cue_words:])),
        captions_from(transcript),
        seconds,
        "voice.wav",
        cfg,
        screenshot_src=shot,
        spoken_cta=cta,
    )


def test_the_spec_records_the_frame_the_ask_starts_on():
    """What lets a surface with no private replies cut the ask off.

    Recorded rather than derived from scene lengths, because the ask only gets
    a scene of its own when the split lands clear of a boundary, and a cut in
    the wrong place is worse than no cut at all.
    """
    spec = _spec_with_cta()

    assert spec.ctaFromFrame == spec.scenes[-1].fromFrame


def test_no_clean_split_means_no_frame_to_cut_at(caplog):
    """An ask too close to a boundary keeps its place in the last cue, so there
    is no frame where the video stops being about the repo. None says so."""
    with caplog.at_level("INFO"):
        spec = _spec_with_cta(transcript=f"{NARRATION} comment colibri", seconds=3.0)

    if spec.ctaFromFrame is None:
        assert len(spec.scenes) < 4
    else:
        assert spec.ctaFromFrame == spec.scenes[-1].fromFrame


def test_the_ask_gets_a_scene_instead_of_the_last_cue_holding_through_it():
    spec = _spec_with_cta()

    # One scene per cue, plus the intro, plus the ask.
    assert len(spec.scenes) == 4
    ask = spec.scenes[-1]
    assert ask.kind is CueKind.SCREENSHOT
    assert ask.imageSrc == "hero.png"
    # It starts on "comment", word 18 of the transcript, and runs to the end.
    assert ask.fromFrame == 270
    assert ask.fromFrame + ask.durationInFrames == spec.durationInFrames


def test_the_last_cue_stops_when_it_stops_being_spoken():
    """The whole point: those seconds used to be charged to this scene."""
    spec = _spec_with_cta()

    last_cue = spec.scenes[-2]
    assert last_cue.fromFrame + last_cue.durationInFrames == 270


def test_the_ask_falls_back_to_the_repo_card_with_no_screenshot():
    spec = _spec_with_cta(shot=None)

    assert spec.scenes[-1].kind is CueKind.REPO_CARD


def test_nothing_is_split_when_no_ask_was_spoken():
    spec = _spec_with_cta(transcript=NARRATION, seconds=9.0, cta=None)

    assert len(spec.scenes) == 3
    assert spec.scenes[-1].kind is not CueKind.SCREENSHOT


def test_a_split_too_tight_to_be_readable_is_declined():
    """Better one long-ish scene than two nobody can read."""
    spec = _spec_with_cta(first_cue_words=16)

    # The last cue is two words, so taking the ask out of it would leave well
    # under the 1.8s floor on the content side.
    assert len(spec.scenes) == 3
    assert all(s.kind is not CueKind.SCREENSHOT for s in spec.scenes[1:])
