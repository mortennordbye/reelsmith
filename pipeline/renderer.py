"""Step 5 -- render the MP4 via the Remotion CLI.

Everything renderer-specific is confined to this file. The rest of the
pipeline only ever produces a VideoSpec, so swapping Remotion for another
backend means writing a sibling of this module and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from config import Settings
from pipeline.models import VideoSpec

log = logging.getLogger(__name__)

# Where each run stages its assets, under video/public/, and how old a run's
# directory has to be before another run may delete it. A day is far past the
# longest render and short enough that a dead run's files do not pile up.
STAGING_DIR = "staged"
STAGING_TTL_S = 24 * 60 * 60

# The flat filenames stage_asset() produced before per run staging:
# "<slug>-<original name>". Nothing writes them any more; the prune still
# recognises them so a checkout that rendered under the old layout empties out.
# Slugs are lowercase alphanumerics and hyphens (see RepoCandidate.slug).
#
# `repo-page.png` has to be named here explicitly. The slug pattern is greedy
# over hyphens, so a rule written for `repo.png` does not cover it, and an
# asset this sweep does not recognise is one that is never deleted: public/ is
# a staging area and the prune is the only thing that empties it. A tall README
# capture is the largest file the pipeline stages, so accumulating one per run
# is the worst version of that to get wrong.
# `art<N>` is the second niche's staged artefact, a Commons scan per shot.
STAGED_ASSET_RE = re.compile(
    r"[a-z0-9-]+-(?:voice\.(?:wav|mp3)|repo\.png|repo-page\.png|art\d+\.(?:jpg|png))"
)

# Frame of the opening scene to grab the cover from. The hero entrance is a
# spring that settles well inside a second; 90 frames (3s at 30fps) is past it
# with room to spare, and still inside the scene's 7-second hold.
COVER_FRAME = 90

# Staged files that belong to nothing this pipeline renders and must survive a
# prune anyway. `cv-` is the hand built prototypes' prefix: their assets are
# scans that are no longer trivially re-fetchable, they are the only copy
# outside a backup, and one of them matched the sweep and was deleted by the
# first generated render.
PROTECTED_PREFIXES = ("cv-",)

# Past this an episode is close enough to TikTok's 64 MB single chunk cap to
# say so out loud. Not a refusal: the other three destinations take it, and a
# render that already exists is not worth throwing away over one of them.
EPISODE_SIZE_WARNING_MB = 55


class RenderError(RuntimeError):
    pass


def _ensure_node_deps(video_dir: Path) -> None:
    """Install the Remotion deps if they are missing, rather than refusing.

    This used to raise and name the command to run. That is the right shape for
    a person at a terminal and the wrong one for the nightly, because of where
    the check sits: stage 5 of 5, after discovery, after a Claude script, after
    a Chatterbox voiceover and after Whisper. A missing `node_modules` there
    throws away everything the run already paid for, and it does it once per
    video, every night, until somebody looks.

    That is not hypothetical. The render host lost `video/node_modules` on
    2026-09-07 when the pod was recreated, and the next three nights each wrote
    a script and a voiceover and produced nothing. Nothing alerted, because a
    render host that queues no rows looks exactly like one where `--max-queue`
    stopped the batch, which is the normal outcome. It surfaced as the account's
    views decaying days later, once the queue drained.

    So it installs. `npm ci` is idempotent, takes about fifteen seconds against
    a warm cache, and is what `scripts/pod-setup.sh` already runs. A failure to
    install still raises, because at that point there is genuinely nothing to
    render with.

    The marker is `.bin/remotion` rather than the directory, for the reason
    `pod-setup.sh --check` uses it: an interrupted install leaves a
    `node_modules` that exists and cannot render.
    """
    if (video_dir / "node_modules" / ".bin" / "remotion").exists():
        return

    # `npm ci` needs the lockfile and is the reproducible one; without it there
    # is nothing to be reproducible against, so fall back rather than fail.
    cmd = ["npm", "ci"] if (video_dir / "package-lock.json").exists() else ["npm", "install"]
    log.warning("Remotion dependencies are missing; running %s", " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=video_dir, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:  # npm itself is not on PATH
        raise RenderError(
            f"Remotion dependencies are missing and npm is not installed.\n"
            f"Run: cd {video_dir} && npm install"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RenderError(
            f"Remotion dependencies are missing and {' '.join(cmd)} failed.\n"
            f"{(exc.stderr or exc.stdout or '').strip()[-2000:]}"
        ) from exc

    if not (video_dir / "node_modules" / ".bin" / "remotion").exists():
        raise RenderError(
            f"{' '.join(cmd)} finished but Remotion is still not installed.\n"
            f"Run: cd {video_dir} && npm install"
        )
    log.info("Installed the Remotion dependencies")


def staging_key(account: str, slug: str) -> str:
    """Where one run's staged assets live, relative to `video/public/`.

    `staged/<account>/<slug>`. Under a `staged/` directory of its own rather
    than straight under `public/`, so the prune can remove whole run
    directories without ever having to decide whether a directory a person
    made by hand is one of them.
    """
    return f"{STAGING_DIR}/{account or '_'}/{slug}"


def stage_asset(asset_path: Path, video_dir: Path, run_key: str) -> str:
    """Copy an asset into this run's staging directory, return its staticFile() path.

    Remotion can only load assets from public/, so this copy is required
    rather than incidental. `staticFile()` takes subpaths, so the returned
    path carries the run directory and the spec needs nothing else.
    """
    target_dir = video_dir / "public" / run_key
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(asset_path, target_dir / asset_path.name)
    # `copy2` keeps the source's mtime, so the directory is what says how
    # recently a run staged anything, and the prune ages runs by it.
    os.utime(target_dir)
    return f"{run_key}/{asset_path.name}"


def release_staged(video_dir: Path, run_key: str) -> None:
    """Delete this run's staging directory, once its video and covers exist."""
    shutil.rmtree(video_dir / "public" / run_key, ignore_errors=True)


