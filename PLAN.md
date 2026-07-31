# Plan: reelsmith

Written 2026-07-31. The repo grows from a render pipeline into a system with
three parts: automated posting (built), a self-hosted comment-to-DM gateway
(new), and a deployment of that gateway on the homelab cluster (new). The repo
has a private GitHub remote named **reelsmith** and the container images
follow (`ghcr.io/mortennordbye/reelsmith-gateway`).

This document is the contract for the sessions that build it. Phase 0 is a
migration with no code in it; do it first and completely, because half the
facts in it were verified against the live machine and will go stale.

## State as of 2026-07-31

Done this session, so do not redo it:

- Last session's automated-posting work is committed (`19949a6`).
- `mortennordbye/reelsmith` exists on GitHub, **private** (it was briefly
  created public and flipped before anything was pushed), and `main` is
  pushed from the old working tree.
- Repo and image naming decided: repo `reelsmith`, gateway image
  `ghcr.io/mortennordbye/reelsmith-gateway`.
- Cover hosting decided: the gateway serves covers (homelab cluster).
- `HANDOVER.md` deleted; its open items are resolved.

Not done, and known true on this machine as of today:

- **The working tree of record is still `~/Documents/local-git/tech-ig`.**
  The clone at `~/Documents/github/reelsmith` was made before the first push:
  it has zero commits and its `origin/main` shows as gone. A fetch fixes it.
- **No pipeline launchd jobs are installed.** `launchctl list` shows only an
  unrelated `com.nordbye.jarvis-bridge`. The snapshot job that INSTAGRAM.md
  assumes is running is not; nothing to uninstall, but also no token refresh
  and no star history accumulating until it is installed.
- The one-time Meta dashboard setup (INSTAGRAM.md §3 plus B4 below) is not
  done. `.env` has no `IG_USER_ID`.

## Phase 0: migrate to ~/Documents/github/reelsmith

Everything tracked arrives by git; the migration is the gitignored residue
plus the jobs. Old tree stays untouched until the checklist at the end
passes.

1. **Bring the clone up to date.**

   ```bash
   cd ~/Documents/github/reelsmith
   git fetch origin
   git checkout -B main origin/main
   ```

2. **Copy the private assets across.** All gitignored, so the pull brings
   none of them:

   ```bash
   OLD=~/Documents/local-git/tech-ig
   rsync -a "$OLD/.env" .
   rsync -a "$OLD/data/" data/                    # token store, star history, cooldowns
   rsync -a "$OLD/models/" models/                # Kokoro weights, or re-download
   rsync -a "$OLD/tools/chatterbox/ref/" tools/chatterbox/ref/   # THE VOICE. Exists nowhere else.
   rsync -a "$OLD/build/" build/                  # optional, past run artifacts
   ```

   The voice recording is the one irreplaceable file on this list. Copy it,
   do not move it, and do not let any step of this end up tracked by git
   (`.gitignore` already excludes `tools/chatterbox/ref/*` deliberately;
   the recordings are biometric).

3. **Recreate the environments, do not copy them.** Venvs bake absolute
   paths and are not relocatable:

   ```bash
   uv venv --python 3.13 && uv pip install -r requirements.txt
   uv pip install -e ".[dev]"
   .venv/bin/playwright install chromium
   cd video && npm install && cd ..            # TypeScript already pinned 5.x
   # Chatterbox venv: follow tools/chatterbox/README.md (its own venv, ~3 GB)
   ```

4. **Rename the launchd labels while nothing is installed.** The plists in
   `launchd/` are templates installed via `sed "s|__REPO__|$PWD|g"`, so they
   are path-agnostic already. Since no job is installed yet, this is the free
   moment to rename `it.nordbye.tech-ig.*` to `it.nordbye.reelsmith.*`
   (filenames, `Label` keys, and the install commands in their headers), then
   install both from the new checkout per those headers. The snapshot job is
   the load-bearing one (token refresh rides it); install it even if the
   daily render job waits.

