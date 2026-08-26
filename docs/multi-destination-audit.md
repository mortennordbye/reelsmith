# Three destinations and two accounts, audited

Written 2026-08-26 against `main` at `d6393b7`. Every claim below was read in the
code rather than taken from `PLAN.md`, because section H was written on
2026-08-02 and predates the YouTube change that answered half of it.

The job is two questions that are usually confused with each other:

- **A third platform.** Instagram and YouTube publish today. TikTok would be the
  third. `docs/tiktok-api-setup.md` covers whether it can; this covers what the
  code costs.
- **A second account.** `SPINOFFS.md` and `HANDOVER.md` both stop at the same
  sentence, which is that `--account` does not exist.

They are not the same question and the code is in very different shape on each.
**The gateway is close to done on both. The pipeline is single account and knows
about a second destination only by accident.**

Every finding carries a verdict:

- **Works as is.** Verified, no change needed. Say so, because a generalisation
  written against something already general is wasted.
- **Mechanical.** Known shape, no decision to make, just work.
- **Decision.** Somebody has to choose, and the choice is named here.

## The four claims worth re-checking, checked

`PLAN.md` H asserted these before YouTube shipped. All four hold, and the first
one holds with a caveat that matters.

| Claim | Verdict |
|---|---|
| The scheduler is platform agnostic | **True.** The entire fork is one `if` at `gateway/scheduler.py:68`. Everything above it, `tick_once`, `run_slot`, both claims, the retry rule, is written about "a queue that publishes something". |
| `live_media_names` exempts every queued row regardless of account | **True.** `gateway/db.py:1069` filters on `state` only and has no account clause at all. A third platform's rows are exempt for free. |
| `rendered_repos` is account keyed | **True.** Primary key `(repo_full_name, ig_user_id)` at `gateway/db.py:280`, and `rendered_repos_list` filters `ig_user_id IN (?, '')` at `db.py:1636`. |
| The insights sweep and the poller filter by platform | **True.** `insights.refresh_once` calls `db.all_accounts(conn)` and `poller` calls `db.active_accounts(conn)`, both of which default to `platform='instagram'` (`db.py:571-590`). |

**The caveat on the first one is worth a finding of its own.** The dispatch is

```python
if account["platform"] == db.PLATFORM_YOUTUBE:
    return await publish_queued_youtube(...)
return await publish_queued_instagram(...)
```

Instagram is the fallthrough, so a `platform='tiktok'` row reaching the
scheduler before its branch exists **is handed to Meta's publisher**, which will
try to create a Reels container against a TikTok open id with an empty
`access_token`. That is the opposite of the fail safe design at `db.py:561-568`,
where the account readers default to Instagram *so that a missed call site is
inert*. Here the same default is fail open.

> **F1. The publish dispatch falls through to Instagram.**
> `gateway/scheduler.py:68`. **Mechanical**, and it should be fixed in the same
> change that adds `PLATFORM_TIKTOK` rather than after: match on the platform
> explicitly and raise on an unknown one, so a row for a platform with no
> publisher fails its own row instead of being misrouted into another
> platform's API.

## What already carries over, verified

Do not rebuild any of this. It was written about a queue, not about Meta.

- `queued_posts` and every column on it. `title` exists for YouTube and is empty
  on Instagram rows; `caption` doubles as the description. TikTok needs no new
  column: its caption and its title are one string.
- `schedule_slots`, `slot_fires`, and all of `gateway/schedule.py`. The jitter is
  derived from `sha256(slot_id:local_day:salt)` (`schedule.py:121`), survives
  restarts and replicas, skips round minutes, and adds the offset in UTC so DST
  cannot move a slot. Slots are already per account and a config line already
  takes `account=`.
- The two claims in order, slot fire then post, and the rule that retry is only
  handed back when nothing was created upstream. YouTube reused `container_id`
  to hold the resumable session URI rather than adding a column. **TikTok's
  `publish_id` is the same shape and belongs in the same column**, which is one
  fewer migration.
