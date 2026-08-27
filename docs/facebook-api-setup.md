# Facebook API setup

One-time setup to let the gateway publish Reels to a Facebook Page.

**Status: walked on 2026-08-27, up to the registration.** The app is
configured, the Page exists, the consent trip completed and the Page access
token was proved against the Graph API. The one step outstanding is
registering it with the gateway, which cannot happen until the gateway is
deployed with the `/api/accounts/facebook` route. See
`## What actually happened when this was attempted`.

**This was the cheapest of the four destinations, and one fact is why.** A Page
access token is a token plus an expiry, which is the shape `accounts` has held
since the first migration. YouTube needed a client pair plus a refresh token
and TikTok needed a rotating one, so each got a credentials table and TikTok
also got a refresher loop. This needed neither. There is no
`facebook_credentials` table anywhere and there is no `GATEWAY_FACEBOOK_ENABLED`.

## What actually happened when this was attempted

One session on 2026-08-27, in a driven browser signed in as the developer
account. **Everything on the app side worked first time**, which is worth
recording precisely because the TikTok trip did not.

**What is done and verified:**

- The app is `reelsmith`, id `3259676274234377`, **Live**, type Business. It is
  the same app the Instagram path uses, and it had exactly one product on it,
  Instagram.
- **Facebook Login for Business is added.** It was in Available products and
  the Set up link added it without a dialog.
- **`https://gate.nordbye.it/facebook/callback` is saved** in Valid OAuth
  Redirect URIs. Verified by reloading the page rather than by the absence of
  an error, which is the lesson from TikTok. Client OAuth login, Web OAuth
  login, Enforce HTTPS and Use Strict Mode for redirect URIs were all already
  on.
- **The three `pages_*` permissions are Standard access, Ready to use**, with
  no App Review requested and none needed. That is the claim this page said was
  most worth checking, and it holds: `pages_show_list`, `pages_manage_posts`
  and `pages_read_engagement` are all auto-granted at standard access, which
  covers assets the app admin also administers.
- **The consent dialog renders and the redirect URI is accepted.** Opening the
  classic `/v23.0/dialog/oauth` with the three scopes reached "Continue as
  ...?" rather than a URL Blocked error, and the dialog already carries links
  to this service's own privacy policy and terms. Not completed, because there
  was nothing to grant.

**The consent trip, walked:**

- **The Page id is `1236596692875712`.** Written here because the Page shows
  **two** ids and one of them is wrong for this purpose: its own profile URL is
  `profile.php?id=61593782854313`, and that is not what `/{page-id}/video_reels`
  wants. `GET /me/accounts` is the only authority, and both the consent
  dialog's Page picker and the API agree on `1236596692875712`.
- **Facebook forces an account switch if the browser is acting as the Page.**
  The OAuth dialog bounces to `/forced_account_switch` with "you must switch to
  <your name> to continue". Consent is granted by the person, not the Page.
- **The Pages screen offers all-and-future or a specific list.** The narrower
  one was chosen and one Page ticked, so a Page created later is not
  automatically covered. That is deliberate: re-consenting is one command.
- **The review screen listed exactly the three scopes and no more**, one Page
  each, which is the check that the scope list is not over-broad.
- **The `public_profile` advanced access warning blocked nothing.** The grant
  completed with the app on standard access. Business Verification was not
  needed. If that ever changes, the warning is where to look.
- **The callback 404s until the gateway is deployed**, and the trip still
  works, because the code is in the address bar and the page is only a display.
  Expect the 404 on a first setup and read the address anyway.
- **The Page token was verified before being trusted**: a read of the Page node
  returned `The Nightly Build`, `Digital creator`. A registration that stores an
  unproven token fails days later at a slot.

**What is not done:**

- **The gateway registration.** `POST /api/accounts/facebook` returns 404,
  because the route ships with this change and the deployed image predates it.
  So the order is deploy first, then run
  `scripts/facebook_authorise.py`. The consent above is already granted, which
  makes the re-run a click-through rather than a fresh authorisation.
- **The Page username, and it is not merely unclaimed: the Page is not
  eligible for one yet.** Facebook offers no username field at all on a new
  Page. The documented bar is roughly 25 followers plus at least one post, and
  new Pages are excluded for a few weeks regardless of that. There is no
  username control anywhere in this Page's settings, which is the symptom
  rather than a UI that is hard to find.

  So `gateway/templates/index.html`, `privacy.html` and `terms.html` link to
  `https://www.facebook.com/1236596692875712` instead, which is the `link` the
  Graph API returns for the Page and works today. **Swap all three to
  `facebook.com/thenightlybuild` once the username can be claimed**, which is
  after the account has posted here for a while. A vanity URL written before it
  exists is a privacy policy pointing at a 404.


- **There was no Facebook Page, and `docs/instagram-api-setup.md` implies
  there should have been.** That doc lists a linked Page as a prerequisite; the
  Instagram path here runs on Instagram Login, which needs no Page, so the
  prerequisite was never binding and no Page was ever made. One was created on
  2026-08-27 to PROFILE.md's identity: name **The Nightly Build**, category
  **Digital creator**, the Instagram bio, `gate.nordbye.it` as the website and
  `thenightlybuild@nordbye.it` as the contact.
