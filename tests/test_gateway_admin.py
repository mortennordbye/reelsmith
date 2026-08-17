"""The queue API and the control panel.

Two things are worth testing here. That every control changes exactly the state
it claims to and refuses the transitions that would make the record wrong, and
that none of them can be reached by someone who has not signed in. The second
matters because this panel publishes to a real account on a service that has to
be publicly reachable for Meta to fetch media from it.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from gateway import db, schedule
from gateway.app import create_app
from gateway.config import GatewayConfigError
from tests.gateway_harness import ACCOUNT, API_TOKEN, FakeMeta, settings

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
                app.state.db, ig_user_id=ACCOUNT, access_token="tok", username="nightly"
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
        "ig_user_id": ACCOUNT, "video_name": video, "caption": "hello",
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
        "ig_user_id": ACCOUNT, "video_name": "ghost.mp4", "keyword": "X", "link": LINK,
    })
    assert response.status_code == 400
    assert "ghost.mp4" in response.json()["detail"]


async def test_a_traversing_filename_is_rejected_by_the_model(client):
    http, _ = client
    response = await http.post("/api/queue", headers=AUTH, json={
        "ig_user_id": ACCOUNT, "video_name": "../../gateway.sqlite3",
        "keyword": "X", "link": LINK,
    })
    assert response.status_code == 422


async def test_the_keyword_must_be_one_word(client):
    http, _ = client
    video = await upload(http)
    response = await http.post("/api/queue", headers=AUTH, json={
        "ig_user_id": ACCOUNT, "video_name": video,
        "keyword": "two words", "link": LINK,
    })
    assert response.status_code == 422


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
        app.state.db, media_id="1", ig_user_id=ACCOUNT,
        keyword="UV", link="https://github.com/DietrichGebert/ponytail",
    )
    assert "DietrichGebert/ponytail" in await covered(http)


async def test_the_earliest_commitment_wins(client):
    """A repo queued once and published later is one commitment, and the
    cooldown turned on at the first of the two."""
    http, app = client
    await queue(http)
    await db.register_post(
        app.state.db, media_id="2", ig_user_id=ACCOUNT,
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
    await db.record_rendered(app.state.db, repo_full_name="astral-sh/uv", ig_user_id="")

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


@pytest.mark.parametrize(
    "path", ["/admin/", "/admin/posts", "/admin/slots", "/admin/health"]
)
async def test_the_pages_render(client, path):
    http, _ = client
    response = await http.get(path)
    assert response.status_code == 200
    assert "reelsmith" in response.text


async def test_the_queue_page_shows_a_queued_post_and_its_keyword(client):
    http, _ = client
    await queue(http)
    body = (await http.get("/admin/")).text
    assert "astral-sh/uv" in body
    assert "UV" in body


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


async def test_a_published_post_cannot_be_cancelled(client):
    """Deleting the record of something that is live would only make it wrong."""
    http, app = client
    queued_id = (await queue(http))["id"]
    await db.mark_queue_published(app.state.db, queued_id, media_id="m1", permalink=None)

    await http.post(f"/admin/queue/{queued_id}/cancel")
    assert (await db.get_queued(app.state.db, queued_id))["state"] == db.QUEUE_PUBLISHED


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
        "ig_user_id": ACCOUNT, "hour": "18", "minute": "30",
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
        await db.upsert_account(conn, ig_user_id=ACCOUNT, access_token="tok")

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
            await db.upsert_account(app.state.db, ig_user_id=ACCOUNT, access_token="tok")
            first = [r["id"] for r in await db.all_slots(app.state.db, ACCOUNT)]

    app = create_app(cfg, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        assert [r["id"] for r in await db.all_slots(app.state.db, ACCOUNT)] == first


async def test_a_slot_removed_from_config_disappears(tmp_path):
    two = settings(tmp_path, slots="18:00 UTC\n09:00 UTC")
    app = create_app(two, http=FakeMeta().client(), background=False)
    async with app.router.lifespan_context(app):
        await db.upsert_account(app.state.db, ig_user_id=ACCOUNT, access_token="tok")
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
        await db.upsert_account(conn, ig_user_id=ACCOUNT, access_token="tok")
        await db.add_slot(conn, ig_user_id=ACCOUNT, hour=7, minute=45, tz="UTC")

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
        "ig_user_id": ACCOUNT, "hour": "25", "minute": "0",
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
            await db.upsert_account(app.state.db, ig_user_id=ACCOUNT, access_token="tok")
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
        ("/admin/queue/1/edit", {"caption": "x"}),
        ("/admin/slots/add", {"ig_user_id": ACCOUNT, "hour": "3"}),
        ("/admin/accounts/" + ACCOUNT + "/flags", {"field": "active", "value": "0"}),
    ],
)
async def test_a_stranger_cannot_touch_any_control(anon, path, data):
    http, app = anon
    response = await http.post(path, data=data)
    assert response.status_code == 401
    # And nothing moved.
    assert (await db.get_account(app.state.db, ACCOUNT))["active"] == 1


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
        app.state.db, media_id="media-1", ig_user_id=ACCOUNT,
        metrics={"views": 1500, "reach": 1173, "likes": 23, "comments": 0,
                 "saved": 20, "shares": 9},
    )
    await db.claim_comment(
        app.state.db, comment_id="c1", media_id="media-1",
        ig_user_id=ACCOUNT, author_id="a1",
    )
    await db.record_delivery(
        app.state.db, igsid="a1", ig_user_id=ACCOUNT, media_id="media-1"
    )

    body = (await http.get("/admin/posts")).text

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
        app.state.db, media_id="media-1", ig_user_id=ACCOUNT,
        metrics={"views": 1500, "reach": 1173, "likes": 23, "comments": 0,
                 "saved": 20, "shares": 9, "avg_watch_ms": 8370,
                 "total_watch_ms": 10_622_235, "skip_rate": 64.2},
    )

    body = (await http.get("/admin/posts")).text

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

    body = (await http.get("/admin/posts")).text

    assert "No reading yet" in body


async def test_the_switcher_appears_only_once_there_is_a_choice(client):
    """At one account a switcher offering one option is furniture."""
    http, app = client
    assert 'class="switcher' not in (await http.get("/admin/")).text

    await db.upsert_account(
        app.state.db, ig_user_id="17841400000000009", access_token="t2", username="second"
    )
    assert 'class="switcher' in (await http.get("/admin/")).text


async def test_scoping_to_an_account_hides_the_others(client):
    http, app = client
    other = "17841400000000009"
    await db.upsert_account(
        app.state.db, ig_user_id=other, access_token="t2", username="second"
    )

    body = (await http.get(f"/admin/?account={other}")).text

    assert "second" in body
    # The scoped page must not still be rendering a board for the other one.
    assert body.count("nightly") <= 1, "only the switcher may still name it"


async def test_an_unknown_account_falls_back_to_everything(client):
    """A bookmark that outlived its account should show the panel, not a 404."""
    http, _ = client
    response = await http.get("/admin/?account=does-not-exist")
    assert response.status_code == 200
    assert "nightly" in response.text


# --- Which surface a board describes ----------------------------------------


@pytest.mark.parametrize("page", ["/admin/", "/admin/slots", "/admin/posts", "/admin/health"])
async def test_every_board_names_its_platform(client, page):
    """A YouTube channel is its own account row publishing on its own schedule
    under its own rules. Before this, two boards appeared with nothing but a
    username between them."""
    http, app = client
    await db.upsert_account(
        app.state.db, ig_user_id="UC-chan", access_token="tok",
        username="thenightlybuild", platform=db.PLATFORM_YOUTUBE,
    )

    body = (await http.get(page)).text

    # The badge markup, not the bare word: the stylesheet explains itself and
    # mentions both platforms by name, so a substring match passes on a page
    # that renders no badge at all.
    assert 'class="platform youtube"' in body, f"{page} does not mark the channel board"
    assert 'class="platform instagram"' in body, f"{page} does not mark the Reels board"


async def test_a_single_board_still_names_its_platform(client):
    """The posts page used to hide the heading when only one board was shown,
    which is exactly when the switcher has narrowed to one account and nothing
    else on the page says which surface it is."""
    http, app = client
    await db.upsert_account(
        app.state.db, ig_user_id="UC-chan", access_token="tok",
        username="thenightlybuild", platform=db.PLATFORM_YOUTUBE,
    )

    body = (await http.get("/admin/posts?account=UC-chan")).text

    assert 'class="platform youtube"' in body
    assert 'class="platform instagram"' not in body, "the switcher narrowed to one account"
