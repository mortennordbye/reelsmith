# The DM gateway

The Reel used to say "comment SEND and I will DM you the link". This is the
service that makes that true, and it still works exactly like this:

```
comment "SEND" on the Reel
  └─► private reply, the one Meta allows per comment, within 7 days
      "Follow the account and send me any message here."
      └─► they reply anything        (opens the 24h window, and only now is
          │                           their profile readable at all)
          └─► do they follow?
              ├─ yes ─► send the link. Converted.
              └─ no  ─► nudge, re-check on the next message
```

**Nothing advertises the keyword any more, so the DM half is dormant.** The ask
ran for the account's first 53 posts and drew two comments, both from people who
unfollowed once the link arrived. Every channel asks for a follow instead,
in `SPOKEN_CTA` and `CAPTION_CTA` in `pipeline/gateway.py`. The mechanic is left
wired rather than deleted, and nobody can guess an unadvertised keyword, so
everything above runs and never fires. Putting it back is a change to those two
constants and the end card, and it should take numbers beating a follow ask.

It also holds the **scheduled queue**: the render host builds a batch of Reels,
pushes them here, and this service publishes them on a schedule. Nothing on the
machine that rendered is needed once the video is uploaded.

It runs in the homelab cluster. The pipeline, the voice and the rendering stay
on the render host, and nothing here could reproduce the voice.

**Deployed since 2026-07-31.** Manifests live in the Homelab repo under
`k8s/talos/apps/reelsmith/`, promoted by Kargo from the `0.0.<run>` tags this
repo's `build-gateway.yml` publishes. One replica, `strategy: Recreate`, because
the state is a single SQLite file and the comment poller is a singleton: two
pods would race for the one private reply Meta allows per comment.

## Why it polls comments but receives DMs

Real-time `comments` webhooks need Advanced Access, which means App Review plus
Business Verification. `messages` webhooks do not. So DMs arrive by webhook and
comments are polled once a minute, which is nowhere near the 750 private replies
per hour per account that Meta allows.

Polling is also the more reliable half. Meta never replays a missed webhook,
while a failed poll simply happens again in sixty seconds, and the window for
replying to a comment is seven days wide.

## The rules that shape the code

| Rule | Where it is enforced |
|---|---|
| One private reply per comment, ever | `db.claim_comment`, on the primary key, *before* the send |
| Follower data is consent gated | `conversations._follow_state`, only ever called from the message path |
| 24 hour messaging window | `conversations.window_is_open`, checked before every outbound DM |
| The kill switch | `accounts.dm_enabled`, checked below the state machine so nothing routes around it |

The claim deliberately happens before the send. A crash in between loses that
one reply, because Meta may already have accepted it and a person receiving the
same automated message twice is worse than one who receives it never.

## Running it locally

```bash
uv pip install -e ".[dev,gateway]"

export GATEWAY_APP_SECRET=...      # Meta app secret, signs the webhooks
export GATEWAY_VERIFY_TOKEN=...    # any random string, must match the dashboard
export GATEWAY_API_TOKEN=...       # what the render host presents on /api/*
uvicorn gateway.app:app --reload
```

Or `docker compose -f gateway/docker-compose.yml up --build`, which is the same
thing in the shape the cluster will run it.

Webhooks need a public URL. The deployed instance already has one, so a tunnel
is only for iterating on the code locally against a second Meta app. For that, a
quick tunnel is enough and needs no account:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

Put the resulting `https://...trycloudflare.com/webhook` in the Meta dashboard
as the callback, with the same verify token, and subscribe the `messages` field.
The URL changes every time the tunnel restarts, which is the reason this only
lasts until the cluster deployment exists.

## Checking it by hand

