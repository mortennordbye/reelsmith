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

`--cover-url` is the seam for a hosted cover. Meta cURLs that URL, so it cannot
be a local path. Without it the thumbnail comes from `thumb_offset` at
`COVER_FRAME`, which is the same moment `cover.png` renders, so the fallback
loses the hook band and nothing else.

### The nightly run is not in this repo

`launchd/` is the Mac story and drives nothing on the Linux host. There the
02:00 run is a scheduled agent session whose whole behaviour is one prompt,
held outside git, so looking for the schedule in this checkout finds nothing
and editing this checkout cannot change what fires tonight. Anything about it
that is worth knowing has to be written down here instead, which is what this
section is.

- **It arms what it renders.** The nightly enqueues with `--approve`, so a
  finished Reel goes straight into the gateway's schedule and the next free
  slot publishes it. The alternative was a draft, which waits for somebody to
  watch it, and a draft queued at 02:00 waits until somebody remembers it
  exists. Three slots a day drain a queue faster than anyone reliably reviews
  one.
- **It renders four a night against three slots, up to a queue of ten.**
  `--batch 4 --max-queue 10`, and the surplus is the point: the queue is meant
  to sit about three days deep so a night that produces nothing is absorbed
  rather than showing up as a gap on the feed. A power cut, a wedged pod, or
  four scripts that all trip the dash validator then costs the account nothing.
  Because `--max-queue` clamps the batch to the room left, most nights render
  one or two and stop on their own. A ten-post queue is the case
  `db.live_media_names` already exists to protect, so nothing on the gateway
  side has to change to hold one.
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
- **`queue_depth` is filled in at scrape time**, in the `/metrics` route rather
  than from the scheduler tick. It is a gauge describing a table, so the honest
  value is what the table says now; and the scheduler is off by default, which
  would have left the gauge empty on exactly the deployments where a stuck post
  goes unnoticed longest.
- **Publish a series per state including the empty ones.** A gauge that only
  reports what exists leaves `failed` at its last non-zero value forever, and
  the alert that fired on it never resolves.

## The cooldown list, on both sides

`data/used_repos.json` is what discovery reads, and it is one JSON file on one
laptop, outside git and outside every backup here. `GET /api/covered` hands the
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

### Rendered is a second, weaker list

`GET /api/rendered` is the repos a video already exists for, and it is
deliberately not part of `/api/covered`. A commitment is irreversible and starts
a 30 day cooldown; a render is neither, and folding the two together would merge
"I built this and have not watched it yet" into `data/used_repos.json` and block
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
  siblings are normal. `_hooks_by_repo` ranks them: an unsuffixed name first,
  since `RepoCandidate.slug` turns every dot into a hyphen and a dot can only
  come from a human; then a publish receipt; then the later date. Taking the
  last folder in sorted order instead reported a rejected draft as the hook that
  scored 79.5 percent, which nothing downstream could have caught.
- **The block states the benchmark next to the list.** Every hook so far skipped
  64 to 80 percent of viewers against a 30 to 40 percent average for the format,
  so the top of a sorted list is still a bad hook and the prompt has to say so or
  the model copies it.
- **A post that skipped the pipeline joins on its caption instead.**
  `main.py --backfill` lists the account's live Reels against the run folders
  and `--yes` registers the ones it matched. The join is the first paragraph of
  the caption, because the gateway appends the comment ask above the hashtags on
  one publish path and not the other, so the body is the only part written once.
  A body two run folders claim matches neither, for the same reason
  `_hooks_by_repo` ranks them.
- **A backfilled post is registered to be measured, never to be answered.**
  `poll_comments=False`, because registering is also what arms the comment
  poller and the post is days old by then. The flag is decided when the row is
  inserted and a re-registration never changes it, so running the backfill twice
  cannot disarm a live post. A gateway older than schema v8 drops the field
  silently and arms the poller anyway, which is why the reply says "measuring"
  rather than "watching" and the pipeline reads it.
- Failure is an empty list everywhere. A gateway that is down costs a run its
  hindsight, which is what every run had before this existed.

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

- `pipeline/models.py` holds the only interface between stages. Change a field
  there and mirror it in `video/src/schema.ts`.
- Stages re-use artifacts already on disk. To force a regeneration, move the run
  folder aside rather than deleting it.
- `pytest` and `ruff check` before considering a change done.
- Rendering does not start the repo cooldown. `main.py --posted <owner/repo>`
  does, and it is deliberately manual so a rejected video costs nothing. A
  finished render does tell the gateway it happened, which is a weaker thing
  and is undone by `--unmark`; see "Rendered is a second, weaker list".
- **A batch ranks once, not once per video.** `--batch N` fills the gateway's
  three daily slots from one sitting. It cannot rank per video, because the
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
- **A scheduled render needs a ceiling.** `--max-queue N` asks the gateway how
  many posts are already waiting and skips the batch when the line is at least
  that long, because three slots a day drain slower than a nightly job fills.
  An unreachable gateway is refusal, not zero: the two are opposite answers and
  guessing wrong costs a batch.
- **This repo is public.** `PROFILE.md`, `PLAN.md`, `.env`, `data/` and the
  voice recording are gitignored and hold the private half. Before adding a
  file, decide which half it belongs to. `scripts/backup-secrets.sh` backs up
  the private half, driven by the `# backup:start` block in `.gitignore`.
