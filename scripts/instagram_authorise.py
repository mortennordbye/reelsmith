#!/usr/bin/env python
"""instagram_authorise.py - turn a long-lived token into a stored account.

The fourth destination, and until now the only one with no script at all:
`docs/instagram-api-setup.md` walked you through a Meta app and ended with an
id and a token to paste into two files by hand. That was fine for one account
and is the step that does not survive a fifth.

Run it once per account:

    uv run python scripts/authorise.py instagram --account thewholequote

It opens the Meta app dashboard on the page the token comes from, walks the
three steps there, then asks for the long-lived user token on a prompt rather
than taking it on argv, which is visible in `ps` and lands in shell history.
From there it does what the other three trips do: read the account back from
Meta rather than taking it on trust, register it with the gateway, and write
`IG_USER_ID` into the account's `.env`.

**Opening the dashboard is the point rather than a nicety.** The YouTube trip
opens a browser and the operator is where they need to be; this one printed the
name of a documentation file, which is the same answer as "look it up". These
steps happen once per identity and nobody remembers them in between.

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
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import consent  # noqa: E402  - after the sys.path insert above


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

    A refusal here is **not** fatal, and the first version of this was wrong
    about that in the direction that blocks the normal path. `ig_refresh_token`
    does refuse a short-lived token, which is the check worth wanting: that
    mistake otherwise surfaces as an account that published once and stopped,
    with a failure saying the token is invalid rather than that it was born
    wrong. But Meta refuses a token under 24 hours old as well, and the
    ordinary way to get one of these is the dashboard's Generate token button,
    which hands back a long-lived token that is seconds old. Matching on the
    wording of the refusal is no answer either, since that is Meta's text to
    change.

    So it warns and carries on with what was pasted, recording no expiry. That
    is not a shrug: an unknown expiry is what the gateway already treats as due
    for refresh, so a genuinely short-lived token fails there, visibly, on a
    service that is watched, rather than here on a trip that would have to be
    walked again.
    """
    try:
        data = _graph(host, "/refresh_access_token", {
            "grant_type": "ig_refresh_token",
            "access_token": token,
        })
    except consent.ConsentError as exc:
        print(
            f"\nMeta would not refresh that token:\n  {exc}\n"
            f"That is expected for a token minted in the last 24 hours, which\n"
            f"a freshly generated one is, and it has nearly 60 days either way.\n"
            f"It is not expected for a short-lived token, which would stop\n"
            f"working in about an hour. Registering with no expiry recorded,\n"
            f"which the gateway reads as due for refresh."
        )
        return token, None
    return data.get("access_token") or token, data.get("expires_in")


# The dashboard page holding the Generate token button, and the apps list to
# fall back to when nobody has said which app this is.
APPS_URL = "https://developers.facebook.com/apps/"
SETUP_PATH = "instagram-business/API-setup-with-instagram-login/"


def open_dashboard(app_id: str) -> None:
    """Put the operator on the page the token comes from.

    The YouTube trip opens a browser and the operator is where they need to be.
    This one used to print the name of a documentation file, which is the same
    answer as "look it up", and the steps below are not ones anybody remembers
    between accounts because they happen once per identity.

    The app id is public, so a deep link costs nothing. Without one this opens
    the apps list, which is still one click from the right place rather than a
    file path.
    """
    url = f"{APPS_URL}{app_id}/{SETUP_PATH}" if app_id else APPS_URL
    print(
        "\nA browser is opening on the Meta app dashboard. Three steps there,\n"
        "in this order, and the second is the one that is easy to miss:\n"
        "\n"
        "  1. Instagram, then app roles: add the account as an Instagram\n"
        "     tester. This is what grants Standard Access to an account you\n"
        "     own, and it is why none of this needs App Review.\n"
        "  2. Accept the invite from Instagram itself, signed in as that\n"
        "     account: Settings, Website permissions, Tester invites. Until\n"
        "     this is accepted the account is not offered in step 3, and the\n"
        "     error much later names nothing useful.\n"
        "  3. Instagram, API setup with Instagram business login, Generate\n"
        "     access tokens. Pick the account and copy what it hands back.\n"
        "     That button returns a long-lived token, so there is no\n"
        "     short-lived exchange to do by hand.\n"
        f"\n  If it does not open: {url}\n"
    )
    if not app_id:
        print(
            "  No IG_APP_ID in .env, so this is the apps list rather than the\n"
            "  page itself. Setting it deep links every trip after this one.\n"
        )
    webbrowser.open(url)


