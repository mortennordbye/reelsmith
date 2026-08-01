# The DM gateway

The Reel says "comment SEND and I will DM you the link". This is the service
that makes that true.

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

It also holds the **scheduled queue**: the Mac renders a batch of Reels, pushes
them here, and this service publishes them on a schedule. The laptop is only
needed while rendering.

It runs in the homelab cluster. The pipeline, the voice and the rendering stay
on the Mac, and nothing here could reproduce the voice.

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
export GATEWAY_API_TOKEN=...       # what the Mac presents on /api/*
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
  -d '{"ig_user_id":"...","access_token":"...","expires_in":5184000}'
```

That call also subscribes the account to `messages`. Skipping it produces no
error and no webhooks, which looks exactly like nobody messaging the account.

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
  # only the time is required; days are ISO weekdays, 1 for Monday
```

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
resolution is seconds rather than minutes, and offsets that would land on :00,
:15, :30 or :45 are skipped. A column of timestamps on a fifteen minute grid is
the cheapest automation tell there is.

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
deliberately outside all of this, because neither Meta nor the Mac can log in.

One consequence worth knowing: **`/media/<name>` is public**, which is what lets
Meta fetch a video, and therefore an unpublished queued Reel is readable by
anyone who knows its filename. The name carries a 48 bit digest of the file's
own bytes, so knowing it means already having it.

## State

One SQLite file, seven tables, `PRAGMA user_version` for migrations. Postgres is
the migration path the day this needs a second replica; nothing here makes that
hard.

The single replica is now load bearing rather than merely tidy. Two pods would
mean two schedulers, and the slot-fire claim only protects against that because
they would share the one SQLite file.

Times are stored as ISO 8601 strings in UTC. They survive a dump, they sort as
text, and they come back as the string that went in, which matters when the
thing being debugged is a 24 hour window.
