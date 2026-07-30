"""Step 2 -- turn a repository into a 30-45s script.

This shells out to the Claude Code CLI in headless mode rather than calling the
paid Anthropic API. Claude Code authenticates with the existing subscription,
so there is no ANTHROPIC_API_KEY anywhere in this project.

    claude -p "<prompt>" --json-schema '<schema>' --output-format json

The response envelope carries a top-level `structured_output` key that is
already parsed into an object, alongside `is_error` / `subtype` / usage stats.

Two things to know before editing this file:

  * Never add --bare. Its own help text says auth becomes *strictly*
    ANTHROPIC_API_KEY or apiKeyHelper, with OAuth never read. It would
    silently reintroduce the paid-API dependency we are avoiding.

  * Each invocation loads Claude Code's full harness (~35k cached prompt
    tokens), so this makes exactly ONE call per run and asks for the whole
    script at once. Do not split this into a call per scene.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from config import Settings, resolve_claude_cli
from pipeline.models import RepoCandidate, VideoScript

log = logging.getLogger(__name__)


class ScriptGenerationError(RuntimeError):
    """Claude Code failed to produce a usable script."""


SYSTEM_PROMPT = """\
You write scripts for short-form vertical videos aimed at working software \
engineers, IT consultants, and AI practitioners. Your audience is technical: \
they can read code, they know what an API is, and they resent being talked \
down to.

Rules you never break:
- No hype words: "game-changer", "revolutionary", "insane", "mind-blowing", \
"you won't believe". No emoji. No hashtags in the spoken script.
- Lead with what the thing actually does, not with how excited you are.
- Be concrete and specific. "Cuts cold-start from 900ms to 40ms" beats \
"incredibly fast".
- Assume the viewer is scrolling. The first line has to earn the second.
- Never invent facts, benchmarks, version numbers, or quotes. If you are not \
certain of a number, leave it out entirely.

Energy is not the same as hype, and this is the distinction that matters most.
Hype is empty adjectives. Energy is pace and momentum, and it comes from
sentence construction:

- Keep sentences SHORT. Average under twelve words. A three-word sentence is a
  weapon -- use a few.
- Vary the length hard. Long, then short. That contrast is what creates punch;
  a run of same-length sentences flattens into drone no matter who reads it.
- Open on a verb or a concrete noun. Never "This is a tool that..." or
  "The project aims to...". Cut every throat-clearing phrase.
- Active voice, strong verbs. "It replaces pip" not "pip can be replaced by it".
- Address the viewer as "you" where it fits naturally.
- No subordinate-clause pile-ups. One idea per sentence.

A useful test: read it aloud at speed. If you run out of breath or lose the
thread mid-sentence, it is too long for short-form.
"""


def _build_prompt(repo: RepoCandidate, cfg: Settings) -> str:
    facts = [
        f"Repository: {repo.full_name}",
        f"URL: {repo.url}",
        f"Description: {repo.description or '(none provided)'}",
        f"Stars: {repo.stars:,}",
        f"Primary language: {repo.language or 'unknown'}",
        f"License: {repo.license_spdx or 'unknown'}",
    ]
    if repo.velocity_is_measured and repo.stars_gained_today:
        facts.append(f"Stars gained in the last day: ~{repo.stars_gained_today:,}")
    if repo.topics:
        facts.append(f"Topics: {', '.join(repo.topics[:12])}")
    if repo.hn_points:
        facts.append(f"On Hacker News right now with {repo.hn_points} points ({repo.hn_url})")

    research_note = (
        "Before writing, use web search to check what this project actually does, "
        "what problem it solves, and how it compares to the obvious alternative. "
        "Do not rely on the README alone -- READMEs oversell. If your research "
        "contradicts the README, trust your research.\n"
        if cfg.claude_research
        else "Work only from the facts and README below.\n"
    )

    return f"""{research_note}
Write a script for a {cfg.max_script_words}-words-or-fewer vertical video about \
this trending repository.

## Facts
{chr(10).join(facts)}

## README (may be truncated)
{repo.readme or '(no README available)'}

## What to produce

hook
    The text overlay for the first 3 seconds. Under {cfg.max_hook_chars} \
characters -- this is validated, and a longer hook fails the run. It must make
    a specific claim or pose a real question -- not "This tool is amazing". No
    trailing period.

