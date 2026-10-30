"""The LinkedIn routes: registration, text rows in the queue, and the panel's
compose form.

A text row is the one queue row with no video, and the route has to refuse
that shape for every other platform: a YouTube row with no file would sit
armed until its slot and fail there, days later, with nobody watching.
"""

from __future__ import annotations

import httpx
import pytest

from gateway import db
from gateway.app import create_app
from tests.gateway_harness import ACCOUNT, API_TOKEN, FakeMeta, settings

AUTH = {"authorization": f"Bearer {API_TOKEN}"}
ADMIN_TOKEN = "test-admin-token-0123456789abcdef"
PERSON = "abc123XYZ"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path, admin_enabled=True, admin_token=ADMIN_TOKEN)


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
            await db.upsert_account(
                app.state.db, account_id=ACCOUNT, access_token="tok", username="nightly"
            )
            yield http, app


async def register(http, **overrides) -> httpx.Response:
    body = {
        "person_id": PERSON,
        "access_token": "li-token",
        "expires_in": 60 * 86_400,
        "username": "Somebody",
        "brand": "me",
        **overrides,
    }
    return await http.post("/api/accounts/linkedin", json=body, headers=AUTH)


async def test_registration_stores_the_profile_with_its_clock(client):
    http, app = client
    response = await register(http)
    assert response.status_code == 200, response.text

    row = await db.get_account(app.state.db, PERSON)
    assert row["platform"] == db.PLATFORM_LINKEDIN
    assert row["brand"] == "me"
    days = (db.parse_iso(row["token_expires_at"]) - db.now()).days
    assert 58 <= days <= 60


async def test_a_pasted_urn_is_trimmed_to_the_id(client):
    http, app = client
    assert (await register(http, person_id=f"urn:li:person:{PERSON}")).status_code == 200
    assert await db.get_account(app.state.db, PERSON) is not None


async def test_a_linkedin_token_needs_its_expiry(client):
    http, _ = client
    assert (await register(http, expires_in=0)).status_code == 422


async def test_a_text_row_is_queued_on_linkedin(client):
    http, _ = client
    await register(http)
    response = await http.post(
        "/api/queue",
        json={"account_id": PERSON, "caption": "A post.", "link": "https://blog.example/x/"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text


async def test_a_row_with_no_video_is_refused_everywhere_else(client):
    http, _ = client
    response = await http.post(
        "/api/queue", json={"account_id": ACCOUNT, "caption": "No file."}, headers=AUTH
    )
    assert response.status_code == 422
    assert "LinkedIn" in response.json()["detail"]


async def test_the_panel_composes_a_draft_with_its_image(client, cfg):
    http, app = client
    await register(http)
    await http.post("/admin/login", data={"token": ADMIN_TOKEN})

    response = await http.post(
        "/admin/b/me/compose",
        data={"text": "First line.\n\nSecond.", "link": "https://blog.example/x/", "title": "X"},
        files={"image": ("cover.png", b"\x89PNG fake", "image/png")},
    )
    assert response.status_code in (200, 303), response.text

    rows = await db.queued_posts(app.state.db, account_id=PERSON)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == db.QUEUE_DRAFT
    assert row["video_name"] == ""
    assert row["hook"] == "First line."
    assert row["title"] == "X"
    assert (cfg.covers_dir / row["cover_name"]).read_bytes() == b"\x89PNG fake"


async def test_compose_is_refused_for_a_brand_with_no_linkedin(client):
    http, _ = client
    await http.post("/admin/login", data={"token": ADMIN_TOKEN})
    response = await http.post("/admin/b/nightly/compose", data={"text": "Hi."})
    assert response.status_code == 404


async def test_the_schedule_shows_the_form_and_a_queued_text_post(client):
    """A row with no video renders as a text post, not as a missing file."""
    http, _ = client
    await register(http)
    await http.post("/admin/login", data={"token": ADMIN_TOKEN})
    await http.post("/admin/b/me/compose", data={"text": "Opening line here.", "arm": "1"})

    page = await http.get("/admin/b/me/schedule")
    assert page.status_code == 200
    assert "Write a LinkedIn post" in page.text
    assert "Opening line here." in page.text
    assert "The video file is not on the gateway" not in page.text


async def test_a_brand_without_linkedin_has_no_compose_form(client):
    http, _ = client
    await http.post("/admin/login", data={"token": ADMIN_TOKEN})
    page = await http.get("/admin/b/nightly/schedule")
    assert page.status_code == 200
    assert "Write a LinkedIn post" not in page.text
