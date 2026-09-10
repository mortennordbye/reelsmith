"""Wikipedia, Wikidata and Commons, which between them answer the three
questions this niche's discovery has to ask about a name.

**Is it a person, and are they long enough dead.** Wikidata, because it is the
only one of the three that answers in data rather than in prose. `P31 = Q5` is
the human check and `P570` is the death date; a subject dead under a century is
one whose translations and photographs are probably still in copyright, which
matters here in a way it never did for account 1, where every subject shipped
its own licence file.

**Is anybody reading about them.** Pageviews per day, which is this niche's
star velocity: a real number that moves daily, published by the same
foundation, with no key and no quota worth worrying about. The shape of the
answer is the same as `sources/github.py`, deliberately: a recent window
against a baseline, so a spike reads as a spike rather than as a big number.

**Is there something to look at.** Commons, since the format's first test is a
real per item visual artefact nobody drew for it. The portrait Wikipedia
already picked is the reliable one; the rest of a Commons category is mostly
coats of arms and plaques, and choosing among them is the open problem this
module deliberately does not pretend to solve.

Every read here shrugs and returns nothing on failure. One signal missing costs
a candidate its ranking; an exception costs the night.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PAGEVIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"

USER_AGENT = "reelsmith/1.0 (https://github.com/mortennordbye/reelsmith)"

HUMAN = "Q5"
# A subject dead this long is one whose own words are out of copyright
# everywhere this posts, and whose portraits are old enough that the scan is
# the only thing anybody could claim. Life plus 70 is the long pole; the extra
# quarter century is for the gap between a death and a last publication.
PUBLIC_DOMAIN_YEARS = 95


@dataclass(frozen=True)
class Person:
    """What Wikidata says about a name, reduced to what discovery ranks on."""

    qid: str
    label: str
    article: str
    description: str = ""
    born: int | None = None
    died: int | None = None
    portrait: str = ""
    works: tuple[str, ...] = ()

    @property
    def public_domain(self) -> bool:
        if self.died is None:
            return False
        return date.today().year - self.died >= PUBLIC_DOMAIN_YEARS


@dataclass
class Artefact:
    """One Commons file, with the licence that decides whether it may be used."""

    title: str
    url: str
    width: int = 0
    height: int = 0
    licence: str = ""
    credit: str = ""

    @property
    def usable(self) -> bool:
        """Public domain only, and big enough to fill a 1080 wide frame.

        Deliberately narrower than what Commons allows. A CC-BY file is usable
        with attribution, and attribution in a caption nobody reads is a
        promise this account cannot keep on four platforms at once, so the
        licence filter is the cheap way to never owe one.
        """
        licence = self.licence.lower()
        public = "public domain" in licence or licence.startswith("pd") or "cc0" in licence
        return public and self.width >= 1080


@dataclass
class Facts:
    """Everything one subject's lookup produced, with the failures as absences."""

    person: Person | None = None
    views: list[int] = field(default_factory=list)
    artefacts: list[Artefact] = field(default_factory=list)


def _get(url: str, params: dict, client: httpx.Client | None, timeout: float = 20.0) -> dict:
    # A caller's client is used rather than entered: a shared session is the
    # whole point of passing one, and `with client or ...` re-enters it, which
    # httpx refuses on the second call with a message about the client rather
    # than about the loop that reused it.
    try:
        if client is not None:
            response = client.get(url, params=params, headers={"User-Agent": USER_AGENT})
        else:
            with httpx.Client(timeout=timeout, follow_redirects=True) as http:
                response = http.get(url, params=params, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("%s did not answer: %s", url, exc)
        return {}


def article_for(title: str, *, client: httpx.Client | None = None) -> tuple[str, str, str]:
    """Resolve a phrase to an article, its Wikidata id and its free page image.

    The search rather than a direct title lookup, because an encyclopedia entry
    is called "Locke on Real Essence" and the article is called "John Locke".
    Redirects are followed by the API itself.
    """
    hit = _get(
        WIKIPEDIA_API,
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": title,
            "gsrlimit": 1,
            "prop": "pageprops|extracts",
            "exintro": 1,
            "explaintext": 1,
            "redirects": 1,
        },
        client,
    )
    pages = (hit.get("query") or {}).get("pages") or {}
    if not pages:
        return "", "", ""
    page = next(iter(pages.values()))
    props = page.get("pageprops") or {}
    return page.get("title", ""), props.get("wikibase_item", ""), props.get("page_image_free", "")


