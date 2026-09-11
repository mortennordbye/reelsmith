"""The queue API and the control panel.

Two things are worth testing here. That every control changes exactly the state
it claims to and refuses the transitions that would make the record wrong, and
that none of them can be reached by someone who has not signed in. The second
matters because this panel publishes to a real account on a service that has to
be publicly reachable for Meta to fetch media from it.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from gateway import db, schedule
from gateway.app import create_app
from gateway.config import GatewayConfigError
from tests.gateway_harness import (
    ACCOUNT,
    API_TOKEN,
    CHANNEL,
    PAGE_ID,
    FakeMeta,
    settings,
)

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
LINK = "https://github.com/astral-sh/uv"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
# Long enough to pass the minimum the config enforces; a short token on a
# route reachable from the internet is a password someone will guess.
ADMIN_TOKEN = "test-admin-token-0123456789abcdef"


@pytest.fixture
def cfg(tmp_path):
    return settings(
        tmp_path, scheduler_enabled=True, admin_enabled=True, admin_token=ADMIN_TOKEN
    )


# https, because the session cookie is Secure and a browser would not send it
# back over http. Testing against http would quietly exercise a weaker cookie
# than the one that ships.
BASE = "https://gateway"


@pytest.fixture
async def client(cfg):
    """A signed-in panel, signed in the way a person would be.

    Injecting the cookie directly would skip the login route and hide any
    mistake in how it is set. The auth itself is exercised further down.
    """
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http,
        ):
            await db.upsert_account(
                app.state.db, account_id=ACCOUNT, access_token="tok", username="nightly"
            )
            await http.post("/admin/login", data={"token": ADMIN_TOKEN})
            yield http, app


async def upload(http, name: str = "out.mp4") -> str:
    # The payload varies with the name because the stored filename carries a
    # digest of the bytes, so two uploads of identical content are deliberately
    # the same file. Tests that want two files need two contents.
    response = await http.post(
        "/api/media", headers=AUTH,
        files={"file": (name, MP4 + name.encode(), "video/mp4")},
        data={"slug": "astral-sh-uv"},
    )
    return response.json()["name"]


async def queue(http, *, approved: bool = False, **overrides) -> dict:
    video = overrides.pop("video_name", None) or await upload(http)
    body = {
        "account_id": ACCOUNT, "video_name": video, "caption": "hello",
        "keyword": "UV", "link": LINK, "repo_full_name": "astral-sh/uv",
        "approved": approved, **overrides,
    }
    response = await http.post("/api/queue", headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- The queue API --------------------------------------------------------


async def test_queue_needs_the_bearer_token(client):
    http, _ = client
    response = await http.post("/api/queue", json={})
    assert response.status_code == 401


async def test_a_post_arrives_as_a_draft_by_default(client):
    """Arming is a separate act, because the failure mode of the other default
    is a bad video posting itself while nobody is watching."""
    http, _ = client
    assert (await queue(http))["state"] == db.QUEUE_DRAFT


async def test_approved_on_arrival_when_asked(client):
    http, _ = client
    assert (await queue(http, approved=True))["state"] == db.QUEUE_APPROVED


async def test_a_post_pointing_at_a_missing_file_is_refused(client):
    """Otherwise it fails at publish time, days later, with nobody watching."""
    http, _ = client
    response = await http.post("/api/queue", headers=AUTH, json={
        "account_id": ACCOUNT, "video_name": "ghost.mp4", "keyword": "X", "link": LINK,
    })
    assert response.status_code == 400
    assert "ghost.mp4" in response.json()["detail"]


async def test_a_row_may_have_no_link(client):
    """A destination is not always a GitHub project. An account whose subject
    is a book has nothing to link, and the alternative to accepting the empty
    string is a row holding a URL somebody invented for it. The keyword
    mechanic is the only thing that reads the link, and that is dormant and
    Instagram only."""
    http, _ = client
    row = await queue(http, link="", repo_full_name=None)

    assert row["state"] == db.QUEUE_DRAFT


async def test_a_link_that_is_not_a_url_is_still_refused(client):
    """Empty is a claim that there is nothing to link. A bare word is a
    mistake, and it stays a 422."""
    http, _ = client
    video = await upload(http)
    response = await http.post("/api/queue", headers=AUTH, json={
        "account_id": ACCOUNT, "video_name": video, "keyword": "X", "link": "montaigne",
    })
    assert response.status_code == 422


async def test_a_traversing_filename_is_rejected_by_the_model(client):
    http, _ = client
    response = await http.post("/api/queue", headers=AUTH, json={
        "account_id": ACCOUNT, "video_name": "../../gateway.sqlite3",
        "keyword": "X", "link": LINK,
    })
    assert response.status_code == 422


async def test_the_keyword_must_be_one_word(client):
    http, _ = client
    video = await upload(http)
    response = await http.post("/api/queue", headers=AUTH, json={
        "account_id": ACCOUNT, "video_name": video,
        "keyword": "two words", "link": LINK,
    })
    assert response.status_code == 422


async def test_the_recipe_travels_from_the_render_host_to_the_queue(client):
    """The field exists so the numbers can be grouped by what wrote the script.

    Asserted at the API boundary rather than only in the database, because
    every other way of knowing which code made a video has already been tried
    and failed: `recipe.json` sits on whichever machine rendered, and the
    publish date is days off from the render date once the queue is a few deep.
    """
    http, app = client
    await queue(http, recipe="abc1234.deadbeef")

    body = (await http.get("/api/queue", headers=AUTH)).json()

    assert body["queue"][0]["recipe"] == "abc1234.deadbeef"


async def test_a_client_that_sends_no_recipe_is_taken_anyway(client):
    """An older Mac, or `--recover` on a folder made before recipes existed.

    Refusing would strand a video that is already rendered and uploaded, and
    trading a Reel for a metadata label is the wrong way round. Empty says
    "nothing recorded what made this", which is different from claiming the
    current checkout did.
    """
    http, _ = client
    await queue(http)

    body = (await http.get("/api/queue", headers=AUTH)).json()

    assert body["queue"][0]["recipe"] == ""


async def test_listing_the_queue(client):
    http, _ = client
    await queue(http)
    body = (await http.get("/api/queue", headers=AUTH)).json()
    assert [row["state"] for row in body["queue"]] == [db.QUEUE_DRAFT]
    assert body["queue"][0]["repo_full_name"] == "astral-sh/uv"


# --- Covered repos, the cooldown list the Mac reads back ------------------


async def covered(http) -> dict[str, str]:
    response = await http.get("/api/covered", headers=AUTH)
    assert response.status_code == 200, response.text
    return {row["repo_full_name"]: row["covered_at"] for row in response.json()["covered"]}


async def test_covered_needs_the_bearer_token(client):
    http, _ = client
    assert (await http.get("/api/covered")).status_code == 401


async def test_a_draft_already_counts_as_covered(client):
    """The cooldown starts at enqueue, so a post still in the line is committed.
    published_media filters to `published` and would miss exactly this."""
    http, _ = client
    await queue(http)
    assert "astral-sh/uv" in await covered(http)


async def test_a_cancelled_post_is_not_covered(client):
    """Cancelling is the moment --unmark is meant to run. Reporting it as
    covered would fight that by hand on every discovery."""
    http, app = client
    body = await queue(http)
    await db.set_queue_state(app.state.db, body["id"], db.QUEUE_CANCELLED)
    assert await covered(http) == {}


async def test_a_directly_published_post_is_covered_via_its_link(client):
    """--publish registers with `posts` and never touches the queue, so the
    repo has to come back out of the GitHub link."""
    http, app = client
    await db.register_post(
        app.state.db, media_id="1", account_id=ACCOUNT,
        keyword="UV", link="https://github.com/DietrichGebert/ponytail",
    )
    assert "DietrichGebert/ponytail" in await covered(http)


async def test_the_earliest_commitment_wins(client):
    """A repo queued once and published later is one commitment, and the
    cooldown turned on at the first of the two."""
    http, app = client
    await queue(http)
    await db.register_post(
        app.state.db, media_id="2", account_id=ACCOUNT,
        keyword="UV", link=LINK, published_at="2099-01-01T00:00:00+00:00",
    )
    assert (await covered(http))["astral-sh/uv"] < "2099"


async def test_nothing_committed_is_an_empty_list(client):
    http, _ = client
    assert await covered(http) == {}


# --- Rendered repos, the weaker "do not build that twice" list ------------


async def rendered(http) -> dict[str, str]:
    response = await http.get("/api/rendered", headers=AUTH)
    assert response.status_code == 200, response.text
    return {row["repo_full_name"]: row["rendered_at"] for row in response.json()["rendered"]}


async def test_rendered_needs_the_bearer_token(client):
    http, _ = client
    assert (await http.get("/api/rendered")).status_code == 401
    assert (await http.post("/api/rendered", json={"repo_full_name": "a/b"})).status_code == 401


async def test_a_recorded_render_comes_back(client):
    http, _ = client
    body = {"repo_full_name": "firecrawl/anydoc", "run_folder": "2026-08-08/firecrawl-anydoc"}
    assert (await http.post("/api/rendered", json=body, headers=AUTH)).status_code == 200
    assert "firecrawl/anydoc" in await rendered(http)


async def test_a_render_is_not_a_commitment(client):
    """The whole reason these are two lists. `/api/covered` is merged into the
    Mac's cooldown store, and a video nobody has watched yet must not start a
    30 day block on its repo."""
    http, _ = client
    await http.post("/api/rendered", json={"repo_full_name": "firecrawl/anydoc"}, headers=AUTH)
    assert await covered(http) == {}


async def test_rendering_the_same_repo_twice_keeps_the_first_date(client):
    """A repeat render is the thing this table exists to prevent, so when it
    happens the honest date is the one that already made the repo redundant."""
    http, _ = client
    first = {"repo_full_name": "firecrawl/anydoc", "run_folder": "2026-08-07/firecrawl-anydoc"}
    await http.post("/api/rendered", json=first, headers=AUTH)
    was = (await rendered(http))["firecrawl/anydoc"]

    second = {"repo_full_name": "firecrawl/anydoc", "run_folder": "2026-08-08/firecrawl-anydoc"}
    await http.post("/api/rendered", json=second, headers=AUTH)

    listed = await http.get("/api/rendered", headers=AUTH)
    rows = listed.json()["rendered"]
    assert len(rows) == 1, "an upsert, not a second row"
    assert rows[0]["rendered_at"] == was
    assert rows[0]["run_folder"] == "2026-08-08/firecrawl-anydoc", "but the folder is the latest"


async def test_forgetting_a_render_unblocks_the_repo(client):
    """--unmark is the undo. Rendering is the one step meant to be free to
    throw away, so the record of it has to be as cheap to delete."""
    http, _ = client
    await http.post("/api/rendered", json={"repo_full_name": "firecrawl/anydoc"}, headers=AUTH)
    response = await http.delete("/api/rendered/firecrawl/anydoc", headers=AUTH)

    assert response.status_code == 200, response.text
    assert "no longer recorded" in response.json()["detail"]
    assert await rendered(http) == {}


async def test_forgetting_a_render_that_never_happened_is_not_an_error(client):
    """--unmark clears the cooldown and calls this in one breath, and most
    repos it is run on were marked by hand and never rendered here."""
    http, _ = client
    response = await http.delete("/api/rendered/some/repo", headers=AUTH)

    assert response.status_code == 200, response.text
    assert "was not recorded" in response.json()["detail"]


async def test_a_render_from_before_the_account_existed_still_lists(client):
    """A blank owner matches every account. Filtering it out would hide exactly
    the early records the table was added to keep."""
    http, app = client
    await db.record_rendered(app.state.db, repo_full_name="astral-sh/uv", account_id="")

    rows = await db.rendered_repos_list(app.state.db, ACCOUNT)
    assert [row["repo_full_name"] for row in rows] == ["astral-sh/uv"]


# --- Media retention ------------------------------------------------------


async def test_uploading_does_not_prune_a_queued_video(client, cfg, monkeypatch):
    """Queue ten posts at one a day and the last three are older than the TTL
    before their turn. Age alone is the wrong rule once posts are scheduled."""
    http, app = client
    from gateway import api

    name = await upload(http, "keeper.mp4")
    await queue(http, video_name=name)
    # Everything on disk is now well past the TTL.
    monkeypatch.setattr(api, "_MEDIA_TTL_DAYS", -1)

    await upload(http, "newer.mp4")
    assert (cfg.covers_dir / name).is_file()


async def test_an_unreferenced_file_is_still_pruned(client, cfg, monkeypatch):
    http, _ = client
    from gateway import api

    orphan = await upload(http, "orphan.mp4")
    monkeypatch.setattr(api, "_MEDIA_TTL_DAYS", -1)

    await upload(http, "newer.mp4")
    assert not (cfg.covers_dir / orphan).is_file()


# --- The panel ------------------------------------------------------------


SCHEDULE = "/admin/b/nightly/schedule"
LIBRARY = "/admin/b/nightly/library"
PERFORMANCE = "/admin/b/nightly/performance"
SUBJECTS = "/admin/b/nightly/subjects"
PAGES = [
    "/admin/", "/admin/calendar", "/admin/destinations", "/admin/system",
    "/admin/search?q=uv", "/admin/settings",
    "/admin/b/nightly/", SCHEDULE, LIBRARY, PERFORMANCE, SUBJECTS, "/admin/b/nightly/setup",
]


@pytest.mark.parametrize("path", PAGES)
async def test_the_pages_render(client, path):
    """With a queued video on every page, because a page with nothing on it
    never reaches the code that breaks."""
    http, _ = client
    await queue(http, approved=True, hook="It reads 40 pages of a PDF in a single pass")

    response = await http.get(path)

    assert response.status_code == 200, response.text[:400]
    assert "reelsmith" in response.text


async def test_the_pages_render_with_numbers_on_two_platforms(client):
    """One render published to Instagram and YouTube with a reading on each,
    which is the shape every brand page exists to show and the one an empty
    fixture never exercises."""
    http, app = client
    conn = app.state.db
    await db.upsert_account(
        conn, account_id=CHANNEL, access_token="", username="@nightly",
        platform=db.PLATFORM_YOUTUBE, brand="nightly",
    )
    await db.add_slot(conn, account_id=ACCOUNT, hour=8, minute=10, tz="Europe/Oslo")
    for account_id, media in ((ACCOUNT, "ig-1"), (CHANNEL, "yt-1")):
        qid = await db.enqueue_post(
            conn, account_id=account_id, video_name="shared.mp4", cover_name=None,
            caption="c", keyword="UV", link=LINK, repo_full_name="astral-sh/uv",
            approved=True, hook="One render, two feeds",
        )
        await db.mark_queue_published(conn, qid, media_id=media, permalink="https://ig/p")
    await db.record_insights(
        conn, media_id="ig-1", account_id=ACCOUNT,
        metrics={"views": 120, "reach": 100, "skip_rate": 61.0, "avg_watch_ms": 5000},
    )
    await db.record_insights(
        conn, media_id="yt-1", account_id=CHANNEL, platform=db.PLATFORM_YOUTUBE,
        metrics={"views": 700, "avg_view_pct": 55.0, "avg_watch_ms": 18000},
    )
    await queue(http, approved=True)

    for path in PAGES:
        response = await http.get(path)
        assert response.status_code == 200, (path, response.text[:400])
    assert "Where the views came from" in (await http.get("/admin/b/nightly/")).text
    assert (await http.get(LIBRARY)).text.count("One render, two feeds") == 1, (
        "one video is one row, however many platforms it went to"
    )


async def test_the_schedule_shows_a_queued_video(client):
    http, _ = client
    await queue(http)
    body = (await http.get(SCHEDULE)).text
    assert "astral-sh/uv" in body


async def test_the_queue_page_says_when_the_video_was_made(client):
    """A queue three days deep means the date a post goes out says nothing
    about how old the video is, and a Reel about a repo that trended on Monday
    is a different proposition on Thursday."""
    http, app = client
    await queue(http)
    await db.record_rendered(
        app.state.db,
        repo_full_name="astral-sh/uv",
        account_id=ACCOUNT,
        rendered_at="2026-08-14T02:31:00+00:00",
    )

    body = (await http.get(SCHEDULE)).text

    assert "made Fri 14 Aug 02:31" in body


async def test_a_post_with_no_render_on_record_says_when_it_arrived_instead(client):
    """`--unmark` deletes the render row and a backfilled post never had one,
    so the fallback has to be labelled as the different thing it is rather than
    printed as if it were the render date."""
    http, _ = client
    await queue(http)

    body = (await http.get(SCHEDULE)).text

    assert "made " not in body
    assert "queued " in body


async def test_the_youtube_row_gets_the_date_too(client):
    """The render is recorded once, under whichever account the Mac had, and
    one MP4 feeds both destinations. Filtering by the reading account would
    leave the second board blank."""
    http, app = client
    await queue(http)
    await db.record_rendered(
        app.state.db,
        repo_full_name="astral-sh/uv",
        account_id="UCq0Ff3lJ7dK2sWnEv8mXtLp",
        rendered_at="2026-08-14T02:31:00+00:00",
    )

    body = (await http.get(SCHEDULE)).text

    assert "made Fri 14 Aug 02:31" in body


async def test_the_page_says_so_when_the_scheduler_is_off(tmp_path):
    """A queue nothing drains looks identical to a working one, right up until
    the first slot is missed."""
    off = settings(
        tmp_path, scheduler_enabled=False, admin_enabled=True, admin_token=ADMIN_TOKEN
    )
    app = create_app(off, http=FakeMeta().client(), background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE,
            cookies={"reelsmith_admin": ADMIN_TOKEN},
        ) as http,
    ):
        assert "scheduler is off" in (await http.get("/admin/")).text


async def test_the_panel_is_absent_when_disabled(tmp_path):
    off = settings(tmp_path, admin_enabled=False)
    app = create_app(off, http=FakeMeta().client(), background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as http,
    ):
        assert (await http.get("/admin/")).status_code == 404


async def test_the_panel_is_off_by_default(tmp_path):
    """This service is publicly reachable because Meta fetches media from it,
    so a control panel that publishes to a real account cannot default on."""
    assert settings(tmp_path).admin_enabled is False


# --- The controls ---------------------------------------------------------


async def test_approve_then_hold(client):
    http, app = client
    queued_id = (await queue(http))["id"]

    await http.post(f"/admin/queue/{queued_id}/approve")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_APPROVED

    await http.post(f"/admin/queue/{queued_id}/hold")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_DRAFT


async def test_cancel(client):
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_CANCELLED


async def test_publish_now_claims_the_row_without_waiting_for_a_slot(client):
    """The control that was missing. Everything else here decides what the
    scheduler does later, so a row whose failure had just been fixed still cost
    a day to find out about."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/approve")

    await http.post(f"/admin/queue/{queued_id}/publish")

    # Claimed the moment the button is pressed, which is what stops a tick
    # landing mid-flight from taking the same row.
    state = (await db.get_queued(app.state.db, queued_id))["state"]
    assert state in (db.QUEUE_CLAIMED, db.QUEUE_PUBLISHED, db.QUEUE_FAILED)
    assert state != db.QUEUE_APPROVED


