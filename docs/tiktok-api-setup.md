# TikTok API setup

One-time setup to let the gateway publish for you. Read on 2026-08-26 against
TikTok's live developer documentation and then walked in the portal on
2026-08-27; every claim carries a link in `## Sources`, what the portal actually
did is in `## What actually happened when this was attempted`, and what is still
guesswork is in `## What was unknown, and what is still`.

**This is done.** TikTok has published from this deployment since 2026-08-27, on
the inbox path, against the app's sandbox credentials. Read the runbook below if
you are setting up another account; read the *what actually happened* section
first either way, because three of the four walls it hit look nothing like their
cause.

Three things shape all of it, and the first two are the opposite of what the
YouTube side taught:

- **The audit is real here, and an error code proves it.** YouTube's private
  lock turned out not to apply to this project (`docs/youtube-handover.md`).
  TikTok's does not have that escape hatch: `unaudited_client_can_only_post_to_private_accounts`
  is a documented 403 on the publish endpoint, and the content sharing
  guidelines state the caps in the same breath. An unaudited client posts
  `SELF_ONLY`, to a private account, for at most five posting users per 24 hours.
- **The audit reviews a product, not a project.** It is not a compliance
  checkbox the way Meta's Standard Access was. TikTok asks for a public website,
  a privacy policy, a terms of service and up to five demo videos of the
  end to end flow, and the app review guidelines say an app is rejected if it is
  "designed for private/personal use only". That sentence is the single most
  important thing on this page. See `## The audit, read honestly`.
- **There is a path that needs no audit at all**, and it is not the one that was
  planned for. `video.upload` drops the video into the creator's TikTok inbox and
  the creator publishes it from the app. No forced private, no audit, no UX
  review, one tap a day. It is not unattended, which is the whole objection to
  it, and it is the path this deployment runs on, because the other one cannot
  even be configured here.

## What actually happened when this was attempted

Two sessions on 2026-08-27, both in a driven browser signed in as the developer
account. The first stopped at step 3 believing nothing could be saved. The
second got the sandbox configured and saved. Read both halves, because the
first one's conclusion was wrong in a way that is easy to repeat.

**What is done and permanent:**

- The TikTok account `@thenightlybuild` exists, created by hand.
- The developer account and the app exist. App id `7678556799749900295`, type
  Other, ownership Individual.
- A sandbox exists, `nightlybuild-inbox`, id `7678567978794829831`, and its
  **configuration is saved**: icon, category, description, terms, privacy and
  website URLs, the Login Kit and Content Posting API products, the three
  scopes, and the redirect URI. Verified by reloading the page rather than by
  the absence of an error.
- **`gate.nordbye.it` is a verified URL property.** This is the expensive step
  and it survives everything else. Domain verification, one DNS TXT record,
  `tiktok-developers-site-verification=...`, held in the homelab repo at
  `terraform/cloudflare/nordbye-it/dns.tf`. A verified domain carries its
  subdomains and every URL beneath it, so this one record covers the media
  TikTok pulls **and** the privacy, terms, website and OAuth callback URLs.

**What blocked it, in the order the walls appeared:**

1. **A DNS ad blocker breaks the portal silently.** AdGuard on the local
   network sinkholes `tiktokv.com`, `tiktokw.eu` and `byteoversea.com`, which
   are where TikTok loads the SDK that signs its own API requests from. With
   them blocked the portal loads, logs in and reads fine, and every write
   silently does nothing: the console shows `a.init is not a function` and the
   click handler dies before it sends anything. Three `@@||domain^` exception
   rules fix it and they stay. Anyone doing this from a network with DNS
   filtering will hit this first and it looks nothing like its cause.
2. **The URL fields will not take a GitHub URL.** "This URL is not verified",
   because TikTok requires privacy, terms and website URLs to sit on a domain
   proved by DNS record, and nobody can put a record on `github.com`. This is
   what moved those documents onto the gateway.
3. **The products have to be added before the scopes exist.** With no products
   on the app, the Add scopes dialog offers only `user.info.profile`,
   `user.info.stats` and `video.list`, which reads as "this app may never have
   `video.upload`" and is not what it means. Add Login Kit, then Content
   Posting API, which requires it; `user.info.basic` and `video.upload` arrive
   attached to their products and cannot be added on their own.
