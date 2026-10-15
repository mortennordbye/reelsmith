"""A LinkedIn post for each new blog entry, drafted and queued.

The subject is somebody's own blog rather than a trending repository, so this
is not a render stage and shares almost nothing with the reel path: no voice,
no frames, no cooldown store. What it shares is the end of the line. The draft
goes to the gateway's queue like a Reel does, the gateway publishes it at a
slot, and the panel is where it is read, edited, approved or cancelled.

**The source is the published blog, never a checkout.** The feed lists what is
live, and only what is live can be drafted, so merging an entry with
`draft: false` stays the one trigger and the render host needs no clone of the
repository the blog is built from.

**A row with the entry's URL, in any state, means done.** Cancelling a draft
is how somebody says "no post for this one", so a cancelled row has to stop
the next night from drafting it again rather than inviting it.

**The editorial rules are a private file**, `accounts/<name>/ref/linkedin-style.md`,
for the reason `PROFILE.md` is gitignored: they are written in one identifiable
person's voice, about that person's audience, with that person's numbers.
`sync-private.sh` already carries `accounts/*/ref/*` to the render host.
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

import httpx

from config import Settings
from pipeline import claude, gateway

log = logging.getLogger(__name__)

_TIMEOUT = 30
# Plenty for a 2,000 word article and well under what is worth sending.
_ARTICLE_CHARS = 14_000
# What LinkedIn shows above "see more", roughly. The schedule shows it as the
# row's hook because it is the part that decides whether anyone reads on.
_HOOK_CHARS = 210
_MAX_ATTEMPTS = 3
# LinkedIn refuses more than this, and the gateway checks it again at publish.
MAX_COMMENTARY = 3000

# Dashes a reader takes for generated text. A hyphen is not one of them here:
# "self-hosted" and "zero-downtime" are how this audience writes, and unlike
# the spoken reel there is no voice to trip over one.
_BANNED_DASHES = "‒–—―−"
_URL = re.compile(r"https?://\S+")
# Emoji and pictographs. The style bans them and the model drifts toward them.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿️]")


class DraftError(RuntimeError):
    """A draft that could not be made. The night carries on without it."""


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    published: date
    summary: str = ""

    @property
    def slug(self) -> str:
        tail = self.url.rstrip("/").rsplit("/", 1)[-1] or "entry"
        return re.sub(r"[^a-z0-9-]", "-", tail.lower())[:60]


@dataclass(frozen=True)
class Draft:
    opener: str
    commentary: str
    hashtags: tuple[str, ...]

    def full(self, url: str) -> str:
        """The post as it goes out: the body, the link, the hashtags.

        The link is in the body as well as on the card, which is what the
        style guide settled on: a body link out-clicks a card.
        """
        return f"{self.commentary.strip()}\n\n{url}\n{' '.join(self.hashtags)}"


# --- Reading the blog ---------------------------------------------------------


def parse_feed(xml: str) -> list[Entry]:
    """Every item in an RSS 2.0 feed, newest first."""
    root = ElementTree.fromstring(xml)
    entries = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        raw_date = item.findtext("pubDate") or ""
        if not url or not title or not raw_date:
            continue
        try:
            published = parsedate_to_datetime(raw_date).date()
        except (TypeError, ValueError):
            continue
        summary = _text_of(html.unescape(item.findtext("description") or ""))
        entries.append(Entry(title=title, url=url, published=published, summary=summary))
    return sorted(entries, key=lambda e: e.published, reverse=True)


class _Text(HTMLParser):
    """The readable text of a page, from its `<article>` when it has one."""

    _SKIP = {"script", "style", "nav", "header", "footer", "svg", "noscript"}
    _BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote", "tr", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.article: list[str] = []
        self.og_image = ""
        self._skip = 0
        self._in_article = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "article":
            self._in_article += 1
        if tag == "meta":
            found = dict(attrs)
            if found.get("property") == "og:image" and found.get("content"):
                self.og_image = found["content"]

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "article" and self._in_article:
            self._in_article -= 1
        if tag in self._BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if not self._skip:
            self._emit(data)

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        if self._in_article:
            self.article.append(text)


def _text_of(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    return _tidy("".join(parser.parts))


def _tidy(text: str) -> str:
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def read_article(markup: str) -> tuple[str, str]:
    """The article's text and its Open Graph image URL."""
    parser = _Text()
    parser.feed(markup)
    body = "".join(parser.article) or "".join(parser.parts)
    return _tidy(body)[:_ARTICLE_CHARS], parser.og_image


