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
import os
import subprocess
from typing import Any

from config import Settings, resolve_claude_cli

log = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    """The CLI ran and did not produce a usable answer."""


class TransientClaudeError(ClaudeError):
    """A failure worth simply trying again, with nothing to correct."""


class ClaudeAuthError(ClaudeError):
    """The CLI is not signed in. Nothing after this can succeed either.

    Not transient, and not something one repo or one subject can be skipped
    past: every later call fails the same way. Callers that catch ClaudeError
    to move on to the next candidate must let this one through, or a night
    with no sign in reads as a night where every candidate happened to fail.
    """


# What the CLI says in `result` when it has no usable login. Matched loosely,
# because the wording has changed between releases and a miss only costs the
# retries this exists to skip.
_AUTH_MARKERS = ("authenticate", "oauth", "not logged in", "/login", "invalid api key")


def _cli_reason(stdout: str) -> str:
    """The error the CLI put in its JSON envelope, or the raw output.

    With --output-format json a failure is an envelope on stdout whose `result`
    holds the reason, and that field sits past the first 400 characters. Cut
    off there, five nights of "OAuth session expired" were logged as a usage
    block with nothing in it.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout[:400]
    if isinstance(envelope, dict) and envelope.get("result"):
        return str(envelope["result"])[:400]
    return stdout[:400]


def _env(cfg: Settings) -> dict[str, str] | None:
    """The CLI's environment: this one, plus the pipeline's own sign in if set."""
    if not cfg.claude_code_oauth_token:
        return None
    return {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": cfg.claude_code_oauth_token}


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
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg.claude_timeout_s,
            check=False,
            env=_env(cfg),
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
        reason = _cli_reason(proc.stdout)
        if any(m in reason.lower() for m in _AUTH_MARKERS):
            raise ClaudeAuthError(
                f"claude is not signed in: {reason}\n"
                f"Set CLAUDE_CODE_OAUTH_TOKEN in this repo's .env to a token from "
                f"`claude setup-token`. Under a Claude Code session, signing that "
                f"session in does not help: its token is withheld from the commands "
                f"it runs, so this CLI reads ~/.claude/.credentials.json instead."
            )
        raise TransientClaudeError(
            f"claude exited {proc.returncode}: {reason}\n"
            f"stderr: {proc.stderr[:400]}"
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