4. **The redirect URI has to be https, and loopback is not an exception.** The
   field rejects `http://127.0.0.1:8723/callback` with "Enter a valid URL
   beginning with https://" and rejects `https://127.0.0.1:8723/callback` too.
   `scripts/tiktok_authorise.py` ran a loopback listener until this was found.
   It now sends the browser to `https://gate.nordbye.it/tiktok/callback`, a
   page the gateway serves that prints the authorisation code, and the operator
   pastes the address back into the waiting script.

**The wall that was not one.** The first session recorded that the sandbox
form "validates clean, reports no errors, and dispatches no network request at
all". Two of those three were wrong. `Apply changes` runs a client side
validation and sends nothing when it fails, which does look exactly like a dead
button; and the page **was** reporting `This form has 1 error`, in a banner
that is easy to miss and against a field that was empty because an earlier fill
had gone into the wrong input. The field was **Web/Desktop URL**, which only
appears once the Web platform checkbox is ticked, so it is both required and
invisible until something else is done. Filled in, `Apply changes` sent
`POST /devportal/sandbox/publish` and returned 200.

**So the lesson is about reading the portal, not about the portal being
broken.** When a save appears to do nothing, look for the error banner and for
a required field that appeared late, before concluding the page is dead.

**Production still cannot be saved, and this part of the first session's
account holds.** Save in the production configuration reports two errors and
both are in the App review block: a usage description, which can be written,
and "Upload at least one demo video that shows the complete end-to-end flow",
which cannot. There is no save-as-draft that skips them. What TikTok specifies
is a recording of "the website or app where the features will actually be
integrated" showing "the user interface and user interactions" for every
product and scope, and this project has no such interface: the consent is a CLI
script and the posting is a background scheduler. Building the screen described
in the content sharing guidelines is the project, and `## The audit, read
honestly` below still expects a refusal afterwards.

**Which means the sandbox is the path, and it is the one that was built for.**
The inbox upload needs no audit, and a sandbox exercises it against target
users added by hand. What a sandbox cannot do is post publicly through Direct
Post, which this deployment does not use.

**`video.publish` is still not held.** It arrives with the Direct Post switch
inside the Content Posting API product, which is deliberately off, so
`scripts/tiktok_authorise.py` does not request it. Requesting a scope the app
does not hold fails the whole authorisation rather than being dropped from it.

## Activation, in order, with this deployment's values

Written 2026-08-27, when the code was merged and running and nothing account
shaped existed: `accounts` had an Instagram row and a YouTube row and no TikTok
row, `tiktok_credentials` had no rows, the render host had no `TIKTOK_OPEN_ID`,
and `GATEWAY_SLOTS` had no TikTok line. **All seven steps were completed that
afternoon**, so this now reads as the procedure for the next account rather than
as a to-do list. Everything in it is portal work and one config change; no code
is waiting on any of it.

**The order is forced, and not by preference.** Step 5 produces the open id,
and the open id is what steps 6 and 7 are keyed on, so nothing after step 5 can
be done ahead of it. Doing 7 before 6 is the safe way round: a queued TikTok row
with no slot waits harmlessly, and a slot firing with `GATEWAY_TIKTOK_ENABLED`
off fails that row rather than retrying it, because a flag that is off is not a
transient condition.

1. **A TikTok account for the content**, and a decision about whether it is the
   same identity as the Instagram and YouTube ones. Nothing forces them to
   match. Not a Business account; the creator info endpoint reports whatever
   options the account has and the API works from those.

2. **A developer account** at <https://developers.tiktok.com> against that
   login, developer terms accepted, then an app from Manage apps, then a
   **sandbox** on it. Everything below is done in the sandbox configuration,
   not the production one, because production will not save without a demo
   video of a user interface this project does not have.

   In the sandbox, in this order, because the order is load bearing:

   - **Add Login Kit, then Content Posting API**, which requires it. Leave the
     Direct Post switch off: the shipped default is the inbox path, which needs
     no audit.
   - **Then add `video.list`.** The other two scopes, `user.info.basic` and
     `video.upload`, arrive attached to their products and cannot be added on
     their own. Before the products are on, the Add scopes dialog offers
     neither, which reads as a refusal and is not one.
   - Three scopes and no more, because asking for scopes the app does not use
     is a named rejection reason at audit time. All three in one consent trip;
     adding one later means going back through it.

