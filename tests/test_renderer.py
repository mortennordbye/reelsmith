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
from types import SimpleNamespace

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


# --- Restaging, which is what makes the second render possible --------------


def _staged(tmp_path):
    """A run folder holding the real files, and an empty public/.

    The state after a batch: this video rendered, then the next one pruned
    everything that was not its own on its way in.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "voice.wav").write_text("audio")
    (run_dir / "repo.png").write_text("image")
    video_dir = tmp_path / "video"
    (video_dir / "public").mkdir(parents=True)
    return run_dir, SimpleNamespace(video_dir=video_dir)


def test_a_specs_assets_are_put_back_before_it_renders_again(tmp_path):
    """`main.py` prunes every other slug on its way in, so staging survives
    until the next video renders. `render_without_cta` runs after the whole
    batch, which is exactly when it is gone."""
    run_dir, cfg = _staged(tmp_path)

    assert renderer.restage_spec_assets(_spec(), run_dir, cfg) == 2
    assert {p.name for p in (cfg.video_dir / "public").iterdir()} == {
        "a-b-voice.wav", "a-b-repo.png",
    }


def test_restaging_covers_everything_the_spec_asks_remotion_for(tmp_path):
    """Missing one is not a smaller version of the bug. A spec whose audio is
    absent does not render at all, and one whose screenshot is absent renders
    the Short without the asset the format is built around."""
    run_dir, cfg = _staged(tmp_path)
    spec = _spec()

    renderer.restage_spec_assets(spec, run_dir, cfg)

    wanted = {spec.audioSrc} | {s.imageSrc for s in spec.scenes if s.imageSrc}
    for name in wanted:
        assert (cfg.video_dir / "public" / name).is_file()


def test_an_already_staged_asset_is_not_copied_again(tmp_path):
    run_dir, cfg = _staged(tmp_path)
    (cfg.video_dir / "public" / "a-b-voice.wav").write_text("already here")

    assert renderer.restage_spec_assets(_spec(), run_dir, cfg) == 1
    assert (cfg.video_dir / "public" / "a-b-voice.wav").read_text() == "already here"


def test_a_source_that_is_gone_is_skipped_rather_than_raising(tmp_path):
    """The run folder is the only place a staged copy can come from. When it no
    longer holds the file either, the render fails the way it did before this
    existed, which is the honest answer."""
    run_dir, cfg = _staged(tmp_path)
    (run_dir / "repo.png").unlink()

    assert renderer.restage_spec_assets(_spec(), run_dir, cfg) == 1
