#!/usr/bin/env python
"""youtube_authorise.py - turn a one-time browser consent into a stored channel.

Google hands out a refresh token exactly once per authorisation, and from then
on it is the whole of the gateway's ability to publish. This script does the
browser half, reads back which channel the consent actually covered, and hands
the result to `scripts/consent.py`, which registers it with the gateway and
writes the channel id into the account's `.env`. The token never lands in a
file, a shell history or a terminal scrollback on the way.

Run it once per channel:

    uv run python scripts/authorise.py youtube --account thewholequote

It reads `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` from `.env`, the same
way every other secret in this repo is read. The console's downloaded JSON
works too, if you would rather not keep the pair on disk:

    uv run python scripts/authorise.py youtube --account x ~/Downloads/client_secret_*.json

Neither route takes the secret on the command line, and that is deliberate:
argv is visible in `ps` and lands in shell history.

`google-auth-oauthlib` rather than a hand-rolled loopback listener, because
this is the authorisation code flow with PKCE and a single-shot local server,
and none of that is worth reimplementing for a script that runs twice a year.
The gateway's own token refresh is a different case and stays on httpx: one
async POST, inside a service that has no room for a synchronous client.

**This is the one trip that still uses a loopback redirect**, unlike TikTok's
and Facebook's, which land on a page the gateway serves. Google accepts one,
the library already runs the listener, and there is nothing to paste back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import consent  # noqa: E402  - after the sys.path insert above

# Asked for together, in one authorisation, because adding a scope later means
# going back through the browser and re-consenting.
#
# `youtube.upload` publishes. `youtube.readonly` is what lets this script read
# the channel id back instead of asking for a paste, which is also the check
# that consent landed on the channel you meant. `yt-analytics.readonly` is what
# the insights sweep asks the Analytics API with.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def _flow(secrets_file: Path | None, account: str) -> InstalledAppFlow:
    """The console's JSON if given, otherwise the pair from `.env`.

    The account's `.env` layered over the root one, because the client pair is
    an app credential and may sit in either. See `consent.account_settings`.
    """
    if secrets_file:
        return InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes=SCOPES)

    cfg = consent.account_settings(account)
    if not cfg.youtube_client_id or not cfg.youtube_client_secret:
        raise consent.ConsentError(
            f"No YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env or in\n"
            f"accounts/{account}/.env. They identify the Google Cloud project\n"
            f"rather than the channel, so one pair authorises every account and\n"
            f"the root .env is where it belongs. Or pass the client_secret_*.json\n"
            f"the console downloaded."
        )
    return InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": cfg.youtube_client_id,
                "client_secret": cfg.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )


def authorise(secrets_file: Path | None, account: str):
    """Open the browser, catch the code on loopback, exchange it.

    `access_type=offline` asks for a refresh token and `prompt=consent` insists
    on one. Without the second, a re-authorisation of a client that has already
    been granted returns an access token and no refresh token, which looks like
    success and stores nothing usable.
    """
    flow = _flow(secrets_file, account)
    return flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "\nA browser is opening. Two things to watch for:\n"
            "  - Pick the channel you mean. If this account has a personal\n"
            "    channel as well as the brand one, both are offered here and\n"
            "    the wrong pick is not visible again until the first upload.\n"
            "  - An unverified app warning is expected. Advanced, then proceed.\n"
            # The other two trips print their URL and this one did not, which
            # leaves nothing to fall back on when the browser does not open or
            # opens the wrong profile. That is the common case rather than the
            # rare one: the consent has to happen in a browser already signed
            # in to the Google account that manages the channel.
            "  - If it does not open, or opens a profile that is not signed in\n"
            "    to the right Google account, paste this instead:\n    {url}\n"
        ),
        success_message="Authorised. You can close this tab and go back to the terminal.",
    )


def channel_of(credentials) -> dict:
    """Which channel the consent actually covered.

    Read back rather than taken on trust. `mine=true` answers for the channel
    the authorisation was granted against, so this is the only moment the wrong
    pick at the consent screen is cheap to notice.
    """
    response = httpx.get(
        CHANNELS_URL,
        params={"part": "snippet", "mine": "true"},
        headers={"authorization": f"Bearer {credentials.token}"},
        timeout=30,
    )
    response.raise_for_status()
    items = response.json().get("items") or []
    if not items:
        raise consent.ConsentError(
            "That authorisation covers no channel. It usually means consent was\n"
            "granted for a Google account that has not created one yet."
        )
    return items[0]


def trip(args: argparse.Namespace) -> consent.Trip:
    """The browser half, and what it produced. Called by `authorise.py`."""
    # Before the browser, so a misspelt account name costs nothing rather than
    # a spent consent screen.
    brand = consent.brand_for(args.account, args.brand)

    if args.secrets_file and not args.secrets_file.exists():
        raise consent.ConsentError(f"No such file: {args.secrets_file}")

    credentials = authorise(args.secrets_file, args.account)
    if not credentials.refresh_token:
        raise consent.ConsentError(
            "Google returned no refresh token, so nothing here could publish\n"
            "unattended. That happens when the client has been authorised\n"
            "before and consent was not forced. Revoke this app's access at\n"
            "https://myaccount.google.com/permissions and run this again."
        )

    channel = channel_of(credentials)
    snippet = channel["snippet"]
    print(f"\nChannel:  {snippet.get('title', '?')}")
    print(f"Handle:   {snippet.get('customUrl', '(none)')}")
    print(f"Id:       {channel['id']}")
    if input("\nIs that the channel to publish to? [y/N] ").strip().lower() != "y":
        raise consent.ConsentError("Stopped. Nothing was stored.")

    return consent.Trip(
        platform="youtube",
        account_id=channel["id"],
        username=snippet.get("customUrl", ""),
        payload={
            "channel_id": channel["id"],
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "username": snippet.get("customUrl", ""),
            "brand": brand,
        },
        # No token file, unlike the Instagram side. `data/ig_token.json` exists
        # because a Meta token is refreshed every 60 days and the new one has
        # to be written back somewhere; a Google refresh token does not rotate
        # and does not expire on a clock, so `.env` can simply hold it.
        secret_label="YOUTUBE_REFRESH_TOKEN",
        secret_value=credentials.refresh_token,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    consent.add_common_arguments(parser)
    parser.add_argument(
        "secrets_file",
        type=Path,
        nargs="?",
        help=(
            "the client_secret_*.json from the Google Cloud console; omit to "
            "use YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET from .env"
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args()
    consent.finish(args, trip(args))


if __name__ == "__main__":
    main()
