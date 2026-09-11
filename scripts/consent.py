"""The half of a consent trip that is the same on every platform.

Three platforms, three OAuth flows, and one tail: read the gateway's address
and token, post the result to the registration route, write the account key
into `accounts/<name>/.env`, and say what is left. That tail was copied into
`youtube_authorise.py`, `tiktok_authorise.py` and `facebook_authorise.py`
verbatim, which is three places to fix when a fourth destination arrives and
three places for the argument surface to drift apart in the meantime.

What is deliberately *not* here is the flow itself. Google's is the authorisation
code flow with PKCE behind `google-auth-oauthlib`, TikTok's and Facebook's are a
browser trip that lands on a page the gateway serves and a code pasted back, and
Facebook's has a second exchange that nothing else needs. Those differ down to
the error messages, and a single `authorise()` covering all three would be a
switch statement wearing an abstraction's clothes.

**The brand is read from the account, never derived from `--account`.** That
looks like the obvious default and it is wrong here: account 1's directory is
`nightlybuild` and every one of its gateway rows is grouped under
`thenightlybuild`, because the first three were registered without an explicit
brand and took the handle-derived value. Defaulting the brand to the directory
name would regroup all of them on the next re-authorisation, which is precisely
the failure `CLAUDE.md` records costing a re-registration. So the brand is a
`BRAND=` line in the account's own `.env`, written down once where the rest of
that identity already lives, and absent it nothing is sent and the gateway
derives from the handle exactly as it does today.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ACCOUNTS_DIR, Settings  # noqa: E402  - after the sys.path insert

# Every destination the gateway knows how to publish to, and what each one
# calls its account key on the render host. The key is what `--enqueue` reads
# to decide whether to fan out to that platform at all, so a trip that
# registers a destination and does not write its key produces an account the
# gateway can publish for and the pipeline never queues anything to.
ENV_KEYS = {
    "instagram": "IG_USER_ID",
    "youtube": "YOUTUBE_CHANNEL_ID",
    "tiktok": "TIKTOK_OPEN_ID",
    "facebook": "FACEBOOK_PAGE_ID",
}


@dataclass(frozen=True)
class Trip:
    """What one finished consent trip produced.

    The flow builds this and hands it to `finish`, which is the whole of the
    shared tail. `payload` is the registration route's body and is the one
    field that differs in shape between platforms, which is why it is a dict
    rather than four optional columns.
    """

    platform: str
    # The opaque account key: a channel id, an open id, a Page id, an Instagram
    # user id. What `accounts.account_id` holds and what a `GATEWAY_SLOTS` line
    # names. Deliberately one name for four things, the way the gateway's own
    # column is.
    account_id: str
    payload: dict
    # Label and value, for `--print-token`. The label says what the secret is
    # so the person sealing it into a cluster secret does not have to guess.
    secret_label: str = ""
    secret_value: str = ""
    username: str = ""
    # Anything worth saying after a successful registration that is specific to
    # this platform. The generic next steps are printed by `finish`.
    notes: list[str] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        """Instagram's route is the original and unsuffixed; the rest are named.

        Not a field, because a registration route that does not follow the
        pattern is a thing to notice rather than a thing to configure.
        """
        if self.platform == "instagram":
            return "/api/accounts"
        return f"/api/accounts/{self.platform}"

    @property
    def env_key(self) -> str:
        return ENV_KEYS[self.platform]


class ConsentError(SystemExit):
    """A trip that stopped. `SystemExit` so a flow can raise and be done."""


# --- The argument surface, shared so it cannot drift ------------------------


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """The five options every trip takes.

    `--account` is required and that is the point of this file. It is what
    names the directory the account key is written into and what finds the
    `BRAND=` line, so a trip without one is a trip whose result has to be
    copied somewhere by hand afterwards, which is the step this exists to
    delete.
    """
    parser.add_argument(
        "--account",
        required=True,
        help="the pipeline's --account <name>: which accounts/<name>/ this destination belongs to",
    )
    parser.add_argument(
        "--brand",
        default="",
        help=(
            "which identity this destination is grouped under in the panel; "
            "defaults to BRAND= in the account's .env, and absent that the "
            "gateway derives it from the handle"
        ),
    )
    parser.add_argument(
        "--gateway",
        default=os.environ.get("GATEWAY_BASE_URL", ""),
        help="gateway base URL; also read from GATEWAY_BASE_URL, then GATEWAY_URL in .env",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print the credential instead of registering it, for sealing into a cluster secret",
    )
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="register, but leave the account's .env alone",
    )


def account_home(name: str) -> Path:
    """The account directory, or a refusal naming what exists.

    Refuses rather than creating one. `--new-account` writes a directory with a
    template `.env` of commented out lines, and a consent trip that made its
    own would produce a profile holding one id and nothing else, which looks
    configured and fails at the first render.
    """
    home = ACCOUNTS_DIR / name
    if not home.is_dir():
        known = sorted(p.name for p in ACCOUNTS_DIR.glob("*") if p.is_dir())
        raise ConsentError(
            f"No accounts/{name}/. Known: {', '.join(known) or '(none)'}.\n"
            f"Make it first with `python main.py --new-account {name}`."
        )
    return home


def brand_for(name: str, explicit: str) -> str:
    """`--brand`, then `BRAND=` in the account's `.env`, then nothing.

    Nothing is a real answer rather than a gap: the gateway derives the brand
    from the handle when none is sent, and `upsert_account` only overwrites a
    stored brand when the argument is non-empty. So a re-authorisation that
    says nothing leaves the grouping exactly as it was, which is what makes
    re-running a trip safe on an account whose brand was corrected by hand.
    """
    if explicit.strip():
        return explicit.strip()
    home = account_home(name)  # refuse early, before a browser is opened
    # The account's own `.env` alone, not layered over the root one the way
    # `get_settings()` layers everything else. A brand is a fact about one
    # identity, so a `BRAND=` in the root file would silently group every
    # account under the first one somebody set up.
    return Settings(_env_file=home / ".env").brand.strip()


# --- The browser trip that lands on the gateway ----------------------------


def paste_code(url: str, state: str, *, platform: str, watch_for: list[str]) -> str:
    """Open the browser, take the whole address back, return the code.

    TikTok and Facebook both land on a page `gateway/pages.py` serves rather
    than on a loopback port, because TikTok will not register a redirect URI
    that is not https and a loopback address cannot have a certificate anybody
    trusts. Facebook would permit localhost while an app is in development and
    deliberately does not rely on it, since the app that publishes these Reels
    is live and the page next door already exists.

    The prompt asks for the address rather than for the code because `state` is
    compared on the way back, and a bare code carries no state to check.
    """
    lines = "\n".join(f"  - {line}" for line in watch_for)
    print(f"\nA browser is opening.\n{lines}\n  - If it does not open, paste this:\n    {url}\n")
    webbrowser.open(url)

    pasted = input("\nPaste the address you landed on: ").strip()
    if not pasted:
        raise ConsentError("Nothing pasted. Nothing was exchanged.")

    returned = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if not returned:
        raise ConsentError(
            "No query string in that. Paste the whole address, including "
            "everything after the question mark."
        )
    if returned.get("error"):
        raise ConsentError(
            f"{platform} refused the authorisation: {returned['error'][0]} "
            f"{returned.get('error_description', [''])[0]}"
        )
    if returned.get("state", [""])[0] != state:
        raise ConsentError("The state did not match. Nothing was exchanged.")
    if not returned.get("code"):
        raise ConsentError("That address carries no code. Nothing was exchanged.")
    return returned["code"][0]


def new_state() -> str:
    return secrets.token_urlsafe(24)


# --- Writing the account key down ------------------------------------------


def set_env_key(text: str, key: str, value: str) -> tuple[str, str]:
    """Set one `KEY=value` in an `.env`, and say what that did.

    A pure function on the file's text, because the interesting cases are all
    about what was already there and none of them are worth a temporary
    directory to test.

    Four cases, and the second is the one that matters. `--new-account` writes
    every line commented out on purpose, so the normal state of the line this
    is about to set is `# YOUTUBE_CHANNEL_ID=`. Appending instead of
    uncommenting would leave the commented line above the live one, which reads
    on the next visit as a setting somebody disabled.

    Everything else in the file is left byte for byte alone, including the
    comment block explaining the key. Rewriting the file from a template would
    be how a hand written note in an account's `.env` disappears.
    """
    lines = text.splitlines(keepends=True)
    live = f"{key}={value}"

    for index, line in enumerate(lines):
        stripped = line.strip()
        commented = stripped.startswith("#") and stripped.lstrip("#").strip().startswith(f"{key}=")
        if not (stripped.startswith(f"{key}=") or commented):
            continue
        was = stripped.lstrip("#").strip()
        if was == live:
            return text, "unchanged"
        ending = "\n" if line.endswith("\n") else ""
        lines[index] = live + ending
        if commented:
            return "".join(lines), "set"
        old = was.partition("=")[2]
        return "".join(lines), f"replaced {old!r}" if old else "set"

    # No line at all, which is an account whose `.env` predates this key.
    prefix = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{prefix}{live}\n", "added"


def write_env_key(name: str, key: str, value: str) -> str:
    """Put the account key in `accounts/<name>/.env`, and report what happened.

    This is the step that used to be a printed line and a paste. It is worth
    automating rather than documenting because the failure it removes is
    silent: the fan-out skips a destination whose id is missing and queues the
    rest, so a forgotten paste looks exactly like a night that published to
    three platforms on purpose.
    """
    env = account_home(name) / ".env"
    before = env.read_text() if env.exists() else ""
    after, what = set_env_key(before, key, value)
    if after != before:
        env.write_text(after)
    return what


# --- Registration ----------------------------------------------------------


def gateway_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """The same `GATEWAY_URL` and `GATEWAY_TOKEN` the pipeline already reads.

    One gateway and one bearer token, so a second pair of names here would only
    be a second pair to keep in step. Requiring one was not a stricter setup,
    it was a failure after the browser consent had already been spent, and the
    recovery for that is another trip through the consent screen.
    """
    cfg = Settings()
    base_url = args.gateway or cfg.gateway_url
    api_token = os.environ.get("GATEWAY_API_TOKEN", "") or cfg.gateway_token
    if not base_url or not api_token:
        raise ConsentError(
            "Set GATEWAY_URL and GATEWAY_TOKEN in .env to register this, or\n"
            "pass --print-token and store the credential by hand."
        )
    return base_url, api_token


def register(base_url: str, api_token: str, trip: Trip) -> str:
    response = httpx.post(
        f"{base_url.rstrip('/')}{trip.endpoint}",
        json=trip.payload,
        headers={"authorization": f"Bearer {api_token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise ConsentError(f"The gateway refused it ({response.status_code}): {response.text}")
    return str(response.json().get("detail", ""))


def finish(args: argparse.Namespace, trip: Trip) -> None:
    """The tail every trip shares: print, or register and write the key down.

    `--print-token` returns before anything is stored, and it is deliberately
    the only path that puts a credential on a terminal. The default keeps it
    between this process and the gateway, which is why none of these scripts
    ever leave a secret in a file or a shell history on the way.
    """
    named = f" ({trip.username})" if trip.username else ""
    print(f"\nAuthorised {trip.platform} {trip.account_id}{named}")
    for note in trip.notes:
        print(note)

    if args.print_token:
        # Deliberately the only path that puts a credential on a terminal, and
        # it has to be asked for. Printed as `.env` lines rather than as a bare
        # value, because the two reasons to want this are sealing a cluster
        # secret and driving an upload from this machine, and both want the
        # account key next to the credential. That is what the separate `--env`
        # flag on each of these scripts used to be.
        print(f"\n{trip.secret_label}={trip.secret_value}")
        print(f"{trip.env_key}={trip.account_id}")
        print("\nNothing was registered and nothing was written.")
        return

    base_url, api_token = gateway_credentials(args)
    print(f"\nRegistered with the gateway: {register(base_url, api_token, trip)}")

    if args.no_env:
        print(
            f"\n--no-env, so nothing was written. The render host needs\n"
            f"  {trip.env_key}={trip.account_id}\n"
            f"in accounts/{args.account}/.env before a render will fan out here."
        )
    else:
        what = write_env_key(args.account, trip.env_key, trip.account_id)
        print(f"accounts/{args.account}/.env: {trip.env_key} {what}")

    _say_what_is_left(args, trip)
    print(f"\nCheck it: python main.py --account {args.account} --destinations")


def _say_what_is_left(args: argparse.Namespace, trip: Trip) -> None:
    """The schedule, which is the one step still in another repo.

    Said in terms of the brand where there is one, because a `brand=` slot line
    covers every platform that identity holds. That is the difference between
    one homelab edit per identity and one per destination, and it is the whole
    reason `BRAND=` is worth setting before the first trip rather than after
    the fourth.
    """
    brand = trip.payload.get("brand", "")
    if brand:
        print(
            f"\nGrouped under {brand!r}.\n"
            f"Left to do: a GATEWAY_SLOTS line in homelab reading `08:10 Europe/Oslo\n"
            f"brand={brand}`, which covers every platform this identity has. If\n"
            f"{brand!r} already has one, there is nothing to add."
        )
        return
    print(
        f"\nGrouped by its handle, since accounts/{args.account}/.env sets no BRAND=.\n"
        f"Left to do: a GATEWAY_SLOTS line in homelab naming `account={trip.account_id}`,\n"
        f"which covers this destination alone. Setting BRAND= and re-running this\n"
        f"is what makes one line per identity cover all of them."
    )
