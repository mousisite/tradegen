"""The daily lean read from hourly bars, on scripted prices with a known answer.

Each of the four rules on a series built for it: the 4-hour and hourly trends
agreeing; the course's 5:07:09 case (a fall, today's lows taken, then the
hourly turning up, by a close through a gap and by a break of structure);
the 4-hour gap closed through; and a 4-hour rise with the hourly turned down
and nothing taken today. Then the rule everything else rests on: a read made
at a time uses only the hourly bars closed by then. No network.
"""
import math
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402,F401

harness.isolate("bias")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import bias, clock  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


HOUR = 3600
NAMES = ("yesterday's", "last week's", "the Asia session's", "the London session's",
         "equal", "an earlier swing")


def plain(r):
    text = " ".join(r.reasons).lower()
    return not any(w in text for w in ("none", "nan", " will ", "guarantee", "atr",
                                       "smart money", "probability"))


def sane_targets(r, close):
    if r.lean == 0:
        return r.targets == []
    side_ok = all((p > close) if r.lean == 1 else (p < close) for _, p in r.targets)
    ordered = [abs(p - close) for _, p in r.targets] == sorted(abs(p - close) for _, p in r.targets)
    named = all(any(name.startswith(n) for n in NAMES) for name, _ in r.targets)
    finite = all(math.isfinite(p) for _, p in r.targets)
    return side_ok and ordered and named and finite and len(r.targets) <= 3


# ---------------------------------------------------------------------------
head("RULE 3: THE HOURLY AGREES WITH THE 4-HOUR TREND")
# ---------------------------------------------------------------------------
b = sc.hourly_path(sc.BIAS_UP)
r = bias.read(b)
check("4-hour and hourly both up, nothing turned: leans up",
      r.lean == 1 and r.checks["rule"] == 3 and r.checks["trend_4h"] == 1
      and r.checks["trend_1h"] == 1, (r.lean, r.checks))
check("the reasons say each check in words", len(r.reasons) == 3 and plain(r)
      and "agrees with the 4-hour trend" in r.reasons[-1], r.reasons)
close = float(b.close[-1])
check("up to three targets above the price, nearest first, each named",
      r.targets and sane_targets(r, close), r.targets)
check("and the hourly close that would change its mind is below the price",
      r.flip_level is not None and r.flip_level < close, (r.flip_level, close))
check("as of the last hourly bar", r.as_of == int(b.timestamp[-1]))

# ---------------------------------------------------------------------------
head("RULE 1: LOWS TAKEN TODAY, THEN THE HOURLY TURNS UP (VIDEO 5:07:09)")
# ---------------------------------------------------------------------------
b = sc.hourly_path(sc.BIAS_TAKEN_THEN_GAP)
r = bias.read(b)
check("4-hour down, yesterday's low taken, then an hourly close back through the "
      "fall's gap: leans up", r.lean == 1 and r.checks["rule"] == 1
      and r.checks["trend_4h"] == -1, (r.lean, r.checks))
check("it says what was taken and how it turned",
      r.checks["sweep"] and r.checks["sweep"]["level_name"] == "yesterday's low"
      and "closed back through a gap the fall had left" in r.reasons[-1], r.reasons[-1])
check("in plain words, with New York times", plain(r) and "New York time" in r.reasons[-1])
check("with targets above", sane_targets(r, float(b.close[-1])), r.targets)

b = sc.hourly_path(sc.BIAS_TAKEN_THEN_FLIP)
at = int(b.timestamp[136]) + HOUR             # the same trading day as the take and the break
r = bias.read(b, at=at)
check("the same with an hourly break of structure up: leans up",
      r.lean == 1 and r.checks["rule"] == 1 and r.checks["trend_4h"] == -1
      and r.checks["trend_1h"] == 1, (r.lean, r.checks))
check("the reason names the break", "broke its last swing high" in r.reasons[-1], r.reasons[-1])
next_day = bias.read(b)
check("read on the next trading day, yesterday's take no longer counts: both trends "
      "are up by then, so rule 3", next_day.lean == 1 and next_day.checks["rule"] == 3
      and next_day.checks["sweep"]["dir"] == -1, next_day.checks)

# ---------------------------------------------------------------------------
head("RULE 2: THE 4-HOUR GAP CLOSED THROUGH")
# ---------------------------------------------------------------------------
b = sc.hourly_path(sc.BIAS_GAP_CLOSED)
r = bias.read(b)
check("a 4-hour rise whose newest gap was closed through: leans against it, down",
      r.lean == -1 and r.checks["rule"] == 2 and r.checks["trend_4h"] == 1, (r.lean, r.checks))
check("the key level is the gap, closed through",
      r.checks["key"] and r.checks["key"]["kind"] == "gap"
      and r.checks["key"]["status"] == "closed through"
      and r.checks["key"]["bottom"] <= r.checks["key"]["top"], r.checks["key"])
check("said as the course's reading", "The course reads that as a turn" in r.reasons[-1]
      and plain(r), r.reasons[-1])
check("with targets below", sane_targets(r, float(b.close[-1])), r.targets)

