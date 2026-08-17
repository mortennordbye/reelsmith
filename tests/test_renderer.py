"""The version of a video that carries no ask.

The ask is Instagram's word: "Follow for a new one every night" points a YouTube
viewer at a button that surface calls subscribing. It is spoken, captioned and
shown on a chip that runs from halfway to the last frame, so this is a second
render rather than a cut: pixels present from the middle of a file cannot be
absent from a truncation of it.

What is tested here is the scene surgery, because that is the part with a
judgement in it. The render itself is Remotion's job.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline import renderer
from pipeline.models import CueKind, RepoMeta, Scene, VideoSpec

# --- The version without the ask --------------------------------------------


def _spec(**over):
    """A spec shaped like a real one: hero at the front, content, ask at the end."""
    fps = 30
    base = dict(
        slug="a-b",
        createdOn=date(2026, 8, 16),
        fps=fps,
        durationInFrames=811,
        hook="A hook",
        audioSrc="a-b-voice.wav",
        repo=RepoMeta(fullName="a/b", owner="a", name="b", stars=1, url="https://x.test"),
        scenes=[
            Scene(kind=CueKind.SCREENSHOT, fromFrame=0, durationInFrames=120,
                  imageSrc="a-b-repo.png"),
            Scene(kind=CueKind.STAT, fromFrame=120, durationInFrames=529),
            Scene(kind=CueKind.BULLETS, fromFrame=649, durationInFrames=78),
            Scene(kind=CueKind.SCREENSHOT, fromFrame=727, durationInFrames=84,
                  imageSrc="a-b-repo.png"),
        ],
        captions=[],
        showFollowCta=True,
        ctaFromFrame=727,
    )
    base.update(over)
    return VideoSpec(**base)


def test_the_youtube_cut_ends_on_the_hero():
    """Otherwise the best looking asset in the video appears only in the part
    that was removed, and the Short ends on whichever cue happened to be last."""
    scenes = renderer._scenes_ending_on_hero(_spec())

    last = scenes[-1]
    assert last.kind is CueKind.SCREENSHOT
    assert last.imageSrc == "a-b-repo.png"
    assert last.fromFrame + last.durationInFrames == 727, "ends exactly at the ask"
    assert scenes[-2].fromFrame + scenes[-2].durationInFrames == last.fromFrame


def test_the_youtube_cut_never_runs_past_the_ask():
    for scene in renderer._scenes_ending_on_hero(_spec()):
        assert scene.fromFrame + scene.durationInFrames <= 727


def test_a_last_scene_with_no_room_keeps_its_content():
    """The hero has to come out of the closing content scene, so a short one
    keeps what it has rather than being cut to a sliver for a closing hold."""
    spec = _spec(
        scenes=[
            Scene(kind=CueKind.SCREENSHOT, fromFrame=0, durationInFrames=120,
                  imageSrc="a-b-repo.png"),
            Scene(kind=CueKind.STAT, fromFrame=120, durationInFrames=577),
            Scene(kind=CueKind.BULLETS, fromFrame=697, durationInFrames=30),
            Scene(kind=CueKind.SCREENSHOT, fromFrame=727, durationInFrames=84,
                  imageSrc="a-b-repo.png"),
        ]
    )

    scenes = renderer._scenes_ending_on_hero(spec)

    assert scenes[-1].kind is CueKind.BULLETS
    assert scenes[-1].fromFrame + scenes[-1].durationInFrames == 727


def test_no_hero_image_means_a_plain_truncation():
    spec = _spec(
        scenes=[
            Scene(kind=CueKind.STAT, fromFrame=0, durationInFrames=649),
            Scene(kind=CueKind.BULLETS, fromFrame=649, durationInFrames=78),
            Scene(kind=CueKind.STAT, fromFrame=727, durationInFrames=84),
        ]
    )

    scenes = renderer._scenes_ending_on_hero(spec)

    assert [s.kind for s in scenes] == [CueKind.STAT, CueKind.BULLETS]
    assert scenes[-1].fromFrame + scenes[-1].durationInFrames == 727


def test_no_second_render_when_the_video_carries_no_ask():
    """The cut exists only to remove the ask, so with no ask there is nothing to
    remove and the caller falls back to the full render.

    Worth pinning: the flag was `ctaKeyword` and pydantic ignores unknown
    fields, so a fixture left on the old name sets nothing, `showFollowCta`
    defaults to False, and every test of this path passes while exercising none
    of it.
    """
    assert renderer.render_without_cta(_spec(showFollowCta=False), Path("/nope"), None) is None
