"""Step 4 -- word-level caption timings.

Uses faster-whisper (CTranslate2) rather than openai-whisper, which would pull
in PyTorch for ~2.5 GB. Same Whisper models, word timestamps built in, and it
ships PyAV so audio decoding works without a system ffmpeg.

We transcribe the synthesized audio rather than trusting the script text,
because that is what makes the captions land on the actual spoken syllables.

A glossary of the script's distinctive terms is passed as `initial_prompt` so
Whisper spells project names the way the script does -- "uv" rather than "you
vee", "Ruff" rather than "rough".

Do NOT put the script's prose in that prompt. Whisper treats `initial_prompt`
as text that *precedes* the audio, so handing it the transcript makes it
believe the opening has already been said and start partway in. It cost the
first nine words of the colibri run, and the loss is silent: you get a shorter
caption list with plausible timings, and only notice when the burned-in words
disagree with the voiceover over the opening seconds. A comma-separated list of
terms does not read as prose, so it biases spelling without triggering that.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
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


_TOKEN = re.compile(r"[A-Za-z0-9]+")

# Capitalised words that are almost always just opening a sentence rather than
# naming anything. Excluded so they do not crowd out real terms. Position is
# not a reliable test on its own: "Colibri wants your disk" starts a sentence
# with the one word in the script that most needs spelling help.
_SENTENCE_OPENERS = frozenset({
    "a", "an", "the", "it", "its", "this", "that", "these", "those", "they",
    "you", "your", "we", "our", "i", "if", "when", "while", "then", "now",
    "here", "there", "and", "but", "so", "or", "no", "not", "every", "each",
    "most", "some", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "first", "next", "last",
})


def glossary(script: str, limit: int = 24) -> str:
    """The script's distinctive terms, as a comma-separated list.

    Distinctive means a term Whisper is likely to render wrongly rather than
    mishear: anything carrying a digit (`744`, `int4`, `5090s`, which also
    biases it toward numerals over spelled-out numbers), anything with an
    interior capital (`GLM`, `MoE`, `NVMe`), and any capitalised word that is
    not a bare sentence opener (`Colibri`, `Python`, `CUDA`).

    Ordinary prose is excluded deliberately. See the module docstring for what
    including it does.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for match in _TOKEN.finditer(script):
        word = match.group()
        lowered = word.lower()
        distinctive = (
            any(c.isdigit() for c in word)
            or any(c.isupper() for c in word[1:])
            or (word[0].isupper() and lowered not in _SENTENCE_OPENERS)
        )
        if distinctive and lowered not in seen:
            seen.add(lowered)
            terms.append(word)
    return ", ".join(terms[:limit])


def _norm(word: str) -> str:
    """Compare on letters and digits only, so 'PRC,' and 'pure' still line up
    positionally even though they do not match."""
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _spread(words: list[str], start_ms: float, end_ms: float) -> list[Caption]:
    """Lay words out evenly across a span. Used where Whisper and the script
    disagree, so there is no per-word timing to inherit."""
    if not words:
        return []
    step = (end_ms - start_ms) / len(words)
    out = []
    for i, word in enumerate(words):
        s = start_ms + i * step
        e = s + step
        out.append(
            Caption(text=word, startMs=round(s, 1), endMs=round(e, 1),
                    timestampMs=round((s + e) / 2, 1), confidence=None)
        )
    return out


def _align_to_script(heard: list[Caption], script: str) -> list[Caption]:
    """Put the script's own words on Whisper's timings.

    Whisper is answering two questions here, what was said and when. We already
    know what was said -- we wrote it and handed it to the TTS -- so letting it
    answer that one only costs us. On the colibri run it turned "pure C" into
    "PRC" and emitted "744 billion -parameter" and "10 -20 seconds", and those
    render verbatim: `Captions.tsx` draws `c.text` with no cleanup, and the
    dashes are exactly what this project bans from burned-in text.

    So the transcription is used purely as an alignment signal. Where the two
    agree, each script word inherits its spoken timing. Where they diverge, the
    script's words are spread across the disputed span, which keeps captions on
    screen at roughly the right moment without inventing per-word precision.

    Returns the transcription unchanged if no script is available, since some
    timing is better than none.
    """
    wanted = script.split()
    if not wanted or not heard:
        return heard

    matcher = SequenceMatcher(
        None, [_norm(c.text) for c in heard], [_norm(w) for w in wanted], autojunk=False
    )

    out: list[Caption] = []
    for tag, h1, h2, w1, w2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, word in enumerate(wanted[w1:w2]):
                src = heard[h1 + offset]
                out.append(src.model_copy(update={"text": word}))
        elif tag == "delete":
            continue  # Whisper heard words the script does not have; drop them.
        else:
            # "replace" or "insert". Take the span the disputed words occupy,
            # falling back to the surrounding words when the script has words
            # Whisper missed entirely.
            if h1 < h2:
                start, end = heard[h1].startMs, heard[h2 - 1].endMs
            else:
                start = heard[h1 - 1].endMs if h1 > 0 else 0.0
                end = heard[h1].startMs if h1 < len(heard) else start + 400.0
                if end <= start:
                    end = start + 400.0
            out.extend(_spread(wanted[w1:w2], start, end))

    dropped = len(wanted) - len(out)
    if dropped:
        log.warning("Alignment produced %d words for a %d word script", len(out), len(wanted))
    return out


def transcribe(audio_path: Path, cfg: Settings, script_hint: str = "") -> list[Caption]:
    """Return one Caption per spoken word, in order."""
    model = _load_model(cfg)

    terms = glossary(script_hint)
    log.info("Caption glossary: %s", terms or "(none)")

    segments, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language="en",
        initial_prompt=terms or None,
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

    captions = _align_to_script(captions, script_hint)

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