5. **Verify before retiring the old tree.** From the new checkout:
   - `pytest` (expect 118 passed) and `ruff check` clean
   - `python main.py --preview-voice` proves the Chatterbox venv and the
     copied reference recording work from the new path
   - `python main.py --candidates` proves `.env` and the GitHub token came
     across
   - `launchctl list | grep reelsmith` shows the snapshot job

   Then rename `~/Documents/local-git/tech-ig` to `tech-ig.retired` (keep it
   for a while, do not delete). Two side facts for whoever does this: Claude
   Code's per-project memory and settings are keyed to the directory path,
   so the new location starts fresh; and the `.gitignore` was reviewed this
   session and needs no changes for the migration. When Phase 1 starts, add
   the gateway's local dev database (e.g. `gateway/dev.sqlite3`) to it, and
   nothing else.

Every Meta API fact below was verified against Meta's developer docs on
2026-07-31 (Graph API v25.0). Where a claim comes from community reports
rather than Meta's own text, it is flagged.

---

## The shape of the system

```
 Mac (laptop, stays local)                 Homelab cluster (hyper-cluster)
 ─────────────────────────                 ──────────────────────────────
 pipeline: scrape → script →               gateway (FastAPI, one container)
 voice → captions → render                   • Meta webhook receiver (DMs)
      │                                      • comment poller (keyword watch)
      │  publish_reel()                      • private reply + follow gate
      ├────────────► Instagram ◄────────────┘      + link sender
      │                                      • cover file host (public URL)
      └── register post ────────────────►    • SQLite state on a PVC
          (media_id, keyword, link,
           cover.png upload)
```

The voice, the reference recording, the Chatterbox venv, and rendering stay on
the Mac. Only the gateway runs in the cluster, and it holds nothing that could
reproduce the voice. Git already tracks no voice artifacts (only the passage
text in `tools/chatterbox/ref/RECORD-THIS.txt`), so a **private** GitHub
remote leaks nothing; open-sourcing later is a separate decision per part.

---

## A. Automated posting (mostly built, two gaps)

`--post` and `--publish` work. The daily launchd job renders at 07:00 and
deliberately waits for a human `--publish`. Two things remain:

1. **The one-time Meta setup** (section 3 of `INSTAGRAM.md`), which only you
   can click through. This plan extends it: while you are in the dashboard,
   also add the messaging scopes and flip the app to Live (details in B4).
2. **Cover hosting**, decided this session: the gateway serves it. The
   publisher uploads `cover.png` to the gateway's authenticated upload route,
   gets back a public URL, and passes it as `cover_url`. Meta fetches it once
   at container creation, so the route is a trivial static file host. Until
   the gateway exists, the `thumb_offset` fallback keeps working.

Full automation (render *and* post unattended) is one line in the launchd
plist. Recommendation: keep the human veto until the first ten API publishes
have gone cleanly, then flip `--publish` to `--post` in the plist.

## B. The DM gateway

The growth mechanic: the Reel says "comment SEND and I'll DM you the link".
A commenter gets a DM. The DM asks them to follow the account. Once they
follow and reply, the link arrives automatically.

### B1. What the official API allows (the design constraints)

This is built on Meta's official **Instagram API with Instagram Login**. No
Facebook Page needed, no ManyChat subscription, no unofficial client. The
unofficial route (instagrapi et al.) automates the private mobile API against
Instagram's Terms of Use, and DM automation is among its most aggressively
policed behaviours; for an account posting daily it is an existential risk
for zero benefit. Not considered further.

Facts that shape the design, all at **Standard Access** (no App Review), app
set to **Live**, account holding a role on the app:

| Capability | Status without App Review |
|---|---|
| DM (`messages`) webhooks | Works for accounts with a role on the app. Flagged: Meta's page does not spell the tester restriction out; corroborated by community reports. |
| Send DMs, private replies | Works |
| Follower check | Works (`GET /<IGSID>?fields=is_user_follow_business`) |
| Read comments by polling | Works |
| Real-time `comments` webhooks | **Requires Advanced Access** (App Review + Business Verification). The one thing review gates. |

So the no-review design **polls comments** and uses **webhooks for DMs**.
Polling a recent post's comments every 60 seconds is well inside limits
(private replies alone allow 750/hour/account) and the reply-to-comment
window is 7 days, so even an hour of downtime loses nothing.

App Review is the growth path, not the start: one review of the app (plus
Business Verification) later unlocks comment webhooks and onboarding accounts
you do not own, and it is per-app, once.

### B2. The conversation flow