```bash
curl -s localhost:8000/healthz

# The handshake Meta performs once, when the callback URL is saved.
curl -s "localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=$GATEWAY_VERIFY_TOKEN&hub.challenge=42"

# A signed delivery. An unsigned one gets a 403 and never reaches the parser.
BODY='{"entry":[{"id":"<IG_USER_ID>","messaging":[{"sender":{"id":"<IGSID>"},"recipient":{"id":"<IG_USER_ID>"},"message":{"text":"hi"}}]}]}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GATEWAY_APP_SECRET" | awk '{print $2}')
curl -s -X POST localhost:8000/webhook \
  -H "content-type: application/json" \
  -H "x-hub-signature-256: sha256=$SIG" \
  --data "$BODY"
```

## Registering an account

An account needs a long-lived token with `instagram_business_basic`,
`instagram_business_manage_messages` and `instagram_business_manage_comments`,
and the app has to be **Live** or no webhook is ever delivered.

```bash
curl -s -X POST localhost:8000/api/accounts \
  -H "authorization: Bearer $GATEWAY_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{"account_id":"...","access_token":"...","expires_in":5184000}'
```

That call also subscribes the account to `messages`. Skipping it produces no
error and no webhooks, which looks exactly like nobody messaging the account.

### TikTok, and why there is a refresher

The third destination, and the only one whose token can be lost for good.

Instagram's token is refreshed by the render host's `--snapshot` job, and a
missed pass costs a day. Google's refresh token has no clock, so YouTube needs
no loop at all. TikTok's access token lasts 24 hours and its refresh token is
**rewritten on every use**: the one just spent is dead the moment the response
arrives. So `poller.tiktok_refresher_loop` runs daily, and it commits what came
back before doing anything with the access token it came with. Failing after
that write costs a day. Failing before it costs the account, recoverable only
by a person in a browser.

`tiktok_credentials` is its own table for the same reason `youtube_credentials`
is: four platforms with four credential shapes, and one table holding all of
them would be mostly null on every row. Only this one needs
`refresh_expires_at`. The two Meta surfaces need no table at all, because a
token plus an expiry is what `accounts` already holds.

**Two publish paths, and they end differently.** Direct Post needs an audit that
reviews a posting screen this repo does not have; the unaudited path forces
`SELF_ONLY` on a private account. The inbox path needs no audit and drops the
video into the creator's drafts for one tap. They differ by one field, one
endpoint and one success state, which is why `gateway/tiktok.py` serves both and
a refusal costs a config flag rather than a rewrite. `SEND_TO_USER_INBOX` is the
finish line on the inbox path and an intermediate state on the other, so waiting
for `PUBLISH_COMPLETE` on the inbox path times out on a video that worked.

**TikTok reports failure inside a 200** as readily as with a status code, so
every response is read for `error.code` rather than for its status.

Turning it on is three settings and a registration:

```bash
GATEWAY_TIKTOK_ENABLED=true       # the refresher, the sweep, and publishing
GATEWAY_TIKTOK_DIRECT_POST=false  # the inbox path, which needs no audit
GATEWAY_TIKTOK_PRIVACY_LEVEL=SELF_ONLY   # what an unaudited client may ask for

uv run python scripts/tiktok_authorise.py --gateway https://gate.example
```

`GATEWAY_TIKTOK_ENABLED` gates all three of those things, and a queued TikTok
row reaching a slot with it off fails that row rather than retrying: a flag
that is off is not a transient condition. There is deliberately no
`GATEWAY_YOUTUBE_ENABLED`, because nothing on that path runs unless a slot
fires and a slot only fires when the scheduler is on. A flag earns its place
when something runs without it.

### Registering a YouTube channel

A second destination. It shares the queue, the slots, the claims and the admin
panel, because none of that is Meta-specific; only the publish call is. What
tells them apart is `accounts.platform`, and `account_id` holds the channel id
on a YouTube row. It was called `ig_user_id` until 2026-08-26, when it stopped
being true on two thirds of the rows it was about to carry; every route and body
still accepts the old name, because a render host that has not been pulled yet
is still sending it.

```bash
curl -s -X POST localhost:8000/api/accounts/youtube \
  -H "authorization: Bearer $GATEWAY_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{"channel_id":"UC...","client_id":"...","client_secret":"...",
       "refresh_token":"...","username":"@handle"}'
```

