"""The pipeline's side of the gateway.

The thing worth pinning here is not the happy path, it is that none of it can
ever break a publish. A cluster that is down must cost a cover image and a
keyword, never a video that already rendered.
"""

from __future__ import annotations

import httpx
import pytest

from config import Settings
from pipeline import gateway
from pipeline.models import _BANNED_PUNCTUATION

CAPTION = (
    "Colibri keeps ten gigabytes of a huge model on disk and streams it.\n"
    "The benchmark is its own, so treat it as a claim rather than a result.\n"
    "\n"
    "#devtools #localllm #inference"
)


# `_env_file=None` is load bearing in both fixtures. Settings reads the repo's
# real .env by default, so without it these tests pass or fail depending on
# whether the developer running them happens to have a gateway configured. That
# is exactly how this file first went green here and red in CI.
@pytest.fixture
def cfg() -> Settings:
    return Settings(
        github_token="x",
        ig_user_id="17841400000000000",
        gateway_url="https://gate.example.test",
        gateway_token="test-token",
        _env_file=None,
    )


@pytest.fixture
def off() -> Settings:
    """The normal state for anyone who does not run the gateway."""
    return Settings(github_token="x", _env_file=None)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Cover upload -----------------------------------------------------------


def test_a_hosted_cover_returns_the_url_meta_will_fetch(cfg, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    def handler(request):
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"name": "x.png", "url": "https://gate.example.test/covers/x.png"})

    url = gateway.upload_cover(cover, "owner-repo", cfg, client=_client(handler))
    assert url == "https://gate.example.test/covers/x.png"


def test_a_gateway_that_is_down_falls_back_to_a_video_frame(cfg, tmp_path):
    """None is a normal answer. The caller then lets thumb_offset pick."""
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")

    def handler(request):
        raise httpx.ConnectError("cluster is down")

    assert gateway.upload_cover(cover, "slug", cfg, client=_client(handler)) is None


def test_a_500_from_the_gateway_is_not_fatal(cfg, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    handler = lambda request: httpx.Response(500, text="boom")  # noqa: E731

    assert gateway.upload_cover(cover, "slug", cfg, client=_client(handler)) is None


def test_a_missing_cover_is_not_an_error(cfg, tmp_path):
    # render_covers is itself best effort, so the file legitimately may not
    # exist by the time a publish runs.
    assert gateway.upload_cover(tmp_path / "nope.png", "slug", cfg) is None


def test_nothing_is_uploaded_when_the_gateway_is_not_configured(off, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("should not call an unconfigured gateway")

    assert gateway.upload_cover(cover, "slug", off, client=_client(handler)) is None


# --- Post registration ------------------------------------------------------


def test_registering_a_post_sends_what_the_poller_needs(cfg):
    seen = {}

    def handler(request):
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    assert gateway.register_post("media-1", "https://github.com/a/b", cfg, client=_client(handler))
    assert seen == {
        "media_id": "media-1",
        "ig_user_id": "17841400000000000",
        "link": "https://github.com/a/b",
        "keyword": "send",
    }


def test_a_failed_registration_reports_false_rather_than_raising(cfg):
    """It costs the keyword mechanic on one post, not the post."""

    def handler(request):
        raise httpx.ConnectError("cluster is down")

    assert gateway.register_post("m", "https://x.test", cfg, client=_client(handler)) is False


def test_registration_is_skipped_when_unconfigured(off):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("should not call an unconfigured gateway")

    assert gateway.register_post("m", "https://x.test", off, client=_client(handler)) is False


# --- The caption call to action ---------------------------------------------


def test_the_cta_lands_above_the_hashtags(cfg):
    out = gateway.add_caption_cta(CAPTION, cfg)
    lines = [line for line in out.splitlines() if line.strip()]

    cta = next(i for i, line in enumerate(lines) if line.startswith("Comment SEND"))
    tags = next(i for i, line in enumerate(lines) if line.startswith("#"))
    assert cta < tags, "the call to action must not sit below the hashtag block"
    assert lines[0].startswith("Colibri"), "the hook keeps the first line"


def test_a_caption_with_no_hashtags_still_gets_the_cta(cfg):
    out = gateway.add_caption_cta("One sentence.", cfg)
    assert "Comment SEND and I will send you the link." in out


def test_the_cta_is_not_added_twice(cfg):
    once = gateway.add_caption_cta(CAPTION, cfg)
    assert gateway.add_caption_cta(once, cfg) == once


def test_no_cta_when_nothing_is_listening(off):
    """Telling people to comment a word no service watches is a promise the
    account cannot keep."""
    assert gateway.add_caption_cta(CAPTION, off) == CAPTION


def test_the_cta_obeys_the_repo_text_rules(cfg):
    out = gateway.add_caption_cta("Body.", cfg)
    cta = next(line for line in out.splitlines() if line.startswith("Comment"))

    assert not (set(cta) & _BANNED_PUNCTUATION), "no colons or dashes in viewer-facing copy"
    assert not any(ord(c) > 0x2500 for c in cta), "no emoji"


def test_the_keyword_is_configurable_and_shown_uppercase(cfg):
    cfg = cfg.model_copy(update={"gateway_keyword": "link"})
    assert "Comment LINK and I will send you the link." in gateway.add_caption_cta("B.", cfg)


def test_a_per_post_keyword_overrides_the_default(cfg):
    out = gateway.add_caption_cta("B.", cfg, keyword="GROK")
    assert "Comment GROK and I will send you the link." in out


# --- Per-post keywords ------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("xai-org/grok-build", "GROK"),       # first segment, not GROKBUILD
        ("DietrichGebert/ponytail", "PONYTAIL"),
        ("baidu/Unlimited-OCR", "UNLIMITED"),
        ("affaan-m/ECC", "ECC"),
        ("img2threejs/img2threejs", "IMG2THREEJS"),
        ("owner/some_tool", "SOME"),          # underscores split too
    ],
)
def test_the_keyword_comes_from_the_repo_name(cfg, repo, expected):
    assert gateway.keyword_for(repo, cfg) == expected


def test_a_name_too_short_to_type_falls_back_to_the_default(cfg):
    # Two letters is not a word anyone comments deliberately, and it collides
    # with ordinary text.
    assert gateway.keyword_for("some/go", cfg) == "SEND"


def test_the_keyword_is_always_a_single_typable_word(cfg):
    for repo in ("a-b/c.d-e_f", "owner/UPPER-lower", "owner/with.dots"):
        word = gateway.keyword_for(repo, cfg)
        assert word.isalnum(), f"{word} would not survive the API's one-word rule"
        assert word == word.upper()
