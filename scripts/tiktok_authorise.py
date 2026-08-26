#!/usr/bin/env python
"""tiktok_authorise.py - turn a one-time browser consent into a stored account.

The same job `scripts/youtube_authorise.py` does, and a different flow, because
TikTok's OAuth is a plain authorisation code exchange with CSRF state and no
PKCE library worth pulling in for it. So this hand-rolls the loopback listener
that the Google script deliberately does not, and for the opposite reason:
there is no `google-auth-oauthlib` equivalent here, and the whole flow is one
redirect and one POST.

Run it once per account:

    uv run python scripts/tiktok_authorise.py

It reads `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` from the environment,
never from argv, which is visible in `ps` and lands in shell history. The result
is posted straight to the gateway so the refresh token never reaches a file or
a terminal scrollback on the way.

**The refresh token this returns is not the one that will be in use tomorrow.**
TikTok rotates it on every refresh and the gateway's refresher loop rewrites it
daily. So this value is a seed, and re-running this script is how an account is
recovered when that chain is ever broken.

The redirect URI has to match what the app declares in the TikTok developer
portal exactly, including the port, so it is a fixed port rather than port 0.
"""

from __future__ import annotations

import argparse
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Fixed, because it has to match the app's declared redirect URI character for
# character. TikTok rejects a mismatch with an error that names neither side.
REDIRECT_PORT = 8723
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

# Asked for together, in one authorisation, because adding a scope later means
# going back through the browser and re-consenting.
#
# `video.publish` is Direct Post and `video.upload` is the inbox path. Both,
# because which one is usable depends on an audit that has not happened and the
# consent screen is not somewhere to go twice. `video.list` is what carries the
# view and engagement counts, and without it nothing comes back at all.
# `user.info.basic` is what makes the open id readable here rather than pasted.
#
# Asking for scopes the app does not use is a named rejection reason at audit
# time, so this list should not grow speculatively.
SCOPES = "user.info.basic,video.publish,video.upload,video.list"


class _Catcher(http.server.BaseHTTPRequestHandler):
    """One request, then done. Holds the query string on the server object."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib's spelling
        self.server.query = urllib.parse.parse_qs(  # type: ignore[attr-defined]
            urllib.parse.urlparse(self.path).query
        )
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorised. You can close this tab and go back to the terminal.")

    def log_message(self, *_args) -> None:
        """Silence. The default writes every request to stderr."""


def authorise(client_key: str) -> str:
    """Open the browser, catch the code on loopback. Returns the code.

    `state` is compared on the way back rather than ignored. It is the only
    thing standing between this listener and any page the browser happens to
    load while it is open.
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
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _Catcher)
    server.query = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = f"{AUTH_URL}?{query}"
    print(
        "\nA browser is opening. Two things to watch for:\n"
        "  - Pick the account you mean. The wrong pick is not visible again\n"
        "    until the first publish.\n"
        f"  - If it does not open, paste this:\n    {url}\n"
    )
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    returned = server.query  # type: ignore[attr-defined]
    if not returned:
        raise SystemExit("No callback arrived within five minutes.")
    if returned.get("error"):
        raise SystemExit(
            f"TikTok refused the authorisation: {returned['error'][0]} "
            f"{returned.get('error_description', [''])[0]}"
        )
    if returned.get("state", [""])[0] != state:
        raise SystemExit("The state did not match. Nothing was exchanged.")
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

    if args.print_token or not args.gateway:
        if not args.gateway:
            print("\nNo gateway URL given, so nothing was registered.")
        print("\nRefresh token (seed only; the gateway rotates it daily):")
        print(tokens["refresh_token"])
        return

    api_token = os.environ.get("GATEWAY_API_TOKEN", "")
    if not api_token:
        raise SystemExit("Set GATEWAY_API_TOKEN to register, or use --print-token.")
    register(args.gateway, api_token, payload)


if __name__ == "__main__":
    main()
