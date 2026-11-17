"""Draw the iPhone app's icon and launch screens from the Orenth mark.

The mark is drawn by tools/make_logo.py, the one drawing every Orenth icon
comes from, so the app's icon cannot drift from the site's. This writes the
three source pictures @capacitor/assets turns into the Xcode asset catalog:

  mobile/assets/icon-only.png    1024 x 1024, the mark on its dark tile, as on
                                 the site's own icons. Opaque and full-bleed
                                 with no alpha channel at all: App Store
                                 Connect refuses an icon with one (ITMS-90717),
                                 and iOS rounds the corners itself.
  mobile/assets/splash.png       2732 x 2732, the mark small and centred on the
                                 site's paper colour.
  mobile/assets/splash-dark.png  the same on the site's dark ground.

Then, in mobile/:  npx @capacitor/assets generate --ios

    python mobile/scripts/make_assets.py      (from the project root)
"""
from __future__ import annotations

import importlib.util
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "mobile", "assets")


def _logo():
    spec = importlib.util.spec_from_file_location(
        "make_logo", os.path.join(ROOT, "tools", "make_logo.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hex(colour: str):
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


# The grounds and the accent from static/style.css: --paper and --ink in the
# light theme, and the dark theme's lifted accent.
PAPER = _hex("#f4f3ee")
DARK_GROUND = _hex("#191817")
DARK_ACCENT = _hex("#e89268")

ICON = 1024
SPLASH = 2732
# The mark's width on the launch screen. The picture is shown filling the
# screen's height, so on a large iPhone this comes out near 125 points.
SPLASH_MARK = 400


def icon(logo) -> Image.Image:
    # The site's own tile: paper-coloured mark on the ink ground, at the
    # proportion its 180 to 512 pixel icons use.
    return logo.render(ICON, background=logo.INK, ring=logo.PAPER,
                       candle=logo.PAPER, scale=0.74)


def splash(logo, ground, ink, accent) -> Image.Image:
    canvas = Image.new("RGB", (SPLASH, SPLASH), ground)
    mark = logo.render(SPLASH_MARK, background=ground, ring=ink, candle=ink,
                       accent=accent, scale=0.96)
    at = (SPLASH - SPLASH_MARK) // 2
    canvas.paste(mark, (at, at))
    return canvas


def main() -> None:
    logo = _logo()
    os.makedirs(OUT, exist_ok=True)
    pictures = {
        "icon-only.png": icon(logo),
        "splash.png": splash(logo, PAPER, logo.INK, logo.ACCENT),
        "splash-dark.png": splash(logo, DARK_GROUND, PAPER, DARK_ACCENT),
    }
    for name, image in pictures.items():
        if image.mode != "RGB":
            raise SystemExit("%s came out as %s, not RGB" % (name, image.mode))
        path = os.path.join(OUT, name)
        image.save(path, "PNG", optimize=True)
        print("  %-16s %dx%d %s %7d bytes" % (name, image.width, image.height,
                                              image.mode, os.path.getsize(path)))


if __name__ == "__main__":
    main()
