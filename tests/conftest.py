"""Shared builders.

Everything under tests/ is pure logic: no network, no model weights, no
Remotion. If a test needs a fixture file, it is testing the wrong thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings
from pipeline.models import Caption, CueKind, RepoCandidate, VideoScript, VisualCue


@pytest.fixture(autouse=True)
def _no_writing_into_the_real_build_dir():
    """Fail the test that puts something in `build/`, naming it.

    `Settings.build_dir` is a property rooted at the repo and it creates the
    directory on access, so a test that reads it rather than a tmp_path writes
    into the developer's real runs. That is not hypothetical: the first version
    of `tests/test_results.py` left nineteen fixture folders in there, with
    repo names like `r/7`, and they would have been joined to real Instagram
    numbers by the feedback loop.
    """
    root = Path(__file__).resolve().parent.parent / "build"
    before = {p.name for day in root.glob("*") if day.is_dir() for p in day.glob("*")}

    yield

    after = {p.name for day in root.glob("*") if day.is_dir() for p in day.glob("*")}
    added = after - before
    assert not added, (
        f"this test wrote {sorted(added)} into the real build/. Pass a tmp_path "
        f"instead of reading Settings.build_dir."
    )


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    """Keep the developer's real `.env` out of every test.

    `Settings` reads the repo's `.env` by default, so without this a test's
    result depends on whether the machine running it happens to have Instagram
    configured, or a gateway URL set, or a different TTS backend. That is not a
    hypothetical: adding GATEWAY_URL to a working `.env` turned three passing
    tests red without touching a line of their code.

    Tests that want a value now have to pass it, which is the point.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


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
