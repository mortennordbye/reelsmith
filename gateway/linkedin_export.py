"""LinkedIn's own analytics export, read into the insights tables.

Post statistics through the API need `r_member_postAnalytics`, which is not
self-serve, so the numbers come from the file LinkedIn hands any member:
Analytics, Content, Export, as XLSX. Its shape, read off a real one on
2026-10-15:

| sheet | holds |
|---|---|
| DISCOVERY | the period's impressions and members reached |
| ENGAGEMENT | impressions and engagements per day |
| TOP POSTS | up to 50 posts, two lists side by side: by engagements, by impressions |
| FOLLOWERS | the total on the export date, then new followers per day |
| AUDIENCE / CONTENT DEMOGRAPHICS | percentages, not read here |

**Per post it gives impressions and engagements and nothing else**: no
reactions, comments or reposts apart, and no reach. So those columns stay
absent rather than zero, and engagements lives in `extra` under its own name.

**The post URL carries the id the Posts API returned**, as
`...-share-<id>-...` or `...-ugcPost-<id>-...`, so a post the gateway
published is matched on that id rather than on its date. A post made by hand
has no queue row, and gets one in `published` state so it shows on the boards
beside the rest, marked by an empty `video_name` and a `recipe` of `imported`.

Read with the standard library rather than a spreadsheet package: the file is
a zip of a few XML sheets of strings, and the gateway image carries no
dependency it does not need.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree

import aiosqlite

from gateway import db

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_POST_ID = re.compile(r"-(share|ugcPost|activity)-(\d{10,})")
IMPORTED = "imported"


class ExportError(ValueError):
    """A file that is not the export this reads, said in words."""


@dataclass
class PostStats:
    url: str
    urn: str
    published: date
    impressions: int | None = None
    engagements: int | None = None

    @property
    def number(self) -> str:
        return self.urn.rsplit(":", 1)[-1]

    @property
    def title(self) -> str:
        """What the URL's slug says, as the nearest thing to a hook it has."""
        tail = self.url.rsplit("/", 1)[-1]
        slug = tail.split("_", 1)[-1] if "_" in tail else tail
        slug = _POST_ID.split(slug)[0]
        words = slug.replace("-", " ").strip()
        return words[:1].upper() + words[1:]


@dataclass
class Export:
    start: date
    end: date
    impressions: int | None = None
    members_reached: int | None = None
    followers: int | None = None
    posts: list[PostStats] = field(default_factory=list)
    # date -> {"impressions", "engagements", "new_followers"}
    days: dict[date, dict[str, int]] = field(default_factory=dict)


# --- Reading the workbook -----------------------------------------------------


def _sheets(payload: bytes) -> dict[str, list[list[str]]]:
    try:
        book = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ExportError("That is not an XLSX file.") from exc
    with book:
        names = set(book.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
                      for si in root.findall("m:si", _NS)]
        workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", _NS)}
        out: dict[str, list[list[str]]] = {}
        for sheet in workbook.findall("m:sheets/m:sheet", _NS):
            target = targets.get(sheet.get(f"{{{_NS['r']}}}id"), "")
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if path in names:
                out[str(sheet.get("name"))] = _rows(book.read(path), shared)
        return out


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref)
    index = 0
    for ch in letters.group(0) if letters else "A":
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _rows(xml: bytes, shared: list[str]) -> list[list[str]]:
    rows = []
    for row in ElementTree.fromstring(xml).iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", _NS):
            kind = c.get("t")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            else:
                raw = c.findtext("m:v", default="", namespaces=_NS)
                value = shared[int(raw)] if kind == "s" and raw.isdigit() else raw
            cells[_column(c.get("r") or "A")] = value.strip()
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _int(text: str) -> int | None:
    cleaned = text.replace(",", "").replace(" ", "")
    return int(cleaned) if cleaned.isdigit() else None


