# YouTube API setup

One-time setup to let the pipeline publish Shorts. Everything here is generic
and applies to any channel.

Two facts shape all of it, and both are the opposite of what the Instagram side
taught:

- **The audit is not optional, and it is not about quota.** Every video uploaded
  through `videos.insert` from an unaudited API project created after 28 July
  2020 is *locked* to private. Not defaulted to private. Locked, and not
  changeable in Studio afterwards. Quota is fine without an audit; publishing is
  not. Budget weeks, not days, and submit the form before writing any code.

  **This did not hold for `the-nightly-build`.** Tested on 2026-08-16, one day
  after the project was created and with no audit submitted: three uploads
  asking for private, unlisted and public each kept the value they asked for,
  and the values still held after processing. Why is unknown, so the paragraph
  above stays as the documented behaviour and the thing to design against. It
  is not what happens here. See `docs/youtube-handover.md`.
- **YouTube takes pushed bytes.** Meta fetches the MP4 from a public URL, which
  is why `pipeline/gateway.upload_media` exists and why the gateway hosts
  anything at all. YouTube is a resumable upload: three HTTP calls, file goes up
  as raw bytes, no public URL, no hosting. Every docstring in this repo that
  says "the video is fetched, never pushed" is describing Meta specifically.

## The account layout

This matters more than the API steps and is much harder to change later.

**Use a dedicated Google account, not your primary one.** The reasoning is blast
radius rather than anonymity. Viewers never see the account behind a channel, so
a separate account buys no privacy. What it buys is that YouTube enforcement
against an automated channel cannot reach the Google account holding your mail,
your 2FA anchor and the recovery address for everything else. Losing the channel
means starting over. Losing your primary account does not have an equivalent.

**Prefer an old account you already own over a new one.** A fresh Google account
that immediately begins uploading over the API is the exact pattern anti-abuse
systems exist to catch. An aged account is not, and you can prove ownership of
it to support if you ever have to.

**Do not add your primary account as a channel owner.** It is tempting for
convenient access, and it relinks the two accounts, which is the one thing the
separation was for. Accept the second login. The gateway does the posting, so
you are not in there daily anyway.

**Make it a Brand Account channel, not the account's own channel.** Converting
later is possible but is the step Google's docs warn can delete the wrong
channel. A Brand Account can also be moved and can hold several owners, which
keeps options open at no cost now.

**Put the GCP project on the same dedicated account.** Then the project, the
OAuth grant and the channel share one fate, and it is not your fate. The
reviewers also ask which channel the client uploads to, and one account is a
simpler story than two.

Before relying on it: current recovery phone and email, 2FA on a device you
actually hold, and the backup codes stored with the rest of the private half.
`scripts/backup-secrets.sh` covers that, driven by the `# backup:start` block in
`.gitignore`. A dedicated account is infrastructure. The word "burner" invites
exactly the sloppiness that loses accounts.

## What you need

1. **A YouTube channel**, created as a Brand Account on the dedicated Google
   account. Creating several in a row triggers phone verification, which is
   routine.

2. **A Google Cloud project** with the **YouTube Data API v3** enabled. No
   billing account required. Add the **YouTube Analytics API** in the same trip
   if you want the feedback loop; see below.

3. **An OAuth consent screen**, user type External, publishing status
   **Production**. Not Testing. This is the single most common way to lose a
   weekend: see below.

4. **An OAuth client**, type **Desktop app**. The installed-app flow gives you a
   refresh token from a one-time browser authorisation, which is what an
   unattended service needs. Web application type wants a redirect URI and buys
   nothing here.

5. **Scopes.** `https://www.googleapis.com/auth/youtube.upload` to publish. Add
   `https://www.googleapis.com/auth/yt-analytics.readonly` if the scriptwriter
   is going to be shown how YouTube posts performed. Ask for both in the same
   authorisation; adding one later means going back through the browser.

6. **A refresh token.** Authorise once in a browser, exchange the code, and
   store the refresh token in `data/yt_token.json` alongside
   `data/ig_token.json`. Google refresh tokens do not expire on a clock the way
   Meta's 60 day tokens do, so there is no equivalent of the `--refresh-token`
   margin job. Access tokens last an hour and are minted on demand.

7. **The audit submitted**, via the [YouTube API Services Audit and Quota
   Extension Form](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits).
   It reviews the API project, not a video, so it can sit in the queue while
   everything else gets built.

