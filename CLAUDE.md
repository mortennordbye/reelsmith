# reelsmith

Automated short videos about trending dev and AI tooling, published to
Instagram, YouTube and TikTok. Python orchestration, Remotion rendering, plus a
self-hosted gateway holding the queue that publishes them. See `README.md` for
architecture and `gateway/README.md` for the service.

**If `PROFILE.md` exists, read it before writing anything a viewer will see.**
It is gitignored and therefore absent from a fresh clone. It holds the account
identity and the editorial register every script, caption and DM has to match.
Without it you can still work on the code, but do not write copy.

**If `IDEAS.md` exists, read it before proposing a change to the video.** It is
gitignored, so a fresh clone does not have it. It ranks what to try next and,
more usefully, records what was already rejected and why, argued from the
account's own view counts and skip rates. Three separate proposals here have
been for things the pipeline already had. The panel that maintains it is
`.claude/skills/reel-council`.

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
  transformers and `setuptools<81`; the pipeline venv is Python 3.14 on numpy
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

**The torch device is chosen by platform, not pinned.** `mps` on Darwin and
`cpu` everywhere else, in `config.py`. The wrong one does not degrade, it
fails: asking for `mps` on Linux raises `Storage device not recognized: mps` at
model load, three stages into a run that has already paid for a script.
Measured at about 35 seconds of compute per 25 seconds of audio on `mps`, and
about ten minutes for the same clip on six CPU cores, which is the single
slowest thing about rendering off a Mac.

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

  **That is true on the laptop and a silent no-op on the render host**, which is
  the machine `--snapshot` actually runs on nightly. The render host has no
  `ig_token.json` and no `IG_ACCESS_TOKEN`, because it enqueues rather than
  publishes, so `refresh_token_if_due` cannot load a token and returns `None`
  rather than failing, and the caller swallows the error besides. Nothing
  anywhere refreshes the laptop's token automatically; the gateway refreshes its
  own separately, which is why posting has never noticed. So `main.py
  --refresh-token` on the laptop is a manual job with a 60 day clock on it, and
  the only thing that will remind you is reading the expiry out of
  `accounts/<name>/data/ig_token.json`. Checked 2026-08-27: last refreshed
  2026-07-31, expires 2026-09-29.

`--cover-url` is the seam for a hosted cover. Meta cURLs that URL, so it cannot
be a local path. Without it the thumbnail comes from `thumb_offset` at
`COVER_FRAME`, which is the same moment `cover.png` renders, so the fallback
loses the hook band and nothing else.

### Three destinations, and what is shared between them

**All three publish**, since 2026-08-27. One render fans out to three queue
rows, one slot each at 08:10 Europe/Oslo, one post a day per platform.
`docs/tiktok-api-setup.md` opens with the ordered runbook; the reasoning behind
it is in that doc's later sections, `docs/multi-destination-audit.md` and
`docs/tiktok-publishing-plan.md`, both of which are records of a decision rather
than guides.

The order in that runbook is forced rather than preferred. The consent trip is
what produces the open id, and the open id is what the `GATEWAY_SLOTS` line and
the render host's `TIKTOK_OPEN_ID` are both keyed on, so neither can be written
ahead of it. Re-authorising an account means walking it again.

**TikTok runs on the app's sandbox credentials, and that is permanent rather
than a stage.** The production configuration cannot be saved at all: its Save
validates the App review block, which demands a demo video of an end to end user
interface, and this project has none. A sandbox reaches only its target users,
`@thenightlybuild` is one, and the inbox path needs no audit, so nothing is lost
except the ability to post unattended. Re-read this if a posting screen is ever
built.

Three portal facts that cost a session each, all in
`docs/tiktok-api-setup.md` under *What actually happened when this was
attempted*:

- **The scopes do not exist until the products are added.** With no products
  on the app the Add scopes dialog lists three read scopes and neither
  `user.info.basic` nor `video.upload`, which reads as a permanent refusal and
  is not one. Login Kit first, then Content Posting API.
- **The redirect URI must be https, and loopback is not an exception.** Both
  `http://127.0.0.1:8723/callback` and the https form of it are rejected by the
  field. `scripts/tiktok_authorise.py` no longer runs a listener: the browser
  lands on `GET /tiktok/callback` on the gateway, that page prints the code,
  and the operator pastes the address back. The httproute allowlist has to
  carry the path or the consent trip lands on a 404.
- **A save that sends no request is a failed validation, not a dead button.**
  The first attempt recorded the sandbox as unsaveable on exactly that
  evidence. The banner said `This form has 1 error` and the field was
  `Web/Desktop URL`, which does not appear until the Web platform checkbox is
  ticked, so it is required and invisible at the same time.

- **A destination is an `accounts` row, not a table.** `accounts.platform` says
  which service and `account_id` is an opaque account key holding a Meta user id
  on one platform and a channel id on another. Credentials live in a table per
  platform, because Meta's shape is a token plus an expiry and Google's is a
  client pair plus a refresh token, and one table holding both is half null on
  every row.
- **It was called `ig_user_id` until 2026-08-26, and three places kept the old
  name on purpose.** `gateway/graph.py` and `gateway/publisher.py` still take an
  `ig_user_id`, because at that point the value is being handed to Meta as an
  Instagram user id, which is what it is; the two names mark the boundary
  between the account key and one platform's id for it. The `reelsmith_token_days_left`
  gauge keeps its `ig_user_id` label, because a Prometheus label is part of a
  series' identity and the alert rules reading it are in the homelab repo, so it
  moves in a homelab PR or not at all. And every API route and body still
  accepts `ig_user_id` alongside `account_id`: the gateway image deploys itself
  and the render host is pulled by hand, so the side that lags is always the one
  sending the old name, and refusing it would turn a rename with no behaviour
  into discovery reading every account's commitments as its own.
- **The whole publish fork is one lookup in `scheduler.publish_queued`.**
  Everything above it is written about a queue that publishes something on a
  timetable and never looks at what. That is why a platform costs a module and a
  branch rather than a subsystem. **Instagram used to be the fallthrough**, so a
  row for a platform with no branch was handed to Meta's publisher: a Reels
  container against a TikTok open id with an empty token, on a live account.
  That was the opposite of `db.active_accounts`, which defaults to Instagram
  precisely so a missed call site is inert. It matches on the platform now and
  fails the row rather than the tick, so one misconfigured account cannot stop
  the other two publishing. F1.
