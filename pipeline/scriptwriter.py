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
import time
from typing import Any

from pydantic import ValidationError

from config import Settings, resolve_claude_cli
from pipeline.models import RepoCandidate, VideoScript

log = logging.getLogger(__name__)

# One generation plus two corrections. Past that the model is not going to get
# there and the run should fail loudly rather than keep spending.
_MAX_SCRIPT_ATTEMPTS = 3
# The CLI dying is usually the network rather than the request. Retried with a
# short backoff, because the alternative is what happened on 2026-08-01: a
# dropped connection five minutes into the daily run threw the whole thing
# away, and the day produced no video at all.
_MAX_CLI_ATTEMPTS = 3
_CLI_BACKOFF_S = 20


class ScriptGenerationError(RuntimeError):
    """Claude Code failed to produce a usable script."""


class TransientScriptError(ScriptGenerationError):
    """The CLI failed in a way that another attempt might survive.

    A non-zero exit is nearly always the connection rather than the prompt: a
    bad prompt comes back as a valid envelope carrying a bad answer, which the
    validator handles separately. So this is retried and a validation failure
    is not.
    """


SYSTEM_PROMPT = """\
You write scripts for short-form vertical videos aimed at working software \
engineers, IT consultants, and AI practitioners. Your audience is technical: \
they can read code, they know what an API is, and they resent being talked \
down to.

But technical is not the same as already familiar. Assume the viewer has never \
heard of this specific project, does not work in its ecosystem, and does not \
know its jargon. A backend engineer scrolling past a frontend tool should still \
understand what it is for. So: expand every acronym and term of art the first \
time you use it, in the sentence itself rather than as an aside. Never put an \
unexplained acronym in the hook. Write so that someone outside the niche \
follows it and someone inside it does not feel patronised. Those are the same \
script when you lead with the problem instead of the vocabulary.

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

    It must be understandable to someone who has never heard of this project or
    its ecosystem. No unexplained acronyms or terms of art. "92k stars for a
    rewrite of YAGNI" fails, because a viewer who does not know the acronym is
    told nothing at all.

    No colons and no hyphens or dashes anywhere in the hook. This is validated
    and a violation fails the run. Rewrite around them: "92k stars" not
    "92k-star", "seven words" not "seven-word", and split a colon into two
    sentences or drop it. Hyphenated compounds almost always have a plain
    equivalent that reads better on screen.

spoken_script
    The voiceover, UNDER {cfg.max_script_words} WORDS. This is a hard limit: at
    normal speaking pace it becomes roughly 30-45 seconds of audio, and going
    over means the video runs long. Structure it as: what it is -> the specific
    problem it solves -> one concrete detail a developer would care about ->
    what to do next. Write for the ear. No semicolons, no parentheses, and
    expand symbols ("about 20 percent", not "~20%").

    **Never restate the hook.** The hook is already on screen, read in under a
    second, and the viewer is still there because of it. Saying it again in the
    first spoken sentence spends the three seconds that decide whether they stay
    on an idea they already have. The first line must move to the NEXT beat: the
    consequence, the cost, the number. If the hook is "Your agent codes before
    it understands you", open on "Twenty minutes later you delete all of it",
    not on "Your coding agent starts writing before it understands the ask".

    Open on the PROBLEM, not on the project. The first two sentences must
    describe a frustration the viewer has actually felt, in plain language,
    before the project is named. "Your coding agent builds a custom date picker
    when the browser already has one" earns attention. "Ponytail is a skill
    that applies a decision ladder" does not, because nobody cares what a thing
    is until they know why it exists.

    Then answer, in this order and explicitly: what does it change, why would I
    use it, and who is it for. A viewer should be able to say "I would use this
    when X" after watching. Naming a feature is not the same as explaining what
    it solves.

    Do not assume familiarity with the project's ecosystem or vocabulary. If a
    term like YAGNI, RAG or MCP is load bearing, say what it means in the same
    breath ("the rule that you should not build what you do not yet need")
    rather than using it bare.

    No colons and no hyphens or dashes anywhere in the spoken script either.
    This is validated and a violation fails the run. They are invisible to a
    listener, and the captions burned onto the video are generated from this
    text, so a hyphen that helps nobody aloud still clutters the screen.

    This is read aloud by a synthetic voice, which flattens whatever prosody
    your punctuation implies. It cannot rescue a limp sentence, so the momentum
    has to be built into the words. Apply the energy rules above hard here --
    short sentences, varied length, verb-first openings. If two consecutive
    sentences have a similar shape, rewrite one.

visual_cues
    5 to 8 ordered beats describing what is on screen. Each cue's
    spoken_excerpt must be the portion of spoken_script playing during that
    beat -- concatenated in order they should reconstruct the whole script,
    because the renderer uses them to allocate screen time. Available kinds:

      repo_card  title=repo name, subtitle=one-line description
      code       code=a SHORT snippet (max 12 lines, max 60 chars per line --
                 it must be legible on a phone), code_language=the language
      terminal   code=the tool being run, code_language="bash"
      stat       stat_value=a short number/figure, stat_label=what it measures
      bullets    bullets=2 to 4 lines, each 6 words or fewer

    **Every word of spoken_script is burned onto the video as captions**, synced
    to the voice, along the bottom of the frame. The viewer is already reading
    what they are hearing. So any `title`, `bullets` or `stat_label` that
    restates the narration puts the same words on screen twice, and the screen
    is the scarcest thing in the video.

    Cue text must add something the sentence being spoken does not:

      say "slash grill me interviews you first"   show the command itself
      say "it drags on interface work"            show a labelled bullet, "interface work"
      say "small markdown files"                  show the file tree, or a snippet
      say "built for solo devs"                   show nothing, or the repo card

    `title` is a label, not a sentence, and it is optional. Two or three words
    naming what the viewer is looking at. Leave it out rather than paraphrase
    the voiceover into it. `bullets` are keywords and fragments, never the
    script's own sentences rewritten.

    When a beat has nothing to show that the words do not already carry, prefer
    `code`, `terminal`, `repo_card` or `stat`, which show a thing rather than
    describe one.

    Keep each spoken_excerpt to about 15 words. A cue's screen time comes from
    how long its excerpt takes to say, so a 40 word excerpt becomes a ten second
    static hold, and a static hold is where a viewer scrolls. More cues with
    shorter excerpts is the same script with more cuts in it.

    Use at least one `code` or `terminal` cue -- this audience wants to see the
    actual interface. Only cite a `stat` you are confident is real.

    **Never show an install, add or setup command, and never read one aloud.**
    Explain what the project is and how it works; getting hold of it is the
    viewer's next step, not this video's. They can search the name, or comment
    the keyword and the account sends them the link. That is what makes the ask
    worth answering rather than decoration, so a video that hands the install
    line over for free has given away the only thing it had to trade.

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
        # stdout, not just stderr. With --output-format json the CLI puts its
        # error envelope on stdout, so reporting stderr alone produced a blank
        # message and the real reason had to be dug out of ~/.claude/projects.
        raise TransientScriptError(
            f"claude exited {proc.returncode}.\n"
            f"stderr: {proc.stderr[:400]}\n"
            f"stdout: {proc.stdout[:400]}\n"
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


def _run_claude_with_retry(
    prompt: str, schema: dict[str, Any], cfg: Settings
) -> dict[str, Any]:
    """`_run_claude`, but a dropped connection does not cost the day.

    Separate from the validation retry below on purpose. That one hands the
    model its own rejected answer and asks for a fix; this one simply tries the
    same call again, because there is nothing to correct.
    """
    for attempt in range(1, _MAX_CLI_ATTEMPTS + 1):
        try:
            return _run_claude(prompt, schema, cfg)
        except TransientScriptError as exc:
            if attempt == _MAX_CLI_ATTEMPTS:
                raise
            log.warning(
                "Claude Code failed (attempt %d/%d), retrying in %ds: %s",
                attempt, _MAX_CLI_ATTEMPTS, _CLI_BACKOFF_S, str(exc).splitlines()[0],
            )
            time.sleep(_CLI_BACKOFF_S)
    raise AssertionError("unreachable")  # pragma: no cover


def write_script(repo: RepoCandidate, cfg: Settings) -> tuple[VideoScript, dict[str, Any]]:
    """Generate the script. Returns (script, raw_envelope) so callers can
    persist the envelope for auditing -- it carries the web-search count and
    cost, which is how you verify research actually happened."""
    # Generated from the model so the prompt contract and parser cannot drift.
    schema = VideoScript.model_json_schema()
    prompt = _build_prompt(repo, cfg)

    # The constraints the model most often trips on -- hook length, and the ban
    # on colons and dashes -- are pydantic validators, which JSON Schema cannot
    # express, so the CLI cannot enforce them for us. Handing the failure back
    # and asking for a fix costs one extra call; failing the run throws away the
    # whole generation over a hyphen.
    envelope: dict[str, Any] = {}
    script: VideoScript | None = None
    for attempt in range(_MAX_SCRIPT_ATTEMPTS):
        envelope = _run_claude_with_retry(prompt, schema, cfg)

        payload = envelope.get("structured_output")
        if payload is None:
            raise ScriptGenerationError(
                "Claude Code returned no structured_output. This usually means the "
                "installed CLI predates --json-schema support; check `claude --version`."
            )

        try:
            script = VideoScript.model_validate(payload)
            break
        except ValidationError as exc:
            if attempt == _MAX_SCRIPT_ATTEMPTS - 1:
                raise ScriptGenerationError(
                    f"Script still invalid after {_MAX_SCRIPT_ATTEMPTS} attempts: {exc}"
                ) from exc
            log.warning(
                "Script failed validation (attempt %d/%d), asking for a fix: %s",
                attempt + 1, _MAX_SCRIPT_ATTEMPTS,
                "; ".join(e["msg"] for e in exc.errors()),
            )
            prompt = (
                f"{_build_prompt(repo, cfg)}\n\n"
                f"## Your previous answer was rejected\n\n"
                f"{json.dumps(payload, indent=2)}\n\n"
                f"It failed validation:\n\n"
                + "\n".join(
                    f"- {'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                )
                + "\n\nProduce a corrected version. Fix only what was rejected; "
                "keep everything else as close to the original as you can."
            )

    assert script is not None  # loop either breaks with a script or raises

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
