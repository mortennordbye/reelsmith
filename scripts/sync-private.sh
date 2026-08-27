#!/usr/bin/env bash
#
# sync-private.sh — move the authored half of the private files between this
# laptop and the share the render host reads.
#
# The repo is public and the identity is not, so `PROFILE.md`, the per-account
# `.env` and the cloned voice are git-ignored. That left one way to get them
# onto the render host, which was to copy them by hand "once". On 2026-08-26
# the copy on the share turned out to be eighteen days old: it predated the
# whole YouTube section and still called account 2 a template, and the nightly
# had been writing copy against it the entire time.
#
# There was never a network gap. verksted mounts
# nas.local.bigd.no:/volume1/shared-data at /mnt/reelsmith with subPath
# media/reelsmith, and this laptop can mount the same share over SMB, which is
# what scripts/backup-secrets.sh already does. The two are the same bytes. All
# that was missing was a command that says so.
#
#   scripts/sync-private.sh --push            what would change, and nothing else
#   scripts/sync-private.sh --push --yes      do it
#   scripts/sync-private.sh --pull            the same, in the other direction
#   scripts/sync-private.sh --list            what is in scope, and what is not
#
# ## Two routes to the same bytes, and why there are two
#
# This mounted the NAS over SMB and nothing else until 2026-08-27, when the
# mount started failing with `server rejected the connection: Authentication
# error` and took the whole command down with it. The share was fine and the
# render host was reading it the entire time. A sync that only works while one
# password is remembered is a sync that stops working, quietly, on the day you
# need it, which is the same failure the eighteen-day-old copy already was.
#
# So the far side is reached one of three ways, and `--via` forces one:
#
#   pod   `kubectl exec` into the verksted pod, which already has the share
#         mounted at /mnt/reelsmith. No NAS password, and it works from
#         anywhere the cluster is reachable rather than only on the LAN.
#   dir   a path you mounted yourself, via NAS_DIR. Finder counts.
#   smb   mount_smbfs, the original route, still here because it needs no
#         cluster and is the only one that works if the pod is down.
#
# Left alone it takes the first of those that answers, and says which in the
# log. They are the same bytes either way: /mnt/reelsmith on the pod is this
# share at subPath media/reelsmith.
#
# ## Only the authored half moves
#
# The private files split by who writes them, and the split is what makes this
# safe to run without thinking about it:
#
#   PROFILE.md, accounts/*/.env, accounts/*/ref/*   you, here, rarely
#   accounts/*/data/*.json                          the render host, nightly
#
# ## The account .env is projected, not copied
#
# It is the one file in scope that is partly host specific, in exactly the way
# the root `.env` is refused for. This laptop's copy holds `IG_ACCESS_TOKEN`
# and the three `YOUTUBE_*` credentials because it can publish directly. The
# render host cannot and does not: it enqueues, the gateway publishes and holds
# its own credentials, and on 2026-08-27 that host's `data/` had no
# `ig_token.json` and had never needed one. Copying the file wholesale would
# put four live credentials on a machine that has no use for them.
#
# So `ENV_PROJECTED_KEYS` below is what crosses, and everything else stays
# here. **An allowlist rather than a list of secrets to strip**, because this
# is a security boundary and the only rule worth having on one is fail closed:
# a key nobody has thought about does not travel. The cost is that a genuinely
# new id has to be added to that list, and the cost of the other direction is
# a credential leaking the first time one is named unusually.
#
# The dropped keys are printed rather than dropped quietly, so a new id that
# nobody allowlisted shows up as a line you can read instead of as a render
# host that mysteriously skips a destination.
#
# **`--pull` refuses that file outright.** The far copy is derived and holds
# strictly less, so pulling it is always a loss: before this, a `--pull --yes`
# would have overwritten this laptop's `.env` with the render host's two line
# version and taken four credentials with it. `PROFILE.md` and the voice
# recording are plain copies in both directions, because both sides hold the
# same bytes for those.
#
# **`data/` is refused, not merely skipped.** It holds the cooldown store and
# the star history, and the render host's copies are the live ones: measured on
# 2026-08-26, its `used_repos.json` was 3573 bytes against this laptop's 2375,
# and its `star_history.json` 233 KB against 172 KB. Pushing from here would
# hand the nightly a cooldown list missing a month of repos, which reads as
# discovery suddenly rediscovering everything.
#
# Those two stores are not meant to be file-synced at all. `_sync_covered` in
# pipeline/scraper.py folds `GET /api/covered` in before every discovery run,
# so the gateway is what reconciles them, deliberately and already.
#
# The root `.env` is refused for a different reason: it is host specific.
# `CHATTERBOX_DEVICE` is `mps` here and `cpu` there, and the render host
# deliberately holds no Instagram token at all.
#
# The thinking documents — IDEAS.md, HANDOVER.md, SPINOFFS.md, PLAN.md,
# notes/ — are also out of scope, and that is a decision rather than an
# oversight. They are written on both sides, so a one-way push is the wrong
# shape for them and would quietly lose a handover written by a session that
# failed overnight. They are still backed up by backup-secrets.sh.
#
# Requires: git, and one working route to the share — kubectl for `pod`, or
# macOS mount_smbfs for `smb`, or a mount you made yourself for `dir`. Auth:
# nothing is stored and nothing reaches argv. `pod` uses your kubeconfig and
# mount_smbfs prompts for the password itself.
#
set -euo pipefail