async def test_publish_now_arms_a_failed_row_first(client):
    """A failed row is exactly the case this exists for, and `claim_queued`
    only takes one that is approved."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.set_queue_state(app.state.db, queued_id, db.QUEUE_FAILED, failure="nope")

    await http.post(f"/admin/queue/{queued_id}/publish")

    row = await db.get_queued(app.state.db, queued_id)
    assert row["state"] != db.QUEUE_FAILED or row["failure"] != "nope"


async def test_publish_now_leaves_the_slot_alone(client):
    """It answers "publish this row", not "pretend the slot has not run".
    Consuming the fire would cost the account a post the next day."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.claim_slot_fire(app.state.db, slot_id=1, local_date="2026-08-28")

    await http.post(f"/admin/queue/{queued_id}/publish")

    # Still claimed, so today's slot stays spent and tomorrow's is untouched.
    assert await db.claim_slot_fire(app.state.db, slot_id=1, local_date="2026-08-28") is False


async def test_publish_now_refuses_a_row_with_a_container(client):
    """The same line the scheduler draws. Something exists at the platform and
    may already be live, so re-sending risks a duplicate nothing here could
    detect afterwards."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.set_container(app.state.db, queued_id, "container-1")

    await http.post(f"/admin/queue/{queued_id}/publish")

    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_DRAFT


async def test_publish_now_refuses_a_published_row(client):
    """Sending it again is the one outcome that cannot be undone."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.mark_queue_published(app.state.db, queued_id, media_id="m1", permalink=None)

    await http.post(f"/admin/queue/{queued_id}/publish")

    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_PUBLISHED


