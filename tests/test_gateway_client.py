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
from pipeline.models import _BANNED_PUNCTUATION, CueKind, VisualCue

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


# --- Covered repos ----------------------------------------------------------


def test_covered_repos_come_back_keyed_by_name(cfg):
    def handler(request):
        assert request.url.path == "/api/covered"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"covered": [
            {"repo_full_name": "astral-sh/uv", "covered_at": "2026-07-30T09:00:00+00:00"},
            {"repo_full_name": "obra/superpowers", "covered_at": "2026-08-02T11:03:34+00:00"},
        ]})

    assert gateway.fetch_covered(cfg, client=_client(handler)) == {
        "astral-sh/uv": "2026-07-30",
        "obra/superpowers": "2026-08-02",
    }


def test_the_timestamp_is_cut_to_a_date(cfg):
    """UsedRepos stores YYYY-MM-DD and the cooldown counts in days."""
    handler = lambda request: httpx.Response(200, json={"covered": [  # noqa: E731
        {"repo_full_name": "a/b", "covered_at": "2026-08-02T23:59:59+00:00"},
    ]})
    assert gateway.fetch_covered(cfg, client=_client(handler)) == {"a/b": "2026-08-02"}


def test_a_row_missing_either_field_is_skipped(cfg):
    handler = lambda request: httpx.Response(200, json={"covered": [  # noqa: E731
        {"repo_full_name": None, "covered_at": "2026-08-02"},
        {"repo_full_name": "a/b", "covered_at": None},
        {"repo_full_name": "c/d", "covered_at": "2026-08-02"},
    ]})
    assert gateway.fetch_covered(cfg, client=_client(handler)) == {"c/d": "2026-08-02"}


def test_a_gateway_that_is_down_costs_discovery_nothing(cfg):
    """Empty means "add nothing", so the local store is used exactly as before."""
    def handler(request):
        raise httpx.ConnectError("cluster is down")

    assert gateway.fetch_covered(cfg, client=_client(handler)) == {}


def test_a_500_reading_covered_repos_is_not_fatal(cfg):
    handler = lambda request: httpx.Response(500, text="boom")  # noqa: E731
    assert gateway.fetch_covered(cfg, client=_client(handler)) == {}


def test_no_gateway_configured_reads_no_covered_repos(off):
    assert gateway.fetch_covered(off) == {}


# --- Rendered repos ---------------------------------------------------------


def test_rendered_repos_come_back_keyed_by_name(cfg):
    def handler(request):
        assert request.url.path == "/api/rendered"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"rendered": [
            {"repo_full_name": "firecrawl/anydoc", "rendered_at": "2026-08-07T20:11:00+00:00"},
        ]})

    assert gateway.fetch_rendered(cfg, client=_client(handler)) == {
        "firecrawl/anydoc": "2026-08-07",
    }


def test_a_gateway_that_is_down_may_cost_a_repeated_render(cfg):
    """Empty means "drop nothing", which is how discovery behaved before this
    endpoint existed. A duplicate render is the worst case, not a broken run."""
    def handler(request):
        raise httpx.ConnectError("cluster is down")

    assert gateway.fetch_rendered(cfg, client=_client(handler)) == {}


def test_an_old_gateway_without_the_endpoint_is_not_fatal(cfg):
    handler = lambda request: httpx.Response(404, text="Not Found")  # noqa: E731
    assert gateway.fetch_rendered(cfg, client=_client(handler)) == {}


def test_recording_a_render_sends_the_repo_and_the_folder(cfg):
    seen = {}

    def handler(request):
        import json

        assert request.url.path == "/api/rendered"
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "detail": "recorded"})

    assert gateway.register_rendered(
        "firecrawl/anydoc", cfg, run_folder="2026-08-08/firecrawl-anydoc",
        client=_client(handler),
    )
    assert seen["repo_full_name"] == "firecrawl/anydoc"
    assert seen["run_folder"] == "2026-08-08/firecrawl-anydoc"


def test_a_failed_render_record_never_fails_the_run(cfg):
    """The video is already on disk. Losing the record costs one repeated
    render on a later day, which is not worth failing a finished run over."""
    def handler(request):
        raise httpx.ConnectError("cluster is down")

    assert gateway.register_rendered("a/b", cfg, client=_client(handler)) is False


def test_forgetting_a_render_targets_the_repo_path(cfg):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "detail": "no longer recorded"})

    assert gateway.forget_rendered("firecrawl/anydoc", cfg, client=_client(handler))
    assert seen == {"method": "DELETE", "path": "/api/rendered/firecrawl/anydoc"}


