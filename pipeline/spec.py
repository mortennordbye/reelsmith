"""Assemble the renderer-agnostic video.json.

The key idea: scene durations are allocated proportionally to the *spoken
words* each cue accompanies, and the total is pinned to the real measured audio
duration. That is what makes it structurally impossible for the visuals to
drift out of sync with the voiceover, no matter how long Claude's script runs
or how fast the TTS voice reads it.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from config import Settings
from pipeline.models import (
    Caption,
    CueKind,
    RepoCandidate,
    RepoMeta,
    Scene,
    VideoScript,
    VideoSpec,
)

log = logging.getLogger(__name__)

# Every scene gets at least this long, or fast cuts become unreadable.
MIN_SCENE_SECONDS = 1.8

# How long the opening README hero holds. Still the longest scene in the video:
# it is the one shot the maintainer actually designed (logo, title lockup,
# badges), it carries the most information per second, and it needs time to be
# read rather than just glimpsed. Everything after it is ours.
#
# Trimmed from 7.0 because the hook overlay covers the first 3 seconds of it, so
# the back half was a static image with nothing new arriving, at exactly the
# point a viewer decides whether to stay.
INTRO_SECONDS = 5.5

# No scene should hold longer than this. Nothing enforces it, because a scene's
# length comes from how long its cue is spoken for and there is no second thing
# to cut to. It is the prompt's job: more cues and shorter excerpts. This is
# here so the render says when that failed rather than quietly shipping a ten
# second static hold.
MAX_SCENE_SECONDS = 5.5


def _norm_words(text: str) -> list[str]:
    """Lowercase alphanumeric-only words, for matching script text against
    what Whisper actually heard. Punctuation and casing never survive the
    round trip through TTS and transcription, so both sides get stripped."""
    out = []
    for raw in text.split():
        w = re.sub(r"[^a-z0-9]", "", raw.lower())
        if w:
            out.append(w)
    return out


def _align_to_captions(
    script: VideoScript,
    captions: list[Caption],
    total_frames: int,
    fps: int,
    start_frame: int,
) -> list[tuple[int, int]] | None:
    """Place each scene boundary at the moment its cue is actually spoken.

    Proportional allocation by word count is a decent guess, but words are not
    spoken at a uniform rate -- "ArgoCD" takes far longer than "on top" -- so
    scenes drift a few tenths of a second out of step with the narration. We
    already have exact per-word timings from Whisper, so use them: find where
    each cue's spoken_excerpt begins in the transcript and cut there.

    Returns None if the transcript can't be matched, so the caller can fall
    back to proportional allocation.
    """
    cues = script.visual_cues
    if not cues or not captions:
        return None

    cap_words = [(_norm_words(c.text), c.startMs) for c in captions]
    flat: list[tuple[str, float]] = [
        (w, ms) for words, ms in cap_words for w in words
    ]
    if not flat:
        return None

    # Where in the transcript does each cue (after the first) begin?
    boundaries_ms: list[float] = []
    cursor = 0
    for cue in cues[1:]:
        probe = _norm_words(cue.spoken_excerpt)[:4]
        if len(probe) < 2:
            return None  # too little to match on; not worth guessing

        found = -1
        for i in range(cursor, len(flat) - len(probe) + 1):
            if [w for w, _ in flat[i : i + len(probe)]] == probe:
                found = i
                break
        if found < 0:
            return None  # transcript diverged from the script; fall back
        boundaries_ms.append(flat[found][1])
        cursor = found + 1

    # Convert to frames and enforce the minimum scene length.
    min_frames = int(MIN_SCENE_SECONDS * fps)
    end_frame = start_frame + total_frames
    # Audio starts at frame 0, so a caption timestamp maps straight to a frame.
    # (The screenshot intro overlaps the first seconds of narration by design,
    # which is why the first cue can start later than its excerpt is spoken.)
    starts = [start_frame]
    for ms in boundaries_ms:
        f = int(round(ms / 1000 * fps))
        # Monotonic, and never so tight that a scene is unreadable.
        starts.append(max(f, starts[-1] + min_frames))

    if starts[-1] + min_frames > end_frame:
        return None  # squeezed past the end; proportional handles this better

    return [
        (s, (starts[i + 1] if i + 1 < len(starts) else end_frame) - s)
        for i, s in enumerate(starts)
    ]


def _allocate_frames(
    script: VideoScript, total_frames: int, fps: int, start_frame: int = 0
) -> list[tuple[int, int]]:
    """Split the timeline across cues, weighted by spoken word count.

    Returns [(from_frame, duration_frames)] aligned with script.visual_cues.
    `total_frames` is the span available to the cues, and `start_frame` is
    where that span begins -- the screenshot intro occupies everything before.
    """
    cues = script.visual_cues
    if not cues:
        return []

    # Weight by the words spoken during each cue. Cues with no excerpt still
    # get a floor weight so they don't collapse to zero frames.
    weights = [max(len(c.spoken_excerpt.split()), 1) for c in cues]
    total_weight = sum(weights)

    min_frames = int(MIN_SCENE_SECONDS * fps)
    # If honouring the minimum would overflow the timeline, drop the minimum
    # and let everything scale down -- a too-short scene beats a video that
    # outruns its own audio.
    if min_frames * len(cues) > total_frames:
        min_frames = total_frames // len(cues)

    raw = [total_frames * w / total_weight for w in weights]
    durations = [max(int(r), min_frames) for r in raw]

    # Correct the rounding drift onto the longest scene, so the sum lands
    # exactly on total_frames.
    drift = total_frames - sum(durations)
    if drift:
        longest = max(range(len(durations)), key=lambda i: durations[i])
        durations[longest] = max(durations[longest] + drift, min_frames)

    out: list[tuple[int, int]] = []
    cursor = start_frame
    for d in durations:
        out.append((cursor, d))
        cursor += d
    return out


def build_spec(
    repo: RepoCandidate,
    script: VideoScript,
    captions: list[Caption],
    audio_seconds: float,
    audio_src: str,
    cfg: Settings,
    on: date | None = None,
    screenshot_src: str | None = None,
) -> VideoSpec:
    fps = cfg.fps
    # A short tail so the last word isn't clipped and the outro can breathe.
    total_frames = int(round((audio_seconds + 0.9) * fps))

    scenes: list[Scene] = []
    intro_frames = 0

    if screenshot_src:
        # The real README hero opens the video, under the hook overlay. Never
        # let it eat more than a third of the timeline on a very short script.
        intro_frames = min(int(INTRO_SECONDS * fps), total_frames // 3)
        scenes.append(
            Scene(
                kind=CueKind.SCREENSHOT,
                fromFrame=0,
                durationInFrames=intro_frames,
                imageSrc=screenshot_src,
            )
        )
        # The README hero already shows the repo name, description, language
        # and license, so a repo_card immediately after it is the same
        # information twice in a row. Drop it and give the time back.
        if script.visual_cues and script.visual_cues[0].kind == CueKind.REPO_CARD:
            script = script.model_copy(update={"visual_cues": script.visual_cues[1:]})
            log.info("Dropped leading repo_card: the README hero already covers it.")

    # Prefer real spoken timings; fall back to proportional word-count split if
    # the transcript can't be matched to the script (heavy mis-transcription,
    # or cues whose excerpts don't appear verbatim).
    allocations = _align_to_captions(
        script, captions, total_frames - intro_frames, fps, intro_frames
    )
    if allocations is None:
        log.info("Caption alignment unavailable; using proportional scene timing.")
        allocations = _allocate_frames(
            script, total_frames - intro_frames, fps, start_frame=intro_frames
        )
    else:
        log.info("Scenes aligned to spoken word timings.")

    # A static hold is where a viewer scrolls, so say when one got through.
    overlong = [
        round(d / fps, 1) for _, d in allocations if d > MAX_SCENE_SECONDS * fps
    ]
    if overlong:
        log.warning(
            "Scene(s) holding %s seconds, over the %.1fs guideline. The script "
            "gave too few cues for its length; more cues means more cuts.",
            ", ".join(str(x) for x in overlong), MAX_SCENE_SECONDS,
        )

    for cue, (from_frame, duration) in zip(script.visual_cues, allocations, strict=True):
        scenes.append(
            Scene(
                kind=cue.kind,
                fromFrame=from_frame,
                durationInFrames=duration,
                title=cue.title,
                subtitle=cue.subtitle,
                bullets=cue.bullets,
                code=cue.code,
                codeLanguage=cue.code_language,
                statValue=cue.stat_value,
                statLabel=cue.stat_label,
            )
        )

    spec = VideoSpec(
        slug=repo.slug,
        createdOn=on or date.today(),
        width=cfg.width,
        height=cfg.height,
        fps=fps,
        durationInFrames=total_frames,
        hook=script.hook,
        audioSrc=audio_src,
        repo=RepoMeta(
            fullName=repo.full_name,
            owner=repo.owner,
            name=repo.name,
            stars=repo.stars,
            starsGainedToday=repo.stars_gained_today,
            language=repo.language,
            license=repo.license_spdx,
            url=repo.url,
        ),
        scenes=scenes,
        captions=captions,
    )

    log.info(
        "Spec: %d frames (%.1fs @ %dfps), %d scenes, %d caption words",
        total_frames, total_frames / fps, fps, len(scenes), len(captions),
    )
    return spec
