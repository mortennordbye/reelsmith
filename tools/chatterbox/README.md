# Chatterbox: the cloned voice

The voiceover in every video is a clone of my own voice, built from one 25
second recording. This directory holds the engine, its isolated environment,
and the bench used to tune it.

`pipeline/tts.py` runs `synth.py` here as a subprocess. It never imports it.

## Why it is isolated

Chatterbox needs torch, transformers and `setuptools<81`. The pipeline venv is
Python 3.14 on numpy 2.5, with a setuptools that no longer ships
`pkg_resources`. Putting both in one environment means downgrading a working
pipeline to suit a voice, so the two never meet and talk over JSON instead.
Same trade as shelling out to the Claude CLI rather than taking an SDK
dependency.

The `setuptools<81` pin is load bearing and its failure is silent. Chatterbox's
watermarker imports `pkg_resources`, which setuptools 81 removed.
`perth/__init__.py` swallows the resulting ImportError and leaves
`PerthImplicitWatermarker` set to None, so the only symptom is `TypeError:
'NoneType' object is not callable` at model load, pointing nowhere near the
cause.

## Why Chatterbox and not the others

Licence. MIT for both code and weights. F5-TTS ships CC-BY-NC weights and
XTTS-v2 is under Coqui's non-commercial CPML, so neither is safe for an account
that might ever be monetised. Chatterbox also clones zero-shot, so there is no
training step and no dataset to assemble.

It stamps every output with a Perth watermark. Inaudible, and it survives
re-encoding, which is a point in its favour.

## Fresh checkout

Neither the venv (~3 GB) nor the reference recording is in git. Until both
exist, set `TTS_BACKEND=kokoro` in `.env` and the pipeline works without them.

```bash
uv venv --python 3.12 tools/chatterbox/.venv
VIRTUAL_ENV=tools/chatterbox/.venv uv pip install chatterbox-tts soundfile "setuptools<81"
```

Then record. `ref/RECORD-THIS.txt` has the passage laid out for reading aloud
and the rules that matter. Two of them decide the result: read at the pace you
want the videos to have, because the clone copies pacing and energy rather than
just timbre, and record somewhere quiet, because whatever the room sounds like
gets baked into every video. Save it as `ref/morten.wav`, or point
`CHATTERBOX_REF` somewhere else.

## Re-auditioning the settings

`exaggeration` 0.5 and `cfg_weight` 0.3 were picked by ear from a four-preset
sweep. Those renders are still in `out/`. The two knobs interact, so listen
rather than guess:

```bash
.venv/bin/python clone.py --ref ref/morten.wav --sweep
./compare.sh
```

`clone.py` renders four presets of the same script, converting non-wav
references with `afconvert` so there is no ffmpeg dependency. `compare.sh`
plays Kokoro's `am_michael` first as a baseline, then each clone.
`out/kokoro-am_michael.wav` is the real Ponytail voiceover from
`build/2026-07-31/`, so the comparison is against something that shipped.

One trap: `clone.py` does **not** normalise, while `synth.py` does. Raw
Chatterbox output clips past 1.0 where Kokoro sits at 0.63, so bench renders
sound louder and harsher than what the pipeline actually produces. Judge timbre
and pacing there, not level.

## Cost

About 35 seconds of compute for 25 seconds of audio once warm, on an M4 Pro via
MPS. The first call in a fresh process pays a much larger one-off for kernel
compilation, measured at 208s against 33s for the next identical render. The
pipeline starts a new process per video, so every run pays it.
