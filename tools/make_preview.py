"""Draw the link-preview card at static/preview.png.

This is the picture that appears when the address is pasted into Discord,
iMessage, Reddit or anywhere else that unfurls a link. It carries the product
name, so it has to be redrawn whenever the name changes -- which is why this
exists as a script rather than as an image somebody made once by hand and
cannot reproduce.

Run it from the project root:

    python tools/make_preview.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

NAME = "Orenth"
DOMAIN = "orenth.app"
TAGLINE = "It back-tests its own advice and tells you when it has no edge."
POINTS = [
    "Every figure cites the filing it came from",
    "Position sizes price what a gap past your stop costs",
    "Closed theses separate being right from being lucky",
]

# The app's own palette, so the card and the site look like one thing.
PAPER = (244, 243, 238)
INK = (25, 24, 23)
SOFT = (110, 106, 100)
ACCENT = (201, 100, 66)

WIDTH, HEIGHT = 1200, 630
MARGIN = 84


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """Georgia, with a fallback so this still runs off Windows."""
    for path in (r"C:\Windows\Fonts\georgia%s.ttf" % ("b" if bold else ""),
                 "/usr/share/fonts/truetype/dejavu/DejaVuSerif%s.ttf"
                 % ("-Bold" if bold else "")):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw() -> Image.Image:
    card = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    pen = ImageDraw.Draw(card)

    # A rule down the left edge, the same accent as the site's links.
    pen.rectangle([0, 0, 10, HEIGHT], fill=ACCENT)

    pen.text((MARGIN, 96), NAME, font=_font(True, 82), fill=INK)
    pen.text((MARGIN, 212), TAGLINE, font=_font(False, 34), fill=SOFT)

    y = 392
    for point in POINTS:
        pen.ellipse([MARGIN, y + 11, MARGIN + 9, y + 20], fill=ACCENT)
        pen.text((MARGIN + 28, y), point, font=_font(False, 27), fill=INK)
        y += 44

    pen.text((MARGIN, 556), DOMAIN, font=_font(True, 30), fill=ACCENT)
    return card


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "static", "preview.png")
    draw().save(out, "PNG", optimize=True)
    size = os.path.getsize(out)
    print("wrote %s  (%dx%d, %d bytes)" % (out, WIDTH, HEIGHT, size))
    if size > 5_000_000:
        sys.exit("preview is too large for some scrapers")