def test_no_gateway_configured_records_no_renders(off):
    assert gateway.register_rendered("a/b", off) is False
    assert gateway.fetch_rendered(off) == {}
    assert gateway.forget_rendered("a/b", off) is False


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
        # The publisher registers a post the moment its media id exists, so it
        # wants the poller armed and has nothing to say about a publish date
        # that `registered_at` is about to record anyway. Only a backfill sends
        # either of those differently.
        "poll_comments": True,
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
    assert "Comment SEND if you want the link." in out


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
    assert "Comment LINK if you want the link." in gateway.add_caption_cta("B.", cfg)


def test_a_per_post_keyword_overrides_the_default(cfg):
    out = gateway.add_caption_cta("B.", cfg, keyword="GROK")
    assert "Comment GROK if you want the link." in out


def test_a_model_written_cta_is_replaced_rather_than_joined(cfg):
    """The caption may only ask for one word, and it must be the registered one.

    Seen for real on ayghri/i-have-adhd: Claude wrote "Comment ADHD" while the
    post was registered for IHAVEADHD. `comment_matches` compares whole words,
    so every viewer who did as the caption said would have got nothing back.
    """
    caption = (
        "Ten rules, zero lines of code.\n"
        "\n"
        "Comment ADHD and I will send you the link.\n"
        "\n"
        "#devtools\n"
    )
    out = gateway.add_caption_cta(caption, cfg, keyword="IHAVEADHD")

    asks = [line for line in out.splitlines() if line.lower().startswith("comment ")]
    assert asks == ["Comment IHAVEADHD if you want the link."]


@pytest.mark.parametrize(
    "written",
    [
        "Comment SEND and I will send you the link.",
        "comment send for the link",
        "Comment SEND below and I will DM it to you.",
    ],
)
def test_any_wording_of_the_ask_is_replaced(cfg, written):
    out = gateway.add_caption_cta(f"Body.\n\n{written}\n\n#tag\n", cfg)
    asks = [line for line in out.splitlines() if line.lower().startswith("comment ")]
    assert asks == ["Comment SEND if you want the link."]


def test_an_ask_at_the_tail_of_a_paragraph_is_removed_too(cfg):
    """Seen on alibaba/open-code-review: the ask rode the end of the prose.

    A line-anchored rule misses this, and the caption ends up asking for REVIEW
    in the paragraph and OPENCODEREVIEW two lines below it.
    """
    caption = (
        "It is the reviewer they ran internally for two years. "
        "Comment REVIEW for the link.\n"
        "\n"
        "#golang\n"
    )
    out = gateway.add_caption_cta(caption, cfg, keyword="OPENCODEREVIEW")

    assert "Comment REVIEW" not in out
    assert out.count("Comment ") == 1
    assert "ran internally for two years." in out, "the prose it rode on must survive"


def test_the_voice_never_reads_two_asks_back_to_back():
    """Heard in a rendered voiceover, which is the channel nobody can skip.

    alibaba/open-code-review read "Comment REVIEW and I will send the link.
    Comment OPENCODEREVIEW if you want the link." in one breath, asking for two
    different words. The caption had been fixed and the audio had not.
    """
    spoken = (
        "Higher precision, lower recall. Fewer false alarms. More misses. "
        "Comment REVIEW and I will send the link."
    )
    cleaned = gateway.strip_written_cta(spoken)

    assert "Comment" not in cleaned
    assert cleaned.endswith("More misses."), "the script it rode on must survive"


def test_stripping_leaves_a_script_that_never_asked_untouched():
    spoken = "Use it when you are skimming for the command, not reading an essay."
    assert gateway.strip_written_cta(spoken) == spoken


def test_a_cue_that_is_only_the_models_ask_is_dropped():
    """The third channel. Stripping the voiceover and leaving the cue behind
    leaves an excerpt quoting speech that is never spoken, and one unmatchable
    cue costs the whole video its word level scene timing."""
    cues = [
        VisualCue(kind=CueKind.STAT, spoken_excerpt="No machine learning, so it is fast."),
        VisualCue(kind=CueKind.REPO_CARD, spoken_excerpt="Comment ANYDOC for the repo."),
    ]
    kept = gateway.strip_cta_cues(cues)

    assert len(kept) == 1
    assert kept[0].spoken_excerpt == "No machine learning, so it is fast."


def test_an_ask_riding_on_a_real_cue_loses_only_the_ask():
    cues = [
        VisualCue(
            kind=CueKind.BULLETS,
            spoken_excerpt="Scanned pages need text recognition. Comment ANYDOC for the link.",
        )
    ]
    kept = gateway.strip_cta_cues(cues)

    assert len(kept) == 1, "the beat it rode on must survive"
    assert kept[0].spoken_excerpt == "Scanned pages need text recognition."


def test_cues_that_never_asked_come_back_unchanged():
    cues = [
        VisualCue(kind=CueKind.CODE, spoken_excerpt="Merged cells and footnotes survive."),
        VisualCue(kind=CueKind.SCREENSHOT, spoken_excerpt=""),
    ]
    assert gateway.strip_cta_cues(cues) == cues


