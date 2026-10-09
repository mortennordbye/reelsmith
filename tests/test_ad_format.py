"""The ad format: one device per beat, the README first, a fallback to classic.

It runs armed on the nightly, so nothing reviews a video before it posts. The
tests here pin the three things that make that safe: an overfull cue goes back
to the model instead of rendering off the frame, every device lands on the
words it illustrates, and a failed ad render still produces a video.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from conftest import candidate, captions_from
from pydantic import ValidationError

from config import Settings
from pipeline import renderer, screenshot
from pipeline.models import CueKind, ReadmeSection, VideoScript
from pipeline.scriptwriter import _build_prompt, prompt_source
from pipeline.spec import AD_INTRO_MIN_SECONDS, build_spec, to_classic

FPS = 30


def ad_script(*cues: dict, hook: str = "A hook") -> VideoScript:
    return VideoScript(
        hook=hook,
        spoken_script=" ".join(c.get("spoken_excerpt", "") for c in cues),
        visual_cues=list(cues),
    )


def cfg(fmt: str = "ad") -> Settings:
    return Settings(github_token="x", reel_format=fmt, _env_file=None)


# --------------------------------------------------------------------------
# What a cue may hold
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cue", "message"),
    [
        ({"kind": "poster", "bullets": ["ONE"]}, "2 to 4 bullets"),
        ({"kind": "poster", "bullets": ["fine", "overlylongword"]}, "at most 11"),
        ({"kind": "poster", "bullets": ["two words", "fine"]}, "one word"),
        ({"kind": "statement", "title": "x" * 40}, "1 to 32"),
        ({"kind": "verdict", "items": [{"label": "a", "ok": True}, {"label": "b"}]}, "ok set"),
        ({"kind": "bars", "items": [{"label": "a", "value": 1}, {"label": "b"}]}, "positive value"),
        ({"kind": "command", "code": "one\ntwo"}, "one line"),
        ({"kind": "readme"}, "readme_text"),
        ({"kind": "compare", "items": [{"label": "x" * 30}, {"label": "b"}]}, "too long"),
    ],
)
def test_an_overfull_cue_is_refused_with_the_limit(cue, message):
    # Refused rather than trimmed: the refusal goes back to the model with the
    # number, and a trimmed cue would say something the script did not.
    with pytest.raises(ValidationError, match=message):
        ad_script(cue)


def test_a_cue_inside_every_limit_is_accepted():
    s = ad_script(
        {"kind": "poster", "bullets": ["IDEA", "CODE", "REVIEW"]},
        {"kind": "verdict",
         "items": [{"label": "Office", "ok": True}, {"label": "PDF", "ok": False}]},
        {"kind": "bars", "items": [{"label": "a", "value": 1}, {"label": "b", "value": 10}]},
        {"kind": "command", "code": "markitdown report.pdf -o report.md"},
        {"kind": "readme", "readme_text": "PowerPoint"},
    )
    assert [c.kind for c in s.visual_cues] == [
        CueKind.POSTER, CueKind.VERDICT, CueKind.BARS, CueKind.COMMAND, CueKind.README,
    ]


# --------------------------------------------------------------------------
# The opening and the timeline
# --------------------------------------------------------------------------


def _spec(fmt: str = "ad", sections=None):
    s = ad_script(
        {"kind": "repo_card", "spoken_excerpt": "Feed a two column paper in and it breaks."},
        {"kind": "statement", "spoken_excerpt": "That is MarkItDown.", "title": "MarkItDown."},
        {"kind": "readme", "spoken_excerpt": "It turns Word and Excel into Markdown.",
         "readme_text": "PowerPoint"},
    )
    caps = captions_from(
        "Feed a two column paper in and it breaks. That is MarkItDown. "
        "It turns Word and Excel into Markdown."
    )
    return build_spec(
        candidate("microsoft/markitdown"), s, caps, 20.0, "voice.wav", cfg(fmt),
        screenshot_src="hero.png", sections=sections,
    )


def test_the_intro_ends_where_the_second_beat_is_spoken():
    # The first beat plays under the hero and hook. A fixed four second intro
    # ended mid sentence and pushed every later scene back by the minimum
    # scene length, a second and a half behind the voice in the first render.
    spec = _spec()
    # 500ms a word: "That" is word 9, at frame 135. "It" is word 12, at 180,
    # but a three word statement is shorter than the minimum scene, so the
    # readme waits for 135 + 54. That is the floor working, not a drift.
    assert [(s.kind, s.fromFrame) for s in spec.scenes[:3]] == [
        (CueKind.SCREENSHOT, 0), (CueKind.STATEMENT, 135), (CueKind.README, 189)
    ]
    assert spec.scenes[0].durationInFrames == 135


def test_the_intro_is_never_shorter_than_the_hook_needs():
    s = ad_script(
        {"kind": "repo_card", "spoken_excerpt": "Short."},
        {"kind": "statement", "spoken_excerpt": "That is it.", "title": "It."},
    )
    spec = build_spec(
        candidate("a/b"), s, captions_from("Short. That is it."), 6.0, "v.wav", cfg(),
        screenshot_src="hero.png",
    )
    assert spec.scenes[0].durationInFrames == int(AD_INTRO_MIN_SECONDS * FPS)


def test_the_classic_format_keeps_its_fixed_intro():
    spec = _spec("classic")
    assert spec.format == "classic"
    assert spec.scenes[0].durationInFrames == 120


def test_a_found_section_rides_on_its_scene_and_a_missing_one_does_not():
    section = ReadmeSection(src="s.png", w=100, h=50)
    found = _spec(sections={"PowerPoint": section})
    missing = _spec(sections={})
    assert found.scenes[2].section == section
    assert missing.scenes[2].section is None
    assert missing.scenes[2].kind is CueKind.README


def test_the_spec_says_which_composition_and_carries_the_end_card():
    spec = build_spec(
        candidate("a/b"),
        ad_script({"kind": "statement", "spoken_excerpt": "One two.", "title": "One."}),
        captions_from("One two."), 3.0, "v.wav",
        Settings(github_token="x", endcard_handle="@h", _env_file=None),
    )
    assert (spec.format, spec.endcardHandle) == ("ad", "@h")


# --------------------------------------------------------------------------
# The fallback
# --------------------------------------------------------------------------


def test_the_classic_fallback_maps_every_ad_device_to_a_classic_kind():
    s = ad_script(
        {"kind": "poster", "spoken_excerpt": "a b", "bullets": ["A", "B"]},
        {"kind": "verdict", "spoken_excerpt": "c d",
         "items": [{"label": "x", "ok": True}, {"label": "y", "ok": False}]},
        {"kind": "command", "spoken_excerpt": "e f", "code": "npx x"},
        {"kind": "files", "spoken_excerpt": "g h", "items": [{"label": "a/"}, {"label": "b/"}]},
        {"kind": "readme", "spoken_excerpt": "i j", "readme_text": "x"},
    )
    spec = build_spec(
        candidate("a/b"), s, captions_from("a b c d e f g h i j"), 10.0, "v.wav", cfg(),
        screenshot_src="hero.png",
    )
    classic = to_classic(spec)
    classic_kinds = {"repo_card", "code", "stat", "bullets", "terminal", "screenshot", "diagram"}
    assert classic.format == "classic"
    assert {s.kind.value for s in classic.scenes} <= classic_kinds
    # Same audio, same captions, same cuts: nothing is re-asked.
    assert [s.fromFrame for s in classic.scenes] == [s.fromFrame for s in spec.scenes]


def test_the_renderer_picks_the_composition_the_spec_names(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "video_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(renderer, "_ensure_node_deps", lambda _dir: None)
    out = tmp_path / "out.mp4"
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        out.write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    for fmt in ("ad", "classic"):
        spec = SimpleNamespace(
            slug="s", durationInFrames=30, format=fmt, model_dump_json=lambda: "{}"
        )
        renderer.render(spec, out, cfg())
    assert seen[0][3] == "AdReel"
    assert seen[1][3] == "Reel"


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


def test_the_prompt_offers_the_menu_for_the_format_in_use():
    repo = candidate("a/b")
    ad, classic = _build_prompt(repo, cfg("ad")), _build_prompt(repo, cfg("classic"))
    assert "readme_text" in ad and "readme_text" not in classic
    assert "Use at least one `code` or `terminal` cue" in classic


def test_both_menus_move_the_recipe():
    # A post made with the ad menu must not be comparable to one made with the
    # classic menu, so both have to be inside the fingerprint's digest.
    source = prompt_source()
    assert "readme_text" in source and "Use at least one `code` or `terminal` cue" in source


# --------------------------------------------------------------------------
# README sections
# --------------------------------------------------------------------------


def test_a_tall_block_is_cut_around_the_quoted_line():
    found = {"x": 20, "y": 1000, "w": 600, "h": 2000, "hit": 2400, "lines": []}
    clip = screenshot._section_clip(found)
    assert clip["height"] == screenshot.SECTION_MAX_HEIGHT + screenshot.SECTION_PAD * 2
    assert clip["y"] < 2400 < clip["y"] + clip["height"]


def test_section_lines_are_in_image_pixels_and_inside_the_clip():
    found = {"lines": [{"y": 110, "h": 20, "text": " a "}, {"y": 900, "h": 20, "text": "b"}]}
    clip = {"x": 0, "y": 100, "width": 500, "height": 200}
    assert screenshot._section_lines(found, clip, 3) == [{"y": 30, "h": 60, "text": "a"}]


def test_the_ask_always_gets_the_end_card_in_the_ad_format():
    # The first real render's last beat ran 1.7 seconds before the ask, under
    # the minimum scene, so the split was refused and the end card never came.
    s = ad_script(
        {"kind": "statement", "spoken_excerpt": "One two three four five six.", "title": "One."},
        {"kind": "command", "spoken_excerpt": "Clone it.", "code": "git clone x"},
    )
    caps = captions_from("One two three four five six. Clone it. Follow for more.")
    ad = build_spec(candidate("a/b"), s, caps, 6.0, "v.wav", cfg(), spoken_cta="Follow for more.")
    classic = build_spec(
        candidate("a/b"), s, caps, 6.0, "v.wav", cfg("classic"), spoken_cta="Follow for more."
    )
    # "Follow" is the ninth word, at 4.0s: frame 120, one second after "Clone".
    assert ad.ctaFromFrame == 120
    assert classic.ctaFromFrame is None


# --------------------------------------------------------------------------
# The devices added 2026-10-09
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cue", "message"),
    [
        ({"kind": "diff", "code": "+ only added"}, "2 to 8 lines"),
        ({"kind": "diff", "code": "+ a\n+ b"}, "one '-' line"),
        ({"kind": "diff", "code": "- a\nb"}, "must start with"),
        ({"kind": "diff", "code": "- a\n+ " + "x" * 50}, "under 44"),
        ({"kind": "meter", "items": [{"label": "a", "value": 5}]}, "value and max"),
        ({"kind": "meter", "items": [{"label": "a", "value": 5, "max": 2}]}, "value <= max"),
        ({"kind": "hud", "items": [{"label": "a"}]}, "2 to 4 items"),
        ({"kind": "glass", "items": [{"label": c} for c in "abcd"]}, "2 or 3 items"),
    ],
)
def test_a_new_device_outside_its_limits_is_refused(cue, message):
    with pytest.raises(ValidationError, match=message):
        ad_script(cue)


def test_stars_and_demo_appear_at_most_once():
    with pytest.raises(ValidationError, match="stars cue at most once"):
        ad_script({"kind": "stars"}, {"kind": "statement", "title": "x"}, {"kind": "stars"})


def _device_spec(series=None, demo=None):
    from pipeline.models import DemoClip, StarPoint

    s = ad_script(
        {"kind": "repo_card", "spoken_excerpt": "z y"},
        {"kind": "stars", "spoken_excerpt": "a b"},
        {"kind": "demo", "spoken_excerpt": "c d"},
        {"kind": "glass", "spoken_excerpt": "e f", "items": [{"label": "x"}, {"label": "y"}]},
        {"kind": "diff", "spoken_excerpt": "g h", "code": "- old()\n+ new()"},
        {"kind": "meter", "spoken_excerpt": "i j",
         "items": [{"label": "m", "value": 1, "max": 4}]},
    )
    return build_spec(
        candidate("a/b", stars=1234), s, captions_from("z y a b c d e f g h i j"), 12.0, "v.wav",
        cfg(), screenshot_src="hero.png",
        star_series=[StarPoint(**p) for p in series or []],
        demo=DemoClip(**demo) if demo else None,
    )


def test_fetched_data_rides_on_its_scene():
    spec = _device_spec(
        series=[{"t": "2026-01-01", "v": 1}, {"t": "2026-05-01", "v": 600},
                {"t": "2026-10-09", "v": 1234}],
        demo={"src": "d.mp4", "w": 800, "h": 600, "seconds": 4.0},
    )
    kinds = {s.kind: s for s in spec.scenes}
    assert kinds[CueKind.STARS].series[-1].v == 1234
    assert kinds[CueKind.DEMO].demo.src == "d.mp4"
    assert kinds[CueKind.GLASS].imageSrc == "hero.png"


def test_a_device_with_no_data_degrades_rather_than_drawing_nothing():
    # A curve that could not be read is today's count; a README with no demo
    # shows its hero. Neither leaves an empty frame.
    kinds = [s.kind for s in _device_spec().scenes]
    assert CueKind.STARS not in kinds and CueKind.DEMO not in kinds
    stat = next(s for s in _device_spec().scenes if s.kind is CueKind.STAT)
    assert (stat.statValue, stat.statLabel) == ("1,234", "GitHub stars")


def test_the_classic_fallback_maps_the_new_devices_too():
    spec = _device_spec(
        series=[{"t": "2026-01-01", "v": 1}, {"t": "2026-05-01", "v": 9},
                {"t": "2026-10-09", "v": 20}],
        demo={"src": "d.mp4", "w": 800, "h": 600, "seconds": 4.0},
    )
    classic = to_classic(spec)
    classic_kinds = {"repo_card", "code", "stat", "bullets", "terminal", "screenshot", "diagram"}
    assert {s.kind.value for s in classic.scenes} <= classic_kinds
    assert all(s.demo is None and not s.series for s in classic.scenes)


@pytest.mark.parametrize(
    ("readme", "want"),
    [
        ("![demo](docs/demo.gif)", "https://raw.githubusercontent.com/o/r/HEAD/docs/demo.gif"),
        ('<img src="https://x.io/a.png"> <video src="https://x.io/b.mp4">', "https://x.io/b.mp4"),
        ("text\nhttps://github.com/user-attachments/assets/0f4c2a9e-1111-2222-3333-444455556666\n",
         "https://github.com/user-attachments/assets/0f4c2a9e-1111-2222-3333-444455556666"),
        ("![logo](logo.png) ![badge](https://img.shields.io/x.svg)", None),
        ("![d](https://github.com/o/r/blob/main/demo.gif)", "https://github.com/o/r/raw/main/demo.gif"),
    ],
)
def test_the_demo_is_the_first_moving_image_in_the_readme(readme, want):
    from pipeline.extras import demo_url

    assert demo_url(readme, "o/r") == want


def test_the_star_curve_is_the_accounts_own_snapshots_plus_today(tmp_path):
    import json as _json

    from pipeline.extras import star_series

    history = tmp_path / "star_history.json"
    history.write_text(_json.dumps({"o/r": {"2026-10-01": 100, "2026-09-30": 40}}))
    run = tmp_path / "run"
    run.mkdir()
    series = star_series(history, "o/r", 250, run)
    assert [p["v"] for p in series] == [40, 100, 250]
    # Cached, so a resume draws the same curve whatever the store says later.
    history.write_text("{}")
    assert star_series(history, "o/r", 999, run) == series


def test_a_blank_context_line_is_part_of_a_diff():
    s = ad_script({"kind": "diff", "code": "- old()\n \n+ new()"})
    assert s.visual_cues[0].code == "- old()\n \n+ new()"


def test_an_uploaded_video_in_a_video_tag_is_a_demo():
    from pipeline.extras import demo_url

    url = "https://github.com/user-attachments/assets/0f4c2a9e-1111-2222-3333-444455556666"
    assert demo_url(f'<video src="{url}" controls></video>', "o/r") == url
    # The same link as a markdown image is a screenshot, not a demo.
    assert demo_url(f"![shot]({url})", "o/r") is None
