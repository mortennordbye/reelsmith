"""Caption gap repair.

Whisper emits tight per-word bounds with silence between them. Rendered
verbatim, captions flicker off between every word.
"""

from __future__ import annotations

from pipeline import captions
from pipeline.captions import _repair_gaps
from pipeline.models import Caption


def caps(*spans: tuple[float, float]) -> list[Caption]:
    return [Caption(text=f"w{i}", startMs=a, endMs=b) for i, (a, b) in enumerate(spans)]


def test_a_small_gap_is_closed():
    got = _repair_gaps(caps((0, 300), (500, 800)), max_gap_ms=400)
    assert [(c.startMs, c.endMs) for c in got] == [(0, 500), (500, 800)]


def test_a_real_pause_is_left_alone():
    # A 900ms gap is a breath between sentences. Stretching a word across it
    # makes the caption sit on screen through silence, which reads as a stall.
    got = _repair_gaps(caps((0, 300), (1200, 1500)), max_gap_ms=400)
    assert [(c.startMs, c.endMs) for c in got] == [(0, 300), (1200, 1500)]


def test_the_boundary_gap_is_closed():
    got = _repair_gaps(caps((0, 300), (700, 900)), max_gap_ms=400)
    assert got[0].endMs == 700


def test_overlapping_words_are_not_pulled_backwards():
    got = _repair_gaps(caps((0, 600), (500, 900)), max_gap_ms=400)
    assert got[0].endMs == 600


def test_the_last_word_keeps_its_own_end():
    got = _repair_gaps(caps((0, 300), (400, 900)), max_gap_ms=400)
    assert got[-1].endMs == 900


def test_repair_runs_across_a_whole_sequence():
    got = _repair_gaps(caps((0, 100), (200, 300), (400, 500), (2000, 2100)), max_gap_ms=400)
    assert [(c.startMs, c.endMs) for c in got] == [
        (0, 200), (200, 400), (400, 500), (2000, 2100),
    ]


def test_a_single_word_is_untouched():
    got = _repair_gaps(caps((0, 300)))
    assert [(c.startMs, c.endMs) for c in got] == [(0, 300)]


def test_an_empty_transcript_is_handled():
    assert _repair_gaps([]) == []


class TestGlossary:
    """The caption prompt must never contain the script's prose.

    Whisper reads `initial_prompt` as text preceding the audio, so feeding it
    the transcript makes it skip the opening. That cost the first nine words of
    a run and was invisible until the burned-in captions were compared against
    the voiceover.
    """

    SCRIPT = (
        "Frontier models want a datacenter. Colibri wants your disk. It runs a "
        "744 billion parameter model in pure C with no Python and no CUDA runtime. "
        "Six RTX 5090s reach about six tokens a second."
    )

    def test_picks_up_terms_whisper_would_mis_spell(self):
        terms = captions.glossary(self.SCRIPT)
        for expected in ["Colibri", "744", "Python", "CUDA", "RTX", "5090s"]:
            assert expected in terms, f"{expected} missing from {terms!r}"

    def test_drops_ordinary_prose(self):
        terms = captions.glossary(self.SCRIPT)
        for common in ["want", "wants", "your", "disk", "model", "about", "second"]:
            assert common not in terms.split(", "), f"{common!r} should not be a term"

    def test_bare_sentence_openers_are_not_terms(self):
        # "It" and "Six" only open sentences. "Colibri" also opens one, but is
        # the single word here that most needs spelling help, so position alone
        # cannot be the test.
        terms = captions.glossary(self.SCRIPT).split(", ")
        assert "It" not in terms
        assert "Six" not in terms
        assert "Colibri" in terms

    def test_numerals_are_kept(self):
        # Biases Whisper toward "744" over "seven hundred forty four".
        terms = captions.glossary(self.SCRIPT).split(", ")
        assert "744" in terms
        assert "5090s" in terms

    def test_is_a_list_not_prose(self):
        # The whole fix. If this ever reads as a sentence, Whisper will treat it
        # as preceding transcript and drop the opening words again.
        terms = captions.glossary(self.SCRIPT)
        assert ". " not in terms
        assert terms.count(",") >= 3

    def test_empty_script_yields_no_prompt(self):
        assert captions.glossary("") == ""

    def test_is_bounded(self):
        long_script = " ".join(f"Term{i} X{i}Y" for i in range(200))
        assert len(captions.glossary(long_script).split(", ")) <= 24


def _heard(*pairs) -> list[Caption]:
    """Build a caption list from (text, start_ms) pairs, 200 ms each."""
    return [
        Caption(text=t, startMs=s, endMs=s + 200, timestampMs=s + 100)
        for t, s in pairs
    ]


class TestAlignToScript:
    """Whisper supplies timing; the script supplies words.

    Trusting Whisper for text turned "pure C" into "PRC" and produced
    "744 billion -parameter", and `Captions.tsx` renders `text` verbatim, so
    both would have burned onto the video.
    """

    def test_mishearings_are_replaced_by_the_script(self):
        heard = _heard(("in", 0), ("PRC,", 200), ("with", 400))
        out = captions._align_to_script(heard, "in pure C with")
        assert [c.text for c in out] == ["in", "pure", "C", "with"]

    def test_timings_survive_the_replacement(self):
        heard = _heard(("in", 0), ("PRC,", 200), ("with", 400))
        out = captions._align_to_script(heard, "in pure C with")
        assert out[0].startMs == 0  # matched word keeps its own timing
        assert out[-1].startMs == 400
        assert all(out[i].startMs <= out[i + 1].startMs for i in range(len(out) - 1))

    def test_punctuation_whisper_invents_is_dropped(self):
        # The project bans dashes from burned-in text; Whisper emits them.
        heard = _heard(("744", 0), ("billion", 200), ("-parameter", 400))
        out = captions._align_to_script(heard, "744 billion parameter")
        assert "-parameter" not in [c.text for c in out]
        assert [c.text for c in out] == ["744", "billion", "parameter"]

    def test_every_script_word_reaches_the_screen(self):
        heard = _heard(("It", 0), ("runs", 200))
        out = captions._align_to_script(heard, "It runs a model")
        assert [c.text for c in out] == ["It", "runs", "a", "model"]

    def test_extra_words_whisper_hallucinated_are_dropped(self):
        heard = _heard(("It", 0), ("uh", 200), ("runs", 400))
        out = captions._align_to_script(heard, "It runs")
        assert [c.text for c in out] == ["It", "runs"]

    def test_no_script_leaves_transcription_untouched(self):
        # Some timing beats none if the script is unavailable.
        heard = _heard(("It", 0), ("runs", 200))
        assert captions._align_to_script(heard, "") == heard

    def test_empty_transcription_is_not_fabricated(self):
        assert captions._align_to_script([], "It runs") == []

    def test_full_disagreement_still_spans_the_audio(self):
        heard = _heard(("aaa", 0), ("bbb", 1000))
        out = captions._align_to_script(heard, "one two three")
        assert [c.text for c in out] == ["one", "two", "three"]
        assert out[0].startMs >= 0
        assert out[-1].endMs <= 1400
