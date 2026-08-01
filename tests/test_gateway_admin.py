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


@pytest.mark.parametrize("path", ["/admin/", "/admin/slots", "/admin/health"])
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


@pytest.mark.parametrize("path", ["/admin/", "/admin/slots", "/admin/health"])
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
