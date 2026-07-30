"""Shared builders.

Everything under tests/ is pure logic: no network, no model weights, no
Remotion. If a test needs a fixture file, it is testing the wrong thing.
"""

from __future__ import annotations

from pipeline.models import Caption, CueKind, RepoCandidate, VideoScript, VisualCue


def cue(excerpt: str, kind: CueKind = CueKind.BULLETS) -> VisualCue:
    return VisualCue(kind=kind, spoken_excerpt=excerpt)


def script(*excerpts: str, hook: str = "A hook") -> VideoScript:
    return VideoScript(
        hook=hook,
        spoken_script=" ".join(excerpts),
        visual_cues=[cue(e) for e in excerpts],
    )


def captions_from(text: str, *, word_ms: float = 500.0, start_ms: float = 0.0) -> list[Caption]:
    """One caption per word, evenly spaced. Word i starts at start_ms + i*word_ms."""
    out = []
    for i, word in enumerate(text.split()):
        begin = start_ms + i * word_ms
        out.append(Caption(text=word, startMs=begin, endMs=begin + word_ms * 0.8))
    return out


def candidate(full_name: str, **kwargs) -> RepoCandidate:
    owner, _, name = full_name.partition("/")
    defaults = {
        "name": name,
        "owner": owner,
        "url": f"https://github.com/{full_name}",
        "stars": 1000,
    }
    return RepoCandidate(full_name=full_name, **{**defaults, **kwargs})
