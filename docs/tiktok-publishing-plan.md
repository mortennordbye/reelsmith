# Publishing to TikTok from the gateway

**Executed as of 2026-08-27.** Every phase below is built, deployed and
authorised, on the inbox path and against the app's sandbox credentials. The
Direct Post audit is not, and on the evidence never will be: the production
configuration cannot be saved without a demo video of a posting interface this
project does not have.

Kept as the record of why the design is shaped the way it is. **It is not the
guide.** `docs/tiktok-api-setup.md` opens with the ordered runbook and the
portal traps, and its *What actually happened when this was attempted* section
is the one to read before touching the portal. `docs/multi-destination-audit.md`
is what the code cost, and finding numbers below refer to it.

Two things below were written as open questions and are now answered, so read
them as history rather than as instructions: the four items under "what could
not be verified" are resolved in the setup doc under *What was unknown, and what
is still*, and the split recommendation on the audit resolved to the unaudited
half, exactly as this plan hedged for.

## Handover

Written while the operator was out, so this section carries what would otherwise
have been a conversation.

**What was verified**, on 2026-08-26 against TikTok's live docs, with links in
`docs/tiktok-api-setup.md`: the endpoints and their rate limits, the token
lifetimes and the refresh token rotation, the media limits against what the
renderer produces, the full `post_info` field list, the unaudited restrictions
and the error code that enforces them, the UX specification the audit reviews,
and that no retention or watch time metric exists on any TikTok API.

**What could not be verified** without a logged in developer account: whether
individual registration suffices at audit time, the exact DNS record for domain
verification, whether the audit form asks for a creator cap, and whether Direct
Post configuration is available before approval. All four are collected under
"What was unknown, and what is still" in the setup doc, where three of the four
are now answered.

**In the code**, four claims from `PLAN.md` H were re-checked and all four hold,
and one thing was found that nobody was looking for: **registering a second
Instagram account deletes the first account's schedule at boot**, quietly, with
one warning line. That is F0 in the audit and it is the reason the second
account cannot be started casually. It is also two lines of defence to prevent.

**The judgement calls I made**, each reversible:

- **The recommendation on TikTok is split rather than yes or no.** The Direct
  Post audit is worth submitting and is unlikely to pass as things stand; the
  reasoning is in `docs/tiktok-api-setup.md` under "The audit, read honestly",
  and the short version is that TikTok's own guidelines say no unattended
  posting and reject apps designed for personal use. Rather than return nothing,
  the plan below builds one publisher that serves both the audited and the
  unaudited path, so a refusal costs a config flag.
- **TikTok numbers do not reach `_results_block`**, following the rule
  `PLAN.md` H6 set for YouTube. TikTok exposes no three second equivalent and no
  watch time at all, so there is nothing to substitute.
- **The `ig_user_id` to `account_id` rename lands after `--account` and before
  TikTok's tables**, as a change with no behaviour in it.
- **A second cloned voice for account 2.** `PROFILE.md` is explicit that sharing
  one across two accounts meant to look unrelated is the strongest link between
  them, and a second reference recording is 25 seconds and `CHATTERBOX_REF`.

**The first three things to do, in order:**

1. Read "The audit, read honestly" and decide whether to submit at all. It is
   free and blocks nothing, so the default answer is yes.
2. Put an explicit `account=` on every line of `GATEWAY_SLOTS` in the Homelab
   ConfigMap. This is F0's cheap half and it is worth doing this week whether or
   not anything else here happens.
3. Approve or reject phase 1 below, which is `--account` and has nothing to do
   with TikTok.

## What already carries over

Most of the machinery is not about Meta. The queue, the draft and approved
states, per account slots with derived jitter, the two claims, the retry rules,
the admin panel, the metrics and the backups all work on any destination. Only
the publish call is platform shaped.

Two things make TikTok the easier of the two remaining platforms and one makes
it harder.

- **The media seam already exists and TikTok wants exactly it.**
  `PULL_FROM_URL` fetches the MP4 from a URL, which is the same model Meta uses
  and the reason `GET /media/{name}` is public at all. Nothing new gets hosted.
  One DNS record verifies the domain.
