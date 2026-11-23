"""Hunt for real defects, especially ones daily bars may have introduced."""
import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import numpy as np
from bot.market import fetch_bars
from bot import strategies as S
from bot import indicators as ind

print("=" * 70)
print("1. IS SESSION VWAP DEGENERATE ON DAILY BARS?")
print("=" * 70)
for sym, iv in [("AAPL", "5m"), ("AAPL", "1h"), ("AAPL", "1d")]:
    b = fetch_bars(sym, iv)
    sessions = len(np.unique(b.session_id))
    bars_per_session = len(b) / max(sessions, 1)
    vw = ind.session_vwap(b.high, b.low, b.close, b.volume, b.session_id)
    typical = (b.high + b.low + b.close) / 3.0
    # If VWAP equals the bar's own typical price, it carries no information.
    same = np.mean(np.abs(vw - typical) < 1e-9)
    atr = ind.atr(b.high, b.low, b.close)
    dist = np.abs(b.close - vw) / np.where(atr > 0, atr, np.nan)
    print("  %-8s %-3s  bars=%-6d sessions=%-4d bars/session=%5.1f  "
          "VWAP==own typical: %5.1f%%  median |price-VWAP| = %.3f ATR"
          % (sym, iv, len(b), sessions, bars_per_session, same * 100,
             np.nanmedian(dist)))

print()
print("  Strategies that depend on session VWAP:")
b = fetch_bars("AAPL", "1d")
ctx = S.build_context(b)
i = len(b) - 1
for name in ("vwap_position", "vwap_band_fade"):
    sig = S.BY_NAME[name].fn(ctx, i)
    print("    %-16s score=%+.3f  weight=%.2f  %s"
          % (name, sig.score, sig.weight, sig.reason))

fails = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


# One bar a session makes session VWAP the bar's own middle, which says nothing.
check("on daily bars the VWAP is not the session's", ctx.vwap_is_session is False)
check("nor is it each bar's own typical price",
      np.mean(np.abs(ctx.s["vwap"] - (b.high + b.low + b.close) / 3.0) < 1e-9) < 0.5)
check("on 5-minute bars it is the session's",
      S.build_context(fetch_bars("AAPL", "5m")).vwap_is_session is True)

print()
print("=" * 70)
print("2. DOES THE OPENING RANGE STRATEGY MEAN ANYTHING ON DAILY BARS?")
print("=" * 70)
sig = S.BY_NAME["opening_range_break"].fn(ctx, i)
print("  opening_range_break  score=%+.3f weight=%.2f  %s"
      % (sig.score, sig.weight, sig.reason))
check("the opening range has no say on daily bars",
      sig.weight == 0 and sig.score == 0, (sig.score, sig.weight))
sig = S.BY_NAME["session_sweep"].fn(ctx, i)
check("nor have the day's sessions", sig.weight == 0 and sig.score == 0,
      (sig.score, sig.weight))

print()
print("=" * 70)
print("3. IS THE HOLDING HORIZON SENSIBLE PER INTERVAL?")
print("=" * 70)
print("  horizon_bars default is 24. In real time that is:")
for iv, mins in [("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60), ("1d", 60 * 24)]:
    total = 24 * mins
    if total < 60 * 8:
        span = "%.1f hours" % (total / 60)
    elif iv == "1d":
        span = "24 trading days, about 5 weeks"
    else:
        span = "%.1f trading days" % (total / (60 * 6.5))
    print("    %-4s -> %s" % (iv, span))

print()
print("=" * 70)
print("4. HOW MANY STRATEGIES GO IDLE ON DAILY BARS?")
print("=" * 70)
for iv in ("5m", "1h", "1d"):
    bb = fetch_bars("AAPL", iv)
    cc = S.build_context(bb)
    j = len(bb) - 1
    sigs, comp, reg = S.evaluate(cc, j)
    idle = [s.name for s in sigs if s.weight <= 0]
    degenerate = [s.name for s in sigs if s.weight > 0 and abs(s.score) < 0.001]
    print("  %-3s composite=%+.3f  idle=%d  zero-score=%d"
          % (iv, comp, len(idle), len(degenerate)))
    if idle:
        print("       idle: %s" % ", ".join(idle))
    check("%s: every score and the combined one are between -1 and 1" % iv,
          -1.0 <= comp <= 1.0 and all(-1.0 <= s.score <= 1.0 for s in sigs))
    check("%s: no weight is negative" % iv, all(s.weight >= 0 for s in sigs))
    check("%s: most strategies have a say" % iv, len(idle) < len(sigs) / 2,
          (len(idle), len(sigs)))

print()
print("=" * 70)
print("5. MARK-TO-MARKET COST WITH MANY OPEN POSITIONS")
print("=" * 70)
import time
t0 = time.time()
fetch_bars("AAPL", "1d")
one = time.time() - t0
print("  one instrument fetch: %.2fs" % one)
print("  20 open positions, fetched one after another: about %.0fs" % (one * 20))
print("  That happens inside the page request, so the page would hang.")


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
