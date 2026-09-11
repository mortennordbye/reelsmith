# Instagram API setup

One-time setup to let the pipeline publish for you. Everything here is generic:
it applies to any account, and none of it needs App Review.

Two things this used to claim, both wrong, both worth stating plainly because
they are the reasons people give up on this:

- **App Review is not needed.** Review gates *Advanced Access*, which means
  acting on accounts you do not own. *Standard Access* is granted automatically
  and covers any account holding a role on your app. Your own account holds a
  role on your own app, so your own app publishes fine, in either mode.
- **The MP4 does not need a public URL.** Creating the container with
  `upload_type=resumable` returns an upload URI on `rupload.facebook.com` that
  takes the file as raw bytes. The public URL requirement belongs to the older
  `video_url` flow. No object storage, no S3, no R2.

## What you need

1. **A professional Instagram account.** Business rather than Creator. Personal
   is disqualified: the Content Publishing API does not work with it and neither
   do full insights. Meta's own docs say "professional accounts" without
   distinguishing Business from Creator, while several third-party integration
   guides report Reels publishing works with Business only, so Business is the
   safer read. Switching type is free and reversible.

2. **A linked Facebook Page.** A Business account normally wants one, and the
   API publishing path expects it. Create a bare Page and link it.

3. **A Meta app** at <https://developers.facebook.com/apps>, type Business, with
   the Instagram product added.

   **The mode does not matter for publishing and this used to say otherwise.**
   What grants access to an account you own is the tester role below, not
   Development. It said to leave the app in Development while the gotchas
   further down said Live is required for webhooks, which is a document
   contradicting itself; this app has been Live since August and publishing
   throughout. Corrected 2026-09-11, when the second account was registered
   against it.

4. **Your account added as an Instagram tester** on the app, with the invite
   accepted from the Instagram side (Settings, Website permissions, Tester
   invites). This is the step that grants Standard Access to your own account.
   Skipping it produces a confusing permissions error much later.

5. **Scopes** `instagram_business_basic` and
   `instagram_business_content_publish`. The Facebook Login path wants
   `instagram_basic`, `instagram_content_publish` and `pages_read_engagement`
   instead; set `IG_GRAPH_HOST=https://graph.facebook.com` if you go that way.

6. **A long-lived token.** Authorise once and exchange the short-lived token
   for a long-lived one. That is where this document used to end, with two
   values to paste into two files.

7. **Hand it to the consent trip**, which does the rest:

   ```bash
   uv run python scripts/authorise.py instagram --account <name>
   ```

   It asks for the token on a prompt rather than on argv, refreshes it, and
   refuses a short-lived one: `ig_refresh_token` only accepts a long-lived
   token, which turns the commonest mistake here from a publish that works for
   an hour into a refusal now. Then it reads `GET /me` back, so the account it
   registers is the one the token actually belongs to rather than an id copied
   from beside the wrong one, registers it with the gateway, and writes
   `IG_USER_ID` into `accounts/<name>/.env`.

   Then `python main.py --account <name> --refresh-token` to put this
   machine's own copy in `accounts/<name>/data/ig_token.json`, which is what
   `--publish` reads. The gateway holds and refreshes its own separately.

   By hand still works if you would rather: `IG_ACCESS_TOKEN` and `IG_USER_ID`
   in the account's `.env`. Nothing registers with the gateway that way, so
   `--enqueue` will queue a row nothing can publish.

   **If you do it by hand, `/me` returns two seventeen digit ids and the
   obvious one is wrong.** `id` is the app-scoped user id and `user_id` is the
   Instagram Business account id. `IG_USER_ID` wants the second, because the
   publisher addresses `graph.instagram.com/{id}/media` with it. Neither looks
   more correct than the other and nothing is visible until the first publish
   fails against a node that does not exist. On this account's own token `id`
   is `37342907808657598` and `user_id` is `17841441696714445`, and it is the
   second that has been publishing since 2026-08-01. The script reads both and
   labels them.

   Set `IG_APP_ID` in the root `.env` and the trip deep links straight to the
   Generate token page rather than the apps list. It is a public value, not a
   secret: it appears in every authorisation URL.

**This is the one trip that is a paste rather than a browser flow**, and the
reason is written in the script: the full flow needs an `/instagram/callback`
route on the gateway, and that route is half the change. The other half is an
`Exact` match in `k8s/talos/apps/reelsmith/httproute.yaml` in the homelab repo,
because that allowlist 404s anything not named in it. Costed rather than
absent.