- **The scriptwriter learns from Instagram alone, and this is not a gap to
  close.** `skip_rate` is the share who scrolled past inside three seconds and
  it is the one number the loop turns on. YouTube's `averageViewPercentage`
  scores a whole video and TikTok exposes no retention metric at all. So
  `insights` carries a `platform` column, TikTok's four counts are stored and
  shown on the Posts page, and `/api/results` and the Insights page both filter
  to Instagram **explicitly** rather than relying on nothing else filling the
  `skip_rate` column, which is a rule that holds by accident. Feeding anything
  else to `_results_block` would corrupt the single measurement everything else
  is argued from.
- **On a TikTok row the unmeasured columns are 0 and the platform column is
  what says so.** `reach`, `saved`, `avg_watch_ms`, `total_watch_ms` and
  `skip_rate` are absences rather than results, and the Posts page renders the
  column set for the platform so a TikTok post is not shown as one that got
  zero reach and zero saves.
- **The publish id is not a video id**, which is a shape Meta never had.
  `status/fetch` reports the post finished and returns neither an id nor a URL,
  so the row carries its `publish_id` until the insights sweep lists the
  account's recent videos and matches on the title this service wrote. The
  publish id stays in `container_id`, where it was written before the publish
  was attempted.
- **TikTok's gate is not YouTube's gate.** YouTube's private lock turned out not
  to apply here. TikTok's is enforced by a documented error code, and the audit
  that lifts it reviews a posting screen this repo does not have and rejects
  apps "designed for private/personal use only". So the publisher serves both
  the audited and the unaudited path and a refusal costs a flag:
  `GATEWAY_TIKTOK_DIRECT_POST` off is the inbox, which needs no audit and drops
  the video into the creator's drafts for one tap, and
  `GATEWAY_TIKTOK_PRIVACY_LEVEL` is `SELF_ONLY` until the audit lands so the
  pre-audit behaviour is chosen rather than discovered.
- **`GATEWAY_TIKTOK_ENABLED` gates three things and there is no YouTube
  equivalent on purpose.** It gates the token refresher, the insights sweep and
  publishing, and a queued TikTok row reaching a slot with it off fails that row
  rather than retrying, because a flag that is off is not a transient condition.
  `docs/youtube-publishing-plan.md` named a `GATEWAY_YOUTUBE_ENABLED` that was
  never built, and it is decided against rather than added: nothing on the
  YouTube path runs unless a slot fires, and a slot only fires when the
  scheduler is on. A flag earns its place when something runs without it, which
  is what the TikTok refresher does.
- **`is_aigc` and `containsSyntheticMedia` are the same question and they move
  together.** Both are `false`, and since 2026-08-26 for a reason rather than
  because a value had to be sent: the fields ask whether the content depicts
  something that did not happen, and here a cloned voice reads its owner's own
  words about a repository that exists over a screenshot of that repository's
  README. Worth re-reading if the format ever gains a face, a person who did not
  consent, or a claim the video acts out rather than reports. One changed
  without the other is the bug to look for; the reasoning is in
  `gateway/config.py` next to the flag.

### The public pages, and why they are not on GitHub

`GET /`, `/privacy`, `/terms` and `/tiktok/callback` are served by
`gateway/pages.py`. Every platform demands a privacy policy URL before it will
take an application, and TikTok additionally demands terms of service and an
official website. They were `docs/privacy.md` and `docs/terms.md` until
2026-08-27.

The callback is on the same router for the same reason and is not a legal page:
**TikTok will not register an OAuth redirect URI that is not https**, so the
consent trip cannot land on a loopback listener. The page prints the
authorisation code and does nothing else. It deliberately does not exchange it,
because the exchange needs the client secret and this service is not told that
until the account is registered at the end of the same trip.

- **TikTok will not accept a URL on a domain it cannot verify by DNS record**,
  and `github.com` can never be one. The field simply reads "This URL is not
  verified" and the form refuses to save. That is what moved them, not a
  preference for HTML.
- **One verification covers all four URLs.** A verified domain carries its
  subdomains, and `gate.nordbye.it` is already the host TikTok pulls media from
  for `PULL_FROM_URL`, so the media and the three documents need one record
  between them rather than one each.
- **The router mounts unconditionally, unlike the admin panel.** The obvious
  home was `admin.public`, which already serves the one page that cannot require
  a login, but that router is only included when `GATEWAY_ADMIN_ENABLED` is on.
  Three platforms hold these URLs on file, and a legal page that 404s because a
  feature flag moved is worse than one nobody reads. `test_gateway_pages.py`
  pins it with the panel off.
- **The templates are the only copy.** The Dockerfile copies `gateway/` alone,
  so the image cannot see `docs/`; rendering markdown at runtime would put a
  parser in an image that carries no dependency it does not import, and keeping
  both would be two privacy policies drifting apart. `docs/privacy.md` and
  `docs/terms.md` are pointers now.
- **No script tags and no external stylesheet**, asserted rather than intended.
  These render for somebody who arrived from an app listing on an unknown
  device, and an asset on a third-party host is both a tracking vector on a
  privacy policy and a way for the page to become unstyled text in a few years.

### An account is a directory, and it is never guessed

`accounts/<name>/` holds the machine readable half of an identity: the per
account `.env`, `data/` for the cooldown store and the live token, and `ref/`
for the voice recording the clone is built from. Its run folders live under
`build/<name>/<date>/<slug>/`. `--account <name>` binds the process before
`get_settings()` is built, and since every stage already reads one `Settings`,
no stage signature changes.

- **There is no default and no resolve by count.** `--account`, or
  `REELSMITH_ACCOUNT` in the host `.env`, and neither one fails the run naming
  the accounts it could see. The gateway resolves an unnamed slot line to the
  single registered Instagram account, and a second account registering was
  enough to delete a working schedule at boot; that is F0, and the pipeline's
  version of the same shortcut publishes to the wrong audience, which nothing
  later undoes. A run that fails at startup costs a night.
- **The editorial half stays in one root `PROFILE.md`.** It already carries the
  shared rules at the top and one section per account, and its own template
  says everything not overridden inherits them. A file per directory would
  either duplicate the shared half, which is the drift this repo refuses
  everywhere else, or invent an include mechanism for markdown.
- **A `<date>/<slug>` argument keeps its shape.** `--resume`, `--publish` and
  `--enqueue` are resolved against `cfg.build_dir`, so they gain the account
  from `--account` rather than from the path, and `--recover` scans the same
  subtree and needs no account level of its own.
- **The cooldown store and the voice are per account and cannot be shared.**
  One `used_repos.json` between two accounts hands the second one a 30 day
  exclusion on every repo the first ever covered (F9). One cloned voice across
  two accounts meant to look unrelated is, per `PROFILE.md`, the strongest link
  between them.
- **Every gateway read says which account is asking.** `fetch_covered`,
  `fetch_rendered`, `fetch_results`, `fetch_queue` and `forget_rendered` all
  send `ig_user_id` now (F8). Unscoped they answered for every account, which
  was harmless while the Instagram row and the YouTube row were the same video,
  and with two accounts starves both out of the top of a stars-sorted result
  set.