- `_prune_media` plus `live_media_names`, `POST /api/media` content addressed by
  digest, and `GET /media/{name}` served unauthenticated. TikTok's
  `PULL_FROM_URL` wants exactly this and needs nothing new hosted. One video
  going to three destinations is still one upload.
- The admin panel's scoping. `admin._scope` (`admin.py:211`) reads `?account=`
  and every page loops `scope["visible"]`, so a page never knows whether it is
  showing one account or all. `_account_heading.html` already switches on
  `account['platform']` for an icon and a colour.
- `covered_repos` and `rendered_repos` are repo level and platform agnostic.

## Adding a third platform

The YouTube feature landed as one squashed commit, `675c7cf`, touching 24 files.
That list is the checklist. Walked for TikTok:

| Stop | What it costs |
|---|---|
| `gateway/db.py` | `PLATFORM_TIKTOK` in `PLATFORMS`, plus a `tiktok_credentials` table. **Mechanical.** Its shape is not YouTube's: `client_key`, `client_secret`, `refresh_token`, `open_id`, and unlike Google's it needs `refresh_expires_at` and it is rewritten on every refresh, because TikTok rotates the refresh token. |
| `gateway/tiktok.py` | New, and the closest analogue is `gateway/youtube.py`. `creator_info` then `video/init` then poll `status/fetch`. **Mechanical**, but see F2. |
| `gateway/scheduler.py` | One more branch, and F1. **Mechanical.** |
| `gateway/api.py`, `models.py` | `POST /api/accounts/tiktok` mirroring the YouTube route, with a validator on the open id the way `_looks_like_a_channel_id` guards `UC`. **Mechanical.** |
| `gateway/poller.py` | **Decision, see F2.** A refresher loop that does not exist for either current platform. |
| `gateway/insights.py` | **Decision, see F5.** |
| `gateway/config.py`, `.env.example` | `GATEWAY_TIKTOK_*`. **Mechanical**, and note that `GATEWAY_YOUTUBE_ENABLED` is named in `docs/youtube-publishing-plan.md:106` and **does not exist in `gateway/config.py`**. Do not copy a flag that was never built; decide whether to add both. |
| `gateway/templates` | An icon, a hue, and F6. **Mechanical.** |
| `gateway/metrics.py` | **Decision, see F7.** |
| `main.py`, `pipeline/gateway.py` | A third caption shape and a third enqueue call. **Mechanical**, and F3 is where it gets interesting. |
| `scripts/tiktok_authorise.py` | Mirrors `scripts/youtube_authorise.py`, which posts the refresh token straight to the gateway so it never lands in a file. **Mechanical.** |
| `tests/` | `gateway_harness.FakeTikTok` alongside `FakeYouTube`, plus `test_gateway_tiktok_accounts.py` and `test_gateway_tiktok_publish.py`. The harness already chains fakes: `FakeMeta._handle` tries `self.youtube.handle` first and falls through (`gateway_harness.py:110`). A third link is two lines. **Mechanical.** |

> **F2. TikTok needs a token refresher and neither existing platform has one.**
> Instagram's refresh rides on the render host's `--snapshot` job
> (`main.py:272`), which `CLAUDE.md` already flags as load bearing for posting.
> YouTube needs none: Google refresh tokens have no clock and an access token is
> minted per publish (`gateway/youtube.py:89`). TikTok's access token is 24 hours
> and its refresh token **rotates on every use**, so a missed write loses the
> account permanently rather than for a day. **Decision:** it belongs next to
> `poller.refresh_tokens_once` (`poller.py:101`) as its own loop, and the loop
> must persist the returned refresh token in the same transaction that uses it.
> This is the one genuinely new mechanism a third platform brings.

