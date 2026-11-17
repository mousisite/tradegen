"""Take the App Store screenshots of the iPhone app's pages.

The app shows the live site, so its screenshots are the site's pages drawn
the way the app asks for them: a phone-sized window at three pixels per
point, with the app's "OrenthApp/1" User-Agent so the site leaves out what it
leaves out inside the app.

Two sets, the sizes App Store Connect asks for (checked 16 November 2026):

  6.9-inch   1320 x 2868  iPhone with Dynamic Island, large display
  6.3-inch   1206 x 2622  iPhone with Dynamic Island, medium display

Apple refuses a screenshot with an alpha channel, so every picture is saved
as plain RGB.

Start the site first, signed out and with sign-in required, so visitors see
the example account, as App Review will. In one window:

    set STOCKBOT_DB=%TEMP%\\orenth-shots.db
    set STOCKBOT_BEHIND_PROXY=1
    python serve.py --host 127.0.0.1 --port 8110

Wait until the popular checks have run (a few minutes after start; the track
record and strategy pages need them), then, from the project root:

    python mobile/scripts/take_screenshots.py [http://127.0.0.1:8110]

Needs Playwright and Microsoft Edge (or set ORENTH_BROWSER_CHANNEL).
"""
from __future__ import annotations

import io
import os
import re
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "mobile", "store", "screenshots")
AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Mobile/15E148 OrenthApp/1")

# Folder name, window size in points, the picture size Apple wants.
SIZES = [("6.9-inch", (440, 956), (1320, 2868)),
         ("6.3-inch", (402, 874), (1206, 2622))]


def screens(base: str, first_test: str):
    """(file name, address, what to bring to the top first or None)."""
    return [
        ("01-front-page", base + "/", None),
        ("02-analysis", base + "/analyse?symbol=NVDA&interval=1d", "section.tradechart"),
        ("03-monitor", base + "/monitor", "h2:has-text('Alerts')"),
        ("04-journal", base + "/trades", None),
        ("05-track-record", base + "/record", None),
        ("06-strategy-test", base + first_test, None),
    ]


# Room for the bar at the top, which stays put while the page scrolls.
UNDER_TOP_BAR = 72


def first_strategy_test(page, base: str) -> str:
    """The first strategy test listed, so its page has results on it."""
    page.goto(base + "/backtests", wait_until="networkidle", timeout=120000)
    for href in page.eval_on_selector_all(
            "a[href^='/backtest/']", "links => links.map(a => a.getAttribute('href'))"):
        if re.match(r"^/backtest/[a-z0-9-]+$", href or ""):
            return href
    raise SystemExit("No strategy test is listed yet. Wait for the popular checks.")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8110").rstrip("/")
    channel = os.environ.get("ORENTH_BROWSER_CHANNEL", "msedge")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel)
        for folder, (width, height), (px_w, px_h) in SIZES:
            os.makedirs(os.path.join(OUT, folder), exist_ok=True)
            context = browser.new_context(
                viewport={"width": width, "height": height}, device_scale_factor=3,
                is_mobile=True, has_touch=True, user_agent=AGENT, color_scheme="light")
            page = context.new_page()
            test = first_strategy_test(page, base)
            for name, url, focus in screens(base, test):
                page.goto(url, wait_until="networkidle", timeout=180000)
                if focus:
                    page.locator(focus).first.evaluate(
                        "(el, gap) => window.scrollTo(0, el.getBoundingClientRect().top"
                        " + window.scrollY - gap)", UNDER_TOP_BAR)
                page.wait_for_timeout(600)
                shot = Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB")
                if shot.size != (px_w, px_h):
                    raise SystemExit("%s came out %dx%d, not %dx%d"
                                     % (name, shot.width, shot.height, px_w, px_h))
                path = os.path.join(OUT, folder, name + ".png")
                shot.save(path, "PNG", optimize=True)
                print("  %s/%s.png  %dx%d" % (folder, name, shot.width, shot.height))
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
