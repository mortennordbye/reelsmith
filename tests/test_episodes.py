"""The second niche's scriptwriter, and the arc that lives in its model.

`VideoScript` is a hook plus prose because a video about a repository is one
argument. This niche's arc took a session to find, and the beats that went
missing longest are the ones a model drops when asked for "a script about
Montaigne", so they are fields rather than instructions: a script that ends on
an aphorism cannot satisfy the schema.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from config import Settings
from pipeline import claude as claude_cli
from pipeline import episodes
from pipeline.models import EpisodeScript, SubjectCandidate

QUOTE = (
    "For he does not expect to do it the First, Second, or Seventh time; "
    "but mends on, on, on, by a little at a time, till at last it is so finisht."
)

WHOLE = {
    "hook": "You fixed it six times and it is still wrong",
    "situation": "You have fixed the same thing six times. It is still wrong.",
    "who": "Joseph Moxon cut type in London.",
    "quote": QUOTE,
    "plain": "Seven goes is not failure.",
    "did": (
        "He measured twenty samples against a pattern, mended the mould a little, "
        "and cast twenty fresh."
    ),
    "back": "Stop judging your seventh try against your first.",
    "steps": ["Find one known good example", "Mend one thing", "Delete the old result"],
    "source": "Moxon, Mechanick Exercises, London 1683. Project Gutenberg 72217.",
}


@pytest.fixture
def cfg(tmp_path) -> Settings:
    # A placeholder name, not a real account. This repo is public and the
    # account identities are the private half; a handle in a fixture here is
    # the same link PROFILE.md refuses to let a shared voice make.
    return Settings(account="secondaccount", _env_file=None)


@pytest.fixture
def subject() -> SubjectCandidate:
    return SubjectCandidate(
        qid="Q3057690", name="Joseph Moxon", article="Joseph Moxon", born=1627, died=1691
    )


def test_the_arc_is_required_rather_than_hoped_for():
    """A script with no way back to the viewer and no steps is the failure the
    format's own history records, so it cannot be expressed."""
    missing = {k: v for k, v in WHOLE.items() if k not in {"back", "steps"}}

    with pytest.raises(ValidationError):
        EpisodeScript.model_validate(missing)


def test_there_are_exactly_three_steps():
    with pytest.raises(ValidationError):
        EpisodeScript.model_validate({**WHOLE, "steps": ["one", "two"]})


def test_the_ban_on_dashes_reaches_the_quote():
    """A real cost, taken knowingly. The captions burned into the video come
    from what is spoken, and a dash is invisible to a listener and clutter on
    screen. The way out is quoting the clause that has none."""
    with pytest.raises(ValidationError) as exc:
        EpisodeScript.model_validate({**WHOLE, "quote": "a self-evident truth"})

    assert "quote the clause that has none" in str(exc.value)


def test_a_long_quote_is_split_at_its_own_punctuation():
    """One line is one shot, so a 36 word sentence is an 11 second shot. The
    words are not ours to change; where it is cut is."""
    script = EpisodeScript.model_validate(WHOLE)
    spoken = script.lines

    assert all(len(line.split()) <= 18 for line in spoken), spoken
    assert " ".join(spoken).count("mends on, on, on") == 1
    assert QUOTE.replace("; ", "; ") in " ".join(spoken)


def test_the_break_lands_where_a_reader_would_pause():
    """Filling greedily to the limit broke "mends on, on, on" between the
    second and third "on", which is the one place nobody would pause."""
    script = EpisodeScript.model_validate(WHOLE)

    assert any(line.endswith("Seventh time;") for line in script.lines)


def test_the_steps_are_the_last_three_lines():
    script = EpisodeScript.model_validate(WHOLE)

    assert script.lines[-3:] == WHOLE["steps"]


def test_a_rejected_script_is_handed_back_for_a_fix(cfg, subject, monkeypatch):
    """One extra call, against throwing away a whole generation over a hyphen.
    The same trade `scriptwriter.py` makes."""
    attempts: list[str] = []

    def answer(prompt, schema, cfg, system, research=None):
        attempts.append(prompt)
        if len(attempts) == 1:
            return {"structured_output": {**WHOLE, "hook": "A self-evident hook"}}
        return {"structured_output": WHOLE}

    monkeypatch.setattr(claude_cli, "run", answer)

    script = episodes.write(cfg, subject)

    assert len(attempts) == 2
    assert "was rejected" in attempts[1]
    assert script.hook == WHOLE["hook"]


def test_it_gives_up_rather_than_returning_half_a_script(cfg, subject, monkeypatch):
    monkeypatch.setattr(
        claude_cli,
        "run",
        lambda *a, **kw: {"structured_output": {**WHOLE, "steps": ["only", "two"]}},
    )

    with pytest.raises(claude_cli.ClaudeError):
        episodes.write(cfg, subject)


def test_the_prompt_names_the_artefacts_the_video_will_be_cut_from(cfg, subject, monkeypatch):
    """The shots come from what Commons actually holds, so a script written
    against pictures nobody has is a script that cannot be rendered."""
    seen = {}

    def answer(prompt, schema, cfg, system, research=None):
        seen["prompt"] = prompt
        return {"structured_output": WHOLE}

    monkeypatch.setattr(claude_cli, "run", answer)
    episodes.write(cfg, subject.model_copy(update={"artefacts": ["File:Mechanick.jpg"]}))

    assert "File:Mechanick.jpg" in seen["prompt"]


def test_research_is_on_for_the_script(cfg, subject, monkeypatch):
    """Discovery runs without it because Wikidata checks the answer afterwards.
    Nothing checks a quote."""
    seen = {}

    def answer(prompt, schema, cfg, system, research=None):
        seen["research"] = research
        return {"structured_output": WHOLE}

    monkeypatch.setattr(claude_cli, "run", answer)
    episodes.write(cfg, subject)

    assert seen["research"] is True


def test_the_schema_comes_from_the_model():
    """Generated rather than written, so the prompt contract and the parser
    cannot drift apart."""
    schema = episodes.schema()

    assert set(schema["required"]) >= {"hook", "quote", "did", "back", "steps", "source"}
    assert json.dumps(schema)
