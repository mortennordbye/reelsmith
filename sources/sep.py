"""The Stanford Encyclopedia's revision feed, which is this niche's change feed.

Account 1 ranks on star velocity, which works because GitHub publishes a number
that moves every day. A catalogue of dead philosophers publishes nothing, so
the equivalent has to be found rather than read: `plato.stanford.edu/new.html`
lists every entry revised in the last three months, dated, with the sections
that changed named underneath it. That is a change feed about ideas, written by
people who are paid to be careful, and it is the closest thing this subject has
to a commit log.

**It is a trigger, not a ranking.** A revision says an entry moved, not that
anybody is reading it, and most entries are about a topic rather than a person
with a scan and a quote. `pipeline/subjects.py` is what turns one of these into
a candidate, and it drops most of them.

The RSS feed at `/rss/sep.xml` carries the same list. This parses the HTML
because the page is the canonical one, it carries the changed sections and the
feed does not, and the markup has been an unstyled `<ul>` of `<li>` for as long
as the archive goes back.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime

import httpx

log = logging.getLogger(__name__)

FEED_URL = "https://plato.stanford.edu/new.html"
ENTRY_ROOT = "https://plato.stanford.edu/entries"

# One list item, which is one revision. Deliberately not a full parse: the page
# is a fragment of hand written HTML inside a template that has not changed in
# a decade, and an HTML parser would be a dependency the pipeline venv does not
# have for the sake of one <ul>.
_ITEM = re.compile(
    r'<a href="entries/(?P<slug>[^/"]+)/"><strong>(?P<title>[^<]+)</strong></a>\s*'
    r"\((?P<authors>[^)]*)\)\s*"
    r"\[(?P<kind>REVISED|NEW)(?::)?\s*<em>(?P<when>[^<]+)</em>\]\s*"
    r'(?:<div class="small">Changes to:\s*(?P<changes>[^<]*)</div>)?',
    re.S,
)


@dataclass(frozen=True)
class Revision:
    """One entry that moved, and what moved in it."""

    slug: str
    title: str
    authors: tuple[str, ...]
    revised: date
    changes: tuple[str, ...]
    is_new: bool

    @property
    def url(self) -> str:
        return f"{ENTRY_ROOT}/{self.slug}/"

    @property
    def substantive(self) -> bool:
        """Whether the main text moved, rather than only the reading list.

        A bibliography update is an author adding a paper somebody else wrote.
        The main text moving is the entry itself saying something different,
        which is the part worth treating as news.
        """
        return self.is_new or any("main text" in c.lower() for c in self.changes)


def parse(html: str) -> Iterator[Revision]:
    """Yield every revision on the page, newest first, as the page lists them."""
    for m in _ITEM.finditer(html):
        try:
            when = datetime.strptime(m.group("when").strip(), "%B %d, %Y").date()
        except ValueError:
            log.debug("Unparseable date %r on %s", m.group("when"), m.group("slug"))
            continue
        authors = tuple(
            part.strip()
            for part in re.split(r",| and ", m.group("authors") or "")
            if part.strip()
        )
        changes = tuple(
            part.strip() for part in (m.group("changes") or "").split(",") if part.strip()
        )
        yield Revision(
            slug=m.group("slug"),
            title=m.group("title").strip(),
            authors=authors,
            revised=when,
            changes=changes,
            is_new=m.group("kind") == "NEW",
        )


def fetch(*, client: httpx.Client | None = None, timeout: float = 20.0) -> list[Revision]:
    """The feed, or an empty list.

    Empty rather than raising, for the reason every read in `pipeline/results.py`
    shrugs: this is one signal among several, and a night where the encyclopedia
    is down should rank on the others rather than not run.
    """
    # A caller's client is used rather than entered; see the note in
    # `sources/wikimedia.py`, which shares this shape and found the trap.
    try:
        if client is not None:
            response = client.get(FEED_URL, headers={"User-Agent": USER_AGENT})
        else:
            with httpx.Client(timeout=timeout, follow_redirects=True) as http:
                response = http.get(FEED_URL, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return list(parse(response.text))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("The revision feed did not answer: %s", exc)
        return []


# Wikimedia asks for a contact in the agent string and the encyclopedia is
# read by the same client, so one constant serves both.
USER_AGENT = "reelsmith/1.0 (https://github.com/mortennordbye/reelsmith)"
