# reelsmith

Automated short videos about trending AI/dev tooling, on Instagram, YouTube,
TikTok and Facebook. One command goes from "what's trending on GitHub today" to
a posted-ready 1080x1920 MP4.

```
python main.py
```

```
GitHub + HN  ->  Claude Code  ->  Chatterbox  ->  faster-whisper  ->  Playwright  ->  Remotion
  scraper        script.json    voice.wav       captions.json       repo.png        out.mp4
                               (my own voice)
```

**No paid API keys.** Script generation runs through the Claude Code CLI in
headless mode, using your existing subscription. The only credential you need
is a GitHub token.

---

## Setup

```bash
# Python side
uv venv --python 3.14
uv pip install -r requirements.txt
.venv/bin/playwright install chromium   # ~95 MB, for the opening screenshot

# Voice. The default backend is a clone of my own voice, which needs its own
# ~3 GB environment and a reference recording. Neither is in git.
# See tools/chatterbox/README.md. To skip it entirely, set TTS_BACKEND=kokoro
# and download the Kokoro model instead (~350 MB, one time):
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# Remotion side
cd video && npm install && cd ..

# Credentials
cp .env.example .env      # then add your GITHUB_TOKEN

# Which account this checkout is for. One checkout serves several; an account
# is a directory holding its own .env, data/ store and ref/ voice recording.
mkdir -p accounts/nightlybuild
echo "REELSMITH_ACCOUNT=nightlybuild" >> .env
```

`python main.py --new-account <name>` makes the directory, a `data/`, a `ref/`
and an `.env` of commented out lines, and prints what is left to do by hand.

There is no default account and no resolving one by counting. Without
`REELSMITH_ACCOUNT` or `--account <name>`, every run fails at startup naming the
accounts it can see. Guessing wrong publishes to the wrong audience and nothing
later undoes it. Upgrading a checkout that predates this is
`python main.py --migrate-account <name>`, which prints the plan and moves
nothing until it is given `--yes`.

A classic GitHub PAT with **no scopes** is enough for public repos:
<https://github.com/settings/tokens>. Without one you get 10 search requests
per minute, which is not enough for a single run.

Verify Claude Code is authenticated: `claude --version` should work, and
`ANTHROPIC_API_KEY` should be **unset** so the CLI uses subscription OAuth.

To let the pipeline post for you, add `IG_USER_ID` and `IG_ACCESS_TOKEN` too.
That part is optional and everything except `--post` and `--publish` runs
without it; `.env.example` has the four-step setup.

---

## Usage

```bash
python main.py                        # full run
python main.py --batch 3              # render the top 3 repos back to back
python main.py --candidates           # just show today's ranked repos
python main.py --covered              # list every repo already made into a Reel
python main.py --repo astral-sh/uv    # skip discovery, use a specific repo
python main.py --stop-after script    # stop early to inspect an artifact
python main.py --no-research          # skip Claude's web search (faster)
python main.py --resume 2026-07-30/astral-sh-uv   # re-render from artifacts
python main.py --recover --approve    # finish and queue what a killed batch left

python main.py --post                     # render, then publish it unattended
python main.py --publish 2026-07-30/astral-sh-uv   # publish a run you approved

python main.py --snapshot                 # record star counts only (see below)
python main.py --refresh-token            # renew the Instagram token
python main.py --posted astral-sh/uv      # start the 30-day cooldown
python main.py --unmark astral-sh/uv      # undo that
```

`--repo` does **not** need a `GITHUB_TOKEN` — fetching one named repo costs a
single core request, which fits inside the anonymous budget. Only discovery
needs the token.

### Posting

Three ways out, in increasing order of how much you trust it.

**By hand.** The default. When the render finishes the caption is on your
clipboard and the run folder is open. Drop `out.mp4` into Instagram, paste,
post, then `python main.py --posted astral-sh/uv`.

**Reviewed.** Watch the video, then `python main.py --publish <date>/<slug>`.
That uploads it and starts the cooldown in one step. This is the recommended
setup: you keep the veto, and you never touch a file.

**Unattended.** `python main.py --post` renders and publishes in one run.
`launchd/it.nordbye.reelsmith.daily.plist` does it on a schedule.

All three need the same thing to be true: **nothing starts the cooldown except
posting.** A video you looked at and rejected should not burn that repo for a
month, so rendering deliberately does not mark it.

A published run gets a `published.json` receipt, and `--publish` refuses to run
twice against the same folder. Delete the receipt to override it.