```
comment "SEND" on the Reel
  └─► private reply (POST /<IG_ID>/messages, recipient.comment_id)
      "That link is yours. Follow the account and reply here, I send it the
       moment you have."          [one shot: Meta allows exactly ONE private
                                   reply per comment, within 7 days]
      └─► user replies anything          [this opens the 24h messaging window
                                          and grants profile-read consent]
          └─► GET /<IGSID>?fields=is_user_follow_business
              ├─ true  ─► send the link. Done. Mark converted.
              └─ false ─► "Not seeing the follow yet. Reply once you have."
                          re-check on every inbound message (there is no
                          "user followed" webhook, re-checking is the only way)
```

Three API rules the code must treat as invariants:

- **One private reply per comment, ever.** The state machine records the
  comment_id before sending; a crash after send must not retry.
- **Profile (follower) data is consent-gated.** It only becomes readable
  after the user has messaged the account. Checking before the reply arrives
  returns an error, which is why the gate happens at message time, not
  comment time.
- **24-hour window.** After each user message the account may reply for 24h.
  No messages outside it (the `human_agent` tag is for actual humans and
  using it for automation is a policy violation). If the window lapses, the
  user gets the link the next time they message, whenever that is.

Policy notes folded into the copy: Meta requires automated experiences to
disclose automation where law requires, and expects bots to respond within
30 seconds. The gateway replies instantly, and the message copy should not
pretend to be a human typing. Follow-gating itself has no explicit policy
text either way; ManyChat ships it as a headline feature, so it is the
industry-standard tolerated pattern. All outgoing copy obeys the repo's text
rules in `CLAUDE.md` (no em dashes, no hype words), both because it is
viewer-facing and because DM copy that reads like a bot template kills the
conversion.

### B3. The service

`gateway/` package inside this repo, plus its own `Dockerfile`. FastAPI +
httpx + SQLite (aiosqlite), all already in the ecosystem this repo uses.
Deliberately boring, no queue, no Redis: at this scale a poller task and a
webhook handler in one process is the honest architecture.

Routes:

| Route | Auth | Purpose |
|---|---|---|
| `GET /webhook` | Meta verify token | Subscription handshake (`hub.challenge`) |
| `POST /webhook` | `X-Hub-Signature-256` verified against app secret | Inbound DM events |
| `POST /api/posts` | Bearer token | Pipeline registers a post: media_id, keyword, link, account |
| `POST /api/covers` | Bearer token | Pipeline uploads cover.png, returns public URL |
| `GET /covers/<name>.png` | none | The URL Meta fetches |
| `GET /healthz` | none | Liveness |
| `GET /metrics` | cluster-internal | Prometheus (comment polls, replies sent, links sent, follow-gate conversion) |

Background tasks in the same process:

- **Comment poller.** For each account, every 60s, `GET /<media_id>/comments`
  on posts registered in the last 7 days. New comment matching the post's
  keyword (case-insensitive, default `send`) → private reply → record.
- **Token refresher.** Daily, refreshes any account token inside a 15-day
  margin, same policy as the pipeline's. The gateway authorizes its own
  token per account (messaging + comments scopes) so laptop and cluster
  never fight over one token.

State, one SQLite file on a PVC (Postgres per the homelab house pattern is
the migration path if this ever needs more than one replica):

- `accounts` (ig_user_id, username, token, token_expiry, active)
- `posts` (media_id, account, keyword, link, registered_at)
- `comments_handled` (comment_id PK, media_id, igsid?, replied_at)
- `conversations` (igsid, account, state: replied/awaiting_follow/converted,
  last_inbound_at, link_sent_at)

### B4. Meta dashboard work (you, ~15 min on top of the publishing setup)

1. Same Meta app as publishing. Add scopes `instagram_business_manage_messages`
   and `instagram_business_manage_comments` next to the existing two.
2. Configure the webhook product: callback `https://<gateway-host>/webhook`,
   verify token from the gateway's secret, subscribe to the `messages` field.
3. Switch the app to **Live** (webhooks are only delivered to Live apps; Live
   does not require review).
4. Re-run the OAuth flow with the full scope set; the gateway stores the
   long-lived token via `POST /api/accounts` (or a small CLI helper).
5. `POST /me/subscribed_apps?subscribed_fields=messages` with the account
   token (the gateway does this itself on account registration).

## C. Deployment on the homelab

Follows the existing house pattern exactly (`k8s/talos/apps/logeverylift/`
as the template):

- **Image**: `ghcr.io/mortennordbye/reelsmith-gateway`, built by a GitHub
  Actions workflow in this repo on push to `main` (paths-filtered to
  `gateway/`), pinned by SHA in the manifest. This is why the repo needs the
  GitHub remote; nothing else does.
