"""Render profile-picture candidates using the project's theme palette."""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).parent / "avatars"
OUT.mkdir(exist_ok=True)

BG = "#0D1117"
DEEP = "#010409"
ACCENT = "#58A6FF"
TEXT = "#E6EDF3"
MUTED = "#8B949E"
BORDER = "#30363D"
STAR = "#E3B341"

FONTS = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Inter:wght@700;800;900&family=JetBrains+Mono:wght@700;800&display=swap');"
)

# Each entry: (name, inner html, extra css)
CANDIDATES = [
    (
        "a-monogram",
        f"""<div class="wrap" style="background:{BG}">
              <span class="mono" style="color:{ACCENT};font-size:210px;font-weight:800;
                letter-spacing:-8px">nb</span>
            </div>""",
        "",
    ),
    (
        "b-moon",
        f"""<div class="wrap" style="background:{DEEP}">
              <svg viewBox="0 0 100 100" width="330" height="330">
                <path d="M62 8 a42 42 0 1 0 30 62 A46 46 0 0 1 62 8 Z"
                      fill="{ACCENT}"/>
              </svg>
            </div>""",
        "",
    ),
    (
        "c-moon-commit",
        f"""<div class="wrap" style="background:{DEEP}">
              <svg viewBox="0 0 100 100" width="360" height="360">
                <path d="M0 62 H22" stroke="{BORDER}" stroke-width="4"
                      stroke-linecap="round"/>
                <path d="M78 62 H100" stroke="{BORDER}" stroke-width="4"
                      stroke-linecap="round"/>
                <path d="M60 14 a38 38 0 1 0 26 56 A42 42 0 0 1 60 14 Z"
                      fill="{ACCENT}"/>
              </svg>
            </div>""",
        "",
    ),
    (
        "d-prompt",
        f"""<div class="wrap" style="background:{BG}">
              <div style="display:flex;align-items:center;gap:22px">
                <span class="mono" style="color:{ACCENT};font-size:200px;
                  font-weight:800">$</span>
                <span style="width:78px;height:170px;background:{TEXT};
                  border-radius:6px"></span>
              </div>
            </div>""",
        "",
    ),
    (
        "e-moon-ring",
        f"""<div class="wrap" style="background:{DEEP}">
              <div style="width:400px;height:400px;border-radius:50%;
                border:14px solid {BORDER};display:flex;align-items:center;
                justify-content:center">
                <svg viewBox="0 0 100 100" width="230" height="230">
                  <path d="M62 8 a42 42 0 1 0 30 62 A46 46 0 0 1 62 8 Z"
                        fill="{ACCENT}"/>
                </svg>
              </div>
            </div>""",
        "",
    ),
    (
        "f-nb-boxed",
        f"""<div class="wrap" style="background:{DEEP}">
              <div style="width:380px;height:380px;border-radius:64px;
                background:{BG};border:10px solid {ACCENT};display:flex;
                align-items:center;justify-content:center">
                <span class="mono" style="color:{ACCENT};font-size:180px;
                  font-weight:800;letter-spacing:-6px">nb</span>
              </div>
            </div>""",
        "",
    ),
]

PAGE = """
<html><head><style>
{fonts}
* {{ margin:0; padding:0; box-sizing:border-box }}
body {{ background:#000 }}
.wrap {{ width:512px; height:512px; display:flex; align-items:center;
         justify-content:center; overflow:hidden }}
.mono {{ font-family:'JetBrains Mono', monospace }}
.sans {{ font-family:'Inter', sans-serif }}
{extra}
</style></head><body>{inner}</body></html>
"""


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 512, "height": 512},
                                      device_scale_factor=2)
        for name, inner, extra in CANDIDATES:
            await page.set_content(PAGE.format(fonts=FONTS, inner=inner, extra=extra))
            await page.wait_for_timeout(1200)  # webfont load
            await page.locator(".wrap").screenshot(path=str(OUT / f"{name}.png"))
            print("wrote", name)
        await browser.close()


asyncio.run(main())
