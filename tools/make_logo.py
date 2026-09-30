"""Draw the Orenth mark, and every file that is made from it.

The mark is an O -- the ring -- holding three candlesticks that step upward.
The last one is in the accent colour: of the three setups, the one that
cleared the bar, which is the only thing this app is for.

It is defined once, in a 1000-unit square, and every output below is drawn
from that single definition. The favicon, the app icons, the profile picture
and the SVG in the site header cannot drift apart, because there is only one
drawing and they are all renderings of it.

Two weights exist because one drawing cannot serve every size. At 16 to 48
pixels the thin wicks turn to grey fuzz and the ring thins to nothing, so the
small renderings drop the wicks and thicken the strokes. That is optical
sizing, the same thing a typeface does, not a second design.

Run it from the project root:

    python tools/make_logo.py
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw

INK = (25, 24, 23)
PAPER = (244, 243, 238)
ACCENT = (201, 100, 66)

CENTRE = 500
RING_OUTER = 400
# Thin enough to read as a letter O rather than a warning sign, which is what
# a ring heavier than the shapes inside it turns into.
RING_WIDTH = 60

BODY_WIDTH = 104
BODY_RADIUS = 16
WICK_WIDTH = 24

# centre x, body top, body bottom, wick top, wick bottom, colour role.
#
# The run climbs along the circle's diagonal, which is the only direction
# with room: its two outermost corners, bottom-left of the first candle and
# top-right of the last, sit the same distance from the ring, so the group
# fills the O instead of floating in it. Each body starts higher than the one
# before and overlaps it, the way a real rising run does.
CANDLES = [
    (340, 540, 705, 470, 750, "ink"),
    (500, 405, 620, 355, 670, "ink"),
    (660, 295, 510, 250, 545, "accent"),
]

Rect = Tuple[float, float, float, float, float, str]


def shapes(bold: bool) -> Tuple[float, List[Rect]]:
    """The ring's stroke and the candle rectangles, in design units."""
    ring = RING_WIDTH * (1.3 if bold else 1.0)
    body = BODY_WIDTH * (1.15 if bold else 1.0)
    # The bold weight draws the group slightly smaller about the centre: its
    # heavier ring is thicker on the inside too, and without this the last
    # candle's corner touches it at favicon sizes.
    pull = 0.94 if bold else 1.0

    def near(v: float) -> float:
        return CENTRE + (v - CENTRE) * pull

    rects: List[Rect] = []
    for cx, top, bottom, wick_top, wick_bottom, role in CANDLES:
        cx, top, bottom = near(cx), near(top), near(bottom)
        if not bold:
            rects.append((cx - WICK_WIDTH / 2, near(wick_top),
                          cx + WICK_WIDTH / 2, near(wick_bottom),
                          WICK_WIDTH / 2, role))
        rects.append((cx - body / 2, top, cx + body / 2, bottom,
                      BODY_RADIUS, role))
    return ring, rects


def render(size: int, background: Optional[Tuple[int, int, int]] = None,
           ring=INK, candle=INK, accent=ACCENT, scale: float = 0.96,
           bold: bool = False) -> Image.Image:
    """Draw the mark at `size` pixels.

    `scale` is how much of the canvas the ring's outer diameter fills. A
    profile picture is cropped to a circle, so it needs more margin than a
    favicon, which is shown as a square.
    """
    # Supersampled, then reduced. Pillow does not antialias shapes, and a
    # ring drawn at final size has a visibly stepped edge.
    over = 4
    px = size * over
    if background is None:
        img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    else:
        img = Image.new("RGBA", (px, px), background + (255,))
    pen = ImageDraw.Draw(img)

    ring_w, rects = shapes(bold)
    k = px * scale / (2.0 * RING_OUTER)

    def at(v: float) -> float:
        return px / 2.0 + (v - CENTRE) * k

    outer = RING_OUTER * k
    mid = px / 2.0
    pen.ellipse([mid - outer, mid - outer, mid + outer, mid + outer],
                outline=ring + (255,), width=max(1, round(ring_w * k)))

    colour = {"ink": candle, "accent": accent}
    for x0, y0, x1, y1, radius, role in rects:
        pen.rounded_rectangle([at(x0), at(y0), at(x1), at(y1)],
                              radius=radius * k, fill=colour[role] + (255,))

    small = img.resize((size, size), Image.LANCZOS)
    return small if background is None else small.convert("RGB")


