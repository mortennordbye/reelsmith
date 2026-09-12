"""Making an empty `accounts/<name>/` profile.

This module also used to move a single account checkout into `accounts/`. That
migration has run on every host that has one, and the fallback that kept an
unmigrated checkout working was what pointed a second account with no `data/`
of its own at the shared root `data/`. Both are gone; what is left is
`--new-account`.
"""

from __future__ import annotations

from pathlib import Path

from config import ACCOUNTS_DIR, ROOT

# What a new account's `.env` starts as. Only the per account half, because the
# root `.env` still holds the global one and a fragment repeating it is a
# fragment that drifts from it. The split is F4's table in
# docs/multi-destination-audit.md, measured rather than guessed.
#
# Every line is commented out. A profile with a blank `IG_USER_ID` looks
# configured and fails at the first publish; one with nothing set fails at
# `require_instagram`, which says what is missing and where to set it.
_ENV_TEMPLATE = """\
# {name}: the per account half of the settings.
#
# The root .env still holds the global half, and this file is layered over it,
# so anything not named here is inherited. Repeating a global value here is how
# the two drift apart.
#
# Select this account with --account {name}, or REELSMITH_ACCOUNT={name} in the
# root .env on a host that only ever runs one.

# --- Identity ---
# Which identity this account's destinations are grouped under in the panel.
# Set it before the first consent trip: every trip reads it, and a destination
# registered without one is grouped by its handle instead, which splits one
# identity across two headings the day a handle differs between platforms.
#
# It is deliberately not defaulted to {name}. Account 1's directory is
# `nightlybuild` and all of its gateway rows are grouped under
# `thenightlybuild`, so a default would have regrouped them on the next
# re-authorisation. Match what the gateway already holds, which
# `--destinations` prints.
# BRAND={name}

# --- Destinations ---
# Written for you by `scripts/authorise.py <platform> --account {name}`, which
# is the consent trip. Left here commented out because a run that finds none of
# them fans out to nothing and says so, where a blank one looks configured.
#
# The ids only, apart from Instagram. The gateway holds every credential and
# does the publishing, so no Google, TikTok or Page secret belongs on the
# machine that renders. IG_ACCESS_TOKEN is the exception because this machine
# can publish to Instagram directly.
# IG_USER_ID=
# IG_ACCESS_TOKEN=
# YOUTUBE_CHANNEL_ID=
# TIKTOK_OPEN_ID=
# FACEBOOK_PAGE_ID=

# --- End card ---
# What the second niche's episodes sign off with. Public machinery with no
# identity in it, so these are empty in a fresh checkout.
# ENDCARD_NAME=
# ENDCARD_HANDLE=
# ENDCARD_TAGLINE=

# --- Voice ---
# Left unset this resolves to accounts/{name}/ref/voice.wav. PROFILE.md is
# explicit that sharing one cloned voice across two accounts meant to look
# unrelated is the strongest link between them, so record a second one rather
# than pointing this at the first.
# CHATTERBOX_REF=
# CHATTERBOX_EXAGGERATION=
# CHATTERBOX_CFG_WEIGHT=

# --- Discovery ---
# The thresholds this account ranks on, if they differ from the checkout's.
# MIN_STARS_BREAKOUT=
# MIN_STARS_ESTABLISHED=
"""


def create(name: str, *, root: Path = ROOT) -> tuple[Path, list[Path]]:
    """Make an account directory, or return what is already there.

    Four directories and one file, and it cannot lose anything, which is why
    it needs no `--yes`. `brand/` is where the avatar goes, and both accounts
    that exist had to make it by hand, which is how a backup glob for it came
    to exist before the directory did.

    Returns the account directory and the paths it created, so the caller can
    say what it did rather than claiming it all.
    """
    accounts_dir = ACCOUNTS_DIR if root == ROOT else root / "accounts"
    home = accounts_dir / name
    made = []
    for directory in (home, home / "data", home / "ref", home / "brand"):
        if not directory.exists():
            directory.mkdir(parents=True)
            made.append(directory)

    env = home / ".env"
    if not env.exists():
        env.write_text(_ENV_TEMPLATE.format(name=name))
        made.append(env)
    return home, made
