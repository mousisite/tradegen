"""Does per-strategy measurement actually separate good from bad strategies?"""
import sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness
harness.isolate("learning")      # the default settings, whatever config.json says

from bot import config as config_mod
from bot.market import fetch_bars
from bot.strategies import build_context, evaluate, classify_regime
from bot.learning import measure_strategies, learned_weights, summarise

# The bounds the learning promises, written out so that widening them shows.
LEAST, MOST = 0.25, 1.90
fails = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


cfg = config_mod.load()
for sym, iv in [("AAPL", "5m"), ("BTC-USD", "1h"), ("SPY", "1d")]:
    bars = fetch_bars(sym, iv)
    ctx = build_context(bars)
    i = len(bars) - 1
    bps = config_mod.cost_bps_for(bars.asset_class, cfg)
    t0 = time.time()
    rep = measure_strategies(ctx, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                             config_mod.horizon_for(bars.interval, cfg), cfg["calibration_stride"], bps)
    el = time.time() - t0
    reg = classify_regime(ctx, i)

    print("\n=== %s %s  (%d bars, %d samples, %.1fs) ===" % (sym, iv, len(bars), rep.samples, el))
    print("regime now: %s | regime mix in history: %s" % (reg.describe(), rep.regime_counts))
    print("cost %.3fR  reward:risk %.2f  breakeven %.0f%%" % (
        rep.cost_r, rep.reward_risk, rep.breakeven * 100))
    print(summarise(rep, reg))

    print("  BEST by expectancy:")
    for s in rep.best(minimum=40, limit=6):
        lo, hi = s.interval()
        print("    %-22s %-14s n=%-5d hit=%.0f%% [%.0f-%.0f%%] exp=%+.3fR mult=%.2f" % (
            s.name, s.family, s.n, s.hit_rate*100, (lo or 0)*100, (hi or 0)*100,
            s.expectancy, s.multiplier()))
    print("  WORST by expectancy:")
    for s in rep.worst(minimum=40, limit=4):
        print("    %-22s %-14s n=%-5d hit=%.0f%% exp=%+.3fR mult=%.2f" % (
            s.name, s.family, s.n, s.hit_rate*100, s.expectancy, s.multiplier()))

    lw = learned_weights(rep, reg)
    spread = sorted(lw.items(), key=lambda kv: -kv[1])
    print("  learned multipliers: top %s ... bottom %s" % (
        ["%s=%.2f" % (k, v) for k, v in spread[:3]],
        ["%s=%.2f" % (k, v) for k, v in spread[-3:]]))

    # Does applying learned weights change the composite?
    _, comp_base, _ = evaluate(ctx, i)
    _, comp_learn, _ = evaluate(ctx, i, learned=lw)
    print("  composite: base %+.3f -> learned %+.3f" % (comp_base, comp_learn))

    check("%s %s: there are learned weights to look at" % (sym, iv), len(lw) > 0)
    check("%s %s: every one is between %g and %g" % (sym, iv, LEAST, MOST),
          all(LEAST <= v <= MOST for v in lw.values()),
          {k: v for k, v in lw.items() if not LEAST <= v <= MOST})
    check("%s %s: the combined score stays between -1 and 1" % (sym, iv),
          -1.0 <= comp_base <= 1.0 and -1.0 <= comp_learn <= 1.0,
          (comp_base, comp_learn))

print("\nLEARNING OK" if not fails else "\nFAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
