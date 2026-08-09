"""Chatterbox worker. Runs under its own interpreter, never the pipeline's.

`pipeline/tts.py` invokes this as a subprocess rather than importing it. The
reason is dependency isolation, not taste: Chatterbox wants torch, transformers
and `setuptools<81`, while the pipeline venv is Python 3.14 on numpy 2.5 with a
setuptools that no longer ships `pkg_resources`. Putting both in one
environment means downgrading a working pipeline to suit a voice, so the two
never meet. Same reasoning as shelling out to the Claude CLI instead of taking
an SDK dependency.

Arguments come in as one JSON object on argv[1] so there is no flag parsing to
keep in sync across the boundary. Result goes out as one JSON object on stdout.
Anything else this file prints is noise from torch and is ignored by the caller.

    .venv/bin/python synth.py '{"text": "...", "ref": "...", "out": "..."}'
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Peak to normalise to. Chatterbox renders hot, routinely clipping past 1.0,
# where Kokoro lands around 0.63. Left alone the voiceover would both distort
# and jump in loudness whenever the backend changed, so every render is pinned
# to the same peak instead. -3 dBFS.
TARGET_PEAK = 0.707


def main() -> int:
    args = json.loads(sys.argv[1])
    text: str = args["text"]
    ref = Path(args["ref"])
    out = Path(args["out"])
    exaggeration = float(args.get("exaggeration", 0.5))
    cfg_weight = float(args.get("cfg_weight", 0.3))
    device = args.get("device", "mps")

    if not ref.exists():
        print(json.dumps({"ok": False, "error": f"reference audio missing: {ref}"}))
        return 1

    import torch
    import torchaudio
    from chatterbox.tts import ChatterboxTTS

    # Chatterbox ships CUDA-serialised checkpoints; on Apple silicon every load
    # has to be redirected or torch.load raises on the missing device.
    if device == "mps":
        real_load = torch.load

        def load(*a, **kw):
            kw["map_location"] = torch.device(device)
            return real_load(*a, **kw)

        torch.load = load

    model = ChatterboxTTS.from_pretrained(device=device)
    wav = model.generate(
        text,
        audio_prompt_path=str(ref),
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
    ).cpu()

    peak = wav.abs().max().item()
    if peak > 0:
        wav = wav * (TARGET_PEAK / peak)

    out.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(out), wav, model.sr)

    print(
        json.dumps(
            {
                "ok": True,
                "path": str(out),
                "seconds": wav.shape[-1] / model.sr,
                "sample_rate": model.sr,
                "peak_before_normalise": peak,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # surface the reason across the process boundary
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)
