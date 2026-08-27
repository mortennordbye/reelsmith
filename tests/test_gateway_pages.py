"""The public pages, and the one property that makes them worth a test file.

Three platforms hold these URLs on file for this app. Instagram and YouTube
wanted a privacy policy; TikTok wants a privacy policy, terms of service and an
official website, and will not take a URL that is not on a DNS-verified domain,
which is what moved them off GitHub and onto this service.

So the thing to pin is not the wording. It is that the URLs answer, and keep
answering when the admin panel is off, because the obvious place to have put
them was a router that only mounts when it is on.

`/tiktok/callback` rides on the same router for the same reason, and carries
one property of its own: it must never exchange anything. It is a redirect
target that prints a code, and the tests below say so, because the obvious
"improvement" to it is to have the gateway finish the trip.
"""

from __future__ import annotations

import httpx
import pytest

from gateway.app import create_app
from tests.gateway_harness import FakeMeta, settings

BASE = "https://gateway"
PAGES = ("/", "/privacy", "/terms")


async def fetch(cfg, path: str) -> httpx.Response:
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(cfg, http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http,
        ):
            return await http.get(path)


@pytest.mark.parametrize("path", PAGES)
async def test_the_public_pages_answer(tmp_path, path):
    response = await fetch(settings(tmp_path), path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", PAGES)
async def test_they_answer_with_the_admin_panel_off(tmp_path, path):
    """The property this router exists for.

    `admin.public` already serves a page that cannot require a login, so it was
    the obvious home for these. It is only included when `admin_enabled` is on,
    and a legal page that 404s because a feature flag moved is worse than one
    nobody reads. Three platforms have these URLs on file.
    """
    response = await fetch(settings(tmp_path, admin_enabled=False), path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", PAGES)
async def test_the_pages_need_no_login(tmp_path, path):
    """They are reached by strangers arriving from an app listing, and by
    whatever TikTok uses to check a URL resolves."""
    response = await fetch(
        settings(tmp_path, admin_enabled=True, admin_token="x" * 32), path
    )

    assert response.status_code == 200
    assert "login" not in str(response.url)


async def test_each_page_links_to_the_other_two(tmp_path):
    """A platform review reaches one URL and expects to find the rest without
    opening a menu. It is also how somebody who lands on the terms finds out
    what the account actually is."""
    for path in PAGES:
        body = (await fetch(settings(tmp_path), path)).text
        assert 'href="/privacy"' in body, path
        assert 'href="/terms"' in body, path
        assert 'href="/"' in body, path


async def test_the_policy_says_a_viewer_is_not_recorded(tmp_path):
    """The one sentence in it that most people need, and the one most likely to
    be lost in an edit that tightens the wording.

    Whitespace is collapsed before matching, because the template wraps its
    source lines and a test that fails when a sentence rewraps is testing the
    line length rather than the promise.
    """
    body = " ".join((await fetch(settings(tmp_path), "/privacy")).text.split())

    assert "holds nothing about you at all" in body


async def test_the_pages_carry_no_external_assets(tmp_path):
    """Self-contained on purpose. These render for somebody who arrived from an
    app listing on an unknown device, and a stylesheet on a third-party host is
    both a tracking vector and a way for a legal page to render as unstyled
    text years from now."""
    for path in PAGES:
        body = (await fetch(settings(tmp_path), path)).text
        assert "<script" not in body, path
        assert "stylesheet" not in body, path


async def test_the_callback_shows_the_code_it_was_handed(tmp_path):
    """The page exists so an operator can read the code out of a browser and
    paste it back. TikTok refuses to register a redirect URI that is not
    https, so a loopback listener cannot be the target however much simpler it
    would be to run one."""
    response = await fetch(
        settings(tmp_path), "/tiktok/callback?code=abc123&state=xyz789"
    )

    assert response.status_code == 200
    assert "abc123" in response.text
    assert "xyz789" in response.text


async def test_the_callback_answers_with_the_admin_panel_off(tmp_path):
    """Same property as the legal pages, and it matters more here: this URL is
    saved in the app's configuration in TikTok's portal, and a redirect that
    404s spends a consent trip."""
    response = await fetch(
        settings(tmp_path, admin_enabled=False), "/tiktok/callback?code=abc123"
    )

    assert response.status_code == 200
    assert "abc123" in response.text


async def test_the_callback_reports_a_refusal_rather_than_a_blank_page(tmp_path):
    """TikTok sends `error` instead of `code` when the user declines or the
    app asks for a scope it does not hold. Rendering nothing would look like
    the code failed to arrive, which points at the wrong half."""
    body = (
        await fetch(
            settings(tmp_path),
            "/tiktok/callback?error=access_denied&error_description=nope",
        )
    ).text

    assert "access_denied" in body
    assert "nope" in body


async def test_the_callback_is_inert_when_nobody_is_authorising(tmp_path):
    """A stranger reaching it should find a page saying so, not a half
    rendered form suggesting there is something to fill in."""
    response = await fetch(settings(tmp_path), "/tiktok/callback")

    assert response.status_code == 200
    assert "Nothing to do here" in response.text


async def test_the_callback_never_calls_tiktok(tmp_path):
    """The one behaviour worth pinning. Exchanging the code here would need the
    client secret, which this service is not told until the account is
    registered at the end of the same trip. The page is a display and nothing
    else."""
    meta = FakeMeta()
    async with meta.client() as fake_meta:
        app = create_app(settings(tmp_path), http=fake_meta, background=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http,
        ):
            await http.get("/tiktok/callback?code=abc123&state=xyz789")

    assert meta.calls == []
