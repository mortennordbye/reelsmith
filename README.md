# tech-ig

Automated Instagram Reels for trending AI/dev tooling. One command goes from
"what's trending on GitHub today" to a posted-ready 1080x1920 MP4.

```
python main.py
```

```
GitHub + HN  ->  Claude Code  ->  edge-tts  ->  faster-whisper  ->  Playwright  ->  Remotion
  scraper        script.json     voice.mp3     captions.json       repo.png        out.mp4
```

**No paid API keys.** Script generation runs through the Claude Code CLI in
headless mode, using your existing subscription. The only credential you need
is a GitHub token.

---

## Setup

```bash
# Python side
uv venv --python 3.13
uv pip install -r requirements.txt
.venv/bin/playwright install chromium   # ~95 MB, for the opening screenshot

# Kokoro voice model, ~350 MB, one time (skip if you set TTS_BACKEND=edge)
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# Remotion side
cd video && npm install && cd ..

# Credentials
cp .env.example .env      # then add your GITHUB_TOKEN
```

A classic GitHub PAT with **no scopes** is enough for public repos:
<https://github.com/settings/tokens>. Without one you get 10 search requests
per minute, which is not enough for a single run.

Verify Claude Code is authenticated: `claude --version` should work, and
`ANTHROPIC_API_KEY` should be **unset** so the CLI uses subscription OAuth.

---

## Usage

```bash
python main.py                        # full run
python main.py --candidates           # just show today's ranked repos
python main.py --repo astral-sh/uv    # skip discovery, use a specific repo
python main.py --stop-after script    # stop early to inspect an artifact
python main.py --no-research          # skip Claude's web search (faster)
python main.py --resume 2026-07-30/astral-sh-uv   # re-render from artifacts
```

`--repo` does **not** need a `GITHUB_TOKEN` — fetching one named repo costs a
single core request, which fits inside the anonymous budget. Only discovery
needs the token.

Every stage writes to `build/<date>/<owner-repo>/` before the next one runs,
and re-uses what is already there. A failure at render time never costs you the
scrape, the script, or the transcription.

```
build/
  2026-07-30/
    astral-sh-uv/
    mortennordbye-homelab/
```

| Artifact | Written by |
|---|---|
| `repo.json` | scraper |
| `script.json` + `claude_envelope.json` | scriptwriter |
| `voice.mp3` | tts |
| `captions.json` | captions |
| `repo.png` | screenshot |
| `video.json` | spec |
| `out.mp4` | renderer |

### Iterating on the visuals

Rendering to check a font size is miserable. Use the Studio instead:

```bash
cd video && npm run studio
```

Then load `build/<date>-<slug>/video.json` as props. Hot reload, ~1s feedback.

---

## Architecture

`pipeline/models.py` holds Pydantic models that are the *only* interface
between stages. Each stage reads one model and writes another, which is what
makes stages independently re-runnable.

`VideoSpec` is deliberately renderer-agnostic — no React, no CSS, no Remotion
types. `video/src/types.ts` mirrors it. If you rename a field on one side,
rename it on the other; TypeScript will catch most of the fallout.

Scene boundaries are placed at the moment each cue is **actually spoken**:
`spec.py` matches every cue's `spoken_excerpt` against the Whisper transcript
and cuts on that word's real timestamp. Splitting the timeline by word count
instead sounds equivalent but isn't — words aren't spoken at a uniform rate, and
in testing that put scenes 1–3 seconds behind the narration. Proportional
allocation remains as a fallback for when the transcript can't be matched.

The total is pinned to the **measured** audio duration, so the video can never
outrun its own soundtrack.

---

## Notable decisions

**Remotion over MoviePy.** This niche is code on screen. Remotion gives real
Shiki syntax highlighting (same tokenizer as VS Code), flexbox layout, and
per-word caption animation that costs nothing. MoviePy would mean rasterizing
snippets to static PNGs with Pygments. Remotion is source-available and free
for individuals and companies of ≤3 people — check
<https://remotion.pro/license> if that changes for you.

**faster-whisper over openai-whisper.** Same models, word timestamps built in,
and it avoids pulling ~2.5 GB of PyTorch. It also bundles PyAV, so no system
ffmpeg is required.

