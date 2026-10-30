"""The blog to LinkedIn drafter.

Most of what can go wrong here is quiet: a first run drafting a post for every
entry the blog ever had, a cancelled draft coming back the next night, an
unreadable queue read as an empty one, or a draft that breaks the style rules
going out because nobody reads it before an armed slot fires.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from config import Settings
from pipeline import linkedin

FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Newest entry</title><link>https://blog.example.test/blog/newest/</link>
<pubDate>Tue, 13 Oct 2026 00:00:00 +0000</pubDate>
<description>&lt;p&gt;The opening line.&lt;/p&gt;</description></item>
<item><title>Older entry</title><link>https://blog.example.test/blog/older/</link>
<pubDate>Thu, 20 Aug 2026 00:00:00 +0000</pubDate><description></description></item>
</channel></rss>"""

PAGE = """<html><head>
<meta property="og:image" content="https://blog.example.test/blog/newest/featured.png">
<script>var noise = 1;</script></head>
<body><nav>Home Blog</nav><article><h1>Newest entry</h1>
<p>It runs on Talos with <code>Prometheus</code>.</p><p>Second paragraph.</p></article>
<footer>Copyright</footer></body></html>"""

GOOD = {
    "opener": "statement",
    "commentary": "By default a cluster tells you nothing.\n\nSo I built one screen.",
    "hashtags": ["#Kubernetes", "#Observability", "#Prometheus", "#Grafana"],
}


# --- Reading the blog ---------------------------------------------------------


def test_the_feed_is_read_newest_first():
    entries = linkedin.parse_feed(FEED)
    assert [e.title for e in entries] == ["Newest entry", "Older entry"]
    assert entries[0].published == date(2026, 10, 13)
    assert entries[0].summary == "The opening line."
    assert entries[0].slug == "newest"


def test_the_article_is_the_article_and_the_image_comes_along():
    text, image = linkedin.read_article(PAGE)
    assert "Talos with Prometheus" in text
    assert "Second paragraph." in text
    assert "Home Blog" not in text and "Copyright" not in text and "noise" not in text
    assert image == "https://blog.example.test/blog/newest/featured.png"


def test_a_first_run_does_not_draft_the_whole_archive():
    entries = linkedin.parse_feed(FEED)
    todo = linkedin.pending(entries, [], today=date(2026, 10, 15), max_age_days=14)
    assert [e.title for e in todo] == ["Newest entry"]


def test_a_cancelled_draft_counts_as_done():
    """Cancelling is how somebody says no post for this one."""
    entries = linkedin.parse_feed(FEED)
    queue = [{"state": "cancelled", "link": "https://blog.example.test/blog/newest"}]
    assert linkedin.pending(entries, queue, today=date(2026, 10, 15), max_age_days=14) == []


# --- The rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("commentary", "fault"),
    [
        ("One thing — another.", "dash"),
        ("One thing – another.", "dash"),
        ("The result: nothing broke.", "colon"),
        ("Shipped it \U0001f680", "emoji"),
        ("Read it at https://blog.example.test/x/", "URL"),
        ("Read it #now", "hashtag"),
        ("a\n\nb\n\nc\n\nd\n\ne", "paragraphs"),
    ],
)
def test_a_draft_that_breaks_a_rule_is_refused_with_the_reason(commentary, fault):
    draft = linkedin.Draft("statement", commentary, tuple(GOOD["hashtags"]))
    assert any(fault in problem for problem in linkedin.problems(draft))


def test_a_hyphenated_word_is_fine_here():
    """The reel validator refuses hyphens because a voice trips on them. A post
    has no voice, and this audience writes self-hosted with one."""
    text = "A self-hosted, zero-downtime cutover."
    draft = linkedin.Draft("statement", text, tuple(GOOD["hashtags"]))
    assert linkedin.problems(draft) == []


def test_hashtags_are_exactly_four_single_words():
    draft = linkedin.Draft("statement", "Fine.", ("#One", "#Two", "#Three"))
    assert linkedin.problems(draft)
    draft = linkedin.Draft("statement", "Fine.", ("#One", "#Two", "#Three", "#Four Five"))
    assert linkedin.problems(draft)


def test_the_post_carries_the_link_in_the_body_and_hashtags_last():
    draft = linkedin.Draft(**{**GOOD, "hashtags": tuple(GOOD["hashtags"])})
    text = draft.full("https://blog.example.test/blog/newest/")
    lines = text.splitlines()
    assert lines[-1] == "#Kubernetes #Observability #Prometheus #Grafana"
    assert lines[-2] == "https://blog.example.test/blog/newest/"


