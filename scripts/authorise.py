#!/usr/bin/env python
"""authorise.py - one consent trip, for any destination, for one account.

    uv run python scripts/authorise.py youtube   --account <name>
    uv run python scripts/authorise.py tiktok    --account <name>
    uv run python scripts/authorise.py facebook  --account <name>
    uv run python scripts/authorise.py instagram --account <name>

Four platforms behind one command, because the thing that does not scale about
adding an account is not any one OAuth flow. It is that each of them had its
own script, its own argument names, its own idea of what to print at the end,
and between them no idea at all which `accounts/<name>/` the result belonged
to. What came out was a channel id on a terminal and three places to paste it.

What this adds over calling the four scripts directly:

- **`--account` is required**, so every trip knows whose destination this is.
  That is what lets it write the account key into `accounts/<name>/.env`
  instead of printing a line to copy. A forgotten paste is silent: the fan-out
  skips a destination whose id is missing and queues the rest, so it looks
  exactly like a night that published to three platforms on purpose.
- **The brand comes from the account**, from a `BRAND=` line in its `.env`, so
  a destination lands in the right group without anybody remembering a flag.
  Two of the four scripts had no `--brand` at all before this, which meant the
  grouping could only ever be derived from the handle.
- **One argument surface.** `--gateway`, `--print-token` and `--no-env` mean
  the same thing on all four.

The four flows themselves stay in their own files and are deliberately not
merged. Google's is the authorisation code flow with PKCE behind a library,
TikTok's and Facebook's are a browser trip landing on a page the gateway
serves, Instagram's is a paste because it has no callback route yet. Those
differ down to the error messages.

Each platform's own script still runs on its own, unchanged in what it does, so
nothing that already points at `scripts/youtube_authorise.py` breaks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import (  # noqa: E402  - after the sys.path insert above
    consent,
    facebook_authorise,
    instagram_authorise,
    tiktok_authorise,
    youtube_authorise,
)

# Ordered by what a new identity does first rather than alphabetically. A
# channel is one OAuth trip and an Instagram token is a Meta app, so a second
# account reaches YouTube first every time.
FLOWS = {
    "youtube": youtube_authorise,
    "tiktok": tiktok_authorise,
    "facebook": facebook_authorise,
    "instagram": instagram_authorise,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="authorise.py",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="platform", required=True, metavar="platform")
    for name, module in FLOWS.items():
        sub = subparsers.add_parser(
            name,
            help=(module.__doc__ or "").splitlines()[0],
            description=module.__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        module.add_arguments(sub)

    args = parser.parse_args()
    consent.finish(args, FLOWS[args.platform].trip(args))


if __name__ == "__main__":
    main()
