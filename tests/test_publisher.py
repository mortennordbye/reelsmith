"""Instagram publishing.

Still no network: httpx.MockTransport answers every request in-process, which
is what lets the publish sequence be asserted at all. The thing worth testing
here is the sequence and the failure handling, not Meta's JSON.

These tests used to assert a resumable byte upload that Meta never actually
accepted on this API path. They passed for weeks because a mock will happily
confirm whatever you tell it. The lesson is in `test_publishing_without_a_hosted_video_says_why`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from config import Settings
from pipeline import publisher
from pipeline.publisher import PublishError


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    """Settings pointed at a temp data dir, so no test touches the real token."""
    monkeypatch.setattr(Settings, "data_dir", property(lambda self: tmp_path))
    return Settings(
        ig_user_id="17841400000000000",
        ig_access_token="seed-token",
        ig_poll_interval_s=0,
        ig_publish_timeout_s=5,
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# --- Tokens ----------------------------------------------------------------


def test_load_token_falls_back_to_the_env_seed(cfg):
    state = publisher.load_token(cfg)
    assert state.access_token == "seed-token"
    # Nothing issued it here, so the expiry is genuinely unknown rather than 0.
    assert state.expires_at is None


def test_stored_token_wins_over_the_seed(cfg):
    publisher.save_token(cfg, "stored-token", 60 * 86_400)
    assert publisher.load_token(cfg).access_token == "stored-token"


def test_saved_token_records_an_expiry(cfg):
    state = publisher.save_token(cfg, "t", 60 * 86_400)
    assert 59 < state.days_left < 61
    assert json.loads(cfg.ig_token_path.read_text())["access_token"] == "t"


def test_load_token_without_a_seed_or_a_store_is_an_error(cfg):
    cfg.ig_access_token = ""
    with pytest.raises(PublishError, match="No Instagram token"):
        publisher.load_token(cfg)


def test_refresh_stores_the_new_token(cfg):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["grant_type"] == "ig_refresh_token"
        assert request.url.params["access_token"] == "seed-token"
        return _json({"access_token": "fresh", "expires_in": 60 * 86_400})

    state = publisher.refresh_token(cfg, client=_client(handler))
    assert state.access_token == "fresh"
    assert publisher.load_token(cfg).access_token == "fresh"


def test_refresh_if_due_skips_a_healthy_token(cfg, monkeypatch):
    publisher.save_token(cfg, "healthy", 60 * 86_400)
    monkeypatch.setattr(
        publisher, "refresh_token", lambda *a, **k: pytest.fail("should not refresh")
    )
    assert publisher.refresh_token_if_due(cfg) is None


def test_refresh_if_due_fires_inside_the_margin(cfg, monkeypatch):
    publisher.save_token(cfg, "old", 5 * 86_400)  # margin is 15 days
    monkeypatch.setattr(publisher, "refresh_token", lambda *a, **k: "refreshed")
    assert publisher.refresh_token_if_due(cfg) == "refreshed"


def test_an_expired_token_says_so_rather_than_retrying(cfg):
    # Past its expiry, Meta will not refresh it at all: this needs a browser,
    # so failing with the wrong message costs someone a debugging session.
    expired = datetime.now(UTC) - timedelta(days=1)
    cfg.ig_token_path.write_text(
        json.dumps({"access_token": "dead", "expires_at": expired.isoformat()})
    )
    with pytest.raises(PublishError, match="can no longer be refreshed"):
        publisher.refresh_token_if_due(cfg)


# --- Publishing ------------------------------------------------------------


VIDEO_URL = "https://gate.example.test/media/out-abc123.mp4"


def _video(tmp_path):
    path = tmp_path / "out.mp4"
    path.write_bytes(b"\x00" * 2048)
    return path


def _happy_handler(calls: list[httpx.Request], *, statuses=("FINISHED",)):
    """A Meta that behaves. `statuses` is the poll sequence to hand back."""
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/media"):
            return _json({"id": "container-1"})
        if path.endswith("/media_publish"):
            return _json({"id": "media-9"})
        if path.endswith("/container-1"):
            return _json({"status_code": remaining.pop(0) if remaining else "FINISHED"})
        if path.endswith("/media-9"):
            return _json({"permalink": "https://instagram.com/reel/abc"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


def test_publish_runs_the_three_calls_in_order(cfg, tmp_path):
    calls: list[httpx.Request] = []
    result = publisher.publish_reel(
        _video(tmp_path), "a caption", cfg, video_url=VIDEO_URL,
        client=_client(_happy_handler(calls)),
    )

    assert result.media_id == "media-9"
    assert result.permalink == "https://instagram.com/reel/abc"
    steps = [f"{c.method} {c.url.path.rsplit('/', 1)[-1]}" for c in calls]
    # Create the container, poll while Meta fetches and transcodes, publish.
    # There is no upload leg: Meta pulls the file rather than receiving it.
    assert steps[:2] == ["POST media", "GET container-1"]
    assert "POST media_publish" in steps


def test_the_container_points_meta_at_the_hosted_video(cfg, tmp_path):
    calls: list[httpx.Request] = []
    publisher.publish_reel(
        _video(tmp_path), "hello", cfg, video_url=VIDEO_URL,
        client=_client(_happy_handler(calls)),
    )

    body = dict(pair.split("=", 1) for pair in calls[0].content.decode().split("&"))
    assert body["media_type"] == "REELS"
    assert body["caption"] == "hello"
    # upload_type=resumable is Facebook Login for Business only. Sending it here
    # gets "The parameter video_url is required" and nothing else.
    assert "upload_type" not in body
    assert body["video_url"]


def test_publishing_without_a_hosted_video_says_why(cfg, tmp_path):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("should not call Meta with nowhere to fetch from")

    with pytest.raises(PublishError, match="video_url"):
        publisher.publish_reel(_video(tmp_path), "hi", cfg, client=_client(handler))


def test_without_a_cover_url_it_points_at_the_cover_frame(cfg, tmp_path):
    # The fallback has to be the same moment render_covers uses, or the grid
    # thumbnail stops matching cover.png. 90 frames at 30fps is 3000ms.
    calls: list[httpx.Request] = []
    publisher.publish_reel(
        _video(tmp_path), "hi", cfg, video_url=VIDEO_URL,
        client=_client(_happy_handler(calls)),
    )

    body = dict(pair.split("=", 1) for pair in calls[0].content.decode().split("&"))
    assert body["thumb_offset"] == "3000"
    assert "cover_url" not in body


def test_a_cover_url_replaces_the_frame_offset(cfg, tmp_path):
    calls: list[httpx.Request] = []
    publisher.publish_reel(
        _video(tmp_path),
        "hi",
        cfg,
        video_url=VIDEO_URL,
        cover_url="https://example.com/cover.png",
        client=_client(_happy_handler(calls)),
    )

    body = dict(pair.split("=", 1) for pair in calls[0].content.decode().split("&"))
    # Meta ignores thumb_offset when both are sent, so sending both would only
    # mislead whoever reads the request next.
    assert "thumb_offset" not in body



def test_publish_waits_out_in_progress(cfg, tmp_path):
    calls: list[httpx.Request] = []
    handler = _happy_handler(calls, statuses=("IN_PROGRESS", "IN_PROGRESS", "FINISHED"))
    assert publisher.publish_reel(
        _video(tmp_path), "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
    )
    assert sum(1 for c in calls if c.url.path.endswith("/container-1") and c.method == "GET") == 3


def test_a_failed_container_never_reaches_media_publish(cfg, tmp_path):
    calls: list[httpx.Request] = []
    handler = _happy_handler(calls, statuses=("ERROR",))
    with pytest.raises(PublishError, match="ERROR"):
        publisher.publish_reel(
            _video(tmp_path), "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
        )
    assert not any(c.url.path.endswith("/media_publish") for c in calls)


def test_a_timed_out_container_says_it_can_be_retried(cfg, tmp_path):
    calls: list[httpx.Request] = []
    handler = _happy_handler(calls, statuses=["IN_PROGRESS"] * 200)
    cfg.ig_publish_timeout_s = 0
    with pytest.raises(PublishError, match="still processing"):
        publisher.publish_reel(
            _video(tmp_path), "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
        )


def test_a_missing_video_fails_before_any_request(cfg, tmp_path):
    def handler(request):
        raise AssertionError("should not have made a request")

    with pytest.raises(PublishError, match="No video"):
        publisher.publish_reel(
            tmp_path / "gone.mp4", "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
        )



def test_an_expired_token_error_explains_the_fix(cfg, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {"error": {"message": "Session has expired", "code": 190, "type": "OAuthException"}},
            status=400,
        )

    with pytest.raises(PublishError, match="re-authorise"):
        publisher.publish_reel(
            _video(tmp_path), "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
        )


def test_the_run_receipt_stops_a_second_publish(cfg, tmp_path, monkeypatch, capsys):
    # The failure this prevents is posting the same Reel twice, which is only
    # obvious to a human and --post runs without one.
    # Imported here rather than at module scope: main pulls in the whole
    # pipeline, and the rest of this file does not need it.
    import main
    from tests.conftest import candidate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.mp4").write_bytes(b"\x00")
    (run_dir / "caption.txt").write_text("a caption\n")
    (run_dir / "repo.json").write_text(candidate("astral-sh/uv").model_dump_json())

    marked: list[str] = []
    monkeypatch.setattr(main.scraper, "mark_featured", lambda _cfg, name: marked.append(name))
    # Meta fetches the MP4 from a public URL, so _publish_run refuses to go on
    # without one. This test is about the receipt, not about hosting.
    monkeypatch.setattr(main.gateway, "upload_media", lambda *a, **k: VIDEO_URL)
    monkeypatch.setattr(
        main.publisher,
        "publish_reel",
        lambda *a, **k: publisher.PublishResult(media_id="m1", permalink="https://ig/p/1"),
    )

    main._publish_run(cfg, run_dir)
    assert marked == ["astral-sh/uv"]
    assert json.loads((run_dir / "published.json").read_text())["media_id"] == "m1"

    # Second time: no upload, no second cooldown.
    monkeypatch.setattr(
        main.publisher, "publish_reel", lambda *a, **k: pytest.fail("published twice")
    )
    main._publish_run(cfg, run_dir)
    assert marked == ["astral-sh/uv"]
    assert "Already published" in capsys.readouterr().out


def test_graph_errors_keep_metas_own_message(cfg, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {"error": {"message": "Media type not supported", "fbtrace_id": "AbC123"}}, status=400
        )

    with pytest.raises(PublishError) as exc:
        publisher.publish_reel(
            _video(tmp_path), "hi", cfg, video_url=VIDEO_URL, client=_client(handler)
        )
    assert "Media type not supported" in str(exc.value)
    assert "AbC123" in str(exc.value)