> **F3. `_publish_run` and `_enqueue_run` are two definitions of posting, and
> only one of them knows a second destination exists.** `main.py:1225` publishes
> to Instagram directly and writes `published.json`. `main.py:1324` queues, fans
> out to YouTube at `main.py:1421`, and writes `queued.json`. So `--post` and
> `--batch --post` reach Instagram and nothing else, silently. Today that is
> defensible: the nightly uses `--enqueue`/`--recover` and `--publish` is the
> manual escape hatch. With three platforms the gap triples.
> **Decision:** say in `CLAUDE.md` that `--publish` is deliberately Instagram
> only and is the one path that does not fan out, or delete it in favour of
> `--enqueue --approve`. Either is fine. Leaving it undocumented is not, because
> the next person adding a platform will not find it.

## Adding a second account

This is where the work actually is.

`config.py` is one flat `Settings` over one `.env`, with `ig_user_id`,
`ig_token_path`, `youtube_channel_id`, `chatterbox_ref`, `used_repos_path` and
`build_dir` all resolved from module level constants. `require_instagram`
(`config.py:329`) is the only guard and there is no YouTube equivalent. `main.py`
has no `--account` flag.

> **F4. `--account` selects a profile, and the profile is larger than a
> credential set.** **Decision**, and it is the decision the whole second
> account waits on. What has to be per account, measured rather than guessed:
>
> | Per account | Global |
> |---|---|
> | `ig_user_id`, `ig_token_path` | `github_token` |
> | `youtube_channel_id`, TikTok open id | model, effort, research flags |
> | `PROFILE.md` and the editorial register | the dash and hype validators |
> | `chatterbox_ref`, the voice reference | `fps`, `width`, `height` |
> | `used_repos_path`, the cooldown store | `repo_cooldown_days` |
> | `build_dir` subtree | the Remotion project |
> | discovery queries and thresholds | `gateway_url`, `gateway_token` |
>
> The cheapest shape that fits the existing code is a per account `.env`
> fragment plus a per account data directory, selected by `--account <name>`
> before `get_settings()` is built, because `get_settings()` is `lru_cache`d
> (`config.py:306`) and everything downstream already reads a single `Settings`.
> No stage signature changes. `SPINOFFS.md` reaches the same conclusion from the
> content side and adds that `VideoSpec.repo: RepoMeta` is the one real
> interface break, which is a niche problem rather than an account problem and
> is out of scope here.

**The gateway needs almost nothing for a second account, with one exception that
is dangerous.**

> **F0. Registering a second Instagram account deletes the first one's
> schedule.** Highest severity finding in this document.
>
> `_apply_config_slots` (`gateway/app.py:46`) resolves slot lines that carry no
> `account=` token. With `GATEWAY_SLOTS_ACCOUNT` unset it falls back to "the
> single registered Instagram account when that is unambiguous"
> (`app.py:75-78`), which is `len(accounts) == 1`. Register a second Instagram
> account and that becomes false, `account` stays empty, and the unnamed lines
> are dropped with a `log.warning`.
>
> The drop alone would be survivable. The next block is not. `config_slot_accounts`
> (`db.py:1317`) returns every account holding config slots, and each is visited
> with a default of `[]` (`app.py:94-95`). `sync_config_slots` **replaces rather
> than merges** and deletes every config slot not in the wanted set
> (`db.py:1297-1299`). So account 1's three slots are deleted at boot, the pod
> starts healthy, and Instagram stops posting with one warning line in the log.
>
> **Reproduced on 2026-08-26**, not inferred. One Instagram account, three
> unnamed slot lines, `_apply_config_slots` run twice with a second Instagram
> account registered in between:
>
> ```
> INFO     Applied 3 slot(s) from config for 17841400000000000
> WARNING  3 slot line(s) name no account and it could not be resolved.
> INFO     Applied 0 slot(s) from config for 17841400000000000
> ```
>
> `active_slots` returns three rows before and zero after. The second `Applied
> 0` line is the deletion, and it is logged at INFO next to a warning that
> describes a different symptom.
>
> Every guard here is individually correct and the combination is not. The
> resolve by count exists so registering a YouTube channel could not break a
> working schedule, and it does that job; it just cannot survive a second row of
> its own platform.
>
> **Mechanical, and it is a prerequisite rather than a follow up.** Two lines of
> defence, both cheap: put an explicit `account=` on every line in
> `GATEWAY_SLOTS` before any second account is registered, and make
> `_apply_config_slots` refuse to visit an account with an empty list when the
> ambiguity was unresolved, rather than treating "I could not tell" as "there
> are none". `docs/youtube-handover.md` already records that ConfigMap ordering
> crashlooped the pod once; this is the same class of trap with a quieter
> failure.

