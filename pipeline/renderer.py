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
from pipeline.models import CueKind, Scene, VideoSpec

log = logging.getLogger(__name__)

# The filenames stage_asset() produces: "<slug>-<original name>". Slugs are
# lowercase alphanumerics and hyphens (see RepoCandidate.slug).
STAGED_ASSET_RE = re.compile(r"[a-z0-9-]+-(?:voice\.(?:wav|mp3)|repo\.png)")

# Frame of the opening scene to grab the cover from. The hero entrance is a
# spring that settles well inside a second; 90 frames (3s at 30fps) is past it
# with room to spare, and still inside the scene's 7-second hold.
COVER_FRAME = 90


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


# The name the version without the ask always takes, beside `out.mp4` in the
# run folder. Fixed rather than passed around, because two callers have to
# agree on it: the step that writes it and the enqueue that looks for it later.
NO_CTA_NAME = "out-no-cta.mp4"



# How long the README hero holds at the end of the version without the ask.
# Only that version: the hero bookends the video, and on the full one the
# closing hero is the scene the ask is spoken over. Cutting at the ask removes
# it, so a Short would end on whichever content cue happened to be last, and
# the best looking asset in the video would appear only in the part that was
# cut off.
#
# Done here rather than in `spec.py` because this is a separate render. Moving
# it there would take the same seconds off the last content scene on Instagram
# too, to fix an ending only YouTube has.
HERO_TAIL_SECONDS = 1.6

# What the content scene the hero cuts into keeps. Lower than
# `MIN_SCENE_SECONDS`, deliberately: that floor is about a cut being
# readable in the middle of a video, and this is the last thing on screen
# before a closing hold on an image the viewer already saw at the start.
# Without the lower floor the hero never fits at all, because the last
# content scene usually runs about 2.6s against a 1.8s minimum.
MIN_TAIL_CONTENT_SECONDS = 1.0

# Below this a closing hero is a flash rather than a hold, and the frames
# are worth more on the content scene that would otherwise lose them.
MIN_HERO_TAIL_SECONDS = 0.8


def _scenes_ending_on_hero(spec: VideoSpec) -> list[Scene]:
    """The scenes up to the ask, closing on the README hero.

    The hero has to come from somewhere, and the only place is the tail of the
    last content scene. That is affordable here in a way it is not on the full
    video: this render exists to end well, and the seconds spent are seconds a
    viewer would otherwise have spent on a cue that has finished being spoken.

    Falls back to a plain truncation when the last content scene cannot spare
    the time without dropping under `MIN_SCENE_SECONDS`, or when there is no
    hero image to cut to.
    """
    cut = spec.ctaFromFrame
    kept = [s for s in spec.scenes if s.fromFrame < cut]
    if not kept:
        return spec.scenes

    hero = next((s for s in spec.scenes if s.kind is CueKind.SCREENSHOT and s.imageSrc), None)
    last = kept[-1]
    spare = (cut - last.fromFrame) - int(MIN_TAIL_CONTENT_SECONDS * spec.fps)
    tail = min(int(HERO_TAIL_SECONDS * spec.fps), max(0, spare))

    truncated = [
        s.model_copy(update={"durationInFrames": min(s.durationInFrames, cut - s.fromFrame)})
        for s in kept
    ]
    if hero is None or tail < int(MIN_HERO_TAIL_SECONDS * spec.fps):
        return truncated

    truncated[-1] = truncated[-1].model_copy(
        update={"durationInFrames": cut - tail - last.fromFrame}
    )
    truncated.append(
        hero.model_copy(update={"fromFrame": cut - tail, "durationInFrames": tail})
    )
    return truncated


def render_without_cta(spec: VideoSpec, out_dir: Path, cfg: Settings) -> Path | None:
    """Render the same video with no ask in it. Returns the path, or None.

    The ask is Instagram's word. "Follow for a new one every night" asks a
    YouTube viewer for something that surface calls subscribing, and an account
    that cannot name the button it is pointing at reads as reposted from
    somewhere else, which is the one thing a channel under an inauthentic
    content review should not look like. It was a harder promise before, when
    the ask was "comment ANYDOC if you want the link" and YouTube had no private
    replies to deliver it with. The ask is in three places at once: spoken in
    the voiceover, in the burned-in captions, and on a chip that runs from the
    middle of the video to the last frame.

    That last one is why this is a second render rather than a cut of the
    first. A chip visible from halfway cannot also be absent from a truncation
    of the same file; the pixels are either there or they are not. Clearing
    `showFollowCta` removes it, and stopping at `ctaFromFrame` drops the spoken
    ask and the captions that transcribe it, because Remotion renders the audio
    for the frames it renders and no others.

    **No second voiceover.** The audio file is reused exactly as it is, which
    matters: TTS is the step that holds about four gigabytes and has taken the
    whole nightly batch down with it. This costs CPU and minutes, not memory.

    None when there is nothing to remove, or when the ask never got a frame of
    its own, in which case there is no honest place to stop and the caller
    falls back to the full video.
    """
    if not spec.showFollowCta or spec.ctaFromFrame is None:
        return None

    # Same spec, minus the ask and everything after it.
    without = spec.model_copy(
        update={
            "showFollowCta": False,
            "durationInFrames": spec.ctaFromFrame,
            "scenes": _scenes_ending_on_hero(spec),
        }
    )
    out_path = out_dir / NO_CTA_NAME
    try:
        render(without, out_path, cfg)
    except RenderError as exc:
        # Never fatal. The Reel is already rendered and queued by the time this
        # runs, and a Short carrying an ask it cannot honour is a smaller
        # problem than no Short at all.
        log.warning("Could not render the version without the ask: %s", exc)
        return None

    log.info(
        "Rendered %s, %.1fs of %.1fs, no ask",
        out_path.name, spec.ctaFromFrame / spec.fps, spec.durationInFrames / spec.fps,
    )
    return out_path
