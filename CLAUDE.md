# reelsmith

Automated Instagram Reels about trending dev and AI tooling. Python
orchestration, Remotion rendering, plus a self-hosted comment to DM gateway.
See `README.md` for architecture and `gateway/README.md` for the service.

**If `PROFILE.md` exists, read it before writing anything a viewer will see.**
It is gitignored and therefore absent from a fresh clone. It holds the account
identity and the editorial register every script, caption and DM has to match.
Without it you can still work on the code, but do not write copy.

**If `HANDOVER.md` exists, read it before doing anything else.** It means a
session ended mid-thread, and it records uncommitted work and open decisions
that are not visible from the code. Delete it once its open items are resolved.

The audience is working software engineers. They can read code, and they have
seen a thousand generated videos. Most of what follows exists because some
detail gives generated content away, and once a viewer clocks one tell they stop
watching and start pattern matching.

## Text

The editorial rules live in `PROFILE.md`. Two of them are enforced in code, so
they are stated here as well, because otherwise the validator looks like a bug:

- **No colons and no dashes of any kind** in `hook` or `spoken_script`. A
  validator in `pipeline/models.py` rejects every dash variant including en
  dash, em dash and non-breaking hyphen. It rejects rather than strips, because
  deleting a hyphen turns "seven-word" into "sevenword". The rejection goes back
  to Claude with the specific error and up to two corrections are allowed before
  the run fails (`_MAX_SCRIPT_ATTEMPTS` in `pipeline/scriptwriter.py`).
  `gateway/copy.py` applies the same check to the DM templates.
- **No hype vocabulary and no emoji.** Enforced in the prompt rather than the
  parser, so it needs review rather than trusting a green test.

Write "92k stars" rather than "92k-star", and split a colon into two sentences.
`SYSTEM_PROMPT` in `pipeline/scriptwriter.py` is the full contract.

## Voice

`TTS_BACKEND=chatterbox`, a cloned voice. Which voice and why is in
`PROFILE.md`; the reference recording is gitignored and a fresh checkout has
neither it nor the venv, so re-record from
`tools/chatterbox/ref/RECORD-THIS.txt` or set `TTS_BACKEND=kokoro`.

Engine is Chatterbox, picked over F5-TTS and XTTS-v2 on licence: MIT for both
code and weights, where F5-TTS ships CC-BY-NC weights and XTTS-v2 is
non-commercial CPML. It clones zero-shot, so the whole input is one 25 second
recording.

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
- **`exaggeration` and `cfg_weight` were picked by ear** from a four-preset
  sweep. They interact. Re-audition with `clone.py --sweep` rather than
  guessing.

Kokoro (`am_michael`) remains the fallback and is what shipped before this.

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

## Publishing

`publisher.publish_reel` uploads to Instagram directly: a resumable container,
the MP4 as raw bytes to `rupload.facebook.com`, a poll while Meta transcodes,
then `media_publish`. No object storage and no App Review, both of which this
repo spent a while believing were required. Setup is
`docs/instagram-api-setup.md`.

Four things here are load bearing:

- **Committing to a post is what starts a cooldown.** Not rendering, not
  uploading, not a failed publish. There are exactly two places that call
  `mark_featured`: `_publish_run`, at the moment a media id exists, and
  `_enqueue_run`, at the moment a video is handed to the gateway's schedule.
  The second exists because nothing on this machine is present when a queued
  post goes out days later, and queueing a repo is committing it. `--unmark`
  undoes it if the post is cancelled.
- **`published.json` is a duplicate guard, not a log.** `--publish` refuses a
  folder that already has one. Unattended is exactly where posting the same
  Reel twice goes unnoticed, and the receipt is cheaper than noticing.
  `queued.json` is the same guard for `--enqueue`, and both are checked by
  both, so a run cannot be published one way and queued the other.
- **The publish path raises where the cover path logs.** A half-finished
  upload is worth stopping on. That is the opposite of `render_covers` and
  `copy_to_clipboard`, and the difference is deliberate.
- **The token is in `data/ig_token.json`, refreshed by the `--snapshot` job.**
  Long-lived tokens last 60 days, are refreshed rather than reissued, and an
  expired one cannot be refreshed at all. That makes a job whose stated purpose
  is star velocity also the thing keeping posting alive, so do not "simplify"
  the refresh out of it without moving it somewhere that runs as often.

`--cover-url` is the seam for a hosted cover. Meta cURLs that URL, so it cannot
be a local path. Without it the thumbnail comes from `thumb_offset` at
`COVER_FRAME`, which is the same moment `cover.png` renders, so the fallback
loses the hook band and nothing else.

## The gateway

`gateway/` is a separate FastAPI service, not a pipeline stage. It turns
"comment SEND and I will DM you the link" into something that happens, and it
holds the scheduled queue that publishes a batch of Reels over the following
days. It imports nothing from `pipeline/` or `config.py`, which is what keeps
its container image free of the models and the voice. Its own README carries
the three Meta rules it exists to obey.

Two things about the queue are load bearing and easy to undo by accident:

- **Media retention is keyed on the queue, not on age.** `_prune_media` sweeps
  by mtime, and a post scheduled eight days out is older than the TTL by the
  time its turn comes. `db.live_media_names` is the exemption list, and without
  it the back of a ten post queue is deleted before it publishes and fails with
  a 404 from Meta's fetcher a week after the upload that caused it.
- **The slot jitter is derived from the slot id and the local date, never
  rolled.** A random offset is re-rolled on every restart, which lets one
  evening's slot fire twice. This is also why the config sync keeps the id of
  an unchanged slot rather than recreating the row.

## Working on this repo

- `pipeline/models.py` holds the only interface between stages. Change a field
  there and mirror it in `video/src/schema.ts`.
- Stages re-use artifacts already on disk. To force a regeneration, move the run
  folder aside rather than deleting it.
- `pytest` and `ruff check` before considering a change done.
- Rendering does not start the repo cooldown. `main.py --posted <owner/repo>`
  does, and it is deliberately manual so a rejected video costs nothing.
- **A batch ranks once, not once per video.** `--batch N` fills the gateway's
  three daily slots from one sitting. It cannot rank per video, because the
  cooldown only starts at publish or enqueue, so a freshly rendered repo is
  still the top candidate and every run in the batch would pick it again.
  `--covered` prints the store; covered repos are dropped during discovery,
  before enrichment, so they cost no README fetch on their way to losing.
- **This repo is public.** `PROFILE.md`, `PLAN.md`, `.env`, `data/` and the
  voice recording are gitignored and hold the private half. Before adding a
  file, decide which half it belongs to. `scripts/backup-secrets.sh` backs up
  the private half, driven by the `# backup:start` block in `.gitignore`.