def svg(bold: bool = False, ring: str = "currentColor",
        candle: str = "currentColor", accent: str = "#c96442",
        background: Optional[str] = None) -> str:
    """The same drawing as SVG, sharp at any size."""
    ring_w, rects = shapes(bold)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">']
    if background:
        parts.append('<rect width="1000" height="1000" fill="%s"/>' % background)
    parts.append('<circle cx="500" cy="500" r="%g" fill="none" stroke="%s" '
                 'stroke-width="%g"/>' % (RING_OUTER - ring_w / 2, ring, ring_w))
    for x0, y0, x1, y1, radius, role in rects:
        parts.append('<rect x="%g" y="%g" width="%g" height="%g" rx="%g" '
                     'fill="%s"/>' % (round(x0, 2), y0, round(x1 - x0, 2),
                                      y1 - y0, radius,
                                      accent if role == "accent" else candle))
    parts.append("</svg>")
    return "".join(parts)


def _partial() -> str:
    """The inline version for the site header and the sign-in page.

    Ring and plain candles follow the text colour and the last candle follows
    the accent, so dark mode swaps both without a second file. It is the bold
    weight because the header draws it at 20 pixels.
    """
    body = svg(bold=True, accent="var(--accent)")
    body = body.replace('fill="var(--accent)"', 'style="fill:var(--accent)"')
    body = body.replace(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" '
        'width="{{ size or 20 }}" height="{{ size or 20 }}" aria-hidden="true" '
        'focusable="false">')
    return ("{# Generated by tools/make_logo.py. Edit the geometry there, not "
            "here. #}\n" + body + "\n")


# What each file is for, and how it is drawn.
#   icons: a dark tile, because platforms show app icons as filled squares and
#          a light mark on a dark tile is what survives being shrunk.
#   avatar: profile pictures are cropped to a circle, so the ring sits well
#          inside it rather than being clipped by the crop.
ICON_SIZES = (48, 96, 180, 192, 512)
ICO_SIZES = (16, 32, 48)


def _write(root: str) -> List[Tuple[str, int]]:
    static = os.path.join(root, "static")
    brand = os.path.join(static, "brand")
    os.makedirs(brand, exist_ok=True)
    wrote: List[Tuple[str, int]] = []

    def save(img: Image.Image, *path: str) -> None:
        full = os.path.join(static, *path)
        img.save(full, "PNG", optimize=True)
        wrote.append(("/".join(path), os.path.getsize(full)))

    def tile(size: int) -> Image.Image:
        bold = size <= 48
        return render(size, background=INK, ring=PAPER, candle=PAPER,
                      scale=0.82 if bold else 0.74, bold=bold)

    for size in ICON_SIZES:
        save(tile(size), "icon-%d.png" % size)
    save(tile(512), "logo.png")

    # Each size of the .ico drawn at its own size, so 16px gets the bold
    # weight rather than a blurred reduction of the 48px one.
    small = [tile(s) for s in ICO_SIZES]
    ico = os.path.join(static, "favicon.ico")
    small[-1].save(ico, "ICO", sizes=[(s, s) for s in ICO_SIZES],
                   append_images=small[:-1])
    wrote.append(("favicon.ico", os.path.getsize(ico)))

    save(render(1080, background=INK, ring=PAPER, candle=PAPER, scale=0.62),
         "brand", "avatar-1080.png")
    save(render(1024, scale=0.96), "brand", "mark-1024.png")
    save(render(1024, ring=PAPER, candle=PAPER, scale=0.96),
         "brand", "mark-1024-light.png")

    for name, text in (("logo.svg", svg()),
                       ("logo-tile.svg", svg(ring="#f4f3ee", candle="#f4f3ee",
                                             background="#191817"))):
        full = os.path.join(static, "brand", name)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        wrote.append(("brand/" + name, os.path.getsize(full)))

    partial = os.path.join(root, "templates", "_mark.html")
    with open(partial, "w", encoding="utf-8") as fh:
        fh.write(_partial())
    wrote.append(("templates/_mark.html", os.path.getsize(partial)))
    return wrote


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, size in _write(here):
        print("  %-26s %7d bytes" % (name, size))
