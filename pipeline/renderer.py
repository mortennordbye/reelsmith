"""Step 5 -- render the MP4 via the Remotion CLI.

Everything renderer-specific is confined to this file. The rest of the
pipeline only ever produces a VideoSpec, so swapping Remotion for another
backend means writing a sibling of this module and nothing else.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from config import Settings
from pipeline.models import VideoSpec

log = logging.getLogger(__name__)

# The filenames stage_asset() produces: "<slug>-<original name>". Slugs are
# lowercase alphanumerics and hyphens (see RepoCandidate.slug).
STAGED_ASSET_RE = re.compile(r"[a-z0-9-]+-(?:voice\.(?:wav|mp3)|repo\.png)")


class RenderError(RuntimeError):
    pass


def _ensure_node_deps(video_dir: Path) -> None:
    if not (video_dir / "node_modules").exists():
        raise RenderError(
            f"Remotion dependencies are not installed.\nRun: cd {video_dir} && npm install"
        )


def stage_asset(asset_path: Path, video_dir: Path, slug: str) -> str:
    """Copy an asset into video/public/ and return its staticFile() path.

    Remotion can only load assets from public/, so this copy is required
    rather than incidental. The slug prefix keeps concurrent runs from
    clobbering each other's files.
    """
    public = video_dir / "public"
    public.mkdir(parents=True, exist_ok=True)
    target = public / f"{slug}-{asset_path.name}"
    shutil.copy2(asset_path, target)
    return target.name


def prune_staged_assets(video_dir: Path, keep_slug: str) -> int:
    """Delete staged assets belonging to other slugs. Returns the count removed.

    public/ is a staging area, not a store: everything in it was copied from
    build/ and is re-staged on demand, so a stale copy is pure waste. Left alone
    it accumulates an audio file and a screenshot per video, forever.

    Matching is deliberately narrow -- only the exact filenames this pipeline
    stages -- so anything a human drops into public/ by hand survives. Adding a
    new staged asset type means adding it here, or its old copies just linger.
    """
    public = video_dir / "public"
    if not public.is_dir():
        return 0

    removed = 0
    for path in public.iterdir():
        if not path.is_file() or not STAGED_ASSET_RE.fullmatch(path.name):
            continue
        if path.name.startswith(f"{keep_slug}-"):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # a locked file is not worth failing a render over
            log.debug("Could not prune %s (%s)", path, exc)

    if removed:
        log.info("Pruned %d stale staged asset(s) from %s", removed, public)
    return removed


def render(
    spec: VideoSpec, out_path: Path, cfg: Settings, *, concurrency: int | None = None
) -> Path:
    video_dir = cfg.video_dir
    _ensure_node_deps(video_dir)

    # Remotion reads props from a file rather than argv: a full spec with
    # captions easily exceeds the OS argument-length limit.
    props_path = video_dir / f".props-{spec.slug}.json"
    props_path.write_text(spec.model_dump_json())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npx", "remotion", "render", "Reel", str(out_path.resolve()),
        f"--props={props_path.resolve()}",
        "--log=info",
    ]
    if concurrency:
        cmd.append(f"--concurrency={concurrency}")

    log.info("Rendering %d frames -> %s", spec.durationInFrames, out_path.name)
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, cwd=video_dir, capture_output=True, text=True, check=False, timeout=1800
        )
    finally:
        props_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-2500:]
        raise RenderError(f"remotion render failed (exit {proc.returncode}):\n{tail}")

    if not out_path.exists():
        raise RenderError(f"Remotion reported success but {out_path} does not exist")

    log.info("Rendered %s (%.1f MB)", out_path.name, out_path.stat().st_size / 1_048_576)
    return out_path


def write_spec(spec: VideoSpec, path: Path) -> Path:
    """Persist video.json so the Studio can load the exact same props."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json.loads(spec.model_dump_json()), indent=2))
    return path