# ---------------------------------------------------------------------------
head("RULE 4: NOTHING CLEAR")
# ---------------------------------------------------------------------------
b = sc.hourly_path(sc.bias_hourly_turn(2.5))
r = bias.read(b)
check("4-hour up, hourly turned down yesterday, nothing taken today: unclear",
      r.lean == 0 and r.checks["rule"] == 4 and r.checks["trend_4h"] == 1
      and r.checks["trend_1h"] == -1 and r.checks["sweep"] is None, (r.lean, r.checks))
check("no targets for no lean, and it says so", r.targets == []
      and "no clear lean" in r.reasons[-1])
check("the close that would change the hourly is above the price (the hourly is down)",
      r.flip_level is not None and r.flip_level > float(b.close[-1]))
b = sc.hourly_path(sc.bias_hourly_turn(3.5))
r = bias.read(b)
check("an earlier hold at the 4-hour gap does not outvote the hourly's later turn",
      r.lean == 0 and r.checks["trend_1h"] == -1, (r.lean, r.checks, r.reasons[-1]))

# ---------------------------------------------------------------------------
head("A READ AT A TIME USES ONLY THE BARS CLOSED BY THEN")
# ---------------------------------------------------------------------------
long = sc.walk(sc.futures_times(1600, step=3600), seed=21, kind="future", interval="1h",
               vol=0.003, price=6000.0)
rng = np.random.RandomState(4)
picks = sorted(rng.choice(np.arange(80, len(long.close) - 1), size=25, replace=False).tolist())
bad = []
for m in picks:
    t = int(long.timestamp[m]) + HOUR             # bar m has just closed
    whole = bias.read(long, at=t)
    cut = bias.read(clock.head(long, m + 1), at=t)
    cut_default = bias.read(clock.head(long, m + 1))
    mid = bias.read(long, at=t + 1800)            # half way through bar m + 1
    if not (whole == cut == cut_default == mid):
        bad.append(m)
check("on 25 times: the read on all the bars equals the read on the bars cut there",
      not bad, bad)
m = picks[10]
t = int(long.timestamp[m]) + HOUR
later = sc.make(long.open.copy(), long.high.copy(), long.low.copy(), long.close.copy(),
                long.timestamp, kind="future", interval="1h")
later.high[m + 1:] *= 1.5
later.close[m + 1:] *= 1.4
check("and changing every later bar changes nothing", bias.read(later, at=t) == bias.read(long, at=t))
ready = bias.prepare(long)
differ = []
for m in list(range(60, len(long.close), 37)) + picks:
    for t in (int(long.timestamp[m]) + HOUR, int(long.timestamp[m]) + HOUR + 1800):
        if bias.read(None, at=t, prepared=ready) != bias.read(long, at=t):
            differ.append(m)
check("prepared once for the whole series, a read at any time is the same read",
      not differ, differ[:5])
stock_ready = bias.prepare(sc.walk(sc.stock_times(7 * 120, step=3600), seed=3, interval="1h",
                                   vol=0.004))
st_bars = stock_ready.bars
differ = [m for m in range(70, len(st_bars.close), 9)
          if bias.read(None, at=int(st_bars.timestamp[m]) + HOUR, prepared=stock_ready)
          != bias.read(st_bars, at=int(st_bars.timestamp[m]) + HOUR)]
check("and for a stock's hourly bars, the open and the close included", not differ, differ[:5])
reads = [bias.read(long, at=int(long.timestamp[k]) + HOUR) for k in range(80, 480, 4)]
odd = [(r.lean, r.checks) for r in reads if not (
    (r.checks["rule"] == 4) == (r.lean == 0)
    and (r.checks["rule"] != 3 or r.lean == r.checks["trend_4h"] == r.checks["trend_1h"])
    and (r.checks["rule"] != 2 or r.lean == -r.checks["trend_4h"])
    and (r.checks["rule"] != 1 or r.checks["sweep"]["dir"] == r.lean))]
check("on 100 reads of a random walk, every lean matches the rule that made it", not odd, odd[:3])
check("and all four rules occur", {r.checks["rule"] for r in reads} == {1, 2, 3, 4},
      sorted({r.checks["rule"] for r in reads}))
held = [r for r in reads if r.checks["key"] and r.checks["key"]["status"] == "touched and held"]
check("a hold at the 4-hour gap or halfway line is given as rule 3's reason",
      held and all(r.checks["rule"] == 3 and "held there" in r.reasons[-1] for r in held))
check("every read's words are plain", all(plain(r) for r in reads))

# ---------------------------------------------------------------------------
head("WHEN THERE IS TOO LITTLE TO READ, AND A STOCK BEFORE ITS OPEN")
# ---------------------------------------------------------------------------
check("fewer than 60 closed hourly bars: no read", bias.read(clock.head(long, 59)) is None)
check("daily bars: no read", bias.read(sc.daily_series(300)) is None)
stock = sc.walk(sc.stock_times(7 * 40, step=3600), seed=8, interval="1h", vol=0.004)
opening = sc.stock_times(7 * 41, step=3600)[-7]       # the next session's 09:30
r = bias.read(stock, at=int(opening))
check("a stock read at 09:30 uses the sessions before it and cannot use rule 1",
      r is not None and r.checks["rule"] != 1 and r.as_of == int(stock.timestamp[-1]),
      r.checks if r else None)
if r is not None and r.lean:
    check("its targets are on the lean's side, named",
          sane_targets(r, float(stock.close[-1])), r.targets)


print()
print("%d failure(s)" % len(fails))
print("BIAS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
