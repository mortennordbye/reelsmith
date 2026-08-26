#!/usr/bin/env bash
#
# pod-setup.sh — build everything the pipeline needs on a Linux host that only
# has the repo, so a checkout can render without a Mac.
#
# Written for the verksted pod, where `uv` is on PATH and the private half of
# the repo arrives on an NFS mount rather than in git. It is idempotent: every
# step is skipped when its output already exists, so it is safe to re-run after
# a failure, after a dependency bump, or on a rebuilt volume.
#
# Nothing here is baked into an image on purpose. All of it lands under the
# repo and $HOME, both of which sit on the pod's persistent volume, so it
# survives restarts and image upgrades. Roughly 11 GB and a few minutes, once.
#
#   ./scripts/pod-setup.sh          build what is missing
#   ./scripts/pod-setup.sh --check  report what is present, change nothing
#
# What it does NOT do, because both hold secrets:
#   - write .env. Four keys are needed: GITHUB_TOKEN, GATEWAY_URL,
#     GATEWAY_TOKEN, CHATTERBOX_DEVICE=cpu. IG_ACCESS_TOKEN and IG_USER_ID are
#     deliberately not among them; the gateway owns publishing and refreshes
#     its own token, so a render host never needs an Instagram credential.
#   - populate PRIVATE_DIR. Copy PROFILE.md and one accounts/<name>/ directory
#     there by hand, once. `python main.py --migrate-account <name>` builds that
#     directory out of the single account layout this repo used before.
#   - set REELSMITH_ACCOUNT=<name> in .env. There is no default and a run
#     without one fails at startup rather than guessing.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Where the git-ignored half lives on this host. The pod mounts one NFS folder
# holding PROFILE.md, ref/ and data/; override for a different layout.
PRIVATE_DIR="${PRIVATE_DIR:-/mnt/reelsmith}"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

# The interpreter pyproject pins. One place, because it appears in the check,
# the build and the mismatch test below.
PY_VERSION="3.14"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
have() { [[ -e "$1" ]] && printf '  ok    %s\n' "$2" || printf '  MISS  %s\n' "$2"; }