def account_of(host: str, token: str, api_version: str) -> dict:
    """Which account this token actually belongs to.

    Read back rather than taken on trust, the same as `channel_of` on the
    YouTube trip. A token generated next to the wrong account in the dashboard
    looks exactly like the right one.

    **`/me` returns two ids and the obvious one is wrong**, which is the same
    shape of trap as a Facebook Page having two. On the Instagram Login path
    `id` is the app-scoped user id and `user_id` is the Instagram Business
    account id. The publisher addresses `graph.instagram.com/{id}/media` with
    the second, so it is `user_id` that belongs in `IG_USER_ID` and on the
    account row. Both are seventeen digits and neither looks more correct than
    the other, so nothing about the wrong one is visible until the first
    publish fails against a node that does not exist.

    Measured on this account's own token: `id` is 37342907808657598 and
    `user_id` is 17841441696714445, and it is the second that has been
    publishing since 2026-08-01.
    """
    found = _graph(
        host,
        f"/{api_version}/me",
        {"fields": "id,user_id,username,account_type", "access_token": token},
    )
    if not found.get("user_id"):
        raise consent.ConsentError(
            f"That token answered with no user_id: {found}\n"
            f"This trip is the Instagram Login path, which is what\n"
            f"IG_GRAPH_HOST defaults to. A token minted through Facebook Login\n"
            f"answers on graph.facebook.com and reaches its Instagram account\n"
            f"through a Page instead, which is a different flow this does not do."
        )
    kind = str(found.get("account_type") or "").upper()
    if kind == "PERSONAL":
        raise consent.ConsentError(
            "That account is Personal. The Content Publishing API does not "
            "work with it and neither do full insights. Switch it to Business, "
            "which is free and reversible, and run this again."
        )
    if kind and kind != "BUSINESS":
        print(
            f"\nThat account is {kind} rather than Business. Reels publishing is "
            f"reported to work on Business only, so this may fail at the first "
            f"publish."
        )
    return found


def trip(args: argparse.Namespace) -> consent.Trip:
    """The paste, and what it produced. Called by `authorise.py`."""
    # Before the prompt, so a misspelt account name is caught before a
    # credential has been typed into a terminal.
    brand = consent.brand_for(args.account, args.brand)
    cfg = consent.account_settings(args.account)

    if not args.no_browser:
        open_dashboard(cfg.ig_app_id)

    print(
        "\nPaste the long-lived Instagram user token. It is not echoed, so\n"
        "nothing will appear. It is not taken on the command line either:\n"
        "argv is visible in `ps` and lands in shell history."
    )
    pasted = getpass.getpass("Token: ").strip()
    if not pasted:
        raise consent.ConsentError("Nothing pasted. Nothing was registered.")

    token, expires_in = refresh(cfg.ig_graph_host, pasted)
    account = account_of(cfg.ig_graph_host, token, cfg.ig_api_version)
    username = str(account.get("username") or "")
    print(f"\nAccount:  @{username or '(no username)'}  ({account.get('account_type', '?')})")
    print(f"Id:       {account['user_id']}   [the one that publishes]")
    print(f"App id:   {account['id']}   [app scoped, not this]")
    if input("\nIs that the account to publish to? [y/N] ").strip().lower() != "y":
        raise consent.ConsentError("Stopped. Nothing was stored.")

    return consent.Trip(
        platform="instagram",
        account_id=str(account["user_id"]),
        username=username,
        payload={
            "account_id": str(account["user_id"]),
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
        "--no-browser",
        action="store_true",
        help="skip opening the Meta app dashboard",
    )
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