- **`accounts/<name>` has to be a *directory* symlink on a host that keeps it
  on a share**, for the reason `data` always did: `StarHistory.save()` renames
  a temp file over its target, and that rename replaces a file symlink with a
  real file. `scripts/pod-setup.sh` links one per account.
- **`python main.py --migrate-account <name>` moves a single account checkout
  into the new layout.** It prints the plan and moves nothing until it is given
  `--yes`. The root `.env` is copied rather than moved, because it also holds
  the global half and which lines are global is a judgement rather than a rule.
  Until it is run, `data_dir` and the voice reference fall back to where they
  were, so a checkout mid migration reads the store it already has rather than
  starting an empty one.

### A second account is not a second niche, and only one of them is built

`--account` gives a second account its own credentials, cooldown store, voice
and build subtree. It does not give it a different **subject**, and the
distance between those two is worth knowing before anything is promised.

`python main.py --new-account <name>` makes the directory, a `data/`, a `ref/`
and an `.env` of commented out lines. Every line is commented out on purpose: a
profile with a blank `IG_USER_ID` looks configured and fails at the first
publish, where one with nothing set fails at `require_instagram`, which says
what is missing and where to set it. It fills nothing in, because the voice
recording, the identity and the editorial section are all things only a person
can produce.

**A second account in the same niche works today.** A second niche does not,
and these are the measured reasons rather than a guess:

- **`VideoSpec.repo: RepoMeta` is the one real interface break.** It is
  required, and mirrored in `video/src/schema.ts`, so generalising it to a
  subject touches every stage. Nothing else in the models is niche specific.
- **`spec.repo` is read in exactly two places on the render side**, the
  `repo_card` branch of `SceneRenderer.tsx` and the `BrowserFrame` URL label.
  Verified 2026-08-26 and still true. A new niche needs one scene component and
  a label, not a redesign.
- **Reusable unchanged**: `tts.py`, `captions.py`, `spec.py`, `renderer.py`,
  `publisher.py`, `results.py`, `backfill.py`, the whole of `gateway/`, the
  scheduler, the cooldown tables and the insights sweep.
- **Not reusable**: `sources/github.py`, the discovery and ranking half of
  `pipeline/scraper.py`, and `SYSTEM_PROMPT` in `pipeline/scriptwriter.py`.

**The generalisation is deliberately not built yet, and that is a decision
rather than an omission.** The only second niche that exists is prototyped
outside the pipeline: `video/src/spinoff/` has its own Remotion entry point and
`tools/spinoff/voice.py` is a script rather than a stage, so nothing consumes a
`VideoSpec` in that shape. Generalising a required field for a consumer that
does not exist, whose shot kit has not met `SceneRenderer` yet, is inventing the
abstraction before the second case can argue with it. `PROFILE.md` also records
that niche's supply problem as open, which means the second case is not settled
enough to design against.

So the order is: settle the second niche's shape against the real renderer,
then break the interface once, with two real callers to check it. Not before.

**Registering a second Instagram account used to delete the first one's
schedule**, at boot, with one warning line describing a different symptom. Fixed
2026-08-26: the config slot sweep no longer runs while any slot line is
unresolved, because an account's absence from the config means "delete its rows"
and an unresolved line means "I could not work out whose these are". Put an
explicit `account=` on every `GATEWAY_SLOTS` line anyway, which is never
ambiguous and so is never subject to any of it. F0.

### The nightly run is not in this repo

`launchd/` is the Mac story and drives nothing on the Linux host. There the
02:00 run is a scheduled agent session whose whole behaviour is one prompt,
held outside git, so looking for the schedule in this checkout finds nothing
and editing this checkout cannot change what fires tonight. Anything about it
that is worth knowing has to be written down here instead, which is what this
section is.

- **It has to name its account.** `REELSMITH_ACCOUNT` in the render host's
  `.env` is the one line version and is what the 02:00 and 05:00 sessions rely
  on, since a scheduled prompt held outside git cannot easily gain a flag.
  Without it every run fails at startup naming the accounts it could see, which
  is the trade that was chosen: a night lost is recoverable and a Reel posted to
  the wrong audience is not.
- **It arms what it renders.** The nightly enqueues with `--approve`, so a
  finished Reel goes straight into the gateway's schedule and the next free
  slot publishes it. The alternative was a draft, which waits for somebody to
  watch it, and a draft queued at 02:00 waits until somebody remembers it
  exists. A slot drains a queue faster than anyone reliably reviews one.
- **It renders two a night against one slot, up to a queue of three.**
  `--batch 2 --max-queue 3`, and the surplus is the point: the queue is meant
  to sit about three days deep so a night that produces nothing is absorbed
  rather than showing up as a gap on the feed. A power cut, a wedged pod, or two
  scripts that both trip the dash validator then costs the account nothing.
  Because `--max-queue` clamps the batch to the room left, most nights render
  one and stop on their own.

  **It was `--batch 4 --max-queue 10` until 2026-08-27**, when Instagram went
  from three posts a day to one and ten stopped meaning three days. The numbers
  are a function of the cadence and have to move with it, which is the thing to
  remember rather than either pair of numbers.
- **One render feeds all three destinations.** `--enqueue` and `--recover` make
  an Instagram row, a YouTube row and a TikTok row from the same MP4, uploaded
  once, so the nightly needs no second render and no extra step to keep three
  surfaces fed. It is driven by `YOUTUBE_CHANNEL_ID` and `TIKTOK_OPEN_ID` in
  the render host's `.env`, and without either the fan-out skips that
  destination silently and queues the rest. The id is all that host needs: the
  gateway holds the credentials, so no Google or TikTok secret reaches the
  machine that renders.
- **Which render goes where is a decision, not a detail.** YouTube gets
  `out-no-cta.mp4`, because a follow ask on a surface that calls following
  subscribing reads wrong. TikTok gets `out.mp4`, because it is a feed like
  Instagram's and the word is the same word. The caption follows the same
  split: `youtube_description` takes the ask out and `tiktok_title` keeps it.
  Say so in the code rather than letting it be whichever variable was nearest.
- **The cut for YouTube is a second render, so it has to restage its own
  assets.** `video/public/` is staging, and a run prunes every other slug on
  its way in, so a spec's screenshot and voiceover survive exactly until the
  next video renders. `render_without_cta` runs at enqueue, after the whole
  batch, and `renderer.restage_spec_assets` is what puts them back. Without it
  the failure is silent and shaped like a partial success: a `RenderError`
  means "no trimmed version", the caller falls back to the full video, and on
  2026-08-27 the first two Shorts of a three video batch went out carrying an
  Instagram follow ask while the third, still staged because it rendered last,
  got the cut. Anything else that re-renders a finished `video.json` needs the
  same call.