def pending(
    entries: list[Entry], queue: list[dict], *, today: date, max_age_days: int
) -> list[Entry]:
    """Entries young enough to post about that no queue row names yet.

    The age bound is what keeps a first run from drafting a post for every
    entry the blog ever had, most of which were posted about by hand.
    """
    taken = {_norm(str(row.get("link") or "")) for row in queue}
    oldest = today - timedelta(days=max_age_days)
    return [e for e in entries if e.published >= oldest and _norm(e.url) not in taken]


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


# --- Writing the post ---------------------------------------------------------

SYSTEM = """You write one LinkedIn post that sends readers to a blog entry, in the
author's own voice. The author's style guide follows the task and is binding:
its beats, its voice, its banned content, and its examples of posts that worked.

Hard rules, which a validator enforces and will send back:
- No em dash, en dash or minus sign. Use a full stop or a comma instead.
- No colons in the prose. Split the sentence in two.
- No emoji.
- Do not include the URL or the hashtags in `commentary`. They are added after
  it, the URL on its own line and the hashtags on the last line.
- Exactly four hashtags, each starting with #, no spaces inside one.
- Four short paragraphs at most. The first line has to survive LinkedIn's two
  line truncation, so the sharpest idea goes first.

Ground every claim in the article. Name one concrete detail from it. Do not
invent numbers the article does not state."""

SCHEMA = {
    "type": "object",
    "properties": {
        "opener": {
            "type": "string",
            "enum": ["two_questions", "statement"],
            "description": "Which opener this post uses.",
        },
        "commentary": {"type": "string", "description": "The post, without URL or hashtags."},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 4,
            "maxItems": 4,
        },
    },
    "required": ["opener", "commentary", "hashtags"],
}


def problems(draft: Draft) -> list[str]:
    """Everything wrong with a draft, phrased for the model to fix."""
    found = []
    prose = _URL.sub("", draft.commentary)
    if any(ch in prose for ch in _BANNED_DASHES):
        found.append("It contains a dash. Rewrite those sentences with a full stop or a comma.")
    if ":" in prose:
        found.append("It contains a colon. Split that sentence into two.")
    if _EMOJI.search(draft.commentary):
        found.append("It contains an emoji. Remove it.")
    if "http" in draft.commentary:
        found.append("The commentary contains a URL. Leave the URL out; it is added after.")
    if "#" in draft.commentary:
        found.append("The commentary contains a hashtag. Hashtags go only in `hashtags`.")
    if len(draft.hashtags) != 4 or not all(re.fullmatch(r"#\w+", t) for t in draft.hashtags):
        found.append("Give exactly four hashtags, each like #Kubernetes with no spaces.")
    paragraphs = [p for p in draft.commentary.split("\n\n") if p.strip()]
    if len(paragraphs) > 4:
        found.append(f"It has {len(paragraphs)} paragraphs. Four at most.")
    if len(draft.commentary) > MAX_COMMENTARY - 400:
        found.append("It is too long. Cut it to four short paragraphs.")
    return found


def history_block(queue: list[dict], limit: int = 6) -> str:
    """The posts this profile already made through the queue, newest first.

    So the model can vary the opener against the last one, which the style
    guide asks for and which cannot be checked from the article alone.
    """
    made = [
        row for row in queue
        if row.get("state") in ("published", "approved", "claimed", "draft") and row.get("caption")
    ]
    made.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    if not made:
        return ""
    blocks = [
        f"<post state=\"{row.get('state')}\" date=\"{str(row.get('created_at') or '')[:10]}\">\n"
        f"{row['caption']}\n</post>"
        for row in made[:limit]
    ]
    return (
        "<recent_posts>\nThe most recent first. Do not open the same way as the newest one.\n"
        + "\n".join(blocks)
        + "\n</recent_posts>"
    )


def prompt_for(entry: Entry, article: str, style: str, history: str) -> str:
    return "\n\n".join(
        part
        for part in (
            f"<style_guide>\n{style}\n</style_guide>",
            history,
            f"<article title=\"{entry.title}\" url=\"{entry.url}\" "
            f"published=\"{entry.published.isoformat()}\">\n{article}\n</article>",
            "Write the LinkedIn post for this article.",
        )
        if part
    )


