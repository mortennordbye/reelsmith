"""The Claude CLI wrapper, on the failures that cost five nights.

From 2026-10-01 the render host's `claude` had no usable sign in. Every call
exited 1 with "OAuth session expired" in the envelope's `result`, the message
cut that field off, each repo was retried three times and then skipped, and
the night reported "ok, 0 rendered". These pin the three things that hid it.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from config import Settings
from pipeline import claude as claude_cli
from pipeline import scriptwriter

_EXPIRED = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "duration_api_ms": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "padding": "x" * 500},
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
})


@pytest.fixture
def cfg() -> Settings:
    return Settings(account="testaccount", _env_file=None)


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(cmd, kwargs.get("_rc", 1), stdout=_EXPIRED, stderr="")

    monkeypatch.setattr(claude_cli, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return seen


def test_an_expired_sign_in_is_named_not_truncated(cfg, calls):
    with pytest.raises(claude_cli.ClaudeAuthError, match="OAuth session expired"):
        claude_cli.run("p", {}, cfg, system="s")


def test_any_other_exit_stays_transient_and_says_why(cfg, monkeypatch):
    body = json.dumps({"is_error": True, "result": "Overloaded", "usage": {"x": "y" * 500}})
    monkeypatch.setattr(claude_cli, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=body, stderr=""),
    )
    with pytest.raises(claude_cli.TransientClaudeError, match="Overloaded"):
        claude_cli.run("p", {}, cfg, system="s")


def test_the_pipeline_token_reaches_the_cli_and_nothing_else_changes(cfg, calls):
    with pytest.raises(claude_cli.ClaudeAuthError):
        claude_cli.run("p", {}, cfg, system="s")
    assert calls[-1]["env"] is None

    signed = cfg.model_copy(update={"claude_code_oauth_token": "sk-ant-oat01-test"})
    with pytest.raises(claude_cli.ClaudeAuthError):
        claude_cli.run("p", {}, signed, system="s")
    assert calls[-1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert "PATH" in calls[-1]["env"]


def test_a_script_does_not_retry_a_missing_sign_in(cfg, calls, monkeypatch):
    monkeypatch.setattr(scriptwriter.time, "sleep", lambda s: None)
    with pytest.raises(claude_cli.ClaudeAuthError):
        scriptwriter._run_claude_with_retry("p", {}, cfg)
    assert len(calls) == 1
