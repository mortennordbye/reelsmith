#!/usr/bin/env bash
# Play every render back to back on the same text, Kokoro first as the baseline.
# afplay ships with macOS, so there is nothing to install.
set -euo pipefail
cd "$(dirname "$0")/out"

for f in kokoro-am_michael.wav clone-*.wav; do
  [ -e "$f" ] || continue
  printf '\n>>> %s\n' "$f"
  afplay "$f"
  # A beat of silence between takes. Back to back with no gap, everything
  # sounds the same after the third one.
  sleep 1
done