def person_for(qid: str, article: str, portrait: str, *, client: httpx.Client | None = None):
    """The Wikidata half. None when the subject is not a human.

    A topic is not an episode. The format is a person, a thing they wrote and a
    thing they then did about it, so an entry about supervenience has nothing
    for it however often the entry is revised.
    """
    if not qid:
        return None
    data = _get(
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "format": "json",
            "ids": qid,
            "props": "claims|labels|descriptions",
            "languages": "en",
        },
        client,
    )
    entity = ((data.get("entities") or {}).get(qid)) or {}
    claims = entity.get("claims") or {}

    def ids(prop: str) -> list[str]:
        out = []
        for claim in claims.get(prop, []):
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
            if isinstance(value, dict) and value.get("id"):
                out.append(value["id"])
        return out

    def year(prop: str) -> int | None:
        for claim in claims.get(prop, []):
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
            stamp = value.get("time") if isinstance(value, dict) else None
            if stamp:
                # Wikidata stamps look like +1592-09-13T00:00:00Z, and a
                # negative year is a BCE date, which int() reads correctly.
                try:
                    return int(stamp[: stamp.index("-", 1)])
                except ValueError:
                    continue
        return None

    if HUMAN not in ids("P31"):
        return None

    files = claims.get("P18", [])
    image = ""
    if files:
        value = ((files[0].get("mainsnak") or {}).get("datavalue") or {}).get("value")
        image = value if isinstance(value, str) else ""

    return Person(
        qid=qid,
        label=((entity.get("labels") or {}).get("en") or {}).get("value", article),
        article=article,
        description=((entity.get("descriptions") or {}).get("en") or {}).get("value", ""),
        born=year("P569"),
        died=year("P570"),
        portrait=image or portrait,
        works=tuple(ids("P800")),
    )


def pageviews(article: str, *, days: int = 60, client: httpx.Client | None = None) -> list[int]:
    """Daily views for an article, oldest first.

    Wikimedia's own metric, which lags about a day, so the window ends
    yesterday rather than today and a run at 02:00 does not read a zero.
    """
    if not article:
        return []
    end = datetime.now(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=days)
    title = article.replace(" ", "_")
    url = (
        f"{PAGEVIEWS}/en.wikipedia/all-access/user/{title}/daily/"
        f"{start:%Y%m%d}/{end:%Y%m%d}"
    )
    data = _get(url, {}, client)
    return [int(item.get("views", 0)) for item in data.get("items", [])]


def velocity(views: list[int], *, window: int = 7) -> float:
    """Recent daily views against the baseline before them.

    The same shape as star velocity and for the same reason: a big number is a
    famous subject, and this account's ranking is about what moved. 1.0 is
    "being read at its usual rate", so the interesting range is above it.
    """
    if len(views) < window * 2:
        return 0.0
    recent = sum(views[-window:]) / window
    baseline = sum(views[:-window]) / len(views[:-window])
    return recent / baseline if baseline else 0.0


def artefacts(
    files: list[str], *, client: httpx.Client | None = None, limit: int = 12
) -> list[Artefact]:
    """Resolve Commons filenames to usable images.

    One call for every file, because `imageinfo` takes a title list and the
    licence lives in `extmetadata`, which is the field that decides whether the
    format may use the picture at all.
    """
    names = [f if f.startswith("File:") else f"File:{f}" for f in files if f][:limit]
    if not names:
        return []
    data = _get(
        COMMONS_API,
        {
            "action": "query",
            "format": "json",
            "titles": "|".join(names),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 2000,
        },
        client,
    )
    # **Restored to the order asked for.** The API answers with a page map in
    # its own order, so the portrait a caller deliberately put first came back
    # somewhere in the middle, and the first live render opened on a map of
    # Canaan rather than on the printing manual the script was about.
    order = {name.lower(): i for i, name in enumerate(names)}
    out = []
    for page in sorted(
        ((data.get("query") or {}).get("pages") or {}).values(),
        key=lambda page: order.get(str(page.get("title", "")).lower(), len(order)),
    ):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        out.append(
            Artefact(
                title=page.get("title", ""),
                url=info.get("thumburl") or info.get("url", ""),
                width=int(info.get("thumbwidth") or info.get("width") or 0),
                height=int(info.get("thumbheight") or info.get("height") or 0),
                licence=(meta.get("LicenseShortName") or {}).get("value", ""),
                credit=(meta.get("Artist") or {}).get("value", ""),
            )
        )
    return out


def category_files(
    article: str, *, client: httpx.Client | None = None, limit: int = 40
) -> list[str]:
    """Every file in the subject's Commons category, unranked.

    Unranked on purpose. This returns coats of arms, plaques and statues
    alongside the manuscript pages, and picking the four an episode is cut from
    is the open problem, not something a filename can answer.
    """
    data = _get(
        COMMONS_API,
        {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{article}",
            "cmtype": "file",
            "cmlimit": limit,
        },
        client,
    )
    return [m["title"] for m in ((data.get("query") or {}).get("categorymembers") or [])]