## The cooldown machinery

`rendered_repos` is account keyed and safe. `covered_repos` is not a table: it is
a UNION over `queued_posts` and `posts`, deduped to the earliest date
(`db.py:1702`). It takes an optional `ig_user_id` and filters both halves when
given.

> **F8. The pipeline never passes `ig_user_id` when it reads the covered list.**
> `pipeline/gateway.py:493` calls `GET /api/covered` with no params, so it gets
> every account's commitments. Harmless today, because the Instagram row and the
> YouTube row are the same video for the same repo. **With a second account it is
> wrong in the expensive direction**: account 2's discovery would treat account
> 1's repos as covered and drop them, and the two accounts would starve each
> other out of the top of the search results, which `CLAUDE.md` already records
> as the failure that killed two nights of batches in August.
> **Mechanical**, but only once `--account` exists to say which id to send.

> **F9. `data/used_repos.json` has no account dimension at all.**
> `config.py:276` resolves one path and `scraper.UsedRepos` (`scraper.py:101`)
> is constructed from it in five places. **Mechanical** once F4 decides that the
> data directory is per account, and it must be part of the same change: a
> second account pointed at the same file inherits account 1's 30 day cooldown
> on every repo it has ever covered.

One nuance worth keeping. `rendered_repos_list` matches `ig_user_id IN (?, '')`
deliberately (`db.py:1630`), because a render can predate the account being
configured. That blank wildcard is correct for the migration it was written for
and becomes a small leak between accounts later. **Works as is** for now; note
it rather than change it.

## Data, which is what "the same numbers as now" means

The `insights` table columns are Meta's REELS metric names, `skip_rate`
included, and the sweep runs Instagram only.

> **F5. `/api/results` structurally cannot return a non Instagram post.**
> `gateway/api.py:472` omits any row with no `skip_rate` rather than zeroing it,
> which is the right call for its own purpose and means a YouTube post has never
> appeared there and a TikTok one never could. This is the concrete shape of
> "get data like we do now" for a third platform.
>
> **Decision**, and the recommendation is to split the question in two:
>
> - **Storage.** Add `platform` to `insights` and let a TikTok sweep write
>   `views`, `likes`, `comments`, `shares` from `/v2/video/query/`. The existing
>   columns fit; `reach`, `saved`, `avg_watch_ms`, `total_watch_ms` and
>   `skip_rate` stay 0 and mean "not measured on this platform". `PastPost`
>   already carries saves and shares as `None` rather than 0 for exactly this
>   distinction, so the precedent is in the codebase.
> - **The loop.** `_results_block` keeps reading Instagram alone. TikTok exposes
>   no retention, watch time or completion metric of any kind, so there is no
>   three second equivalent to substitute. This is the same rule
>   `docs/youtube-publishing-plan.md:144` set for `averageViewPercentage` and it
>   holds here more strongly, because YouTube at least has a curve.
>
> One shape TikTok adds that Meta never needed: the `publish_id` returned at
> post time is not a video id, so the sweep has to list the account's recent
> videos and match before it can query counts.

## The panel and `analysis.py`

The templates are already per account. The arithmetic is not.

