"""Discovery and ranking for the second niche, in the shape of `scraper.py`.

Account 1 ranks repositories on star velocity because GitHub is a catalogue
with a number that moves. This niche's catalogue is Wikidata, its number is
Wikipedia pageviews, and the whole of the difference is that Wikimedia
publishes the daily series itself, so there is no `StarHistory` here: the API
is the history.

**The encyclopedia is a bonus, not the pool.** The first design read
`plato.stanford.edu/new.html` alone, which was the honest reading of what
`PROFILE.md` proposed. Measured on 2026-09-10 it yields 89 revisions in three
months, 69 of them substantive, 18 of those about a person, and 10 about a
person dead long enough for their words to be public domain. That is one
subject every nine days against a format that wants one a night, so a feed
that answers "what changed" was being asked "what exists". It is scored as a
signal now, the way Hacker News is for account 1, and the pool is the
catalogue underneath it: about 1,100 people at 60 sitelinks and 2,100 at 40,
which at a 30 day cooldown is years of supply.

**Nothing here fetches a quote or writes a script.** Discovery says who
tonight is about and hands over the artefacts to look at; what they said and
what they did about it is the scriptwriter's job, against the primary source.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from config import Settings
from pipeline.models import SubjectCandidate
from sources import sep
from sources import wikimedia as wm

log = logging.getLogger(__name__)

SPARQL = "https://query.wikidata.org/sparql"

# Writers, philosophers, scientists, historians and poets. Deliberately narrow:
# the format is a person who wrote something down and then acted on it, so a
# monarch with a portrait and no sentences of their own is not a candidate.
OCCUPATIONS = (
    "wd:Q4964182",  # philosopher
    "wd:Q36180",  # writer
    "wd:Q901",  # scientist
    "wd:Q170790",  # mathematician
    "wd:Q11063",  # astronomer
    "wd:Q1234713",  # theologian
    "wd:Q49757",  # poet
    "wd:Q201788",  # historian
    "wd:Q18805",  # naturalist
    "wd:Q39631",  # physician
    "wd:Q81096",  # engineer
    "wd:Q205375",  # inventor
    "wd:Q11774202",  # essayist
    "wd:Q1930187",  # journalist
    "wd:Q13582652",  # explorer
)

# How well known a subject has to be before it is worth ranking at all,
# measured in Wikipedia language editions. The pool is 2,072 people at 40 and
# 1,086 at 60, so this is the dial between depth and how much of the catalogue
# is names nobody recognises.
MIN_SITELINKS = 60

# How many of the pool get a pageviews call on a given night. One call covers
# 60 days for one article, so this is the whole cost of ranking, and it is
# ordered by sitelinks so the ones nobody would recognise are the ones that go
# unread on a quiet night.
RANK_DEPTH = 120

POOL_TTL_DAYS = 30

# Weekly pageviews below which a velocity ratio is not believed in full. Set at
# the point where a doubling is a few hundred people rather than a few dozen.
VELOCITY_FLOOR = 2_000

_QUERY = """
SELECT DISTINCT ?p ?pLabel ?pDescription ?died ?born ?article ?links WHERE {
  ?p wdt:P31 wd:Q5 ; wdt:P570 ?died ; wdt:P18 ?img ; wikibase:sitelinks ?links .
  OPTIONAL { ?p wdt:P569 ?born }
  ?p wdt:P106 ?occ . VALUES ?occ { %(occupations)s }
  ?article schema:about ?p ; schema:isPartOf <https://en.wikipedia.org/> .
  FILTER(YEAR(?died) < %(cutoff)d) FILTER(?links >= %(links)d)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?links)
