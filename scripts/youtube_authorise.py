#!/usr/bin/env python
"""youtube_authorise.py - turn a one-time browser consent into a stored channel.

Google hands out a refresh token exactly once per authorisation, and from then
on it is the whole of the gateway's ability to publish. This script does the
browser half, reads back which channel the consent actually covered, and posts
the result straight to the gateway so the token never lands in a file, a shell
history or a terminal scrollback on the way.

Run it once per channel:

    uv run python scripts/youtube_authorise.py

It reads `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` from `.env`, the same
way every other secret in this repo is read. The console's downloaded JSON
works too, if you would rather not keep the pair on disk:

    uv run python scripts/youtube_authorise.py ~/Downloads/client_secret_*.json

Neither route takes the secret on the command line, and that is deliberate:
argv is visible in `ps` and lands in shell history.

`google-auth-oauthlib` rather than a hand-rolled loopback listener, because
this is the authorisation code flow with PKCE and a single-shot local server,
and none of that is worth reimplementing for a script that runs twice a year.
The gateway's own token refresh is a different case and stays on httpx: one
async POST, inside a service that has no room for a synchronous client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Asked for together, in one authorisation, because adding a scope later means
# going back through the browser and re-consenting.
#
# `youtube.upload` publishes. `youtube.readonly` is what lets this script read
# the channel id back instead of asking for a paste, which is also the check
# that consent landed on the channel you meant. `yt-analytics.readonly` is for
# the feedback loop that does not exist yet; it costs nothing now and a second
# trip through the consent screen later.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


class YouTubeClient(BaseSettings):
    """The OAuth client pair, from `.env` like everything else in this repo.

    A settings class of its own rather than a field on `config.Settings`,
    because the pipeline never touches these and should not fail to start over
    a variable it has no use for.
    """

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    youtube_client_id: str = ""
    youtube_client_secret: str = ""


def _flow(secrets_file: Path | None) -> InstalledAppFlow:
    """The console's JSON if given, otherwise the pair from `.env`."""
    if secrets_file:
        return InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes=SCOPES)

    client = YouTubeClient()
    if not client.youtube_client_id or not client.youtube_client_secret:
        raise SystemExit(
            "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env, or pass\n"
            "the client_secret_*.json the Google Cloud console downloaded."
        )
    return InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client.youtube_client_id,
                "client_secret": client.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )


def authorise(secrets_file: Path | None):
    """Open the browser, catch the code on loopback, exchange it.

    `access_type=offline` asks for a refresh token and `prompt=consent` insists
    on one. Without the second, a re-authorisation of a client that has already
    been granted returns an access token and no refresh token, which looks like
    success and stores nothing usable.
    """
    flow = _flow(secrets_file)
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
        ),
        success_message=(
            "Authorised. You can close this tab and go back to the terminal."
        ),
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
        raise SystemExit(
            "That authorisation covers no channel. It usually means consent was\n"
            "granted for a Google account that has not created one yet."
        )
    return items[0]


def register(base_url: str, api_token: str, payload: dict) -> None:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/accounts/youtube",
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
        "secrets_file",
        type=Path,
        nargs="?",
        help=(
            "the client_secret_*.json from the Google Cloud console; omit to "
            "use YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET from .env"
        ),
    )
    parser.add_argument(
        "--gateway",
        default=os.environ.get("GATEWAY_BASE_URL", ""),
        help="gateway base URL; also read from GATEWAY_BASE_URL",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help=(
            "print the refresh token instead of registering it, for sealing "
            "into a cluster secret by hand"
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=(
            "write the credentials to data/yt_token.json instead of "
            "registering them, for proving an upload from the Mac before the "
            "gateway can accept them"
        ),
    )
    args = parser.parse_args()

    if args.secrets_file and not args.secrets_file.exists():
        raise SystemExit(f"No such file: {args.secrets_file}")

    credentials = authorise(args.secrets_file)
    if not credentials.refresh_token:
        raise SystemExit(
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
        raise SystemExit("Stopped. Nothing was stored.")

    payload = {
        "channel_id": channel["id"],
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
        "username": snippet.get("customUrl", ""),
    }

    if args.save:
        # The same home `data/ig_token.json` has, and gitignored by the same
        # `data/*` rule. A stepping stone rather than the destination: once the
        # gateway can take the registration, the token belongs in the cluster
        # secret and this file should be deleted.
        token_file = ROOT / "data" / "yt_token.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(payload, indent=2) + "\n")
        token_file.chmod(0o600)
        print(f"\nWrote {token_file}. Delete it once the gateway holds these.")
        return

    if args.print_token:
        # Deliberately the only path that puts the token on a terminal, and it
        # has to be asked for. The default keeps it between this process and
        # the gateway.
        print("\nRefresh token (store it in the cluster secret, not in the repo):")
        print(credentials.refresh_token)
        return

    api_token = os.environ.get("GATEWAY_API_TOKEN", "")
    if not args.gateway or not api_token:
        raise SystemExit(
            "Set GATEWAY_BASE_URL and GATEWAY_API_TOKEN to register this, or\n"
            "pass --print-token to seal the refresh token into a secret by hand."
        )
    register(args.gateway, api_token, payload)


if __name__ == "__main__":
    sys.exit(main())