Publishing needs `IG_USER_ID` and `IG_ACCESS_TOKEN` in `.env`; see
`.env.example` and `docs/instagram-api-setup.md`. It does **not** need App
Review, and it does **not** need the MP4 hosted anywhere: the container is
created with `upload_type=resumable` and the file goes up as raw bytes.

The cover image is the one thing that does want a public URL, because Meta
fetches `cover_url` from its own servers when the container is created and
cannot read a local path. There are three ways that resolves, in the order the
publisher tries them:

1. **`--cover-url`**, if you pass one, always wins.
2. **The gateway**, if `GATEWAY_URL` is set. `cover.png` is uploaded and the
   returned URL is used. This is best effort: a gateway that is down logs and
   moves on.
3. **`thumb_offset`**, the fallback, which picks the same frame `cover.png` is
   rendered from, minus the hook band.

```bash
python main.py --publish 2026-07-30/astral-sh-uv \
  --cover-url https://example.com/cover.png
```

### Keeping the token alive

Long-lived Instagram tokens last 60 days, are refreshed rather than reissued,
and **an expired one cannot be refreshed**. Recovering from that costs a browser
round trip through the Meta dashboard. The daily `--snapshot` job refreshes when
it is within `IG_REFRESH_MARGIN_DAYS` (15) of expiring, so keeping that job
installed is what keeps posting unattended. `--refresh-token` forces it.

The live token lives in `data/ig_token.json`, not `.env`. A cron job that
rewrites a hand-edited dotenv eventually eats something you cared about.

### The daily snapshot

Star *velocity* is 55% of the candidate score, and it can only be measured
against a snapshot from an earlier day. On days with no snapshot the scorer
falls back to a damped stars-per-day proxy, which systematically favours large
established repos over genuine breakouts.

So snapshot every day, including days you make no video:

```bash
python main.py --snapshot     # two search requests, a couple of seconds
```

`launchd/it.nordbye.reelsmith.snapshot.plist` runs it at 06:00 daily; its header
comment has the install commands. From day two onward every ranking uses
measured deltas.

Every stage writes to `build/<account>/<date>/<owner-repo>/` before the next one
runs, and re-uses what is already there. A failure at render time never costs
you the scrape, the script, or the transcription.

```
build/
  nightlybuild/
    2026-07-30/
      astral-sh-uv/
      charmbracelet-crush/
```

The `<date>/<owner-repo>` arguments to `--resume`, `--publish` and `--enqueue`
are resolved under the selected account, so they keep the shape they always had
and gain the account from `--account` rather than from the path.

| Artifact | Written by |
|---|---|
| `repo.json` | scraper |
| `script.json` + `claude_envelope.json` | scriptwriter |
| `voice.wav` (`.mp3` on the edge backend) | tts |
| `captions.json` | captions |
| `repo.png` | screenshot |
| `video.json` | spec |
| `out.mp4` | renderer |
| `cover.png` + `cover-clean.png` | renderer |
| `caption.txt` | main |
| `published.json` | publisher, only once it is posted |

### Iterating on the visuals

Rendering to check a font size is miserable. Use the Studio instead:

```bash
cd video && npm run studio
```

Then load `build/<account>/<date>/<slug>/video.json` as props. Hot reload, ~1s
feedback.

---

## How the two halves fit together

There are two machines, and the split is deliberate rather than incidental.

```
 Mac (laptop)                             Homelab cluster
 ────────────                             ───────────────
 scrape ─► script ─► voice ─► captions    gateway (one container)
        └► screenshot ─► render             • Meta webhook receiver (DMs)
              │                             • comment poller (keyword watch)
              │  1. upload cover.png ─────►  • cover host, public URL
              │  2. publish the Reel  ──►  Instagram
              │  3. register the post ────►  • starts watching its comments
              │                             • SQLite state on a PVC
              └─ the voice never leaves ─┘
```

**What lives where, and why.**

| On the Mac | Why it cannot simply move |
|---|---|
| The voice | The reference recording is biometric. The cluster deliberately holds nothing that could reproduce it. |
| Script generation | Runs on a Claude Code subscription, so it costs nothing per run. An API key would. |
| Rendering | Remotion plus a headless browser, and the artifacts are already here. |

The cluster holds the half that has to be awake when the laptop is not: Meta
delivers a DM webhook whenever someone replies, and comments have to be polled
every minute for seven days after a post.

