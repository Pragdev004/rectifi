"""Draw the app icons (a Fraunces "R" with a double rule under it, like a ruled total) into web/public/.

Development only: it needs Playwright and Chromium, which the app itself never does. The PNGs it writes are committed,
so you only run this if you want to change the icon:   python web/scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parent.parent
FONT = WEB / "node_modules" / "@fontsource-variable" / "fraunces" / "files" / "fraunces-latin-opsz-normal.woff2"

PAGE = """<!doctype html><meta charset=utf-8>
<style>
@font-face {{ font-family: Fr; src: url('{font}') format('woff2-variations'); font-weight: 100 900; }}
html, body {{ margin: 0; background: #F6F2E9; }}
.icon {{ width: 512px; height: 512px; position: relative; background: #F6F2E9; }}
.r {{ position: absolute; left: 0; right: 0; top: 44px; text-align: center; font: 600 380px/1 Fr; font-variation-settings: 'opsz' 144;
      color: #1F4E5F; letter-spacing: -0.02em; }}
.rule {{ position: absolute; left: 112px; right: 112px; height: 10px; background: #1F4E5F; }}
</style>
<div class=icon><div class=r>R</div><div class=rule style="top:408px"></div><div class=rule style="top:430px"></div></div>
"""


def main() -> None:
    if not FONT.is_file():
        raise SystemExit("Run `npm ci` in web/ first (the Fraunces font comes from node_modules).")
    html = PAGE.format(font=FONT.as_uri())
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--proxy-server=direct://", "--allow-file-access-from-files"])
        for size in (192, 512):
            page = browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=size / 512)
            page.set_content(html)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(300)
            page.screenshot(path=str(WEB / "public" / f"icon-{size}.png"), clip={"x": 0, "y": 0, "width": 512, "height": 512})
            page.close()
        browser.close()
    print("Wrote web/public/icon-192.png and icon-512.png")


if __name__ == "__main__":
    main()