3. **Four things in the portal that are easy to miss, and all of them fail
   late.**

   - **Redirect URI**, character for character:
     `https://gate.nordbye.it/tiktok/callback`. **It must be https**, and
     loopback is not an exception, which is why the authorise script no longer
     runs a listener. `gateway/pages.py` serves that page and
     `k8s/talos/apps/reelsmith/httproute.yaml` has to allow the path, since
     that file is an allowlist.
   - **Tick the Web platform checkbox before hunting for the website field.**
     `Web/Desktop URL` is required and does not exist until Web is ticked, so
     a form that will not save may be complaining about a field that is not on
     the page yet.
   - **Read the error banner when a save does nothing.** `Apply changes`
     validates in the browser and sends no request when it fails. A dead button
     and a failed validation look identical from the network tab.
   - **A verified URL property for `https://gate.nordbye.it`**, in the URL
     properties widget, plus the DNS record it hands you. **This is no longer
     needed for publishing**, since 2026-08-28: the publisher pushes the bytes
     with `FILE_UPLOAD` rather than having TikTok fetch them, so no post
     depends on a verified domain. It is still what the three legal URLs and
     the OAuth callback are checked against.

     **Read the next bullet before doing this one.**
   - **URL properties are per configuration, and that is the trap.** Verifying
     `gate.nordbye.it` on the production configuration does nothing for the
     sandbox, which mints a *different* signature string and therefore needs a
     second TXT record on the same name. The portal states it once, as "You
     must verify URL properties for all configurations with a URL", and the API
     never mentions configurations at all: it answers
     `url_ownership_unverified` and names the URL, which sends you to check
     DNS, where you find the record present and correct.

     This cost the first TikTok Reel ever queued, on 2026-08-28. Production had
     been verified the day before, the gateway authenticates as the sandbox,
     and the credentials say which: **a sandbox client key starts `sb`.**

4. **Connect the account to the sandbox as a target user**, from Sandbox
   settings, Add account. It redirects to a TikTok login, so it needs the
   content account signed in **in that browser**, and it is a step a person
   does rather than a script. A sandbox reaches only its target users, so
   without this the consent trip in the next step has nothing to consent to.

5. **The consent trip**, from this laptop, once, with the **sandbox** client
   key and secret rather than the production pair:

   ```bash
   TIKTOK_CLIENT_KEY=... TIKTOK_CLIENT_SECRET=... \
     uv run python scripts/tiktok_authorise.py --username '@handle'
   ```

   The keys are read from the environment and never from argv, which is visible
   in `ps` and lands in shell history. It opens the consent screen, TikTok
   redirects to `https://gate.nordbye.it/tiktok/callback`, that page prints the
   code, and the script waits for the whole address to be pasted back. It reads
   `GATEWAY_URL` and `GATEWAY_TOKEN` from `.env` the way `youtube_authorise.py`
   does, exchanges the code and registers the account and its credentials with
   the gateway in one call, then prints the open id and the two lines it is
   needed in.

   **Paste the address, not the code.** The state is checked against the one
   the script generated, and a bare code carries no state to check.

   It prints the open id **before** it registers, so a gateway that refuses is
   not also a lost open id. The refresh token is a different matter: it never
   reaches a file or a terminal unless `--print-token` asks, and the documented
   recovery for a broken chain is another trip through this script.

6. **A homelab PR**, `k8s/talos/apps/reelsmith/configmap.yaml`, both halves in
   the same change:

   ```yaml
   GATEWAY_TIKTOK_ENABLED: "true"
   ```

   and one more `GATEWAY_SLOTS` line, with the open id from step 4:

   ```
   08:10 Europe/Oslo jitter=15 account=<open_id>
   ```

   One a day. The inbox path allows five pending shares per 24 hours and each
   one is a tap in the app, so the cap is attention rather than API quota.

   **The same time as the other two, deliberately.** Since 2026-08-27 all three
   destinations fire at 08:10 Europe/Oslo, which is 06:10 UTC. Only Instagram
   produces a skip rate, so there is nothing to argue a different hour from for
   the other two, and one window is easier to keep gateway deploys out of than
   four. They land minutes apart anyway, because the jitter is derived per slot
   rather than shared.

   Put an explicit `account=` on it like every other line: a line without one
   attaches to the single registered Instagram account, and the config sweep
   freezes rather than deleting anything it cannot attribute.

