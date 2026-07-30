"""Step 3 -- synthesize the voiceover.

Default backend is edge-tts: free, keyless, and the neural voices are close to
indistinguishable from a human read. Be aware it is *not* local -- it calls
Microsoft's Edge read-aloud endpoint. Install the `offline-tts` extra and set
TTS_BACKEND=kokoro for a fully on-device alternative.

Both backends satisfy the same protocol, so the rest of the pipeline neither
knows nor cares which one produced the file.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from config import Settings

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


class TTSBackend(Protocol):
    def synthesize(self, text: str, out_path: Path) -> Path: ...


class EdgeTTSBackend:
    """Microsoft Edge neural voices. Free, no API key, network required."""

    def __init__(self, voice: str, rate: str = "+0%"):
        self.voice = voice
        self.rate = rate

    def synthesize(self, text: str, out_path: Path) -> Path:
        import edge_tts

        async def _run() -> None:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            await communicate.save(str(out_path))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            asyncio.run(_run())
        except Exception as exc:  # edge_tts raises a variety of network errors
            raise TTSError(
                f"edge-tts failed ({exc}). It needs network access; for a fully "
                f"offline run install the offline-tts extra and set TTS_BACKEND=kokoro."
            ) from exc

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise TTSError(f"edge-tts produced no audio at {out_path}")
        return out_path


_KOKORO_CACHE: dict[str, object] = {}

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)


class KokoroBackend:
    """Fully local, Apache-2.0, 54 voices.

    The reason to prefer this over edge-tts is not quality -- they are close --
    but recognisability. Edge's only natural English voices are Andrew, Brian,
    Ava and Emma, which are the default in every AI video tool on the market,
    so viewers have heard them a thousand times. Kokoro's voices have not been
    worn out.
    """

    def __init__(self, cfg: Settings):
        self.cfg = cfg

    def _load(self):
        key = f"{self.cfg.kokoro_model_path}"
        if key in _KOKORO_CACHE:
            return _KOKORO_CACHE[key]

        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise TTSError(
                "The kokoro backend needs: uv pip install kokoro-onnx soundfile"
            ) from exc

        model, voices = self.cfg.kokoro_model_path, self.cfg.kokoro_voices_path
        if not model.exists() or not voices.exists():
            raise TTSError(
                f"Kokoro model files are missing from {self.cfg.models_dir}.\n"
                f"Download them once (~350 MB total):\n"
                f"  curl -L -o {model} {KOKORO_MODEL_URL}\n"
                f"  curl -L -o {voices} {KOKORO_VOICES_URL}\n"
                f"Or set TTS_BACKEND=edge in .env to use Microsoft's voices instead."
            )

        log.info("Loading Kokoro model (first call only)")
        _KOKORO_CACHE[key] = Kokoro(str(model), str(voices))
        return _KOKORO_CACHE[key]

    def synthesize(self, text: str, out_path: Path) -> Path:
        try:
            import soundfile as sf
        except ImportError as exc:
            raise TTSError("The kokoro backend needs: uv pip install soundfile") from exc

        kokoro = self._load()
        samples, sample_rate = kokoro.create(
            text,
            voice=self.cfg.kokoro_voice,
            speed=self.cfg.kokoro_speed,
            lang=self.cfg.kokoro_lang,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Kokoro emits float samples; write WAV and let the renderer mux it.
        # (mp3 would need an extra encoder dependency for no benefit here.)
        sf.write(str(out_path), samples, sample_rate)
        return out_path


def get_backend(cfg: Settings, name: str | None = None) -> TTSBackend:
    name = name or cfg.tts_backend
    if name == "edge":
        return EdgeTTSBackend(cfg.tts_voice, cfg.tts_rate)
    if name == "kokoro":
        return KokoroBackend(cfg)
    raise TTSError(f"Unknown TTS backend {name!r}; expected 'edge' or 'kokoro'")


def voice_name(cfg: Settings, backend: str | None = None) -> str:
    backend = backend or cfg.tts_backend
    return cfg.kokoro_voice if backend == "kokoro" else cfg.tts_voice


def audio_suffix(cfg: Settings, backend: str | None = None) -> str:
    """edge-tts emits mp3; Kokoro emits raw samples we write as wav. Remotion
    and faster-whisper both read either, so the extension just has to match
    what was actually written."""
    return ".wav" if (backend or cfg.tts_backend) == "kokoro" else ".mp3"


def synthesize(text: str, out_path: Path, cfg: Settings, backend: str | None = None) -> Path:
    backend = backend or cfg.tts_backend
    log.info("Synthesizing voiceover with %s (%s)", backend, voice_name(cfg, backend))
    path = get_backend(cfg, backend).synthesize(text, out_path)
    log.info("Wrote %s (%.1f KB)", path.name, path.stat().st_size / 1024)
    return path


def audio_duration_seconds(path: Path) -> float:
    """Read the real duration of the rendered audio.

    The whole video timeline is derived from this rather than from an estimate,
    which is what guarantees the audio and visuals cannot drift apart.
    """
    import av

    with av.open(str(path)) as container:
        if container.duration:
            return container.duration / 1_000_000  # AV_TIME_BASE microseconds
        stream = container.streams.audio[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
    raise TTSError(f"Could not determine duration of {path}")
