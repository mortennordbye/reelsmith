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

It runs in the homelab cluster. The pipeline, the voice and the rendering stay
on the Mac, and nothing here could reproduce the voice.

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

Webhooks need a public URL. For iterating, a quick tunnel is enough and needs no
account:

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

## State

One SQLite file, four tables, `PRAGMA user_version` for migrations. Postgres is
the migration path the day this needs a second replica; nothing here makes that
hard.

Times are stored as ISO 8601 strings in UTC. They survive a dump, they sort as
text, and they come back as the string that went in, which matters when the
thing being debugged is a 24 hour window.
