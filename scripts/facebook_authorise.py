#!/usr/bin/env python
"""facebook_authorise.py - turn a one-time browser consent into a stored Page.

The same job `scripts/tiktok_authorise.py` and `scripts/youtube_authorise.py`
do, and the shortest of the three, because what this ends up storing is one
token rather than a client pair plus a refresh token.

Run it once per Page:

    uv run python scripts/facebook_authorise.py

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
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402  - after the sys.path insert above

API_VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
AUTH_URL = f"https://www.facebook.com/{API_VERSION}/dialog/oauth"

# It has to match one of the app's Valid OAuth Redirect URIs character for
# character, including the trailing slash or its absence.
REDIRECT_URI = os.environ.get(
    "FACEBOOK_REDIRECT_URI", "https://gate.nordbye.it/facebook/callback"
)

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


def authorise(app_id: str) -> str:
    """Open the browser, take the code back by hand. Returns the code.

    `state` is compared on the way back rather than ignored. Pasting the whole
    address is what makes that possible, and it is why the prompt asks for the
    address rather than for the code: a bare code carries no state to check.
    """
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
            "response_type": "code",
        }
    )
    url = f"{AUTH_URL}?{query}"
    print(
        "\nA browser is opening. Three things to watch for:\n"
        "  - Tick the Page you mean on the Pages screen. A consent that grants\n"
        "    no Page looks like a success and returns an empty list later.\n"
        "  - It lands on a page on the gateway showing a code. Copy the whole\n"
        "    address bar, not just the code.\n"
        f"  - If the browser does not open, paste this:\n    {url}\n"
    )
    webbrowser.open(url)

    pasted = input("\nPaste the address you landed on: ").strip()
    if not pasted:
        raise SystemExit("Nothing pasted. Nothing was exchanged.")

    returned = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if not returned:
        raise SystemExit(
            "No query string in that. Paste the whole address, including "
            "everything after the question mark."
        )
    if returned.get("error"):
        raise SystemExit(
            f"Facebook refused the authorisation: {returned['error'][0]} "
            f"{returned.get('error_description', [''])[0]}"
        )
    if returned.get("state", [""])[0] != state:
        raise SystemExit("The state did not match. Nothing was exchanged.")
    if not returned.get("code"):
        raise SystemExit("That address carries no code. Nothing was exchanged.")
    return returned["code"][0]


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
        raise SystemExit(
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
        raise SystemExit("The code exchange returned no access token.")

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
        raise SystemExit("The long-lived exchange returned no access token.")
    return token


def pages(token: str) -> list[dict]:
    """Every Page this token can post to, with its own token on each row."""
    found = _get("/me/accounts", {"fields": "id,name,access_token"}, token=token)
    rows = found.get("data") or []
    if not rows:
        raise SystemExit(
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
        print(f"\nOne Page: {rows[0].get('name')} ({rows[0].get('id')})")
        return rows[0]

    print("\nPages this consent covers:\n")
    for index, row in enumerate(rows, start=1):
        print(f"  {index}. {row.get('name')}  ({row.get('id')})")
    picked = input("\nWhich one? ").strip()
    if not picked.isdigit() or not 1 <= int(picked) <= len(rows):
        raise SystemExit("Not one of those. Nothing was registered.")
    return rows[int(picked) - 1]


def register(base_url: str, api_token: str, payload: dict) -> None:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/accounts/facebook",
        json=payload,
        headers={"authorization": f"Bearer {api_token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"The gateway refused it ({response.status_code}): {response.text}")
    print(f"Registered with the gateway: {response.json().get('detail', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--gateway",
        default=os.environ.get("GATEWAY_BASE_URL", ""),
        help="gateway base URL; also read from GATEWAY_BASE_URL",
    )
    parser.add_argument(
        "--brand",
        default="",
        help="which original account this Page belongs to; the pipeline's --account",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print the line for the account's .env instead of registering",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help=(
            "print the Page access token instead of registering it, for "
            "sealing into a cluster secret by hand"
        ),
    )
    args = parser.parse_args()

    app_id = os.environ.get("FACEBOOK_APP_ID", "")
    app_secret = os.environ.get("FACEBOOK_APP_SECRET", "")
    if not app_id or not app_secret:
        raise SystemExit(
            "Set FACEBOOK_APP_ID and FACEBOOK_APP_SECRET in the environment.\n"
            "Neither is taken on the command line: argv is visible in `ps` and\n"
            "lands in shell history."
        )

    page = choose(pages(user_token(authorise(app_id), app_id, app_secret)))
    page_id = str(page.get("id") or "")
    print(f"\nAuthorised Page {page.get('name')} ({page_id})")

    if args.env:
        # The render host needs the Page id and nothing else: the token lives
        # on the gateway, which is what publishes. Same shape as the TikTok
        # open id it sits next to.
        print("\nAdd to accounts/<name>/.env on the render host:\n")
        print(f"FACEBOOK_PAGE_ID={page_id}")
        print("\nNothing was registered, so run this again without --env.")
        return

    if args.print_token:
        # Deliberately the only path that puts the token on a terminal, and it
        # has to be asked for. The default keeps it between this process and
        # the gateway.
        print("\nPage access token:")
        print(page.get("access_token", ""))
        return

    # The same GATEWAY_URL and GATEWAY_TOKEN the pipeline already uses, which
    # is what the other two authorise scripts read too.
    cfg = Settings()
    base_url = args.gateway or cfg.gateway_url
    api_token = os.environ.get("GATEWAY_API_TOKEN", "") or cfg.gateway_token
    if not base_url or not api_token:
        raise SystemExit(
            "Set GATEWAY_URL and GATEWAY_TOKEN in .env to register this, or\n"
            "pass --print-token and store the Page token by hand."
        )
    register(
        base_url,
        api_token,
        {
            "page_id": page_id,
            "access_token": page.get("access_token", ""),
            "username": str(page.get("name") or ""),
            "brand": args.brand,
        },
    )
    print(
        f"\nNext: FACEBOOK_PAGE_ID={page_id} in the render host's\n"
        f"accounts/<name>/.env, and a GATEWAY_SLOTS line ending account={page_id}\n"
        "in the homelab config."
    )


if __name__ == "__main__":
    main()