async def test_a_published_post_cannot_be_cancelled(client):
    """Deleting the record of something that is live would only make it wrong."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.mark_queue_published(app.state.db, queued_id, media_id="m1", permalink=None)

    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_PUBLISHED


async def test_a_fresh_claim_cannot_be_cancelled(client):
    """It is mid-flight, and cancelling it races the publish that holds it."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/approve")
    assert await db.claim_queued(app.state.db, queued_id) is True

    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_CLAIMED


async def test_an_abandoned_claim_can_be_cancelled(client):
    """A process that died holding a claim left row 55 stuck for nine days."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/approve")
    await db.claim_queued(app.state.db, queued_id)
    stale = db.iso(db.now() - db.CLAIM_STALE_AFTER - timedelta(minutes=1))
    await app.state.db.execute(
        "UPDATE queued_posts SET claimed_at = ? WHERE id = ?", (stale, queued_id)
    )
    await app.state.db.commit()

    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_CANCELLED


async def test_a_claim_from_before_the_column_falls_back_to_created_at(client):
    """Rows predating schema 18 have no claimed_at and must still be resolvable."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/approve")
    await db.claim_queued(app.state.db, queued_id)
    old = db.iso(db.now() - timedelta(days=9))
    await app.state.db.execute(
        "UPDATE queued_posts SET claimed_at = NULL, created_at = ? WHERE id = ?",
        (old, queued_id),
    )
    await app.state.db.commit()

    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_CANCELLED


async def test_approving_a_failed_post_clears_its_reason(client):
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.set_queue_state(app.state.db, queued_id, db.QUEUE_FAILED, failure="boom")

    await http.post(f"/admin/queue/{queued_id}/approve")
    row = await db.get_queued(app.state.db, queued_id)
    assert (row["state"], row["failure"]) == (db.QUEUE_APPROVED, None)


async def test_moving_a_post_up_reorders_the_line(client):
    http, app = client
    first = (await queue(http, video_name=await upload(http, "a.mp4")))["id"]
    second = (await queue(http, video_name=await upload(http, "b.mp4")))["id"]

    await http.post(f"/admin/queue/{second}/move", data={"direction": "up"})
    order = [r["id"] for r in await db.queued_posts(app.state.db)]
    assert order == [second, first]


async def test_editing_the_caption_and_pinning(client):
    http, app = client
    queued_id = (await queue(http))["id"]

    await http.post(f"/admin/queue/{queued_id}/edit", data={
        "caption": "a better caption", "keyword": "RUFF",
        "link": LINK, "pin": "2026-08-09T18:00",
    })
    row = await db.get_queued(app.state.db, queued_id)
    assert row["caption"] == "a better caption"
    assert row["keyword"] == "RUFF"
    assert row["slot_override"].startswith("2026-08-09T18:00")


async def test_clearing_the_pin_puts_it_back_in_the_line(client):
    http, app = client
    queued_id = (await queue(http))["id"]
    await http.post(f"/admin/queue/{queued_id}/edit", data={
        "caption": "x", "keyword": "UV", "link": LINK, "pin": "2026-08-09T18:00",
    })
    await http.post(f"/admin/queue/{queued_id}/edit", data={
        "caption": "x", "keyword": "UV", "link": LINK, "pin": "",
    })
    assert (await db.get_queued(app.state.db, queued_id))["slot_override"] is None


# --- Slots ----------------------------------------------------------------


async def test_adding_pausing_and_deleting_a_slot(client):
    http, app = client
    conn = app.state.db

    await http.post("/admin/slots/add", data={
        "account_id": ACCOUNT, "hour": "18", "minute": "30",
        "tz": "Europe/Oslo", "jitter_minutes": "20", "days": ["1", "3"],
    })
    slot = (await db.all_slots(conn, ACCOUNT))[0]
    assert (slot["hour"], slot["minute"], slot["tz"]) == (18, 30, "Europe/Oslo")
    assert slot["days"] == "1,3"

    await http.post(f"/admin/slots/{slot['id']}/toggle", data={"active": "0"})
    assert (await db.all_slots(conn, ACCOUNT))[0]["active"] == 0

    await http.post(f"/admin/slots/{slot['id']}/delete")
    assert await db.all_slots(conn, ACCOUNT) == []


async def test_slots_declared_in_config_are_applied_at_startup(tmp_path):
    cfg = settings(
        tmp_path,
        scheduler_enabled=True,
        slots="18:00 Europe/Oslo jitter=15\n08:30 Europe/Oslo jitter=20 days=6,7",
    )
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        conn = app.state.db
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")

    # The account has to exist before the slots can be attached to it, so a
    # second boot is what applies them. That is the real sequence too: the
    # account is registered by the Mac, not by the config.
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        rows = await db.all_slots(app.state.db, ACCOUNT)
        assert [(r["hour"], r["minute"], r["source"]) for r in rows] == [
            (8, 30, "config"), (18, 0, "config"),
        ]


