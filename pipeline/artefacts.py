"""Stage a subject's pictures into the renderer's public directory.

The second niche's counterpart to `capture_repo`. Account 1's shot is a
screenshot this pipeline takes; here the shot is somebody else's scan of a four
hundred year old page, so staging is a download plus the licence check that
decides whether it may be used at all.

**Four artefacts, not one.** The first prototype had a single page in it, which
meant every shot could only be a pan or a push, and sixteen shots of one page
reads as a slideshow. A face, a second leaf with different annotation and a
third give the edit something to cut between. That is why `want` defaults to
four and why fewer is a warning rather than a quiet success.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict
from pathlib import Path

import httpx

from pipeline.models import SubjectCandidate
from sources import wikimedia as wm

log = logging.getLogger(__name__)

MIN_ARTEFACTS = 2
_TIMEOUT = 60.0


_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "on", "in", "and", "or", "at", "by", "for",
        "to", "from", "his", "her", "its", "with", "london", "paris",
    }
)


def rank_by_relevance(candidates: list[wm.Artefact], prefer: str) -> list[wm.Artefact]:
    """Put the pictures the script is actually about first.

    **This is the open problem, answered as far as a filename can answer it.**
    Commons hands back everything in a subject's category, which for a printer
    is his maps, his coats of arms and a plaque on a wall, and the first live
    render opened on a map of Canaan while the voice talked about a mould for
    casting letters. The script names its primary source, so a title sharing
    words with it is the closest thing to evidence that the picture is the
    thing being discussed.

    It is a heuristic and it is meant to be looked at. A subject whose source
    is named nowhere in Commons falls back to the order the category gave,
    which is what happened before this existed.
    """
    wanted = {w for w in _tokens(prefer) if w not in _STOPWORDS}
    if not wanted:
        return candidates

    def overlap(artefact: wm.Artefact) -> int:
        return len(wanted & set(_tokens(artefact.title)))

    return sorted(candidates, key=overlap, reverse=True)


def _tokens(text: str) -> set[str]:
    return {"".join(c for c in word if c.isalnum()).lower() for word in text.split()}


def stage(
    subject: SubjectCandidate,
    video_dir: Path,
    *,
    run_key: str,
    keep_dir: Path | None = None,
    want: int = 4,
    prefer: str = "",
    client: httpx.Client | None = None,
) -> list[dict]:
    """Download the usable artefacts and return what the spec needs about them.

    Written as `art<N>.jpg` into the run's staging directory, `run_key` from
    `renderer.staging_key`, and also into `keep_dir` when one is given. That
    second copy is what lets a resumed episode restage its pictures without
    asking Commons again: staging is deleted when a run finishes, and the
    ranking that chose these files may choose different ones next time.

    The width and height travel with the filename for `pageAspect`'s reason:
    the renderer needs an image's shape to know how far it may pan, and
    measuring an image inside a frame render is how one frame ends up different
    from its neighbour for no visible reason.
    """
    public = video_dir / "public" / run_key
    public.mkdir(parents=True, exist_ok=True)
    if keep_dir is not None:
        keep_dir.mkdir(parents=True, exist_ok=True)

    candidates = wm.artefacts(subject.artefacts or [subject.portrait], client=client)
    usable = rank_by_relevance([a for a in candidates if a.usable], prefer)[:want]
    if len(usable) < MIN_ARTEFACTS:
        log.warning(
            "%s has %d usable artefacts; every shot will be a pan on the same picture",
            subject.name, len(usable),
        )

    staged: list[dict] = []
    for i, artefact in enumerate(usable):
        suffix = ".png" if artefact.url.lower().endswith(".png") else ".jpg"
        name = f"art{i}{suffix}"
        try:
            payload = _download(artefact.url, client)
        except httpx.HTTPError as exc:
            # Best effort per file. A missing artefact costs the edit a cut;
            # failing the run costs a script that has already been paid for.
            log.warning("Could not fetch %s: %s", artefact.title, exc)
            continue
        (public / name).write_bytes(payload)
        if keep_dir is not None:
            (keep_dir / name).write_bytes(payload)
        staged.append(
            {
                "src": f"{run_key}/{name}",
                "w": artefact.width,
                "h": artefact.height,
                "title": artefact.title,
                "licence": artefact.licence,
                "credit": artefact.credit,
            }
        )
        log.info("Staged %s (%.1f MB)", name, len(payload) / 1_048_576)

    return staged


def restage(
    staged: list[dict], video_dir: Path, *, run_key: str, keep_dir: Path
) -> list[dict] | None:
    """Put a resumed episode's pictures back into staging, or None if it cannot.

    `artefacts.json` outlives the staging directory it describes: a finished
    run deletes its staging, and a run folder written before per run staging
    names flat files another run's prune may already have removed. Rendering
    from that list without checking is how an episode renders with missing
    images. So each file is copied back from `keep_dir`, and the `src` rewritten
    to this run's staging path.

    None means some file has no kept copy, which is every folder from before
    copies were kept. The caller stages afresh rather than rendering a spec
    with holes in it.
    """
    dest = video_dir / "public" / run_key
    restaged: list[dict] = []
    for entry in staged:
        name = Path(entry["src"]).name
        target = dest / name
        if not target.exists():
            kept = keep_dir / name
            if not kept.exists():
                return None
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kept, target)
        restaged.append({**entry, "src": f"{run_key}/{name}"})
    return restaged


def _download(url: str, client: httpx.Client | None) -> bytes:
    if client is not None:
        response = client.get(url, headers={"User-Agent": wm.USER_AGENT})
    else:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as http:
            response = http.get(url, headers={"User-Agent": wm.USER_AGENT})
    response.raise_for_status()
    return response.content


def describe(artefact: wm.Artefact) -> dict:
    """A Commons artefact as plain data, for a spec or a receipt."""
    return asdict(artefact)
