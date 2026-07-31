"""Terminal-prompt avatar variants that avoid the currency-symbol read."""

import asyncio
import base64
import os
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).parent
OUT = HERE / "avatars"
OUT.mkdir(exist_ok=True)

# The contact sheet mocks up a feed row, because an avatar can only really be
# judged at the size it will be seen next to a handle and a tagline. Both are
# account identity, which lives outside this repo, so they come from the
# environment and fall back to placeholders.
HANDLE = os.environ.get("BRAND_HANDLE", "yourhandle")
TAGLINE = os.environ.get("BRAND_TAGLINE", "What the account posts, in one line")

BG = "#0D1117"
DEEP = "#010409"
ACCENT = "#58A6FF"
TEXT = "#E6EDF3"

FONTS = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=JetBrains+Mono:wght@700;800&display=swap');"
)


def prompt(glyph: str, bg: str, glyph_color: str, cursor_color: str,
           gap: int = 20, size: int = 200) -> str:
    return f"""<div class="wrap" style="background:{bg}">
        <div style="display:flex;align-items:center;gap:{gap}px">
          <span class="mono" style="color:{glyph_color};font-size:{size}px;
            font-weight:800;line-height:1">{glyph}</span>
          <span style="width:70px;height:{int(size * 0.82)}px;
            background:{cursor_color};border-radius:5px"></span>
        </div>
      </div>"""


CANDIDATES = [
    # chevron: terminal-native, zero currency ambiguity
    ("g-chevron", prompt("&gt;", BG, ACCENT, TEXT)),
    # heavier chevron used by starship/fish prompts
    ("h-caret", prompt("&#10095;", BG, ACCENT, TEXT, gap=24, size=190)),
    # the current pick, but cursor in accent so the $ is not the loudest mark
    ("i-dollar-refined", prompt("$", BG, "#8B949E", ACCENT)),
    # chevron on deep black, accent cursor
    ("j-chevron-accent", prompt("&gt;", DEEP, TEXT, ACCENT)),
]

PAGE = """
<html><head><style>
{fonts}
* {{ margin:0; padding:0; box-sizing:border-box }}
.wrap {{ width:512px; height:512px; display:flex; align-items:center;
         justify-content:center; overflow:hidden }}
.mono {{ font-family:'JetBrains Mono', monospace }}
</style></head><body>{inner}</body></html>
"""


def b64(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 512, "height": 512},
                                device_scale_factor=2)
        for name, inner in CANDIDATES:
            await page.set_content(PAGE.format(fonts=FONTS, inner=inner))
            await page.wait_for_timeout(1100)
            await page.locator(".wrap").screenshot(path=str(OUT / f"{name}.png"))
            print("wrote", name)

        # comparison sheet, including the current pick and the moon
        names = ["d-prompt", "g-chevron", "h-caret", "i-dollar-refined",
                 "j-chevron-accent", "b-moon"]
        rows = ""
        for n in names:
            src = b64(OUT / f"{n}.png")
            tag = " (current)" if n == "d-prompt" else ""
            rows += f"""<div class="row">
                <div class="label">{n}{tag}</div>
                <img class="big" src="{src}">
                <div class="feed"><img class="sm" src="{src}">
                  <div><div class="fname">{HANDLE}</div>
                  <div class="fsub">{TAGLINE}</div></div>
                </div></div>"""
        sheet = f"""<html><head><style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap');
        * {{ margin:0;padding:0;box-sizing:border-box }}
        body {{ background:#0D1117;font-family:Inter,sans-serif;padding:32px 40px;
                width:900px }}
        h1 {{ color:#E6EDF3;font-size:19px;margin-bottom:6px }}
        .sub {{ color:#8B949E;font-size:13px;margin-bottom:24px }}
        .row {{ display:flex;align-items:center;gap:30px;padding:15px 0;
                border-bottom:1px solid #21262D }}
        .label {{ color:#8B949E;font-size:12px;font-family:monospace;width:150px }}
        .big {{ width:108px;height:108px;border-radius:50%;border:1px solid #30363D }}
        .feed {{ display:flex;align-items:center;gap:11px;background:#161B22;
                 padding:11px 16px;border-radius:10px;flex:1 }}
        .sm {{ width:32px;height:32px;border-radius:50%;flex-shrink:0 }}
        .fname {{ color:#E6EDF3;font-size:13px;font-weight:600 }}
        .fsub {{ color:#8B949E;font-size:11.5px }}
        </style></head><body><h1>Terminal-prompt variants</h1>
        <div class="sub">The 32px column on the right is the one that matters.</div>
        {rows}</body></html>"""
        await page.set_content(sheet)
        await page.wait_for_timeout(1100)
        await page.screenshot(path=str(HERE / "prompt-sheet.png"), full_page=True)
        print("wrote sheet")
        await b.close()


asyncio.run(main())