- **The app secret is behind a password re-entry**, so reading it is the
  operator's step and cannot be automated away.

**One warning that turned out not to block anything.** The Facebook
Login for Business settings page shows:

> Facebook Login for Business requires advanced access. Your app has standard
> access to `public_profile`. To use Facebook Login for Business, switch
> `public_profile` to advanced access.

and `public_profile`'s own row reads **Verification required**, linking to
Business Verification. The whole consent trip completed anyway, on standard
access, and the resulting Page token reads and is accepted by the Graph API.
So this warning is about the full Facebook Login for Business feature set
rather than about this integration. **If a future trip fails, look here
first**, because Business Verification is a documents-and-days process rather
than a click.

## The runbook

Ordered, and the order matters in one place, noted at step 5.

1. **Create the Page.** As of 2026-08-27 there is none, so this is not an "if".
   A Reel is published to a Page, not to a personal profile, and there is no
   API for a profile. Note its **numeric id**, from the Page's About panel. `facebook.com/<vanity-name>` addresses the
   Page perfectly well in a browser and not at all on `/{page-id}/video_reels`;
   the gateway refuses a non-numeric id at registration rather than at the first
   publish, which is the whole reason that validator exists.

2. **Done.** The app is `reelsmith`, id `3259676274234377`, and it has the
   **Facebook Login for Business** product, which is a different product from
   the Instagram Login this repo's Instagram path uses. Both live on the one
   app; the tokens they mint are not interchangeable, which is why
   `gateway/facebook.py` names `graph.facebook.com` as its own constant rather
   than reading `cfg.graph_host`.

3. **Done.** `https://gate.nordbye.it/facebook/callback` is in the app's Valid
   OAuth Redirect URIs, character for character. `gateway/pages.py` serves it
   and `tests/test_gateway_pages.py` pins that it answers with the admin panel
   off. Strict Mode is on, so a URI that differs by a trailing slash is a
   different URI.

   Facebook does permit a `localhost` redirect while an app is in development.
   That is deliberately not used: the app that publishes these Reels is live,
   the allowance is one Meta has narrowed before, and the page next door already
   exists for TikTok.

4. **Walk the consent trip.**

   ```bash
   export FACEBOOK_APP_ID=...
   export FACEBOOK_APP_SECRET=...
   uv run python scripts/facebook_authorise.py --gateway https://gate.nordbye.it
   ```

   Neither value is taken on the command line: argv is visible in `ps` and lands
   in shell history.

   **Tick the Page on the Pages screen.** None is ticked by default, and a
   consent that grants no Page looks like a success and returns an empty list at
   the next step. The script says so rather than failing obscurely, because this
   is the mistake that is free to make and expensive to diagnose.

5. **Nothing before this point can be keyed on the Page id**, which is what
   forces the order. The `GATEWAY_SLOTS` line and the render host's
   `FACEBOOK_PAGE_ID` both name it, and the consent trip is what confirms which
   Page the token actually covers. Same constraint as TikTok's open id.

   ```
   # accounts/<name>/.env on the render host
   FACEBOOK_PAGE_ID=1236596692875712
   ```

   ```
   # GATEWAY_SLOTS, in the homelab config
   08:10 Europe/Oslo account=1236596692875712
   ```

   Put an explicit `account=` on the line. A line without one is resolved to the
   single registered Instagram account, and that shortcut is F0.

6. **Check it.** The Posts page grows a Facebook board once a Reel has
   published, and the switcher grows a fourth mark. Before that, the account row
   is visible in the panel as soon as step 4 finishes.

## The scopes, and why there are only three

`pages_show_list`, `pages_manage_posts`, `pages_read_engagement`.

- `pages_show_list` is what makes `GET /me/accounts` return anything.
- `pages_manage_posts` is the publish.
- `pages_read_engagement` is the insights sweep, including the comment count.

**`read_insights` is deliberately absent.** It covers Page level insights, and
nothing here reads those: the sweep asks a video node for its own numbers, which
is post level. A scope an app does not use is a named rejection reason at review
time, so this list should not grow speculatively.

**App Review is not expected to be needed.** An admin of both the app and the
Page is granted all three without it. Review is what publishing to somebody
else's Page would need, which this account will never do. That is the claim on
this page most worth checking against the portal, because it is the one that
would cost a session if it is wrong. Note the contrast with TikTok, where the
audit is real, is enforced by a documented error code, and is not expected ever
to pass.

## What the token is, and why nothing refreshes it

A **long-lived Page access token**, and the adjective is the whole point.

`scripts/facebook_authorise.py` does four steps rather than two: code, then a
short-lived user token, then a **long-lived** user token, then
`GET /me/accounts`. A Page token derived from a long-lived user token does not
expire on a clock. One derived from a short-lived user token expires with it,
roughly an hour later, and the failure that follows says the token is invalid
rather than saying it was born wrong.