- **The queue rows made before that fix were left as they were, deliberately.**
  Eighteen of the twenty six pending YouTube rows point at the Instagram file
  and eight had already published that way. Recutting them is cheap and the run
  folders are all still on the render host; repointing them is not, because
  nothing in the API changes a row's video and the only lever is a hand written
  `UPDATE` of `video_name` against the live database. Decided 2026-08-27 that a
  month of Shorts saying follow rather than subscribe is worth less than that
  write. So a Short from before 2026-08-27 carrying the ask is expected, not a
  sign the fix regressed.
- **`--max-queue` counts the Instagram queue only.** YouTube drains one a day
  against Instagram's three, so its queue grows by design, and a third queue
  draining at its own rate does not change that reasoning. Counted in the
  ceiling either would climb past `--max-queue` on its own and pin the batch at
  zero renders, permanently, with every component still reporting healthy. The
  ceiling asks whether the feed is stocked far enough ahead, and the feed is
  Instagram.
- **Cancelling is the only review left.** Nothing reads the script before it
  goes live. The validators still catch dashes and hype vocabulary; they cannot
  catch a claim that is wrong about the project. Cancelling in the admin UI
  before the slot fires is the veto, and `--unmark <owner/repo>` afterwards, so
  a bad video does not also burn its repo for 30 days.
- **`--snapshot` runs first and unconditionally**, ahead of any render, because
  velocity is 55 percent of the ranking score and a missing day cannot be
  backfilled. The `--max-queue` ceiling stops the batch before discovery would
  have recorded anything, so a quiet night still has to take the snapshot.
- **The ceiling stopping the batch is the normal outcome**, not a failure to
  investigate and not something to compensate for by rendering by hand.
- **The batch is not guaranteed to reach its own last step.** It runs inside
  the verksted session container, which is capped at 8 GiB, and chatterbox
  holds about 4 GiB while it speaks against a baseline that climbs through a
  batch. On 2026-08-14 the third video's TTS crossed the limit at 02:33, the
  OOM killer took the container, and with it the batch, the session, and
  `/tmp/nightly.log`. One finished video survived because it was already on
  disk; a script that had been researched and paid for did not get used.
- **So queueing is `--recover`, not a list of `--enqueue` lines.** The nightly
  ends with `--recover --approve --max-queue 3`, which sweeps the last two
  days of build folders and finishes whatever each one still owes. It is what
  makes the run idempotent: interrupted anywhere, the next pass picks the work
  up rather than abandoning it. `queued.json`/`published.json` make it safe to
  run twice, a dot in a folder name keeps it away from a render a human moved
  aside, and it never writes a script, so a folder holding only `repo.json` is
  reported and left for discovery to decide about.
- **A second session runs the same sweep at 05:00**, because the failure being
  covered is the one that kills the session that would otherwise recover from
  it. A morning with nothing to recover is silent; one that recovers something
  reports "attention", since the video is safe but the night was not.
- **The batch log lives in `build/`, not `/tmp`.** The container restart that
  makes the log worth reading is the same event that deletes it.

## The gateway

`gateway/` is a separate FastAPI service, not a pipeline stage. It turns
"comment SEND and I will DM you the link" into something that happens, and it
holds the scheduled queue that publishes a batch of Reels over the following
days. It imports nothing from `pipeline/` or `config.py`, which is what keeps
its container image free of the models and the voice. Its own README carries
the three Meta rules it exists to obey.

**Nothing advertises the keyword any more, so the DM half is dormant.** The
video, the end card and the caption asked for a comment for the first 53 posts
and drew two, both from people who unfollowed once the link arrived. It could
not have gone otherwise: what the DM trades is a public GitHub URL, findable
faster than a comment can be typed, and gating that behind a follow selects
exactly the follower who leaves with it. All three channels now ask for a
follow instead (`SPOKEN_CTA` and `CAPTION_CTA` in `pipeline/gateway.py`).

The mechanic is left wired rather than deleted: `keyword_for` still runs,
`register_post` still arms the poller, and the comment and DM paths are
untouched. Nobody can guess an unadvertised keyword, so treat it as off. Putting
it back is a change to those two constants and the end card, and it should take
numbers beating a follow ask, not a hunch.

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
- **Keep gateway deploys out of the slot window.** Since 2026-08-27 that is one
  window rather than four: all three platforms fire at 08:10 Europe/Oslo, so
  06:10 UTC plus up to twenty minutes of jitter either side. A rollout landing
  inside it restarts the pod mid publish, and the claim the row is holding is
  deliberately never swept back automatically, because Meta may already have
  accepted the post. The result is a row stuck in `claimed` with no failure,
  which `CLAIM_STALE_AFTER` makes visible after an hour but nothing undoes for
  you.

### The panel answers two different questions

Queue and Posts list things. Insights compares them, and the split is
deliberate: a list cannot answer "did that change work" while posts go out on a
timetable and the prompt changes underneath them.

- **The hook is on the queue card, and it is not decoration.** Cancelling
  before the slot fires is the only review this account has, and until the hook
  travelled with the video, reviewing meant pressing play on every queued Reel
  to see the one line `skip_rate` actually scores. On the Posts page it sits
  directly above the percentage it earned, which is the only pair on that page
  where one plainly caused the other.
- **`gateway/analysis.py` holds the arithmetic and no FastAPI.** Cohorts, the
  trailing median and the chart geometry are pure functions on plain rows,
  because a page that renders is not a page that is right and nobody
  re-derives a cohort table by hand once it looks plausible.
- **The chart carries one measure on one axis.** Views deliberately does not
  share it: two scales on one plot is the most common way a chart lies, and
  views here run from 86 to 1614, so a linear axis holding both would be a flat
  line with one spike. Views lives in the cohort tables as a median plus a
  count of how many cleared 500, which is what that distribution can support.
- **The axis is not inverted and the good band is at the bottom.** Skip rate
  reads like every other percentage, with 100 at the top, so better is down.
  Inverting it reads correctly for one second and wrongly afterwards.
- **Dots and the trend line are one series, not two.** They share the hue and
  differ by weight. `--accent` and `--accent-soft` used as two series fail a
  colour separation check, being two steps of one hue, and a post under the
  threshold is marked with a ring rather than a second colour so the
  distinction survives being read without colour.
- **Server rendered SVG, no chart library and no measurement step.** The
  viewBox is fixed and scales to its container, so the page has content before
  any script runs.
