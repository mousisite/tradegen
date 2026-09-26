"""Draw static/logo.png, the square mark used by search engines.

Organisation schema asks for a logo, and a link-preview card is the wrong
shape for it: search engines want something square-ish they can put beside a
name. This draws the same rising-line glyph the favicon uses, so the mark in a
search result and the mark in a browser tab are the same drawing.

Run it from the project root:

    python tools/make_logo.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZE = 512
PAPER = (244, 243, 238)
ACCENT = (201, 100, 66)

# The favicon path, in its original 24x24 coordinate space:
#   M3 16.5 l5.5-6 4 3.5 L21 5      the rising line
#   M15 5 h6 v6                     the corner tick at its head
LINE = [(3, 16.5), (8.5, 10.5), (12.5, 14), (21, 5)]
TICK = [(15, 5), (21, 5), (21, 11)]


def _scale(points, pad: float = 0.0):
    """24x24 design units to pixels, with room for the stroke to breathe."""
    span = SIZE - 2 * pad
    return [(pad + x / 24.0 * span, pad + y / 24.0 * span) for x, y in points]


def draw(pad_frac: float = 0.16, weight: float = 0.085) -> Image.Image:
    # Supersampled, then reduced: Pillow will not antialias a thick line, and
    # the diagonals look like a staircase at full size without this.
    scale = 4
    big = Image.new("RGB", (SIZE * scale, SIZE * scale), PAPER)
    pen = ImageDraw.Draw(big)

    width = int(SIZE * scale * weight)
    pad = SIZE * scale * pad_frac

    def place(points):
        span = SIZE * scale - 2 * pad
        return [(pad + x / 24.0 * span, pad + y / 24.0 * span)
                for x, y in points]

    pen.line(place(LINE), fill=ACCENT, width=width, joint="curve")
    pen.line(place(TICK), fill=ACCENT, width=width, joint="curve")

    # Round the ends by hand; Pillow has no line cap setting.
    for x, y in place([LINE[0], LINE[-1], TICK[0], TICK[-1]]):
        r = width / 2.0
        pen.ellipse([x - r, y - r, x + r, y + r], fill=ACCENT)

    return big.resize((SIZE, SIZE), Image.LANCZOS)


# Google will not use a data: URI as a search-result favicon; it wants a real
# file it can fetch. These are the sizes it and the mobile platforms ask for.
# Google's guidance is a multiple of 48px, so the small end starts there.
PNG_SIZES = (48, 96, 180, 192, 512)

# The .ico still matters: browsers request /favicon.ico whether or not a page
# links to one, and a 404 there is a wasted request on every visit.
ICO_SIZES = (16, 32, 48)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static = os.path.join(root, "static")
    master = draw()
    # A favicon is read at 16-48px, where generous margins just throw away
    # pixels. The icons use a tighter frame and a heavier stroke than the
    # full-size logo so the shape survives being shrunk.
    tight = draw(pad_frac=0.09, weight=0.105)

    wrote = []
    master.save(os.path.join(static, "logo.png"), "PNG", optimize=True)
    wrote.append(("logo.png", SIZE))

    for size in PNG_SIZES:
        name = "icon-%d.png" % size
        tight.resize((size, size), Image.LANCZOS).save(
            os.path.join(static, name), "PNG", optimize=True)
        wrote.append((name, size))

    # Pillow builds a multi-resolution .ico from one image given the sizes.
    tight.save(os.path.join(static, "favicon.ico"), "ICO",
               sizes=[(s, s) for s in ICO_SIZES])
    wrote.append(("favicon.ico", max(ICO_SIZES)))

    for name, size in wrote:
        path = os.path.join(static, name)
        print("  %-16s %4dpx  %6d bytes" % (name, size, os.path.getsize(path)))
