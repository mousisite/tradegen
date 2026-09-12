"""Console output setup.

Windows consoles still default to legacy code pages such as cp1252 and cp437,
which cannot represent the box-drawing and block characters the report uses.
Left alone, that raises UnicodeEncodeError partway through printing, aborting a
report the user already waited several seconds for.

This must run *before* `bot.report` is imported, because that module picks its
glyph set from the stream encoding at import time.
"""
from __future__ import annotations

import sys


def prepare_output() -> bool:
    """Switch stdout and stderr to UTF-8 where the runtime allows it.

    `errors="replace"` is the important half: even if a terminal cannot display
    a character, printing must never raise. Returns True when reconfiguration
    succeeded, in which case the report can safely use its full glyph set.
    """
    ok = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            ok = False
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Redirected to something that will not take a new encoding.
            ok = False
    return ok
