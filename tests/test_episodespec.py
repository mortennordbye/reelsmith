"""The spec that turns an episode, a voice track and some scans into shots.

Every case here is a defect the first live render actually produced. The
pipeline ran end to end before any of them were true, which is the point: a
render that finishes is not a render worth posting, and none of these would
have failed a test that only asked whether an mp4 appeared.
"""

from __future__ import annotations

import pytest

from config import Settings
from pipeline import artefacts, episodespec
from pipeline.models import EpisodeScript, SubjectCandidate
from sources import wikimedia as wm

SCRIPT = EpisodeScript(
    hook="You fixed it six times and it is still wrong",
    situation="You have fixed the same thing six times.",
    who="Joseph Moxon cut type in London.",
    quote="For he does not expect to do it the First time.",
    plain="Seven goes is not failure.",
    did="He measured twenty samples against a pattern.",
    back="Stop judging your seventh try against the first.",
    steps=["Find one known good example", "Mend one thing", "Delete the old result"],
    source="Moxon, Mechanick Exercises, London 1683.",
)

SUBJECT = SubjectCandidate(
    qid="Q3057690", name="Joseph Moxon", article="Joseph Moxon", born=1627, died=1691
)

WIDE = {"src": "a.jpg", "w": 2000, "h": 1342, "title": "Mechanick Exercises"}
TALL = {"src": "b.jpg", "w": 1200, "h": 2400, "title": "A portrait"}
VOICE = "staged/secondaccount/joseph-moxon/voice.wav"


@pytest.fixture
def cfg() -> Settings:
    return Settings(account="secondaccount", _env_file=None)


def build(artefacts_: list[dict], timing_: dict, cfg: Settings):
    return episodespec.build(SCRIPT, SUBJECT, artefacts_, timing_, cfg, audio_src=VOICE)


def timing(lines: int, *, fps: int = 30, each: int = 60) -> dict:
    return {
        "fps": fps,
        "seconds": lines * each / fps,
        "durationInFrames": lines * each,
        "lines": [{"i": i, "from": i * each, "to": i * each + each - 2} for i in range(lines)],
    }


def test_the_cut_lands_where_the_line_ends(cfg):
    """Shot boundaries come from what the voice produced, never from a plan."""
    spec = build([WIDE, TALL], timing(len(SCRIPT.lines)), cfg)

    assert [shot.start for shot in spec.shots] == [i * 60 for i in range(len(SCRIPT.lines))]
    assert all(shot.durationInFrames == 60 for shot in spec.shots)


def test_a_voice_that_disagrees_with_the_script_is_a_failure(cfg):
    """One of the two is stale, and rendering either way produces a video whose
    captions do not match its audio."""
    with pytest.raises(ValueError, match="disagree"):
        build([WIDE], timing(3), cfg)


def test_a_crop_stays_inside_its_artefact(cfg):
    """A rectangle that runs off the edge is a frame with a strip of nothing in
    it, and nothing downstream would notice."""
    spec = build([WIDE, TALL], timing(len(SCRIPT.lines)), cfg)

    for shot in spec.shots:
        art = spec.artefacts[shot.art]
        assert shot.crop is not None
        assert shot.crop.sx + shot.crop.sw <= art.w
        assert shot.crop.sy + shot.crop.sh <= art.h


def test_a_landscape_page_is_established_before_it_is_detailed(cfg):
    """Any 9:16 crop of a page photographed open is a narrow column of it,
    which is a fine close up and a terrible first look. The first live render
    opened on one: the title page of Moxon's manual came out as a vertical
    slice with three letters of its own title in it."""
    spec = build([WIDE], timing(len(SCRIPT.lines)), cfg)
    on_wide = [shot for shot in spec.shots if spec.artefacts[shot.art].src == WIDE["src"]]

    assert on_wide[0].fit == "contain"
    assert all(shot.fit == "cover" for shot in on_wide[1:])