The one-time browser authorisation happens wherever is convenient; its result
lives here from then on, in the cluster secret rather than in a file beside the
renderer. Google refresh tokens do not expire on a clock the way Meta's 60 day
tokens do, so there is no refresher loop for these and no expiry to watch.

**Every Meta loop reads Instagram rows only**, and that filter is load bearing
rather than tidy. A YouTube row has an empty `access_token` and a null
`token_expires_at`, which the refresher would otherwise read as an unknown
expiry and therefore as due, posting an empty token to Meta forever. The
comment sweep and the insights sweep would ask about video ids that were never
Meta's. `db.active_accounts` and `db.all_accounts` default to Instagram for
that reason: forgetting the argument at a new call site costs a no-op, where a
default of everything would cost a live error. The scheduler and the admin
panel pass `platform=None` and say so in the open.

`docs/youtube-publishing-plan.md` has the rest of the shape, and
`docs/youtube-api-setup.md` covers the account layout and the audit.

### Registering a Facebook Page

The fourth destination and the cheapest, for a reason worth stating rather than
rediscovering: **a Page access token is Meta's credential shape**, which is
what `accounts` has held since the first migration. So there is no
`facebook_credentials` table, no token mint per publish and no refresher loop.
One write registers a Page.

```bash
uv run python scripts/facebook_authorise.py --gateway https://gate.example
```

That script exists for one step that is easy to leave out and impossible to
notice afterwards. A Page token minted from a *short-lived* user token expires
with it about an hour later, and the failure that follows says the token is
invalid rather than saying it was born wrong. So the trip is code, short-lived
user token, **long-lived** user token, then `GET /me/accounts`.

The registration itself is one call, if the token is already in hand:

```bash
curl -s -X POST localhost:8000/api/accounts/facebook \
  -H "authorization: Bearer $GATEWAY_API_TOKEN" \
  -H "content-type: application/json" \
  -d '{"page_id":"1047...","access_token":"EAA...","username":"The Nightly Build"}'
```

**This is not the Instagram Login path.** `gateway/publisher.py` talks to
`graph.instagram.com` with a token that cannot see a Page, and
`gateway/facebook.py` talks to `graph.facebook.com` with a Page token that
cannot see the Instagram account. The host is a constant in that module rather
than `cfg.graph_host`, because one shared host setting serving two login paths
is a setting that is wrong for one of them.

**There is no `GATEWAY_FACEBOOK_ENABLED`,** the same decision YouTube's absence
records: publishing runs only when a slot fires, and a slot only fires when the
scheduler is on. `GATEWAY_FACEBOOK_INSIGHTS_ENABLED` exists because the sweep
is the one thing here that runs without a slot, which is exactly what earned
TikTok its flag.

`docs/facebook-api-setup.md` is the runbook.

## The five public pages

`gateway/pages.py` serves `GET /`, `/privacy`, `/terms`, `/tiktok/callback` and
`/facebook/callback`, and they are the only routes here written for a human who
is not the operator.

**The first three exist because every platform demands them before it will take
an application**, and TikTok adds a condition the others do not: the URLs must
sit on a domain it can prove by DNS record, so a GitHub blob URL is refused with
"This URL is not verified" and the form will not save. That is what moved them
off `docs/` and onto this service. One domain verification covers all four URLs
and the media TikTok pulls, because a verified domain carries its subdomains.

**The last two are OAuth redirect targets and they deliberately do nothing.**
TikTok will not register a redirect URI that is not https, and loopback is not
an exception, so `scripts/tiktok_authorise.py` cannot run a listener; Facebook
does allow a localhost redirect while an app is in development and the app that
publishes these Reels is live, so the Page trip lands here too. The browser
arrives, the page prints the authorisation code, and the operator pastes it
back into the waiting script. Exchanging it here would need the client secret,
which this service is not told until the account is registered at the end of
that same trip, so the obvious improvement puts a secret in a second place for
the sake of a one-off. One template serves both, because the wording is all
that differs; two routes rather than one, because each platform holds its own
URL in its own portal.