7. **`TIKTOK_OPEN_ID` on the render host**, in
   `accounts/nightlybuild/.env` on the share, next to `YOUTUBE_CHANNEL_ID`.
   That file, not the root `.env`: the open id is a property of one account.
   The open id is all that host needs, because the gateway holds the
   credentials, so no TikTok secret reaches the machine that renders.

   Until this line exists the nightly fan-out skips TikTok silently and queues
   only the Reel and the Short, which is what every render did before this.
   After it, `--enqueue` and `--recover` make a third row from the same MP4,
   uploaded once, and `queued.json` gains a `tiktok_id`.

**Then check it the way `## Verify it by API, not by the green ticks` says**,
not by the portal's green ticks. Seeing
`unaudited_client_can_only_post_to_private_accounts` is the proof that
everything else is wired correctly.

## The two paths, and which one you are on

| | Direct Post | Inbox upload |
|---|---|---|
| Scope | `video.publish` | `video.upload` |
| Init endpoint | `/v2/post/publish/video/init/` | `/v2/post/publish/inbox/video/init/` |
| Sets privacy by API | yes, `post_info.privacy_level` | no, the creator picks in the app |
| Needs the audit | yes, or everything is `SELF_ONLY` | no |
| Needs the UX review | yes | no |
| Unattended | yes, once audited | no, one tap per video |
| Daily cap | ~15 posts per creator once audited | 5 pending shares per 24 hours |

Both take the same media, both support `PULL_FROM_URL`, and both return a
`publish_id` polled at the same status endpoint. **The publisher is nearly the
same code either way**, which is what makes building for the inbox path first a
cheap hedge rather than throwaway work.

## What you need

1. **A TikTok account for the content**, and a decision about whether it is the
   same identity as the Instagram and YouTube ones. Nothing forces them to
   match. A Business account is not required for either posting path; the
   creator info endpoint reports the account's own privacy options and the API
   works from whatever those say.

2. **A TikTok for Developers account** at <https://developers.tiktok.com>,
   registered against that TikTok login, with the developer terms accepted.
   Individual registration is enough for everything short of the audit,
   confirmed on 2026-08-27: the app, the sandbox, both products, all three
   scopes, the URL property and the consent trip were all done on one. Whether
   it is enough to *submit* the audit is still unknown and is blocked behind the
   demo video rather than behind the registration type.

3. **An app**, created from Manage apps. The app review guidelines want the app
   name to be the product's actual name rather than a description of what it
   does, an icon, a description, and a **valid official website that is not a
   landing page or a login page**, with the privacy policy and terms of service
   reachable from it without opening a menu.

   The portal is stricter than that reads. All three URL fields refuse a domain
   it cannot verify by DNS record, so a GitHub blob URL simply will not save,
   and that is what moved these documents onto the gateway: `GET /`, `/privacy`
   and `/terms` off `gateway/pages.py`. `docs/privacy.md` and `docs/terms.md`
   are pointers to those templates now, not the documents.

   **`Web/Desktop URL` is required and does not exist until the Web platform
   checkbox is ticked**, which is worth knowing because a form refusing to save
   over a field that is not on the page yet reads as a broken form.

4. **The Content Posting API product added to the app**, and for Direct Post,
   **Direct Post configuration enabled** on it. These are two separate switches
   and the second is easy to miss.

5. **Scopes, and they do not exist until the products are added.** With no
   products on the app the Add scopes dialog lists only `user.info.profile`,
   `user.info.stats` and `video.list`, which reads as a permanent refusal and is
   not one. `user.info.basic` arrives attached to Login Kit and `video.upload`
   to the Content Posting API, and neither can be added on its own.
   `video.publish` arrives only with the Direct Post switch inside that product.

   So for the inbox path it is three: `user.info.basic`, `video.upload`,
   `video.list`. The last carries the view and engagement counts and without it
   nothing comes back at all. Asking for scopes the app does not use is a named
   rejection reason, so do not add the rest.

