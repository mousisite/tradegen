"""Full regression across the expanded system."""
import sys, io, os, tempfile, traceback, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from rich.console import Console
from bot import config as config_mod, report as report_mod, database as db
from bot.market import fetch_bars, DataError
from bot.strategies import build_context, evaluate, REGISTRY
from bot.levels import build_levels
from bot.calibrate import calibrate
from bot.learning import measure_strategies, learned_weights
from bot.sentiment import Sentiment
from bot.decision import decide

tmp = os.path.join(tempfile.gettempdir(), "t_full2.db")
for s in ("", "-wal", "-shm"):
    if os.path.exists(tmp + s):
        os.remove(tmp + s)
conn = db.connect(tmp)

cfg = config_mod.load()
flat = Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped")
combos = [(s, i) for s in ["AAPL", "NVDA", "TSLA", "SPY", "GME", "XOM", "KO",
                           "BTC-USD", "ETH-USD", "DOGE-USD"]
          for i in ["5m", "15m", "1h", "1d"]]

ok = fail = 0
actions = {}
t_start = time.time()
for sym, iv in combos:
    try:
        bars = fetch_bars(sym, iv)
        ctx = build_context(bars)
        i = len(bars) - 1
        bps = config_mod.cost_bps_for(bars.asset_class, cfg)
        sigs, comp, reg = evaluate(ctx, i, cfg["strategy_weights"])
        rep = measure_strategies(ctx, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                                 config_mod.horizon_for(bars.interval, cfg), cfg["calibration_stride"], bps,
                                 weights=cfg["strategy_weights"])
        lw = learned_weights(rep, reg)
        sigs, comp, reg = evaluate(ctx, i, cfg["strategy_weights"], learned=lw, regime=reg)
        levels = build_levels(bars, float(ctx.atr[i]))
        cal = calibrate(ctx, comp, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                        config_mod.horizon_for(bars.interval, cfg), cfg["calibration_stride"],
                        cfg["strategy_weights"], cfg["min_bucket_samples"], cost_bps=bps)
        plan = decide(ctx, bars, sigs, comp, reg, cal, flat, levels, cfg)
        actions[plan.action] = actions.get(plan.action, 0) + 1

        rid = db.record_run(conn, bars, plan, sigs, comp, reg, cal, flat, source="regress")
        db.save_strategy_stats(conn, bars.symbol, bars.interval, rep)

        buf = Console(file=io.StringIO(), width=100, force_terminal=False)
        report_mod.render(buf, bars, plan, sigs, comp, reg, cal, flat, cfg, "note",
                          strategy_report=rep, show_all_signals=False)
        payload = report_mod.to_json(bars, plan, sigs, comp, reg, cal, flat)
        assert len(payload) > 500

        assert len(sigs) == len(REGISTRY), "signal count mismatch"
        assert all(-1.0 <= s.score <= 1.0 for s in sigs), "score out of range"
        assert -1.0 <= comp <= 1.0, "composite out of range"
        assert all(0.25 <= v <= 1.9 for v in lw.values()), "learned weight out of bounds"
        if plan.action in ("BUY", "SHORT", "WAIT") and plan.entry:
            if plan.direction > 0:
                assert plan.stop < plan.entry < plan.target1, (sym, iv, "long ordering")
            else:
                assert plan.stop > plan.entry > plan.target1, (sym, iv, "short ordering")
            assert 0 < (plan.breakeven_rate or 0) <= 1
        ok += 1
    except DataError as exc:
        print("   skip %-9s %-4s %s" % (sym, iv, str(exc)[:50]))
    except Exception:
        fail += 1
        print("   FAIL %-9s %-4s" % (sym, iv))
        traceback.print_exc()

print("\npassed %d, failed %d in %.1fs" % (ok, fail, time.time() - t_start))
print("actions:", actions)
print("db:", db.stats(conn))

print("\ncross-instrument leaderboard (gross):")
for r in db.strategy_leaderboard(conn, minimum=300, limit=10):
    print("  %-22s %-14s n=%-6d hit=%.0f%% gross=%+.3f net=%+.3f syms=%d" % (
        r["strategy"], r["family"], r["n"], r["hit_rate"]*100,
        r["gross_exp"], r["net_exp"], r["symbols"]))

conn.close()
print("\nFULL REGRESSION", "OK" if fail == 0 else "HAS FAILURES")


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
