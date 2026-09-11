"""The scriptwriter for the second niche.

A separate module rather than a second prompt inside `scriptwriter.py`, because
`SYSTEM_PROMPT` there is the one part of that file that is genuinely about
repositories and this is the one part of this file that is genuinely about
people. What the two share is the CLI invocation, which is `pipeline/claude.py`,
and the text rules, which are validators on the models.

**The arc is in the model, not in the prompt.** `EpisodeScript` has a field per
beat, so a script that ends on an aphorism cannot satisfy the schema. That is
deliberate: the format's arc took a session to find and the two beats that went
missing longest, coming back to the viewer and the three steps, are exactly the
ones a model drops when it is asked for "a script about Montaigne".

**Research is on, and this is the call that needs it.** Discovery runs without
it because Wikidata checks the answer afterwards. Nothing checks a quote, so
the model has to go and read the source, and the source is named in the output
so that a person can check it too.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from config import Settings
from pipeline import claude as claude_cli
from pipeline.models import EpisodeScript, SubjectCandidate

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3

# The claim is a placeholder the account fills in, not a constant.
#
# It read the second account's end card line verbatim, which was wrong twice
# over. **This repo is public**, so a line naming an identity puts that
# identity in it, which is what `PROFILE.md` is gitignored to prevent, and the
# handle was already carried alongside account 1's throughout the tests.
# And a claim is a fact about one account, so a hardcoded one is a prompt that
# cannot serve a second niche, which `CLAUDE.md` already says needs one
# `SYSTEM_PROMPT` per niche.
#
# `ENDCARD_TAGLINE` is where it lives, on the account, next to the end card it
# is the text of. Empty, the sentence is dropped rather than printed with
# nothing after it, the same rule `youtube_description` follows for a missing
# repo line.
EPISODE_SYSTEM = """You write one episode of a short video account whose whole
claim is that every piece of advice arrives with a source a viewer could go and
check.{claim} The audience is working
adults, many of them engineers, who have seen a thousand generated videos.

The arc, in order, and every beat earns its place:

1. The viewer's own situation, in the second person. Not the quote.
2. Who the person was and when, so the quote can mean something.
3. The quote, in their own words.
4. The same thing in plain words.
5. What they actually did about it, taken from the source.
6. Back to the viewer, in the second person.
7. Three numbered steps, derived from the source rather than invented.

Steps 6 and 7 are the ones that get dropped. A cut that ends on an aphorism
sounds like a conclusion and changes nothing for the person watching, and a
video that opens on the viewer and never returns to them is trivia with a nice
sentence on the end.

Step 7 must come from the source or the account is what it claims not to be.
Advice invented here and dressed in a historical costume is exactly the move
this format exists against. If the source does not support three steps, say so
by choosing a different aspect of the person, not by inventing them.

Language:
- Plain words carry the argument. A quotation keeps its own words.
- Any hard word that has to survive gets its meaning in the same breath.
- Short sentences, average under twelve words, varied hard in length.
- No colons and no dashes of any kind, anywhere, including inside the quote.
  Quote the clause that has none rather than rewriting the quotation.
- No emoji. No hype words. No "in today's fast paced world".
- Never invent a fact, a date, a number or a quotation. If a figure would have
  to be estimated, leave it out.

What this must not sound like: the ancient wisdom register, a slowed dramatic
read, a decontextualised quote card, or a motivational post. It is one person
telling another something they found out, with the receipt attached.

Length is 40 to 50 seconds spoken, so about 110 to 140 words in total."""


def schema() -> dict[str, Any]:
    """The JSON Schema handed to the CLI, generated from the model.

    Generated rather than written, for `scriptwriter.py`'s reason: the prompt
    contract and the parser cannot drift apart if there is only one of them.
    """
    return EpisodeScript.model_json_schema()


def _prompt(subject: SubjectCandidate, note: str = "") -> str:
    lived = " to ".join(
        str(year) if year is not None else "unknown" for year in (subject.born, subject.died)
    )
    artefacts = "\n".join(f"- {name}" for name in subject.artefacts[:8])
    return (
        f"Tonight's subject is {subject.name}, {lived}.\n"
        f"{subject.description}\n\n"
        f"Read a primary source of theirs before writing. Name it in `source` "
        f"precisely enough that a viewer could find the same page: the work, the "
        f"translation if it is one, and where it can be read.\n\n"
        f"The pictures the video will be cut from, which are what exists on "
        f"Wikimedia Commons for this subject:\n{artefacts or '- a portrait'}\n\n"
        f"Write the episode. {note}".strip()
    )


def episode_system(cfg: Settings) -> str:
    """The system prompt with this account's own claim in it.

    A function rather than an f-string at import time, because `EPISODE_SYSTEM`
    is read by tests and by anyone reading the file, and the account is not
    selected when this module is imported.
    """
    tagline = (cfg.endcard_tagline or "").strip().rstrip(".")
    claim = f' The end card reads "{tagline}".' if tagline else ""
    return EPISODE_SYSTEM.format(claim=claim)


def write(cfg: Settings, subject: SubjectCandidate) -> EpisodeScript:
    """One episode, or a raised error. Never a half script.

    The correction loop is `scriptwriter.py`'s and for the same reason: the
    constraints a model trips on here, the hook length, the three steps and the
    ban on dashes, are pydantic validators that JSON Schema cannot express, so
    handing the failure back costs one call where failing the run throws away a
    generation over a hyphen.
    """
    prompt = _prompt(subject)
    for attempt in range(_MAX_ATTEMPTS):
        envelope = claude_cli.run(
            prompt, schema(), cfg, system=episode_system(cfg), research=True
        )
        payload = envelope.get("structured_output") or claude_cli.payload(envelope)
        if payload is None:
            raise claude_cli.ClaudeError(
                "Claude Code returned no structured_output. This usually means the "
                "installed CLI predates --json-schema support; check `claude --version`."
            )
        try:
            script = EpisodeScript.model_validate(payload)
        except ValidationError as exc:
            if attempt == _MAX_ATTEMPTS - 1:
                raise claude_cli.ClaudeError(
                    f"Episode still invalid after {_MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
            log.warning(
                "Episode failed validation (attempt %d/%d), asking for a fix: %s",
                attempt + 1,
                _MAX_ATTEMPTS,
                "; ".join(e["msg"] for e in exc.errors()),
            )
            prompt = (
                f"{_prompt(subject)}\n\n"
                f"## Your previous answer was rejected\n\n"
                f"{json.dumps(payload, indent=2)}\n\n"
                f"It failed validation:\n\n"
                + "\n".join(
                    f"- {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                + "\n\nProduce a corrected version. Fix only what was rejected; "
                "keep everything else as close to the original as you can."
            )
            continue

        log.info(
            "Episode for %s: %d words across %d lines", subject.name, script.word_count,
            len(script.lines),
        )
        return script
    raise AssertionError("unreachable")  # pragma: no cover
