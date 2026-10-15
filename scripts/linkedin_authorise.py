#!/usr/bin/env python
"""linkedin_authorise.py - turn a browser consent into a stored LinkedIn profile.

    uv run python scripts/authorise.py linkedin --account <name>

It reads `LINKEDIN_CLIENT_ID` and `LINKEDIN_CLIENT_SECRET` from the environment
or either `.env`, never from argv, which is visible in `ps` and lands in shell
history.

Three steps:

1. The browser consent, which returns a code.
2. The code for an access token. The self-serve products issue a 60 day token
   and **no refresh token**, so this script is also how the token is renewed:
   walk it again before the panel's countdown runs out.
3. `GET /v2/userinfo`, whose `sub` is the profile's id and the account key.

**The redirect lands on the gateway**, at `/linkedin/callback`, like the TikTok
and Facebook trips. LinkedIn would take a `localhost` redirect; the reason to
land on the gateway anyway is one kind of page for every trip. The homelab
httproute has to name the path or the trip lands on a 404.

The app needs two self-serve products, *Sign In with LinkedIn using OpenID
Connect* and *Share on LinkedIn*. Neither costs anything or needs review.
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

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
APPS_URL = "https://www.linkedin.com/developers/apps/"

# Character for character what the app lists under Authorized redirect URLs.
REDIRECT_URI = os.environ.get(
    "LINKEDIN_REDIRECT_URI", "https://gate.nordbye.it/linkedin/callback"
)

# `openid profile` is what makes `/v2/userinfo` answer, which is where the
# account key comes from. `w_member_social` is the post. Reading post
# statistics needs `r_member_postAnalytics`, which is not self-serve and is
# deliberately not asked for: asking for a scope the app was not granted
# breaks the consent screen rather than being ignored.
SCOPES = "openid profile w_member_social"


def setup_pages(client_id: str) -> list[tuple[str, bool, str]]:
    """What has to be true before this trip can work. Printed, never opened."""
    return [
        (
            f"{APPS_URL}{client_id}/products",
            True,
            "Sign In with LinkedIn using OpenID Connect and Share on LinkedIn\n"
            "     are both added. Without the first, /v2/userinfo refuses and the\n"
            "     trip cannot learn whose profile it was.",
        ),
        (
            f"{APPS_URL}{client_id}/auth",
            True,
            "The redirect URI below is listed under Authorized redirect URLs.",
        ),
    ]


def authorise(client_id: str) -> str:
    """Open the browser, take the code back by hand. Returns the code."""
    state = consent.new_state()
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
        }
    )
    return consent.paste_code(
        f"{AUTH_URL}?{query}",
        state,
        platform="LinkedIn",
        watch_for=[
            "Sign in as the person the posts should come from. The consent is\n"
            "    granted by whoever the browser is signed in as.",
            "It lands on a page on the gateway showing a code. Copy the whole\n"
            "    address bar, not just the code.",
        ],
    )


def exchange(code: str, client_id: str, client_secret: str) -> tuple[str, int]:
    """The code for an access token. Returns the token and its lifetime."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": urllib.parse.unquote(code),
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    payload = response.json() if response.content else {}
    token = payload.get("access_token", "") if isinstance(payload, dict) else ""
    if response.status_code >= 400 or not token:
        reason = (payload or {}).get("error_description") if isinstance(payload, dict) else ""
        raise consent.ConsentError(
            f"LinkedIn refused the code exchange: {reason or response.text[:400]}"
        )
    return token, int(payload.get("expires_in") or 0)


def whoami(token: str) -> dict:
    """The OpenID profile. `sub` is the account key."""
    response = httpx.get(
        USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    if response.status_code >= 400:
        raise consent.ConsentError(
            f"/v2/userinfo refused ({response.status_code}). Is Sign In with LinkedIn\n"
            f"using OpenID Connect added to the app? {response.text[:300]}"
        )
    profile = response.json()
    if not profile.get("sub"):
        raise consent.ConsentError("/v2/userinfo returned no sub.")
    return profile


def trip(args: argparse.Namespace) -> consent.Trip:
    """The browser half, and what it produced. Called by `authorise.py`."""
    consent.require_terminal()
    brand = consent.brand_for(args.account, args.brand)

    cfg = consent.account_settings(args.account)
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "") or cfg.linkedin_client_id
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "") or cfg.linkedin_client_secret
    if not client_id:
        raise consent.ConsentError(
            f"No LINKEDIN_CLIENT_ID, in the environment or in .env or\n"
            f"accounts/{args.account}/.env. It is on the app's Auth tab at\n"
            f"{APPS_URL}, and it identifies the app rather than the person."
        )

    if not args.no_browser:
        consent.list_prerequisites(setup_pages(client_id))
        print(f"  The redirect URI this trip uses: {REDIRECT_URI}\n")

    if not client_secret:
        client_secret = consent.ask_secret(
            "LINKEDIN_CLIENT_SECRET",
            what="The Auth tab, Primary Client Secret.",
            where=f"{APPS_URL}{client_id}/auth",
        )

    token, expires_in = exchange(authorise(client_id), client_id, client_secret)
    profile = whoami(token)
    person_id = str(profile["sub"])
    name = str(profile.get("name") or "")
    consent.confirm(
        what="About to register this LinkedIn profile:",
        lines=[("name", name or "?"), ("id", person_id)],
        account=args.account,
        brand=brand,
    )
    days = expires_in // 86_400 if expires_in else 60
    return consent.Trip(
        platform="linkedin",
        account_id=person_id,
        username=name,
        payload={
            "person_id": person_id,
            "access_token": token,
            # LinkedIn says 60 days; if it ever says nothing, 60 days is what
            # the countdown should assume rather than no countdown at all.
            "expires_in": expires_in or 60 * 86_400,
            "username": name,
            "brand": brand,
        },
        secret_label="LINKEDIN_ACCESS_TOKEN",
        secret_value=token,
        notes=[
            f"The token lasts {days} days and cannot be refreshed. The panel warns\n"
            f"two weeks before it runs out; this trip is how it is renewed.",
        ],
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    consent.add_common_arguments(parser)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="skip listing the setup pages",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args()
    consent.finish(args, trip(args))


if __name__ == "__main__":
    main()