def prune_staged_assets(video_dir: Path, keep: str, *, now: float | None = None) -> int:
    """Delete what other runs left staged, if it is old. Returns the count removed.

    public/ is a staging area, not a store: everything in it was copied from
    build/ and is re-staged on demand. A run releases its own directory when it
    finishes, so this only catches runs that died before that.

    **Only past `STAGING_TTL_S`, never by name alone.** Remotion symlinks
    `video/public` into its bundle, so a prune from another process deletes
    files out of a render already in flight. It used to remove every other
    slug's files on the way in, which was safe only while nothing ever
    rendered at the same time as anything else.

    Two things are swept: run directories under `staged/`, and the flat
    `<slug>-voice.wav` style files every run wrote before per run staging.
    Matching on the flat names stays deliberately narrow, so anything a human
    drops into public/ by hand survives, and `PROTECTED_PREFIXES` still guards
    the prototypes' scans that sit beside them.
    """
    public = video_dir / "public"
    if not public.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0

    staged_root = public / STAGING_DIR
    if staged_root.is_dir():
        for account_dir in staged_root.iterdir():
            if not account_dir.is_dir():
                continue
            for run in account_dir.iterdir():
                if not run.is_dir() or run == public / keep:
                    continue
                if now - run.stat().st_mtime < STAGING_TTL_S:
                    continue
                shutil.rmtree(run, ignore_errors=True)
                removed += 1

    for path in public.iterdir():
        if not path.is_file() or not STAGED_ASSET_RE.fullmatch(path.name):
            continue
        if path.name.startswith(PROTECTED_PREFIXES):
            continue
        if now - path.stat().st_mtime < STAGING_TTL_S:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # a locked file is not worth failing a render over
            log.debug("Could not prune %s (%s)", path, exc)

    if removed:
        log.info("Pruned %d stale staged run(s) and file(s) from %s", removed, public)
    return removed


def render_episode(
    spec, out_path: Path, cfg: Settings, *, concurrency: int | None = None
) -> Path:
    """Render the second niche's composition from an `EpisodeSpec`.

    A second entry point rather than a branch inside `render`, because the two
    take different specs and name different compositions, and the only thing
    they share is the subprocess. Untyped in the signature on purpose: this
    module imports `VideoSpec` and nothing else from the models, and an episode
    spec arriving here as a pydantic model with `model_dump_json` is the whole
    contract.
    """
    video_dir = cfg.video_dir
    _ensure_node_deps(video_dir)

    props_path = video_dir / f".props-episode-{spec.slug}.json"
    props_path.write_text(spec.model_dump_json())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npx", "remotion", "render", "Episode", str(out_path.resolve()),
        f"--props={props_path.resolve()}",
        # **Not the reel's quality settings, and the difference is the shot.**
        # A reel is flat colour and syntax highlighted text, where Remotion's
        # default CRF keeps glyph edges clean. An episode is a photograph of a
        # four hundred year old page with grain over it, where every frame
        # differs from the last in a way h264 cannot predict: the first render
        # came out at 185 MB for 40 seconds, against about 10 MB for a reel,
        # which is past what TikTok will take in one chunk. `EPISODE_CRF` is
        # read rather than written here, because the size warning below tells
        # the operator to raise it and a hardcoded value made that advice inert.
        f"--crf={cfg.episode_crf}",
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

    megabytes = out_path.stat().st_size / 1_048_576
    log.info("Rendered %s (%.1f MB)", out_path.name, megabytes)
    # Said here rather than discovered at publish. TikTok takes one chunk of
    # 64 MB and the gateway refuses to split, so a file over this is a row that
    # queues cleanly and fails days later on a platform nobody was watching.
    if megabytes > EPISODE_SIZE_WARNING_MB:
        log.warning(
            "%s is %.0f MB. TikTok takes 64 MB in one chunk, so raise EPISODE_CRF "
            "or shorten the episode before queueing it.",
            out_path.name, megabytes,
        )
    return out_path


