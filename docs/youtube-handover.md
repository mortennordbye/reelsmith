# YouTube: what is done and what is left

Written 2026-08-15, at the point where publishing works end to end and the
remaining work is decisions rather than code. `docs/youtube-api-setup.md` is the
API background, `docs/youtube-publishing-plan.md` is the design and why it is
shaped that way, and this is the list.

## What is live

The gateway runs `0.0.29` on schema v11. The channel `@thenightlybuild`
(`UCH8RDOkbzDna2mDAlq4GaFw`) is registered, and one slot fires at 21:05
Europe/Oslo against Instagram's three. `--enqueue` puts one render into both
queues from a single upload.

One video is on the channel, uploaded from the Mac as a proof before the
scheduler path existed. **The gateway itself has not published to YouTube yet**,
because nothing has been enqueued since it went live. The first one is the real
test of the path, and it is worth watching the log for rather than assuming.

## The audit, which turned out not to block anything

**The private lock does not apply to this project.** Measured on 2026-08-16
rather than assumed: three uploads asking for `private`, `unlisted` and
`public` each kept the value they asked for, with no rejection reason and the
values still holding after processing.

```
Zm9_d8rJBGU  public    reject=none
asYaC8xnhbE  unlisted  reject=none
8mruyznHbMs  private   reject=none
```

This contradicts `docs/youtube-api-setup.md` and the published behaviour, which
say uploads from an API project created after 28 July 2020 and not yet audited
are locked to private permanently. That doc's warning shaped this whole design,
and the reason it does not hold here is unknown. Do not assume it will keep not
holding: `gateway/scheduler.py` logs whenever a returned privacy status differs
from the one requested, which is what the restriction would look like if it
starts.

So the audit is worth submitting for quota headroom and compliance standing,
and it gates neither. Draft answers are in the private `PROFILE.md` under "The
audit, and what to answer"; the form is linked from
`docs/youtube-api-setup.md`. One thing to say explicitly on it: you are **not**
requesting a quota increase. The form's only sensible entry point is the
"request additional quota" option, and one upload a day against an allowance of
100 otherwise reads as confusion.

Going public is `GATEWAY_YOUTUBE_PRIVACY_STATUS=public` in the Homelab
ConfigMap, whenever you want it.

## Decisions left

**The cadence mismatch is the one with a deadline.** Three renders go in a day
and one comes out on YouTube, so that queue grows by two a day and never
drains. It will not break anything soon: `db.live_media_names` exempts every
queued row from the media sweep regardless of account, so the videos are kept
rather than pruned out from under it, and the PVC requests 1Gi but sits on an
NFS mount with 446G free that does not enforce the request. What it does mean is
a backlog that eventually holds months, and an "upcoming" column on the queue
page that projects further out than it is worth reading. Three ways out: raise
the YouTube cadence, send only some renders there, or let it run deep on
purpose. The first is one line in the ConfigMap.

**`containsSyntheticMedia` is declared false**, in `gateway/config.py`. The
setup doc calls it a judgment call rather than an obvious no: the voice is a
clone of a real person reading a script that person commissioned. Currently
false because a value had to be sent, not because the question was answered.
It is a per-upload declaration, so changing it costs nothing and applies from
the next video.

**The channel is the Google account's own, not a Brand Account.** It works, and
it uploaded fine. What it gives up is that the channel cannot be moved to
another account and cannot hold a second owner. Converting is possible and is
the step Google's docs warn can delete the wrong channel, so it is much cheaper
now, with one private video on it, than at any later point.

## Known gaps

**No custom thumbnail.** The pipeline renders `cover.png` and Instagram uses it
as `cover_url`; the YouTube row is created with `cover_name=None` and YouTube
picks its own frame. `thumbnails.set` is one more call and works with the scope
already granted. Matters little for the Shorts shelf, which uses a frame, but it
shows on the channel page and in search.

**`tags` goes up empty.** The hashtags are in the description, which is where
YouTube reads the ones it displays above the title, so nothing is broken. The
separate `tags` field is unset because nothing was passed, which is an accident
rather than a decision.

**No analytics.** Deliberately out of scope, and the reason is worth keeping:
`skip_rate` scores the opening three seconds and is the signal the scriptwriter
is tuned on, while `averageViewPercentage` scores the whole video. Feeding both
into `_results_block` would corrupt the one measurement the loop depends on, so
the work is a separate column and a separate prompt block, not a shared one. The
`yt-analytics.readonly` scope is already granted, so it needs no browser trip.

**Shorts treatment is unverified.** The uploaded video is vertical and 32
seconds, which qualifies, but it plays at `/watch?v=` rather than `/shorts/`.
Private videos are not eligible for the Shorts experience, so this cannot be
confirmed until the audit clears and something goes out public.

## Debt

**`ig_user_id` is a lie on YouTube rows.** It is the opaque account key now and
holds a channel id. Renaming it to `account_id` across `db.py`, `admin.py`, the
templates and the tests is mechanical and was deliberately not done in the same
change that added the feature. Nothing breaks if it never happens; it just reads
wrong.

**GHCR tags `0.0.1` to `0.0.12` are broken and stay broken.** Their manifests
were deleted by the prune job. `provenance: false` stops new ones being created
and the Warehouse's `discoveryLimit: 10` keeps discovery above them, so this is
worked around rather than fixed. Deleting the dangling tags needs a token with
`delete:packages`. Left alone, the window slides further away every build.

**Two paths have never run.** `scripts/youtube_authorise.py` with no flags,
which is the browser-then-register path, was never exercised: the channel was
registered by posting the credentials from `.env` directly, because a valid
refresh token already existed and a second consent round trip would have proved
nothing. And there is no mode that re-registers from `.env` without a browser,
which is what a rebuilt gateway database would want. That is a ten line flag if
it ever bites.

## Two things to know before touching the deploy

**Order matters between the ConfigMap and the image.** `parse_slots` fails the
boot on a line it cannot read, which is correct and is why `account=` has to
land in an image before it lands in `GATEWAY_SLOTS`. Doing it the other way
crashlooped the pod on 2026-08-15.

**A green build is not a deploy.** Kargo's Warehouse fails discovery entirely on
the first unreadable tag, and when it does, nothing promotes while every build
and every check still reports success. That silently swallowed three merged PRs
between `0.0.22` and `0.0.29`. If a change does not appear in the cluster, check
the Warehouse conditions before anything else:

```bash
kubectl get warehouse reelsmith -n reelsmith-cd \
  -o jsonpath='{range .status.conditions[*]}{.type}: {.status} {.reason}{"\n"}{end}'
```