**The dependency runs one way.** The pipeline works with the gateway down: the
cover falls back to a video frame, post registration logs and moves on, and any
comment missed meanwhile is still inside Meta's seven day reply window when the
gateway returns. A dead cluster can never block a publish. That is why
`pipeline/gateway.py` returns rather than raises, the same rule `render_covers`
follows and the opposite of `publish_reel`.

**What the viewer used to see.** The caption said "comment SEND and I will send
you the link". The gateway's poller spots the comment, sends the one private
reply Meta allows per comment, asks the person to follow, and sends the link
once they have. Measured end to end on a real post: 31 seconds from comment to
link. Nothing advertises the keyword now, so all of that is wired and dormant,
and the caption asks for a follow instead. See `SPOKEN_CTA` and `CAPTION_CTA` in
`pipeline/gateway.py`.

## Architecture

`pipeline/models.py` holds Pydantic models that are the *only* interface
between stages. Each stage reads one model and writes another, which is what
makes stages independently re-runnable.

`VideoSpec` is deliberately renderer-agnostic — no React, no CSS, no Remotion
types. `video/src/schema.ts` mirrors it as a zod schema, and the renderer parses
`video.json` through it in `calculateMetadata` before the first frame. TypeScript
only checks the code we wrote, not the JSON another process handed us — without
the runtime check, a field renamed on the Python side shows up as an `undefined`
painted into a finished MP4. `video/src/types.ts` infers its types from those
same schemas, so there is one definition per shape.

Scene boundaries are placed at the moment each cue is **actually spoken**:
`spec.py` matches every cue's `spoken_excerpt` against the Whisper transcript
and cuts on that word's real timestamp. Splitting the timeline by word count
instead sounds equivalent but isn't — words aren't spoken at a uniform rate, and
in testing that put scenes 1–3 seconds behind the narration. Proportional
allocation remains as a fallback for when the transcript can't be matched.

The total is pinned to the **measured** audio duration, so the video can never
outrun its own soundtrack.

`gateway/` is a separate service, not a pipeline stage. It holds the scheduled
queue that publishes finished videos over the following days, hosts the MP4 and
the cover image that publishing wants a public URL for, sweeps each post's
insights, and serves the admin panel. It also carries the comment to DM
mechanic, which is wired but dormant because nothing advertises the keyword any
more. It runs in the homelab cluster, imports nothing from `pipeline/` or
`config.py`, and the pipeline works with it down. See `gateway/README.md`.

**A destination is a row, not a service.** Instagram, YouTube, TikTok and
Facebook all publish from the same queue, the same slots and the same claims;
the entire difference is one branch in `scheduler.publish_queued` and, on two
of them, a credentials table. One render feeds every destination it is enqueued
to, uploaded once, because `/api/media` is content addressed.

TikTok is not on the same footing as the others. It posts by dropping the video
into the creator's drafts for one tap, because the audit that would allow
unattended posting reviews a posting screen this project does not have and
rejects apps "designed for private or personal use only". A refusal therefore
costs a config flag rather than a rewrite, which is what the two paths sharing a
publisher buys. `docs/tiktok-api-setup.md` is the runbook and the portal traps.

Facebook was the cheapest of the four and the reason is worth stating: a Page
access token is a token plus an expiry, which is the shape `accounts` has held
since the first migration, so it needed no credentials table, no token mint and
no refresher. Wired and registered on 2026-08-27; nothing has published there
yet, so its publisher has met a test double and not Meta.
`docs/facebook-api-setup.md` is the runbook and the record.

**Several accounts, one niche.** `accounts/<name>/` holds an account's `.env`,
its cooldown store and its voice reference, and `--account` binds a run to one
before the settings are built, so no stage signature changes. There is no
default and no resolve-by-count: a run without one fails at startup naming the
accounts it could see, because publishing to the wrong audience is not
recoverable and a lost night is.

What `--account` does not give a second account is a different **subject**.
`VideoSpec.repo` is required and mirrored in `video/src/schema.ts`, so a second
niche breaks that interface once, deliberately, when there is a second real
caller to argue with it. The cost of both is audited in
`docs/multi-destination-audit.md`.

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

**My own cloned voice, over any stock voice.** This started on edge-tts, moved
to Kokoro, and ended here, and each step was about recognisability rather than
quality. edge-tts has exactly five natural-sounding English voices — Andrew,
Brian, Ava, Emma, and one Australian — and the first four are the default in
every AI video tool on the market, so viewers have heard them a thousand times.
Kokoro fixed that with 54 unworn voices. Cloning fixes the remaining problem,
which is that a stock voice is still a voice someone else can pick.

