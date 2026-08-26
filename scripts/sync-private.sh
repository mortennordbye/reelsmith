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
# ## Only the authored half moves
#
# The private files split by who writes them, and the split is what makes this
# safe to run without thinking about it:
#
#   PROFILE.md, accounts/*/.env, accounts/*/ref/*   you, here, rarely
#   accounts/*/data/*.json                          the render host, nightly
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
# Requires: macOS (mount_smbfs, diskutil), git. Auth: mount_smbfs prompts for
# the password itself, so nothing is stored and nothing reaches argv.
#
set -euo pipefail

NAS_HOST="${NAS_HOST:-nas.local.bigd.no}"
NAS_SHARE="${NAS_SHARE:-shared-data}"
# Where the render host's /mnt/reelsmith actually points. Read off the pod's
# volumeMount subPath rather than guessed; change both together.
NAS_PRIVATE_ROOT="${NAS_PRIVATE_ROOT:-media/reelsmith}"
NAS_DIR="${NAS_DIR:-}"

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
)

log()  { printf '\033[0;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[skip]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") (--push | --pull) [--yes] | --list | --help

Move the authored half of the private files between this laptop and the share
the render host reads. Shows what would change and does nothing without --yes.

  --push       laptop  -> share   (after you have tuned something here)
  --pull       share   -> laptop  (a fresh clone, or to pick up an edit made there)
  --yes        actually copy; without it this is a dry run
  --list       what is in scope, and what is deliberately not
  --help       this

The cooldown store and the star history are never moved by this. See the top of
the script for why.
EOF
}

MODE=""
APPLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)     MODE=push ;;
    --pull)     MODE=pull ;;
    --yes)      APPLY=true ;;
    --list)     MODE=list ;;
    -h|--help)  usage; exit 0 ;;
    *)          usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
[[ -n "$MODE" ]] || { usage >&2; die "say --push, --pull or --list"; }

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

# --- The share -------------------------------------------------------------
MNT=""
MOUNTED=false
cleanup() {
  if $MOUNTED; then
    umount "$MNT" 2>/dev/null || diskutil unmount force "$MNT" >/dev/null 2>&1 || true
  fi
  [[ -n "$MNT" ]] && rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

if [[ -n "$NAS_DIR" ]]; then
  [[ -d "$NAS_DIR" ]] || die "NAS_DIR '$NAS_DIR' is not a directory (is the share mounted?)"
  BASE="$NAS_DIR"
else
  # macOS refuses to mount the same share twice, so reuse an existing mount and
  # do not unmount one we did not open.
  existing="$(/sbin/mount | awk -v sh="/$NAS_SHARE" '/\(smbfs/ && $1 ~ sh"$" {print $3; exit}')"
  if [[ -n "$existing" ]]; then
    log "//$NAS_HOST/$NAS_SHARE already mounted at $existing — reusing it"
    BASE="$existing"
  else
    NAS_USER=""
    while [[ -z "$NAS_USER" ]]; do read -r -p "NAS username: " NAS_USER </dev/tty; done
    MNT="$(mktemp -d)"
    log "Mounting //$NAS_USER@$NAS_HOST/$NAS_SHARE — enter the NAS password when asked"
    mount_smbfs "//${NAS_USER}@${NAS_HOST}/${NAS_SHARE}" "$MNT" \
      || die "SMB mount failed — check the username and password, that SMB is enabled, and that '$NAS_USER' can reach '$NAS_SHARE'"
    MOUNTED=true
    BASE="$MNT"
  fi
fi

SHARE="$BASE/$NAS_PRIVATE_ROOT"
[[ -d "$SHARE" ]] || die "no $NAS_PRIVATE_ROOT on the share — is NAS_PRIVATE_ROOT right?"

# --- What differs ----------------------------------------------------------
# Expanded against the source side, so --push picks up a file this laptop has
# and the share does not, and --pull picks up the reverse.
if [[ "$MODE" == push ]]; then FROM="$REPO_ROOT"; TO="$SHARE"; else FROM="$SHARE"; TO="$REPO_ROOT"; fi

sum() { [[ -f "$1" ]] && md5 -q "$1" 2>/dev/null || echo "-"; }

declare -a CHANGED=() SAME=()
for pattern in "${IN_SCOPE[@]}"; do
  # shellcheck disable=SC2086
  for src in $FROM/$pattern; do
    [[ -f "$src" ]] || continue
    rel="${src#"$FROM"/}"
    # A guard rather than a filter. These patterns are not in IN_SCOPE, so
    # reaching here means someone widened it without reading the header.
    case "$rel" in
      accounts/*/data/*|.env)
        warn "refusing $rel — see the header"; continue ;;
    esac
    if [[ "$(sum "$src")" == "$(sum "$TO/$rel")" ]]; then
      SAME+=("$rel")
    else
      CHANGED+=("$rel")
    fi
  done
done

log "$MODE: $FROM  →  $TO"
if [[ ${#SAME[@]} -gt 0 ]]; then
  printf '  \033[0;90m= %s\033[0m\n' "${SAME[@]}"
fi
if [[ ${#CHANGED[@]} -eq 0 ]]; then
  log "Nothing differs."
  exit 0
fi
for rel in "${CHANGED[@]}"; do
  if [[ -f "$TO/$rel" ]]; then
    printf '  \033[0;33m~\033[0m %s\n' "$rel"
  else
    printf '  \033[0;32m+\033[0m %s\n' "$rel"
  fi
done

if ! $APPLY; then
  log "${#CHANGED[@]} file(s) would change. Re-run with --yes to do it."
  exit 0
fi

# --- Copy, then read it back ----------------------------------------------
for rel in "${CHANGED[@]}"; do
  mkdir -p "$(dirname "$TO/$rel")"
  # A copy beside the old one first, so a half-written file over SMB never
  # replaces a good one. The voice recording is a re-record if it is lost.
  cp "$FROM/$rel" "$TO/$rel.tmp.$$"
  mv "$TO/$rel.tmp.$$" "$TO/$rel"
done

# Read every file back from the far side and compare. An SMB write that
# truncated is the failure this catches, and it is silent without it.
bad=0
for rel in "${CHANGED[@]}"; do
  [[ "$(sum "$FROM/$rel")" == "$(sum "$TO/$rel")" ]] || { warn "verify failed: $rel"; bad=1; }
done
[[ $bad -eq 0 ]] || die "at least one file did not land intact — nothing was deleted, re-run"

log "Copied and verified ${#CHANGED[@]} file(s)."
[[ "$MODE" == push ]] && log "The render host reads the share directly, so there is nothing to restart."
