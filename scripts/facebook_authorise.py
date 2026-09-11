#!/usr/bin/env python
"""facebook_authorise.py - turn a one-time browser consent into a stored Page.

The same job `scripts/tiktok_authorise.py` and `scripts/youtube_authorise.py`
do, and the shortest of the three, because what this ends up storing is one
token rather than a client pair plus a refresh token.

Run it once per Page:

    uv run python scripts/authorise.py facebook --account <name>

It reads `FACEBOOK_APP_ID` and `FACEBOOK_APP_SECRET` from the environment,
never from argv, which is visible in `ps` and lands in shell history.

**Four steps, and the third is the one worth knowing about.**

1. The browser consent, which returns a code.
2. The code for a short-lived user token, which lasts about an hour.
3. **That token for a long-lived one**, which lasts about 60 days.
4. `GET /me/accounts` with the long-lived user token, which hands back one
   Page access token per Page. A Page token derived from a *long-lived* user
   token does not expire on a clock, and one derived from a short-lived token
   expires with it. Skipping step 3 therefore produces a registration that
   works perfectly and stops publishing in an hour, with nothing in the failure
   to say why. That is the whole reason this script exists rather than a note
   saying "paste a Page token".

**The redirect lands on the gateway, not on a loopback port**, the same as the
TikTok trip. Facebook does permit a `localhost` redirect while an app is in
development, and this deliberately does not rely on that: the app that
publishes these Reels is live, and the page next door already exists.

**Nothing here is refreshed afterwards.** The gateway has no Facebook
refresher, because a long-lived Page token has no clock to run down. Re-running
this script is how a Page is recovered if a token is ever invalidated, which is
what a password change or a permission revocation does.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import consent  # noqa: E402  - after the sys.path insert above

API_VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
AUTH_URL = f"https://www.facebook.com/{API_VERSION}/dialog/oauth"

# It has to match one of the app's Valid OAuth Redirect URIs character for
# character, including the trailing slash or its absence.
REDIRECT_URI = os.environ.get("FACEBOOK_REDIRECT_URI", "https://gate.nordbye.it/facebook/callback")

# Asked for together, in one authorisation, because adding a scope later means
# going back through the browser and re-consenting.
#
# `pages_show_list` is what makes /me/accounts return anything at all.
# `pages_manage_posts` is the publish. `pages_read_engagement` is the insights
# sweep, including the comment count on a video node.
#
# **`read_insights` is deliberately absent.** It covers Page level insights,
# which nothing here reads: the sweep asks a video node for its own numbers,
# and that is post level. A scope the app does not use is a named rejection
# reason at review time, so this list should not grow speculatively.
#
# An admin of both the app and the Page is granted all three without App
# Review. Review is what publishing to somebody else's Page would need, which
# this account will never do.
SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement"

APPS_URL = "https://developers.facebook.com/apps/"
# A Page is created on Facebook itself rather than in the app dashboard, and it
# is the step that is not a click: a Page is a public surface with a name, and
# `PROFILE.md` treats naming one as a decision rather than a form.
CREATE_PAGE_URL = "https://www.facebook.com/pages/create/"


def setup_pages(app_id: str) -> list[tuple[str, bool, str]]:
    """What has to be true before this trip can work. Printed, never opened.

    Neither has a prompt behind it and both are usually already done, which is
    what makes them prerequisites rather than steps. The two things this trip
    actually asks for, the app secret and the consent, open their own page at
    the moment they are asked, and nothing else opens at all.
    """
    return [
        (
            CREATE_PAGE_URL,
            True,
            "A Page exists for this identity. A Page is a public surface with a\n"
            "     name rather than a form to fill in, the consent dialog offers\n"
            "     only Pages you already administer, and it is not eligible for\n"
            "     a username until it has followers and a post.",
        ),
        (
            # Not `fb-login/settings/`, which is this app's other login product
            # and silently redirects to the dashboard. Read off the address bar
            # on 2026-09-11 after the guess landed on the wrong page, which is
            # the failure the `confirmed` flag exists to make visible.
            f"{APPS_URL}{app_id}/business-login/settings/",
            True,
            "The redirect URI below is listed under Valid OAuth Redirect URIs,\n"
            "     character for character, and Strict Mode means exactly that.\n"
            "     Usually already there, since one app serves every Page.",
        ),
    ]


def authorise(app_id: str) -> str:
    """Open the browser, take the code back by hand. Returns the code."""
    state = consent.new_state()
    query = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
            "response_type": "code",
        }
    )
    return consent.paste_code(
        f"{AUTH_URL}?{query}",
        state,
        platform="Facebook",
        watch_for=[
            "Tick the Page you mean on the Pages screen. A consent that grants\n"
            "    no Page looks like a success and returns an empty list later.",
            "The dialog bounces to /forced_account_switch if the browser is\n"
            "    acting as the Page. Consent is granted by the person.",
            "It lands on a page on the gateway showing a code. Copy the whole\n"
            "    address bar, not just the code.",
        ],
    )


def _get(path: str, params: dict, *, token: str = "") -> dict:
    """One Graph read. The token rides in a header where there is one.

    The OAuth endpoints are the exception rather than an oversight: their whole
    job is to trade one credential for another, so the value is a parameter and
    there is no header form. Everything afterwards uses the header, which is the
    rule `gateway/graph.py` follows.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.get(f"{GRAPH}{path}", params=params, headers=headers, timeout=30)
    payload = response.json() if response.content else {}
    if not isinstance(payload, dict) or payload.get("error"):
        error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        raise consent.ConsentError(
            f"Facebook refused {path}: {error.get('message') or response.text[:400]}"
        )
    return payload


