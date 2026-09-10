"""A hand rendered video, taken into the shape the rest of the pipeline reads.

Everything after the render reads `build/<account>/<date>/<slug>/` rather than
the stage that filled it, which is what lets `--adopt` exist at all: a video
made outside the pipeline becomes an ordinary run, and the queue, the fan-out,
the slots and the panel need to know nothing about where it came from.

It is the seam a second niche publishes through until discovery and scripting
cover that niche. Without it those episodes can only be uploaded by hand, and
nothing the gateway measures ever sees them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import main
from config import Settings
from pipeline.models import VideoScript

HOOK = "You have cut a sentence because of who might read it"


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "build_dir", property(lambda self: tmp_path / "build"))
    return Settings(account="thewholequote", _env_file=None)


@pytest.fixture
def episode(tmp_path) -> Path:
    """A rendered episode with the sidecars written beside it, which is how
    they are already written rather than a shape invented here."""
    src = tmp_path / "episodes"
    src.mkdir()
    video = src / "ep1-montaigne.mp4"
    video.write_bytes(b"video")
    (src / "cover.png").write_bytes(b"png")
    (src / "caption.txt").write_text("One line.\n\n#montaigne\n")
    (src / "lines.json").write_text(json.dumps({"lines": ["He wrote this.", "So do this."]}))
    return video


def test_it_writes_the_run_folder(cfg, episode):
    run = main._adopt_video(cfg, episode, hook=HOOK, slug="montaigne-essais")

    assert run.name == "montaigne-essais"
    assert (run / "out.mp4").read_bytes() == b"video"
    assert (run / "cover.png").exists()
    assert (run / "caption.txt").read_text().startswith("One line.")


def test_the_hook_travels(cfg, episode):
    """The one thing with no natural file beside the video, and the one the
    feedback loop reads back into the next prompt. A row with no hook is
    skipped by `past_posts` entirely, so an empty default would silently drop
    the episode out of the account's own history."""
    run = main._adopt_video(cfg, episode, hook=HOOK, slug="montaigne-essais")

    script = VideoScript.model_validate_json((run / "script.json").read_text())
    assert script.hook == HOOK
    assert script.spoken_script == "He wrote this. So do this."


def test_the_slug_defaults_to_the_filename(cfg, episode):
    run = main._adopt_video(cfg, episode, hook=HOOK, slug=None)

    assert run.name == "ep1-montaigne"


def test_it_refuses_to_overwrite_a_video(cfg, episode):
    """Adopting twice is a typo, not an intention. It writes a folder and
    nothing else, so refusing costs a rerun with a different slug."""
    main._adopt_video(cfg, episode, hook=HOOK, slug="montaigne-essais")

    with pytest.raises(main.typer.Exit):
        main._adopt_video(cfg, episode, hook=HOOK, slug="montaigne-essais")


def test_the_sidecars_are_optional(cfg, tmp_path, capsys):
    """A video on its own still adopts. Both missing sidecars are said out
    loud, because an empty caption and a black player in the panel are things
    to notice now rather than at the slot."""
    bare = tmp_path / "bare.mp4"
    bare.write_bytes(b"video")

    run = main._adopt_video(cfg, bare, hook=HOOK, slug=None)

    assert (run / "out.mp4").exists()
    assert not (run / "caption.txt").exists()
    out = capsys.readouterr().out
    assert "No cover.png" in out
    assert "No caption.txt" in out


def test_an_adopted_run_queues(cfg, episode, monkeypatch):
    """The point of the whole thing. What comes out of `--adopt` is a run
    folder, so `--enqueue` takes it with no knowledge of where it came from,
    and it queues without a repo and without a link."""
    rows: list[dict] = []
    monkeypatch.setattr(
        main.gateway,
        "enqueue",
        lambda video_name, link, cfg, **kw: rows.append({"link": link, **kw})
        or {"id": len(rows), "state": "draft", "detail": "queued"},
    )
    monkeypatch.setattr(
        main.gateway, "upload_media", lambda path, name, cfg: f"https://g/media/{name}.mp4"
    )
    run = main._adopt_video(cfg, episode, hook=HOOK, slug="montaigne-essais")

    queued = cfg.model_copy(
        update={
            "gateway_url": "https://g",
            "gateway_token": "t",
            "youtube_channel_id": "UCq0Ff3lJ7dK2sWnEv8mXtLp",
        }
    )
    main._enqueue_run(queued, run, approved=True)

    assert [row["link"] for row in rows] == [""]
    assert rows[0]["hook"] == HOOK
    assert (run / "queued.json").exists()