def test_history_lets_the_model_vary_the_opener():
    queue = [
        {"state": "published", "caption": "Older post.", "created_at": "2026-09-01"},
        {"state": "published", "caption": "Newest post.", "created_at": "2026-10-01"},
        {"state": "cancelled", "caption": "Never went out.", "created_at": "2026-10-02"},
    ]
    block = linkedin.history_block(queue)
    assert block.index("Newest post.") < block.index("Older post.")
    assert "Never went out." not in block


# --- The night ----------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "build_dir", property(lambda self: tmp_path / "build"))
    style = tmp_path / "linkedin-style.md"
    style.write_text("Five beats. No emoji.")
    return Settings(
        account="someaccount",
        linkedin_person_id="abc123",
        linkedin_blog_feed="https://blog.example.test/blog/index.xml",
        linkedin_style_path=style,
        _env_file=None,
    )


@pytest.fixture
def web(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("index.xml"):
            return httpx.Response(200, text=FEED)
        if url.endswith("featured.png"):
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        return httpx.Response(200, text=PAGE)

    real = httpx.Client
    monkeypatch.setattr(
        linkedin.httpx, "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


@pytest.fixture
def gateway_calls(monkeypatch):
    calls: dict[str, list] = {"enqueue": [], "upload": []}
    monkeypatch.setattr(linkedin.gateway, "fetch_queue", lambda cfg: [])
    monkeypatch.setattr(
        linkedin.gateway, "upload_media",
        lambda path, slug, cfg: calls["upload"].append(path) or "https://gate/media/thumb.png",
    )

    def enqueue(video_name, link, cfg, **kw):
        calls["enqueue"].append({"video_name": video_name, "link": link, **kw})
        return {"id": 1, "state": "draft"}

    monkeypatch.setattr(linkedin.gateway, "enqueue", enqueue)
    return calls


def answers(monkeypatch, *replies):
    queue = list(replies)
    prompts: list[str] = []

    def run(prompt, schema, cfg, *, system, research=None):
        prompts.append(prompt)
        return {"result": json.dumps(queue.pop(0))}

    monkeypatch.setattr(linkedin.claude, "run", run)
    return prompts


def test_a_new_entry_becomes_a_queued_draft(cfg, web, gateway_calls, monkeypatch):
    answers(monkeypatch, GOOD)

    run_dir = linkedin.draft_next(cfg, approve=False)

    assert run_dir is not None and (run_dir / "queued.json").exists()
    (row,) = gateway_calls["enqueue"]
    assert row["video_name"] == ""
    assert row["link"] == "https://blog.example.test/blog/newest/"
    assert row["account"] == "abc123"
    assert row["cover_name"] == "thumb.png"
    assert row["approved"] is False
    assert row["hook"] == "By default a cluster tells you nothing."


def test_a_refused_draft_goes_back_with_the_reason(cfg, web, gateway_calls, monkeypatch):
    bad = {**GOOD, "commentary": "The result: nothing broke."}
    prompts = answers(monkeypatch, bad, GOOD)

    linkedin.draft_next(cfg, approve=False)

    assert len(prompts) == 2
    assert "colon" in prompts[1]


def test_three_bad_drafts_queue_nothing(cfg, web, gateway_calls, monkeypatch):
    bad = {**GOOD, "commentary": "Bad — draft."}
    answers(monkeypatch, bad, bad, bad)

    with pytest.raises(linkedin.DraftError):
        linkedin.draft_next(cfg, approve=True)
    assert gateway_calls["enqueue"] == []


def test_an_unreadable_queue_is_not_an_empty_one(cfg, web, gateway_calls, monkeypatch):
    monkeypatch.setattr(linkedin.gateway, "fetch_queue", lambda cfg: None)
    answers(monkeypatch, GOOD)

    with pytest.raises(linkedin.DraftError, match="could not be read"):
        linkedin.draft_next(cfg, approve=False)
    assert gateway_calls["enqueue"] == []


def test_no_style_guide_means_no_draft(cfg, web, gateway_calls, monkeypatch, tmp_path):
    cfg.linkedin_style_path = tmp_path / "missing.md"
    with pytest.raises(linkedin.DraftError, match="style guide"):
        linkedin.draft_next(cfg, approve=False)


def test_a_dry_run_writes_the_draft_and_queues_nothing(cfg, web, gateway_calls, monkeypatch):
    answers(monkeypatch, GOOD)

    run_dir = linkedin.draft_next(cfg, approve=False, dry_run=True)

    assert json.loads((run_dir / "draft.json").read_text())["opener"] == "statement"
    assert gateway_calls == {"enqueue": [], "upload": []}