- **`publish_id` fits the existing retry rule with no new column.** YouTube put
  its resumable session URI in `container_id` because the meaning is identical:
  no id means nothing was created upstream and the slot gets its turn back, an
  id means something may exist and the row stops in `failed` for a person.
  TikTok's `publish_id` is the same object.
- **The refresh token rotates**, which neither current platform does. See
  phase 3.

## The account model

Unchanged from the YouTube decision, and it holds for a third platform without
argument. `accounts.ig_user_id` is an opaque account key and holds a TikTok open
id on a TikTok row; `accounts.platform` says which. Credentials get their own
table rather than more nullable columns, for the same reason
`youtube_credentials` does: Meta's shape is a token plus an expiry, Google's is a
client pair plus a stable refresh token, and TikTok's is a client pair plus a
refresh token **with an expiry that must be rewritten on every use**. One table
holding all three would be two thirds null on every row.

The one thing that does change is that `ig_user_id` will then be lying on two of
three platforms, which is F10.

## Phases

Sequenced so the thing with weeks of external latency starts first and the thing
with no external gate is done while there is little to be wrong about.

**0. Submit the audit, and separately verify the four unknowns.** Not code.
Sandbox first, since a first time app is required to demo from one. Read
`docs/tiktok-api-setup.md` end to end before filling anything in, because the
form asks for scopes and a creator cap and both are easier to get right once.
*Verify:* the confirmation mail, and the four "what only you can check" items
answered in that same session.

**1. `--account` in the pipeline. One account, behaviour unchanged.** This is
F4 and it is the largest single piece of work in this document. It has nothing
to do with TikTok and everything to do with the second account, and it goes
first because every later phase touches the same call sites.

`--account <name>` resolves before `get_settings()` is built, since that is
`lru_cache`d and every stage already reads one `Settings`. A profile is an `.env`
fragment plus a data directory: `ig_user_id`, `ig_token_path`, the channel and
open ids, `PROFILE.md`, `CHATTERBOX_REF`, `used_repos_path` and a `build_dir`
subtree. Global stays global: `github_token`, the model knobs, the validators,
the video dimensions, the gateway URL and token.

Carries F9 and F8 with it, because they are the same change: the cooldown store
becomes per account, and `fetch_covered` starts sending the account's own id so
two accounts stop treating each other's repos as covered.
*Verify:* the whole suite green with no `--account` given, which is the real
claim. Then `--account` naming the current profile produces byte identical
`repo.json` and `script.json` for a `--repo` run.
*Rollback:* the flag defaults to the existing single profile, so not passing it
is the old behaviour.

**2. F0, before any second account exists anywhere.** Explicit `account=` on
every `GATEWAY_SLOTS` line, and `_apply_config_slots` refusing to visit an
account with an empty list when the ambiguity was unresolved rather than
treating "I could not tell" as "there are none".
*Verify:* a test that registers a second Instagram account and asserts the first
one's config slots survive. That test is the whole point of the phase.
*Rollback:* none needed, it is strictly more conservative.

**3. `gateway/tiktok.py`, plus the credentials table and the refresher.**
Raw `httpx`, no vendor SDK, following `gateway/youtube.py`'s reasoning: three
documented calls do not justify a synchronous client library inside an async
service.

Migration adds `PLATFORM_TIKTOK` and `tiktok_credentials` with
`open_id`, `client_key`, `client_secret`, `refresh_token`, `refresh_expires_at`.

The publisher is `creator_info` then `video/init` then poll `status/fetch`, and
it is written so the two paths differ by one field: Direct Post sends
`post_info` to `/v2/post/publish/video/init/`, the inbox path sends none to
`/v2/post/publish/inbox/video/init/`, and success is `PUBLISH_COMPLETE` on the
first and `SEND_TO_USER_INBOX` on the second. That is the hedge against the
audit, and it is cheap only if it is built in from the start.

`creator_info` is not optional and its result is not decoration: the
`privacy_level` sent must come from the `privacy_level_options` it returned, or
the post fails `privacy_level_option_mismatch`.

