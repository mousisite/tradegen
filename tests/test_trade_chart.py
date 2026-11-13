"""The analysis page's trade chart, from fixed prices: no network."""
import os as _os
import re
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot import charts  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


DAY = 86400
T0 = 1760000000
times = [T0 + DAY * k for k in range(120)]
closes = [100 + (k % 7) - 3 for k in range(120)]
opens = [c - 1 if k % 2 else c + 1 for k, c in enumerate(closes)]
highs = [max(o, c) + 1.5 for o, c in zip(opens, closes)]
lows = [min(o, c) - 1.5 for o, c in zip(opens, closes)]
vols = [1000 + 10 * k for k in range(120)]


def day(ts):
    return time.strftime("%d %b", time.gmtime(ts)).lstrip("0")


svg = charts.trade_chart(times, opens, highs, lows, closes, vols,
                         levels={"Buy at": 98.0, "Stop": 94.0, "Target": 140.0},
                         last=101.0, as_of="11 Nov 21:00 UTC", day_label=day)

print("=" * 72)
print("THE TRADE CHART")
print("=" * 72)
check("it draws", svg.startswith("<svg") and svg.endswith("</svg>"))
check("the last 90 days, one candle each", svg.count("<title>") == 90 + 1, svg.count("<title>"))
check("each candle tells its date and prices", "open " in svg and "close " in svg)
check("the plan's three prices are drawn and labelled",
      "Buy at 98.00" in svg and "Stop 94.00" in svg and "Target 140.00" in svg)
ys = [float(v) for v in re.findall(r'y1="([\d.]+)"', svg)]
check("a target far above the candles widens the range rather than being dropped",
      "Target 140.00" in svg and min(ys) >= 0)
check("it says when the price was set", "price as of 11 Nov 21:00 UTC" in svg)
check("volume is drawn underneath", svg.count('opacity="0.35"') == 90)
check("the label for the screen reader says what it is",
      'aria-label="Daily price, as of 11 Nov 21:00 UTC"' in svg)
check("too few candles draws nothing",
      charts.trade_chart(times[:5], opens[:5], highs[:5], lows[:5], closes[:5]) == "")
broken = list(closes)
broken[50] = float("nan")
check("a broken bar is skipped, not drawn as a spike",
      charts.trade_chart(times, opens, highs, lows, broken, levels={}).count("<title>") == 90 - 1 + 0)
check("a level of None or zero is left out",
      "Stop" not in charts.trade_chart(times, opens, highs, lows, closes, levels={"Stop": None, "Target": 0}))
check("text is escaped", "&lt;b&gt;" in charts.trade_chart(
    times, opens, highs, lows, closes, levels={"<b>": 100.0}))

print()
print("%d failure(s)" % len(fails))
print("TRADE CHART OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
