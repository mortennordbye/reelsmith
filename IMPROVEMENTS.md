# Improvement list

Ranked by value. Check items off as they land.

## High value

- [x] **Daily star snapshot job.**
  Velocity is 55% of the candidate score, but it is only *measured* when a
  discovery run happened the day before; otherwise it falls back to the damped
  stars/day proxy, which favors established repos over real breakouts.
  Add a lightweight snapshot-only entry point (search + `StarHistory.record`,
  skip enrichment/scoring) and run it daily via launchd. Result: from day two
  onward every ranking uses measured deltas.
  Landed as `scraper.snapshot_stars` + `main.py --snapshot`, with
  `launchd/it.nordbye.tech-ig.snapshot.plist` (06:00 daily, `RunAtLoad` so a
  sleeping laptop catches up). Install commands are in the plist header.

- [x] **Test suite for the pure logic.**
  70 tests in `tests/`, no network and no fixtures. `pytest` and `ruff` are in
  a new `[project.optional-dependencies] dev` extra; `pip install -e ".[dev]"`
  then `pytest`.
  - `spec._align_to_captions` / `spec._allocate_frames` — `tests/test_spec.py`
  - `scraper._readme_quality` / `score_candidates` / `_is_relevant` /
    `_cold_start_velocity` / `UsedRepos` — `tests/test_scraper.py`
  - `StarHistory` — `tests/test_star_history.py`
  - `captions._repair_gaps` — `tests/test_captions.py`
  - the two contracts that used to drift — `tests/test_contracts.py`

- [x] **Close the last manual step: posting.**
  `publisher.publish_reel` uploads and publishes; `main.py --post` does it at
  the end of a render, `--publish <date>/<slug>` does it for a run you have
  already watched, and both start the cooldown themselves.
  The infrastructure this was blocked on turned out not to be needed. App
  Review only gates acting on accounts you do not own, so an app in development
  mode with your own account as a tester publishes fine; and
  `upload_type=resumable` takes the MP4 as raw bytes, so there is no object
  storage. Both claims were wrong in this file and in `INSTAGRAM.md`, and both
  are corrected there now.
  Left over: `cover_url` is still fetched by Meta, so a designed cover needs
  hosting. Without `--cover-url` the thumbnail falls back to `thumb_offset` at
  the same frame `cover.png` uses, minus the hook band.

- [x] **Schedule the full run.** `launchd/it.nordbye.tech-ig.daily.plist`,
  07:00, an hour behind the snapshot job so the ranking has today's stars.
  Ships *without* `--post` on purpose: the reviewed path renders overnight and
  waits for a `--publish`. Adding one line makes it unattended.
  Token upkeep rides on the existing `--snapshot` job, because a long-lived
  token that nobody refreshed for 60 days is dead and needs a browser to
  replace. That job is now load bearing for posting, not just for scoring.

## Correctness

- [x] **Cooldown starts at render, not at posting.** `mark_featured` is no
  longer called by the render step. `main.py --posted <owner/repo>` starts the
  cooldown, `--unmark <owner/repo>` clears it, and the render's closing output
  tells you which command to run.

- [x] **Stale contract claim in `pipeline/models.py`.** Fixed the way the
  docstring promised: `video/src/schema.ts` is now a real zod mirror of
  `VideoSpec`, parsed in `calculateMetadata` before the first frame.
  `video/src/types.ts` infers its types from those schemas, so there is one
  definition per shape. Verified both directions against a real `video.json` —
  a valid spec renders, a renamed field aborts the render naming the field.

- [x] **Hook length contract mismatch.** One number, `Settings.max_hook_chars`
  (60). `pipeline/models.py` reads it at import for both the JSON Schema
  description handed to Claude and the validator that checks his answer;
  `scriptwriter.py` interpolates the same value into the prompt.
  `tests/test_contracts.py` fails if the two ever disagree again.

## Housekeeping

- [x] Removed dead code: `renderer.stage_audio`.
- [x] `video/public/` is pruned before each render (`renderer.prune_staged_assets`).
      Matching is narrow on purpose — only the exact filenames `stage_asset`
      writes — so anything dropped in there by hand survives.
- [x] `--repo` runs now record a star snapshot (`scraper.record_snapshot`).
- [x] `main.py --candidates` calls the public `scraper.inspect_candidates()`.

## Left over

- `video/public/astral-sh-uv.mp3` predates the current `<slug>-<name>` staging
  convention, so the pruner does not recognise it and leaves it alone. Delete
  it by hand if you want the directory clean.
