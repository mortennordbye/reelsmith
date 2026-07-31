# Instagram API setup

One-time setup to let the pipeline publish for you. Everything here is generic:
it applies to any account, and none of it needs App Review.

Two things this used to claim, both wrong, both worth stating plainly because
they are the reasons people give up on this:

- **App Review is not needed.** Review gates *Advanced Access*, which means
  acting on accounts you do not own. *Standard Access* is granted automatically
  and covers any account holding a role on your app. Your own account holds a
  role on your own app, so an app in development mode publishes fine.
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
   the Instagram product added. Leave it in **development** mode.

4. **Your account added as an Instagram tester** on the app, with the invite
   accepted from the Instagram side (Settings, Website permissions, Tester
   invites). This is the step that grants Standard Access to your own account.
   Skipping it produces a confusing permissions error much later.

5. **Scopes** `instagram_business_basic` and
   `instagram_business_content_publish`. The Facebook Login path wants
   `instagram_basic`, `instagram_content_publish` and `pages_read_engagement`
   instead; set `IG_GRAPH_HOST=https://graph.facebook.com` if you go that way.

6. **A long-lived token.** Authorise once, exchange the short-lived token for a
   long-lived one, put it in `IG_ACCESS_TOKEN`, then run
   `python main.py --refresh-token` to move it into `data/ig_token.json`.

7. **Your IG user ID**, a 17 digit number, into `IG_USER_ID`.

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

## Verify it by API, not by the green ticks

The dashboard will show a subscription as configured that is not actually
attached to the account. One call settles it:

```bash
curl -s "https://graph.instagram.com/v23.0/me/subscribed_apps?access_token=$TOKEN"
# {"data":[{"id":"...","subscribed_fields":["messages"]}]}
```

An empty `data` array means no webhook will ever arrive, which is
indistinguishable from nobody messaging you.

## Two operational facts

- **Tokens last 60 days and an expired one cannot be refreshed.** Recovering
  means going back through the dashboard in a browser. The daily `--snapshot`
  job refreshes automatically inside a 15 day margin, which makes that job load
  bearing for posting and not just for scoring.
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