NAS_HOST="${NAS_HOST:-nas.local.bigd.no}"
NAS_SHARE="${NAS_SHARE:-shared-data}"
# Where the render host's /mnt/reelsmith actually points. Read off the pod's
# volumeMount subPath rather than guessed; change both together.
NAS_PRIVATE_ROOT="${NAS_PRIVATE_ROOT:-media/reelsmith}"
NAS_DIR="${NAS_DIR:-}"

# The pod route. The default kubectl context on this laptop is a work cluster,
# so the context is named rather than assumed; getting that wrong is not an
# error, it is a sync against somebody else's namespace.
KUBE_CONTEXT="${KUBE_CONTEXT:-admin@genesis}"
POD_NAMESPACE="${POD_NAMESPACE:-verksted}"
POD_SELECTOR="${POD_SELECTOR:-app=verksted-app}"
POD_CONTAINER="${POD_CONTAINER:-verksted}"
# Already the private root, so NAS_PRIVATE_ROOT is not appended to it. The pod
# mounts the share with subPath media/reelsmith, which is that path.
POD_PRIVATE_ROOT="${POD_PRIVATE_ROOT:-/mnt/reelsmith}"

# What moves. Globs, expanded against whichever side is the source, so a second
# account is picked up without editing this list.
IN_SCOPE=(
  "PROFILE.md"
  "accounts/*/.env"
  "accounts/*/ref/*"
)

# What is refused, and the reason each one is refused, printed by --list so the
# rule is readable rather than inferred from an absence.
declare -a REFUSED=(
  "accounts/*/data/*|the render host writes these; the gateway reconciles them"
  ".env|host specific: CHATTERBOX_DEVICE, and no token on the render host"
  "IDEAS.md, HANDOVER.md, SPINOFFS.md, PLAN.md, notes/|written on both sides"
  "accounts/*/.env on --pull|the far copy is a projection and holds strictly less"
)

# The only keys that cross from an account .env. See the header: an allowlist,
# so a key nobody has considered stays here rather than travelling because it
# did not look like a secret. The render host layers this over the root .env,
# so anything absent is inherited rather than missing.
ENV_PROJECTED_KEYS=(
  IG_USER_ID
  YOUTUBE_CHANNEL_ID
  TIKTOK_OPEN_ID
)

log()  { printf '\033[0;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[skip]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") (--push | --pull) [--yes] [--via pod|smb|dir] | --list | --help

Move the authored half of the private files between this laptop and the share
the render host reads. Shows what would change and does nothing without --yes.

  --push       laptop  -> share   (after you have tuned something here)
  --pull       share   -> laptop  (a fresh clone, or to pick up an edit made there)
  --yes        actually copy; without it this is a dry run
  --via        how to reach the share; left out, the first one that answers
                 pod  kubectl exec into verksted, which has it mounted already
                 dir  a path you mounted yourself, set NAS_DIR
                 smb  mount_smbfs to $NAS_HOST, asks for a password
  --list       what is in scope, and what is deliberately not
  --help       this

The cooldown store and the star history are never moved by this. See the top of
the script for why.
EOF
}