- **The Repos page is the cooldown list, made visible.** It is what decides
  whether tonight's batch may pick a repo, and it existed as
  `accounts/<name>/data/used_repos.json` on the machine that renders plus two
  gateway tables
  nothing displayed, so "have we already done this one" was a question you
  answered by running a command on the right machine. It joins `covered_repos`,
  `rendered_repos` and the queue's publish dates, keeps the three
  distinguishable rather than flattening them, and flags the row nothing else
  in the panel would mention: a repo with a render and no commitment, which is
  a finished video that cost a script, a voiceover and a render and that
  nothing will bring up again. `analysis.REPO_COOLDOWN_DAYS` mirrors the
  pipeline's `config.py` because the gateway cannot import it; discovery still
  reads its own value, so a drift costs a word on a page rather than a
  decision.
- **Unsettled posts are held back from the cohorts, not annotated.** The daily
  `insights` history had never been read by anything, and it answers the
  question every comparison here was hedging: a Reel reaches about 56 percent of
  its final views at its first reading, 92 at the second and 99 at the third, so
  it is finished in roughly two days. `analysis.maturity` recomputes that curve
  from this account's own rows every time the page is drawn and prints it beside
  the rule it applies, because a number written into prose goes stale silently.
  A cohort holding yesterday's post is not reporting a worse slot, it is
  reporting a post that has not finished arriving, and the old age column made
  the reader do that correction by eye. The count held back is always shown: a
  table that quietly dropped four posts reads as one that covered everything.
- **`/api/results` carries `readings` so the CLI holds back the same posts.**
  `main.py --cohorts` cannot import `gateway/analysis.py` and duplicates the
  threshold, which is acceptable; two views of one question disagreeing is not.
- **The chart keeps every post.** Skip rate settles a reading earlier than views
  and then drifts about a point, so holding back the newest dots would hide the
  most recent evidence to avoid an error smaller than the marker.
- **The Repos page says why the scorer picked each repo.**
  `score_candidates` splits the score into velocity, stars, Hacker News and
  README quality and writes it into `repo.json`, where it never left the machine
  that ranked. It rides on `register_rendered`, because a score is a property of
  the pick rather than of the post, and a repo can be rendered without ever
  being queued. Stored as the JSON the pipeline already produces rather than as
  columns, since the weights are config and have changed twice.
- **On a phone the cohort tables scroll rather than dropping columns.** The
  panel's global rule hides column four and up under 700px, which on this table
  would have taken the breakout count, the column the page itself says to read
  before the median. They opt out and scroll instead, and the chart does the
  same, since 58 dots inside 120 pixels of height stop being separable.

## Alerting

**Alerting lives in homelab, not in this process.** Prometheus scrapes the
gateway and `k8s/talos/apps/reelsmith/monitoring.yaml` holds the rules;
Alertmanager already has a Discord receiver on `#homelab-alerts`. A Discord
webhook in the gateway was written and thrown away: it duplicated that, needed a
second webhook secret, and had one fatal gap, which is that an in-process
notifier is silent exactly when the process is wedged. `ReelsmithPollerStalled`
and `ReelsmithSchedulerStalled` are the alerts that catch it, and only something
outside the process can raise them.

What this repo owes that arrangement is metrics that move:

- **Every terminal failure has to reach a counter.** The `no video file` path
  never asks Meta for anything and for a while it was also the only failure
  Prometheus could not see, so a post dying for want of a file was invisible to
  `ReelsmithPublishFailing` while every other failure fired it.
- **A claim nobody finished is a terminal failure that never reached one.**
  `claimed` is held for one publish attempt and is deliberately never swept back
  automatically, because Meta may have accepted the post. But a process dying
  between the container and `media_publish` leaves the row there with no
  `failure`, so the admin UI showed it as an ordinary recent post, `cancel`
  refused it as mid-flight, and nothing alerted. Row 55 sat that way for nine
  days with its container already in ERROR at Meta. `claimed_at` (schema 18)
  is what makes a claim held for ninety seconds distinguishable from one held
  for nine days; `CLAIM_STALE_AFTER` is an hour, and past it the row reaches
  `reelsmith_stale_claims` and becomes cancellable from the panel. Still
  nothing automatic: the gauge and the button only make the decision available,
  because only the account can say whether the post went out.
- **`queue_depth` is filled in at scrape time**, in the `/metrics` route rather
  than from the scheduler tick. It is a gauge describing a table, so the honest
  value is what the table says now; and the scheduler is off by default, which
  would have left the gauge empty on exactly the deployments where a stuck post
  goes unnoticed longest.
- **Publish a series per state including the empty ones.** A gauge that only
  reports what exists leaves `failed` at its last non-zero value forever, and
  the alert that fired on it never resolves.

## The cooldown list, on both sides

`accounts/<name>/data/used_repos.json` is what discovery reads, and it is one
JSON file on one laptop, outside git and outside every backup here. It is per
account because a 30 day cooldown is a fact about one audience, and a second
account pointed at the same file inherits every repo the first ever covered. `GET /api/covered` hands the
same list back from the gateway, whose volume gets `VACUUM INTO` every six
hours. `_sync_covered` in `pipeline/scraper.py` folds it in before the first
Search call, so a repo recovered this way costs no query slot and no README
fetch on its way to being dropped.

- **It merges, it never replaces.** `main.py --posted` marks a repo the account
  published by hand and the gateway is never told, which is how
  `DietrichGebert/ponytail` got into the local store. Replacing would un-cover
  exactly the posts that exist nowhere else. The earlier date wins on a
  conflict, because taking the later one extends the cooldown by however long
  the two records disagree.
- **Committed, not published.** `db.covered_repos` cannot reuse
  `published_media`, which filters to `state = published` and would omit every
  post still in the line. The cooldown starts at enqueue, so a draft counts as
  much as a Reel from three weeks ago. `cancelled` is the one state left out,
  since cancelling is the moment `--unmark` is meant to run.
- Failure is an empty dict, like the results loop. A gateway that is down, or
  one older than this endpoint, leaves discovery behaving exactly as before.

### Depth is what the list costs discovery

Search is sorted by stars descending, so the repos featured in the last 30 days
are the highest starred things the queries match and they sit in front of
everything else every night. A fixed window of *results* is therefore not a
fixed number of *candidates*, and on 17 and 18 August 2026 it was zero of them
twice in a row: 60 repos on cooldown, 54 of them inside the top 150 by stars,
and the batch died at ranking with `No candidate repositories survived
filtering`. The pool was never thin, the two queries matched about 9,900 repos
that morning. The window into it was.

- **Discovery pages until it has candidates, not until it has results.**
  `CANDIDATE_TARGET` (50) is survivors per query and `CANDIDATE_SEARCH_CAP`
  (400) is how deep it may page to find them, so the window widens by exactly
  as much as the cooldown list grows and costs nothing on a night when it does
  not have to. `iter_repositories` is a generator for that reason: a page
  nobody asks for is a search request never spent.