So there is no refresher loop here, unlike TikTok, and no 60 day clock, unlike
Instagram's own token. Re-running the script is how a Page is recovered if a
token is ever invalidated, which is what a password change or a permission
revocation does.

## Publishing, in three calls across two hosts

| phase | endpoint |
|---|---|
| `start` | `POST /{page-id}/video_reels`, `upload_phase=start` |
| upload | `POST rupload.facebook.com/video-upload/{version}/{video-id}` |
| `finish` | `POST /{page-id}/video_reels`, `upload_phase=finish`, `video_state=PUBLISHED` |

**Meta fetches the video**, which is the seam this service already provides for
Instagram and TikTok. The upload phase takes `file_url` as a *header*, not a
form field, and `Authorization: OAuth <token>` rather than `Bearer`. Both are
the kind of detail that fails as a 401 and reads as a bad token.

**The token is in a header on every call, never in a URL.** Meta documents most
of these with `access_token` as a parameter and the header form works on both
Graph hosts, so this follows the rule `gateway/graph.py` already holds to and
`tests/test_gateway_facebook_publish.py` pins it. The OAuth exchanges in
`scripts/facebook_authorise.py` are the exception, because trading one
credential for another has no header form.

**The retry line sits at `start`, one step earlier than the API's own
irreversibility.** `start` creates an unpublished video id and publishes
nothing, so in principle a failed upload could be retried. It is terminal
anyway, because a retry restarts at `start` and cannot tell a `finish` that
never landed from one whose response was lost. The second of those posts the
Reel twice, which is the one failure here that cannot be undone quietly.

**`finish` returning `{"success": true}` is not "published".** Transcoding
follows and can fail on its own, so the publisher polls
`GET /{video-id}?fields=status,permalink_url` until `publish_status` is
`published`. A timeout is terminal for the reason above.

Reels are **3 to 90 seconds**. These run 30 to 45, so that is a note about a
format change rather than about today's videos.

## What comes back, and what must not be read into it

`GET /{video-id}?fields=permalink_url,comments.summary(true),video_insights.metric(...)`,
one request per Reel per sweep.

| column | metric |
|---|---|
| `views` | `blue_reels_play_count` |
| `reach` | `post_impressions_unique` |
| `likes` | `post_video_likes_by_reaction_type`, summed |
| `comments` | the node's own `comments.summary` |
| `avg_watch_ms` | `post_video_avg_time_watched` |
| `total_watch_ms` | `post_video_view_time` |

Three things about that table.

- **The reaction metric is a breakdown, not a count.** Read as a number it is
  zero, which looks like a Reel nobody reacted to.
- **Shares are an absence, not a zero.** `post_video_social_actions` is
  documented as comments plus shares in one number, and splitting it by
  subtracting a separately fetched comment count would be arithmetic on two
  different definitions. So `shares` stays 0 and the `platform` column is what
  says so, the same way `reach` does on a TikTok row.
- **Nothing goes into `skip_rate`.** `post_video_avg_time_watched` includes
  replays and scores the whole Reel, which is YouTube's `averageViewPercentage`
  problem in different units. `skip_rate` is the share who left inside three
  seconds and it is the one number the feedback loop turns on.

**This board is the one that looks most like Instagram's.** It carries reach,
which no other platform here reports, and watch time, which TikTok has none of.
That resemblance is exactly why `/api/results` filters to Instagram explicitly
rather than relying on nothing else filling `skip_rate`.

## Sources

- [Reels Publishing API](https://developers.facebook.com/docs/video-api/guides/reels-publishing)
  — the three phases, the hosted `file_url` upload, the status fields, the 3 to
  90 second and 9x16 requirements, and the 30 posts per 24 hours limit.
- [Video Insights reference](https://developers.facebook.com/docs/graph-api/reference/video/video_insights/)
  — the Reels metric names and what each one counts, including that
  `post_video_avg_time_watched` includes replays and that
  `post_video_social_actions` is comments and shares together.
- [Reels metrics in the video insights API](https://developers.facebook.com/blog/post/2022/12/15/introducing-reels-metrics-api/)
  — when these became available and to whom.

## What is still guesswork

Written down rather than left to be rediscovered, in the order it would cost a
session.

- **Whether the vanity name `facebook.com/thenightlybuild` is available.** The
  public pages link to it and the privacy policy names it. If the Page ends up
  on a different name, `gateway/templates/index.html`, `privacy.html` and
  `terms.html` all carry the link and all three have to move together.
- **The `permalink_url` shape on a Reel.** It is handled as a site-relative path
  and prefixed with `https://www.facebook.com`, and an absolute one is passed
  through unchanged, so both work. Which one Meta returns for a Reel is still
  unconfirmed; the Page node itself returns an absolute `link`.
- **Everything past the registration.** No Reel has been published here. The
  publisher, the insights sweep and the board are tested against a fake and
  have never met Meta.
- **Whether the Reel appears on the linked Instagram account too.** A Page and
  an Instagram account can be linked, and this repo publishes to both
  independently. If a linked Page cross-posts, the same video could appear twice
  on Instagram, which nothing here would notice. Worth watching on the first
  publish.