MODE=""
APPLY=false
VIA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)     MODE=push ;;
    --pull)     MODE=pull ;;
    --yes)      APPLY=true ;;
    --via)      shift; VIA="${1:-}"; [[ -n "$VIA" ]] || die "--via needs pod, dir or smb" ;;
    --via=*)    VIA="${1#--via=}" ;;
    --list)     MODE=list ;;
    -h|--help)  usage; exit 0 ;;
    *)          usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
[[ -n "$MODE" ]] || { usage >&2; die "say --push, --pull or --list"; }
case "${VIA:-pod}" in pod|dir|smb) ;; *) die "--via takes pod, dir or smb, not '$VIA'" ;; esac

REPO_ROOT="$(git rev-parse --show-toplevel)" || die "not inside the git repo"
cd "$REPO_ROOT"

if [[ "$MODE" == list ]]; then
  log "Moves, expanded here:"
  for pattern in "${IN_SCOPE[@]}"; do
    # shellcheck disable=SC2086
    for f in $pattern; do
      [[ -e "$f" ]] && printf '  \033[0;32m•\033[0m %s\n' "$f" \
                    || printf '  \033[0;33m·\033[0m %s (none yet)\n' "$pattern"
      break
    done
  done
  echo
  log "Never moves:"
  for entry in "${REFUSED[@]}"; do
    printf '  \033[0;31m✗\033[0m %-46s %s\n' "${entry%%|*}" "${entry#*|}"
  done
  exit 0
fi

# --- Reaching the far side -------------------------------------------------
# Everything below this point talks to the share through far_sum, far_glob,
# far_read and far_write, so a new route is four functions and not a rewrite.

MNT=""
MOUNTED=false
# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
  if $MOUNTED; then
    umount "$MNT" 2>/dev/null || diskutil unmount force "$MNT" >/dev/null 2>&1 || true
  fi
  [[ -n "$MNT" ]] && rmdir "$MNT" 2>/dev/null || true
  [[ -n "${STAGE:-}" ]] && rm -rf "$STAGE" 2>/dev/null || true
  return 0
}
trap cleanup EXIT

POD=""
kube() { kubectl --context "$KUBE_CONTEXT" -n "$POD_NAMESPACE" --request-timeout=30s "$@"; }
# `sh -c`, so the remote side expands globs and redirects. -i only where stdin
# is actually being fed: without a pipe it waits for a terminal that is not
# coming.
kube_sh()  { kube exec "$POD" -c "$POD_CONTAINER" -- sh -c "$1"; }
kube_shi() { kube exec -i "$POD" -c "$POD_CONTAINER" -- sh -c "$1"; }

pod_answers() {
  command -v kubectl >/dev/null 2>&1 || return 1
  POD="$(kube get pods -l "$POD_SELECTOR" \
          -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' 2>/dev/null \
        | awk '{print $1}')"
  [[ -n "$POD" ]] || return 1
  kube_sh "[ -d '$POD_PRIVATE_ROOT' ]" >/dev/null 2>&1
}