- **The target is per query, not per run.** One counter shared between them
  would let a productive first query satisfy the whole run and leave the second
  unread, which is the difference between breakouts and established projects.
- **The snapshot reads to the cap, not to the target**, because it cannot know
  how deep discovery will go tonight. Reading only the shallow slice would hand
  measured velocity to the repos most likely to be on cooldown and the cold
  start proxy to everything discovery had to dig for.
- **The old error message named the wrong lever.** `MIN_STARS_BREAKOUT` and
  `BREAKOUT_WINDOW_DAYS` widen the query, which does nothing when the top of
  the result set is what is being lost. It points at the cap and the cooldown
  length now.
- GitHub refuses to read one query past 1000 results whatever `total_count`
  says, so the cap has a ceiling of its own. Getting past that needs a
  different query, not another page.

### Rendered is a second, weaker list

`GET /api/rendered` is the repos a video already exists for, and it is
deliberately not part of `/api/covered`. A commitment is irreversible and starts
a 30 day cooldown; a render is neither, and folding the two together would merge
"I built this and have not watched it yet" into the cooldown store and block
the repo for a month. So `fetch_rendered` is read next to `fetch_covered` before
the first Search call and its repos are dropped for that run only, never written
down.

- **It exists because rendering is invisible everywhere else.** Nothing marks a
  repo until publish or enqueue, so a video sitting unqueued on the laptop does
  not stop discovery ranking its repo again the next morning.
  `firecrawl/anydoc` was built twice on consecutive days that way, the second
  time by a batch that had just been told to avoid duplicates.
- **A table of its own, not another `queued_posts` state.** A render has no
  uploaded media, no slot, no keyword and no link, so it would be a queue row
  with all of those null, and the scheduler, `_prune_media` and
  `live_media_names` would each need to learn to skip it. Every row in the
  queue is publishable, and that invariant is worth more than the shared table.
- **`--unmark` deletes it**, in the same breath as clearing the cooldown, since
  a rejected video usually has both and clearing one leaves the repo invisible
  with nothing local left to explain why. Rendering staying free to throw away
  is the rule the whole build step is designed around.
- The first `rendered_at` survives a re-render, because a repeat is the thing
  the table exists to prevent and the honest date is the one that already made
  the repo redundant.

## The feedback loop

The scriptwriter is shown what this account's own hooks scored, so a script is
written with hindsight rather than from scratch every time. `pipeline/results.py`
does the join and `_results_block` in `pipeline/scriptwriter.py` renders it.

- **The join is on `repo_full_name`, because it is all the two sides share.**
  The gateway knows the media id and the numbers and has never seen a hook; the
  run folder holds the hook and never learns its media id, because a queued post
  is published days later by a service somewhere else. The 30 day cooldown is
  what makes the repo name unique enough to join on.
- **A repo has several run folders and only one of them shipped.** Moving a run
  aside is the documented way to force a regeneration, so `.prev` and `.v2`
  siblings are normal. `_runs_by_repo` ranks them: an unsuffixed name first,
  since `RepoCandidate.slug` turns every dot into a hyphen and a dot can only
  come from a human; then a publish receipt; then the later date. Taking the
  last folder in sorted order instead reported a rejected draft as the hook that
  scored 79.5 percent, which nothing downstream could have caught.
- **The block states the benchmark next to the list, and derives its verdict on
  the list from it.** The format averages 30 to 40 percent. When the loop was
  built every hook here skipped 64 to 80, so the top of a sorted list was still
  a bad hook and the prompt had to say so or the model copied it. That sentence
  was hardcoded, and by 53 posts the best had reached 45.8 percent, at which
  point it was telling the model to disregard the one hook that worked. The
  verdict now switches on the actual best score in the block. Do not write it
  back down as prose; a fact about the numbers goes stale silently and the only
  symptom is worse scripts.
- **A post that skipped the pipeline joins on its caption instead.**
  `main.py --backfill` lists the account's live Reels against the run folders
  and `--yes` registers the ones it matched. The join is the first paragraph of
  the caption, because the gateway appends the comment ask above the hashtags on
  one publish path and not the other, so the body is the only part written once.
  A body two run folders claim matches neither, for the same reason
  `_runs_by_repo` ranks them.
- **A backfilled post is registered to be measured, never to be answered.**
  `poll_comments=False`, because registering is also what arms the comment
  poller and the post is days old by then. The flag is decided when the row is
  inserted and a re-registration never changes it, so running the backfill twice
  cannot disarm a live post. A gateway older than schema v8 drops the field
  silently and arms the poller anyway, which is why the reply says "measuring"
  rather than "watching" and the pipeline reads it.
- Failure is an empty list everywhere. A gateway that is down costs a run its
  hindsight, which is what every run had before this existed.

### The recipe is what makes a change answerable

Every run is fingerprinted by `recipe()` in `pipeline/results.py` as the git
commit plus a digest of `SYSTEM_PROMPT` and the knobs, written to `recipe.json`
in the run folder. Two runs with the same fingerprint are comparable and two
with different ones are not, which is the whole claim.

- **It travels with the video, because it cannot be reconstructed later.** The
  recipe is written on whichever machine rendered and the numbers land on the
  gateway, so the only join was the repo name against a local build folder.
  That is wrong rather than merely absent once two machines render: the Mac
  holds `build/2026-08-08/ultraworkers-claw-code/`, a script that was never
  rendered, and the pod built and queued that repo on 2026-08-17, so the local
  join hands the loop an 11 day old hook and calls it the one that scored. It
  is sent at `--enqueue`, stored on `queued_posts`, and returned by
  `/api/results`, which wins over the local guess in `past_posts`.
- **Read from the folder at enqueue, never recomputed.** `--enqueue` can run
  days after the render and `--recover` sweeps two days of folders, so
  recomputing would stamp the video with the checkout that queued it.
- **Dates cannot stand in for it.** The queue is meant to sit three days deep,
  so a Reel published this morning was written from whatever the code said
  earlier in the week. Eight changes landing in one evening on 2026-08-17 were
  indistinguishable from the four videos already queued when they landed, and
  reading publish dates said five posts had run through them when one had.
- Empty means "before recipes" and is comparable to nothing, which is the
  honest answer. A Reel put out with `--publish` leaves no queue row at all.

**The hook travels for the same reason, and it mattered more.** The loop looked
the hook up by repo name in a local build folder, so on a machine that did not
render the video it read an opening that was never on one: the Mac's stale
`claw-code` script had the loop reporting "Anthropic shipped its coding agent's
source by mistake" as the hook that scored 63.6 percent. A wrong recipe misleads
somebody reading a table; a wrong hook is fed into the prompt that writes
tomorrow. It also decides whether a post counts at all, since a row with no hook
is skipped, and that silently dropped the thirteen Reels rendered on the pod.