6. **A sandbox, which turned out to be the whole path rather than
   scaffolding.** Manage apps, toggle to Sandbox mode, Create Sandbox. Up to 5
   sandboxes per app and up to 10 target users each. The app review guidelines
   say a first time app **must** use the sandbox for its demo videos, and
   independently of that it is the only configuration that can be saved at all
   here, because production validates the App review block and demands a demo
   video of an interface this project does not have.

   Adding a target user redirects to a TikTok login and **logs you out first**,
   so it needs the content account's password to hand and cannot be done by a
   driven browser holding only a developer session.

   The ceiling on it: **a sandbox cannot post public videos through the Content
   Posting API**, so it proves the wiring and nothing about visibility. On the
   inbox path that costs nothing, since the creator publishes from the app.

7. **A verified domain or URL prefix**, if you use `PULL_FROM_URL`. This
   publisher does not, so this is here for the legal URLs and as the record of
   why the fetch path was abandoned. Add the
   property in the URL properties widget on the app, then add the signature
   string TikTok gives you to the domain's DNS. A verified domain covers every
   path under it and its subdomains; a verified URL prefix covers only URLs with
   that exact prefix, so the domain is the one worth doing: **one record covers
   the media, the three public pages and the OAuth callback together.**

   It is a **TXT record** holding `tiktok-developers-site-verification=<string>`,
   confirmed on 2026-08-27 for `gate.nordbye.it` and held in the homelab repo at
   `terraform/cloudflare/nordbye-it/dns.tf`. It verified on the first check with
   no waiting worth measuring. This is the single most durable step on the page:
   it survived every other thing that failed.

8. **An OAuth round trip**, once per account, storing `client_key`,
   `client_secret` and the refresh token. The access token lasts 86,400 seconds
   and the refresh token 31,536,000, which is 24 hours and a year.

   **The redirect URI must be https, and loopback is not an exception.** The
   field rejects `http://127.0.0.1:8723/callback` and the https form of the same
   address alike, with "Enter a valid URL beginning with https://". That is what
   removed the loopback listener from `scripts/tiktok_authorise.py`: the browser
   lands on `GET /tiktok/callback` on the gateway, that page prints the code,
   and the operator pastes the whole address back so the CSRF state can still be
   checked. The httproute allowlist in homelab has to carry that path or the
   consent trip lands on a 404 and is spent.

## The audit, read honestly

The audit is submitted at <https://developers.tiktok.com/application/content-posting-api>
and it is what lifts the private lock. Two facts about it decide whether this
integration can exist in the shape you want.

**It is largely a UX review, and the UX it reviews is one this repo does not
have.** The content sharing guidelines are a specification of a screen, and the
audit checks the screen. Before a post, the app must show the creator's
nickname, a preview of the content, and a note that processing takes a few
minutes. It must offer a title field, a **privacy dropdown with no default
value** whose options come from a live `creator_info` call, and Allow Comment,
Duet and Stitch checkboxes that are **unchecked by default** and greyed out
where `creator_info` says the creator has disabled them. It must carry a
commercial content disclosure toggle, off by default, and when it is on, a
choice of Your Brand or Branded Content with the exact label text each produces,
and the matching consent line about the Music Usage Confirmation and the Branded
Content Policy. If the toggle is on and neither option is chosen, the publish
button must be disabled with a specific hover message.

Reported rejections cluster on exactly these points rather than on anything
technical, with the privacy dropdown's missing default the one most often cited
back in the rejection mail.

**And the same guidelines say, in terms, that what you are building is not
allowed.** No automatic posting without express user consent, and no unattended
posting, with users actively consenting before the upload begins. Read next to
the app review guideline that rejects an app "designed for private/personal use
only", the honest reading is that a nightly job posting to its own account is
not the thing this programme is for. Third party accounts of the process say the
same thing more bluntly and put the turnaround at two to six weeks with multiple
rounds.

**What that means in practice**, and this is a judgement rather than a quote:

- The audit is free and blocks nothing else, so submitting it costs a form and
  some waiting. Do that.
- Do not describe reelsmith as a personal utility on the form, and equally do
  not describe it as something it is not. The defensible framing is the one that
  is true: it is a publishing tool with a scheduled queue, an approval step and a
  cancel control, which is what the admin panel already is. The Queue page with
  its hook, its cancel button and its slot list is a screenshot worth having.