## Five things that will cost you an hour each

1. **Testing mode expires refresh tokens after exactly 7 days.** An OAuth
   consent screen left in Testing hands out refresh tokens that die a week
   later, which for an unattended publisher means it works, then silently stops
   every Monday. Set publishing status to Production. `youtube.upload` is a
   sensitive scope, so Production without full verification shows an unverified
   app warning at the one authorisation and caps you at 100 users. For a channel
   you own, both are fine. The 7 day expiry is not.

2. **The private lock lives on the project, not the video.** So no client
   library routes around it, deleting and re-uploading does not help, and Studio
   will not let you flip it. Audited project or private videos, no third option.

3. **The audit and OAuth verification are two different processes.** Completing
   one does not complete the other, and it is easy to finish the consent screen,
   see green ticks, and assume the private lock is dealt with. It is not.

4. **The unverified app warning is expected, not a failure.** At the one
   authorisation you will get an interstitial. Advanced, then proceed. People
   stop here assuming something is misconfigured.

5. **`containsSyntheticMedia` is a decision, not a default.** The status object
   has a field for disclosing altered or synthetic content. This account's voice
   is a clone of a real person reading a script that person commissioned, which
   is a judgment call rather than an obvious yes or no. Decide it deliberately
   and write down why, because leaving it unset is also an answer.

## Quota is not a constraint

`videos.insert` has its own allocation of 100 calls a day, separate from the
10,000 unit pool shared by everything else. Three posts a day is nowhere near
it. If you ever read that an upload costs 1600 units, that is the old model.

## What Shorts requires

Nothing that this pipeline is not already producing. Vertical, at most 3
minutes; `config.py` renders 1080x1920 and the scripts run 30 to 45 seconds.
Shorts is detected from aspect ratio and duration, so no `#Shorts` tag is
needed and adding one reads as a tell.

Title is capped at 100 characters and description at 5000. The hook is already
capped at 60 characters by `max_hook_chars`, so it fits a title as written.

## Two things do not port from the Instagram side

- **The keyword mechanic has no equivalent.** No DMs, no private replies. A
  YouTube description saying "comment DS4 if you want the link" is a promise
  nothing can keep, so the link goes in the description directly and the call to
  action is stripped. `strip_written_cta` in `pipeline/gateway.py` is the seam.
  The wording is copy, so it belongs in `PROFILE.md` first.
- **`skip_rate` does not exist there.** The feedback loop in `pipeline/results.py`
  ranks hooks by the share who scrolled inside three seconds, which scores the
  opening alone. The nearest YouTube metric is `averageViewPercentage`, which
  scores the whole video. Feeding both into `_results_block` without
  distinguishing them would corrupt the one signal the scriptwriter is tuned on.

## On staying published

The risk worth managing is not the API. Compliant use of the official API is
close to zero risk, and the worst it does is the private lock. The real exposure
is the [inauthentic content policy](https://support.google.com/youtube/answer/1311392),
renamed from "repetitious content" in July 2025 specifically to cover
mass-produced AI video.

What this pipeline does is on the right side of that line in the ways the policy
names: the script is researched and written per repo rather than the README read
aloud, the voice is a clone rather than stock text to speech, and the visuals
are a custom render rather than a stock slideshow.

The exposure is the pattern, not the content. Reviewers assess channel themes,
metadata and overall content patterns rather than single videos, and a daily
video of an identical layout about trending GitHub repos is the shape of the
thing they are hunting even when each video is original.

**The cadence mitigation this section used to recommend is no longer in place.**
It said to run YouTube below Instagram's cadence to start, since slots are per
account in the gateway, and that made sense while Instagram posted three a day.
Since 2026-08-27 all three destinations fire one post a day in the same 08:10
Europe/Oslo slot, so YouTube already runs at exactly Instagram's cadence and
there is no lower one to drop to. The other mitigation still stands and is still
configuration rather than code: let the scene mix vary visibly across a week.

## Sources

- [Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Quota and compliance audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- [Determine quota cost](https://developers.google.com/youtube/v3/determine_quota_cost)
- [Using OAuth 2.0 to access Google APIs](https://developers.google.com/identity/protocols/oauth2)
- [YouTube channel monetization policies](https://support.google.com/youtube/answer/1311392)