Engine is Chatterbox, chosen on licence: MIT for code and weights, where
F5-TTS ships CC-BY-NC weights and XTTS-v2 is non-commercial CPML. It clones
zero-shot from a single 25 second recording, with no training step, and adds
about 35 seconds of compute per video.

It runs in its own venv under `tools/chatterbox/`, invoked as a subprocess.
That is deliberate: Chatterbox needs torch, transformers and `setuptools<81`,
while this project runs Python 3.14 on numpy 2.5, and merging them would mean
downgrading a working pipeline to suit a voice.

Kokoro (`TTS_BACKEND=kokoro`) remains the fallback and needs no reference
recording. edge-tts (`TTS_BACKEND=edge`) is the lightest option, with no model
download and no `models/` directory, but it is a network call and the other two
are not.

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
  sticky; without `accounts/<name>/data/used_repos.json` the same three repos
  win all month, and it is per account because a cooldown is a fact about one
  audience rather than about the repo.
  It is written by `--posted`, not by the render, so rejected videos cost
  nothing. `--unmark` is the escape hatch, and `--covered` prints the list.
- **A covered repo is dropped during discovery, not scored to zero.** Same
  rule, applied before enrichment rather than after, so a repo we have already
  featured never costs a README fetch and a Hacker News lookup on its way to
  losing. On a normal day that is most of the store: yesterday's winners are
  still climbing, so they still come back in the search.
- **`--batch N` ranks once and renders N.** Ranking per video would pick the
  same winner every time, because nothing marks a repo as taken until it is
  published or queued, and a batch is neither. One repo failing drops one
  video rather than the whole day.
- **`--recover` finishes a batch that never reached its end.** A scheduled
  render can lose its process to something outside it, and the work already on
  disk is then worth more than the run that produced it. The sweep takes the
  last two days of `build/`, gives each unfinished folder the stages it still
  owes, and queues anything with a video and no receipt. It writes no script
  and asks GitHub for nothing, so recovering is always cheaper than rendering
  again, and a run already carrying a receipt is skipped, so running it twice
  is free.
- **`video/public/` is a staging area, not a store.** Remotion can only load
  assets from there, so each render copies its audio and screenshot in and
  prunes the previous run's. Everything in it is regenerated from `build/`.

---

## Tuning

Everything lives in `.env` (see `.env.example`). The two you will actually
reach for:

- `MAX_SCRIPT_WORDS` — the lever on video length, and not the lever on
  retention. The cloned voice reads 165 to 190 wpm measured over six runs and
  the appended ask adds about seven words, so 80 words ≈ 31s. The model writes
  to the ceiling, so this decides the length rather than suggesting it. Before
  raising it, note that measured average watch time across the first seven
  posts was 2.9 to 8.4 seconds: a longer video is watched by nobody new and
  lowers the completion rate. The opening is where the audience is lost.
- `REPO_COOLDOWN_DAYS` — lower it if you run out of candidates.
- `MAX_HOOK_CHARS` — the on-screen hook. Read by both the prompt handed to
  Claude and the validator that checks his answer, so the two cannot drift.

## Tests

```bash
uvx ruff check . && uv run --with pytest pytest
```

Two suites in one directory. The pipeline half is synchronous and pure: scene
allocation, caption alignment, scoring, the star-history and cooldown stores,
caption gap repair. The gateway half is async and drives the real ASGI app
against a real SQLite file and an `httpx.MockTransport` standing in for Meta and
Google, so it tests the seams rather than the functions.

No network and no fixture files anywhere. The stages that call GitHub, Claude,
Whisper or Remotion are not tested; `--stop-after` is how you inspect those.

The gateway suite needs `fastapi` and `aiosqlite`, which the pipeline venv
deliberately lacks, so on a render host it is
`pytest --ignore-glob="tests/test_gateway_*"`. CI runs the whole thing.

## Not built yet

Unattended posting to TikTok, which needs an audit that reviews a posting screen
this project does not have; the drafts path works today and needs no audit. A
second niche, which is one required field and one scene component away and is
deliberately waiting for a second real caller. A second render backend.

The admin UI, the scheduled queue, the insights feedback loop, YouTube, TikTok,
Facebook and `--account` all used to be listed here and all seven shipped.

The largest open question is how much of the pipeline could move off the laptop
entirely. Research and publishing would move easily; script generation would
start costing money, because it runs on a Claude Code subscription today rather
than an API key; and the voice cannot move at all, because the reference
recording is biometric and stays off the cluster on purpose.