- **Budget for a no.** Build so that a refusal costs a config flag rather than a
  rewrite, which is what the two paths sharing a publisher buys.

**Passing the audit does not backfill.** Anything posted while unaudited stays
private, and no later approval republishes it. So there is nothing to gain from
posting real videos before the gate opens.

## Five things that will cost you an hour each

1. **`creator_info` is mandatory and it is not a formality.** Every post must be
   preceded by `POST /v2/post/publish/creator_info/query/`, because the privacy
   options you are allowed to offer come from it and TikTok checks that you used
   them. Sending a `privacy_level` the creator's account does not currently
   support fails with `privacy_level_option_mismatch`, which reads like a bad
   constant and is actually a stale read. It also returns
   `max_video_post_duration_sec`, which is the only authoritative answer to how
   long a video may be.
2. **The refresh token rotates.** The token endpoint may hand back a different
   `refresh_token` than the one you sent, and the docs say you must use the new
   one. Storing only the original means the integration works until the first
   rotation and then dies with no clock to explain it. This is the opposite of
   the YouTube case, where the refresh token is stable and there is no refresher
   loop at all.
3. **The access token is 24 hours, so there has to be a loop.** Instagram's
   refresh rides on the daily `--snapshot` job and YouTube needs none. Neither
   pattern fits: a token minted per publish is fine, but nothing on the render
   host is awake to do it, so the refresh belongs next to the gateway's own
   background tasks.
4. **`PULL_FROM_URL` fails as `url_ownership_unverified`**, which names the URL
   rather than the missing DNS record, and it fails at init rather than at
   download. The failure looks like a bad media URL and is a portal step nobody
   did, on a configuration nobody thought about. **This is why the publisher
   uses `FILE_UPLOAD` instead**, since 2026-08-28.
5. **The caption goes in `title`, and it is 2,200 UTF-16 runes.** There is no
   separate description field, so the caption and the hashtags share one string
   with the link. This is a third shape after Instagram's caption and YouTube's
   title plus description, and `pipeline/gateway.py` is where it belongs.

## The call sequence

Direct Post, in order, all against `https://open.tiktokapis.com`:

1. `POST /v2/post/publish/creator_info/query/`, 20 requests per minute per
   token. Returns `privacy_level_options`, `comment_disabled`, `duet_disabled`,
   `stitch_disabled` and `max_video_post_duration_sec`.
2. `POST /v2/post/publish/video/init/`, 6 requests per minute per token, with
   `post_info` and `source_info`. Returns `publish_id`, and for `FILE_UPLOAD`
   an `upload_url` valid for one hour.
3. For `FILE_UPLOAD` only, `PUT` the bytes to `upload_url` with `Content-Type`,
   `Content-Length` and `Content-Range: bytes {FIRST}-{LAST}/{TOTAL}`.
4. `POST /v2/post/publish/status/fetch/` with the `publish_id`, 30 requests per
   minute. Status is one of `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`,
   `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE` or `FAILED`.

The inbox path replaces step 2 with `POST /v2/post/publish/inbox/video/init/`
and drops `post_info` entirely, and step 4 finishes at `SEND_TO_USER_INBOX`
rather than `PUBLISH_COMPLETE`.

`post_info` fields: `privacy_level` (required, one of `PUBLIC_TO_EVERYONE`,
`MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`), `title`,
`disable_duet`, `disable_stitch`, `disable_comment`, `video_cover_timestamp_ms`,
`brand_content_toggle`, `brand_organic_toggle`, `is_aigc`.

**`is_aigc` is a decision, not a default.** It is the same question
`containsSyntheticMedia` asks on the YouTube side, which
`docs/youtube-handover.md` records as answered `false` because a value had to be
sent rather than because the question was settled. The voice is a clone of a
real person reading a script that person commissioned. Answer it the same way on
both platforms or write down why not.

**`video_cover_timestamp_ms` is the cover seam.** Instagram gets `cover_url` and
falls back to `thumb_offset` at `COVER_FRAME`; YouTube gets nothing and picks a
frame. TikTok takes a timestamp, so `COVER_FRAME / fps * 1000` is the same
number `pipeline/publisher.py` already computes for `thumb_offset`.