LIMIT %(limit)d
"""


def _pool_path(cfg: Settings) -> Path:
    return Path(cfg.data_dir) / "subject_pool.json"


def _ask(query: str, client: httpx.Client | None) -> list[dict]:
    """One SPARQL POST. Raises, because the retry above is what decides."""
    headers = {"Accept": "application/sparql-results+json", "User-Agent": wm.USER_AGENT}
    if client is not None:
        response = client.post(SPARQL, data={"query": query}, headers=headers)
    else:
        with httpx.Client(timeout=120, follow_redirects=True) as http:
            response = http.post(SPARQL, data={"query": query}, headers=headers)
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def fetch_pool(
    cfg: Settings, *, client: httpx.Client | None = None, limit: int = 2000, refresh: bool = False
) -> list[dict]:
    """The catalogue, cached on disk for a month.

    Cached because it is a query about people who died before 1931 and the
    answer moves at the speed of Wikidata editing, not at the speed of news.
    Re-running it nightly would spend a minute of somebody else's query service
    to learn nothing, which is the same argument that keeps discovery off
    GitHub's search API for repos it already knows about.
    """
    path = _pool_path(cfg)
    if not refresh and path.is_file():
        age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if age < timedelta(days=POOL_TTL_DAYS):
            return json.loads(path.read_text())

    # The query service answers 502 and 504 under load often enough that a
    # single attempt is not a reading of whether it is up; three tries with a
    # pause covered every failure seen while this was written.
    query = _QUERY % {
        "occupations": " ".join(OCCUPATIONS),
        "cutoff": date.today().year - wm.PUBLIC_DOMAIN_YEARS,
        "links": MIN_SITELINKS,
        "limit": limit,
    }
    for attempt in range(3):
        try:
            rows = _ask(query, client)
            break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("The Wikidata pool query failed: %s", exc)
            rows = []
            time.sleep(5 * (attempt + 1))
    if not rows:
        # The cache rather than nothing, however old. A stale catalogue of dead
        # people is worth more than a night that ranks nobody.
        return json.loads(path.read_text()) if path.is_file() else []


    # Deduplicated by id, because a person with three occupations and two
    # portraits is that many rows even under DISTINCT, and the pool is a list
    # of people rather than of statements about them.
    seen: dict[str, dict] = {}
    for row in rows:
        qid = row["p"]["value"].rsplit("/", 1)[-1]
        if qid in seen:
            continue
        article = row["article"]["value"].rsplit("/", 1)[-1].replace("_", " ")
        label = row.get("pLabel", {}).get("value", "")
        seen[qid] = {
            "qid": qid,
            # The label service returns the bare id for a few entities, and an
            # episode about "Q9061" is worse than one about the article title,
            # which is the person's name spelled the way readers search it.
            "name": article if re.fullmatch(r"Q\d+", label) or not label else label,
            "description": row.get("pDescription", {}).get("value", ""),
            "article": article,
            "born": _year(row.get("born", {}).get("value")),
            "died": _year(row.get("died", {}).get("value")),
            "links": int(row["links"]["value"]),
        }
    pool = sorted(seen.values(), key=lambda r: r["links"], reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pool, indent=1) + "\n")
    log.info("Pool refreshed: %d people", len(pool))
    return pool


def _year(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        return int(stamp[: stamp.index("-", 1)])
    except ValueError:
        return None


def score(candidate: SubjectCandidate) -> SubjectCandidate:
    """Split the score into the parts that made it, the way `score_candidates` does.

    The weights mirror account 1's argument rather than its numbers. Velocity
    dominates, because the whole claim of this format is that it covers what
    moved rather than what is famous; attention is there so a spike on somebody
    nobody reads at all does not win; the encyclopedia is the outside signal
    that something was genuinely said about them this week; and artefacts are
    scored because an episode with one picture is a slideshow, which is the
    failure `PROFILE.md` records from the first prototype.
    """
    # **Velocity is damped by how much traffic it is measured on.** The first
    # live proposal run put a printer with 64 views in a week at the top of the
    # list on a velocity of 2.04, which is nine extra readers. A ratio computed
    # on a small base is noise wearing a signal's clothes, and this is the
    # niche's version of the damped stars per day proxy `sources/github.py`
    # falls back to before it has real history.
    confidence = min(candidate.views_recent / VELOCITY_FLOOR, 1.0)
    velocity = min(candidate.velocity / 2.0, 1.0) * confidence if candidate.velocity else 0.0
    attention = min(candidate.views_recent / 20_000, 1.0)
    encyclopedia = 0.0
    if candidate.sep_revised:
        age = (date.today() - candidate.sep_revised).days
        encyclopedia = max(0.0, 1.0 - age / 90.0)
    artefacts = min(candidate.usable_artefacts / 4.0, 1.0)

    breakdown = {
        "velocity": round(velocity * 0.50, 4),
        "attention": round(attention * 0.15, 4),
        "encyclopedia": round(encyclopedia * 0.15, 4),
        "artefacts": round(artefacts * 0.20, 4),
    }
    return candidate.model_copy(
        update={"score": round(sum(breakdown.values()), 4), "score_breakdown": breakdown}
    )


def rank(
    cfg: Settings,
    *,
    depth: int = RANK_DEPTH,
    enrich_top: int = 8,
    client: httpx.Client | None = None,
    covered: set[str] | None = None,
) -> list[SubjectCandidate]:
    """Tonight's ranked subjects, best first.

    One pageviews call per candidate and one Commons call per survivor, so the
    cost is bounded by `depth` rather than by the size of the pool. Covered
    subjects are dropped before either call, for the reason discovery drops
    covered repos before enrichment: a candidate that cannot win should not
    cost a request.
    """
    pool = fetch_pool(cfg, client=client)
    if not pool:
        return []

    revisions = {}
    for revision in sep.fetch(client=client):
        # Keyed on the entry title's last word, which is a surname often enough
        # to be worth having and never enough to be relied on. The pool's own
        # names are what a match is confirmed against.
        revisions[revision.title.lower()] = revision

    covered = covered or set()
    ranked: list[SubjectCandidate] = []
    for row in pool[:depth]:
        candidate = SubjectCandidate(
            qid=row["qid"],
            name=row["name"],
            article=row["article"],
            description=row.get("description", ""),
            born=row.get("born"),
            died=row.get("died"),
        )
        if candidate.key in covered:
            continue

        views = wm.pageviews(candidate.article, client=client)
        if not views:
            continue
        revision = revisions.get(candidate.name.lower())
        candidate = candidate.model_copy(
            update={
                "views_recent": sum(views[-7:]),
                "velocity": round(wm.velocity(views), 4),
                "sep_slug": revision.slug if revision else "",
                "sep_revised": revision.revised if revision else None,
            }
        )
        ranked.append(score(candidate))

    ranked.sort(key=lambda c: c.score, reverse=True)

    # The artefact half of the score costs two Commons calls, so only the top
    # of the list pays for it and only the top of the list can be scored on it.
    # Same staging as `scraper.py`, which fetches a README after the cheap
    # filters rather than before them: a candidate that cannot win should not
    # cost a request. The re-sort is what makes the artefact weight real.
    for i, candidate in enumerate(ranked[:enrich_top]):
        ranked[i] = score(enrich(candidate, client=client))
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked


def enrich(
    candidate: SubjectCandidate, *, client: httpx.Client | None = None
) -> SubjectCandidate:
    """Attach the artefacts, which is the expensive half of a candidate.

    Separate from `rank` because it is two Commons calls and only the top few
    are ever going to be rendered, which is the same reason README fetching in
    `scraper.py` happens after the cheap filters rather than before them.
    """
    _article, qid, portrait = wm.article_for(candidate.name, client=client)
    files = wm.category_files(candidate.article, client=client)
    if portrait:
        files = [portrait, *[f for f in files if f != portrait]]
    usable = [a for a in wm.artefacts(files, client=client) if a.usable]
    return candidate.model_copy(
        update={
            "qid": candidate.qid or qid,
            "portrait": portrait,
            "artefacts": [a.title for a in usable],
            "usable_artefacts": len(usable),
        }
    )


def inspect(
    cfg: Settings, *, top: int = 15, depth: int = RANK_DEPTH, ai: bool = True
) -> list[SubjectCandidate]:
    """Rank tonight's subjects and print the table. Returns the full ranking.

    The counterpart to `scraper.inspect_candidates`, and the same promise: it
    reads, ranks and prints, and it commits the account to nothing.
    """
    from rich.console import Console
    from rich.table import Table

    from pipeline import scraper

    console = Console()
    covered = set(scraper.covered_repos(cfg))

    with console.status("Reading the catalogue and asking for proposals..."):
        ranked = discover(cfg, depth=depth, ai=ai, covered=covered)

    table = Table(title=f"Subjects for {date.today().isoformat()}", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Subject", style="cyan", no_wrap=True, max_width=30)
    table.add_column("Died", justify="right")
    table.add_column("Views 7d", justify="right")
    table.add_column("Velocity", justify="right")
    table.add_column("Art", justify="right")
    table.add_column("SEP")
    table.add_column("Score", justify="right", style="bold")

    for i, c in enumerate(ranked[:top], 1):
        table.add_row(
            str(i),
            c.name,
            str(c.died) if c.died is not None else "?",
            f"{c.views_recent:,}",
            f"{c.velocity:.2f}",
            str(c.usable_artefacts or "-"),
            c.sep_slug or "-",
            f"{c.score:.3f}",
        )

    console.print(table)
    if ranked:
        console.print(
            "\n[dim]Velocity is the last 7 days of pageviews against the 8 weeks "
            "before them, so 1.00 is a subject being read at its usual rate. "
            "'Art' counts public domain Commons files at least 1080 wide, and is "
            "only measured for the top of the list.[/dim]"
        )
        console.print(f"[bold green]Tonight:[/] {ranked[0].name}, {ranked[0].description}")
    else:
        console.print(
            "[yellow]Nothing ranked.[/] "
            "[dim]The catalogue or the metrics API is down.[/]"
        )
    return ranked


# --------------------------------------------------------------------------
# The other way of finding a subject
# --------------------------------------------------------------------------

PROPOSAL_SYSTEM = """You find subjects for a short video account about advice
with a citation. Every episode is one person who wrote something down and then
did something about it, told from a primary source a viewer could go and check.

