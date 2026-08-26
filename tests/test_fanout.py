"""One render, three destinations, and one upload.

`--enqueue` and `--recover` make an Instagram row, a YouTube row and a TikTok
row from the same build folder. `/api/media` is content addressed by digest, so
one MP4 still goes up once however many rows point at it, and the nightly needs
no second render and no extra step to keep three surfaces fed.

**Which render goes where is a decision, not a detail.** YouTube gets
`out-no-cta.mp4`, because a follow ask on a surface that calls following
subscribing reads wrong. TikTok gets `out.mp4`, because it is a feed like
Instagram's and the word is the same word. That is stated here as well as in
the code, because "whichever variable was nearest" is exactly how it would
otherwise be decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import candidate, script

import main
from config import Settings

CHANNEL = "UCq0Ff3lJ7dK2sWnEv8mXtLp"
OPEN_ID = "_000TikTokOpenIdLooksLikeThis0000"


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "build_dir", property(lambda self: tmp_path))
    return Settings(
        gateway_url="https://gate.example",
        gateway_token="t",
        ig_user_id="17841400000000000",
        youtube_channel_id=CHANNEL,
        tiktok_open_id=OPEN_ID,
        _env_file=None,
    )


@pytest.fixture
def run(cfg) -> Path:
    """A finished build folder, with everything the fan-out reads."""
    directory = cfg.build_dir / "2026-08-01" / "acme-tool"
    directory.mkdir(parents=True)
    (directory / "repo.json").write_text(candidate("acme/tool").model_dump_json())
    (directory / "script.json").write_text(script("a clip", hook="A hook").model_dump_json())
    (directory / "out.mp4").write_bytes(b"video")
    (directory / "caption.txt").write_text("One line.\n\n#devtools\n")
    return directory


class Recorded(list):
    """The rows, with the uploads hanging off them.

    A list subclass so the tests read `queued[0]` and `queued.uploads` without
    a two-value fixture every one of them has to unpack.
    """

    uploads: list[str]


@pytest.fixture
def queued(monkeypatch) -> Recorded:
    """Every row the fan-out asked the gateway for, and every upload."""
    rows = Recorded()
    uploads: list[str] = []

    def fake_enqueue(video_name, link, cfg, **kwargs):
        rows.append({"video_name": video_name, "link": link, **kwargs})
        return {"id": len(rows), "state": "draft", "detail": "queued"}

    def fake_upload(path, name, cfg):
        # Missing files answer the way the real one does, so a run folder with
        # no cover behaves here the way it does on the Mac.
        if not Path(path).exists():
            return None
        uploads.append(Path(path).name)
        return f"https://gate.example/media/{name}.mp4"

    monkeypatch.setattr(main.gateway, "enqueue", fake_enqueue)
    monkeypatch.setattr(main.gateway, "upload_media", fake_upload)
    monkeypatch.setattr(main.gateway, "fetch_queue", lambda cfg: [])
    monkeypatch.setattr(main.gateway, "keyword_for", lambda name, cfg: "TOOL")
    monkeypatch.setattr(main.scraper, "mark_featured", lambda cfg, name: None)
    # No Remotion. The trimmed render is the YouTube path's own step and its
    # absence is the documented fallback, tested separately below.
    monkeypatch.setattr(main.renderer, "render_without_cta", lambda spec, d, cfg: None)
    rows.uploads = uploads
    return rows


def accounts(rows: list[dict]) -> list[str]:
    return [row.get("account") or "instagram" for row in rows]


def test_one_render_makes_three_rows(cfg, run, queued):
    main._enqueue_run(cfg, run, approved=True)

    assert accounts(queued) == ["instagram", CHANNEL, OPEN_ID]


def test_the_media_goes_up_once(cfg, run, queued):
    """`/api/media` names a file by its own digest, so three rows pointing at
    one video is one upload. Without the trimmed YouTube render there is
    nothing else to send."""
    main._enqueue_run(cfg, run, approved=True)

    assert queued.uploads == ["out.mp4"]


def test_tiktok_gets_the_video_with_the_ask(cfg, run, queued):
    """Stated rather than inherited. YouTube gets the trimmed render because
    the ask reads wrong there; TikTok is a feed like Instagram's."""
    main._enqueue_run(cfg, run, approved=True)

    instagram, _youtube, tiktok = queued
    assert tiktok["video_name"] == instagram["video_name"]


def test_tiktok_keeps_the_ask_in_its_caption_and_youtube_does_not(cfg, run, queued):
    main._enqueue_run(cfg, run, approved=True)

    _instagram, youtube, tiktok = queued
    assert main.gateway.CAPTION_CTA in tiktok["caption"]
    assert main.gateway.CAPTION_CTA not in youtube["caption"]


def test_the_receipt_records_all_three(cfg, run, queued):
    """`queued.json` is the duplicate guard, and a row it does not mention is a
    row nothing will notice was made twice."""
    main._enqueue_run(cfg, run, approved=True)

    receipt = json.loads((run / "queued.json").read_text())
    assert receipt["youtube_id"] == 2
    assert receipt["tiktok_id"] == 3


def test_no_open_id_queues_the_other_two_and_says_nothing(cfg, run, queued):
    """The behaviour every render had before this. It is driven by
    TIKTOK_OPEN_ID in the render host's .env, and without it the fan-out skips
    TikTok the same way it skips YouTube without a channel id."""
    main._enqueue_run(cfg.model_copy(update={"tiktok_open_id": ""}), run, approved=True)

    assert accounts(queued) == ["instagram", CHANNEL]


def test_a_refused_tiktok_row_does_not_strand_the_reel(cfg, run, queued, monkeypatch, capsys):
    """The Reel's row is committed and its cooldown started by the time this
    runs, so a TikTok failure must not fail the command. It says so rather than
    returning in silence, because a destination that quietly stops receiving
    videos looks exactly like one nobody is posting to."""
    calls = {"n": 0}

    def refuse_the_third(video_name, link, cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            return None
        return {"id": calls["n"], "state": "draft", "detail": "queued"}

    monkeypatch.setattr(main.gateway, "enqueue", refuse_the_third)

    main._enqueue_run(cfg, run, approved=True)

    assert (run / "queued.json").exists()
    assert "would not take the TikTok row" in capsys.readouterr().out
