"""Reading JSON that anyone can send.

json.loads reads arrays and objects by recursion, so a few kilobytes of
brackets nested a few thousand deep raise RecursionError. That is not a
ValueError: an `except ValueError`, and Flask's get_json(silent=True), let it
through, and the request ends in a 500, a traceback in the log and an error
report. So the nesting is measured first, by a loop that does not recurse, and
JSON nested deeper than MAX_DEPTH is refused with a ValueError before
json.loads sees it, like any other text that is not JSON.

web.py reads every request body through loads() (its JSON provider does), and
so do the readers of Apple's tokens, which run before any signature is checked.
"""
from __future__ import annotations

import json
from typing import Any, Union

# Far deeper than anything sent here (Apple's and Stripe's JSON nest less than
# ten deep), and far shallower than Python's recursion limit of a thousand.
MAX_DEPTH = 64


class TooDeep(ValueError):
    """JSON nested deeper than it may be."""


def too_deep(text: str, limit: int = MAX_DEPTH) -> bool:
    """Whether the arrays and objects in some JSON text nest more than limit deep.

    Brackets inside strings are text, not nesting. Up to the first character
    json.loads would refuse, the depth counted here is the depth it would
    reach; past that it reads nothing, so what is counted there cannot matter.
    """
    # Fewer opening brackets than the limit cannot nest past it, so nearly
    # everything is answered here without a loop.
    if text.count("[") + text.count("{") <= limit:
        return False
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > limit:
                return True
        elif ch == "]" or ch == "}":
            depth -= 1
    return False


def loads(raw: Union[str, bytes, bytearray], limit: int = MAX_DEPTH,
          **kwargs: Any) -> Any:
    """json.loads, except that JSON nested more than limit deep is refused
    with TooDeep, a ValueError, before it is read.

    Bytes are decoded the way json.loads decodes them (UTF-8, 16 or 32), so
    the nesting measured is the nesting json.loads would read.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode(json.detect_encoding(raw), "surrogatepass")
    if isinstance(raw, str) and too_deep(raw, limit):
        raise TooDeep("JSON nested more than %d deep" % limit)
    return json.loads(raw, **kwargs)