Three things about this router are load bearing:

- **It mounts unconditionally**, unlike `admin.public`. The obvious home was
  that router, which already serves the one page that cannot require a login,
  but it is only included when `GATEWAY_ADMIN_ENABLED` is on. Four platforms
  hold these URLs on file, and a legal page that 404s because a feature flag
  moved is worse than one nobody reads. `tests/test_gateway_pages.py` pins it
  with the panel off.
- **The templates are the only copy.** The Dockerfile copies `gateway/` alone,
  so the image cannot see `docs/`. Rendering markdown at runtime would put a
  parser in an image that carries no dependency it does not import, and keeping
  both would be two privacy policies drifting apart.
- **No script tags and no external stylesheet**, asserted rather than intended.
  These render for somebody who arrived from an app listing on an unknown
  device, and an asset on a third-party host is both a tracking vector on a
  privacy policy and a way for the page to become unstyled text in a few years.

Whatever fronts this service has to allow the paths explicitly if it is an
allowlist. Shipping the route is not the same as serving it, and a redirect
that 404s spends a consent trip.

## The scheduled queue

```
Mac:  render  ─►  --enqueue  ─►  /api/media (the MP4)  +  /api/queue (the rest)
Gateway:  a slot comes due  ─►  claim  ─►  create container  ─►  publish
                                                             └─►  register the
                                                                  post so the
                                                                  keyword works
```

Off unless `GATEWAY_SCHEDULER_ENABLED=true`. Publishing to the feed is a bigger
power than answering comments, and gaining it by upgrading would be a surprise
rather than a decision.

**Two claims, both committed before any call to Meta.** The slot fire is keyed
on (slot, local date), so a restart cannot make one evening fire twice. The post
is claimed by compare and swap on `state = approved`, so two ticks cannot take
the same one.

**A failure is retried only when a retry is provably safe.** The line is whether
a container exists. Before that, Meta was never asked to make anything, so the
slot gets its turn back and one dropped connection does not cost the day. After
it, a Reel may already be live and no error text proves otherwise, so the row
stops in `failed` and waits for a person. The admin UI offers a Retry, marked
with a warning when a container existed, because that decision is a human's.

### Declaring the schedule

Slots live in config so they survive a redeploy. One per line, which drops into
a ConfigMap as a block string:

```yaml
GATEWAY_SLOTS: |
  18:00 Europe/Oslo jitter=15
  08:30 Europe/Oslo jitter=20 days=6,7
  19:30 Europe/Oslo account=UCH8RDOkbzDna2mDAlq4GaFw
  # only the time is required; days are ISO weekdays, 1 for Monday
```

`account` names the destination, and is how one config holds more than one.
A line without it belongs to `GATEWAY_SLOTS_ACCOUNT`, or to the single
registered Instagram account when that is unambiguous; when it is not, those
lines are dropped with an error and the ones naming an account still apply,
because applying the unambiguous half beats applying nothing.

**Put `account=` on every line before a second account of any platform
exists.** The resolve-by-count above is what a second Instagram account
breaks, and until 2026-08-26 breaking it deleted the first account's schedule
at boot. See "Removing an account's lines" below and F0 in
`docs/multi-destination-audit.md`.

Removing an account's lines removes its slots. That is deliberate rather than
incidental: config is the truth for these, and a channel deleted from it that
kept publishing on a schedule nobody can read any more is the worse failure.

**Except while a line is unresolved, when nothing is removed from anywhere.**
The sweep reads an account's absence from the config as an instruction to
delete its rows, and an unresolved line is an account this code could not
name rather than an account nobody named. Those want opposite actions and the
sweep cannot tell them apart, so it stops: an account whose lines really were
deleted goes on posting until the config is fixed, which is recoverable and
visible on the Queue page, where deleting a working schedule at boot is
neither. The log says which happened, and a removal now says how many rows it
took rather than reporting itself as `Applied 0 slot(s)`.