async def test_config_slot_ids_survive_a_restart(tmp_path):
    """The id seeds the jitter, so rewriting the rows every boot would
    reshuffle every offset on every restart, which is the instability the
    derived jitter exists to prevent."""
    cfg = settings(tmp_path, slots="18:00 UTC jitter=15")
    for _ in range(2):
        app = create_app(cfg, http=FakeMeta().client(), background=False)
        async with app.router.lifespan_context(app):
            await db.upsert_account(app.state.db, account_id=ACCOUNT, access_token="tok")
            first = [r["id"] for r in await db.all_slots(app.state.db, ACCOUNT)]

    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        assert [r["id"] for r in await db.all_slots(app.state.db, ACCOUNT)] == first


async def test_a_slot_removed_from_config_disappears(tmp_path):
    two = settings(tmp_path, slots="18:00 UTC\n09:00 UTC")
    app = create_app(two, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        await db.upsert_account(app.state.db, account_id=ACCOUNT, access_token="tok")
    app = create_app(two, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        assert len(await db.all_slots(app.state.db, ACCOUNT)) == 2

    one = settings(tmp_path, slots="18:00 UTC")
    app = create_app(one, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        rows = await db.all_slots(app.state.db, ACCOUNT)
        assert [r["hour"] for r in rows] == [18]


async def test_a_ui_slot_survives_a_config_sync(tmp_path):
    """Config owns its own slots and nothing else. Deleting one somebody added
    by hand would be a surprise from a redeploy."""
    cfg = settings(tmp_path, slots="18:00 UTC")
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        conn = app.state.db
        await db.upsert_account(conn, account_id=ACCOUNT, access_token="tok")
        await db.add_slot(conn, account_id=ACCOUNT, hour=7, minute=45, tz="UTC")

    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        rows = await db.all_slots(app.state.db, ACCOUNT)
        assert {(r["hour"], r["source"]) for r in rows} == {(7, "ui"), (18, "config")}


async def test_a_bad_slot_declaration_refuses_to_start(tmp_path):
    """Crashlooping with the offending line in the log is a much shorter debug
    than an account that stopped posting on Saturdays."""
    cfg = settings(tmp_path, slots="18:00 Mars/Olympus")
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    with pytest.raises(schedule.SlotSpecError):
        async with app.router.lifespan_context(app):
            pass


async def test_an_out_of_range_hour_is_refused(client):
    http, _ = client
    response = await http.post("/admin/slots/add", data={
        "account_id": ACCOUNT, "hour": "25", "minute": "0",
    })
    assert response.status_code == 400


# --- The kill switch ------------------------------------------------------


async def test_the_kill_switch_stops_dms(client):
    http, app = client
    await http.post(f"/admin/accounts/{ACCOUNT}/flags", data={
        "field": "dm_enabled", "value": "0",
    })
    assert (await db.get_account(app.state.db, ACCOUNT))["dm_enabled"] == 0


async def test_pausing_the_account(client):
    http, app = client
    await http.post(f"/admin/accounts/{ACCOUNT}/flags", data={
        "field": "active", "value": "0",
    })
    assert (await db.get_account(app.state.db, ACCOUNT))["active"] == 0


async def test_an_unknown_flag_is_refused(client):
    http, _ = client
    response = await http.post(f"/admin/accounts/{ACCOUNT}/flags", data={
        "field": "access_token", "value": "0",
    })
    assert response.status_code == 400


# --- Authentication -------------------------------------------------------
#
# The panel can publish to a real account, rewrite captions and flip the kill
# switch, on a service that has to be publicly reachable for Meta to fetch
# media from it. So the gate is its own problem, not only the ingress's.


@pytest.fixture
async def anon(cfg):
    """The same app, seen by someone who has not signed in."""
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http,
        ):
            await db.upsert_account(app.state.db, account_id=ACCOUNT, access_token="tok")
            yield http, app


@pytest.mark.parametrize(
    "path", ["/admin/", "/admin/posts", "/admin/slots", "/admin/health"]
)
async def test_a_stranger_is_sent_to_the_login_page(anon, path):
    http, _ = anon
    response = await http.get(path, headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/login")


async def test_a_stranger_cannot_read_the_queue(anon):
    """Not just a redirect: the page body must not leak on the way."""
    http, _ = anon
    response = await http.get("/admin/", headers={"accept": "text/html"})
    assert "astral-sh" not in response.text


@pytest.mark.parametrize(
    "path,data",
    [
        ("/admin/queue/1/approve", {}),
        ("/admin/queue/1/cancel", {}),
        ("/admin/queue/1/publish", {}),
        ("/admin/queue/1/edit", {"caption": "x"}),
        ("/admin/slots/add", {"account_id": ACCOUNT, "hour": "3"}),
        ("/admin/accounts/" + ACCOUNT + "/flags", {"field": "active", "value": "0"}),
    ],
)
async def test_a_stranger_cannot_touch_any_control(anon, path, data):
    http, app = anon
    response = await http.post(path, data=data)
    assert response.status_code == 401
    # And nothing moved.
    assert (await db.get_account(app.state.db, ACCOUNT))["active"] == 1


# --- The hook, where the decisions are made ---------------------------------


async def test_the_queue_shows_the_hook_being_reviewed(client):
    """Cancelling before the slot fires is the only review this account has.
    Nothing reads a script before it goes live, and the validators catch dashes
    and hype vocabulary but cannot catch a claim that is wrong about the
    project. Without the hook on the card, reviewing meant pressing play on
    every queued video to see the one line that decides whether anybody
    watches."""
    http, _ = client
    await queue(http, hook="It reads 40 pages of a PDF in a single pass")

    page = (await http.get(SCHEDULE, headers={"accept": "text/html"})).text

    assert "It reads 40 pages of a PDF in a single pass" in page


async def test_a_row_queued_before_the_hook_travelled_shows_no_hook(client):
    """Rather than an empty pair of quotation marks, which reads as a video
    whose opening is blank."""
    http, _ = client
    await queue(http)

    page = (await http.get(SCHEDULE, headers={"accept": "text/html"})).text

    assert 'class="hook"' not in page


async def test_the_posts_page_puts_the_hook_next_to_what_it_scored(client):
    """The one pair on the page where one plainly caused the other: `skip_rate`
    is the share who left inside three seconds, and this is what was on screen
    for them. Reading them apart is how a hook that never shipped was believed
    for a fortnight."""
    http, app = client
    conn = app.state.db
    qid = await db.enqueue_post(
        conn, account_id=ACCOUNT, video_name="v.mp4", cover_name=None, caption="c",
        keyword="UV", link=LINK, repo_full_name="a/b", approved=True,
        hook="Your coding agent dies when you close the terminal",
    )
    await db.mark_queue_published(conn, qid, media_id="m1", permalink="https://ig/x")
    await conn.commit()

    page = (await http.get(LIBRARY, headers={"accept": "text/html"})).text

    assert "Your coding agent dies when you close the terminal" in page


# --- Insights ---------------------------------------------------------------


async def test_the_insights_page_opens_with_nothing_to_compare(client):
    """A fresh account has no readings, and an empty chart is worse than a
    sentence saying why there is nothing to draw."""
    http, _ = client

    response = await http.get(PERFORMANCE, headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Nothing to compare yet" in response.text


async def test_the_insights_page_groups_published_reels(client):
    """Two posts on the same recipe and one on another, so a cohort table has
    something to be wrong about."""
    http, app = client
    conn = app.state.db
    for i, recipe in enumerate(("old1234.aaaaaaaa", "old1234.aaaaaaaa", "new5678.bbbbbbbb")):
        qid = await db.enqueue_post(
            conn, account_id=ACCOUNT, video_name=f"v{i}.mp4", cover_name=None,
            caption="c", keyword="UV", link=LINK, repo_full_name=f"a/b{i}",
            approved=True, recipe=recipe, hook=f"hook number {i}",
        )
        await db.mark_queue_published(conn, qid, media_id=f"m{i}", permalink="https://ig/x")
        # Three readings each, because a post is only counted once its numbers
        # have stopped moving and one reading is a post still arriving.
        for day in (1, 2, 3):
            await db.record_insights(
                conn, media_id=f"m{i}", account_id=ACCOUNT, on=f"2026-08-0{day}",
                metrics={"views": 100 + i, "reach": 90, "likes": 1, "comments": 0,
                         "saved": 0, "shares": 0, "avg_watch_ms": 4000,
                         "total_watch_ms": 400000, "skip_rate": 70.0 + i},
            )
    await conn.commit()

    page = (await http.get(PERFORMANCE, headers={"accept": "text/html"})).text

    assert "old1234.aaaaaaaa" in page
    assert "new5678.bbbbbbbb" in page
    # The hook rides along in the chart tooltip, which is the only place a dot
    # can say which post it is.
    assert "hook number 0" in page


async def test_a_post_still_arriving_is_held_back_from_the_cohorts(client):
    """And the page says so rather than quietly reporting a smaller table. A
    Reel has about 71 percent of its final views at its first reading, so
    counting yesterday's post makes its slot look worse than it is."""
    http, app = client
    conn = app.state.db

    async def publish(media_id: str, readings: int):
        qid = await db.enqueue_post(
            conn, account_id=ACCOUNT, video_name=f"{media_id}.mp4", cover_name=None,
            caption="c", keyword="UV", link=LINK, repo_full_name=f"a/{media_id}",
            approved=True, hook="h",
        )
        await db.mark_queue_published(
            conn, qid, media_id=media_id, permalink="https://ig/x"
        )
        for day in range(1, readings + 1):
            await db.record_insights(
                conn, media_id=media_id, account_id=ACCOUNT, on=f"2026-08-{day:02d}",
                metrics={"views": 40, "reach": 30, "likes": 0, "comments": 0,
                         "saved": 0, "shares": 0, "avg_watch_ms": 4000,
                         "total_watch_ms": 40000, "skip_rate": 70.0},
            )

    # Two posts that have stopped moving, so there is something to compare, and
    # one that has not. A page with nothing to compare says so already; the
    # failure worth catching is a table that silently drops the third.
    await publish("settled-a", 3)
    await publish("settled-b", 3)
    await publish("fresh", 1)
    await conn.commit()

    page = (await http.get(PERFORMANCE, headers={"accept": "text/html"})).text

    assert "held back" in page
    assert "1</strong>" in page, "and it says how many"


async def test_the_repos_page_shows_what_blocks_discovery(client):
    """The list that decides whether tonight's batch may pick a repo. It lived
    in a JSON file on one laptop and two tables nothing displayed, so "have we
    already done this one" was a question you answered by running a command on
    the right machine."""
    http, app = client
    conn = app.state.db
    await queue(http, repo_full_name="astral-sh/uv")
    await db.record_rendered(
        conn, repo_full_name="never/committed", account_id=ACCOUNT,
        run_folder="2026-08-18/never-committed",
        score=0.81, score_breakdown='{"velocity": 0.44, "stars": 0.12}',
    )
    await conn.commit()

    page = (await http.get(SUBJECTS, headers={"accept": "text/html"})).text

    assert "astral-sh/uv" in page
    assert "never/committed" in page
    # A finished video nothing committed to, which nothing else in the panel
    # would ever mention.
    assert "not committed" in page
    # And why the scorer chose it, which is the only answer available to why
    # discovery keeps landing on the same corner of GitHub.
    assert "velocity" in page


async def test_a_stranger_cannot_read_the_repo_list(anon):
    anon_http, _ = anon
    assert (await anon_http.get(SUBJECTS)).status_code == 401


async def test_a_stranger_cannot_read_the_insights(anon):
    """Same as every other panel page. The numbers are not secret, but the
    panel that publishes to a real account has to be reachable from the
    internet for Meta to fetch media from it."""
    anon_http, _ = anon
    assert (await anon_http.get(PERFORMANCE)).status_code == 401


async def test_the_wrong_token_does_not_sign_you_in(anon):
    http, _ = anon
    response = await http.post("/admin/login", data={"token": "not-it"})
    assert response.status_code == 401
    assert "reelsmith_admin" not in response.cookies


async def test_the_right_token_signs_you_in_and_the_panel_opens(anon):
    http, _ = anon
    response = await http.post("/admin/login", data={"token": ADMIN_TOKEN})
    assert response.status_code == 303
    assert http.cookies.get("reelsmith_admin") == ADMIN_TOKEN
    assert (await http.get("/admin/", headers={"accept": "text/html"})).status_code == 200


async def test_the_session_cookie_carries_the_flags_that_do_the_work(anon):
    """SameSite=Strict is the primary CSRF defence, HttpOnly keeps it away
    from any script on the page, and Secure keeps it off plaintext."""
    http, _ = anon
    response = await http.post("/admin/login", data={"token": ADMIN_TOKEN})
    header = response.headers["set-cookie"].lower()
    assert "samesite=strict" in header
    assert "httponly" in header
    assert "secure" in header


async def test_the_cookie_is_not_marked_secure_on_a_plain_http_deployment(tmp_path):
    """Otherwise a local run cannot sign in at all, and the usual workaround
    is someone turning the whole gate off."""
    cfg = settings(
        tmp_path, admin_enabled=True, admin_token=ADMIN_TOKEN,
        public_base_url="http://localhost:8000",
    )
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as http,
    ):
        response = await http.post("/admin/login", data={"token": ADMIN_TOKEN})
        assert "secure" not in response.headers["set-cookie"].lower()
        assert (await http.get("/admin/")).status_code == 200


async def test_signing_out_closes_the_panel_again(client):
    http, _ = client
    await http.post("/admin/logout")
    response = await http.get("/admin/", headers={"accept": "text/html"})
    assert response.status_code == 303


async def test_an_empty_token_is_not_a_free_pass(anon):
    """An empty presented value against an empty configured one must not
    compare equal by accident."""
    http, _ = anon
    assert (await http.post("/admin/login", data={"token": ""})).status_code == 401


async def test_a_proxy_authenticated_deployment_needs_no_token(tmp_path):
    """Forward-auth is an explicit statement, never inferred from a header."""
    cfg = settings(tmp_path, admin_enabled=True, admin_trust_proxy_auth=True)
    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as http,
    ):
        assert (await http.get("/admin/")).status_code == 200


async def test_the_panel_refuses_to_start_with_no_authentication(tmp_path):
    """"I thought the ingress was handling it" is how these get exposed, so
    the boot fails instead."""
    cfg = settings(tmp_path, admin_enabled=True)
    with pytest.raises(GatewayConfigError):
        create_app(cfg, http=FakeMeta().client(), background=False)


async def test_the_login_form_posts_to_the_scheme_the_page_was_served_over(anon):
    """The form action must not drop to http on an https page.

    This broke in production the first time the panel was opened. TLS
    terminates at Traefik, so without proxy headers the app believes every
    request is plain http, `url_for` writes an http:// action, and the browser
    warns that the password is being sent insecurely. Submitting it is then a
    cross-scheme POST, which the CSRF check below correctly refuses, so the
    panel cannot be signed into at all.

    The fix is uvicorn's --proxy-headers in the Dockerfile; this test pins the
    behaviour the app must show once the scheme reaching it is right.
    """
    http, _ = anon
    body = (await http.get("/admin/login", headers={"accept": "text/html"})).text
    action = re.search(r'action="([^"]+)"', body).group(1)
    assert action.startswith(f"{BASE}/"), action


def test_the_access_token_cannot_reach_the_log(tmp_path, caplog):
    """httpx logs the full request URL at INFO, and every Graph call carries
    `access_token` as a query parameter.

    Turning this package's logging on therefore started writing a live token
    with publishing and messaging rights into the pod log every twenty seconds,
    and from there into the log store. Found in production the same minute the
    logging change shipped.
    """
    import logging

    create_app(settings(tmp_path), http=FakeMeta().client(), background=False)
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_the_image_tells_uvicorn_to_trust_the_proxy():
    """Guards the other half, which no request through ASGI can reach.

    `--proxy-headers` is applied by the uvicorn server, not by the ASGI app, so
    the test above passes in-process no matter what the container does.
    """
    dockerfile = (Path(__file__).parent.parent / "gateway" / "Dockerfile").read_text()
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips" in dockerfile


# --- CSRF and redirects ---------------------------------------------------


async def test_a_cross_origin_post_is_refused(client):
    """Even holding a valid cookie. SameSite already stops the browser sending
    it; this is the lock that still works if a browser disagrees."""
    http, app = client
    response = await http.post(
        f"/admin/accounts/{ACCOUNT}/flags",
        data={"field": "active", "value": "0"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert (await db.get_account(app.state.db, ACCOUNT))["active"] == 1


async def test_a_same_origin_post_is_allowed(client):
    http, app = client
    response = await http.post(
        f"/admin/accounts/{ACCOUNT}/flags",
        data={"field": "dm_enabled", "value": "0"},
        headers={"origin": BASE},
    )
    assert response.status_code < 400
    assert (await db.get_account(app.state.db, ACCOUNT))["dm_enabled"] == 0


async def test_a_foreign_referer_does_not_become_a_redirect(client):
    """Honouring the Referer blindly would make every control here an open
    redirect, on the hostname the account's own audience is asked to trust."""
    http, _ = client
    queued_id = (await queue(http))["id"]
    response = await http.post(
        f"/admin/queue/{queued_id}/approve",
        headers={"referer": "https://evil.example/phish"},
    )
    assert response.status_code == 303
    assert "evil.example" not in response.headers["location"]


async def test_our_own_referer_is_honoured(client):
    http, _ = client
    queued_id = (await queue(http))["id"]
    response = await http.post(
        f"/admin/queue/{queued_id}/approve",
        headers={"referer": f"{BASE}/admin/slots"},
    )
    assert response.headers["location"] == f"{BASE}/admin/slots"


# --- Posts and account scoping ----------------------------------------------


async def test_the_posts_page_shows_what_a_reel_did(client):
    """The page exists to answer "did that one work", which needs both halves:
    Meta's numbers and the funnel only this service can see."""
    http, app = client
    queued = await queue(http, approved=True)
    await db.mark_queue_published(
        app.state.db, queued["id"], media_id="media-1", permalink="https://ig/p/1"
    )
    await db.record_insights(
        app.state.db, media_id="media-1", account_id=ACCOUNT,
        metrics={"views": 1500, "reach": 1173, "likes": 23, "comments": 0,
                 "saved": 20, "shares": 9},
    )
    await db.claim_comment(
        app.state.db, comment_id="c1", media_id="media-1",
        account_id=ACCOUNT, author_id="a1",
    )
    await db.record_delivery(
        app.state.db, igsid="a1", account_id=ACCOUNT, media_id="media-1"
    )

    body = (await http.get(LIBRARY)).text

    assert "astral-sh/uv" in body
    assert "1500" in body, "the view count"
    assert "1173" in body, "reach"
    assert "100%" in body, "one person asked and one got the link"


async def test_the_posts_page_scores_the_hook(client):
    """Views cannot say whether anyone watched, and skip rate scores the first
    three seconds on their own. It is the number the page exists to show."""
    http, app = client
    queued = await queue(http, approved=True)
    await db.mark_queue_published(
        app.state.db, queued["id"], media_id="media-1", permalink="https://ig/p/1"
    )
    await db.record_insights(
        app.state.db, media_id="media-1", account_id=ACCOUNT,
        metrics={"views": 1500, "reach": 1173, "likes": 23, "comments": 0,
                 "saved": 20, "shares": 9, "avg_watch_ms": 8370,
                 "total_watch_ms": 10_622_235, "skip_rate": 64.2},
    )

    body = (await http.get(LIBRARY)).text

    assert "64.2%" in body, "the skip rate, to the tenth"
    assert "8.4s" in body, "average watch time in seconds, not milliseconds"
    assert "avg skip" in body, "and summarised across the account"


async def test_a_reel_with_no_reading_yet_says_so_rather_than_showing_zero(client):
    """Zero views and "not measured yet" are different facts, and the second one
    is what is true for a post published this morning."""
    http, app = client
    queued = await queue(http, approved=True)
    await db.mark_queue_published(
        app.state.db, queued["id"], media_id="fresh", permalink=None
    )

    body = (await http.get(LIBRARY)).text

    # In the cell, not in the page's explanation of what the words mean, which
    # is how this passed while the cell said "None stored".
    assert ">No reading yet</span>" in body


async def test_the_picker_offers_every_brand(client):
    """One entry per identity, in the sidebar, on every page."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="17841400000000009", access_token="t2", username="second"
    )

    aside = (await http.get("/admin/")).text.split("<aside", 1)[1].split("</aside>", 1)[0]

    assert "/admin/b/nightly/" in aside
    assert "/admin/b/second/" in aside


async def test_a_brand_page_is_about_that_brand_only(client):
    http, app = client
    other = "17841400000000009"
    await db.upsert_account(
        app.state.db, account_id=other, access_token="t2", username="second"
    )
    await queue(http, approved=True)

    body = (await http.get("/admin/b/second/schedule")).text

    # Split at the sidebar, which names every brand by definition. What the
    # brand path promises is about the page itself.
    main = body.split("</aside>", 1)[-1]
    assert "second" in main
    assert "astral-sh/uv" not in main, "the other brand's queue leaked onto this page"


async def test_an_unknown_brand_lands_on_today(client):
    """A bookmark that outlived its identity should open the panel, not a 404."""
    http, _ = client

    response = await http.get("/admin/b/does-not-exist/schedule")

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/")


async def test_old_addresses_land_on_the_new_pages(client):
    """The eight tab URLs are in bookmarks and in this repo's own notes."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id=CHANNEL, access_token="", username="@nightly",
        platform=db.PLATFORM_YOUTUBE, brand="nightly",
    )

    expected = {
        f"/admin/queue?account={CHANNEL}": "/admin/b/nightly/schedule?platform=youtube",
        "/admin/posts?brand=nightly": "/admin/b/nightly/library",
        "/admin/insights?brand=nightly": "/admin/b/nightly/performance",
        "/admin/repos?brand=nightly": "/admin/b/nightly/subjects",
        "/admin/slots": "/admin/destinations",
        "/admin/health": "/admin/system",
        "/admin/queue": "/admin/calendar",
    }
    for old, new in expected.items():
        response = await http.get(old)
        assert response.status_code == 303, old
        assert response.headers["location"].endswith(new), (old, response.headers["location"])


# --- Which surface a board describes ----------------------------------------


@pytest.mark.parametrize(
    "page", ["/admin/b/nightly/", "/admin/b/nightly/setup", "/admin/b/nightly/performance"]
)
async def test_every_destination_names_its_platform(client, page):
    """A YouTube channel is its own row publishing on its own schedule under its
    own rules, and a board with nothing but a handle on it was ambiguous."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="UC-chan", access_token="tok",
        username="@nightly", platform=db.PLATFORM_YOUTUBE, brand="nightly",
    )

    body = (await http.get(page)).text

    # The badge markup, not the bare word, because the word is also in the
    # sidebar and in sentences that explain a platform.
    assert 'class="platform youtube"' in body, f"{page} does not mark the channel"
    assert 'class="platform instagram"' in body, f"{page} does not mark the Reels account"


async def test_a_platform_filter_narrows_the_schedule(client):
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="UC-chan", access_token="tok",
        username="@nightly", platform=db.PLATFORM_YOUTUBE, brand="nightly",
    )
    await queue(http, approved=True)

    main = (await http.get(f"{SCHEDULE}?platform=youtube")).text.split("</aside>", 1)[-1]

    assert "astral-sh/uv" not in main, "an Instagram row survived a YouTube filter"


# --- The dashboard ----------------------------------------------------------


async def test_today_is_the_landing_page(client):
    """What needs a person across every brand, then one row per brand."""
    http, _ = client

    body = (await http.get("/admin/")).text

    assert "Needs a decision" in body
    assert "Brands" in body
    # The manager is loud-only decoration, and loud is what a first visit gets.
    assert "Hi Boss" in body


async def test_the_plain_look_drops_the_manager(client):
    http, _ = client
    http.cookies.set("skin", "plain")

    body = (await http.get("/admin/")).text

    assert "Hi Boss" not in body
    assert "boss-room.jpg" not in body, "the plain look must not download the wallpaper"


async def test_the_brand_page_names_the_next_post_and_when_it_goes(client):
    """The countdown and the hook together, because the hook is what a person
    would cancel over and the time is how long they have to do it."""
    http, app = client
    await db.add_slot(
        app.state.db, account_id=ACCOUNT, hour=8, minute=10,
        tz="Europe/Oslo", jitter_minutes=0, days="0,1,2,3,4,5,6",
    )
    await queue(http, approved=True, hook="It reads 40 pages of a PDF in a single pass")

    body = (await http.get("/admin/b/nightly/")).text

    assert "It reads 40 pages of a PDF in a single pass" in body
    assert "Europe/Oslo" in body, "the slot's own zone, not UTC"
    # `ago` counts the other way and reported a future slot as a negative
    # number of seconds, which shipped once as "-56934s from now".
    assert "-" not in re.search(r'class="cap">\s*(in [^<&]*)', body).group(1)


async def test_the_brand_page_says_so_when_nothing_is_scheduled(client):
    """An empty queue and a queue with no active slot are the same picture from
    here, and both are better than a blank panel."""
    http, _ = client

    assert "Nothing armed on a destination with an active slot" in (
        await http.get("/admin/b/nightly/")
    ).text


async def test_an_empty_queue_with_a_slot_is_a_decision_on_today(client):
    """The shape of an account that went quiet three days ago and nobody noticed."""
    http, app = client
    await db.add_slot(app.state.db, account_id=ACCOUNT, hour=8, minute=10, tz="UTC")

    body = (await http.get("/admin/")).text

    assert "Nothing armed on Instagram" in body


async def test_performance_scores_openings_on_instagram_only(client):
    """`skip_rate` is 0 on every other platform and that 0 is an absence, not a
    perfect opening, so a brand without Instagram gets no skip chart at all."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="UC-chan", access_token="tok",
        username="@tubeonly", platform=db.PLATFORM_YOUTUBE,
    )

    body = (await http.get("/admin/b/tubeonly/performance")).text

    assert "The opening" not in body
    assert "average view percentage" in body


# --- The panel's own images -------------------------------------------------


async def test_an_asset_is_served_to_a_signed_in_browser(client):
    http, _ = client

    response = await http.get("/admin/assets/boss-avatar.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]


async def test_an_unknown_asset_is_a_404_rather_than_a_path(client):
    """An allowlist, so no arrangement of dots and slashes reads a file the
    panel was never meant to serve."""
    http, _ = client

    assert (await http.get("/admin/assets/boss.jpg")).status_code == 404
    assert (await http.get("/admin/assets/..%2f..%2fdb.sqlite3")).status_code == 404


async def test_assets_need_the_login_like_everything_else(anon):
    http, _ = anon

    assert (await http.get("/admin/assets/boss-avatar.jpg")).status_code == 401


# --- Reading a counter that has labels --------------------------------------


async def test_the_system_page_counts_posts_that_carry_a_platform_label(client):
    """`posts_published` gained a `platform` label on 2026-08-26, and
    `prometheus_client` never runs `_metric_init` on a labelled parent, so the
    read swallowed an AttributeError and the tile said 0 on a service that had
    published dozens."""
    http, app = client
    app.state.metrics.posts_published.labels(platform=db.PLATFORM_INSTAGRAM).inc()
    app.state.metrics.posts_published.labels(platform=db.PLATFORM_YOUTUBE).inc(2)

    body = (await http.get("/admin/system")).text

    assert ">3<" in body.replace(" ", "").replace("\n", "")
    # And it says since when, because those counters start again at a restart
    # and "4 published" on a service holding 150 posts reads as a total.
    assert "Process up since" in body


# --- Publishing every destination of one identity at once --------------------
#
# A new identity is registered, credentialled and queued, and the only thing
# that proves any of it works is a post coming out the other end. Waiting for
# 08:10 to find out that a token was minted wrong, or that a Page id is the one
# from a URL, costs a day per attempt, and the first attempt is exactly when
# something is most likely to be wrong.


async def _second_destination(app, *, brand: str) -> str:
    await db.upsert_account(
        app.state.db, account_id=ACCOUNT, access_token="tok", username="nightly", brand=brand
    )
    await db.upsert_account(
        app.state.db,
        account_id=CHANNEL,
        access_token="",
        username="@nightly",
        platform=db.PLATFORM_YOUTUBE,
        brand=brand,
    )
    return CHANNEL


async def test_publish_all_claims_the_next_row_on_every_destination(client):
    http, app = client
    channel = await _second_destination(app, brand="one")
    first = (await queue(http, approved=True))["id"]
    second = (await queue(http, approved=True, account_id=channel))["id"]

    await http.post("/admin/queue/publish-all?brand=one")

    # Asserted on `attempts` rather than on `state`, because a publish that
    # fails is re-armed for its next try, so by the time this reads the row it
    # may legitimately be `approved` again. `claim_queued` increments attempts
    # and nothing puts that back, which makes it the durable evidence that the
    # row was taken.
    for queued_id in (first, second):
        assert (await db.get_queued(app.state.db, queued_id))["attempts"] >= 1, queued_id


async def test_publish_all_takes_one_row_per_destination_not_the_whole_queue(client):
    """It is a rehearsal of a slot fire, and a slot takes one row.

    Draining the queue would be a different and much worse button: the point is
    to prove the path works, not to spend everything queued behind it.
    """
    http, app = client
    await _second_destination(app, brand="one")
    first = (await queue(http, approved=True))["id"]
    behind = (await queue(http, approved=True))["id"]

    await http.post("/admin/queue/publish-all?brand=one")

    assert (await db.get_queued(app.state.db, first))["attempts"] >= 1
    assert (await db.get_queued(app.state.db, behind))["attempts"] == 0


async def test_publish_all_refuses_when_no_identity_is_selected(client):
    """`_scope` falls back to every account when nothing is chosen, which is
    right for a page and wrong for a button that publishes."""
    http, app = client
    queued_id = (await queue(http, approved=True))["id"]

    await http.post("/admin/queue/publish-all")

    assert (await db.get_queued(app.state.db, queued_id))["attempts"] == 0


async def test_publish_all_skips_a_row_that_already_has_a_container(client):
    """The same line the scheduler and cancel draw: something exists at the
    platform and may already be live."""
    http, app = client
    await _second_destination(app, brand="one")
    queued_id = (await queue(http, approved=True))["id"]
    await db.set_container(app.state.db, queued_id, "container-123")

    await http.post("/admin/queue/publish-all?brand=one")

    assert (await db.get_queued(app.state.db, queued_id))["attempts"] == 0


async def test_publish_all_leaves_a_draft_alone(client):
    """A draft is deliberately not armed, and this is not the control that
    decides that. It publishes what a slot would publish."""
    http, app = client
    await _second_destination(app, brand="one")
    queued_id = (await queue(http))["id"]

    await http.post("/admin/queue/publish-all?brand=one")

    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_DRAFT


async def test_publish_all_does_not_consume_the_slot(client):
    """The feature rather than a caveat. Today's fire is still due, so the
    schedule still proves itself on its own timetable while this proves the
    publishing path immediately. Two questions, and this answers one."""
    http, app = client
    await _second_destination(app, brand="one")
    await db.add_slot(app.state.db, account_id=ACCOUNT, hour=8, minute=10, tz="UTC")
    await queue(http, approved=True)
    before = [dict(r) for r in await db.all_slots(app.state.db, ACCOUNT)]

    await http.post("/admin/queue/publish-all?brand=one")

    assert [dict(r) for r in await db.all_slots(app.state.db, ACCOUNT)] == before


# --- The dashboard counting one identity rather than all of them -------------


async def _two_identities(app) -> None:
    await db.upsert_account(
        app.state.db, account_id=ACCOUNT, access_token="t", username="one", brand="one"
    )
    await db.upsert_account(
        app.state.db, account_id=CHANNEL, access_token="", username="@one",
        platform=db.PLATFORM_YOUTUBE, brand="one",
    )
    await db.upsert_account(
        app.state.db, account_id=PAGE_ID, access_token="t", username="two",
        platform=db.PLATFORM_FACEBOOK, brand="two",
    )


async def test_today_counts_each_identity_on_its_own_row(client):
    """Two identities used to be summed into one runway, one next post and one
    retention chart. Each brand is a row now, and its numbers are its own."""
    http, app = client
    await _two_identities(app)
    await queue(http, approved=True)
    await queue(http, approved=True, account_id=CHANNEL)
    await queue(http, approved=True, account_id=PAGE_ID)

    body = (await http.get("/admin/")).text

    def armed(brand: str) -> int:
        match = re.search(
            rf'nm">{brand}<small>.*?</td>\s*<td>.*?</td>\s*<td class="r">(\d+)</td>', body, re.S
        )
        assert match, f"no row for {brand}"
        return int(match.group(1))

    assert armed("one") == 2
    assert armed("two") == 1


async def test_a_repo_rendered_for_one_identity_is_one_subject_not_one_per_destination(client):
    """The cooldown list is per identity. It was one table per destination, so
    one render showed up once for every platform the brand posts to."""
    http, app = client
    await _two_identities(app)
    for account_id in (ACCOUNT, CHANNEL):
        await db.record_rendered(
            app.state.db, account_id=account_id, repo_full_name="astral-sh/uv"
        )

    body = (await http.get("/admin/b/one/subjects")).text

    assert body.count("astral-sh/uv</a>") == 1


async def test_a_render_with_no_owner_stays_with_the_first_identity(client):
    """A blank account id matched every account, so the second brand's page
    listed a repository it never touched and counted it as its one render."""
    http, app = client
    await _two_identities(app)
    await db.record_rendered(app.state.db, account_id="", repo_full_name="teamchong/pxpipe")

    assert "teamchong/pxpipe" in (await http.get("/admin/b/one/subjects")).text
    assert "teamchong/pxpipe" not in (await http.get("/admin/b/two/subjects")).text
    # Discovery still reads it for everyone, which is what the table is for.
    rows = await db.rendered_repos_list(app.state.db, PAGE_ID)
    assert [row["repo_full_name"] for row in rows] == ["teamchong/pxpipe"]


async def test_the_publish_all_button_is_only_inside_a_brand(client):
    """It was not on any page for a while, and the endpoint working hid that.
    It belongs on a brand's Schedule, which is always scoped, and nowhere a
    misread click could fire every identity at once."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id=ACCOUNT, access_token="t", username="one", brand="one"
    )

    assert "Publish all now" in (await http.get("/admin/b/one/schedule")).text
    assert "Publish all now" not in (await http.get("/admin/")).text
    assert "Publish all now" not in (await http.get("/admin/calendar")).text


# --- One video, several destinations ---------------------------------------


async def _one_video_on_two_destinations(app, *, state: str = db.QUEUE_DRAFT) -> list[int]:
    await _two_identities(app)
    ids = []
    for account_id in (ACCOUNT, CHANNEL):
        ids.append(await db.enqueue_post(
            app.state.db, account_id=account_id, video_name="shared.mp4", cover_name=None,
            caption="c", keyword="UV", link=LINK, repo_full_name="astral-sh/uv",
            approved=state == db.QUEUE_APPROVED, hook="One render, two feeds",
        ))
    return ids


async def test_a_video_is_approved_on_every_destination_at_once(client):
    """Reviewing one render used to be the same decision on four cards."""
    http, app = client
    ids = await _one_video_on_two_destinations(app)

    await http.post("/admin/b/one/videos/approve", data={"ids": [str(i) for i in ids]})

    for queued_id in ids:
        assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_APPROVED


async def test_the_schedule_shows_one_row_for_one_video(client):
    http, app = client
    await _one_video_on_two_destinations(app, state=db.QUEUE_APPROVED)

    body = (await http.get("/admin/b/one/schedule")).text

    assert body.count('<p class="hook">One render, two feeds</p>') == 1
    assert 'class="chip instagram' in body
    assert 'class="chip youtube' in body


async def test_a_video_action_cannot_reach_another_brand(client):
    """The ids come from a form, so a forged list must not touch another
    identity's queue."""
    http, app = client
    await _two_identities(app)
    theirs = await db.enqueue_post(
        app.state.db, account_id=PAGE_ID, video_name="v.mp4", cover_name=None,
        caption="c", keyword="UV", link=LINK, approved=True,
    )

    await http.post("/admin/b/one/videos/cancel", data={"ids": [str(theirs)]})

    assert (await db.get_queued(app.state.db, theirs))["state"] == db.QUEUE_APPROVED


async def test_arming_a_whole_video_skips_a_failure_with_a_container(client):
    """Retrying a row whose container existed is a decision made after reading
    the failure. One click arming a video must not make it for somebody."""
    http, app = client
    first, second = await _one_video_on_two_destinations(app)
    await db.set_container(app.state.db, second, "container-1")
    await db.set_queue_state(app.state.db, second, db.QUEUE_FAILED, failure="gave up")

    await http.post("/admin/b/one/videos/approve", data={"ids": [str(first), str(second)]})

    assert (await db.get_queued(app.state.db, first))["state"] == db.QUEUE_APPROVED
    assert (await db.get_queued(app.state.db, second))["state"] == db.QUEUE_FAILED


# --- What each destination is shown ----------------------------------------


async def test_setup_offers_comment_replies_only_where_they_exist(client):
    """The keyword mechanic is Instagram's. Health showed the switch on YouTube."""
    http, app = client
    await _two_identities(app)

    body = (await http.get("/admin/b/one/setup")).text

    assert body.count("Stop DMs") == 1


async def test_a_tiktok_token_reports_the_expiry_it_has(client):
    """Health read `accounts.token_expires_at` for every platform, which TikTok
    never fills, and printed "?" for a refresh token with a year left."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="tt-open", access_token="", username="@nightly",
        platform=db.PLATFORM_TIKTOK, brand="nightly",
    )
    await db.upsert_tiktok_credentials(
        app.state.db, open_id="tt-open", client_key="k", client_secret="s",
        refresh_token="r", refresh_expires_in=300 * 86_400,
    )

    body = (await http.get("/admin/b/nightly/setup")).text

    # 300 days less the seconds the test took, rounded.
    assert re.search(r"(299|300) days left", body)
    # Once, on the Instagram row, whose fixture account has no expiry stored.
    assert body.count("Expiry not recorded") == 1


async def test_a_page_token_says_it_does_not_expire(client):
    http, app = client
    await _two_identities(app)

    assert "Does not expire" in (await http.get("/admin/b/two/setup")).text


async def test_a_tiktok_destination_with_no_readings_says_why(client):
    """The inbox upload sends no title and the sweep matches TikTok videos by
    title, so nothing is ever matched. The page used to say "No reading yet",
    which reads as a post that is merely new."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="tt-open", access_token="", username="@nightly",
        platform=db.PLATFORM_TIKTOK, brand="nightly",
    )
    for n in range(3):
        qid = await db.enqueue_post(
            app.state.db, account_id="tt-open", video_name=f"v{n}.mp4", cover_name=None,
            caption="c", keyword="UV", link=LINK, approved=True,
        )
        await db.mark_queue_published(app.state.db, qid, media_id=f"pub-{n}", permalink=None)

    body = (await http.get("/admin/")).text

    assert "No readings stored for 3 posts on TikTok" in body
    assert "Inbox uploads carry no title" in body


async def test_the_library_offers_no_player_for_a_pruned_video(client):
    """Media is deleted after publish by design, and the Posts page still drew
    a player for every row: 173 of 191 media URLs returned 404 on one load."""
    http, app = client
    live = await upload(http, "live.mp4")
    for name, media in ((live, "m-live"), ("pruned-long-ago.mp4", "m-gone")):
        qid = await db.enqueue_post(
            app.state.db, account_id=ACCOUNT, video_name=name, cover_name=None,
            caption="c", keyword="UV", link=LINK, approved=True, hook=f"hook for {media}",
        )
        await db.mark_queue_published(app.state.db, qid, media_id=media, permalink=None)

    body = (await http.get(LIBRARY)).text

    assert body.count("<video") == 1
    assert f'src="/media/{live}"' in body
    assert "pruned after publishing" in body


async def test_a_platform_that_never_reported_is_not_shown_as_zero_views(client):
    """"0 TikTok views" on a destination that has stored nothing is a result
    nobody measured."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="tt-open", access_token="", username="@nightly",
        platform=db.PLATFORM_TIKTOK, brand="nightly",
    )
    qid = await db.enqueue_post(
        app.state.db, account_id="tt-open", video_name="v.mp4", cover_name=None,
        caption="c", keyword="UV", link=LINK, approved=True,
    )
    await db.mark_queue_published(app.state.db, qid, media_id="pub-1", permalink=None)

    body = (await http.get(LIBRARY)).text

    assert "TikTok readings stored" in body
    assert ">0</span><span class=\"l\">TikTok views" not in body


async def test_one_fresh_post_does_not_blame_the_sweep(client):
    """A post published an hour ago has no reading because it is an hour old.
    Telling someone to check the sweep's flag for that sends them debugging
    something that works."""
    http, app = client
    await db.upsert_account(
        app.state.db, account_id="UC-chan", access_token="", username="@nightly",
        platform=db.PLATFORM_YOUTUBE, brand="nightly",
    )
    qid = await db.enqueue_post(
        app.state.db, account_id="UC-chan", video_name="v.mp4", cover_name=None,
        caption="c", keyword="UV", link=LINK, approved=True,
    )
    await db.mark_queue_published(app.state.db, qid, media_id="yt-1", permalink=None)

    body = (await http.get(PERFORMANCE)).text

    assert "GATEWAY_YOUTUBE_INSIGHTS_ENABLED" not in body
    assert "No reading yet" in body


async def test_one_failed_publish_reads_in_the_singular(client):
    http, app = client
    await db.add_slot(app.state.db, account_id=ACCOUNT, hour=8, minute=10, tz="UTC")
    queued_id = (await queue(http, approved=True))["id"]
    await db.set_queue_state(app.state.db, queued_id, db.QUEUE_FAILED, failure="boom")

    body = (await http.get("/admin/")).text

    assert "1 failed publish on Instagram" in body
    assert "It needs a retry or a give up." in body


async def test_search_finds_a_video_by_its_hook(client):
    http, _ = client
    await queue(http, hook="It reads 40 pages of a PDF in a single pass")

    body = (await http.get("/admin/search?q=40 pages")).text

    assert "It reads 40 pages of a PDF in a single pass" in body
    assert "/admin/b/nightly/library" in body