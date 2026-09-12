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


def test_an_episode_renders_at_the_configured_crf(tmp_path, monkeypatch):
    """`--crf=24` was hardcoded while `EPISODE_CRF` defaulted to 28 and was
    read by nothing. 24 is the value CLAUDE.md records as the trap: 58 MB at
    47 seconds, inside ten percent of TikTok's single chunk cap."""
    from types import SimpleNamespace

    from config import Settings

    monkeypatch.setattr(Settings, "video_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(renderer, "_ensure_node_deps", lambda _dir: None)
    out = tmp_path / "out.mp4"
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        out.write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    spec = SimpleNamespace(slug="s", durationInFrames=30, model_dump_json=lambda: "{}")

    renderer.render_episode(spec, out, Settings(episode_crf=31, _env_file=None))
    renderer.render_episode(spec, out, Settings(_env_file=None))

    assert "--crf=31" in seen[0]
    assert "--crf=28" in seen[1]


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


# --- Staging ----------------------------------------------------------------


def test_the_tall_page_capture_is_pruned_like_every_other_staged_asset():
    """public/ is a staging area and this sweep is the only thing that empties it.

    The slug pattern is greedy over hyphens, so a rule written for `repo.png`
    does not cover `repo-page.png`, and an asset the sweep does not recognise is
    one that is never deleted. The tall capture is the largest file the pipeline
    stages, so accumulating one per run is the worst version of that to miss.
    """
    assert renderer.STAGED_ASSET_RE.fullmatch("a-repo-page.png")
    assert renderer.STAGED_ASSET_RE.fullmatch("a-repo.png")
    assert renderer.STAGED_ASSET_RE.fullmatch("a-voice.wav")
    assert not renderer.STAGED_ASSET_RE.fullmatch("cv-tree.png")


DAY = renderer.STAGING_TTL_S


def _staged_run(video_dir, account, slug, *, age_s=0):
    """A run directory as `stage_asset` leaves it, aged by its mtime."""
    import os

    source = video_dir / f"{slug}.wav"
    source.write_bytes(b"x")
    key = renderer.staging_key(account, slug)
    renderer.stage_asset(source, video_dir, key)
    run = video_dir / "public" / key
    if age_s:
        stamp = run.stat().st_mtime - age_s
        os.utime(run, (stamp, stamp))
    return key


def test_a_run_stages_into_a_directory_of_its_own(tmp_path):
    """Two accounts rendering the same slug no longer share a filename."""
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")

    one = renderer.stage_asset(source, tmp_path, renderer.staging_key("a", "same-slug"))
    two = renderer.stage_asset(source, tmp_path, renderer.staging_key("b", "same-slug"))

    assert one == "staged/a/same-slug/voice.wav"
    assert two == "staged/b/same-slug/voice.wav"
    assert (tmp_path / "public" / one).read_bytes() == b"audio"


def test_a_run_in_flight_elsewhere_is_never_pruned(tmp_path):
    """Remotion symlinks public/ into its bundle, so deleting another run's
    files mid render takes them out from under it. Young means in flight."""
    theirs = _staged_run(tmp_path, "b", "theirs", age_s=60)
    mine = _staged_run(tmp_path, "a", "mine")

    assert renderer.prune_staged_assets(tmp_path, mine) == 0
    assert (tmp_path / "public" / theirs).is_dir()


def test_a_dead_runs_directory_is_pruned_once_a_day_old(tmp_path):
    dead = _staged_run(tmp_path, "b", "dead", age_s=DAY + 60)
    mine = _staged_run(tmp_path, "a", "mine", age_s=DAY + 60)

    assert renderer.prune_staged_assets(tmp_path, mine) == 1
    assert not (tmp_path / "public" / dead).exists()
    assert (tmp_path / "public" / mine).is_dir()


def test_old_flat_files_are_pruned_but_never_the_prototypes_or_strangers(tmp_path):
    """The layout before per run staging left `<slug>-voice.wav` at the root.
    Those go once they are old; the `cv-` scans and anything a person put
    there stay."""
    import os
    import time

    public = tmp_path / "public"
    public.mkdir()
    for name in ("old-repo-voice.wav", "old-repo-repo-page.png", "cv-voice-art1.jpg",
                 "unrelated.txt", "fresh-voice.wav"):
        (public / name).write_bytes(b"x")
    old = time.time() - DAY - 60
    for name in ("old-repo-voice.wav", "old-repo-repo-page.png", "cv-voice-art1.jpg",
                 "unrelated.txt"):
        os.utime(public / name, (old, old))

    removed = renderer.prune_staged_assets(tmp_path, renderer.staging_key("a", "x"))

    assert removed == 2
    assert {p.name for p in public.iterdir()} == {
        "cv-voice-art1.jpg", "unrelated.txt", "fresh-voice.wav",
    }


def test_releasing_deletes_only_this_runs_directory(tmp_path):
    mine = _staged_run(tmp_path, "a", "mine")
    theirs = _staged_run(tmp_path, "a", "theirs")

    renderer.release_staged(tmp_path, mine)

    assert not (tmp_path / "public" / mine).exists()
    assert (tmp_path / "public" / theirs).is_dir()
