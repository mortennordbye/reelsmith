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

from config import Settings
from pipeline import tts


def _cfg(**kw) -> Settings:
    return Settings(github_token="x", **kw)


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

    def test_worker_and_interpreter_exist(self):
        cfg = _cfg()
        assert cfg.chatterbox_worker.exists(), "tools/chatterbox/synth.py is missing"
        assert cfg.chatterbox_python.exists(), (
            "tools/chatterbox/.venv is missing; rebuild it or set TTS_BACKEND=kokoro"
        )

    def test_worker_normalises_below_clipping(self):
        # Chatterbox renders hot enough to clip. The constant lives in the
        # worker, which this interpreter cannot import, so assert on the source.
        src = Path(_cfg().chatterbox_worker).read_text()
        assert "TARGET_PEAK = 0.707" in src