## The media is already right

Nothing about the renderer has to change. Measured against what
`config.py` produces at 1080x1920, 30fps, H.264, about 30 to 45 seconds:

| Limit | TikTok allows | We produce |
|---|---|---|
| Container and codec | MP4, WebM, MOV; H.264, H.265, VP8, VP9 | MP4 / H.264 |
| Size | up to 4 GB | tens of MB |
| Frame rate | 23 to 60 | 30 |
| Resolution | 360 to 4096 on both axes | 1080x1920 |
| Duration | 3 seconds to 10 minutes, and never past `max_video_post_duration_sec` | about 30 to 45 seconds |

`FILE_UPLOAD` chunking: 5 MB minimum, 64 MB maximum with a final chunk up to
128 MB, at most 1,000 chunks, and a video under 5 MB goes as a single chunk.
**None of it is implemented and that is deliberate.** A whole video up to 64 MB
is one chunk, renders here are about 10 MB against a 13 MB worst case, and
`tiktok.MAX_SINGLE_CHUNK` refuses anything larger by name rather than shipping
a multi chunk uploader that nothing would ever exercise. TikTok answers a
malformed multi chunk init with a generic error naming nothing, so the local
refusal is the more useful failure.

This paragraph used to end by calling chunking the second reason to prefer
`PULL_FROM_URL`. That preference did not survive contact with the portal: see
the URL properties bullets above.

## What comes back, and what does not

**There is no retention metric.** `/v2/video/query/` under the `video.list`
scope returns `view_count`, `like_count`, `comment_count`, `share_count`, plus
`id`, `create_time`, `duration`, `share_url`, `cover_image_url`, `title`,
`embed_link` and `is_aigc`, up to 20 ids per request. Nothing exposes watch
time, completion or anything from which a three second skip could be computed.
`/v2/video/list/` pages the account's own videos, 20 at a time, newest first.

So the rule `PLAN.md` H6 set for YouTube holds here without argument:
**`skip_rate` stays Instagram only and TikTok numbers do not reach
`_results_block`.** Views, likes, comments and shares are worth storing and
showing on the Posts page. They are not worth feeding to the prompt that writes
tomorrow's script, because the loop turns on a number TikTok does not have.

One consequence for the insights sweep: the video id is not the `publish_id`.
`status/fetch` reports the post completed; getting from there to something
`video/query` accepts means listing the account's recent videos and matching,
which is a shape the Meta sweep never needed.

## Verify it by API, not by the green ticks

Two calls settle the two things the portal will show as fine either way.

```bash
# 1. Does the token actually carry the scope, and what may this account post?
curl -s -X POST "https://open.tiktokapis.com/v2/post/publish/creator_info/query/" \
  -H "Authorization: Bearer $TIKTOK_ACCESS_TOKEN" \
  -H "Content-Type: application/json; charset=UTF-8"
# {"data":{"creator_username":"...","privacy_level_options":["PUBLIC_TO_EVERYONE",...],
#          "max_video_post_duration_sec":600},"error":{"code":"ok",...}}
```

`"code":"scope_not_authorized"` means the consent round trip did not include
`video.publish`, which is indistinguishable from an app misconfiguration until
you make this call. A `privacy_level_options` list without
`PUBLIC_TO_EVERYONE` means the account itself is private, which is a different
problem from the audit and produces the same silence.

```bash
# 2. Is the domain actually verified? Ask for a post you expect to be refused.
curl -s -X POST "https://open.tiktokapis.com/v2/post/publish/video/init/" \
  -H "Authorization: Bearer $TIKTOK_ACCESS_TOKEN" \
  -H "Content-Type: application/json; charset=UTF-8" \
  -d '{"post_info":{"privacy_level":"SELF_ONLY","title":"probe"},
       "source_info":{"source":"PULL_FROM_URL",
                      "video_url":"https://gate.nordbye.it/media/probe.mp4"}}'
```

`url_ownership_unverified` is the DNS record. `unaudited_client_can_only_post_to_private_accounts`
is the audit, and seeing it is the proof that everything else is wired
correctly.

## Operational facts

- **Access token 24 hours, refresh token 365 days, and the refresh token
  rotates.** Store what comes back, not what you sent.