def user_token(code: str, app_id: str, app_secret: str) -> str:
    """The code for a short-lived user token, then that for a long-lived one.

    Both steps here rather than one, because the second is the step that is
    easy to leave out and impossible to notice afterwards: a Page token minted
    from a short-lived user token expires with it, roughly an hour later, and
    the failure that follows says the token is invalid rather than saying it
    was born wrong.
    """
    short = _get(
        "/oauth/access_token",
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": REDIRECT_URI,
            "code": urllib.parse.unquote(code),
        },
    ).get("access_token", "")
    if not short:
        raise consent.ConsentError("The code exchange returned no access token.")

    long_lived = _get(
        "/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short,
        },
    )
    token = long_lived.get("access_token", "")
    if not token:
        raise consent.ConsentError("The long-lived exchange returned no access token.")
    return token


def pages(token: str) -> list[dict]:
    """Every Page this token can post to, with its own token on each row."""
    found = _get("/me/accounts", {"fields": "id,name,access_token"}, token=token)
    rows = found.get("data") or []
    if not rows:
        raise consent.ConsentError(
            "That consent granted no Pages. The Pages screen in the dialog has "
            "a tick per Page and none is ticked by default, so this usually "
            "means the screen was passed through. Run it again."
        )
    return rows


def choose(rows: list[dict]) -> dict:
    """One Page, picked by hand when there is more than one.

    Never resolved by count, for the reason the pipeline refuses to resolve an
    account by count: the wrong pick here is not visible again until a Reel is
    on the wrong Page, and nothing later undoes that.
    """
    if len(rows) == 1:
        # Printed rather than silently taken. One row is not evidence of the
        # right row: Meta's "continue with your previous settings" shortcut
        # re-grants whatever the last identity to connect this app was given,
        # which is exactly one Page and the wrong one. `consent.confirm` in
        # `trip` is what actually stops it; this line is what makes it legible.
        print(f"\nThis consent covers one Page: {rows[0].get('name')} ({rows[0].get('id')})")
        return rows[0]

    print("\nPages this consent covers:\n")
    for index, row in enumerate(rows, start=1):
        print(f"  {index}. {row.get('name')}  ({row.get('id')})")
    picked = input("\nWhich one? ").strip()
    if not picked.isdigit() or not 1 <= int(picked) <= len(rows):
        raise consent.ConsentError("Not one of those. Nothing was registered.")
    return rows[int(picked) - 1]


def trip(args: argparse.Namespace) -> consent.Trip:
    """The browser half, and what it produced. Called by `authorise.py`."""
    # Both before the browser, so neither a misspelt account name nor a shell
    # that cannot prompt costs a spent consent screen.
    consent.require_terminal()
    brand = consent.brand_for(args.account, args.brand)

    # The account's `.env` layered over the root one, so an identity with its
    # own Meta app stays possible, with the environment still winning over
    # both for a one-off. Reading `os.environ` alone meant typing both on the
    # command line that invoked this, and a secret typed there is one prefixed
    # assignment away from the shell history that argparse is avoided for.
    cfg = consent.account_settings(args.account)
    app_id = os.environ.get("FACEBOOK_APP_ID", "") or cfg.facebook_app_id
    app_secret = os.environ.get("FACEBOOK_APP_SECRET", "") or cfg.facebook_app_secret

    # Refused before anything is printed. Without an app id every URL below
    # would carry a `<app-id>` placeholder, which is a dead link presented as
    # an instruction. A list of addresses that 404 is worse than no list.
    if not app_id:
        raise consent.ConsentError(
            f"No FACEBOOK_APP_ID, in the environment or in .env or\n"
            f"accounts/{args.account}/.env. It identifies the Meta app rather\n"
            f"than the Page, so one id serves every Page and the root .env is\n"
            f"where it belongs. It is public: App settings, Basic, top of the\n"
            f"page, next to the secret."
        )

    if not args.no_browser:
        consent.list_prerequisites(setup_pages(app_id))
        print(f"  The redirect URI this trip uses: {REDIRECT_URI}\n")

    # The secret is asked for rather than required up front. Requiring it is
    # the step somebody discovers they have not done after a browser has been
    # opened, and the tab it comes from is now open in front of them.
    if not app_secret:
        app_secret = consent.ask_secret(
            "FACEBOOK_APP_SECRET",
            what="App settings, Basic, behind the Show button beside it.",
            where=f"{APPS_URL}{app_id}/settings/basic/",
        )

    page = choose(pages(user_token(authorise(app_id), app_id, app_secret)))
    page_id = str(page.get("id") or "")
    consent.confirm(
        what="About to register this Page:",
        lines=[("Page", str(page.get("name") or "?")), ("id", page_id)],
        account=args.account,
        brand=brand,
    )
    return consent.Trip(
        platform="facebook",
        account_id=page_id,
        username=str(page.get("name") or ""),
        payload={
            "page_id": page_id,
            "access_token": page.get("access_token", ""),
            "username": str(page.get("name") or ""),
            "brand": brand,
        },
        secret_label="FACEBOOK_PAGE_TOKEN",
        secret_value=page.get("access_token", ""),
        notes=[
            "A Page has two ids and the obvious one is wrong. This is the one\n"
            "/me/accounts returned, which is what /{page-id}/video_reels wants.",
        ],
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    consent.add_common_arguments(parser)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="skip opening the setup pages",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args()
    consent.finish(args, trip(args))


if __name__ == "__main__":
    main()