Applied at startup and owned by config from then on: these rows are replaced on
every boot, so the admin UI shows them as `config` and does not offer a delete
the next rollout would undo. Slots added in the UI are a separate set and
survive. A line that does not parse **fails the boot**, because a schedule that
silently drops the line with the typo is a schedule that quietly stops posting.

**The jitter is derived, never rolled.** Each firing moves by up to
`jitter` minutes either way, so the account is not posting at 18:00:00 every
evening. A random offset picked at tick time would be re-rolled on every
restart, and a slot judged not-yet-due at 17:55 could come due at 17:50 after a
pod restart, publishing twice or slipping a day. Hashing the slot id and the
local date gives an offset that is stable for the day, different the next, and
identical across replicas and replays. Two further details are on purpose:
the offset resolves to seconds rather than whole minutes, and any that would
land on :00, :15, :30 or :45 is skipped. A column of timestamps on a fifteen
minute grid is the cheapest automation tell there is.

Because the slot id seeds the jitter, the config sync keeps the id of any slot
whose definition has not changed. Rewriting the rows every boot would reshuffle
every offset on every restart, which is the instability this all exists to
avoid.

### The admin UI

`/admin`, server rendered, no build step and no external requests. Three pages:
the queue (approve, hold, reorder, pin, edit the caption, cancel, and the video
plays inline from the same public route Meta fetches from), the slots, and
health (token expiry, queue depth, the funnel, and the kill switch).

### Getting into the panel

**Off by default, and it will not start unauthenticated.** This service is
publicly reachable by necessity: Meta fetches `/media/*` and posts to
`/webhook` from its own servers, so there is no network boundary to hide behind.
A panel that publishes to a real account, rewrites captions and holds the kill
switch cannot rely on an ingress rule someone might reorder. Three states, no
fourth:

| Config | Result |
|---|---|
| `GATEWAY_ADMIN_ENABLED=false` | no `/admin` routes at all (the default) |
| enabled + `GATEWAY_ADMIN_TOKEN` | this service checks the token itself |
| enabled + `GATEWAY_ADMIN_TRUST_PROXY_AUTH=true` | forward-auth in front is doing it |

Enabled with neither **fails the boot**, naming what to set. A crashlooping pod
is a better outcome than a control panel someone finds. A token under 24
characters is refused for the same reason; `openssl rand -hex 24` is the
intended way to make one.

Forward-auth at Traefik is still the intended front door for the deployed
instance, and `GATEWAY_ADMIN_TRUST_PROXY_AUTH` is how you say so. It is an
explicit statement rather than a header this service trusts, because every
header is attacker-settable on a service this exposed.

The session cookie is HttpOnly, `SameSite=Strict` and Secure on https.
SameSite is the primary CSRF defence, since every control is a form POST; the
Origin check is the second lock, and the Referer is only honoured for the
post-redirect-get when it points back here, so none of these controls can be
turned into an open redirect.

`/webhook`, `/covers/*`, `/media/*` and the bearer-token `/api/*` routes are
deliberately outside all of this, because neither Meta nor the render host can
log in.

One consequence worth knowing: **`/media/<name>` is public**, which is what lets
Meta fetch a video, and therefore an unpublished queued Reel is readable by
anyone who knows its filename. The name carries a 48 bit digest of the file's
own bytes, so knowing it means already having it.

## One identity is four rows

`accounts` is one row per platform, so an identity publishing to Instagram,
YouTube, TikTok and Facebook is four of them. That includes the two Meta
surfaces: a Page and the Instagram account beside it are different ids holding
different tokens minted through different login paths. `brand` is what says
they are the same identity, and it is the pipeline's `--account <name>`.

Registration takes it explicitly and derives it from the handle when nobody
says, stripping the leading @ and lowercasing, because some platforms store the
handle with one and others do not. Say it explicitly for a second identity or
for one whose handle differs across platforms, which a Page usually does, since
its name is prose rather than a handle. A re-authorisation that omits it keeps
whatever grouping the row already had.

