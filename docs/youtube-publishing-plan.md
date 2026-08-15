# Publishing to YouTube from the gateway

**Executed as of 2026-08-15.** Every phase below is built and deployed except
the audit, which is not code. Kept as the record of why the design is shaped
the way it is; `docs/youtube-handover.md` is what remains open.

The plan for putting Shorts on the same footing Reels are already on: rendered on
the Mac, queued to the gateway, published unattended from the cluster. The API
side of this is in `docs/youtube-api-setup.md`, which covers the account layout,
the audit and the OAuth traps. This file is what changes in the code.

**Scope for now is uploading.** The YouTube Analytics feedback loop is out. The
`yt-analytics.readonly` scope is still requested in the one-time authorisation,
because adding a scope later means going back through the browser, but nothing
reads it yet.

## What already carries over

Most of the machinery is not Meta-specific. The queue, the draft/approved
states, per-account slots with derived jitter, the two claims, the retry rules,
the admin UI, the metrics and the backups all work on any destination. Only the
publish call itself is Instagram.

Two things make YouTube the easier of the two:

- **No public URL is needed.** Meta fetches the MP4, which is why `/media/*` is
  public at all. YouTube takes pushed bytes, and the gateway already has the
  file on disk in `media_dir`. Nothing new gets hosted.
- **Refresh tokens do not expire on a clock.** There is no equivalent of the 60
  day Meta token and no equivalent of the `--refresh-token` margin job.

## The account model

Everything in `gateway/db.py` is keyed on `ig_user_id`: accounts, queued_posts,
schedule_slots, slot_fires, insights, posts. A second destination either fits
that or fights it.

It fits. Add `platform TEXT NOT NULL DEFAULT 'instagram'` to `accounts` and
register the channel as a second account row whose `ig_user_id` holds the
channel id. That column becomes an opaque account key rather than a Meta id, and
the queue, slots, claims, jitter, admin UI and backups all keep working with no
further change.

The cost is a column whose name now lies on some rows. That is worth paying
rather than renaming `ig_user_id` across 1,614 lines of `db.py`, `admin.py` and
the test suite in the same change. A rename to `account_id` is a clean and
purely mechanical follow-up if it starts to grate.

Credentials get their own table rather than sharing `accounts`. Meta's shape is
`access_token` plus `token_expires_at`; YouTube's is `client_id`,
`client_secret`, `refresh_token` and the channel. One table holding both would
be half null on every row.

Three paths have to learn that not every account is an Instagram one:
`db.active_accounts` behind the comment poller, the insights sweep, and the DM
webhook path. A filter missed in any of them shows up as a Graph call carrying a
YouTube channel id.

## Phases

**0. Submit the audit, before writing anything.** Every video uploaded through
`videos.insert` from an unaudited project is locked to private, permanently and
unfixably in Studio. It reviews the project rather than a video, so it can sit
in the queue while the rest is built. Verify: the confirmation mail.

**1. Account plumbing.** Migration v10 adds `accounts.platform` and a
`youtube_credentials` table. `POST /api/accounts` grows a platform
discriminator. The poller and the insights sweep filter to
`platform='instagram'`.
Verify: the existing suite stays green, and a registered YouTube account is
invisible to the poller.

**2. `gateway/youtube.py`.** Token refresh, one POST to `oauth2.googleapis.com`,
and the resumable upload: initiate, PUT the bytes, read back the video resource.

Raw `httpx` rather than `google-api-python-client`. The client library is
synchronous and would need a thread pool inside an async service, in exchange
for wrapping three fully documented calls. If the OAuth handling ever grows
past one refresh POST, revisit it.

The retry-safety rule ports exactly, with the **resumable session URI standing
in for the container id**. No URI yet means Google was never asked to make
anything, so the slot gets its turn back. A URI exists means a video may be
live and no error text proves otherwise, so the row stops in `failed` and waits
for a person. Store it in the existing `container_id` column: same meaning, same
rule, no new column.
Verify: unit tests against a stubbed transport, mirroring
`tests/test_publisher.py`.

**3. Dispatch.** `scheduler.publish_queued` branches on `account["platform"]`.
The Instagram branch is not touched.
Verify: a YouTube row publishes in the harness, and an Instagram row behaves
identically to before.

**4. The Mac side.** `--enqueue` creates a second queue row against the YouTube
account. `/api/media` is content-addressed by digest, so the same MP4 uploads
once and both rows point at it.

Title and description are built on the Mac, where `strip_written_cta` and
`PROFILE.md` already live. The hook is the title and is already capped at 60
characters by `max_hook_chars`, inside YouTube's 100. The keyword mechanic has
no equivalent there, so the link goes in the description directly and the
written call to action is stripped.
Verify: one render produces two queue rows and one media file.

**5. Config and rollout.** `GATEWAY_YOUTUBE_ENABLED`, off by default for the
same reason `scheduler_enabled` is: publishing to a second real account should
be a decision rather than something gained by upgrading. `privacyStatus`
explicit in config and set to `private` until the audit lands, so the pre-audit
behaviour is chosen rather than discovered. `containsSyntheticMedia` decided and
the reasoning written down. A Homelab PR for the ConfigMap and the secret.

**The slot config needs work here, and more of it than one extra account
implies.** More channels are expected after the first, so this is the one part
of the design that does not already scale.

Everything else does. A channel is an account row with its own credentials, its
own slots, its own queue and its own claims, and nothing in the queue, the
scheduler or the panel is written for exactly one of anything. `GATEWAY_SLOTS`
is the exception twice over: it holds slots for a single account, and with no
`GATEWAY_SLOTS_ACCOUNT` it resolves that account by there being exactly one,
which stops meaning anything the moment there are three.

So `parse_slots` needs an optional `account=` token per line, and the
resolve-by-count fallback should stay only as the single-account convenience it
already is. Doing it when the second channel arrives means a rollout that
quietly leaves a channel with no schedule, which is the failure the
fails-the-boot rule everywhere else in this file exists to prevent.

Cadence starts below the Instagram one. The exposure worth managing is the
pattern rather than any single video, and three a day of an identical layout is
the shape reviewers are hunting even when each video is original. It is config,
so it moves without a code deploy.

## Two things that will otherwise bite

**The refresh token does not belong in `data/yt_token.json`.**
`docs/youtube-api-setup.md` step 6 says to put it there beside
`data/ig_token.json`, which was written when publishing was a Mac-side job.
Since it moves to the gateway, the one-time browser authorisation can happen
anywhere but the token lands in the cluster secret. That doc needs amending in
the same change, or the two will disagree at the worst moment.

**YouTube numbers must not reach `_results_block`.** Out of scope now, but worth
recording so it is not discovered later. `skip_rate` scores the opening three
seconds alone, which is the signal the scriptwriter is tuned on.
`averageViewPercentage` scores the whole video. Feeding both into the same block
would corrupt the one measurement the loop depends on, so the analytics work,
when it happens, needs its own column and its own prompt block.