If you are also running the DM gateway, add its scopes in the same trip rather
than making two: see `gateway/README.md`.

## Webhooks, if you run the gateway

Same app, same visit. In **Instagram → API setup with Instagram business
login → Configure webhooks**, set the callback to your gateway's `/webhook`,
set a verify token that matches `GATEWAY_VERIFY_TOKEN`, and subscribe the
**`messages`** field. Saving it makes Meta call the URL immediately, so the
service has to be reachable first.

Then flip the app to **Live**. Webhooks are delivered only to Live apps, and
Live does not require App Review.

`comments` and `live_comments` can be subscribed too, but they need Advanced
Access to be delivered in most cases, which is exactly why the gateway polls
comments instead. Leaving them subscribed is harmless: the gateway answers 200
and ignores anything that is not a message.

## Four things that will cost you an hour each

Learned the hard way, and none of them are obvious from the dashboard.

1. **The Instagram app secret is not the app secret.** On this login path the
   webhook signature is HMAC'd with the *Instagram* app secret shown on the API
   setup panel, not the one under App settings → Basic. Using the wrong one
   fails every delivery with a 403 that looks exactly like a broken service.
2. **Live is gated on a privacy policy URL.** Meta refuses to leave Development
   without one, and refuses webhooks while in Development, so this blocks
   everything. App settings → Basic.
3. **Generate the token before the subscription toggle.** The per-account
   webhook toggle stays disabled until a token exists, and the tooltip only
   says so if you hover it.
4. **The tester invite has two halves.** Adding the Instagram Tester role in
   the app leaves it `Pending`. It has to be accepted from the Instagram side
   under Apps and websites → Tester invites, signed in as *that* account.
5. **The Instagram app id is not the app id either**, which is the same split
   as the secret in item 1 and lives beside it on the same panel. The Meta App
   ID is `3259676274234377` and the Instagram app ID is `1591725419289044`.
   `IG_APP_ID` in the root `.env` holds the **Meta** one, because the only
   thing reading it is the consent trip opening the dashboard. An Instagram
   Business Login OAuth flow, which this repo does not have and has costed out
   in `scripts/instagram_authorise.py`, would need the other one.

**Everything on this platform comes in pairs and the obvious one is usually
wrong.** Two app ids, two app secrets, two user ids on `/me`, and two ids on a
Facebook Page. None of the wrong ones fail loudly.

## Verify it by API, not by the green ticks

The dashboard will show a subscription as configured that is not actually
attached to the account. One call settles it:

```bash
curl -s "https://graph.instagram.com/v23.0/me/subscribed_apps?access_token=$TOKEN"
# {"data":[{"id":"...","subscribed_fields":["messages"]}]}
```

An empty `data` array means no webhook will ever arrive, which is
indistinguishable from nobody messaging you.

## Three operational facts

- **Tokens last 60 days and an expired one cannot be refreshed.** Recovering
  means going back through the dashboard in a browser, so that clock is the one
  deadline here nothing recovers from on your behalf.
- **Refreshing is automatic only on a machine that holds a token, and the
  machine running the job is not one.** `--snapshot` refreshes inside a 15 day
  margin, and the render host is where `--snapshot` actually runs nightly. That
  host enqueues rather than publishes, so it has no `ig_token.json` and no
  `IG_ACCESS_TOKEN`, `refresh_token_if_due` cannot load a token and returns
  `None`, and the caller swallows the error besides. Nothing anywhere refreshes
  the laptop's token on a schedule. `python main.py --account <name>
  --refresh-token` there is a manual job with a 60 day clock on it, and the only
  thing that will remind you is reading the expiry out of
  `accounts/<name>/data/ig_token.json`. The gateway refreshes its own token
  separately, which is why posting has never noticed.
- **The rate limit is 100 published posts per rolling 24 hours.** Not a
  constraint at one a day.

## The cover image

The one thing that still wants hosting. `cover_url` is fetched by Meta from its
own servers, so a local path cannot work. Without one the Reel thumbnail falls
back to `thumb_offset`, which picks the same frame `cover.png` is rendered from,
minus the hook band. Pass `--cover-url` if you host it somewhere, or run the
gateway, which serves it.

## Sources

- [Instagram Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing)
- [Instagram Platform overview, access levels](https://developers.facebook.com/docs/instagram-platform/overview/)
