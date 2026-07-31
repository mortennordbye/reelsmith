# Instagram account

Decisions and setup notes for the account this pipeline publishes to.
Written 2026-07-31. Anything marked TODO is not done yet.

---

## 1. Identity

| Field | Value |
|---|---|
| Brand name | **The Nightly Build** |
| Handle | `@thenightlybuild` |
| Display name | `The Nightly Build` |
| Category | Digital creator, or Software (set once the account is professional) |

**Why this name.** A nightly build is a term the target audience already owns,
so it signals "this is for you" without explaining itself. It also carries the
cadence, which sets the expectation that there is something new tomorrow. It
reads as a brand rather than a description, so it survives the content widening
from GitHub repos to tooling news generally.

The two costs, accepted knowingly:

- It does not contain "GitHub", "dev", or "tools", so it does nothing for
  handle search. That matters less than it sounds: on Instagram cold discovery
  comes from Reels distribution, and the classifier reads the caption, the
  on-screen text, and the display name, not the handle. The bio and display
  name carry that load instead.
- It implies every night. See section 7.

The signup form's "Full name" is the same field as "Name" in Edit Profile, the
bold line above the bio. It takes the brand, not a legal name, and it is indexed
for search. It is rate limited to roughly two changes per 14 days, so set it
once.

### Bio

Draft, 150 character limit:

```
Trending dev + AI tools, daily.
45 seconds. No hype, no "game-changer".
↓ today's repo
```

85 characters. The last line points at the bio link, which gets updated to the
current repo each day, giving people a reason to open the profile rather than
scroll past.

No personal byline for now, by choice. Worth revisiting later: in a niche full
of faceless generated tech accounts a human name is cheap credibility, and it
would be honest here because posting is manual (every render is watched, and
`--posted` is a separate deliberate step). The reason to be deliberate about it
is that scripts are generated, and while the prompt forbids inventing facts,
benchmarks and version numbers, nothing enforces that. Review is the only fact
check.

### Profile picture

**In use: `brand/avatars/b-moon.png`** — a single crescent in
`theme.color.accent` (`#58A6FF`) on `theme.color.bgDeep` (`#010409`). All
palette values come from `video/src/theme.ts`, so the avatar and the videos
match.

It was the strongest of everything rendered at 32px: one shape, one colour, no
text, and the only candidate nobody else in the feed will have.

Candidates were judged at 32px, the size next to a Reel in the feed, since that
is what actually decides recognition. Full set is in `brand/avatars/`,
regenerate with `python brand/avatars.py` and `python brand/prompt_variants.py`.

Runner-up: `j-chevron-accent.png`, a white chevron and blue cursor. Clean and
unambiguous, but chevron-plus-cursor is the most common mark in developer
tooling, so it reads as generic.

Rejected: `d-prompt` (the `$` reads as a currency symbol at feed size, so the
first impression is a finance account), `i-dollar-refined` (muting the `$` loses
the prompt reading without fixing anything), `h-caret` (the `❯` breaks up
small), `c-moon-commit` (branch lines vanish at 32px), the `nb` monograms
(unreadable small, and the initials mean nothing to a stranger).

Instagram allows up to 5 links in the bio. Use one for the current repo, and
later a second for a GitHub or personal page.

### How the handle landed

`nightlybuild` and `nightly.build` were both taken, which is normal for short
clean handles and usually means a dormant account holds them. Instagram's
suggested alternatives (`nigh.tlybuild`, `night.lybuild`) split the word in the
wrong place and are unsayable, so they were rejected.

`thenightlybuild` was free and the definite article was adopted into the brand
rather than left as a mismatch. A `the-` prefix only reads as scar tissue when
the display name lacks it. With both aligned, "The Nightly Build" reads as a
masthead, which suits a recurring daily column better than the bare noun did.

The display name is not unique and carries most of the recognition, so the
handle mattered less than it seemed at signup.

---

## 2. Email

**Decision: a dedicated alias on `nordbye.it`**, not the primary address.
Address: `thenightlybuild@nordbye.it`.

Matches the handle exactly. Account #2 gets its own brand word rather than a
prefix convention, which reads better and scales the same.

Reasons:

- Instagram's official policy is one unique email per account. A per-account
  alias on a domain you already control scales to account #2 and #3 without
  inventing a new mail provider each time.
- Recovery stays on infrastructure you own. A Gmail lockout would take the
  Instagram account with it.
- It keeps the accounts from being trivially linked to each other and to your
  personal identity. If one account ever gets flagged, shared contact details
  are the first thing that associates the others with it.

`nordbye.it` is on Google Workspace (MX points at `aspmx.l.google.com`, SPF
includes `_spf.google.com`), so aliases are supported and free. Create them in
the Admin console, not in Gmail. Gmail cannot create an address, it can only
send from one that already exists.

