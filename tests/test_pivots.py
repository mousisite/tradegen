"""Pivot levels: the classic sums of the session before, and which side price is on.

On made-up 5-minute days: the pivot is the mean of the prior session's high,
low and close, the levels either side are the standard ones, and the strategy
leans the way price stands from the pivot, measured in the prior session's
range (a daily pivot sits many 5-minute ATRs away). No network.
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402,F401

harness.isolate("pivots")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import indicators as ind  # noqa: E402
from bot import strategies as S  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def near(a, b):
    return abs(a - b) < 1e-9


p = ind.pivot_points(110.0, 100.0, 108.0)
check("the pivot is (high + low + close) / 3", near(p["pivot"], 106.0), p)
check("the first levels either side are twice the pivot less the low, and less the high",
      near(p["r1"], 112.0) and near(p["s1"], 102.0), p)
check("the second are a range either side of the pivot",
      near(p["r2"], 116.0) and near(p["s2"], 96.0), p)
check("the third are past the high and the low", near(p["r3"], 122.0) and near(p["s3"], 92.0), p)

b = sc.stock_series(78 * 6)
ctx = S.build_context(b)
ids = np.unique(b.session_id)
wrong, sides = [], set()
for i in range(len(b.close) - 78 * 2, len(b.close), 7):
    before = b.session_id == ids[list(ids).index(b.session_id[i]) - 1]
    high, low = float(b.high[before].max()), float(b.low[before].min())
    close = float(b.close[before][-1])
    away = (float(b.close[i]) - (high + low + close) / 3.0) / (high - low)
    sig = S.pivot_levels(ctx, i)
    sides.add(away > 0)
    if sig.score * away <= 0 or not sig.reason.startswith(
            "%.2f of the prior range %s the pivot" % (abs(away), "above" if away > 0 else "below")):
        wrong.append((i, away, sig.score, sig.reason))
check("the strategy leans the way price stands from the prior session's pivot, and says how "
      "far in that session's range", not wrong, wrong[:3])
check("both sides were tried", sides == {True, False}, sides)
check("the first session has none to go by", S.pivot_levels(ctx, 40).score == 0)

print()
print("%d failure(s)" % len(fails))
print("PIVOTS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