mount_smb() {
  # macOS refuses to mount the same share twice, so reuse an existing mount and
  # do not unmount one we did not open.
  local existing
  existing="$(/sbin/mount | awk -v sh="/$NAS_SHARE" '/\(smbfs/ && $1 ~ sh"$" {print $3; exit}')"
  if [[ -n "$existing" ]]; then
    log "//$NAS_HOST/$NAS_SHARE already mounted at $existing — reusing it"
    FAR_ROOT="$existing/$NAS_PRIVATE_ROOT"
    return 0
  fi
  local user=""
  while [[ -z "$user" ]]; do read -r -p "NAS username: " user </dev/tty; done
  MNT="$(mktemp -d)"
  log "Mounting //$user@$NAS_HOST/$NAS_SHARE — enter the NAS password when asked"
  mount_smbfs "//${user}@${NAS_HOST}/${NAS_SHARE}" "$MNT" || die \
    "SMB mount failed — check the username and password, that SMB is enabled, and
       that '$user' can reach '$NAS_SHARE'. \`smbutil view //${user}@${NAS_HOST}\`
       tests the login on its own and says more. A Synology also auto-blocks an
       address after a few failures and then refuses the right password too.
       Or skip SMB entirely: --via pod goes through the render host instead."
  MOUNTED=true
  FAR_ROOT="$MNT/$NAS_PRIVATE_ROOT"
}

FAR_ROOT=""
TRANSPORT=""
if [[ -n "$VIA" ]]; then
  TRANSPORT="$VIA"
  case "$TRANSPORT" in
    pod) pod_answers || die "no running $POD_SELECTOR pod in $POD_NAMESPACE on context $KUBE_CONTEXT, or no $POD_PRIVATE_ROOT in it" ;;
    dir) [[ -n "$NAS_DIR" ]] || die "--via dir needs NAS_DIR set to a mounted share root" ;;
  esac
else
  # First one that answers. NAS_DIR first because setting it is an instruction,
  # then the pod because it needs no password, then SMB.
  if   [[ -n "$NAS_DIR" ]]; then TRANSPORT=dir
  elif pod_answers;        then TRANSPORT=pod
  else                          TRANSPORT=smb
  fi
fi

case "$TRANSPORT" in
  dir)
    [[ -d "$NAS_DIR" ]] || die "NAS_DIR '$NAS_DIR' is not a directory (is the share mounted?)"
    FAR_ROOT="$NAS_DIR/$NAS_PRIVATE_ROOT"
    log "Via the mount at $NAS_DIR"
    ;;
  pod)
    FAR_ROOT="$POD_PRIVATE_ROOT"
    log "Via $POD_NAMESPACE/$POD, which has the share at $POD_PRIVATE_ROOT"
    ;;
  smb)
    mount_smb
    ;;
esac

# macOS md5 and coreutils md5sum print the same hex, which is what lets the two
# sides be compared without caring which one ran.
#
# **Missing is fatal rather than "-".** This read `md5 -q` alone until
# 2026-08-27, so on a host without it every checksum came back "-", every file
# compared equal to every other, and the script reported "Nothing differs" and
# copied nothing, with a zero exit. That was survivable while SMB pinned it to
# a Mac. The pod route runs anywhere, and a sync whose failure mode is
# "cheerfully did nothing" is worse than one that will not start.
if command -v md5sum >/dev/null 2>&1; then
  sum_local() { if [[ -f "$1" ]]; then md5sum "$1" | cut -d' ' -f1; else echo "-"; fi; }
elif command -v md5 >/dev/null 2>&1; then
  sum_local() { if [[ -f "$1" ]]; then md5 -q "$1"; else echo "-"; fi; }
else
  die "neither md5sum nor md5 is on PATH, so the two sides cannot be compared"
fi

far_sum() {
  if [[ "$TRANSPORT" == pod ]]; then
    kube_sh "md5sum '$FAR_ROOT/$1' 2>/dev/null | cut -d' ' -f1" 2>/dev/null \
      | tr -d '\r' | grep . || echo "-"
  else
    sum_local "$FAR_ROOT/$1"
  fi
}

# Unquoted on purpose: the far side does the expanding, which is what makes
# --pull pick up an account this laptop has never seen.
far_glob() {
  if [[ "$TRANSPORT" == pod ]]; then
    kube_sh "cd '$FAR_ROOT' 2>/dev/null && ls -1d $1 2>/dev/null" 2>/dev/null | tr -d '\r'
  else
    # shellcheck disable=SC2086  # unquoted so this side expands the glob
    ( cd "$FAR_ROOT" 2>/dev/null && ls -1d $1 2>/dev/null ) || true
  fi
}

# base64 rather than `kubectl cp`, which shells out to tar and reports a
# partial copy as success often enough to be worth avoiding. Every write is
# verified by reading it back regardless.
B64D="-D"
base64 --decode </dev/null >/dev/null 2>&1 && B64D="--decode"

far_read() {  # rel dest
  if [[ "$TRANSPORT" == pod ]]; then
    kube_sh "base64 < '$FAR_ROOT/$1'" | base64 "$B64D" > "$2"
  else
    cp "$FAR_ROOT/$1" "$2"
  fi
}

# A copy beside the old one first, so a half-written file never replaces a good
# one. The voice recording is a re-record if it is lost.
far_write() {  # src rel
  local tmp="$FAR_ROOT/$2.tmp.$$"
  if [[ "$TRANSPORT" == pod ]]; then
    base64 < "$1" | kube_shi \
      "mkdir -p \"\$(dirname '$FAR_ROOT/$2')\" && base64 -d > '$tmp' && mv '$tmp' '$FAR_ROOT/$2'"
  else
    mkdir -p "$(dirname "$FAR_ROOT/$2")"
    cp "$1" "$tmp"
    mv "$tmp" "$FAR_ROOT/$2"
  fi
}

if [[ "$TRANSPORT" != pod ]]; then
  [[ -d "$FAR_ROOT" ]] || die "no $NAS_PRIVATE_ROOT on the share — is NAS_PRIVATE_ROOT right?"
fi

# --- The account .env projection -------------------------------------------
is_account_env() { [[ "$1" == accounts/*/.env ]]; }

# Every assignment's key, ignoring comments and blank lines. Used on both sides:
# on this one to say what is being left behind, on the far one to say what an
# overwrite would remove.
env_keys() { sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$1" 2>/dev/null || true; }

is_projected() {
  local k
  for k in "${ENV_PROJECTED_KEYS[@]}"; do [[ "$k" == "$1" ]] && return 0; done
  return 1
}

# Deterministic, so the same input always gives the same bytes and the md5
# comparison that drives the whole script keeps working unchanged.
project_env() {  # src dst
  local key val
  {
    echo "# Generated by scripts/sync-private.sh — do not edit here."
    echo "#"
    echo "# The authored copy is accounts/<name>/.env on the laptop. Only the keys"
    echo "# this host needs are projected; the credentials in that file stay there,"
    echo "# because the gateway is what publishes. This is layered over the root"
    echo "# .env, so anything absent here is inherited rather than missing."
    for key in "${ENV_PROJECTED_KEYS[@]}"; do
      val="$(sed -n "s/^[[:space:]]*${key}=//p" "$1" | tail -1)"
      if [[ -n "$val" ]]; then printf '%s=%s\n' "$key" "$val"; fi
    done
  } > "$2"
}

STAGE=""
# What should land on the far side, which is the file itself for everything
# except an account .env.
source_for() {  # rel
  if is_account_env "$1"; then
    [[ -n "$STAGE" ]] || STAGE="$(mktemp -d)"
    local staged="$STAGE/${1//\//_}"
    project_env "$REPO_ROOT/$1" "$staged"
    printf '%s' "$staged"
  else
    printf '%s' "$REPO_ROOT/$1"
  fi
}

far_env_keys() {  # rel
  if [[ "$TRANSPORT" == pod ]]; then
    kube_sh "sed -n 's/^[[:space:]]*\\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p' '$FAR_ROOT/$1' 2>/dev/null" \
      2>/dev/null | tr -d '\r' || true
  else
    env_keys "$FAR_ROOT/$1"
  fi
}

# --- What differs ----------------------------------------------------------
# Listed from the source side, so --push picks up a file this laptop has and
# the share does not, and --pull picks up the reverse.
declare -a RELS=()
for pattern in "${IN_SCOPE[@]}"; do
  if [[ "$MODE" == push ]]; then
    # shellcheck disable=SC2086
    for src in $REPO_ROOT/$pattern; do
      [[ -f "$src" ]] && RELS+=("${src#"$REPO_ROOT"/}")
    done
  else
    while IFS= read -r rel; do
      [[ -n "$rel" ]] && RELS+=("$rel")
    done < <(far_glob "$pattern")
  fi
done

declare -a CHANGED=() SAME=() ADDED=() NOTES=()
for rel in "${RELS[@]}"; do
  # A guard rather than a filter. These patterns are not in IN_SCOPE, so
  # reaching here means someone widened it without reading the header.
  case "$rel" in
    accounts/*/data/*|.env)
      warn "refusing $rel — see the header"; continue ;;
  esac
  # The far copy is a projection and holds strictly less, so pulling it over
  # the authored one is always a loss and never what was meant.
  if [[ "$MODE" == pull ]] && is_account_env "$rel"; then
    warn "refusing to pull $rel — the share's copy is a projection, not the original"
    continue
  fi
  if [[ "$MODE" == push ]]; then
    here="$(sum_local "$(source_for "$rel")")"
  else
    here="$(sum_local "$REPO_ROOT/$rel")"
  fi
  there="$(far_sum "$rel")"
  if [[ "$here" == "$there" ]]; then
    SAME+=("$rel")
    continue
  fi
  CHANGED+=("$rel")
  if [[ "$MODE" == push ]]; then
    [[ "$there" == "-" ]] && ADDED+=("$rel")
  else
    [[ "$here" == "-" ]] && ADDED+=("$rel")
  fi
done

# Said out loud rather than left to the diff, because both of these are silent
# by nature: a key that stays behind looks like a key that synced, and a key an
# overwrite removes leaves no trace on the far side at all.
if [[ "$MODE" == push ]]; then
  for rel in "${RELS[@]}"; do
    is_account_env "$rel" || continue
    held=""
    while IFS= read -r key; do
      is_projected "$key" || held+="${held:+, }$key"
    done < <(env_keys "$REPO_ROOT/$rel")
    [[ -n "$held" ]] && NOTES+=("$rel: staying here — $held")
    lost=""
    while IFS= read -r key; do
      [[ -z "$key" ]] && continue
      is_projected "$key" || lost+="${lost:+, }$key"
    done < <(far_env_keys "$rel")
    [[ -n "$lost" ]] && NOTES+=("$rel: the share has keys this would remove — $lost")
  done
fi

if [[ "$MODE" == push ]]; then
  log "push: $REPO_ROOT  →  $FAR_ROOT ($TRANSPORT)"
else
  log "pull: $FAR_ROOT ($TRANSPORT)  →  $REPO_ROOT"
fi
if [[ ${#SAME[@]} -gt 0 ]]; then
  printf '  \033[0;90m= %s\033[0m\n' "${SAME[@]}"
fi
if [[ ${#CHANGED[@]} -eq 0 ]]; then
  log "Nothing differs."
  exit 0
fi
for rel in "${CHANGED[@]}"; do
  new=false
  if [[ ${#ADDED[@]} -gt 0 ]]; then
    for a in "${ADDED[@]}"; do [[ "$a" == "$rel" ]] && new=true; done
  fi
  if $new; then
    printf '  \033[0;32m+\033[0m %s\n' "$rel"
  else
    printf '  \033[0;33m~\033[0m %s\n' "$rel"
  fi
done

if [[ ${#NOTES[@]} -gt 0 ]]; then
  printf '  \033[0;90m· %s\033[0m\n' "${NOTES[@]}"
fi

if ! $APPLY; then
  log "${#CHANGED[@]} file(s) would change. Re-run with --yes to do it."
  exit 0
fi

# --- Copy, then read it back ----------------------------------------------
for rel in "${CHANGED[@]}"; do
  if [[ "$MODE" == push ]]; then
    far_write "$(source_for "$rel")" "$rel"
  else
    mkdir -p "$(dirname "$REPO_ROOT/$rel")"
    far_read "$rel" "$REPO_ROOT/$rel.tmp.$$"
    mv "$REPO_ROOT/$rel.tmp.$$" "$REPO_ROOT/$rel"
  fi
done

# Read every file back from the far side and compare. A write that truncated is
# the failure this catches, and it is silent without it. One loop for both
# directions, because "the two sides agree" is the same question either way.
bad=0
for rel in "${CHANGED[@]}"; do
  if [[ "$MODE" == push ]]; then
    want="$(sum_local "$(source_for "$rel")")"
  else
    want="$(sum_local "$REPO_ROOT/$rel")"
  fi
  [[ "$want" == "$(far_sum "$rel")" ]] || { warn "verify failed: $rel"; bad=1; }
done
[[ $bad -eq 0 ]] || die "at least one file did not land intact — nothing was deleted, re-run"

log "Copied and verified ${#CHANGED[@]} file(s)."
[[ "$MODE" == push ]] && log "The render host reads the share directly, so there is nothing to restart."
exit 0
