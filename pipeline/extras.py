"""The data the `stars` and `demo` devices draw, fetched by the pipeline.

Neither comes from the model, which is the point of both. A star curve the
scriptwriter wrote would be an invented number on a chart, and a demo it
described would be a picture of something that does not exist. So the script
only asks for the device, and this module either finds the real thing or
returns nothing, in which case `spec.build_spec` degrades the scene to today's
star count or to the README hero.

Best effort, like the README hero and the README blocks: every failure here is
logged and returns None, because a missing curve must never cost a night.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# The largest download worth converting. A README GIF past this is usually a
# screen recording of minutes, and the frame only holds a few seconds of it.
MAX_DEMO_BYTES = 40 * 1024 * 1024
# How much of the demo is kept. A scene is four to six seconds; this leaves the
# loop room without staging a file the render has to seek through.
DEMO_SECONDS = 8
# Narrower than this is an icon or a badge, not a demo.
MIN_DEMO_WIDTH = 400

# A markdown image, an HTML img or video, or a bare user-attachments link,
# which is how GitHub embeds an uploaded video in a README.
_MEDIA = re.compile(
    r"""(?:!\[[^\]]*\]\(\s*<?(?P<md>[^)\s>]+)"""
    r"""|<(?P<tag>img|video|source)\b[^>]*?\bsrc\s*=\s*["'](?P<html>[^"']+)["']"""
    r"""|(?P<bare>https://github\.com/user-attachments/assets/[0-9a-f-]{36}))""",
    re.IGNORECASE,
)
_MOTION = re.compile(r"\.(gif|mp4|webm|mov)(\?|#|$)", re.IGNORECASE)


def star_series(history_path: Path, full_name: str, stars: int, run_dir: Path) -> list[dict]:
    """The repo's star count from this account's own nightly snapshots.

    GitHub stopped listing stargazers in 2026: the REST list answers 404 with
    any token and GraphQL reports a total of zero, so the star-history.com
    sampling trick is gone. The snapshot job has recorded every candidate's
    count each night since August, which is a shorter window and a real one.
    Cached as `stars.json` so a resume draws the curve it drew the first time.
    """
    path = run_dir / "stars.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    from sources.github import StarHistory

    series = [{"t": t, "v": v} for t, v in StarHistory(history_path).series(full_name)]
    today = date.today().isoformat()
    if stars and series and series[-1]["t"] < today:
        series.append({"t": today, "v": stars})
    path.write_text(json.dumps(series))
    return series


def demo_url(readme: str, full_name: str) -> str | None:
    """The first moving image in the README, as an absolute URL, or None."""
    for m in _MEDIA.finditer(readme or ""):
        url = m.group("md") or m.group("html") or m.group("bare")
        if not url:
            continue
        # A video tag is a demo whatever its URL ends in: an uploaded video is a
        # user-attachments link with no extension at all.
        tag = (m.group("tag") or "").lower()
        if not (m.group("bare") or tag in ("video", "source") or _MOTION.search(url)):
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith("http"):
            url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{url.lstrip('./')}"
        # A blob link is the HTML page about the file; raw is the file.
        url = re.sub(r"https://github\.com/([^/]+/[^/]+)/blob/", r"https://github.com/\1/raw/", url)
        return url
    return None


def _ffmpeg(video_dir: Path) -> tuple[str, str, dict[str, str] | None] | None:
    """ffmpeg, ffprobe and the environment to run them in.

    The system's pair if there is one, else the pair Remotion ships with. Those
    load their codecs from the directory they sit in and abort without it on
    the loader path, which is a SIGABRT rather than an error message.
    """
    sys_ff, sys_fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if sys_ff and sys_fp:
        return sys_ff, sys_fp, None
    for d in sorted((video_dir / "node_modules" / "@remotion").glob("compositor-*")):
        ff, fp = d / "ffmpeg", d / "ffprobe"
        if ff.exists() and fp.exists():
            env = {**os.environ, "DYLD_LIBRARY_PATH": str(d), "LD_LIBRARY_PATH": str(d)}
            return str(ff), str(fp), env
    return None