def write(entry: Entry, article: str, style: str, history: str, cfg: Settings) -> Draft:
    """Ask for a draft and send it back with the specific faults until it passes."""
    base = prompt_for(entry, article, style, history)
    feedback: list[str] = []
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        prompt = base
        if feedback:
            prompt += (
                "\n\nYour previous draft was refused for these reasons. Fix every one:\n- "
                + "\n- ".join(feedback)
            )
        answer = claude.payload(claude.run(prompt, SCHEMA, cfg, system=SYSTEM, research=False))
        draft = Draft(
            opener=str(answer.get("opener") or ""),
            commentary=unicodedata.normalize("NFC", str(answer.get("commentary") or "")).strip(),
            hashtags=tuple(str(t).strip() for t in answer.get("hashtags") or ()),
        )
        feedback = problems(draft)
        if not feedback:
            return draft
        log.info("Draft %d for %s refused: %s", attempt, entry.slug, "; ".join(feedback))
    raise DraftError(f"No acceptable draft for {entry.url} after {_MAX_ATTEMPTS} attempts")


# --- The night ---------------------------------------------------------------


def style_path(cfg: Settings) -> Path:
    return cfg.linkedin_style_path or cfg.account_dir / "ref" / "linkedin-style.md"


def own_rows(cfg: Settings, queue: list[dict]) -> list[dict]:
    """The profile's own rows. A brand scoped read may carry other platforms."""
    return [
        row for row in queue
        if not row.get("account_id") or str(row.get("account_id")) == cfg.linkedin_person_id
    ]


def draft_next(cfg: Settings, *, approve: bool, dry_run: bool = False) -> Path | None:
    """Draft and queue a post for the newest entry that has none. Returns its run folder.

    None when there is nothing to do, which is the normal outcome on most
    nights: the blog gets an entry a month and this runs every night.
    """
    if not cfg.linkedin_person_id:
        raise DraftError("No LINKEDIN_PERSON_ID in this account's .env; walk the consent first.")
    if not cfg.linkedin_blog_feed:
        raise DraftError("No LINKEDIN_BLOG_FEED in this account's .env, so no blog to read.")
    style_file = style_path(cfg)
    if not style_file.is_file():
        raise DraftError(
            f"No style guide at {style_file}. Without it the drafter writes in nobody's voice."
        )

    queue = gateway.fetch_queue(cfg)
    if queue is None:
        # Unknown is not empty. Drafting on an unreadable queue is how one
        # entry gets three drafts on three nights.
        raise DraftError("The gateway queue could not be read, so it cannot say what is done.")
    queue = own_rows(cfg, queue)

    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as http:
        feed = http.get(cfg.linkedin_blog_feed)
        feed.raise_for_status()
        todo = pending(
            parse_feed(feed.text), queue,
            today=datetime.now(UTC).date(), max_age_days=cfg.linkedin_max_age_days,
        )
        if not todo:
            log.info("No blog entry is waiting for a LinkedIn post")
            return None
        entry = todo[0]
        page = http.get(entry.url)
        page.raise_for_status()
        article, image_url = read_article(page.text)
        image = http.get(image_url) if image_url else None

    run_dir = cfg.run_dir(f"linkedin-{entry.slug}")
    draft = write(entry, article, style_file.read_text(), history_block(queue), cfg)
    text = draft.full(entry.url)
    (run_dir / "draft.json").write_text(
        json.dumps(
            {
                "url": entry.url,
                "title": entry.title,
                "published": entry.published.isoformat(),
                "opener": draft.opener,
                "text": text,
            },
            indent=2,
        )
    )

    cover_name = None
    if image is not None and image.status_code == 200 and image.content:
        suffix = ".jpg" if "jpeg" in image.headers.get("content-type", "") else ".png"
        thumb = run_dir / f"thumbnail{suffix}"
        thumb.write_bytes(image.content)
        if not dry_run:
            hosted = gateway.upload_media(thumb, f"linkedin-{entry.slug}", cfg)
            cover_name = hosted.rsplit("/", 1)[-1] if hosted else None

    if dry_run:
        log.info("Dry run, nothing queued. The draft is in %s", run_dir)
        return run_dir

    result = gateway.enqueue(
        "",
        entry.url,
        cfg,
        caption=text,
        cover_name=cover_name,
        approved=approve,
        account=cfg.linkedin_person_id,
        title=entry.title[:100],
        hook=text.splitlines()[0][:_HOOK_CHARS],
    )
    if result is None:
        raise DraftError(f"The gateway refused the draft for {entry.url}; it is in {run_dir}")
    (run_dir / "queued.json").write_text(json.dumps(result, indent=2))
    return run_dir
