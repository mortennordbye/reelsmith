"""The public pages, and the one property that makes them worth a test file.

Three platforms hold these URLs on file for this app. Instagram and YouTube
wanted a privacy policy; TikTok wants a privacy policy, terms of service and an
official website, and will not take a URL that is not on a DNS-verified domain,
which is what moved them off GitHub and onto this service.

So the thing to pin is not the wording. It is that the URLs answer, and keep
answering when the admin panel is off, because the obvious place to have put
them was a router that only mounts when it is on.

`/tiktok/callback` and `/facebook/callback` ride on the same router for the same
reason, and carry one property of their own: they must never exchange
anything. They are redirect targets that print a code, and the tests below say
so, because the obvious "improvement" to them is to have the gateway finish the
trip. Both are checked rather than only the first, since a route added for the
second platform is exactly the one that would be mounted on the panel's router
by mistake.
"""

from __future__ import annotations

import httpx
import pytest

from gateway.app import create_app
from tests.gateway_harness import FakeMeta, settings

BASE = "https://gateway"
PAGES = ("/", "/privacy", "/terms")
# One page and two providers. Each platform holds its own URL in its own
# developer portal, so both have to answer whatever else changes.
CALLBACKS = ("/tiktok/callback", "/facebook/callback")


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


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_shows_the_code_it_was_handed(tmp_path, path):
    """The page exists so an operator can read the code out of a browser and
    paste it back. Neither platform will register a redirect URI that is not
    https, so a loopback listener cannot be the target however much simpler it
    would be to run one."""
    response = await fetch(settings(tmp_path), f"{path}?code=abc123&state=xyz789")

    assert response.status_code == 200
    assert "abc123" in response.text
    assert "xyz789" in response.text


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_answers_with_the_admin_panel_off(tmp_path, path):
    """Same property as the legal pages, and it matters more here: this URL is
    saved in the app's configuration in the platform's portal, and a redirect
    that 404s spends a consent trip."""
    response = await fetch(settings(tmp_path, admin_enabled=False), f"{path}?code=abc123")

    assert response.status_code == 200
    assert "abc123" in response.text


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_reports_a_refusal_rather_than_a_blank_page(tmp_path, path):
    """Both send `error` instead of `code` when the user declines or the app
    asks for a scope it does not hold. Rendering nothing would look like the
    code failed to arrive, which points at the wrong half."""
    body = (
        await fetch(settings(tmp_path), f"{path}?error=access_denied&error_description=nope")
    ).text

    assert "access_denied" in body
    assert "nope" in body


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_is_inert_when_nobody_is_authorising(tmp_path, path):
    """A stranger reaching it should find a page saying so, not a half
    rendered form suggesting there is something to fill in."""
    response = await fetch(settings(tmp_path), path)

    assert response.status_code == 200
    assert "Nothing to do here" in response.text


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_says_which_platform_sent_the_browser(tmp_path, path):
    """One template serving two providers, so the wording is the only thing
    that can silently go wrong. An operator who reaches a page naming the other
    platform is looking at a misrouted consent trip."""
    body = (await fetch(settings(tmp_path), f"{path}?code=abc123")).text

    assert path.split("/")[1] in body.lower()


@pytest.mark.parametrize("path", CALLBACKS)
async def test_the_callback_never_calls_the_platform(tmp_path, path):
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
            await http.get(f"{path}?code=abc123&state=xyz789")

    assert meta.calls == []


# --- Naming no identity, which is what makes one URL cover all of them -------


@pytest.mark.parametrize("path", PAGES)
async def test_the_public_pages_name_no_account(tmp_path, path):
    """The reason these were rewritten on 2026-09-11.

    Every platform holds one privacy policy URL per *app*, and one app now
    publishes for more than one identity. A policy naming the first identity
    covered neither the second nor any identity after it.

    The obvious fix was the wrong one: listing every registered destination
    would name both identities on a public URL, which is the link `PROFILE.md`
    is gitignored to prevent. Naming none of them covers all of them, and adds
    nothing to cover the next.
    """
    cfg = settings(tmp_path)
    body = (await fetch(cfg, path)).text.lower()

    # The prose, with mailto links taken out. The contact address is the one
    # identifying string left and it is deliberately not covered here: it
    # defaults to an address carrying the first identity's handle, because a
    # privacy policy pointing at a mailbox nobody reads is worse than one
    # naming an account. `GATEWAY_CONTACT_EMAIL` is how that is finished, and
    # the test below pins that it is settable.
    #
    # It is also the weaker link of the two. An address ties the service to
    # the identity that has always been openly tied to it, and this repository
    # is named on these pages. What must never appear is a *second* identity,
    # because that is what ties two accounts to each other.
    prose = body.replace(cfg.contact_email.lower(), "")

    for handle in ("thewholequote", "the whole quote"):
        assert handle not in body, f"{path} names the second identity: {handle}"
    for handle in ("thenightlybuild", "the nightly build"):
        assert handle not in prose, f"{path} names {handle} outside its contact address"


async def test_the_public_pages_still_say_which_platforms(tmp_path):
    """Neutral is not vague. A reviewer arriving from an app listing has to be
    able to tell that the policy covers the surface they are reviewing."""
    body = (await fetch(settings(tmp_path), "/privacy")).text

    for platform in ("Instagram", "YouTube", "TikTok", "Facebook"):
        assert platform in body


@pytest.mark.parametrize("path", PAGES)
async def test_the_contact_address_comes_from_configuration(tmp_path, path):
    """The last identifying string on the pages, so it is a setting rather than
    a name baked into three templates. An address carrying one account's handle
    would put that account back on a page that names none."""
    neutral = settings(tmp_path, contact_email="hello@example.test")

    assert "hello@example.test" in (await fetch(neutral, path)).text