def _date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def parse(payload: bytes) -> Export:
    sheets = {name.upper(): rows for name, rows in _sheets(payload).items()}
    missing = {"DISCOVERY", "TOP POSTS"} - sheets.keys()
    if missing:
        raise ExportError(
            "This does not look like LinkedIn's content analytics export; it has no "
            + " or ".join(sorted(missing)) + " sheet."
        )

    period = next((r[1] for r in sheets["DISCOVERY"] if r and r[0] == "Overall Performance"), "")
    start, _, end = period.partition(" - ")
    export = Export(start=_date(start) or date.min, end=_date(end) or date.today())
    for row in sheets["DISCOVERY"]:
        if len(row) > 1 and row[0] == "Impressions":
            export.impressions = _int(row[1])
        elif len(row) > 1 and row[0] == "Members reached":
            export.members_reached = _int(row[1])

    for row in sheets.get("ENGAGEMENT", []):
        day = _date(row[0]) if row else None
        if day and len(row) > 2:
            export.days.setdefault(day, {}).update(
                impressions=_int(row[1]) or 0, engagements=_int(row[2]) or 0
            )
    for row in sheets.get("FOLLOWERS", []):
        if row and row[0].startswith("Total followers") and len(row) > 1:
            export.followers = _int(row[1])
        day = _date(row[0]) if row else None
        if day and len(row) > 1:
            export.days.setdefault(day, {})["new_followers"] = _int(row[1]) or 0

    # Two lists side by side, each URL, date, figure, separated by a blank
    # column. Read by header position, since the header row says which is which.
    posts: dict[str, PostStats] = {}
    rows = sheets["TOP POSTS"]
    header = next((i for i, r in enumerate(rows) if r and r[0] == "Post URL"), None)
    if header is None:
        raise ExportError("The TOP POSTS sheet has no Post URL header.")
    head = rows[header]
    groups = [i for i, name in enumerate(head) if name == "Post URL"]
    for row in rows[header + 1:]:
        for at in groups:
            if len(row) < at + 3 or not row[at]:
                continue
            url, published = row[at], _date(row[at + 1])
            match = _POST_ID.search(url)
            if not match or published is None:
                continue
            post = posts.setdefault(
                url,
                PostStats(url=url, urn=f"urn:li:{match.group(1)}:{match.group(2)}",
                          published=published),
            )
            metric = head[at + 2].strip().lower()
            if metric == "impressions":
                post.impressions = _int(row[at + 2])
            elif metric == "engagements":
                post.engagements = _int(row[at + 2])
    export.posts = sorted(posts.values(), key=lambda p: p.published)
    return export


# --- Storing it ---------------------------------------------------------------


@dataclass
class Stored:
    matched: int = 0
    added: int = 0
    days: int = 0

    def summary(self) -> str:
        return (
            f"{self.matched} posts matched to the queue, {self.added} added as imported, "
            f"{self.days} days of totals"
        )


async def store(
    conn: aiosqlite.Connection, account_id: str, export: Export, *, moment: datetime | None = None
) -> Stored:
    """Write one export's readings. Importing the same file twice changes nothing.

    A post's reading is dated the export's last day, so a later export of the
    same post is a later reading rather than an overwrite, which is how the
    other platforms' sweeps keep a history.
    """
    moment = moment or db.now()
    stored = Stored()
    on = export.end.isoformat()

    published = await db.published_media(conn, account_id, limit=10_000)
    by_number = {
        str(row["media_id"] or "").rsplit(":", 1)[-1]: row
        for row in published if row["media_id"]
    }
    for post in export.posts:
        row = by_number.get(post.number)
        if row is not None:
            stored.matched += 1
            media_id = str(row["media_id"])
        else:
            media_id = post.urn
            await db.record_external_post(
                conn,
                account_id=account_id,
                media_id=media_id,
                permalink=post.url,
                published_at=datetime.combine(post.published, datetime.min.time(), moment.tzinfo),
                hook=post.title,
                recipe=IMPORTED,
            )
            by_number[post.number] = {"media_id": media_id}
            stored.added += 1
        # Both in `extra` under the export's names, which is what the board's
        # columns read, so a post on only one of the two lists shows a blank
        # rather than a zero in the other. `views` too when it is known, for
        # sorting by views on the Library page.
        metrics: dict[str, Any] = {}
        extra: dict[str, int] = {}
        if post.impressions is not None:
            metrics["views"] = extra["impressions"] = post.impressions
        if post.engagements is not None:
            extra["engagements"] = post.engagements
        await db.record_insights(
            conn, media_id=media_id, account_id=account_id, metrics=metrics, on=on,
            moment=moment, platform=db.PLATFORM_LINKEDIN, extra=extra,
        )

    for day, totals in sorted(export.days.items()):
        last = day == export.end
        await db.record_account_insights(
            conn,
            account_id=account_id,
            platform=db.PLATFORM_LINKEDIN,
            # The total is only known on the export's own date. Earlier days
            # are null rather than a figure worked backwards, since new
            # followers are counted without the ones who left.
            followers=export.followers if last else None,
            extra={"day": {"on": day.isoformat(), **totals}},
            on=day.isoformat(),
            moment=moment,
        )
        stored.days += 1
    return stored
