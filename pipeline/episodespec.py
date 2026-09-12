"""Turn an episode, a voice track and some scans into a render spec.

`pipeline/spec.py` does this for account 1 and this is deliberately a second
file rather than a branch inside it: the two specs share no field that means
the same thing, and one builder serving both would be a function whose every
line is an if.

**The cut lands where the line ends.** Shot boundaries come from what the voice
actually produced, never from a plan, which is the arithmetic the prototypes
did by hand five times while the format was being found.

**Every shot is a cut, not a pan.** One artefact means every shot can only be a
zoom, and sixteen zooms on one page reads as a slideshow. So consecutive shots
change artefact where there is one to change to, and where there is not they
change framing hard enough to read as a cut: the crops step through three
scales and three positions rather than drifting between them.
"""

from __future__ import annotations

import logging
from datetime import date

from config import Settings
from pipeline.models import Crop, EpisodeScript, EpisodeSpec, Shot, SpecArtefact, SubjectCandidate

log = logging.getLogger(__name__)

# How long the end card holds after the last line. Two and a half seconds is
# what the prototypes settled on: long enough to read three short lines, short
# enough that nobody watches a still.
ENDCARD_FRAMES = 75

# The crop ladder. Each entry is a fraction of the artefact's shorter side and
# a vertical anchor from 0 (top) to 1 (bottom). Stepping through it is what
# makes two shots on one picture read as two shots.
_LADDER = ((1.0, 0.5), (0.62, 0.18), (0.78, 0.82), (0.5, 0.5), (0.7, 0.35), (0.55, 0.7))


def _crop(artefact: SpecArtefact, index: int, aspect: float) -> Crop:
    """A rectangle of the frame's aspect inside the artefact, chosen by index.

    Deterministic rather than random, so re-rendering a spec produces the same
    video. Random framing would also be the thing that makes a generated
    channel look generated: nobody frames a shot by rolling dice.

    **A landscape scan is measured across, not down.** Filling a 9:16 frame
    from the full height of a two page spread shows about a fifth of its width,
    which the first live render did: the title page of Moxon's manual came out
    as a vertical slice with three letters of its own title in it. So the
    ladder scales the long side, and the long side of a page photographed open
    is its width.
    """
    scale, anchor = _LADDER[index % len(_LADDER)]
    landscape = artefact.w >= artefact.h

    if landscape:
        width = max(int(artefact.w * scale), 1)
        height = max(int(width / aspect), 1)
        if height > artefact.h:
            height = artefact.h
            width = max(int(height * aspect), 1)
    else:
        height = max(int(artefact.h * scale), 1)
        width = max(int(height * aspect), 1)
        if width > artefact.w:
            width = artefact.w
            height = max(int(width / aspect), 1)

    sx = max((artefact.w - width) // 2, 0)
    sy = max(min(int((artefact.h - height) * anchor), artefact.h - height), 0)
    return Crop(sx=sx, sy=sy, sw=width, sh=height)


def build(
    script: EpisodeScript,
    subject: SubjectCandidate,
    artefacts: list[dict],
    timing: dict,
    cfg: Settings,
    *,
    audio_src: str,
) -> EpisodeSpec:
    """The spec, from the pieces every earlier stage left in the run folder.

    `audio_src` is the voice's path under `video/public/`, as
    `renderer.stage_asset` returned it, because staging is per run and only
    the stage that copied the file knows where it went.
    """
    staged = [SpecArtefact.model_validate(a) for a in artefacts]
    if not staged:
        raise ValueError(
            "No artefacts staged. The format's first rule is a real picture per "
            "episode, so a spec with none is not a video worth rendering."
        )

    beats = script.beats
    marks = timing["lines"]
    if len(marks) != len(beats):
        raise ValueError(
            f"{len(marks)} spoken lines against {len(beats)} written ones. "
            "The voice and the script disagree, which means one of them is stale."
        )

    fps = int(timing.get("fps", 30))
    spoken_frames = int(timing["durationInFrames"])
    aspect = 1080 / 1920

    shots: list[Shot] = []
    art_index, last_kind, step_number = 0, "", 0
    established: set[int] = set()
    for i, ((kind, line), mark) in enumerate(zip(beats, marks, strict=True)):
        start = int(mark["from"])
        end = int(marks[i + 1]["from"]) if i + 1 < len(marks) else spoken_frames
        if kind != last_kind and i:
            # A new beat is a new picture wherever there is one to move to.
            art_index = (art_index + 1) % len(staged)
        if kind == "step":
            step_number += 1

        # **Establish, then detail.** Any 9:16 crop of a page photographed open
        # is a narrow column of it, which is a fine close up and a terrible
        # first look. So the first shot on a landscape artefact shows the whole
        # of it and the shots after it are details, which is the order a person
        # cutting this would use and is also what stops two artefacts reading
        # as one long pan.
        artefact = staged[art_index]
        landscape = artefact.w >= artefact.h
        fit = "contain" if landscape and art_index not in established else "cover"
        established.add(art_index)
        shots.append(
            Shot(
                start=start,
                durationInFrames=max(end - start, 1),
                line=line,
                kind=kind,
                art=art_index,
                crop=_crop(artefact, i, aspect),
                fit=fit,
                step=step_number if kind == "step" else None,
            )
        )
        last_kind = kind

    return EpisodeSpec(
        slug=subject.slug,
        createdOn=date.today(),
        fps=fps,
        durationInFrames=spoken_frames + ENDCARD_FRAMES,
        hook=script.hook,
        audioSrc=audio_src,
        subject=subject.name,
        lived=_lived(subject),
        source=script.source,
        artefacts=staged,
        shots=shots,
        endcardName=cfg.endcard_name,
        endcardHandle=cfg.endcard_handle,
        endcardTagline=cfg.endcard_tagline,
    )


def _lived(subject: SubjectCandidate) -> str:
    """The dates, spelled without a dash, because the shared rules ban one."""
    if subject.born is None and subject.died is None:
        return ""
    born = subject.born if subject.born is not None else "unknown"
    died = subject.died if subject.died is not None else "unknown"
    return f"{born} to {died}"