What makes a subject work:
- They are long dead, so their own words are free to quote.
- There is a specific sentence of theirs worth hearing, in their own words.
- There is a documented thing they DID about it, in a source, not an inference.
- There is something real to look at: a manuscript, a notebook, a letter, a
  drawing, an instrument, a photograph of their own work.

What makes a subject fail:
- The famous names of a philosophy syllabus. Aristotle, Plato, Kant, Nietzsche,
  Marx and Descartes are already over covered everywhere and are not wanted.
- Anyone whose advice has to be invented or extrapolated to be useful.
- A quote whose attribution is disputed, or that only exists on quote sites.

Prefer people a working software engineer would find surprising: engineers,
physicians, naturalists, cartographers, printers, nurses, instrument makers,
explorers, translators. Someone whose problem rhymes with a working life.

Return only people you can name a real primary source for."""

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The person, as Wikipedia names them",
                    },
                    "why": {
                        "type": "string",
                        "description": "One sentence on the episode it makes",
                    },
                    "source": {
                        "type": "string",
                        "description": "The primary source the quote and the action come from",
                    },
                },
                "required": ["name", "why", "source"],
            },
        }
    },
    "required": ["subjects"],
}


def propose(
    cfg: Settings,
    *,
    n: int = 8,
    avoid: set[str] | None = None,
    client: httpx.Client | None = None,
) -> list[SubjectCandidate]:
    """Ask Claude for subjects the catalogue would never surface, then check them.

    **This is the answer to "why not just ask the model".** The model is better
    than a query at the thing a query cannot do, which is knowing that a
    cartographer's notebook makes an episode and a philosopher's syllabus entry
    does not. It is worse than a query at the thing this account exists to be
    good at, which is being right about who said what: a proposal is a claim
    from memory, and the format's whole discipline is that every claim arrives
    with a source.

    So the two are used for what each is good at. Claude proposes, and every
    proposal is then put through the same checks a catalogue subject passes:
    it has to resolve to a real person on Wikidata, be dead long enough for
    their words to be free, and have public domain artefacts to look at.
    Anything that fails is dropped rather than corrected, because a subject the
    encyclopedias cannot confirm is one this account cannot audit.

    The quote itself is still not taken on trust here. Discovery says who
    tonight is about; the scriptwriter fetches what they actually wrote.
    """
    from pipeline import claude as claude_cli

    avoid = avoid or set()
    prompt = (
        f"Propose {n} subjects for tonight, each a different kind of person and a "
        f"different century where you can manage it.\n\n"
        f"Already covered, so do not propose them: "
        f"{', '.join(sorted(avoid)) if avoid else 'nothing yet'}."
    )
    try:
        # **No web search, deliberately, and it is the verification that pays
        # for that.** Researching eight proposals took past the 420 second
        # timeout on the first live run, and it was buying confidence that
        # Wikidata is about to provide for nothing: every name is looked up
        # below, and one the encyclopedias cannot confirm is dropped whether or
        # not the model read a page about it. What research is genuinely needed
        # for is the quote and the documented action, and that is the
        # scriptwriter's call, against the primary source, later.
        envelope = claude_cli.run(
            prompt, PROPOSAL_SCHEMA, cfg, system=PROPOSAL_SYSTEM, research=False
        )
        proposals = claude_cli.payload(envelope).get("subjects", [])
    except claude_cli.ClaudeError as exc:
        # The catalogue is the fallback, not the other way round. A night with
        # no proposals ranks what Wikidata already knows about.
        log.warning("No proposals: %s", exc)
        return []

    checked: list[SubjectCandidate] = []
    for proposal in proposals:
        name = (proposal.get("name") or "").strip()
        if not name:
            continue
        article, qid, portrait = wm.article_for(name, client=client)
        person = wm.person_for(qid, article, portrait, client=client)
        if not person:
            log.info("Dropped %r: not a person on Wikidata", name)
            continue
        if not person.public_domain:
            log.info("Dropped %r: died %s, too recent to quote freely", name, person.died)
            continue
        candidate = SubjectCandidate(
            qid=person.qid,
            name=person.label,
            article=person.article,
            description=person.description or proposal.get("why", ""),
            born=person.born,
            died=person.died,
            portrait=person.portrait,
        )
        if candidate.key in avoid:
            continue
        views = wm.pageviews(candidate.article, client=client)
        candidate = candidate.model_copy(
            update={
                "views_recent": sum(views[-7:]),
                "velocity": round(wm.velocity(views), 4),
            }
        )
        checked.append(score(enrich(candidate, client=client)))

    log.info("Proposed %d, %d survived checking", len(proposals), len(checked))
    return checked


def discover(
    cfg: Settings,
    *,
    depth: int = RANK_DEPTH,
    ai: bool = True,
    client: httpx.Client | None = None,
    covered: set[str] | None = None,
) -> list[SubjectCandidate]:
    """Both ways of finding a subject, merged and scored on one scale.

    The catalogue is what makes tonight possible at all; the proposals are what
    stop every night being another name off the same syllabus. They are ranked
    together rather than one being a fallback for the other, because a proposal
    that nobody is reading about should lose to a catalogue subject that
    somebody is, and the score already says which is which.
    """
    covered = covered or set()
    ranked = rank(cfg, depth=depth, client=client, covered=covered)
    if not ai:
        return ranked

    seen = {c.qid for c in ranked} | covered
    proposals = propose(cfg, avoid=covered | {c.name for c in ranked[:20]}, client=client)
    merged = ranked + [p for p in proposals if p.qid not in seen]
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged


def lookup(name: str, *, client: httpx.Client | None = None) -> SubjectCandidate | None:
    """One named subject, checked and enriched. None when it does not survive.

    The same checks a proposal passes, because a name typed by a person is a
    claim from memory too. It is how `--episode --subject "Joseph Moxon"`
    cannot quietly write an episode about somebody who died in 1994.
    """
    article, qid, portrait = wm.article_for(name, client=client)
    person = wm.person_for(qid, article, portrait, client=client)
    if not person or not person.public_domain:
        return None
    views = wm.pageviews(person.article, client=client)
    candidate = SubjectCandidate(
        qid=person.qid,
        name=person.label,
        article=person.article,
        description=person.description,
        born=person.born,
        died=person.died,
        portrait=person.portrait,
        views_recent=sum(views[-7:]),
        velocity=round(wm.velocity(views), 4),
    )
    return score(enrich(candidate, client=client))
