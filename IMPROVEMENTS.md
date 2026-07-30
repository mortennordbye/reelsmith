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

- [~] **Close the last manual step: posting.**
  Cheap version done: the render now copies `caption_text` to the clipboard and
  opens the run dir (`pipeline/publisher.py`, best-effort — a failed clipboard
  write never fails a run that produced a video).
  Full version **not** done. Instagram Graph API Reels publishing needs a
  business/creator account and the MP4 at a public URL, so it means standing up
  object storage (presigned S3/R2) and a Meta app before the first post. That
  is an infrastructure decision, not a code one. `publisher.py` is where it
  goes when you want it.

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