def test_an_excerptless_cue_keeps_its_place():
    """`_allocate_frames` gives it a floor weight, so dropping it would silently
    delete a beat the script asked for."""
    cues = [VisualCue(kind=CueKind.SCREENSHOT, spoken_excerpt="")]
    assert len(gateway.strip_cta_cues(cues)) == 1


def test_every_cue_reading_as_an_ask_keeps_the_script_intact():
    """A match that greedy is the matcher being wrong, not the script. A video
    with no beats at all is worse than one timed by proportional guess."""
    cues = [VisualCue(kind=CueKind.REPO_CARD, spoken_excerpt="Comment ANYDOC for the repo.")]
    assert gateway.strip_cta_cues(cues) == cues


def test_prose_that_merely_mentions_commenting_is_left_alone(cfg):
    """Only a line that *opens* with the ask is one. Prose about comments is not."""
    caption = "The maintainer will comment on your issue within a day.\n\n#devtools\n"
    out = gateway.add_caption_cta(caption, cfg)
    assert "The maintainer will comment on your issue within a day." in out


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


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("alibaba/open-code-review", "OPENCODEREVIEW"),  # not OPEN
        ("anomalyco/opencode", "OPENCODE"),              # one segment, so untouched
        ("owner/agent-zero", "AGENTZERO"),
        ("owner/code-graph", "CODEGRAPH"),
    ],
)
def test_an_ordinary_word_is_not_used_as_the_ask(cfg, repo, expected):
    """A keyword has to be something nobody types by accident.

    `comment_matches` compares whole words, so OPEN on a video about an open
    source reviewer fires on "open source is great" and DMs a link to someone
    who was only talking. Longer to type beats messaging a stranger uninvited.
    """
    assert gateway.keyword_for(repo, cfg) == expected


def test_the_derived_keyword_never_collides_with_ordinary_comment_text(cfg):
    from gateway import conversations

    chatter = [
        "open source is great",
        "I love open code",
        "this is a cool dev tool",
        "nice ai agent",
        "does it run on the web",
    ]
    for repo in ("alibaba/open-code-review", "owner/dev-tools", "owner/ai-agent"):
        word = gateway.keyword_for(repo, cfg)
        for text in chatter:
            assert not conversations.comment_matches(text, word), f"{word} fires on {text!r}"


def test_a_name_too_short_to_type_falls_back_to_the_default(cfg):
    # Two letters is not a word anyone comments deliberately, and it collides
    # with ordinary text.
    assert gateway.keyword_for("some/go", cfg) == "SEND"


def test_the_keyword_is_always_a_single_typable_word(cfg):
    for repo in ("a-b/c.d-e_f", "owner/UPPER-lower", "owner/with.dots"):
        word = gateway.keyword_for(repo, cfg)
        assert word.isalnum(), f"{word} would not survive the API's one-word rule"
        assert word == word.upper()


# --- The spoken call to action ----------------------------------------------


def test_the_voice_reads_the_ask(cfg):
    assert gateway.spoken_cta("GROK", cfg) == "Comment GROK if you want the link."


def test_no_spoken_ask_when_nothing_is_listening(off):
    assert gateway.spoken_cta("GROK", off) is None


def test_the_spoken_ask_obeys_the_text_rules(cfg):
    line = gateway.spoken_cta("GROK", cfg)
    assert not (set(line) & _BANNED_PUNCTUATION), "no colons or dashes in anything spoken"
    assert not any(ord(c) > 0x2500 for c in line)


def test_the_three_channels_agree_on_the_word(cfg):
    """The end card, the voiceover and the caption all derive from one place.
    If they ever disagreed, viewers would comment a word nothing watches for."""
    keyword = gateway.keyword_for("xai-org/grok-build", cfg)
    assert keyword in gateway.spoken_cta(keyword, cfg)
    assert keyword in gateway.add_caption_cta("Body.", cfg, keyword=keyword)


def test_a_measured_post_is_not_logged_as_watched(cfg, caplog):
    """A backfill printing "watching" for eight old posts reads exactly like the
    failure the measuring guard exists to catch, which is how it got mistaken
    for one."""
    handler = lambda request: httpx.Response(200, json={"detail": "measuring m1"})  # noqa: E731

    with caplog.at_level("INFO"):
        assert gateway.register_post(
            "m1", "https://github.com/a/b", cfg, poll_comments=False, client=_client(handler)
        ) is True

    assert "measuring m1" in caplog.text
    assert "watching m1 for" not in caplog.text


def test_a_polled_post_still_says_watching(cfg, caplog):
    handler = lambda request: httpx.Response(200, json={"detail": "watching m1"})  # noqa: E731

    with caplog.at_level("INFO"):
        gateway.register_post(
            "m1", "https://github.com/a/b", cfg, keyword="UV", client=_client(handler)
        )

    assert "watching m1 for" in caplog.text
