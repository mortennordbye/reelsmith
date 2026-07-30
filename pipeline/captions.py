"""Step 4 -- word-level caption timings.

Uses faster-whisper (CTranslate2) rather than openai-whisper, which would pull
in PyTorch for ~2.5 GB. Same Whisper models, word timestamps built in, and it
ships PyAV so audio decoding works without a system ffmpeg.

We transcribe the synthesized audio rather than trusting the script text,
because that is what makes the captions land on the actual spoken syllables.
The script is still used as an `initial_prompt` so Whisper spells project
names and technical terms the way the script does.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import Settings
from pipeline.models import Caption

log = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, object] = {}


class CaptionError(RuntimeError):
    pass


def _load_model(cfg: Settings):
    """Load and cache the Whisper model. Loading is slow; reuse it."""
    key = f"{cfg.whisper_model}:{cfg.whisper_compute_type}"
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel

        log.info("Loading Whisper model %s (first run downloads it)", cfg.whisper_model)
        # int8 on CPU is the right default on Apple Silicon: CTranslate2 has no
        # Metal backend, and int8 is roughly 3x faster than float32 with no
        # audible accuracy loss on clean TTS audio.
        _MODEL_CACHE[key] = WhisperModel(
            cfg.whisper_model, device="cpu", compute_type=cfg.whisper_compute_type
        )
    return _MODEL_CACHE[key]


def transcribe(audio_path: Path, cfg: Settings, script_hint: str = "") -> list[Caption]:
    """Return one Caption per spoken word, in order."""
    model = _load_model(cfg)

    segments, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language="en",
        # Biases spelling toward the script's vocabulary, so "uv" doesn't come
        # back as "you vee" and "Ruff" doesn't become "rough".
        initial_prompt=script_hint[:900] or None,
        vad_filter=True,
        beam_size=5,
    )

    captions: list[Caption] = []
    for segment in segments:
        for word in segment.words or []:
            text = word.word.strip()
            if not text:
                continue
            start_ms = word.start * 1000.0
            end_ms = word.end * 1000.0
            captions.append(
                Caption(
                    text=text,
                    startMs=round(start_ms, 1),
                    endMs=round(end_ms, 1),
                    timestampMs=round((start_ms + end_ms) / 2, 1),
                    confidence=round(word.probability, 4) if word.probability else None,
                )
            )

    if not captions:
        raise CaptionError(
            f"Whisper found no words in {audio_path}. Check the audio actually "
            f"contains speech."
        )

    log.info(
        "Transcribed %d words over %.1fs (detected language %s)",
        len(captions), captions[-1].endMs / 1000, info.language,
    )
    return _repair_gaps(captions)


def _repair_gaps(captions: list[Caption], max_gap_ms: float = 400.0) -> list[Caption]:
    """Close small gaps between consecutive words.

    Whisper emits tight per-word bounds with silence between them. Rendering
    those verbatim makes captions flicker off between words. Extending each
    word to meet the next one -- but only across short gaps, so real pauses
    still read as pauses -- gives a much calmer result.
    """
    for current, following in zip(captions, captions[1:], strict=False):
        gap = following.startMs - current.endMs
        if 0 < gap <= max_gap_ms:
            current.endMs = following.startMs
    return captions
