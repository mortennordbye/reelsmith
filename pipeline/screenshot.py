"""Capture the repository's GitHub page as a real screenshot.

Opening on the actual page is worth a lot: it grounds the video in something
the viewer recognises and can go look at themselves, rather than a card we
drew. It is also the cheapest possible credibility signal -- the star count on
screen is visibly GitHub's, not ours.

Captured in dark mode at 2x device pixel ratio so the text stays crisp when
scaled into a 1080-wide frame.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class ScreenshotError(RuntimeError):
    pass


# Desktop-ish viewport: GitHub's repo header reads well at this width, and it
# crops to 16:10 which sits nicely inside a browser frame in a 9:16 video.
# Narrower than a real desktop on purpose. GitHub centres the README in a
# fixed-width column, so a narrower viewport means the captured region is
# closer to the 1080px the video renders it at -- roughly 1:1 instead of a
# 0.7x downscale, which is the difference between legible and mush.
VIEWPORT = {"width": 1000, "height": 900}
DEVICE_SCALE = 3  # -> 3x supersampled, downscaled by the renderer = crisp text

# How much of the README to keep. Enough for a hero banner, title, badges, and
# the opening paragraph; not so much that it becomes a wall of prose. Also
# bounded by what fits above the caption safe area once framed in browser
# chrome -- see BrowserFrame.tsx.
HERO_MAX_HEIGHT = 600

# GitHub's README tab bar sticks to the top once scrolled and overlaps the
# article beneath it.
STICKY_HEADER_PX = 64

# Horizontal bleed so the card's rounded border isn't shaved off.
EDGE_BLEED = 10

# GitHub layers a lot of chrome over the interesting part. Hiding it is more
# reliable than trying to click each dismiss button, which moves around.
HIDE_SELECTORS = """
    .js-cookie-consent-banner, [data-testid*="cookie"], dialog[open],
    .js-notice, .flash-messages, .js-header-wrapper > .js-notice,
    .signup-prompt-bg, .js-signup-prompt, [role="dialog"],
    .Popover, .js-feature-preview-indicator-container,
    .js-flash-alert, .position-fixed.bottom-0
"""


def _readme_hero_clip(page) -> dict[str, float] | None:  # noqa: ANN001 - playwright Page
    """Locate the top of the rendered README.

    This is where maintainers put the thing they actually designed -- a logo,
    a title lockup, shields badges. It is almost always the best-looking part
    of the page, and far better opening material than a file listing.
    """
    for selector in ("#readme", "article.markdown-body", ".Box-body article"):
        try:
            node = page.locator(selector).first
            if node.count() == 0:
                continue
            # scroll_into_view_if_needed() centres a tall element, which lands
            # you in the middle of the README -- typically a directory tree.
            # We want the TOP, so align explicitly.
            node.evaluate(
                "el => el.scrollIntoView({block: 'start', inline: 'nearest', behavior: 'instant'})"
            )
            # GitHub's README tab bar turns sticky once scrolled, and it covers
            # the first ~60px of the article -- which is exactly where the
            # title lockup lives. Back off so the hero clears it.
            page.evaluate(f"window.scrollBy(0, -{STICKY_HEADER_PX})")
            page.wait_for_timeout(400)
            box = node.bounding_box()
        except Exception:  # noqa: BLE001 - try the next selector
            continue

        if not box or box["width"] < 200 or box["height"] < 120:
            continue

        # bounding_box() is viewport-relative, which is exactly what
        # page.screenshot(clip=...) wants for a non-full-page capture.
        # A few px of bleed on each side stops the rounded border and the
        # first character of the tab bar getting shaved off.
        x = max(box["x"] - EDGE_BLEED, 0)
        width = min(box["width"] + EDGE_BLEED * 2, VIEWPORT["width"] - x)
        return {
            "x": x,
            "y": max(box["y"], 0),
            "width": width,
            "height": min(box["height"], HERO_MAX_HEIGHT),
        }
    return None


def capture_repo(url: str, out_path: Path, *, timeout_ms: int = 30_000) -> Path | None:
    """Screenshot a GitHub repo page. Returns None on any failure.

    Deliberately never raises: a missing screenshot should degrade the video to
    the card-only opening, not take down the whole pipeline.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed; skipping screenshot")
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=DEVICE_SCALE,
                # GitHub honours prefers-color-scheme, so this gets us the dark
                # theme for free and it matches the video's palette.
                color_scheme="dark",
                # A real UA avoids the occasional interstitial.
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # The star count and language bar hydrate late; without this the
            # screenshot often catches a skeleton loader.
            page.wait_for_timeout(2500)
            page.add_style_tag(content=HIDE_SELECTORS + " { display: none !important; }")

            clip = _readme_hero_clip(page)
            if clip is None:
                log.info("No README panel found on %s; falling back to page header.", url)
                clip = {"x": 0, "y": 36, "width": VIEWPORT["width"], "height": 620}

            page.screenshot(path=str(out_path), type="png", clip=clip)
            browser.close()

    except Exception as exc:  # noqa: BLE001 - any failure degrades gracefully
        log.warning("Screenshot of %s failed (%s); continuing without it.", url, exc)
        return None

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None

    log.info("Captured %s (%.0f KB)", out_path.name, out_path.stat().st_size / 1024)
    return out_path
