"""One original account, three destinations, and a panel that says so.

`accounts` holds one row per platform, so an identity posting to Instagram,
YouTube and TikTok is three rows. Nothing recorded that they were the same
identity, which was invisible at one and unusable at two: the switcher offered
`thenightlybuild` three times, told apart by a twelve pixel icon, and a second
identity would have made it six chips reading the same word.

`brand` is that grouping, and it is the pipeline's `--account <name>`. The two
sides never talk about it, so it is a label rather than a foreign key: a typo
puts a board in the wrong group, where a wrong `account_id` would publish to
the wrong audience.

The end this is built for is many identities, each posting one video a day to
all three platforms. So the assertions here are mostly about what happens at
two, which is where every shortcut that works at one falls over.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db
from gateway.app import create_app
from tests.gateway_harness import FakeMeta, settings

ADMIN_TOKEN = "test-admin-token-0123456789abcdef"

# Two identities. The second deliberately does not share a handle spelling with
# the first, because the grouping has to survive the day an identity cannot get
# the same name on all three platforms.
NIGHT_IG = "17841400000000001"
NIGHT_YT = "UCq0Ff3lJ7dK2sWnEv8mXtLp"
NIGHT_TT = "_000TikTokOpenIdLooksLikeThis001"
DAWN_IG = "17841400000000002"
DAWN_YT = "UCq0Ff3lJ7dK2sWnEv8mXtLq"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path, admin_enabled=True, admin_trust_proxy_auth=True)


@pytest.fixture
async def client(cfg):
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://gateway"
            ) as http,
        ):
            yield http, app


async def seed(conn, *, second: bool = False) -> None:
    """One identity on three platforms, and optionally a second on two."""
    await db.upsert_account(
        conn, account_id=NIGHT_IG, access_token="t", username="thenightlybuild"
    )
    await db.upsert_account(
        conn, account_id=NIGHT_YT, access_token="", username="@thenightlybuild",
        platform=db.PLATFORM_YOUTUBE,
    )
    await db.upsert_account(
        conn, account_id=NIGHT_TT, access_token="", username="@thenightlybuild",
        platform=db.PLATFORM_TIKTOK,
    )
    if second:
        await db.upsert_account(
            conn, account_id=DAWN_IG, access_token="t", username="dawnpatrol",
            brand="dawnpatrol",
        )
        await db.upsert_account(
            conn, account_id=DAWN_YT, access_token="", username="@dawn.patrol",
            platform=db.PLATFORM_YOUTUBE, brand="dawnpatrol",
        )


def marks(body: str, platform: str) -> int:
    return body.count(f'class="mark {platform}')


# --- Which rows belong together ----------------------------------------------


def test_a_handle_is_the_same_identity_however_it_is_spelled():
    """Instagram stores it bare and the other two store it with an @, so an
    identity that groups on the raw string is two identities."""
    assert db.brand_of("thenightlybuild", "x") == "thenightlybuild"
    assert db.brand_of("@thenightlybuild", "x") == "thenightlybuild"
    assert db.brand_of("@TheNightlyBuild", "x") == "thenightlybuild"


def test_a_destination_with_no_handle_is_its_own_group():
    """Falling back to the empty string would put every unnamed row in one
    group, which is the opposite of what an unknown means."""
    assert db.brand_of("", "UC123") == "UC123"


async def test_three_destinations_of_one_identity_group_together(client):
    http, app = client
    await seed(app.state.db)

    rows = await db.all_accounts(app.state.db, platform=None)

    assert {row["brand"] for row in rows} == {"thenightlybuild"}


async def test_an_explicit_brand_beats_the_handle(client):
    """The day an identity cannot get the same name on all three platforms,
    which is the normal case rather than the exception."""
    http, app = client
    await seed(app.state.db, second=True)

    rows = {r["account_id"]: r["brand"] for r in await db.all_accounts(app.state.db, platform=None)}

    assert rows[DAWN_YT] == "dawnpatrol", "the handle says dawn.patrol; the brand says otherwise"
    assert rows[DAWN_IG] == "dawnpatrol"


async def test_re_authorising_keeps_the_grouping(client):
    """Re-running OAuth must not silently regroup a destination, for the same
    reason it must not re-enable a paused one."""
    http, app = client
    await seed(app.state.db, second=True)

    await db.upsert_account(
        app.state.db, account_id=DAWN_YT, access_token="", username="@dawn.patrol",
        platform=db.PLATFORM_YOUTUBE,
    )

    assert (await db.get_account(app.state.db, DAWN_YT))["brand"] == "dawnpatrol"


async def test_a_correction_is_possible(client):
    """A label with no way to fix it is a typo that lives forever."""
    http, app = client
    await seed(app.state.db)

    await db.upsert_account(
        app.state.db, account_id=NIGHT_TT, access_token="", username="@thenightlybuild",
        platform=db.PLATFORM_TIKTOK, brand="somethingelse",
    )

    assert (await db.get_account(app.state.db, NIGHT_TT))["brand"] == "somethingelse"


async def test_boards_arrive_grouped_and_in_platform_order(client):
    """Sorted in the database rather than in five templates, so the switcher
    and the boards cannot disagree about the order."""
    http, app = client
    await seed(app.state.db, second=True)

    rows = await db.all_accounts(app.state.db, platform=None)

    assert [(r["brand"], r["platform"]) for r in rows] == [
        ("dawnpatrol", "instagram"),
        ("dawnpatrol", "youtube"),
        ("thenightlybuild", "instagram"),
        ("thenightlybuild", "youtube"),
        ("thenightlybuild", "tiktok"),
    ]


# --- The picker and the brand pages -------------------------------------------


def sidebar(body: str) -> str:
    return body.split("<aside", 1)[1].split("</aside>", 1)[0]


def page(body: str) -> str:
    return body.split("</aside>", 1)[-1]


async def test_the_picker_is_one_entry_per_identity(client):
    """Not one per destination. Five destinations is two entries."""
    http, app = client
    await seed(app.state.db, second=True)

    aside = sidebar((await http.get("/admin/")).text)

    assert aside.count("/admin/b/thenightlybuild/\"") == 1
    assert aside.count("/admin/b/dawnpatrol/\"") == 1


async def test_a_brand_page_holds_every_platform_it_posts_to(client):
    http, app = client
    await seed(app.state.db, second=True)

    main = page((await http.get("/admin/b/thenightlybuild/")).text)

    assert main.count('class="platform ') == 3
    assert "dawnpatrol" not in main


async def test_today_lists_each_identity_once(client):
    http, app = client
    await seed(app.state.db, second=True)

    main = page((await http.get("/admin/")).text)

    assert main.count('nm">thenightlybuild<small>3 destinations') == 1
    assert main.count('nm">dawnpatrol<small>2 destinations') == 1


async def test_the_brand_survives_navigation(client):
    """The brand is in the path, so a link built for another page of it cannot
    drop it the way a forgotten query string did."""
    http, app = client
    await seed(app.state.db, second=True)

    body = (await http.get("/admin/b/dawnpatrol/library")).text

    assert "/admin/b/dawnpatrol/performance" in body
    assert "/admin/b/dawnpatrol/schedule" in body


async def test_an_unknown_brand_lands_on_today(client):
    http, app = client
    await seed(app.state.db, second=True)

    response = await http.get("/admin/b/gone/library")

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/")


async def test_an_old_destination_link_lands_inside_its_brand(client):
    """`?account=` picked one destination. It opens that brand, narrowed to
    that platform, which is the closest page to what the link meant."""
    http, app = client
    await seed(app.state.db, second=True)

    response = await http.get(f"/admin/posts?account={NIGHT_YT}")

    location = response.headers["location"]
    assert location.endswith("/admin/b/thenightlybuild/library?platform=youtube")


# --- What each platform's page says -------------------------------------------


async def test_performance_says_why_youtube_is_not_a_skip_rate(client):
    """Shown in its own terms rather than refused. The rule was that skip rate
    never shares a table with anything else, not that the others go unshown."""
    http, app = client
    await seed(app.state.db)

    # Whitespace collapsed, because the sentences wrap in the template.
    body = " ".join((await http.get("/admin/b/thenightlybuild/performance")).text.split())

    assert "average view percentage" in body
    assert "no retention, watch time or completion metric" in body
    assert 'class="platform instagram"' in body


async def test_the_dm_funnel_is_absent_where_it_cannot_exist(client):
    """The keyword mechanic is comments and private replies, which is one
    platform. A YouTube card carrying "0 asked, 0 links sent, keyword send"
    reported a mechanic that was never available there as one that failed."""
    http, app = client
    await seed(app.state.db)
    conn = app.state.db
    for account_id in (NIGHT_IG, NIGHT_YT):
        queued_id = await db.enqueue_post(
            conn, account_id=account_id, video_name=f"{account_id}.mp4", cover_name=None,
            caption="c", keyword="SEND", link="https://github.com/a/b", approved=True,
        )
        await db.mark_queue_published(conn, queued_id, media_id=f"m-{account_id}", permalink="")

    instagram = page((await http.get(
        "/admin/b/thenightlybuild/library?platform=instagram"
    )).text)
    youtube = page((await http.get("/admin/b/thenightlybuild/library?platform=youtube")).text)

    assert "links sent" in instagram
    assert "links sent" not in youtube
    assert "keyword" not in youtube