The panel is built on that: the switcher is one chip per identity with a mark
per platform, `?brand=` scopes to an identity and `?account=` to one of its
destinations, and every page groups its boards the same way.

## Insights

`GET /admin/posts` answers the question the rest of the panel could not: did
that Reel work. It joins two things, neither of them new. Meta's numbers, which
a sweep reads once every six hours and stores, and the DM funnel this service
has recorded since the first post.

Three decisions worth knowing:

- **A reading is per media per day, not per media.** A Reel keeps climbing for
  days after it publishes, so one mutable row would answer "how is it doing"
  while making "did the evening slot beat the morning one" unanswerable
  forever. The history costs a few hundred bytes a post a day.
- **A media with no numbers is normal, not an error.** Meta has nothing for a
  Reel published minutes ago, so `graph.media_insights` returns None for that
  and the sweep moves on. It re-raises an auth failure, though, because that is
  true of every media behind it and a sweep that swallowed it would read
  nothing at all while reporting no errors.
- **The sweep is on by default**, where the scheduler is off. It only reads: it
  creates nothing, publishes nothing and messages nobody, so gaining it by
  upgrading is not a surprise worth guarding against.
- **It reads all four platforms, and the `platform` column says which.**
  Instagram per media, TikTok by listing the account's videos and matching on
  the title this service wrote, YouTube in one Analytics report covering the
  whole batch, Facebook one request per Reel. What each of them does not report
  stays 0, and nothing downstream may read those zeroes as a result: a TikTok
  row has no watch time of any kind, a YouTube row has no `skip_rate` and no
  reach, and a Facebook row has reach and watch time but no share count, since
  Meta reports shares fused to the comment count. `GATEWAY_TIKTOK_ENABLED`,
  `GATEWAY_YOUTUBE_INSIGHTS_ENABLED` and `GATEWAY_FACEBOOK_INSIGHTS_ENABLED`
  are the three switches, and only the first is off by default.
- **Only Instagram reaches the scriptwriter.** `/api/results` filters to it
  explicitly. `skip_rate` scores the first three seconds; YouTube's
  `averageViewPercentage` and Facebook's average time watched both score the
  whole video, replays included; TikTok reports nothing about watching. So the
  four are stored side by side and compared to themselves rather than to each
  other. Facebook is the one to watch here, because its board carries reach and
  watch time and therefore looks like Instagram's.

The per-post funnel comes from `comments_handled` and `deliveries`, both keyed
by media id and both written all along. `db.funnel` only ever summed them
account-wide, which cannot say which video converted, and that is the question
that decides what to make more of.

## State

One SQLite file, eight tables, `PRAGMA user_version` for migrations. Postgres is
the migration path the day this needs a second replica; nothing here makes that
hard.

The single replica is now load bearing rather than merely tidy. Two pods would
mean two schedulers, and the slot-fire claim only protects against that because
they would share the one SQLite file.

Times are stored as ISO 8601 strings in UTC. They survive a dump, they sort as
text, and they come back as the string that went in, which matters when the
thing being debugged is a 24 hour window.

### Backups

Every six hours, `VACUUM INTO` writes a consistent copy to `backups/` beside the
database, keeping the newest 14. On by default for the same reason insights are:
it only reads.

`VACUUM INTO` rather than copying the file, because copying a live SQLite
database can capture a torn page mid-write and the copy looks fine until the day
it is needed.

The volume is already NAS backed NFS, so losing a node was never the risk. The
risk is the file being *wrong*: a bad migration, a mistaken delete, a corrupt
page. One table makes it worth doing rather than merely tidy. `comments_handled`
cannot be rebuilt from anywhere, and a poller that starts again with an empty one
re-replies to every comment still inside Meta's seven day window, which is a spam
incident on a live account. `insights` cannot be rebuilt either: Meta serves
current numbers and no history.

**It does not defend against losing the volume**, since the copies live beside
the original, and the storage class reclaims with `Delete`. Pulling one off the
cluster is a separate job and still worth doing.