**Saves and shares reach `/api/results` too.** They have been collected since the
insights sweep existed and were never sent, so every analysis in `IDEAS.md`
optimised the metric that happened to be exposed. They are carried on `PastPost`
as `None` when absent rather than 0, because a zero share count reads as a video
nobody passed on and "not measured" is a different claim. Deliberately **not**
wired into `SYSTEM_PROMPT` yet: that would change what scripts get written while
eight earlier changes are still unattributed.

`skip_rate` is the metric the loop turns on: the share who scrolled past inside
the first three seconds, so it scores the opening alone. It arrives from
`REELS_METRICS` in `gateway/graph.py`, which are Reels-only and therefore
requested separately, because Meta fails a whole insights call when one metric
does not apply to the media type.

## Dependencies

Upkeep is meant to cost no attention. `.github/dependabot.yml` watches all five
ecosystems: the pipeline's pins, the gateway's pins, `video/`, the workflows,
and the base image. Patch and minor updates are grouped into one PR per
ecosystem per week; `dependabot-auto-merge.yml` turns on auto-merge for those
and for every security update, and holds routine majors back for a human.

- **Dependabot does not do routine Python updates, and must not be given
  them back.** Both `requirements.txt` files are `uv pip compile` output, where
  a pin can exist only because another package demands exactly it. Dependabot
  edits one line and never re-solves, so it offered pydantic-core 2.47.0
  against a pydantic pinning `==2.46.4`, and rfc3986 2.0.0 against a csvw
  pinning `<2`. Both were unsatisfiable and neither was an upgrade that
  existed. `open-pull-requests-limit: 0` on the two pip entries turns off
  routine PRs while keeping security ones, and
  `recompile-python-deps.yml` re-solves the whole set weekly instead.
- **That workflow dispatches `ci.yml` at its own PR on purpose.** A PR opened
  with `GITHUB_TOKEN` does not trigger workflows, so the required checks would
  never appear and the PR would sit blocked forever. `workflow_dispatch` is the
  documented exception the token may still trigger, and its check runs attach
  to the head commit, which is what the ruleset reads. Setting a
  `DEPS_PR_TOKEN` secret makes the dispatch redundant rather than wrong.
- **The ruleset is the safety, not the workflow.** Auto-merge only asks GitHub
  to merge *when the required checks pass*. `main` requires the CI check, and
  removing that requirement would turn every one of these into an instant
  unverified merge. The two are a pair, so do not simplify one without the
  other.
- **Both `FROM` lines are pinned by tag and digest**, and the docker ecosystem
  is what keeps the digest current. A digest pin with nothing updating it
  freezes the image on a base that has since been patched, which is worse than
  not pinning at all.
- **`gateway/requirements.in` is the intent, `gateway/requirements.txt` is the
  compiled pin set** and the only thing the Dockerfile installs. Edit the `.in`
  and recompile; editing the `.txt` by hand is undone by the next run.
- **TypeScript is capped below 7 on purpose.** The native port does not expose
  `typescript.sys`, which `@remotion/bundler`'s esbuild loader reads, so the
  project typechecks clean on 7 and then fails to bundle at all. The Dependabot
  `ignore` entry is what holds it at 6.0.x. This is also why CI bundles as well
  as typechecks: `tsc` alone is green on the broken combination.
- **`uvicorn[standard]` is deliberately not used.** It pulls websockets,
  watchfiles and pyyaml, none of which the gateway imports, and `uvloop` plus
  `httptools` are named directly instead. That is ~9 MB off an image that goes
  to a public registry.

## Working on this repo

- **`git pull` before starting anything.** This checkout is not the only thing
  committing to it. The 02:00 and 05:00 sessions run on the Linux host against
  their own clone, and anything they push is not here until you fetch it.
  Starting from a stale `main` means a conflict at the end of the work rather
  than at the start, or worse, quietly reverting a fix that already landed.
  Dependabot and the weekly recompile job push here too, so a checkout left
  alone for a few days is behind by default.
- `pipeline/models.py` holds the only interface between stages. Change a field
  there and mirror it in `video/src/schema.ts`.
- Stages re-use artifacts already on disk. To force a regeneration, move the run
  folder aside rather than deleting it.
- `pytest` and `ruff check` before considering a change done. Neither is in the
  venv on a render host: `pod-setup.sh` installs `requirements.txt`, which is
  the runtime set, and adding unpinned test tooling to a compiled pin set is
  how the CUDA torch got in. Run them alongside it instead, with
  `uvx ruff check .` and `uv run --with pytest pytest`. The gateway suite needs
  `fastapi` and `aiosqlite`, which the pipeline venv deliberately lacks, so on
  a render host it is `pytest --ignore-glob="tests/test_gateway_*"`; CI runs
  the whole thing.
- Rendering does not start the repo cooldown. `main.py --posted <owner/repo>`
  does, and it is deliberately manual so a rejected video costs nothing. A
  finished render does tell the gateway it happened, which is a weaker thing
  and is undone by `--unmark`; see "Rendered is a second, weaker list".
- **A batch ranks once, not once per video.** `--batch N` fills the gateway's
  slots for the next few days from one sitting. It cannot rank per video, because the
  cooldown only starts at publish or enqueue, so a freshly rendered repo is
  still the top candidate and every run in the batch would pick it again.
  `--covered` prints the store; covered repos are dropped during discovery,
  before enrichment, so they cost no README fetch on their way to losing.
- **It runs on a Linux host too, not only the Mac.** `scripts/pod-setup.sh`
  builds both venvs and the node deps and is idempotent, so it is the answer to
  "how do I get this running somewhere else" rather than a list of commands in
  someone's terminal history. The private half arrives on a mount rather than
  in git, and the script links it into place. `data` has to be a *directory*
  symlink: `StarHistory.save()` renames a temp file over the target, and that
  rename replaces a file symlink with a real file, so per-file links silently
  send writes to local disk instead of the share.
- **A drifted venv shows up as a missing browser, not as a version error.**
  Playwright resolves a chromium build number pinned to its own version, so a
  venv one release behind `requirements.txt` asks for build 1228 while the host
  holds the 1234 the pinned version wants, and refuses to launch a chromium it
  did not pin. `capture_repo` treats that as optional and logs it, `renderer`
  falls back to the repo card, and the run finishes green having dropped the
  README hero, which is the whole reason the cover exists. Every Linux render
  did that unnoticed until a cover was looked at. Two guards, because the
  symptom pointed nowhere near the cause: `pod-setup.sh` installs the browser
  as a step of its own, and `pod-setup.sh --check` asks playwright which path
  it wants rather than whether some chromium exists.
