#!/usr/bin/env python
"""tiktok_authorise.py - turn a one-time browser consent into a stored account.

The same job `scripts/youtube_authorise.py` does, and a different flow, because
TikTok's OAuth is a plain authorisation code exchange with CSRF state and no
PKCE library worth pulling in for it.

Run it once per account:

    uv run python scripts/tiktok_authorise.py

It reads `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` from the environment,
never from argv, which is visible in `ps` and lands in shell history. The result
is posted straight to the gateway so the refresh token never reaches a file or
a terminal scrollback on the way.

**There is no loopback listener, and that is not a simplification.** This ran
one until 2026-08-27, on a fixed port so the redirect URI could match character
for character. TikTok will not register the URI at all: the developer portal
rejects anything that does not begin with `https://`, `http://127.0.0.1:8723/`
and `https://127.0.0.1:8723/` alike. So the redirect goes to a page the gateway
already serves on a domain TikTok has verified, that page prints the code, and
the operator pastes it back here. One paste, once per account.

**The exchange stays on this machine.** The gateway could have done it and
saved the paste, but it is never told the client secret until the last step of
this same trip, and handing it over early would put a secret in a second place
for the sake of a one-off.

**The refresh token this returns is not the one that will be in use tomorrow.**
TikTok rotates it on every refresh and the gateway's refresher loop rewrites it
daily. So this value is a seed, and re-running this script is how an account is
recovered when that chain is ever broken.
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

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# It has to match the app's declared redirect URI character for character.
# TikTok rejects a mismatch with an error that names neither side.
#
# A page on the gateway rather than a loopback port, because the developer
# portal refuses to save a redirect URI that does not begin with `https://`,
# and a loopback address cannot have a certificate anybody trusts. The gateway
# already serves this host's privacy policy and terms off the same DNS
# verified domain, so the page costs one route and no new hosting.
REDIRECT_URI = os.environ.get(
    "TIKTOK_REDIRECT_URI", "https://gate.nordbye.it/tiktok/callback"
)

# Asked for together, in one authorisation, because adding a scope later means
# going back through the browser and re-consenting.
#
# `video.upload` is the inbox path and comes with the Content Posting API
# product. `video.list` carries the view and engagement counts, and without it
# nothing comes back at all. `user.info.basic` is what makes the open id
# readable here rather than pasted, and comes with Login Kit.
#
# **`video.publish` is here since 2026-08-28**, and it was deliberately absent
# before that. It is Direct Post, and the app only holds it once the Direct
# Post switch inside the Content Posting API product is on. Requesting a scope
# the app does not hold fails the authorisation rather than being quietly
# dropped, so this line and that switch move together or the consent trip
# breaks for the path that does work.
#
# The switch is now on for both configurations, because the audit that makes
# Direct Post do anything other than `SELF_ONLY` is being applied for. **If
# that application is refused and the switch goes back off, this line has to go
# back with it.**
#
# Asking for scopes the app does not use is a named rejection reason at audit
# time, so this list should not grow speculatively. `video.upload` stays
# because the inbox path is still what runs while the audit is pending.
SCOPES = "user.info.basic,video.publish,video.upload,video.list"


def authorise(client_key: str) -> str:
    """Open the browser, take the code back by hand. Returns the code.

    `state` is compared on the way back rather than ignored. Pasting the whole
    address is what makes that possible, and it is why the prompt asks for the
    address rather than for the code: a bare code carries no state to check.
    """
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )
    url = f"{AUTH_URL}?{query}"
    print(
        "\nA browser is opening. Three things to watch for:\n"
        "  - Pick the account you mean. The wrong pick is not visible again\n"
        "    until the first publish.\n"
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
            f"TikTok refused the authorisation: {returned['error'][0]} "
            f"{returned.get('error_description', [''])[0]}"
        )
    if returned.get("state", [""])[0] != state:
        raise SystemExit("The state did not match. Nothing was exchanged.")
    if not returned.get("code"):
        raise SystemExit("That address carries no code. Nothing was exchanged.")
    return returned["code"][0]


def exchange(code: str, client_key: str, client_secret: str) -> dict:
    """Trade the code for the first access and refresh token pair."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": urllib.parse.unquote(code),
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    payload = response.json()
    if payload.get("error"):
        raise SystemExit(
            f"Token exchange failed: {payload['error']} "
            f"{payload.get('error_description', '')}"
        )
    if not payload.get("refresh_token"):
        raise SystemExit(f"Token exchange returned no refresh token: {payload}")
    return payload


def register(base_url: str, api_token: str, payload: dict) -> None:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/accounts/tiktok",
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
    parser.add_argument("--username", default="", help="the @handle, for the admin UI")
    parser.add_argument(
        "--env",
        action="store_true",
        help="print the line for the account's .env instead of registering",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help=(
            "print the refresh token instead of registering it, for sealing "
            "into a cluster secret by hand"
        ),
    )
    args = parser.parse_args()

    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "")
    if not client_key or not client_secret:
        raise SystemExit(
            "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in the environment.\n"
            "Neither is taken on the command line: argv is visible in `ps` and\n"
            "lands in shell history."
        )

    tokens = exchange(authorise(client_key), client_key, client_secret)
    payload = {
        "open_id": tokens["open_id"],
        "client_key": client_key,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        "refresh_expires_in": tokens.get("refresh_expires_in"),
        "username": args.username,
    }
    print(f"\nAuthorised open id {tokens['open_id']}")
    print(f"Scopes granted: {tokens.get('scope', 'unknown')}")

    if args.env:
        # The render host needs the open id and nothing else: the credentials
        # live on the gateway, which is what publishes. Same shape as the
        # YouTube channel id it sits next to.
        print("\nAdd to accounts/<name>/.env on the render host:\n")
        print(f"TIKTOK_OPEN_ID={tokens['open_id']}")
        print("\nNothing was registered, so run this again without --env.")
        return

    if args.print_token:
        # Deliberately the only path that puts the token on a terminal, and it
        # has to be asked for. The default keeps it between this process and
        # the gateway.
        print("\nRefresh token (seed only; the gateway rotates it daily):")
        print(tokens["refresh_token"])
        return

    # The same GATEWAY_URL and GATEWAY_TOKEN the pipeline already uses, which
    # is what `youtube_authorise.py` reads too. Requiring a second pair of
    # names here was not a stricter setup, it was a failure after the browser
    # consent had already been spent, and the recovery for that is another
    # trip through the consent screen.
    cfg = Settings()
    base_url = args.gateway or cfg.gateway_url
    api_token = os.environ.get("GATEWAY_API_TOKEN", "") or cfg.gateway_token
    if not base_url or not api_token:
        raise SystemExit(
            "Set GATEWAY_URL and GATEWAY_TOKEN in .env to register this, or\n"
            "pass --print-token and store the refresh token by hand."
        )
    register(base_url, api_token, payload)
    print(
        "\nNext: TIKTOK_OPEN_ID="
        f"{tokens['open_id']} in the render host's accounts/<name>/.env, and a\n"
        f"GATEWAY_SLOTS line ending account={tokens['open_id']} in the homelab config."
    )


if __name__ == "__main__":
    main()
