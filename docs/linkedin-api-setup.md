# LinkedIn API setup

One-time setup to let the gateway post to a personal LinkedIn profile.

**Status: code merged, nothing walked yet.** The publisher, the registration
route, the callback page and the consent script exist and are tested against a
fake. The app, the consent and the first real post have not happened. Fill in
`## What actually happened` the first time this runbook is walked, because the
TikTok and Facebook runbooks both learned more from that section than from the
plan above it.

**It is one personal profile and it is not meant to grow.** Nothing here is
designed for a second LinkedIn identity, and the self-serve products this runs
on are scoped to the person who consents.

## What it costs and what it cannot do

Nothing is paid. Two self-serve products, no review:

- **Sign In with LinkedIn using OpenID Connect**, for `openid profile`, which
  is what makes `/v2/userinfo` hand back the profile id.
- **Share on LinkedIn**, for `w_member_social`, which is the post.

Three limits that shape everything else:

- **The token lasts 60 days and cannot be refreshed.** Self-serve apps get no
  refresh token. The panel warns two weeks before expiry and a post whose slot
  arrives after it fails naming the fix. Renewing is the consent trip again,
  which takes a minute.
- **Reading post statistics is not self-serve.** `memberCreatorPostAnalytics`
  needs `r_member_postAnalytics`, from the Community Management API, which
  needs a vetted application (business email, organisation, Page). So the
  numbers come from LinkedIn's own analytics export: on linkedin.com,
  Analytics, Content, Export, past 365 days, then drop the XLSX on the brand's
  Performance page. Per post it carries impressions and engagements only. Do not add the
  scope to the consent until it is granted: asking for a scope the app does not
  hold breaks the consent screen rather than being ignored.
- **The API does not scrape a link card.** The web composer reads Open Graph
  tags; the Posts API does not, by design. An article post therefore sends its
  own title and thumbnail, uploaded through the Images API, which is why a
  LinkedIn row's `cover_name` matters.

## Runbook

The order is forced, as in the other three: the consent is what produces the
account key, and the key is what everything else is written against.

1. **A Company Page.** Every LinkedIn app has to be associated with one. A bare
   Page with a name is enough; it publishes nothing and is not where posts go.
2. **The app**, at <https://www.linkedin.com/developers/apps>, associated with
   that Page. Add both products above. Under Auth, add the redirect URL
   `https://gate.nordbye.it/linkedin/callback`, character for character.
3. **The httproute, in homelab.** `k8s/talos/apps/reelsmith/httproute.yaml`
   needs an `Exact` match for `/linkedin/callback`. The route is an allowlist
   and Traefik 404s anything it does not name. Nothing in this repo checks
   that, and it is the step every other platform rollout tripped on.
4. **Deploy the gateway** with this code, before authorising. Registering
   against an older image is a 404 and the authorisation code is single use.
5. **`BRAND=` in `accounts/<name>/.env`**, before the trip, so the profile is
   grouped under its own brand in the panel.
6. **The consent trip:**

   ```
   uv run python scripts/authorise.py linkedin --account <name>
   ```

   It asks for `LINKEDIN_CLIENT_SECRET` if neither `.env` has it, opens the
   consent, takes the address bar back by hand, registers the profile with the
   gateway and writes `LINKEDIN_PERSON_ID` into the account's `.env`.
7. **A slot.** One `GATEWAY_SLOTS` line, for example
   `08:15 Europe/Oslo jitter=5 days=2,3,4 brand=<brand>` (ISO weekdays, so
   Tuesday to Thursday). Low jitter on purpose; a
   personal feed is not avoiding a pattern detector.
8. **Check it**: `python main.py --account <name> --destinations`.
9. **A first post from the panel.** Schedule, Write a LinkedIn post, leave it
   as a draft, then Publish now. Open the permalink, confirm it reads right,
   and delete it by hand if it was only a test.

## The drafter, after the first post works

10. **The style guide** at `accounts/<name>/ref/linkedin-style.md`, and
    `LINKEDIN_BLOG_FEED=` in that account's `.env`. Both private.
11. **Seed the brand** so the nightly knows what to do with it:

    ```
    python main.py --account <name> --brand-settings push --pipeline linkedin
    ```

    Do this before `sync-private.sh` carries the account to the render host.
    `--all-accounts` reports a brand with no settings as a failure, so an
    account that arrives there first turns the nightly red.
12. **A dry run** reads the blog, writes the draft into the run folder and
    queues nothing:

    ```
    python main.py --account <name> --linkedin-draft --dry-run
    ```

    Then a real one without `--dry-run`, and read the draft on Schedule. An
    entry already posted about by hand gets a draft too; cancel it, and the
    cancelled row is what keeps it from coming back.
13. **Sync** with `scripts/sync-private.sh`, which carries `ref/` and the
    `LINKEDIN_PERSON_ID` line. From then on the nightly drafts one post per new
    entry. `linkedin_auto_arm` in the brand's settings queues them armed.

## The shape of a LinkedIn row

A queue row with an empty `video_name`, which the queue route accepts for a
LinkedIn account and refuses for every other platform.

| column | holds |
|---|---|
| `caption` | the post text, escaped for LinkedIn's little format at publish |
| `link` | the blog URL; present means an article card |
| `title` | the card's title |
| `cover_name` | the card's thumbnail, or the post's image when there is no link |
| `hook` | the opening line, which is what the schedule shows |

Publishing is `scheduler.publish_queued_linkedin`. The thumbnail upload
creates nothing visible and is retried; once `POST /rest/posts` has been sent
the row stops on any doubt, because a duplicate post under your own name is
worse than a missed slot.

## Renewing the token

Run the consent trip again. It upserts the same row and moves the expiry
forward. Nothing else changes.

## What actually happened

Not walked yet.