- **`--history` is the view across all three records.** `--covered` answers
  "may discovery pick this", which is the scorer's question. `--history` joins
  the cooldown store, `GET /api/rendered` and the queue's publish dates, so it
  answers "have we talked about this and when did it go out". It merges the
  gateway's covered list in memory rather than writing it back, since a listing
  command has no business editing the account's cooldown store, and it takes
  publish
  dates from the queue rather than `/api/results`, which omits a post until it
  has a retention reading and would report this afternoon's Reel as never
  posted. Made and Posted are separate columns because the gap between them is
  the queue depth.
- **`--cohorts recipe|slot` compares groups instead of listing posts.** It is
  the payoff of the recipe: rows are comparable when their recipes match. The
  `slot` dimension buckets by the hour, because the scheduler jitters each slot
  by an offset derived from its id and the date, so grouping on the exact stamp
  gave 43 cohorts of one out of 58 posts. **Read the breakouts column, not the
  median**, since eight of the first 58 posts carried a third of all views, so
  the median describes the post that failed.
- **The evening slot underperforms and this is the strongest signal in the
  numbers so far.** Measured 2026-08-19, n=58, and the three main slots have the
  same median age so nothing is confounded by time. 06:00 UTC takes 63.6 percent
  median skip with 27 percent of posts over 500 views, 10:00 UTC takes 65.5 and
  22 percent, and 17:00 UTC takes 76.3 percent with 6 percent over 500. Paired
  within the same day, the evening post is worse on 13 of 19 days by a median
  6.3 points, which is a sign test p of 0.08 and therefore suggestive rather
  than settled. The medians hide it: all three sit near 150 views, and the whole
  difference is in the tail. Slot assignment comes from queue position rather
  than from anything about the video, so the groups are close to randomly
  assigned with respect to content, which is rare enough here to be worth using.
  Re-run `--cohorts slot` rather than trusting these numbers; they are here to
  say what to look at, not to be the answer. **This one was acted on**: the
  cadence went to one post a day on 2026-08-27 and the slot that survived is
  08:10 Europe/Oslo, which is the 06:00 UTC group above. So it is a finding
  already spent rather than an open one, and there is no evening slot left to
  compare anything against.
- **`scripts/pod-setup.sh --check` reporting DRIFT is not cosmetic.** It is a
  flag on that script rather than on `main.py`, unlike every other flag in this
  list. The venv it names is the one
  that renders tonight, and the failure above is what drift actually looked
  like from the outside: a warning in a log nobody reads, on a run that exits
  zero. Re-run `pod-setup.sh` when it says so; it rebuilds on mismatch.
- **A scheduled render needs a ceiling.** `--max-queue N` asks the gateway how
  many posts are already waiting and skips the batch when the line is at least
  that long, because one slot a day drains slower than a nightly job fills.
  An unreachable gateway is refusal, not zero: the two are opposite answers and
  guessing wrong costs a batch. `--recover` honours the same ceiling and makes
  the same refusal, because it runs after something has already gone wrong.
- **This repo is public.** `PROFILE.md`, `PLAN.md`, `.env`, `accounts/` and the
  voice recording are gitignored and hold the private half. Before adding a
  file, decide which half it belongs to. `scripts/backup-secrets.sh` backs up
  the private half, driven by the `# backup:start` block in `.gitignore`.
- **The laptop and the render host read the same share, and
  `scripts/sync-private.sh` is what moves anything between them.**
  `/mnt/reelsmith` on the render host is
  `nas.local.bigd.no:/volume1/shared-data` at subPath `media/reelsmith`, and
  this laptop mounts that share over SMB the way `backup-secrets.sh` already
  does. There was never a network gap between them, only two names for the same
  bytes and no command that said so, which is how the copy on the share reached
  eighteen days stale while the nightly wrote copy against it.
- **It reaches that share three ways and takes the first that answers**, since
  2026-08-27. `--via pod` runs `kubectl exec` against the verksted pod, which
  has the share mounted already, so it needs no NAS password and works off the
  LAN; `--via dir` uses a mount you made yourself through `NAS_DIR`; `--via
  smb` is the original `mount_smbfs` route and is the only one that survives
  the pod being down. The log line says which it took. This is not
  belt-and-braces: SMB auth started failing with `server rejected the
  connection: Authentication error` while the share itself was fine and the
  render host was reading it throughout, and a sync that works only while one
  password is remembered fails on exactly the day it is needed, which is the
  same shape as the eighteen day old copy above. The pod route moves bytes as
  base64 through `sh -c` rather than `kubectl cp`, which shells out to tar and
  reports a partial copy as success.
- **Everything is written beside its target and then read back and compared.**
  One verify loop covers both directions, because "do the two sides agree" is
  the same question either way, and a truncated write is silent without it.
  Nothing is ever deleted, so a failed verify is a re-run.
- **The account `.env` is projected onto the share, not copied.** It is the
  one file in scope that is partly host specific, in the way the root `.env` is
  refused for. This laptop's copy holds `IG_ACCESS_TOKEN` and three `YOUTUBE_*`
  credentials because it can publish directly; the render host enqueues, the
  gateway publishes and holds its own, and that host's `data/` has never had an
  `ig_token.json`. `ENV_PROJECTED_KEYS` in the script is what crosses, and it
  is an **allowlist rather than a list of secrets to strip**, because a
  security boundary is worth having only if it fails closed: a key nobody
  considered stays here rather than travelling because it did not look like a
  secret. The keys left behind are printed, so a new id nobody allowlisted
  reads as a line rather than as a destination the render host quietly skips.
  `tests/test_sync_private.py` asserts on the absence of the secrets, because a
  leak here fails nothing and looks exactly like a successful sync.
- **`--pull` refuses that file, and this was a live footgun.** The far copy is
  a projection and holds strictly less, so pulling it over the authored one
  destroys four credentials on the only machine that has them. The refusal is
  that one file, not the mode; `PROFILE.md` and the voice still come back.
- **The private half splits by who writes it, and that split is the whole
  safety property.** `PROFILE.md`, `accounts/*/.env` and `accounts/*/ref/*` are
  authored here and pushed. `accounts/*/data/*.json` is written by the render
  host nightly and is **refused**, not skipped: its `used_repos.json` and
  `star_history.json` are the live ones, and pushing from here hands the
  nightly a cooldown list missing a month of repos. Those two are not meant to
  be file-synced at all, because `_sync_covered` folds `GET /api/covered` in
  before every discovery run and the gateway is what reconciles them. The root
  `.env` is refused as host specific, and the thinking documents because they
  are written on both sides.