**The refresher is the new mechanism.** A loop beside
`poller.refresh_tokens_once`, running daily, that persists the returned
`refresh_token` in the same transaction that uses it. F2. A dropped write here
is not a bad day, it is an account that cannot be recovered without a browser.
*Verify:* unit tests against a stubbed transport, mirroring
`tests/test_gateway_youtube_publish.py`, plus one test that a refresh returning a
different token stores the new one and one that a `publish_id` is committed
before anything is polled.

**4. Dispatch, panel, metrics.** The scheduler branch, and F1 in the same
change: match the platform explicitly and raise on an unknown one instead of
falling through to Instagram. `POST /api/accounts/tiktok`. An icon and a hue.
F7's `platform` label on `posts_published` and `publish_failures`, and an
`account` label on `queue_depth`, publishing every series including the empty
ones.
*Verify:* `tests/test_gateway_tiktok_accounts.py` asserting what the Meta loops
do not see, in the shape of the YouTube one, plus a test that a row for an
unknown platform fails its own row rather than reaching another platform's API.

**5. The Mac side.** `--enqueue` creates a third queue row. `/api/media` is
content addressed, so one MP4 still uploads once for three rows.

The caption is a third shape: TikTok has one `title` field of 2,200 UTF-16 runes
carrying the ask, the link and the hashtags together, so it is neither
Instagram's `caption.txt` nor `youtube_description`. It belongs next to them in
`pipeline/gateway.py`.

**Which render goes to TikTok is a real question, not a detail.** YouTube gets
`out-no-cta.mp4` because a follow ask reads wrong there. TikTok is a feed like
Instagram's and the ask reads the same way, so the default is `out.mp4`. Say so
in the code rather than letting it be whichever variable was nearest.

`--max-queue` counts Instagram only and deliberately (`pipeline/gateway.py:524`).
A third queue draining at its own rate does not change that reasoning: the
ceiling asks whether the feed is stocked, and the feed is Instagram.
*Verify:* one render produces three queue rows and one media file.

**6. Insights.** F5. `platform` on `insights`, a TikTok sweep writing views,
likes, comments and shares from `/v2/video/query/`, and `/api/results` left
Instagram only so the feedback loop cannot be corrupted by a metric that means
something else. The sweep needs a list and match step, because the `publish_id`
returned at post time is not a video id.
*Verify:* a TikTok post appears on the Posts page with counts and no skip rate,
and `--cohorts` output is byte identical to before.

**7. Config and rollout.** `GATEWAY_TIKTOK_*`, off by default for the same
reason `scheduler_enabled` is: publishing to a third real account should be a
decision rather than something gained by upgrading. `privacy_level` explicit in
config and set to `SELF_ONLY` until the audit lands, so the pre audit behaviour
is chosen rather than discovered, exactly as `youtube_privacy_status` was.
`is_aigc` decided and the reasoning written down, in the same breath as
revisiting `containsSyntheticMedia`, because it is the same question.

While checking that, note that `GATEWAY_YOUTUBE_ENABLED` is named in
`docs/youtube-publishing-plan.md:106` and **does not exist in
`gateway/config.py`**. Decide whether to add it or to stop claiming it.

**8. F10, the rename.** `ig_user_id` to `account_id`, no behaviour, the suite as
the whole proof. After 1 and 2, before 3's tables land if possible, because
every table added first is another table to rename.

**9. Account 2.** Which is the test that phase 1 worked, and is config.

## Three things that will otherwise bite

**Order matters between the ConfigMap and the image, again.** `parse_slots`
fails the boot on a line it cannot read, which is correct, and it is why
`account=` had to land in an image before it landed in `GATEWAY_SLOTS` for
YouTube. Doing it the other way crashlooped the pod on 2026-08-15. Phase 2 moves
in the same order for the same reason.

**A green build is not a deploy.** Kargo's Warehouse fails discovery entirely on
the first unreadable tag and nothing promotes while every check reports success.
That swallowed three merged PRs once already. If a change does not appear in the
cluster, check the Warehouse conditions before anything else.

**Passing the audit does not backfill.** Anything posted while unaudited stays
private and no later approval republishes it, so there is nothing to gain from
running real videos through the unaudited path except proving the wiring. Prove
it with one, not with a week of them.
