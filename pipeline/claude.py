"""One place that runs the Claude CLI, because two would drift.

`pipeline/scriptwriter.py` shelled out to `claude -p` with a JSON schema and an
appended system prompt, and it was the only caller for as long as the only
subject was a repository. Subject discovery for a second niche is the second
caller, and it needs the same invocation with a different system prompt and a
different schema, so the invocation moved here rather than being copied.

The trade this makes is the same one `pipeline/tts.py` makes with the
chatterbox subprocess: a CLI rather than an API call, because the CLI is what
holds the session, the model selection and the web search that makes the answer
better than a paraphrase.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from config import Settings, resolve_claude_cli

log = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    """The CLI ran and did not produce a usable answer."""


class TransientClaudeError(ClaudeError):
    """A failure worth simply trying again, with nothing to correct."""


def run(
    prompt: str, schema: dict[str, Any], cfg: Settings, *, system: str, research: bool | None = None
) -> dict[str, Any]:
    """Invoke the CLI and return its envelope. Raises on anything else.

    `research` overrides `cfg.claude_research` for a caller whose question
    cannot be answered without the web at all. Discovery is one of those: a
    catalogue lookup can be checked afterwards, but a proposal has to come from
    somewhere.
    """
    cmd = [
        resolve_claude_cli(),
        "-p", prompt,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
        "--model", cfg.claude_model,
        "--effort", cfg.claude_effort,
        "--append-system-prompt", system,
    ]
    wants_research = cfg.claude_research if research is None else research
    # Research is what makes this better than a plain API call: Claude Code can
    # look the project up rather than paraphrasing its README.
    cmd += ["--allowedTools", "WebSearch WebFetch"] if wants_research else ["--allowedTools", ""]

    log.info("Invoking Claude Code (model=%s, research=%s)", cfg.claude_model, wants_research)
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, capture_output=True, text=True, timeout=cfg.claude_timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(
            f"Claude Code did not finish within {cfg.claude_timeout_s}s. "
            f"Raise CLAUDE_TIMEOUT_S, or set CLAUDE_RESEARCH=false to skip web search."
        ) from exc

    if proc.returncode != 0:
        # stdout, not just stderr. With --output-format json the CLI puts its
        # error envelope on stdout, so reporting stderr alone produced a blank
        # message and the real reason had to be dug out of ~/.claude/projects.
        raise TransientClaudeError(
            f"claude exited {proc.returncode}.\n"
            f"stderr: {proc.stderr[:400]}\n"
            f"stdout: {proc.stdout[:400]}\n"
            f"If this says you are not authenticated, run `claude` once interactively "
            f"to sign in."
        )

    try:
        envelope: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(
            f"Could not parse Claude Code output as JSON: {proc.stdout[:500]}"
        ) from exc

    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise ClaudeError(
            f"Claude Code reported failure "
            f"(subtype={envelope.get('subtype')}): {str(envelope.get('result'))[:500]}"
        )
    return envelope


def payload(envelope: dict[str, Any]) -> Any:
    """The answer inside the envelope, parsed.

    The CLI puts the model's JSON in `result` as a string, so every caller
    would otherwise repeat the same two lines and the same failure mode.
    """
    result = envelope.get("result")
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise ClaudeError(f"The answer was not JSON: {result[:400]}") from exc
    return result
