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
#   - populate PRIVATE_DIR. Copy PROFILE.md, ref/morten.wav and data/ there by
#     hand, once.

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

if [[ "$CHECK_ONLY" == 1 ]]; then
  say "reelsmith pod setup, current state"
  have_venv .venv/bin/python                  "$PY_VERSION" "pipeline venv"
  have_venv tools/chatterbox/.venv/bin/python "3.12"        "chatterbox venv"
  have video/node_modules/.bin/remotion    "remotion + node deps"
  have .env                                ".env (write by hand, holds secrets)"
  have "$PRIVATE_DIR/PROFILE.md"           "PROFILE.md on $PRIVATE_DIR"
  have "$PRIVATE_DIR/ref/morten.wav"       "voice recording on $PRIVATE_DIR"
  have "$PRIVATE_DIR/data"                 "data store on $PRIVATE_DIR"
  have data                                "data symlink in the repo"
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
  say "1/4  pipeline venv"
  uv venv --python "$PY_VERSION" .venv
  uv pip install --python .venv/bin/python -r requirements.txt
elif [[ "$_have_py" != "$PY_VERSION" ]]; then
  say "1/4  pipeline venv is python $_have_py, rebuilding on $PY_VERSION"
  rm -rf .venv
  uv venv --python "$PY_VERSION" .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  say "1/4  pipeline venv already built (python $_have_py)"
fi

# --- 2. The voice -----------------------------------------------------------
# A second interpreter by design: chatterbox wants torch, transformers and
# setuptools<81, and the pipeline venv is 3.14 on a setuptools that dropped
# pkg_resources. See tools/chatterbox/README.md for why they cannot be merged.
if [[ ! -x tools/chatterbox/.venv/bin/python ]]; then
  say "2/4  chatterbox venv"
  uv venv --python 3.12 tools/chatterbox/.venv
  VIRTUAL_ENV=tools/chatterbox/.venv uv pip install \
    chatterbox-tts soundfile "setuptools<81"
else
  say "2/4  chatterbox venv already built"
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
  say "3/4  remotion and node deps"
  (cd video && npm ci)
else
  say "3/4  remotion already installed"
fi

# --- 4. The private half ----------------------------------------------------
# config.py resolves data/ and PROFILE.md relative to the repo root and offers
# no override, so the mount is linked into place rather than pointed at.
#
# `data` is a directory symlink, never per-file: StarHistory.save() writes a
# temp file and renames it over the target, and that rename would replace a
# file symlink with a real file, silently writing to local disk instead of the
# share. morten.wav is read-only input, so a file symlink is safe there.
say "4/4  linking the private half from $PRIVATE_DIR"
if [[ -d "$PRIVATE_DIR" ]]; then
  [[ -e "$PRIVATE_DIR/PROFILE.md" ]] && ln -sfn "$PRIVATE_DIR/PROFILE.md" PROFILE.md
  [[ -e "$PRIVATE_DIR/ref/morten.wav" ]] &&
    ln -sfn "$PRIVATE_DIR/ref/morten.wav" tools/chatterbox/ref/morten.wav
  if [[ -d "$PRIVATE_DIR/data" && ! -L data ]]; then
    rm -rf data
    ln -sfn "$PRIVATE_DIR/data" data
    # data/.gitkeep is tracked, and replacing the directory reads as an
    # uncommitted deletion forever. Hide it locally rather than committing a
    # change that only makes sense on this host.
    git update-index --skip-worktree data/.gitkeep 2>/dev/null || true
    grep -qxF "/data" .git/info/exclude 2>/dev/null || echo "/data" >> .git/info/exclude
  fi
  printf '  linked\n'
else
  printf '  %s is not mounted; skipping. The pipeline will not find the voice\n' "$PRIVATE_DIR"
  printf '  or the profile until it is.\n'
fi

say "Done. Remaining manual step: .env, if ./scripts/pod-setup.sh --check says MISS."
