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


def draw() -> Image.Image:
    # Supersampled, then reduced: Pillow will not antialias a thick line, and
    # the diagonals look like a staircase at full size without this.
    scale = 4
    big = Image.new("RGB", (SIZE * scale, SIZE * scale), PAPER)
    pen = ImageDraw.Draw(big)

    width = int(SIZE * scale * 0.085)
    pad = SIZE * scale * 0.16

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


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "static", "logo.png")
    draw().save(out, "PNG", optimize=True)
    print("wrote %s  (%dx%d, %d bytes)"
          % (out, SIZE, SIZE, os.path.getsize(out)))
