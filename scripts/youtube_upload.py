#!/usr/bin/env python
"""youtube_upload.py - push one rendered video to YouTube from the Mac.

A proving harness for `gateway/youtube.py`, not a second publisher. It drives
the same module the gateway will call, so what this confirms is the real path
rather than a throwaway near it: mint a token, open a resumable session, push
the bytes, read the video resource back.

    uv run python scripts/youtube_upload.py build/2026-08-08/tensorflow-tensorflow

Credentials come from `data/yt_token.json`, written by
`scripts/youtube_authorise.py`. That file is a stepping stone: once the gateway
can take the registration, these live in `youtube_credentials` and in the
cluster secret, and the local copy should be deleted.

**Expect the video to land private and stay private** until the API project
clears its audit. That lock lives on the project rather than the video, so no
flag here and no edit in Studio changes it. It does not stop this proving what
it is meant to prove.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import youtube  # noqa: E402
from pipeline.gateway import strip_written_cta  # noqa: E402

TOKEN_FILE = ROOT / "data" / "yt_token.json"


def description_for(folder: Path, link: str) -> str:
    """The Instagram caption, rewritten for a surface with no DMs.

    The keyword mechanic has no equivalent here: no private replies, no way to
    answer a comment with a link. A description saying "comment TENSORFLOW if
    you want the link" is a promise nothing can keep, so the written ask comes
    out and the link goes in directly.

    `strip_written_cta` is the same function that defends the voiceover and the
    Instagram caption, reused rather than reimplemented, because a third
    almost-identical stripper is how the wording drifts apart.
    """
    caption = (folder / "caption.txt").read_text()
    body = strip_written_cta(caption)

    lines = [line for line in body.splitlines() if line.strip()]
    tags = lines.pop() if lines and lines[-1].lstrip().startswith("#") else ""
    prose = "\n".join(lines).strip()

    parts = [prose, f"Repo: {link}"]
    if tags:
        parts.append(tags)
    return "\n\n".join(p for p in parts if p)


def link_for(folder: Path) -> str:
    repo = json.loads((folder / "repo.json").read_text())
    full_name = repo.get("full_name") or folder.name.replace("-", "/", 1)
    return f"https://github.com/{full_name}"


async def run(args: argparse.Namespace) -> int:
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"No {TOKEN_FILE}. Run scripts/youtube_authorise.py --save first."
        )
    credentials = json.loads(TOKEN_FILE.read_text())

    folder = args.folder
    video = folder / "out.mp4"
    if not video.exists():
        raise SystemExit(f"No rendered video at {video}")

    title = json.loads((folder / "script.json").read_text())["hook"]
    link = link_for(folder)
    description = description_for(folder, link)

    print(f"Channel:     {credentials['channel_id']} ({credentials.get('username', '')})")
    print(f"Video:       {video} ({video.stat().st_size / 1_048_576:.1f} MB)")
    print(f"Title:       {title}")
    print(f"Privacy:     {args.privacy}")
    print(f"Synthetic:   {args.synthetic}")
    print(f"\nDescription:\n{description}\n")
    if not args.yes and input("Upload this? [y/N] ").strip().lower() != "y":
        return 1

    async with httpx.AsyncClient() as http:
        try:
            result = await youtube.upload(
                http,
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                refresh_token=credentials["refresh_token"],
                video_path=video,
                title=title,
                description=description,
                privacy_status=args.privacy,
                contains_synthetic_media=args.synthetic,
            )
        except youtube.UploadError as exc:
            # The same signal the scheduler will act on. Printed rather than
            # just raised, because "was anything created" is the only question
            # that matters when this fails.
            print(f"\nFAILED: {exc}")
            print(
                "A video may exist, check the channel."
                if exc.session_created
                else "Nothing was created, this is safe to retry."
            )
            return 1

    print(f"\nUploaded: {result.url}")
    print(f"Privacy:  {result.privacy_status}")
    if result.privacy_status == "private" and args.privacy != "private":
        print(
            "\nAsked for "
            f"{args.privacy} and got private. That is the unaudited project "
            "lock, not a bug here: it lives on the API project, so Studio will "
            "not let you change it either. It clears when the audit does."
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="a build/<date>/<repo> folder")
    parser.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
        help="private by default, which is also all an unaudited project can produce",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "declare altered or synthetic content. A decision rather than a "
            "default; see the YouTube section of PROFILE.md"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