**Kokoro over edge-tts, for recognisability rather than quality.** The two are
close on quality. The problem is that edge-tts has exactly five natural-sounding
English voices — Andrew, Brian, Ava, Emma, and one Australian — and the first
four are the default in every AI video tool on the market, so viewers have heard
them a thousand times. Its other 42 English voices are older-generation and
audibly robotic, so there is no way out within edge-tts. Kokoro is Apache-2.0,
runs fully on-device, has 54 voices, and none of them are worn out.

edge-tts is still supported (`TTS_BACKEND=edge`) and is the lighter option: no
model download and no `models/` directory. It is also a network call, which
Kokoro is not.

Audition before committing to a full run:

```bash
python main.py --preview-voice     # synthesizes one sample line and exits
```

**PNG frames, not JPEG.** Remotion defaults to JPEG, which compresses every
frame *before* H.264 sees it — so you encode artifacts on top of artifacts. On
flat dark UI with syntax-highlighted text that shows up immediately as mush
around glyph edges. PNG frames cost render time and nothing else.

**No grid background.** The faint tech-grid overlay is one of the most
recognisable generated-video tells — it appears in every AI explainer template,
so it reads as "template" before the viewer processes a word. Same for particle
fields and circuit lines. `Background.tsx` uses a soft light wash, a light
vignette, and film grain instead; the grain in particular breaks up the
mathematically flat gradients that make CG backgrounds look synthetic.

**The video opens on the README hero, not the file listing.** Playwright scrolls
to the top of the rendered README and captures the title lockup, badges, and
opening paragraph — the part the maintainer actually designed, and the
best-looking thing on the page. Dark mode, 3x DPI, framed in browser chrome.

Three details that make it legible rather than mush:

- **A 1000px viewport, not a desktop one.** GitHub centres the README in a
  fixed-width column, so a narrower viewport puts the captured region close to
  the 1080px the video renders it at — roughly 1:1 instead of a 0.7x downscale.
- **Scroll with `block: "start"`, not `scrollIntoViewIfNeeded()`.** The latter
  centres a tall element, which lands you in the middle of the README —
  usually a directory tree.
- **Back off ~64px after scrolling.** GitHub's README tab bar turns sticky and
  covers the first stretch of the article, which is exactly where the title is.

It holds for 7 seconds, the longest scene in the video, and the pipeline drops
a leading `repo_card` cue when a screenshot exists — the hero already shows the
name, description, language, and license, so the card would be the same
information twice in a row. A capture failure degrades to the card-only opening
rather than failing the run.

---

## Gotchas worth knowing

- **Pin TypeScript to 5.x.** `npm install typescript` now resolves to 7.x (the
  Go-native rewrite), whose JS API no longer exposes `ts.sys`. Remotion's
  bundler needs it and fails with
  `TypeError: Cannot read properties of undefined (reading 'readFile')`.
- **Never pass `--bare` to the `claude` CLI.** It forces
  `ANTHROPIC_API_KEY`/apiKeyHelper auth and never reads OAuth, which
  reintroduces the paid-API dependency this project avoids.
- **One `claude -p` call per run.** Each invocation loads Claude Code's full
  harness (~35k cached prompt tokens), so asking for the whole script at once
  is much cheaper than a call per scene.
- **GitHub ANDs repeated search qualifiers.** `license:mit license:apache-2.0`
  matches *nothing*. All license and topic filtering happens client-side in
  `pipeline/scraper.py` for this reason.
- **The cooldown store is what makes this runnable daily.** Star velocity is
  sticky; without `data/used_repos.json` the same three repos win all month.

---

## Tuning

Everything lives in `.env` (see `.env.example`). The two you will actually
reach for:

- `MAX_SCRIPT_WORDS` — the real lever on video length. This voice reads about
  150 wpm, so 80 words ≈ 32s. Raise it for longer videos rather than changing
  `TTS_RATE`.
- `REPO_COOLDOWN_DAYS` — lower it if you run out of candidates.

## Not built yet

Auto-posting to Instagram (the Graph API needs a Business account and a
publicly reachable video URL), scheduling, and a second render backend.
