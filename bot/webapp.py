"""Orenth as an app on a phone's home screen or a computer's dock.

A browser offers to install a site as an app when it finds a manifest that
names it and its icons, a service worker, and https. This module holds the
manifest and the version the service worker keeps its files under; web.py
serves both, and templates/sw.js is the worker itself.
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Dict, List, Sequence, Tuple

NAME = "Orenth"
DESCRIPTION = ("Stock and crypto research that back-tests its own advice, "
               "charges real trading costs, and says when it has no edge.")

# The site's own colours, from the tokens in static/style.css: the dark
# theme's ground, which is also the icons' tile, and the light theme's paper.
DARK = "#191817"
LIGHT = "#f4f3ee"

# (file in static/, pixels, purpose). The maskable ones fill the square with
# the dark ground, for phones that crop icons to their own shape; the badge
# is the white mark alone, for the status bar beside a notification.
ICONS: Sequence[Tuple[str, int, str]] = (
    ("icon-192.png", 192, "any"),
    ("icon-512.png", 512, "any"),
    ("icon-maskable-192.png", 192, "maskable"),
    ("icon-maskable-512.png", 512, "maskable"),
    ("badge-96.png", 96, "monochrome"),
)
SHORTCUT_ICON = "icon-96.png"

# What the service worker keeps for use without a connection, beside the
# offline page: public files every page uses, and nothing about anyone.
PRECACHE = ("style.css", "favicon.ico", "icon-192.png", "icon-512.png",
            "icon-maskable-192.png", "icon-maskable-512.png", "badge-96.png")

# Templates whose output the worker keeps or is, so a change to them is a new
# version too.
TEMPLATES = ("sw.js", "offline.html", "_mark.html")


def manifest(static: Callable[[str], str], start: str,
             shortcuts: Sequence[Tuple[str, str]]) -> Dict:
    """The web app manifest, with addresses built by the caller.

    static(name) is the address of a file in static/; start is where the app
    opens; shortcuts are (name, address) pairs offered on a long press of
    the icon. The background is the icon's own dark ground, so the screen a
    phone shows while the app starts is the icon itself rather than a dark
    tile on a light sheet. Each page then sets the bar's colour to its own
    theme with the theme-color tags in base.html.
    """
    return {
        "id": "/",
        "name": NAME,
        "short_name": NAME,
        "description": DESCRIPTION,
        "lang": "en",
        "dir": "ltr",
        "start_url": start,
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": DARK,
        "theme_color": DARK,
        "categories": ["finance"],
        "icons": [{"src": static(name), "sizes": "%dx%d" % (size, size),
                   "type": "image/png", "purpose": purpose}
                  for name, size, purpose in ICONS],
        "shortcuts": [{"name": name, "short_name": name, "url": url,
                       "icons": [{"src": static(SHORTCUT_ICON), "sizes": "96x96",
                                  "type": "image/png"}]}
                      for name, url in shortcuts],
    }


def asset_version(root: str) -> str:
    """A short fingerprint of every file the service worker could keep.

    Everything under static/, and the templates the worker is made from or
    keeps. Computed once when the server starts: a new release that changes
    any of them gives the worker a new cache name, so phones fetch the new
    files instead of keeping the old ones for ever.
    """
    digest = hashlib.sha256()
    files: List[Tuple[str, str]] = []
    static = os.path.join(root, "static")
    for folder, dirs, names in os.walk(static):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(folder, name)
            files.append((os.path.relpath(path, root).replace(os.sep, "/"), path))
    for name in TEMPLATES:
        files.append(("templates/" + name, os.path.join(root, "templates", name)))
    for label, path in files:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        digest.update(label.encode("utf-8") + b"\x00")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()[:12]