def render_episode_cover(spec, out_path: Path, cfg: Settings) -> Path | None:
    """One still from the episode, for the cover. None on any failure.

    Best effort, the same as `render_covers` and for the same reason: a failed
    still must never fail a run that already produced a video. What it costs
    when it is missing is a queue row that renders as a black player in the
    panel, which looks exactly like a video that failed.

    Taken past the opening shot's entrance rather than at frame 0, which on the
    reel side captures a hero at zero opacity and yields an empty background.
    """
    video_dir = cfg.video_dir
    props_path = video_dir / f".props-cover-episode-{spec.slug}.json"
    props_path.write_text(spec.model_dump_json())
    frame = min(COVER_FRAME, max(spec.durationInFrames - 1, 0))
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [
                "npx", "remotion", "still", "Episode", str(out_path.resolve()),
                f"--props={props_path.resolve()}",
                f"--frame={frame}",
                "--log=error",
            ],
            cwd=video_dir, capture_output=True, text=True, check=False, timeout=600,
        )
    except subprocess.SubprocessError as exc:
        log.warning("Cover still failed: %s", exc)
        return None
    finally:
        props_path.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        log.warning("Cover still failed: %s", (proc.stderr or proc.stdout)[-400:])
        return None
    return out_path


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


def render_covers(spec: VideoSpec, out_dir: Path, cfg: Settings) -> list[Path]:
    """Render the Reels cover stills.

    Every frame of the video carries the hook or a burned-in caption, so
    Instagram's cover picker has nothing clean to offer. Two variants come out
    of the same composition:

      cover.png        hook set inside the crop-safe band, ready to upload
      cover-clean.png  the README hero alone, to design over by hand

    Best effort. A cover that fails to render must never fail a run that already
    produced a video, so this logs and returns whatever it managed.
    """
    video_dir = cfg.video_dir
    _ensure_node_deps(video_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Scenes animate in from their own frame 0, so frame 0 of the opening scene
    # is the hero at zero opacity. Far enough in for the entrance to have
    # settled, clamped so a very short opening scene still lands in range.
    opening_frames = spec.scenes[0].durationInFrames if spec.scenes else 1
    frame = max(0, min(COVER_FRAME, opening_frames - 1))

    written: list[Path] = []
    for name, show_hook in (("cover.png", True), ("cover-clean.png", False)):
        out_path = out_dir / name
        props = json.loads(spec.model_dump_json())
        props["showHook"] = show_hook
        props_path = video_dir / f".props-cover-{spec.slug}.json"
        props_path.write_text(json.dumps(props))

        cmd = [
            "npx", "remotion", "still", "Cover", str(out_path.resolve()),
            f"--props={props_path.resolve()}",
            f"--frame={frame}",
            "--log=error",
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                cmd, cwd=video_dir, capture_output=True, text=True, check=False, timeout=600
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Cover render failed for %s (%s)", name, exc)
            continue
        finally:
            props_path.unlink(missing_ok=True)

        if proc.returncode != 0 or not out_path.exists():
            tail = (proc.stderr or proc.stdout)[-800:]
            log.warning("Cover render failed for %s:\n%s", name, tail)
            continue
        written.append(out_path)

    if written:
        log.info("Wrote %s", ", ".join(p.name for p in written))
    return written


def write_spec(spec: VideoSpec, path: Path) -> Path:
    """Persist video.json so the Studio can load the exact same props."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json.loads(spec.model_dump_json()), indent=2))
    return path