- **Rate limits are per user access token**: 6 a minute on init, 20 on
  `creator_info`, 30 on `status/fetch`. Not a constraint at one post a day.
- **Daily caps are the real limit.** Unaudited, five posting users per 24 hours
  and everything private. Audited, a creator cap set by the audit application and
  reported as roughly 15 posts per creator per day. The inbox path allows five
  pending shares per 24 hours, which is what `spam_risk_too_many_pending_share`
  means when it appears.
- **`SEND_TO_USER_INBOX` is a success on the inbox path and an intermediate
  state nowhere else.** A publisher that treats anything but `PUBLISH_COMPLETE`
  as unfinished will hang forever on a draft.

## What does not port from the other two

- **`add_caption_cta` in `pipeline/gateway.py`.** The comment ask is dormant on
  all three surfaces and the follow ask is the current CTA, but the caption is
  built for Instagram and rebuilt for YouTube by `youtube_description`. TikTok is
  a third shape: one `title` field carrying the ask, the link and the hashtags
  together, capped at 2,200.
- **`_results_block` in `pipeline/results.py`.** See above. There is no skip
  rate and there is no substitute for one.
- **The DM mechanic in full.** No private reply API, so `posts`,
  `comments_handled`, `conversations` and `deliveries` stay Instagram only, the
  way they already are for YouTube.
- **The token refresher.** Instagram's rides on `--snapshot` and YouTube has
  none. TikTok needs a real one on the gateway's own loop.

## What was unknown, and what is still

This section was five open questions written from the public docs on
2026-08-26. Three were answered in the portal on 2026-08-27 and are recorded
here rather than deleted, because "we checked" is worth more than a shorter
list.

**Answered:**

- **The DNS record is a TXT record** holding
  `tiktok-developers-site-verification=<string>`. It verified on the first
  check, with no propagation delay worth measuring.
- **Direct Post configuration is available before any audit.** It is a switch
  inside the Content Posting API product, present in both the production and
  the sandbox configuration, and it is what grants `video.publish`. Having it
  is not the same as being allowed to use it: unaudited, the documented 403 is
  `unaudited_client_can_only_post_to_private_accounts`. It is deliberately off
  here.
- **Individual registration is enough to build everything short of the audit**:
  the app, the sandbox, the products, the scopes, the URL properties and the
  consent trip were all done on an Individual account.

**Still unknown, and now cheaper to leave that way:**

- Whether individual registration is enough to *submit* the audit, or whether
  an organisation and a business verification are demanded.
- Whether the audit form asks for a creator cap, and what number to ask for.
- Whether the app review form treats a single account publisher as in scope.

**All three are blocked behind the same wall and none of them is the next
thing to do.** The production configuration cannot be saved at all without a
demo video of an end to end user interface, and there is no save-as-draft that
skips it. So the audit cannot be submitted, and the questions about the audit
form are unanswerable, until a posting screen exists. Building that screen is a
project rather than a configuration step, and `## The audit, read honestly`
below expects a refusal at the end of it anyway.

The inbox path needs none of this, which is why it is the one that shipped.

## Sources

Read 2026-08-26 against the public documentation. Everything on this page
marked as observed rather than quoted was checked in the portal on 2026-08-27,
signed in as the developer account, and `## What actually happened when this
was attempted` is the record of that.

- [Content Posting API, get started](https://developers.tiktok.com/doc/content-posting-api-get-started)
- [Direct Post reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post)
- [Inbox upload reference](https://developers.tiktok.com/doc/content-posting-api-reference-upload-video)
- [Query creator info](https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info)
- [Get post status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status)
- [Media transfer guide](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide)
- [Content sharing guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines)
- [App review guidelines](https://developers.tiktok.com/doc/app-review-guidelines/)
- [Scopes](https://developers.tiktok.com/doc/tiktok-api-scopes)
- [OAuth token management](https://developers.tiktok.com/doc/oauth-user-access-token-management)
- [Video query](https://developers.tiktok.com/doc/tiktok-api-v2-video-query)
- [Sandbox](https://developers.tiktok.com/doc/add-a-sandbox/)
- [Audit application](https://developers.tiktok.com/application/content-posting-api)
