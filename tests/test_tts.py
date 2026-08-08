"""Backend selection and the chatterbox subprocess contract.

Nothing here synthesises anything. Loading Chatterbox costs 9 seconds and a
render costs 35 more, which does not belong in a test run. What is worth
pinning is the wiring around it, because every failure mode found while
building it was a wiring failure rather than a model one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import config as config_mod
from config import Settings
from pipeline import tts


def _cfg(**kw) -> Settings:
    return Settings(github_token="x", **kw)


# The chatterbox venv is a ~3 GB local build carrying torch, and it is
# gitignored, so it exists on a machine that has run the voice setup and nowhere
# else. Anything that has to construct the backend needs it, because
# `get_backend` checks for the interpreter before it does anything else.
#
# Skipping rather than building it in CI is the deliberate choice: this suite is
# pure logic by design, and a 3 GB download to reach four assertions would be
# the slowest possible way to learn nothing new. The half of the contract that
# git can actually protect, `synth.py` existing at the path the pipeline shells
# out to, is asserted unconditionally below.
requires_chatterbox_venv = pytest.mark.skipif(
    not _cfg().chatterbox_python.exists(),
    reason="the chatterbox venv is a local 3 GB build and is absent here",
)


class TestBackendSelection:
    def test_each_name_maps_to_its_backend(self):
        assert isinstance(tts.get_backend(_cfg(), "edge"), tts.EdgeTTSBackend)
        assert isinstance(tts.get_backend(_cfg(), "kokoro"), tts.KokoroBackend)
        assert isinstance(tts.get_backend(_cfg(), "chatterbox"), tts.ChatterboxBackend)

    def test_unknown_backend_is_rejected(self):
        with pytest.raises(tts.TTSError, match="chatterbox"):
            tts.get_backend(_cfg(), "elevenlabs")

    def test_config_rejects_a_typo_at_startup(self):
        # Better to fail constructing Settings than three stages into a run.
        with pytest.raises(ValidationError):
            _cfg(tts_backend="chaterbox")


class TestTorchDevice:
    """Only Apple silicon has Metal, and asking for it elsewhere does not
    degrade to CPU, it raises `Storage device not recognized: mps` at model
    load, three stages into a run that has already paid for a script."""

    def test_apple_silicon_gets_metal(self, monkeypatch):
        monkeypatch.setattr(config_mod.sys, "platform", "darwin")
        assert config_mod._default_torch_device() == "mps"

    def test_every_other_platform_gets_cpu(self, monkeypatch):
        monkeypatch.setattr(config_mod.sys, "platform", "linux")
        assert config_mod._default_torch_device() == "cpu"

    def test_an_explicit_setting_still_wins(self):
        assert _cfg(chatterbox_device="cuda").chatterbox_device == "cuda"


class TestAudioSuffix:
    def test_only_edge_emits_mp3(self):
        assert tts.audio_suffix(_cfg(), "edge") == ".mp3"
        assert tts.audio_suffix(_cfg(), "kokoro") == ".wav"
        assert tts.audio_suffix(_cfg(), "chatterbox") == ".wav"

    def test_suffix_follows_the_configured_backend(self):
        # The renderer names the file from this, so a mismatch here means
        # Remotion is handed a path that does not exist.
        assert tts.audio_suffix(_cfg(tts_backend="chatterbox")) == ".wav"
        assert tts.audio_suffix(_cfg(tts_backend="edge")) == ".mp3"


class TestVoiceName:
    def test_clone_is_named_after_its_reference(self):
        assert tts.voice_name(_cfg(), "chatterbox").startswith("clone-")
        assert tts.voice_name(_cfg(), "kokoro") == _cfg().kokoro_voice

    def test_name_is_safe_in_a_filename(self):
        # --preview-voice interpolates this straight into a path.
        name = tts.voice_name(_cfg(), "chatterbox")
        assert not (set(name) & set(':/\\'))


@requires_chatterbox_venv
class TestChatterboxGuards:
    def test_missing_reference_names_the_recording_instructions(self, tmp_path):
        cfg = _cfg(chatterbox_ref=tmp_path / "nope.wav")
        with pytest.raises(tts.TTSError, match="RECORD-THIS"):
            tts.get_backend(cfg, "chatterbox").synthesize("hi", tmp_path / "o.wav")

    def test_worker_error_surfaces_across_the_process_boundary(self, tmp_path, monkeypatch):
        """The worker reports failure as JSON on stdout, not via exit code."""

        class FakeProc:
            stdout = json.dumps({"ok": False, "error": "boom"})
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc())
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"not really audio")
        cfg = _cfg(chatterbox_ref=ref)
        with pytest.raises(tts.TTSError, match="boom"):
            tts.get_backend(cfg, "chatterbox").synthesize("hi", tmp_path / "o.wav")

    def test_result_is_read_past_torch_warning_noise(self, tmp_path, monkeypatch):
        """torch and huggingface print to stdout before the worker's JSON does,
        so the parser has to scan back for the last decodable line."""

        class FakeProc:
            stdout = (
                "FutureWarning: something is deprecated\n"
                "loaded PerthNet (Implicit) at step 250,000\n"
                + json.dumps({"ok": True, "path": "x", "seconds": 25.0, "sample_rate": 24000})
            )
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc())
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"not really audio")
        out = tmp_path / "o.wav"
        cfg = _cfg(chatterbox_ref=ref)
        assert tts.get_backend(cfg, "chatterbox").synthesize("hi", out) == out


class TestWorkerContract:
    """The worker is invoked by path from a different interpreter, so a rename
    or a moved venv breaks the pipeline with no import error to warn about."""

    def test_worker_exists_at_the_path_the_pipeline_shells_out_to(self):
        # Tracked in git, so this one is a real contract: a rename breaks the
        # pipeline at runtime with no import error to warn about.
        assert _cfg().chatterbox_worker.exists(), "tools/chatterbox/synth.py is missing"

    @requires_chatterbox_venv
    def test_interpreter_exists(self):
        assert _cfg().chatterbox_python.exists()

    def test_worker_normalises_below_clipping(self):
        # Chatterbox renders hot enough to clip. The constant lives in the
        # worker, which this interpreter cannot import, so assert on the source.
        src = Path(_cfg().chatterbox_worker).read_text()
        assert "TARGET_PEAK = 0.707" in src
