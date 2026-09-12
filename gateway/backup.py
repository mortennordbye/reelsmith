"""A point in time copy of the state, because one of these tables is not
reconstructible.

The database sits on NAS backed NFS, so it already survives losing a node. What
it had no answer for is the likelier failure: a bad migration, a mistaken
delete, or a corrupt page. There is no second copy to go back to, and the
storage class reclaims with `Delete`, so removing the PVC removes the data.

Ranked by what losing each table actually costs:

- **`comments_handled` cannot be rebuilt and losing it is not neutral.** It is
  the claim table enforcing one private reply per comment, and Meta allows a
  reply for seven days. A poller that starts again with an empty one re-replies
  to every comment still inside that window, which is a spam incident on a live
  account rather than an inconvenience.
- **`insights` cannot be rebuilt either.** Meta serves the current numbers and
  no history, so a per-day reading that is lost is lost. That is the whole
  record of whether anything is working.
- The account token means a browser trip to re-authorise, and the queue means
  re-enqueueing videos that still exist on the Mac. Both are annoying and both
  are recoverable.

`VACUUM INTO` rather than copying the file, because copying a live SQLite
database can capture a torn page while a write is in flight, and the copy looks
fine until the day it is needed. `VACUUM INTO` runs in a read transaction and
writes a consistent, already compacted database.

**The copies beside the database do not defend against losing the volume.**
They defend against the file being wrong, which is the failure that has no
other answer. `GATEWAY_BACKUP_OFFSITE_DIR` adds a second copy of each one on
storage the volume does not own, which is what makes a schema migration safe to
ship: a migration that goes wrong and a PVC that gets deleted while fixing it
are the two failures that happen together.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

import aiosqlite

from gateway import db
from gateway.config import GatewaySettings
from gateway.metrics import Metrics

log = logging.getLogger(__name__)

_STAMP = "%Y%m%dT%H%M%S"
_PREFIX = "state-"
_SUFFIX = ".sqlite3"


def _name(moment: datetime) -> str:
    return f"{_PREFIX}{moment.strftime(_STAMP)}{_SUFFIX}"


def prune(directory: Path, *, keep: int) -> int:
    """Drop all but the newest `keep` copies. Returns how many went.

    Matches on the exact name this module writes rather than on everything in
    the directory, the same rule `renderer.prune_staged_assets` follows: a
    sweep that deletes what it did not create eventually deletes something
    somebody put there on purpose.
    """
    existing = sorted(
        p for p in directory.glob(f"{_PREFIX}*{_SUFFIX}") if p.is_file()
    )
    stale = existing[: max(len(existing) - keep, 0)]
    for path in stale:
        try:
            path.unlink()
        except OSError as exc:  # pragma: no cover - the filesystem misbehaving
            log.warning("Could not remove the old backup %s: %s", path.name, exc)
    return len(stale)


async def backup_once(cfg: GatewaySettings, metrics: Metrics) -> Path | None:
    """Write one consistent copy and prune the old ones.

    Returns the path written, or None if one already exists for this second,
    which is what a restart inside the same second looks like.

    **On its own connection, not the service's.** The whole gateway shares one
    `aiosqlite.Connection`, and SQLite refuses to vacuum a connection that has
    statements in progress. On the first deploy this raised `cannot VACUUM -
    SQL statements in progress` every six hours, forever, while every test
    passed: a test connection is idle at the moment it is asked, and a live one
    with a poller, a scheduler and an insights sweep on it is not.

    A second connection is safe here because the database runs in WAL mode, so
    a reader does not block or get blocked by the writer.
    """
    directory = cfg.backup_dir
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / _name(db.now())
    if target.exists():
        return None

    reader = await aiosqlite.connect(cfg.db_path)
    try:
        # The path is interpolated because SQLite takes no parameter here. It
        # is built from a timestamp and a configured directory, never from a
        # request.
        await reader.execute(f"VACUUM INTO '{target}'")  # noqa: S608
    finally:
        await reader.close()

    dropped = prune(directory, keep=cfg.backup_keep)
    metrics.backup_last_success.set(db.now().timestamp())
    log.info(
        "Backed up the state to %s (%.1f MB), pruned %d",
        target.name, target.stat().st_size / 1e6, dropped,
    )
    if cfg.backup_offsite_dir is not None:
        copy_offsite(target, cfg, metrics)
    return target


def copy_offsite(source: Path, cfg: GatewaySettings, metrics: Metrics) -> Path | None:
    """Copy a finished backup off the state volume. None if that failed.

    The copy beside the database answers a bad row; this one answers a lost
    volume. It copies the file `VACUUM INTO` already wrote rather than
    vacuuming twice, since that file is closed and consistent.

    **Written under a temporary name and renamed**, so a copy interrupted
    halfway never leaves a file that looks like a backup. A failure is logged
    and never raised: the local copy already succeeded, and the staleness
    alert on this gauge is what says the second one stopped.
    """
    directory = cfg.backup_offsite_dir
    assert directory is not None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f".{source.name}.partial"
        shutil.copyfile(source, partial)
        target = partial.rename(directory / source.name)
        dropped = prune(directory, keep=cfg.backup_offsite_keep)
    except OSError as exc:
        log.warning("Offsite backup copy to %s failed: %s", directory, exc)
        return None
    metrics.backup_offsite_last_success.set(db.now().timestamp())
    log.info("Copied the backup offsite to %s, pruned %d", target, dropped)
    return target


async def backup_loop(cfg: GatewaySettings, metrics: Metrics) -> None:
    while True:
        try:
            await backup_once(cfg, metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never take the service down over a backup. A gateway that stops
            # answering Meta because a disk filled has turned a recoverable
            # problem into an outage.
            log.exception("Backup failed, continuing")
        await asyncio.sleep(cfg.backup_interval_s)
