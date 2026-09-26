"""The README capture installs its own chromium when the pinned build is missing.

The render host's image moved its playwright browsers to a build the pinned
playwright does not ask for, and every capture failed quietly for eleven
nights. These pin the retry, not the download.
"""

from __future__ import annotations

import subprocess

import pytest

from pipeline import screenshot


class FakeChromium:
    def __init__(self, failures: list[Exception]):
        self.failures = failures
        self.launches = 0

    def launch(self):
        self.launches += 1
        if self.failures:
            raise self.failures.pop(0)
        return "browser"


class FakePlaywright:
    def __init__(self, failures):
        self.chromium = FakeChromium(failures)


MISSING = RuntimeError(
    "BrowserType.launch: Executable doesn't exist at /opt/ms-playwright/chromium-1234"
)


def test_a_missing_browser_is_installed_and_launched(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    p = FakePlaywright([MISSING])
    assert screenshot._launch(p) == "browser"
    assert p.chromium.launches == 2
    assert ran and ran[0][-3:] == ["playwright", "install", "chromium"]


def test_any_other_launch_failure_is_not_an_install(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    with pytest.raises(RuntimeError, match="sandbox"):
        screenshot._launch(FakePlaywright([RuntimeError("no sandbox")]))
    assert ran == []


def test_a_failed_install_keeps_the_original_error(monkeypatch):
    def refuse(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="offline")

    monkeypatch.setattr(subprocess, "run", refuse)
    p = FakePlaywright([MISSING])
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        screenshot._launch(p)
    assert p.chromium.launches == 1