> **F6. `analysis.py`'s constants are measurements of one account presented as
> constants.** `SKIP_THRESHOLD = 60.0` (`analysis.py:39`), `BREAKOUT_VIEWS = 500`
> (`:45`), `TREND_WINDOW = 7` (`:49`), `SETTLED_READINGS = 3` (`:62`), each
> derived from this account's first 58 posts. `slot_of` (`:110`) formats in UTC
> unconditionally and `admin._display_tz` (`admin.py:195`) takes the timezone of
> the first slot for the whole board.
>
> **Decision, and the recommendation is to do nothing to the numbers.**
> `analysis.maturity` (`:65`) already recomputes the settling curve from live
> rows on every render and prints it beside the rule it applies, precisely
> because a number written into prose goes stale silently. The cheap and honest
> version of the same idea is to label the thresholds with the account they were
> measured on rather than to invent per account config for a second account with
> no posts to measure. Revisit when account 2 has 50 of its own.
>
> Two smaller items in the same file are **mechanical**: a TikTok row has no
> `skip_rate`, so it must be excluded from `cohorts` and from `skip_chart` the
> same way an unmeasured row already is, and the Posts page needs a per platform
> column set or a TikTok row renders as a post that got zero of everything.

> **F7. The publish counters cannot tell the platforms apart.**
> `metrics.posts_published` and `metrics.publish_failures` (`metrics.py:78-86`)
> carry no labels, and `queue_depth` is labelled by `state` only.
> `token_days_left` is labelled `ig_user_id`, which happens to work. With three
> platforms and two accounts, `ReelsmithPublishFailing` fires without saying
> where, and a platform that has stopped publishing entirely is invisible behind
> the other two still succeeding. **Mechanical**: add a `platform` label, and an
> `account` label to `queue_depth`. Note the rule `CLAUDE.md` already states,
> that every series has to be published including the empty ones, or a gauge
> that stops being reported keeps its last value forever.

## Naming and authorisation

> **F10. `ig_user_id` is the account key and the name is wrong on two thirds of
> the rows it will soon hold.** `docs/youtube-handover.md:105` records the rename
> to `account_id` as mechanical and deliberately deferred. It touches `db.py`,
> `admin.py`, `models.py`, `pipeline/gateway.py`, ten templates, the
> `?ig_user_id=` query parameter on six API routes and most of a 5,800 line test
> suite.
>
> **Decision, and the recommendation is to do it, but not first.** The argument
> for deferring it was that a rename competing with a feature makes both harder
> to review, and that argument is unchanged. The argument for doing it is that
> every finding above touches these call sites anyway. So: after F0 and F4, and
> before TikTok's own tables land, as a change with no behaviour in it at all,
> where the whole suite passing is the entire proof.

> **F11. One API token and one admin password for every account.**
> `cfg.api_token` is described as "one token, one client" (`gateway/config.py:35`)
> and `cfg.admin_token` is a single shared password with no per account
> authorisation. **Works as is**, and say so rather than leaving it implied: both
> accounts are yours, the panel is behind Authentik at the ingress, and per
> tenant auth would be building a product for a user base of one. Revisit only
> if somebody else ever needs the panel.

## Ranked

By what it costs if ignored, not by effort.

| | Finding | Verdict |
|---|---|---|
| 1 | **F0** Second Instagram account deletes the first one's slots at boot, reproduced | Mechanical, prerequisite |
| 2 | **F9** One `used_repos.json` shared by two accounts | Mechanical, with F4 |
| 3 | **F8** Covered list read without an account filter | Mechanical, with F4 |
| 4 | **F4** What `--account` selects | Decision, blocks everything |
| 5 | **F2** TikTok's rotating refresh token needs a loop | Decision |
| 6 | **F1** Publish dispatch falls through to Instagram | Mechanical |
| 7 | **F5** Non Instagram posts cannot reach `/api/results` | Decision |
| 8 | **F7** Publish metrics carry no platform label | Mechanical |
| 9 | **F10** `ig_user_id` to `account_id` | Decision, after F0 and F4 |
| 10 | **F3** `--publish` does not fan out | Decision, may be documentation only |
| 11 | **F6** Single account constants in `analysis.py` | Decision, recommend labelling not configuring |
| 12 | **F11** One API token, one admin password | Works as is |

The shape of the answer: **four of the twelve are prerequisites for a second
account and three of those are mechanical.** The second account is a smaller job
than `SPINOFFS.md` implies, and F0 is the reason it cannot be started casually.
