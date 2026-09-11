#!/usr/bin/env python
"""tiktok_authorise.py - turn a one-time browser consent into a stored account.

The same job `scripts/youtube_authorise.py` does, and a different flow, because
TikTok's OAuth is a plain authorisation code exchange with CSRF state and no
PKCE library worth pulling in for it. Everything after the exchange is shared:
`scripts/consent.py` registers the result and writes the open id into the
account's `.env`.

Run it once per account:

    uv run python scripts/authorise.py tiktok --account <name>

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
import sys
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import consent  # noqa: E402  - after the sys.path insert above

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
REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "https://gate.nordbye.it/tiktok/callback")

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
    """Open the browser, take the code back by hand. Returns the code."""
    state = consent.new_state()
    query = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )
    return consent.paste_code(
        f"{AUTH_URL}?{query}",
        state,
        platform="TikTok",
        watch_for=[
            "Pick the account you mean. The wrong pick is not visible again\n"
            "    until the first publish.",
            "It lands on a page on the gateway showing a code. Copy the whole\n"
            "    address bar, not just the code.",
        ],
    )


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
        raise consent.ConsentError(
            f"Token exchange failed: {payload['error']} {payload.get('error_description', '')}"
        )
    if not payload.get("refresh_token"):
        raise consent.ConsentError(f"Token exchange returned no refresh token: {payload}")
    return payload


def trip(args: argparse.Namespace) -> consent.Trip:
    """The browser half, and what it produced. Called by `authorise.py`."""
    # Both before the browser, so neither a misspelt account name nor a shell
    # that cannot prompt costs a spent consent screen.
    consent.require_terminal()
    brand = consent.brand_for(args.account, args.brand)

    # The environment first for a one-off, then `.env` layered by account, the
    # same as the other two trips. These are the sandbox's credentials and that
    # is permanent rather than a stage; the client key starts `sb`.
    cfg = consent.account_settings(args.account)
    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "") or cfg.tiktok_client_key
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "") or cfg.tiktok_client_secret
    if not client_key or not client_secret:
        raise consent.ConsentError(
            f"No TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET, in the environment\n"
            f"or in .env or accounts/{args.account}/.env. They identify the app\n"
            f"rather than the account, so one pair serves every account and the\n"
            f"root .env is where the pair belongs."
        )

    tokens = exchange(authorise(client_key), client_key, client_secret)
    return consent.Trip(
        platform="tiktok",
        account_id=tokens["open_id"],
        username=args.username,
        payload={
            "open_id": tokens["open_id"],
            "client_key": client_key,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "refresh_expires_in": tokens.get("refresh_expires_in"),
            "username": args.username,
            "brand": brand,
        },
        secret_label="TIKTOK_REFRESH_TOKEN",
        secret_value=tokens["refresh_token"],
        notes=[
            f"Scopes granted: {tokens.get('scope', 'unknown')}",
            "The refresh token is a seed only; the gateway rotates it daily.",
        ],
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    consent.add_common_arguments(parser)
    parser.add_argument("--username", default="", help="the @handle, for the admin UI")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args()
    consent.finish(args, trip(args))


if __name__ == "__main__":
    main()