def signed_attachment(url: str, full_name: str, token: str) -> str:
    """The signed address GitHub serves an uploaded README video from.

    A `user-attachments/assets/<id>` link answers 404 to anything but a browser
    session; the README rendered through the API carries the same asset as a
    `private-user-images` URL with a short-lived token, which downloads. The
    original is returned when the render does not hold it.
    """
    m = re.search(r"user-attachments/assets/([0-9a-f-]{36})", url)
    if not m:
        return url
    headers = {"Accept": "application/vnd.github.html+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        html = httpx.get(
            f"https://api.github.com/repos/{full_name}/readme", headers=headers, timeout=30
        ).text
    except httpx.HTTPError:
        return url
    signed = re.search(
        rf'https://private-user-images\.githubusercontent\.com/[^"\s]*{m.group(1)}[^"\s]*', html
    )
    return signed.group(0).replace("&amp;", "&") if signed else url


def fetch_demo(
    readme: str, full_name: str, run_dir: Path, video_dir: Path, token: str = ""
) -> dict | None:
    """Download the README's demo and convert it to a short silent mp4.

    Cached as `demo.mp4` with `demo.json` beside it. Returns the json, or None
    when there is no demo, it is too big, too small, or will not convert.
    """
    meta_path, out = run_dir / "demo.json", run_dir / "demo.mp4"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            return meta if meta and out.exists() else None
        except ValueError:
            pass
    meta = _fetch_demo(readme, full_name, run_dir, video_dir, out, token)
    # Recorded either way, so a resume does not download it again.
    meta_path.write_text(json.dumps(meta))
    return meta


def _fetch_demo(
    readme: str, full_name: str, run_dir: Path, video_dir: Path, out: Path, token: str
) -> dict | None:
    url = demo_url(readme, full_name)
    if url:
        url = signed_attachment(url, full_name, token)
    tools = _ffmpeg(video_dir)
    if not url or not tools:
        log.info("No demo for %s (%s)", full_name, "none in README" if not url else "no ffmpeg")
        return None
    ffmpeg, ffprobe, env = tools
    raw = run_dir / "demo-source"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            size = 0
            with raw.open("wb") as f:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DEMO_BYTES:
                        log.info("Demo for %s is over %d MB", full_name, MAX_DEMO_BYTES >> 20)
                        return None
                    f.write(chunk)
        subprocess.run(  # noqa: S603 - argv list, no shell
            [
                ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), "-t", str(DEMO_SECONDS),
                "-an", "-vf", "scale='min(1080,iw)':-2", "-r", "30", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(out),
            ],
            check=True, capture_output=True, timeout=180, env=env,
        )
        probe = json.loads(
            subprocess.run(  # noqa: S603 - argv list, no shell
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height:format=duration", "-of", "json", str(out),
                ],
                check=True, capture_output=True, text=True, timeout=30, env=env,
            ).stdout
        )
    except Exception as exc:  # noqa: BLE001 - a demo is optional
        log.warning("Demo for %s could not be fetched or converted: %s", full_name, exc)
        out.unlink(missing_ok=True)
        return None
    finally:
        raw.unlink(missing_ok=True)
    w, h = probe["streams"][0]["width"], probe["streams"][0]["height"]
    seconds = float(probe["format"]["duration"])
    if w < MIN_DEMO_WIDTH or seconds < 1:
        log.info("Demo for %s is %dx%d, %.1fs; too small to show", full_name, w, h, seconds)
        out.unlink(missing_ok=True)
        return None
    log.info("Demo for %s: %dx%d, %.1fs from %s", full_name, w, h, seconds, url)
    return {"file": out.name, "w": w, "h": h, "seconds": round(seconds, 2)}
