#!/usr/bin/env python
"""instagram_authorise.py - turn a long-lived token into a stored account.

The fourth destination, and until now the only one with no script at all:
`docs/instagram-api-setup.md` walked you through a Meta app and ended with an
id and a token to paste into two files by hand. That was fine for one account
and is the step that does not survive a fifth.

Run it once per account:

    uv run python scripts/authorise.py instagram --account thewholequote

It asks for the long-lived user token on a prompt rather than taking it on
argv, which is visible in `ps` and lands in shell history. From there it does
what the other three trips do: read the account back from Meta rather than
taking it on trust, register it with the gateway, and write `IG_USER_ID` into
the account's `.env`.

**This is a paste rather than a browser trip, and that is a real difference
from the other three.** Instagram Login has an authorisation code flow like
everything else, and using it would need an `/instagram/callback` route on the
gateway. That route is half the change: the other half is an `Exact` match in
`k8s/talos/apps/reelsmith/httproute.yaml` in the homelab repo, because that
allowlist 404s anything not named in it, and `CLAUDE.md` records the TikTok and
Facebook rollouts walking into exactly that. So the browser half is costed
rather than absent: one route here, one homelab PR, and this script's `trip`
gains an `authorise()` like the other two. Everything after the token is
already shared and would not change.

**The token is read back before it is stored.** `GET /me` answers for whichever
account the token actually belongs to, which is the only cheap moment to notice
that the wrong one was copied out of the Graph API Explorer. The other three
trips all do this and the manual setup never could.

**A short-lived token registers just as cleanly and stops working in an hour.**
So this refreshes before registering: `ig_refresh_token` only accepts a
long-lived token, which turns "you pasted the wrong one" from a failure at the
first publish into a refusal here, and hands back a full 60 days besides.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402  - after the sys.path insert above
from scripts import consent  # noqa: E402


def _graph(host: str, path: str, params: dict) -> dict:
    response = httpx.get(f"{host.rstrip('/')}{path}", params=params, timeout=30)
    payload = response.json() if response.content else {}
    if not isinstance(payload, dict) or payload.get("error"):
        error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        raise consent.ConsentError(
            f"Meta refused {path}: {error.get('message') or response.text[:400]}"
        )
    return payload


def refresh(host: str, token: str) -> tuple[str, int | None]:
    """Trade the pasted token for a fresh 60 days, and prove it is long-lived.

    `ig_refresh_token` refuses a short-lived token, which is the check worth
    having: the mistake this catches otherwise surfaces as an account that
    published once and then stopped, with a failure saying the token is invalid
    rather than saying it was born wrong. The same shape as the third step of
    the Facebook trip, and there for the same reason.

    Meta also refuses a token under 24 hours old, which is not an error worth
    acting on: a token that new has 59 days left. So that one refusal is
    allowed through with what was pasted.
    """
    try:
        data = _graph(host, "/refresh_access_token", {
            "grant_type": "ig_refresh_token",
            "access_token": token,
        })
    except consent.ConsentError as exc:
        if "24 hours" in str(exc):
            print("\nMeta says the token is under 24 hours old, so it has nearly 60 days left.")
            return token, None
        raise
    return data.get("access_token") or token, data.get("expires_in")


def account_of(host: str, token: str, api_version: str) -> dict:
    """Which account this token actually belongs to.

    Read back rather than taken on trust, the same as `channel_of` on the
    YouTube trip. An id copied out of the Graph API Explorer next to the wrong
    account looks exactly like the right one.
    """
    found = _graph(host, f"/{api_version}/me", {"fields": "id,username", "access_token": token})
    if not found.get("id"):
        raise consent.ConsentError(f"That token names no account: {found}")
    return found


def trip(args: argparse.Namespace) -> consent.Trip:
    """The paste, and what it produced. Called by `authorise.py`."""
    # Before the prompt, so a misspelt account name is caught before a
    # credential has been typed into a terminal.
    brand = consent.brand_for(args.account, args.brand)
    cfg = Settings()

    print(
        "\nPaste the long-lived Instagram user token. It is not echoed, and it\n"
        "is not taken on the command line: argv is visible in `ps`.\n"
        "Where to get one is docs/instagram-api-setup.md."
    )
    pasted = getpass.getpass("Token: ").strip()
    if not pasted:
        raise consent.ConsentError("Nothing pasted. Nothing was registered.")

    token, expires_in = refresh(cfg.ig_graph_host, pasted)
    account = account_of(cfg.ig_graph_host, token, cfg.ig_api_version)
    username = str(account.get("username") or "")
    print(f"\nAccount:  {username or '(no username)'}")
    print(f"Id:       {account['id']}")
    if input("\nIs that the account to publish to? [y/N] ").strip().lower() != "y":
        raise consent.ConsentError("Stopped. Nothing was stored.")

    return consent.Trip(
        platform="instagram",
        account_id=str(account["id"]),
        username=username,
        payload={
            "account_id": str(account["id"]),
            "access_token": token,
            "username": username,
            "expires_in": expires_in,
            "brand": brand,
            # The keyword mechanic is this surface alone, and without this the
            # account produces no webhooks at all. Left on even though nothing
            # advertises a keyword today, because the failure of turning it off
            # looks exactly like "nobody is messaging us".
            "subscribe": not args.no_subscribe,
        },
        secret_label="IG_ACCESS_TOKEN",
        secret_value=token,
        notes=[
            "The gateway holds this token and refreshes its own copy. This\n"
            "machine's copy lives in accounts/<name>/data/ig_token.json and is\n"
            "refreshed by `main.py --refresh-token`, which nothing runs for you.",
        ],
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    consent.add_common_arguments(parser)
    parser.add_argument(
        "--no-subscribe",
        action="store_true",
        help="skip subscribed_apps, for an account whose subscription is already known good",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args()
    consent.finish(args, trip(args))


if __name__ == "__main__":
    main()
