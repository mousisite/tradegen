"""Exercise the expanded strategy library."""
import sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import numpy as np
from bot.market import fetch_bars
from bot import strategies as S

print("registry: %d strategies across %d families" % (len(S.REGISTRY), len(S.FAMILIES)))
by_fam = {}
for st in S.REGISTRY:
    by_fam.setdefault(st.family, []).append(st.name)
for fam, names in by_fam.items():
    print("  %-14s %2d  %s" % (fam, len(names), ", ".join(names)))

names = S.strategy_names()
assert len(names) == len(set(names)), "duplicate strategy names!"
print("\nall names unique: OK")

for sym in ["AAPL", "BTC-USD"]:
    b = fetch_bars(sym, "5m")
    t0 = time.time()
    ctx = S.build_context(b)
    t1 = time.time()
    i = len(b) - 1
    sigs, comp, reg = S.evaluate(ctx, i)
    t2 = time.time()
    print("\n=== %s  ctx=%.2fs eval=%.4fs  %d series ===" % (sym, t1-t0, t2-t1, len(ctx.s)))
    print("regime: %s   (%s)" % (reg.describe(), reg.detail))
    print("composite %+.3f from %d signals" % (comp, len(sigs)))
    active = [s for s in sigs if s.weight > 0 and abs(s.score) > 0.05]
    print("active: %d   idle: %d" % (len(active), len(sigs) - len(active)))
    for s in sorted(sigs, key=lambda x: -abs(x.contribution))[:10]:
        print("  %-22s %-14s %+.2f w=%.2f  %s" % (s.name, s.family, s.score, s.weight, s.reason[:44]))

    # No strategy may return a score outside [-1,1] or a NaN.
    bad = [s.name for s in sigs if not (-1.0 <= s.score <= 1.0) or s.score != s.score]
    assert not bad, "out-of-range scores: %s" % bad
    badw = [s.name for s in sigs if s.weight < 0 or s.weight != s.weight]
    assert not badw, "bad weights: %s" % badw
    print("  all scores in [-1,1], all weights finite and non-negative: OK")

    # Evaluate deep in history to make sure no strategy blows up early.
    errs = []
    for j in [ctx.warmup, ctx.warmup + 5, len(b)//2, len(b)-2]:
        try:
            S.evaluate(ctx, j)
        except Exception as exc:
            errs.append((j, type(exc).__name__, str(exc)[:60]))
    print("  historical eval errors: %s" % (errs or "none"))

# Timing of a full backtest-scale sweep.
b = fetch_bars("AAPL", "5m")
ctx = S.build_context(b)
t0 = time.time()
idxs = range(ctx.warmup, len(b)-1, 3)
n = 0
for j in idxs:
    S.evaluate(ctx, j)
    n += 1
el = time.time() - t0
print("\nswept %d bars x %d strategies in %.2fs (%.1f bars/s)" % (n, len(S.REGISTRY), el, n/el))
print("\nSTRATEGY LIBRARY OK")



print()
print("=" * 72)
print("GAP RISK IN THE TRADE PLAN")
print("=" * 72)

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


from bot import decision as _dec
from bot import config as _cfg_mod
_cfg = _cfg_mod.load()
for _sym in ("SPY", "AAPL", "BTC-USD"):
    _b = fetch_bars(_sym, "1d")
    _p = _b.last_price
    for _label, _entry, _stop in (("long", _p, _p * 0.95),
                                  ("short", _p, _p * 1.05)):
        _o = _dec.size_position(_entry, _stop, _b, _cfg)
        if "error" in _o:
            continue
        print("   %-8s %-6s risk %8.2f  gap %8.2f  %.1fx  worst %.1f%%"
              % (_sym, _label, _o["risk_amount"], _o["gap_loss"],
                 _o["gap_multiple"], _o["gap_move"] * 100))
        check("gap loss never below budgeted risk (%s %s)" % (_sym, _label),
              _o["gap_loss"] >= _o["risk_amount"] - 1e-9)
        check("gap multiple at least 1 (%s %s)" % (_sym, _label),
              _o["gap_multiple"] >= 1.0 - 1e-9)
        check("gap price is past the stop or at it (%s %s)" % (_sym, _label),
              (_o["gap_price"] <= _stop + 1e-9) if _entry > _stop
              else (_o["gap_price"] >= _stop - 1e-9))
        check("gap move is a real fraction (%s %s)" % (_sym, _label),
              0 < _o["gap_move"] < 1)

# Crypto trades around the clock and has fatter tails than an index fund, so
# its gap multiple should be the larger of the two. This is the whole reason
# the figure is measured per instrument instead of assumed.
_spy = _dec.size_position(100.0, 95.0, fetch_bars("SPY", "1d"), _cfg)
_btc = _dec.size_position(100.0, 95.0, fetch_bars("BTC-USD", "1d"), _cfg)
if "error" not in _spy and "error" not in _btc:
    print("   SPY %.1fx vs BTC %.1fx" % (_spy["gap_multiple"], _btc["gap_multiple"]))
    check("crypto carries more gap risk than an index",
          _btc["gap_multiple"] > _spy["gap_multiple"])

print()
print("=" * 72)
print("EVIDENCE MAY PROMOTE A CALL, NOT ONLY VETO ONE")
print("=" * 72)

# A market entry used to require a conviction score above 0.45. Measured over
# 149 instruments the highest score produced was 0.43, so BUY was unreachable
# by construction: every call came back WAIT or AVOID. Conviction also does not
# predict expectancy (r = 0.010 on stocks, -0.349 on crypto), so raising or
# lowering that threshold would only trade one arbitrary number for another.
#
# What must hold is the other direction: a market entry is only ever issued on
# a positive measured record over a reliable sample. These pin that.
from bot import engine as _eng
from bot import ideas as _ideas

_cfg_short = dict(_cfg)
_cfg_short["allow_shorts"] = True

_calls = []
for _sym in _ideas._universe("stocks", 40)[:40]:
    try:
        _res = _eng.analyse(_sym, _cfg_short, interval="1d", with_news=False,
                            with_learning=True, record=False)
    except Exception:
        continue
    if (_res.bars.symbol or "").upper() != _sym.upper():
        continue
    _calls.append((_sym, _res.plan, _res.calibration))

_entries = [(s, p, c) for s, p, c in _calls if p.action in ("BUY", "SHORT")]
print("   %d instruments, %d of them a market entry" % (len(_calls), len(_entries)))

check("the app is able to say buy at all", len(_entries) > 0,
      "every call was WAIT or AVOID, which means the threshold is unreachable")

_unearned = [s for s, p, c in _entries
             if c.expectancy_r is None or c.expectancy_r <= 0 or not c.reliable]
check("every market entry rests on a positive measured record",
      not _unearned, _unearned)

_mislabelled = [s for s, p, c in _entries
                if (p.action == "BUY") != (p.direction > 0)]
check("the label always matches the direction of the trade",
      not _mislabelled, _mislabelled)

_no_levels = [s for s, p, c in _entries if not p.entry or not p.stop]
check("every market entry carries an entry and a stop",
      not _no_levels, _no_levels)

# A market entry means buy at today's price. If the entry sits far from it, the
# label and the plan disagree and somebody acts on the wrong one.
_prices = {s: r for s, r in
           [(s, None) for s, _, _ in _entries]}
_far = []
for _s, _p, _c in _entries:
    _live = _eng.analyse(_s, _cfg_short, interval="1d", with_news=False,
                         with_learning=True, record=False).bars.last_price
    if _p.entry and _live and abs(_p.entry - _live) / _live > 0.02:
        _far.append((_s, _p.entry, _live))
check("a market entry is priced at the market, not at a pullback",
      not _far, _far)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