def test_the_opening_shot_shows_the_whole_artefact(cfg):
    """The first thing the format promises is the artefact itself, and a
    cropped one is a detail of something the viewer has not been shown yet."""
    spec = build([WIDE], timing(len(SCRIPT.lines)), cfg)

    assert spec.shots[0].fit == "contain"


def test_a_new_beat_moves_to_another_picture(cfg):
    """One artefact means every shot can only be a zoom, and sixteen zooms on
    one page reads as a slideshow."""
    spec = build([WIDE, TALL], timing(len(SCRIPT.lines)), cfg)
    quote_shot = next(shot for shot in spec.shots if shot.kind == "quote")
    who_shot = next(shot for shot in spec.shots if shot.kind == "who")

    assert quote_shot.art != who_shot.art


def test_the_steps_are_numbered_in_order(cfg):
    spec = build([WIDE], timing(len(SCRIPT.lines)), cfg)

    assert [shot.step for shot in spec.shots if shot.kind == "step"] == [1, 2, 3]


def test_a_spec_with_no_artefacts_is_refused(cfg):
    """The format's first rule is a real picture per episode."""
    with pytest.raises(ValueError, match="No artefacts"):
        build([], timing(len(SCRIPT.lines)), cfg)


def test_the_dates_carry_no_dash(cfg):
    """The shared text rules ban one, and this reaches the screen."""
    spec = build([WIDE], timing(len(SCRIPT.lines)), cfg)

    assert spec.lived == "1627 to 1691"


def test_the_pictures_the_script_is_about_come_first():
    """Commons hands back everything in a subject's category, which for a
    printer is his maps and a plaque on a wall. The first live render opened on
    a map of Canaan while the voice talked about a mould for casting letters."""
    files = [
        wm.Artefact(title="File:Canaan or the Land of Promise", url="u", width=2000, height=1500),
        wm.Artefact(
            title="File:Mechanick Exercises by Joseph Moxon", url="u", width=2000, height=1342
        ),
    ]

    ranked = artefacts.rank_by_relevance(files, "Moxon, Mechanick Exercises, London 1683.")

    assert ranked[0].title.startswith("File:Mechanick")


def test_a_resumed_episode_puts_its_pictures_back_from_the_run_folder(tmp_path):
    """Staging is deleted when a run finishes, and `artefacts.json` outlives
    it. Rendering from the list without checking is an episode with holes."""
    keep = tmp_path / "run" / "artefacts"
    keep.mkdir(parents=True)
    (keep / "art0.jpg").write_bytes(b"scan")
    video = tmp_path / "video"

    restaged = artefacts.restage(
        [{"src": "staged/old/moxon/art0.jpg", "w": 2000, "h": 1342}],
        video, run_key="staged/acct/moxon", keep_dir=keep,
    )

    assert restaged == [{"src": "staged/acct/moxon/art0.jpg", "w": 2000, "h": 1342}]
    assert (video / "public" / "staged/acct/moxon/art0.jpg").read_bytes() == b"scan"


def test_a_folder_from_before_kept_copies_asks_to_stage_again(tmp_path):
    """Flat names from the old layout have no kept copy. None, so the caller
    downloads again rather than rendering a spec with a missing picture."""
    restaged = artefacts.restage(
        [{"src": "joseph-moxon-art0.jpg", "w": 2000, "h": 1342}],
        tmp_path / "video", run_key="staged/acct/joseph-moxon", keep_dir=tmp_path / "none",
    )

    assert restaged is None


def test_relevance_falls_back_to_the_order_it_was_given():
    """A subject whose source is named nowhere in Commons behaves the way it
    did before ranking existed."""
    files = [
        wm.Artefact(title="File:One", url="u", width=2000, height=1500),
        wm.Artefact(title="File:Two", url="u", width=2000, height=1500),
    ]

    assert artefacts.rank_by_relevance(files, "") == files
