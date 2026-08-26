"""Moving a single account checkout into `accounts/<name>/`.

This repo held one account's identity in four places: `data/` for the cooldown
store and the live token, `tools/chatterbox/ref/morten.wav` for the voice, the
root `.env` for the credentials, and `build/<date>/` for every run folder ever
produced. `--account` puts all four under one directory per account, which is
one thing to back up rather than four, and it is what makes a second account
possible at all.

Nothing here is clever. It is a planner and an executor kept apart, because the
files it moves include the only copy of a cloned voice and about a month of run
folders that the feedback loop reads its hooks out of. The planner is pure and
returns what it would do; the executor takes that plan and does it. `main.py`
prints the plan and moves nothing unless it is told twice.

Two rules that are not obvious:

- **`data` moves as a directory, never file by file.** On a host that keeps it
  on a share, `data` is a symlink, and `StarHistory.save()` renames a temp file
  over its target. That rename replaces a *file* symlink with a real file, so
  per file links silently send writes to local disk. Moving the directory entry
  itself keeps whatever it already was.
- **A run folder with a dot in its name is still moved.** The dot marks a run a
  human set aside, which `results._hooks_by_repo` ranks below an unsuffixed
  sibling and which `--recover` skips. That meaning is per folder and it
  survives the move unchanged, so leaving them behind would silently drop the
  losing half of every regenerated script.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from config import ACCOUNTS_DIR, LEGACY_DATA_DIR, LEGACY_VOICE_REF, ROOT


@dataclass(frozen=True)
class Move:
    """One thing the migration would do, in terms a person can check."""

    source: Path
    target: Path
    what: str

    @property
    def blocked(self) -> str:
        """Why this move cannot happen, or empty if it can."""
        if not self.source.exists() and not self.source.is_symlink():
            return "nothing there"
        if self.target.exists() or self.target.is_symlink():
            return "the target already exists"
        return ""


def plan(name: str, *, root: Path = ROOT) -> list[Move]:
    """Everything moving one account into `accounts/<name>/` would touch.

    Ordered the way a person would check it: the credentials first, then the
    state that cannot be regenerated, then the run folders, which are the bulk
    and the least precious.
    """
    accounts_dir = ACCOUNTS_DIR if root == ROOT else root / "accounts"
    data_dir = LEGACY_DATA_DIR if root == ROOT else root / "data"
    voice_ref = LEGACY_VOICE_REF if root == ROOT else root / "tools/chatterbox/ref/morten.wav"
    home = accounts_dir / name

    moves = [
        Move(root / ".env", home / ".env", "the credentials and per account knobs"),
        Move(data_dir, home / "data", "the cooldown store, star history and token"),
        Move(voice_ref, home / "ref" / "voice.wav", "the voice the clone is built from"),
    ]

    build = root / "build"
    if build.is_dir():
        for day in sorted(p for p in build.iterdir() if p.is_dir()):
            # A directory already named after an account is a build subtree
            # that has been migrated, not a date. Dates are the only other
            # thing that has ever been at this level.
            if not _looks_like_a_date(day.name):
                continue
            moves.append(
                Move(day, build / name / day.name, f"{_run_count(day)} run folder(s)")
            )
    return moves


def _looks_like_a_date(name: str) -> bool:
    parts = name.split("-")
    return len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[0]) == 4


def _run_count(day: Path) -> int:
    return sum(1 for p in day.iterdir() if p.is_dir())


def apply(moves: list[Move]) -> list[Move]:
    """Do the moves that are not blocked, and return the ones that were done.

    `.env` is copied rather than moved. It is the one file here that a running
    host may be reading right now, and the root one holds the global half --
    the GitHub token, the gateway URL -- which stays where it is. The operator
    trims the account half out of the root copy afterwards, which is a judgement
    about which lines are global and not something to guess at.
    """
    done = []
    for move in moves:
        if move.blocked:
            continue
        move.target.parent.mkdir(parents=True, exist_ok=True)
        if move.source.name == ".env":
            shutil.copy2(move.source, move.target)
        else:
            # `Path.rename` refuses to cross a filesystem, which `data` being a
            # symlink to a share makes likely. `shutil.move` on a symlink moves
            # the link itself, which is exactly what is wanted.
            shutil.move(str(move.source), str(move.target))
        done.append(move)
    return done


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

# --- Instagram ---
# IG_USER_ID=
# IG_ACCESS_TOKEN=

# --- The other two destinations ---
# The ids only. The gateway holds the credentials and does the publishing, so
# no Google or TikTok secret belongs on the machine that renders.
# YOUTUBE_CHANNEL_ID=
# TIKTOK_OPEN_ID=

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

    Three directories and one file. Deliberately not a `--migrate-account`
    variant: that one moves an existing identity and is dangerous enough to
    need asking twice, and this one creates an empty profile and cannot lose
    anything.

    Returns the account directory and the paths it created, so the caller can
    say what it did rather than claiming it all.
    """
    accounts_dir = ACCOUNTS_DIR if root == ROOT else root / "accounts"
    home = accounts_dir / name
    made = []
    for directory in (home, home / "data", home / "ref"):
        if not directory.exists():
            directory.mkdir(parents=True)
            made.append(directory)

    env = home / ".env"
    if not env.exists():
        env.write_text(_ENV_TEMPLATE.format(name=name))
        made.append(env)
    return home, made
