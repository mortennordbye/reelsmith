# tech-ig

Automated Instagram Reels about trending dev and AI tooling. Python orchestration,
Remotion rendering. See `README.md` for architecture and `INSTAGRAM.md` for the
account.

The audience is working software engineers. They can read code, and they have
seen a thousand generated videos. Everything below exists because some detail
gives generated content away, and once a viewer clocks one tell they stop
watching and start pattern matching.

## Text: the AI tells to avoid

These apply to every word that reaches a viewer, which means the hook, the
spoken script, the burned-in captions, and the Instagram caption.

**Punctuation.** No em dashes. No en dashes. No hyphens. No colons. Em dashes in
particular are the single most recognisable LLM signature in written English,
and on screen they read as a machine wrote it. The others are banned because
they are invisible to a listener but still clutter the caption burned onto the
video.

Enforced by a validator on `hook` and `spoken_script` in `pipeline/models.py`,
covering every dash variant including en dash, em dash and non-breaking hyphen.
It rejects rather than strips, because deleting a hyphen turns "seven-word" into
"sevenword". A rejection is handed back to Claude with the specific error and up
to two corrections are allowed before the run fails
(`_MAX_SCRIPT_ATTEMPTS` in `pipeline/scriptwriter.py`).

Rewrite around them instead:

| Instead of | Write |
|---|---|
| `92k-star repo` | `92k stars` |
| `seven-word prompt` | `seven words` |
| `Ponytail: the lazy senior dev` | two sentences, or drop the colon |
| `state-of-the-art` | `the best available` |

**Vocabulary.** No hype words: game-changer, revolutionary, insane,
mind-blowing, you won't believe, unlock, leverage, delve, seamless, robust,
elevate, harness, in today's fast-paced world. No emoji anywhere. No "This is a
tool that..." or "The project aims to..." throat clearing. Open on a verb or a
concrete noun.

**Structure.** Short sentences, average under twelve words, varied hard in
length. Active voice. One idea per sentence. A run of same-length sentences
flattens into drone no matter who reads it.

**Honesty.** Never invent facts, benchmarks, version numbers or quotes. If a
number is uncertain, leave it out. Where a project's own benchmark disagrees
with independent testing, say both. Scepticism is the differentiator in a niche
full of uncritical tool promotion.

Full prompt lives in `SYSTEM_PROMPT` in `pipeline/scriptwriter.py`.

## Voice

My own voice, cloned. `TTS_BACKEND=chatterbox`. Every stock voice is one some
other account is also using, and that is the one tell no amount of scripting
fixes. This is the only option where that is not true.

Engine is Chatterbox, picked over F5-TTS and XTTS-v2 on licence: MIT for both
code and weights, where F5-TTS ships CC-BY-NC weights and XTTS-v2 is
non-commercial CPML. Same reasoning that put Kokoro here before it. It clones
zero-shot, so the whole input is one 25 second recording.

Three things about it are load bearing:

- **It runs in a subprocess, not this interpreter.** Chatterbox wants torch,
  transformers and `setuptools<81`; the pipeline venv is Python 3.13 on numpy
  2.5 with a setuptools that dropped `pkg_resources`. Merging them means
  downgrading a working pipeline to suit a voice. `pipeline/tts.py` shells out
  to `tools/chatterbox/.venv` and they talk over JSON, the same trade as
  shelling out to the Claude CLI. The failure this avoids is silent:
  `perth/__init__.py` swallows the `pkg_resources` ImportError and leaves
  `PerthImplicitWatermarker` as None, so the only symptom is `TypeError:
  'NoneType' object is not callable` at model load.
- **Output is normalised to -3 dBFS in the worker.** Chatterbox renders hot and
  routinely clips past 1.0 where Kokoro sits at 0.63. Without it the voiceover
  distorts and jumps in loudness whenever the backend changes.
- **`exaggeration` 0.5 and `cfg_weight` 0.3 were picked by ear**, from a
  four-preset sweep still audible in `tools/chatterbox/out/`. They interact.
  Re-audition with `clone.py --sweep` rather than guessing.

The reference recording and the venv are both gitignored, so a fresh checkout
has neither. Re-record from `tools/chatterbox/ref/RECORD-THIS.txt`; the passage
is deliberately in the pipeline's own register, because the clone copies pacing
and energy, not just timbre.

Kokoro (`am_michael`) remains the fallback and is what shipped before this.
`KOKORO_SPEED` is 1.28 in `.env`, the `config.py` default is 1.15, and the
clone ignores both because its pace comes from the reference read.

## Visuals

No grid background. No particle fields. No circuit lines. The faint tech-grid
overlay appears in every AI explainer template, so it reads as "template" before
the viewer processes a word. `Background.tsx` uses a soft light wash, a vignette
and film grain instead; the grain matters because it breaks up the
mathematically flat gradients that make CG backgrounds look synthetic.

PNG frames, not JPEG. Remotion's JPEG default compresses every frame before
H.264 sees it, so you encode artifacts on top of artifacts, which shows as mush
around glyph edges on syntax-highlighted text.

Palette is GitHub-dark-adjacent on purpose (`video/src/theme.ts`). This audience
stares at that exact colour scheme all day, so it reads as native rather than as
"a video about code".

Ligatures are off in `CodeBlock.tsx`. JetBrains Mono fuses operators into single
glyphs, which is pleasant in an editor and wrong on screen: `<!--` rendered as
an arrow and `-->` as a long dash, so an HTML comment stopped looking like one.
The viewer has about two seconds to recognise a snippet and it has to match what
they would type. Anything else that renders code needs the same treatment
(`fontVariantLigatures: "none"` plus `fontFeatureSettings: '"liga" 0, "calt" 0'`).

## Cover stills

**The cover is the README hero. Nothing may cover it.** That screenshot is the
best-looking thing the maintainer designed and it is what makes the post look
like a real project rather than a generated card. Text goes in the empty band
below the browser frame, never centred over the hero. Centring was tried and it
buried the one element the cover exists to show.

Every run writes three files beyond `out.mp4`:

| File | What it is |
|---|---|
| `cover.png` | README hero, hook set below the frame. The one to upload. |
| `cover-clean.png` | README hero alone, no text, to design over by hand. |
| `caption.txt` | The post description, because the clipboard does not survive. |

`video/src/Cover.tsx` is the composition, registered in `Root.tsx` next to
`Reel` and rendered with `remotion still` from `renderer.render_covers`. It
reuses `SceneRenderer` and `theme.ts`, so a cover can never drift from the video
it fronts.

Two constraints that are easy to break:

- **Never render the cover at frame 0.** Scenes animate in from their own frame
  0, so a single-frame composition captures the hero at zero opacity and you get
  an empty background. The composition keeps the opening scene's real duration
  and the renderer asks for `COVER_FRAME` (90), past the entrance spring and
  inside the 7 second hold.
- **Instagram crops covers to a centred 4:5** (1080x1350, y 285 to 1635 in a
  1080x1920 frame) for the profile grid. Text must finish above 1635.

Cover rendering is best effort. It logs and returns what it managed, because a
failed still must never fail a run that already produced a video.

## Working on this repo

- `pipeline/models.py` holds the only interface between stages. Change a field
  there and mirror it in `video/src/schema.ts`.
- Stages re-use artifacts already on disk. To force a regeneration, move the run
  folder aside rather than deleting it.
- `pytest` and `ruff check` before considering a change done.
- Rendering does not start the repo cooldown. `main.py --posted <owner/repo>`
  does, and it is deliberately manual so a rejected video costs nothing.