# Existence is not enough for the venv. An upgrade changes the pinned version
# while the old venv stays perfectly present, so a check that only asks whether
# the file is there reports "ok (python 3.14)" at a 3.13 interpreter and the
# host quietly runs the wrong one against requirements compiled for the new
# one. Print what is actually installed and say so when it disagrees.
venv_python_version() {
  [[ -x "$1" ]] || return 1
  "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

have_venv() {
  local py="$1" want="$2" label="$3" got
  if ! got="$(venv_python_version "$py")"; then
    printf '  MISS  %s (python %s)\n' "$label" "$want"
  elif [[ "$got" == "$want" ]]; then
    printf '  ok    %s (python %s)\n' "$label" "$got"
  else
    printf '  DRIFT %s (python %s, wants %s -- delete it and re-run)\n' "$label" "$got" "$want"
  fi
}

# Same lesson as the venv: the browser being on the host is not the question.
# Playwright resolves a build number pinned to its own version, so a host can
# hold a perfectly good chromium from some other tool and still have none that
# this one will launch. Ask playwright which path it wants and test that.
have_browser() {
  local path
  path="$(.venv/bin/python -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p: print(p.chromium.executable_path)' 2>/dev/null)" || {
    printf '  MISS  chromium for the README screenshot (playwright not installed)\n'
    return
  }
  if [[ -x "$path" ]]; then
    printf '  ok    chromium for the README screenshot\n'
  else
    printf '  MISS  chromium for the README screenshot (wants %s)\n' "$path"
  fi
}

if [[ "$CHECK_ONLY" == 1 ]]; then
  say "reelsmith pod setup, current state"
  have_venv .venv/bin/python                  "$PY_VERSION" "pipeline venv"
  have_venv tools/chatterbox/.venv/bin/python "3.12"        "chatterbox venv"
  have video/node_modules/.bin/remotion    "remotion + node deps"
  have_browser
  have .env                                ".env (write by hand, holds secrets)"
  have "$PRIVATE_DIR/PROFILE.md"           "PROFILE.md on $PRIVATE_DIR"
  have "$PRIVATE_DIR/accounts"             "accounts on $PRIVATE_DIR"
  have accounts                            "accounts symlinks in the repo"
  # Which account tonight's batch is for. There is no default: a run without
  # one fails at startup rather than guessing, so a MISS here is a batch that
  # will not start.
  if grep -qs '^REELSMITH_ACCOUNT=' .env; then
    printf '  ok    REELSMITH_ACCOUNT in .env (%s)\n' \
      "$(grep -m1 '^REELSMITH_ACCOUNT=' .env | cut -d= -f2)"
  else
    printf '  MISS  REELSMITH_ACCOUNT in .env; every run needs --account without it\n'
  fi
  echo
  exit 0
fi

command -v uv >/dev/null || {
  echo "uv is not on PATH. It is what fetches the pinned Pythons." >&2
  echo "Install: curl -fsSL https://astral.sh/uv/install.sh | sh" >&2
  exit 1
}

# --- 1. The pipeline itself -------------------------------------------------
# 3.14 is pinned in pyproject and the base image will not have it; uv fetches a
# standalone build into its cache under $HOME.
#
# Rebuilt on a version mismatch, not just when missing. Idempotent has to mean
# "converges on what pyproject pins", otherwise the first Python upgrade leaves
# every existing host on the old interpreter running requirements compiled for
# the new one, and nothing here ever says so.
_have_py="$(venv_python_version .venv/bin/python || true)"
if [[ -z "$_have_py" ]]; then
  say "1/5  pipeline venv"
  uv venv --python "$PY_VERSION" .venv
  uv pip install --python .venv/bin/python -r requirements.txt
elif [[ "$_have_py" != "$PY_VERSION" ]]; then
  say "1/5  pipeline venv is python $_have_py, rebuilding on $PY_VERSION"
  rm -rf .venv
  uv venv --python "$PY_VERSION" .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  say "1/5  pipeline venv already built (python $_have_py)"
fi

# --- 2. The voice -----------------------------------------------------------
# A second interpreter by design: chatterbox wants torch, transformers and
# setuptools<81, and the pipeline venv is 3.14 on a setuptools that dropped
# pkg_resources. See tools/chatterbox/README.md for why they cannot be merged.
if [[ ! -x tools/chatterbox/.venv/bin/python ]]; then
  say "2/5  chatterbox venv"
  uv venv --python 3.12 tools/chatterbox/.venv
  VIRTUAL_ENV=tools/chatterbox/.venv uv pip install \
    chatterbox-tts soundfile "setuptools<81"
else
  say "2/5  chatterbox venv already built"
fi

# Checked every run rather than only on a fresh venv, because chatterbox-tts
# pins torch==2.6.0 and resolves it from PyPI, which on Linux is the CUDA
# build: ~2.5 GB of GPU runtime a CPU-only node cannot use. Any later
# `uv pip install` in this venv can drag it back, so the guard is on the
# installed build, not on whether we just created the venv.
TORCH_BUILD="$(tools/chatterbox/.venv/bin/python -c \
  'import torch; print(torch.__version__)' 2>/dev/null || echo none)"
if [[ "$TORCH_BUILD" == *"+cpu" ]]; then
  printf '     torch %s, already the CPU build\n' "$TORCH_BUILD"
else
  say "     torch is $TORCH_BUILD, swapping for the CPU build"
  # --reinstall-package is required: the CUDA wheel already satisfies
  # "==2.6.0", so a plain install is a no-op.
  VIRTUAL_ENV=tools/chatterbox/.venv uv pip install \
    --reinstall-package torch --reinstall-package torchaudio \
    "torch==2.6.0" "torchaudio==2.6.0" \
    --index-url https://download.pytorch.org/whl/cpu
fi

# --- 3. The renderer --------------------------------------------------------
if [[ ! -e video/node_modules/.bin/remotion ]]; then
  say "3/5  remotion and node deps"
  (cd video && npm ci)
else
  say "3/5  remotion already installed"
fi

# --- 4. The browser that takes the README screenshot ------------------------
# The cover is the README hero, so this is not an optional extra: without it
# `screenshot.capture_repo` logs a warning, `renderer` falls back to the repo
# card, and the run finishes green having shipped the one thing the cover
# exists to show. That is exactly what happened on this host, unnoticed, until
# a cover was looked at.
#
# Run unconditionally: playwright prints "is already installed" and exits, and
# it re-resolves the build its own version pins, so a dependency bump that
# moves chromium from 1228 to 1234 is repaired by re-running rather than by
# somebody reading a log. Do not substitute another chromium already on the
# host; playwright will not launch a build it did not pin.
say "4/5  chromium for the README screenshot"
.venv/bin/python -m playwright install chromium

# --- 5. The private half ----------------------------------------------------
# config.py resolves accounts/ and PROFILE.md relative to the repo root and
# offers no override, so the mount is linked into place rather than pointed at.
#
# One link per account directory, because an account owns its `.env`, its data
# store and its voice recording, and the whole point of `accounts/<name>/` is
# that those three travel together. PROFILE.md stays one file at the root: it
# carries its shared rules at the top and one section per account, and
# splitting it would duplicate the shared half.
#
# Each `accounts/<name>` is a *directory* symlink, never per-file:
# StarHistory.save() writes a temp file and renames it over the target, and
# that rename would replace a file symlink with a real file, silently writing
# to local disk instead of the share.
say "5/5  linking the private half from $PRIVATE_DIR"
if [[ -d "$PRIVATE_DIR" ]]; then
  [[ -e "$PRIVATE_DIR/PROFILE.md" ]] && ln -sfn "$PRIVATE_DIR/PROFILE.md" PROFILE.md

  if [[ -d "$PRIVATE_DIR/accounts" ]]; then
    mkdir -p accounts
    for home in "$PRIVATE_DIR"/accounts/*/; do
      [[ -d "$home" ]] || continue
      name="$(basename "$home")"
      ln -sfn "${home%/}" "accounts/$name"
      printf '  linked account %s\n' "$name"
    done
    grep -qxF "/accounts" .git/info/exclude 2>/dev/null ||
      echo "/accounts" >> .git/info/exclude
  else
    printf '  %s/accounts is empty. Run\n' "$PRIVATE_DIR"
    printf '    python main.py --migrate-account <name>\n'
    printf '  on the machine that has the single account layout, then copy the\n'
    printf '  resulting accounts/<name>/ onto the share.\n'
  fi

  # The pre-accounts layout is gone. Every host now keeps its identity under
  # $PRIVATE_DIR/accounts/<name>/, so nothing links $PRIVATE_DIR/ref or
  # $PRIVATE_DIR/data any more.
  #
  # It is worth saying why the old block is deleted rather than left as a
  # fallback. It ended in `[[ -e "$PRIVATE_DIR/ref/morten.wav" ]] && ln -sfn`,
  # and under `set -e` a failing test at the end of an `&&` list exits the
  # script. So the moment the legacy recording was tidied away, this stopped
  # linking anything at all and reported success by exiting early. A fallback
  # that breaks when the thing it falls back from is removed is worse than none.
else
  printf '  %s is not mounted; skipping. The pipeline will not find the voice\n' "$PRIVATE_DIR"
  printf '  or the profile until it is.\n'
fi

say "Done. Remaining manual steps:"
printf '  - .env, if ./scripts/pod-setup.sh --check says MISS.\n'
printf '  - REELSMITH_ACCOUNT=<name> in .env, or --account on every call. There\n'
printf '    is no default and a run without one fails at startup.\n'
