"""Caption gap repair.

Whisper emits tight per-word bounds with silence between them. Rendered
verbatim, captions flicker off between every word.
"""

from __future__ import annotations

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
