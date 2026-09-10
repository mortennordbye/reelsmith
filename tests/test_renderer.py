"""The Remotion dependency check.

This sits at stage 5 of 5, which is what makes it worth its own file. By the
time it runs the pipeline has already paid for discovery, a Claude script, a
Chatterbox voiceover and a Whisper alignment, so refusing here throws all of
that away. The render host lost `video/node_modules` on 2026-09-07 and spent
three nights doing exactly that, silently, because a host that queues no rows
looks the same as one where `--max-queue` stopped the batch.

So it installs rather than refusing, and what is asserted is when it shells out,
what it shells out to, and that a genuine failure still raises.
"""

from __future__ import annotations

import subprocess

import pytest

from pipeline import renderer
from pipeline.renderer import RenderError


@pytest.fixture
def video_dir(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    return tmp_path


def _install_marker(video_dir) -> None:
    binaries = video_dir / "node_modules" / ".bin"
    binaries.mkdir(parents=True, exist_ok=True)
    (binaries / "remotion").write_text("#!/bin/sh\n")


@pytest.fixture
def npm(monkeypatch):
    """Record npm invocations. Installs nothing unless a test says so."""
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        effect = getattr(fake_run, "effect", None)
        if effect is not None:
            effect(kwargs["cwd"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    fake_run.runs = runs
    return fake_run


def test_installed_deps_are_left_alone(video_dir, npm):
    """The common path must not shell out on every render."""
    _install_marker(video_dir)
    renderer._ensure_node_deps(video_dir)
    assert npm.runs == []


def test_missing_deps_are_installed(video_dir, npm):
    (video_dir / "package-lock.json").write_text("{}")
    npm.effect = _install_marker

    renderer._ensure_node_deps(video_dir)

    assert npm.runs == [["npm", "ci"]]


def test_a_half_installed_tree_still_counts_as_missing(video_dir, npm):
    """An interrupted install leaves node_modules without the binary.

    Testing the directory rather than the binary is what makes that state read
    as installed, which is the shape `pod-setup.sh --check` already avoids.
    """
    (video_dir / "node_modules").mkdir()
    (video_dir / "package-lock.json").write_text("{}")
    npm.effect = _install_marker

    renderer._ensure_node_deps(video_dir)

    assert npm.runs == [["npm", "ci"]]


def test_no_lockfile_falls_back_to_install(video_dir, npm):
    """`npm ci` needs a lockfile; without one there is nothing to reproduce."""
    npm.effect = _install_marker
    renderer._ensure_node_deps(video_dir)
    assert npm.runs == [["npm", "install"]]


def test_a_failed_install_raises(video_dir, monkeypatch):
    (video_dir / "package-lock.json").write_text("{}")

    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, "", "ENOSPC: no space left")

    monkeypatch.setattr(renderer.subprocess, "run", boom)

    with pytest.raises(RenderError, match="ENOSPC"):
        renderer._ensure_node_deps(video_dir)


def test_a_missing_npm_raises(video_dir, monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(renderer.subprocess, "run", missing)

    with pytest.raises(RenderError, match="npm is not installed"):
        renderer._ensure_node_deps(video_dir)


def test_an_install_that_changes_nothing_raises(video_dir, npm):
    """npm can exit 0 and leave the tree unusable. Do not render into that."""
    (video_dir / "package-lock.json").write_text("{}")

    with pytest.raises(RenderError, match="still not installed"):
        renderer._ensure_node_deps(video_dir)