- **Manifests** in `Homelab/k8s/talos/apps/reelsmith/`: `namespace.yaml`,
  `app.yaml` (Deployment, 1 replica, PVC mount, reloader annotation),
  `httproute.yaml` (Traefik, host e.g. `gate.nordbye.it`; seen only by Meta's
  servers, so the domain is not a doxxing surface), `externalsecret.yaml`
  (app secret, verify token, API bearer token), `ciliumnetworkpolicy.yaml`
  (ingress from Traefik only; egress to graph.instagram.com), and
  `kustomization.yaml`. cert-manager and external-dns pick up the host
  automatically; ArgoCD deploys it.
- **Cluster dependency is one-way.** The pipeline works with the gateway
  down: covers fall back to `thumb_offset`, post registration retries and is
  skippable, and missed keyword comments are caught by the poller for 7 days.
  A dead cluster can never block the 07:00 post.

Local dev never touches the cluster: `docker compose` (or plain `uvicorn`)
plus a `cloudflared` quick tunnel for webhook delivery while iterating.

## D. Pipeline integration

Three small changes on the Mac side:

1. `_publish_run()` gains "upload cover, get URL" and "register post with
   gateway" steps, both best-effort like `render_covers` (log, never fail a
   publish). New settings: `gateway_url`, `gateway_token`.
2. The scriptwriter's caption template gains the CTA line (comment `SEND`
   for the link), subject to the same validator rules as everything else.
   The spoken script can carry it too, but captions convert and narration
   time is scarce, so caption-only first.
3. The bio link habit stays manual for now (the API cannot set the bio link).

## E. Multi-account

Designed in from day one, activated later:

- The gateway is multi-account already (accounts table, per-account tokens,
  webhook events routed by the recipient IG id; one Meta app serves any
  number of owned accounts at Standard Access, each doing its own OAuth).
- The pipeline stays single-account per invocation: `--account <name>`
  selects a profile section in config (IG ids, token file path, topic
  filters). One repo, one gateway, N accounts, N launchd jobs.
- `INSTAGRAM.md` section 4 already covers the account-hygiene side (per
  account alias email, no shared contact details). Accounts you do not own
  would need App Review; owned accounts do not.

---

## Phases

**Phase 0, migration and plumbing (no code)**
- [ ] The migration steps at the top of this document, in order.
- [ ] Meta dashboard: publishing setup (INSTAGRAM.md §3) + messaging scopes
      + webhook config + Live (B4). Human-only, the AI cannot click this.

**Phase 1, gateway core (code, testable offline)**
- [ ] `gateway/` FastAPI app: webhook verify + signature check, DM state
      machine, comment poller, private reply, follow gate, link send.
- [ ] SQLite store + the four tables, httpx.MockTransport test suite in the
      style of `tests/test_publisher.py`.
- [ ] Cover upload + static serving, post registration API.
- [ ] Local run: docker compose + cloudflared quick tunnel, end-to-end test
      against the real account with a throwaway post.

**Phase 2, cluster**
- [ ] Dockerfile + GH Actions build to ghcr (SHA tags).
- [ ] Manifests in Homelab repo, ESO secret, DNS, deploy via ArgoCD.
- [ ] Point the Meta webhook at the cluster URL, retire the tunnel.

**Phase 3, pipeline hookup**
- [ ] Publisher: cover upload + post registration (best effort).
- [ ] Caption CTA line.
- [ ] Flip launchd to `--post` once trust is earned.

**Phase 4, scale (when account #2 exists)**
- [ ] `--account` profiles in the pipeline.
- [ ] App Review + Business Verification if comment webhooks or non-owned
      accounts become worth it.

## Risks worth naming

- **The dev-mode webhook nuance is community-verified, not Meta-verified.**
  If `messages` webhooks turn out not to fire for the account pre-review,
  the fallback is polling conversations (2 calls/sec/account allowed), a
  contained change in one poller.
- **Follow-gating has no explicit policy blessing.** Industry practice at
  massive scale (ManyChat sells it), but a policy change would hit everyone
  doing it; the gateway degrades to "comment, get link" by config.
- **Conversion copy is the real variable.** The mechanic works exactly as
  well as the DM copy convinces; expect to iterate on it like the scripts.
