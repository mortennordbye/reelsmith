"""LinkedIn's analytics export, read into the boards.

The fixture is built here rather than checked in: a real export names the
person and their employer, and this repo is public. It copies the real shape,
including the two side by side lists on TOP POSTS and the strings where numbers
should be.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from gateway import db, linkedin_export
from gateway.app import create_app
from tests.gateway_harness import FakeMeta, settings

PERSON = "abc123XYZ"
ADMIN_TOKEN = "test-admin-token-0123456789abcdef"
URL = "https://www.linkedin.com/posts/someone_{slug}-{kind}-{n}-Ab1c"
CILIUM = URL.format(slug="locking-down-your-cluster", kind="share", n="7462216974467629056")
JOB = URL.format(slug="im-happy-to-share", kind="ugcPost", n="7511744635896119296")
OLD = URL.format(slug="old-one", kind="ugcPost", n="7097136639558246400")


def _col(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _sheet(rows: list[list[str | None]]) -> str:
    body = []
    for y, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_col(x)}{y}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for x, v in enumerate(row) if v is not None
        )
        body.append(f'<row r="{y}">{cells}</row>')
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def workbook(sheets: dict[str, list[list[str | None]]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        entries, rels = [], []
        for i, (name, rows) in enumerate(sheets.items(), start=1):
            entries.append(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>')
            rels.append(
                f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            )
            book.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(rows))
        book.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{''.join(entries)}</sheets></workbook>",
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(rels)}</Relationships>",
        )
    return buffer.getvalue()


EXPORT = {
    "DISCOVERY": [
        ["Overall Performance", "10/16/2025 - 10/15/2026"],
        ["Impressions", "26071"],
        ["Members reached", "6780"],
    ],
    "ENGAGEMENT": [
        ["Date", "Impressions", "Engagements"],
        ["10/14/2026", "300", "12"],
        ["10/15/2026", "120", "4"],
    ],
    "TOP POSTS": [
        ["Maximum of 50 posts available to include in this list"],
        [],
        ["Post URL", "Post Publish Date", "Engagements", "", "Post URL", "Post Publish Date",
         "Impressions"],
        [JOB, "10/2/2026", "124", "", CILIUM, "5/19/2026", "3107"],
        [CILIUM, "5/19/2026", "61", "", JOB, "10/2/2026", "2474"],
        [None, None, None, None, OLD, "8/15/2023", "8"],
    ],
    "FOLLOWERS": [
        ["Total followers on 10/15/2026", "703"],
        [],
        ["Date", "New followers"],
        ["10/14/2026", "3"],
        ["10/15/2026", "1"],
    ],
    "AUDIENCE DEMOGRAPHICS": [["Top Demographics", "Value", "Percentage"]],
}


def test_the_export_is_read_whole():
    export = linkedin_export.parse(workbook(EXPORT))

    assert (export.start, export.end) == (date(2025, 10, 16), date(2026, 10, 15))
    assert (export.impressions, export.members_reached, export.followers) == (26071, 6780, 703)
    assert export.days[date(2026, 10, 14)] == {
        "impressions": 300, "engagements": 12, "new_followers": 3
    }
    posts = {p.urn: p for p in export.posts}
    cilium = posts["urn:li:share:7462216974467629056"]
    assert (cilium.impressions, cilium.engagements) == (3107, 61)
    assert cilium.title == "Locking down your cluster"
    old = posts["urn:li:ugcPost:7097136639558246400"]
    assert (old.impressions, old.engagements) == (8, None)


def test_a_file_that_is_not_the_export_is_refused_in_words():
    with pytest.raises(linkedin_export.ExportError, match="not an XLSX"):
        linkedin_export.parse(b"hello")
    with pytest.raises(linkedin_export.ExportError, match="TOP POSTS"):
        linkedin_export.parse(workbook({"DISCOVERY": EXPORT["DISCOVERY"]}))


@pytest.fixture
async def conn(tmp_path):
    connection = await db.connect(tmp_path / "gateway.sqlite3")
    await db.upsert_account(
        connection, account_id=PERSON, access_token="t", username="Somebody",
        platform=db.PLATFORM_LINKEDIN, brand="me",
    )
    yield connection
    await connection.close()


async def test_a_post_the_gateway_made_is_matched_on_its_id(conn):
    """The URL carries the id the Posts API returned, so no date guessing."""
    queued = await db.enqueue_post(
        conn, account_id=PERSON, video_name="", cover_name=None, caption="Cilium post",
        keyword="send", link="https://blog.example.test/cilium/", approved=True,
    )
    await db.mark_queue_published(
        conn, queued, media_id="urn:li:share:7462216974467629056", permalink=CILIUM
    )

    stored = await linkedin_export.store(conn, PERSON, linkedin_export.parse(workbook(EXPORT)))

    assert (stored.matched, stored.added) == (1, 2)
    readings = await db.latest_insights(conn, PERSON, platform=db.PLATFORM_LINKEDIN)
    cilium = readings["urn:li:share:7462216974467629056"]
    assert cilium["views"] == 3107
    assert db.extra_of(cilium) == {"impressions": 3107, "engagements": 61}


async def test_posts_made_by_hand_join_the_board_and_a_second_import_adds_nothing(conn):
    export = linkedin_export.parse(workbook(EXPORT))
    moment = datetime(2026, 10, 15, 12, tzinfo=UTC)
    await linkedin_export.store(conn, PERSON, export, moment=moment)
    again = await linkedin_export.store(conn, PERSON, export, moment=moment)

    assert (again.matched, again.added) == (3, 0)
    rows = await db.published_media(conn, PERSON, limit=100)
    assert len(rows) == 3
    job = next(r for r in rows if r["media_id"] == "urn:li:ugcPost:7511744635896119296")
    assert job["recipe"] == linkedin_export.IMPORTED
    assert job["published_at"].startswith("2026-10-02")
    assert job["permalink"] == JOB


async def test_an_absent_figure_stays_absent(conn):
    """The oldest post is only on the impressions list. Its engagements are
    unknown, which a zero would misreport as nobody engaging."""
    await linkedin_export.store(conn, PERSON, linkedin_export.parse(workbook(EXPORT)))
    readings = await db.latest_insights(conn, PERSON, platform=db.PLATFORM_LINKEDIN)
    assert db.extra_of(readings["urn:li:ugcPost:7097136639558246400"]) == {"impressions": 8}


async def test_followers_are_known_only_on_the_export_date(conn):
    await linkedin_export.store(conn, PERSON, linkedin_export.parse(workbook(EXPORT)))
    series = await db.account_insights_series(conn, PERSON)
    by_day = {row["fetched_on"]: row for row in series}
    assert by_day["2026-10-15"]["followers"] == 703
    assert by_day["2026-10-14"]["followers"] is None
    assert db.extra_of(by_day["2026-10-14"])["day"]["new_followers"] == 3


async def test_the_panel_imports_the_file_and_shows_the_posts(tmp_path):
    cfg = settings(tmp_path, admin_enabled=True, admin_token=ADMIN_TOKEN)
    async with FakeMeta().client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://gateway"
            ) as http,
        ):
            await db.upsert_account(
                app.state.db, account_id=PERSON, access_token="t", username="Somebody",
                platform=db.PLATFORM_LINKEDIN, brand="me",
            )
            await http.post("/admin/login", data={"token": ADMIN_TOKEN})

            response = await http.post(
                "/admin/b/me/performance/import",
                files={"file": ("export.xlsx", workbook(EXPORT), "application/octet-stream")},
                follow_redirects=True,
            )

            assert response.status_code == 200
            assert "3 added as imported" in response.text
            assert "Locking down your cluster" in response.text
            assert "3,107" in response.text

            refused = await http.post(
                "/admin/b/me/performance/import",
                files={"file": ("x.xlsx", b"not a workbook", "application/octet-stream")},
            )
            assert refused.status_code == 400