1. [admin.google.com](https://admin.google.com), signed in as `morten@nordbye.it`
2. Directory > Users > your user
3. User information > **Email aliases** (older UI: "Alternate email addresses")
4. Type `thenightlybuild` into the top **Alternative email** field, Save

Use the top field, not the expandable "Alternative emails with user alias
domains" section below it. That one defaults to `nordbye.it.test-google-a.com`,
Google's auto-generated test domain, which does not receive external mail.
Confirm the domain reads plain `nordbye.it` before saving.

No licence seat is consumed. The limit is 30 aliases per user, which covers
several accounts.

Then enable sending from it: Gmail > Settings > See all settings > Accounts and
Import > Send mail as > Add another email address. Because it is an alias of
your own account, Google normally skips verification.

Add a filter on `to:thenightlybuild@nordbye.it` with a label. Instagram security
alerts and login codes are exactly the mail you do not want buried in the main
inbox.

Two limitations to be aware of:

- An alias is not a separate Google account. You cannot sign in to Google with
  it and it cannot later be converted into its own user. If the account ever
  needs genuinely separate ownership or to be handed over, that needs a real
  user seat or a different provider.
- All aliases still share one mailbox and one domain, so this separates
  identities, not ownership. That is the right tradeoff here, but it is not
  isolation.

If keeping Instagram mail out of the personal inbox matters, the free
alternative is a Google Group at the same address with you as the only member.
It gets its own archive and can take additional members later.

Do **not** use Gmail plus-addressing (`name+ig@gmail.com`) as the scaling
strategy. All variants resolve to one inbox, so it does not actually give you
separate identities, and some signup forms reject the plus sign outright.

### Settings

Do first:

1. **Two-factor**: Settings > Accounts Center > Password and security >
   Two-factor authentication > **Authentication app**, not SMS. Save backup codes.
2. **Recovery phone**: Accounts Center > Personal details > Contact info.
3. **Switch to Business**: Settings > Account type and tools > Switch to
   professional account. Choose Business, not Creator (see section 3).

Then confirm:

4. **Account stays Public.** A private account gets essentially no Reels
   distribution. The most damaging setting to get wrong.
5. **Allow resharing to Stories: on.** Free reach when someone shares a video.
6. **Suggest similar accounts: on.** Puts the account in follow suggestions
   next to adjacent dev accounts.
7. **Share to Facebook: on**, once the Page exists. Same video, second surface,
   no extra work.

Leave comments open. A small account needs every engagement signal available;
filter later only if it becomes a problem.

### Account hygiene at signup

- Add a phone number for recovery. It is the difference between a recoverable
  account and a lost one.
- Turn on two-factor immediately, using an authenticator app rather than SMS.
- Save the backup codes somewhere outside the phone.
- Password in the password manager, not reused from anything.

---

## 3. Account type

**Set it to Business, not Creator, and not Personal.**

- Personal is disqualified outright. The Content Publishing API does not work
  with personal accounts, and neither do full insights.
- Creator and Business both give insights and both can connect to apps. The
  reason to pick Business is the automated posting path in `IMPROVEMENTS.md`:
  Meta's own documentation says only "Instagram professional accounts" without
  distinguishing the two, while several third-party integration guides report
  that Reels publishing via the API works with Business accounts only. That
  ambiguity is not worth testing later. Business is the superset in practice.
- Switching type is free and takes seconds, so this is reversible if Creator
  turns out to have something you want.

A Business account normally wants a linked Facebook Page. Create a bare Page
for Nightly Build at the same time and link it. The API publishing path
requires it, and doing it now avoids a second identity decision later.

### Setting up automated posting

Built. `python main.py --post` renders and publishes; `--publish <date>/<slug>`
posts a run you have already watched. This section is the one-time setup.

Two of the three blockers this section used to list turned out not to exist,
and both mattered enough to write down:

- **App Review is not needed.** Review gates *Advanced Access*, which means
  acting on accounts you do not own. *Standard Access* is granted
  automatically and covers any account holding a role on the app. Your own
  account holds a role on your own app, so an app left in development mode
  publishes fine. The "budget weeks, not days" note that was here was wrong.
- **The MP4 does not need a public URL.** Creating the container with
  `upload_type=resumable` returns an upload URI on `rupload.facebook.com` that
  takes the file as raw bytes. The public-URL requirement applies to the older
  `video_url` flow. No object storage, no S3, no R2.

What is genuinely required:

1. **Business account linked to a Facebook Page.** Section 3 above already
   commits to both.
2. **A Meta app** at <https://developers.facebook.com/apps>, type Business,
   with the Instagram product added. Leave it in **development** mode.
3. **Your account added as an Instagram tester** on the app, and the invite
   accepted from the Instagram side (Settings, Website permissions, Tester
   invites). This is the step that grants Standard Access to your own account,
   and skipping it is what produces a confusing permissions error later.
4. **Scopes** `instagram_business_basic` and
   `instagram_business_content_publish`. The Facebook Login path wants
   `instagram_basic`, `instagram_content_publish` and `pages_read_engagement`
   instead; set `IG_GRAPH_HOST=https://graph.facebook.com` if you go that way.
5. **A long-lived token.** Authorise once, exchange the short-lived token, put
   it in `IG_ACCESS_TOKEN`, then run `python main.py --refresh-token` to move
   it into `data/ig_token.json`.
6. **Your IG user ID**, a 17-digit number, into `IG_USER_ID`.

Two operational facts worth knowing before you rely on it:

- **Tokens last 60 days and an expired one cannot be refreshed.** Recovering
  means going back through the dashboard in a browser. The daily `--snapshot`
  job refreshes automatically inside a 15-day margin, so that job is now load
  bearing for posting, not just for scoring.
- **Rate limit is 100 published posts per rolling 24 hours.** Not a constraint
  at one a day.

The cover image is the one thing that still wants hosting: `cover_url` is
fetched by Meta, so without a public URL the Reel thumbnail falls back to
`thumb_offset` and the designed hook band is lost. Pass `--cover-url` if you
host it somewhere.

---

## 4. More than one account later

Nothing here needs deciding today, but two things constrain it, so they are
worth knowing before the first account is created.

- Each account needs its own unique email or phone. The alias scheme in section
  2 already handles this.
- Instagram supports about 5 logged-in accounts per device for in-app
  switching. Beyond that you are logging in and out.

**Do not build a house brand yet.** A parent brand with sibling accounts is
overhead that only pays off at three or more accounts with real audiences. For
now Nightly Build is standalone. If a second account arrives, cross-link them
in the bios and that is enough.

The one thing worth avoiding: do not split this niche across two accounts
early. Two accounts at 400 followers each perform far worse than one at 800,
because distribution is driven by per-post engagement rate and a split audience
halves the signal on both.

---

## 5. Can you post today?

**Yes.** There is no waiting period, and the "age the account for two weeks
before posting" advice circulating online comes from bulk-account operators
whose accounts are automated and unattended. It does not apply to one account
posting original video from a real device.

The real constraint is not a ban risk, it is that a first post lands in front
of almost nobody, and a visitor who taps your profile after seeing it finds an
empty grid and does not follow. So the order matters more than the date.

### Order of operations for launch day

1. Create the account, set the handle, set the email, enable 2FA.
2. Switch to Business, link the Facebook Page, set the category.
3. Set display name, bio, and profile picture. **Before the first post.**
4. Post the first Reel.
5. Have two or three more rendered and ready, so the profile fills out over the
   following days rather than sitting on a single post.

The profile picture matters more than it seems, because it is the only brand
element visible in the feed next to the video. Something legible at 32px: a
mark or two letters, not a detailed logo and not a photo.

Do not post three videos in the first hour to fill the grid. It splits the
audience across posts that each get a fraction of the distribution, and rapid
bursts from a brand new account are the pattern automated-behaviour detection
actually looks for. One a day is both the better strategy and the safer one.

---

## 6. First post choice

Use the pipeline as normal, but for post one prefer a repo that is:

- Widely recognised rather than obscure. The first video is the one people
  judge the account by, and recognition buys attention that novelty does not.
- Visually strong. The opening scene is the rendered README hero, so a repo
  with a well-designed README opens far better than one with a wall of text.
  `python main.py --candidates` shows the ranked list; the scraper already
  scores README quality, so pick from the top with the hero in mind.

Remember that rendering does not start the cooldown. Run
`python main.py --posted <owner/repo>` after actually posting, or the repo will
resurface within the 30 day window.

---

## 7. Cadence, and what the name commits you to

`repo_cooldown_days` is 30, so the pipeline will not repeat a repo within a
month. That supports a daily cadence comfortably.

The name says nightly. Two honest options:

- **Post daily.** The name is then accurate and the account has a promise the
  audience can rely on, which is the strongest retention mechanic available to
  a small account.
- **Post 3 to 4 times a week.** Fine in practice, nobody audits it, but the
  name overpromises slightly. If this is the realistic plan, put the actual
  cadence in the bio (`new tools most weeknights`) so the profile does not
  contradict itself.

Decide which one is true before launch rather than drifting into the second.

Also keep running `python main.py --snapshot` every day regardless of whether
you post, since star velocity is 55% of the candidate score and only becomes a
measured value once there is a previous day to diff against.
`launchd/it.nordbye.tech-ig.snapshot.plist` already does this at 06:00.

---

## 8. Cost

Launching adds nothing. Everything below is either free or already paid for.

| Item | Cost |
|---|---|
| Workspace alias | $0, does not consume a licence seat |
| Workspace seat | already paying; Starter is $7/user/mo annual, $8.40 monthly |
| Instagram account, Business conversion, Facebook Page | $0 |
| GitHub token | $0 |
| Script generation | $0 marginal, runs on the Claude Code subscription |
| Kokoro, faster-whisper, Playwright | $0, on-device |
| Remotion | $0 for individuals and companies of 3 or fewer |

Automated posting later stays near zero. The Meta app and app review are free.
Object storage is the only addition, and at one 20-30 MB video per day that is
roughly 1 GB a year. Cloudflare R2's free tier is 10 GB with no egress fees,
which matters because Meta's servers pull the file rather than receiving an
upload. S3 would work but charges egress.

**The real cost is subscription capacity, not money.** With `CLAUDE_MODEL=opus`,
`CLAUDE_EFFORT=high` and `CLAUDE_RESEARCH=true`, every run spends meaningfully
against Claude rate limits, and daily posting means daily runs. If the pipeline
starts competing with actual work, the dials are `--no-research` and
`CLAUDE_MODEL=sonnet`, both documented in `.env.example`.

Two things that could become real costs later:

- Remotion's licence changes above 3 people. See <https://remotion.pro/license>.
- A second Workspace seat, if an account ever needs genuinely separate
  ownership rather than an alias.

---

## 9. Checklist

Signup:

- [ ] Confirm alias support on `nordbye.it`, create `thenightlybuild@nordbye.it`
- [x] Handle secured: `@thenightlybuild`
- [ ] Create account, add recovery phone
- [ ] Enable 2FA (authenticator app), store backup codes
- [ ] Switch to Business, create and link a Facebook Page
- [ ] Profile picture, display name, bio, category
- [ ] Decide daily vs 3-4x weekly, and make the bio match

Pipeline, before the first post:

- [ ] `cp .env.example .env` and add a scopeless classic PAT. Without it a full
      run fails during discovery on the anonymous rate limit. `--repo` still
      works without one.
- [ ] Install the snapshot job, commands in the header of
      `launchd/it.nordbye.tech-ig.snapshot.plist`. **Do this first, it is the
      only time-sensitive item.** `data/` has no star history yet, so until the
      job has run on two separate days every ranking uses the damped
      stars-per-day proxy, which favours large established repos over real
      breakouts.

First week:

- [ ] Post 1 rendered and posted, `--posted` run afterwards
- [ ] 2 to 3 more videos rendered and queued
- [ ] Daily snapshot job confirmed running (`launchctl list | grep nordbye`)

Later:

- [ ] Meta developer app for API publishing (see section 3)
- [ ] Object storage for public MP4 URLs

---

## 9. Post log

Kept so the account has a record of what ran and what worked. Fill in reach and
follows a few days after each post, when the numbers have settled.

| Date | Repo | Notes | Views | Follows |
|---|---|---|---|---|
| 2026-07-31 | `DietrichGebert/ponytail` | First post. Scorer's top pick. | | |

Note on the first pick: `ponytail` had 92.5k stars in seven weeks but had not
been pushed to in 16 days. With no star history the scorer was ranking on the
damped stars-per-day proxy, which rewards exactly that pattern, so the ranking
could not see the repo going quiet. Chosen anyway for the strength of the hook.
Worth checking after a few days whether it aged well, since it is the clearest
early test of whether the proxy ranking can be trusted before velocity data
exists.

---

## 10. To verify

Things in this document that came from third-party sources rather than Meta's
own documentation, and are worth confirming when they matter:

- Whether Reels publishing via the Content Publishing API is genuinely Business
  only. Meta's docs say "professional accounts" without distinguishing.
  Mitigated by choosing Business anyway.
- The roughly 5 accounts per device switching limit. Reported consistently but
  not from an official page.

## Sources

- [Instagram Content Publishing, Meta developer docs](https://developers.facebook.com/docs/instagram-platform/content-publishing)
- [Instagram Reels API Publishing Guide, Postproxy](https://postproxy.dev/blog/instagram-reels-api-publishing-guide/)
- [Instagram API Integration Guide, Phyllo](https://www.getphyllo.com/post/instagram-api-integration-101-for-developers-of-the-creator-economy)
- [Multiple Instagram Accounts: Rules, Limits & Setup](https://lightningproxies.net/blog/can-you-have-multiple-instagram-accounts)