spoken_script
    The voiceover, UNDER {cfg.max_script_words} WORDS. This is a hard limit: at
    normal speaking pace it becomes roughly 30-45 seconds of audio, and going
    over means the video runs long. Structure it as: what it is -> the specific
    problem it solves -> one concrete detail a developer would care about ->
    what to do next. Write for the ear: no semicolons, no parentheses, expand
    symbols ("about 20 percent", not "~20%").

    This is read aloud by a synthetic voice, which flattens whatever prosody
    your punctuation implies. It cannot rescue a limp sentence, so the momentum
    has to be built into the words. Apply the energy rules above hard here --
    short sentences, varied length, verb-first openings. If two consecutive
    sentences have a similar shape, rewrite one.

visual_cues
    3 to 6 ordered beats describing what is on screen. Each cue's
    spoken_excerpt must be the portion of spoken_script playing during that
    beat -- concatenated in order they should reconstruct the whole script,
    because the renderer uses them to allocate screen time. Available kinds:

      repo_card  title=repo name, subtitle=one-line description
      code       code=a SHORT snippet (max 12 lines, max 60 chars per line --
                 it must be legible on a phone), code_language=the language
      terminal   code=the install or run command, code_language="bash"
      stat       stat_value=a short number/figure, stat_label=what it measures
      bullets    bullets=2 to 4 lines, each 6 words or fewer

    Use at least one `code` or `terminal` cue -- this audience wants to see the
    actual interface. Only cite a `stat` you are confident is real.

caption_text
    The Instagram caption: two or three sentences, then 5-8 relevant hashtags.
"""


def _run_claude(prompt: str, schema: dict[str, Any], cfg: Settings) -> dict[str, Any]:
    cmd = [
        resolve_claude_cli(),
        "-p", prompt,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
        "--model", cfg.claude_model,
        "--effort", cfg.claude_effort,
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    # Research is what makes this better than a plain API call: Claude Code can
    # look the project up rather than paraphrasing its README.
    cmd += ["--allowedTools", "WebSearch WebFetch"] if cfg.claude_research else [
        "--allowedTools", ""
    ]

    log.info("Invoking Claude Code (model=%s, research=%s)", cfg.claude_model, cfg.claude_research)
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, capture_output=True, text=True, timeout=cfg.claude_timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ScriptGenerationError(
            f"Claude Code did not finish within {cfg.claude_timeout_s}s. "
            f"Raise CLAUDE_TIMEOUT_S, or set CLAUDE_RESEARCH=false to skip web search."
        ) from exc

    if proc.returncode != 0:
        raise ScriptGenerationError(
            f"claude exited {proc.returncode}.\n"
            f"stderr: {proc.stderr[:800]}\n"
            f"If this says you are not authenticated, run `claude` once interactively "
            f"to sign in."
        )

    try:
        envelope: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ScriptGenerationError(
            f"Could not parse Claude Code output as JSON: {proc.stdout[:500]}"
        ) from exc

    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise ScriptGenerationError(
            f"Claude Code reported failure "
            f"(subtype={envelope.get('subtype')}): {str(envelope.get('result'))[:500]}"
        )
    return envelope


def write_script(repo: RepoCandidate, cfg: Settings) -> tuple[VideoScript, dict[str, Any]]:
    """Generate the script. Returns (script, raw_envelope) so callers can
    persist the envelope for auditing -- it carries the web-search count and
    cost, which is how you verify research actually happened."""
    # Generated from the model so the prompt contract and parser cannot drift.
    schema = VideoScript.model_json_schema()
    envelope = _run_claude(_build_prompt(repo, cfg), schema, cfg)

    payload = envelope.get("structured_output")
    if payload is None:
        raise ScriptGenerationError(
            "Claude Code returned no structured_output. This usually means the "
            "installed CLI predates --json-schema support; check `claude --version`."
        )

    script = VideoScript.model_validate(payload)

    if script.word_count > cfg.max_script_words:
        # Worth surfacing but not worth failing over -- the TTS step reports
        # real duration anyway, and a slight overrun is usually fine.
        log.warning(
            "Script is %d words (budget %d); the video may run long.",
            script.word_count, cfg.max_script_words,
        )

    searches = (
        envelope.get("usage", {}).get("server_tool_use", {}).get("web_search_requests", 0)
    )
    log.info(
        "Script ready: %d words, %d cues, %d web searches, $%.4f",
        script.word_count, len(script.visual_cues), searches,
        envelope.get("total_cost_usd", 0.0),
    )
    return script, envelope
