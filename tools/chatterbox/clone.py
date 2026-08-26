"""Voice-clone test bench.

Standalone. Nothing in `pipeline/` imports this, and it writes nothing the
pipeline reads. The point is to answer one question before any of that is worth
doing: does a clone of your own voice sound like you, and does it beat
am_michael on the same script.

Engine is Chatterbox (Resemble AI). Picked over the alternatives on licence:
MIT for both code and weights, where F5-TTS ships CC-BY-NC weights and XTTS-v2
is non-commercial CPML. Same reason this repo runs Kokoro. It also clones
zero-shot, so there is no training step -- 15 seconds of reference audio is the
whole input.

    python clone.py --ref ../../accounts/<name>/ref/voice.m4a --sweep

Non-wav references are converted with afconvert, which ships with macOS, so
there is no ffmpeg dependency.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
REPO = HERE.parent.parent

# The Ponytail run, already rendered by the real pipeline. Using its script
# means the A/B is against a voiceover that actually shipped, on text with the
# tech nouns and bare numerals that trip TTS up.
DEFAULT_SCRIPT = REPO / "build/2026-07-31/dietrichgebert-ponytail/script.json"

# Chatterbox's two knobs, and they interact.
#   exaggeration -- emotional intensity. 0.5 is neutral. Past ~0.7 it starts
#     acting rather than reading.
#   cfg_weight   -- how hard it is pulled toward the reference. Lower is
#     slower and calmer; the Resemble notes suggest dropping it for a
#     fast-talking reference, which a 45 second Reels read is.
SWEEP = [
    ("neutral", 0.5, 0.5),
    ("calm", 0.5, 0.3),
    ("flat", 0.3, 0.5),
    ("lively", 0.7, 0.4),
]


def to_wav(src: Path) -> Path:
    """Normalise any reference to 24 kHz mono wav via afconvert (built into macOS)."""
    if src.suffix.lower() == ".wav":
        return src
    dst = src.with_suffix(".wav")
    if dst.exists() and dst.stat().st_mtime > src.stat().st_mtime:
        return dst
    print(f"Converting {src.name} -> {dst.name}")
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@24000", "-c", "1", str(src), str(dst)],
        check=True,
        capture_output=True,
    )
    return dst


def load_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    path = Path(args.script)
    if not path.exists():
        sys.exit(f"No script at {path}. Pass --text or point --script somewhere real.")
    return json.loads(path.read_text())["spoken_script"]


def describe(ref: Path) -> None:
    import soundfile as sf

    info = sf.info(str(ref))
    print(f"Reference: {ref.name}  {info.duration:.1f}s  {info.samplerate} Hz  {info.channels}ch")
    if info.duration < 7:
        print("  Warning: under 7 seconds. Chatterbox has little to work with.")
    elif info.duration > 40:
        print("  Warning: over 40 seconds. Longer is not better; 15 to 30 is the sweet spot.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", required=True, help="Reference recording of your voice")
    ap.add_argument("--script", default=str(DEFAULT_SCRIPT), help="script.json to read")
    ap.add_argument("--text", help="Literal text, overrides --script")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--sweep", action="store_true", help="Render all four parameter presets")
    ap.add_argument("--exaggeration", type=float, default=0.5)
    ap.add_argument("--cfg-weight", type=float, default=0.5)
    ap.add_argument("--device", default="mps", choices=["mps", "cpu"])
    args = ap.parse_args()

    ref = to_wav(Path(args.ref).expanduser().resolve())
    if not ref.exists():
        sys.exit(f"No reference audio at {ref}")
    describe(ref)

    text = load_text(args)
    print(f"Text: {len(text.split())} words\n")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    import torch
    import torchaudio
    from chatterbox.tts import ChatterboxTTS

    # Chatterbox ships CUDA-serialised checkpoints. On an M-series Mac the
    # loads have to be redirected or torch.load raises on the missing device.
    if args.device == "mps":
        real_load = torch.load

        def load(*a, **kw):
            kw["map_location"] = torch.device("mps")
            return real_load(*a, **kw)

        torch.load = load

    print(f"Loading Chatterbox on {args.device} (first run downloads ~1 GB)")
    t0 = time.time()
    model = ChatterboxTTS.from_pretrained(device=args.device)
    print(f"Loaded in {time.time() - t0:.0f}s\n")

    presets = SWEEP if args.sweep else [("custom", args.exaggeration, args.cfg_weight)]
    for name, exag, cfg in presets:
        dst = out_dir / f"clone-{name}.wav"
        t0 = time.time()
        wav = model.generate(text, audio_prompt_path=str(ref), exaggeration=exag, cfg_weight=cfg)
        torchaudio.save(str(dst), wav.cpu(), model.sr)
        dur = wav.shape[-1] / model.sr
        print(
            f"{name:8s} exag={exag} cfg={cfg}  ->  {dst.name}  "
            f"{dur:.1f}s  ({time.time() - t0:.0f}s to render)"
        )


if __name__ == "__main__":
    main()
