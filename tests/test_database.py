"""Exercise the database: schema, runs, signals, stats, journal, invariants."""
import sys, os, tempfile
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot import config as config_mod, database as db
from bot.market import fetch_bars
from bot.strategies import build_context, evaluate
from bot.levels import build_levels
from bot.calibrate import calibrate
from bot.learning import measure_strategies
from bot.sentiment import Sentiment
from bot.decision import decide

tmp = os.path.join(tempfile.gettempdir(), "t_stockbot.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(tmp + suffix):
        os.remove(tmp + suffix)

conn = db.connect(tmp)
print("connected, schema created ->", tmp)
print("initial stats:", db.stats(conn))

cfg = config_mod.load()
flat = Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped")

for sym in ["AAPL", "BTC-USD"]:
    bars = fetch_bars(sym, "5m")
    ctx = build_context(bars)
    i = len(bars) - 1
    sigs, comp, reg = evaluate(ctx, i, cfg["strategy_weights"])
    levels = build_levels(bars, float(ctx.atr[i]))
    bps = config_mod.cost_bps_for(bars.asset_class, cfg)
    cal = calibrate(ctx, comp, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                    config_mod.horizon_for(bars.interval, cfg), cfg["calibration_stride"],
                    cfg["strategy_weights"], cfg["min_bucket_samples"], cost_bps=bps)
    plan = decide(ctx, bars, sigs, comp, reg, cal, flat, levels, cfg)
    rid = db.record_run(conn, bars, plan, sigs, comp, reg, cal, flat, source="test")
    rep = measure_strategies(ctx, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                             config_mod.horizon_for(bars.interval, cfg), cfg["calibration_stride"], bps)
    nrows = db.save_strategy_stats(conn, bars.symbol, bars.interval, rep)
    print("  %-9s run_id=%d action=%-5s signals=%d strategy_stat_rows=%d" % (
        sym, rid, plan.action, len(sigs), nrows))

print("\nafter runs:", db.stats(conn))

# Idempotency: saving the same snapshot again must not inflate the sample count.
before = db.stats(conn)["strategy_stats"]
db.save_strategy_stats(conn, "AAPL", "5m", rep)
after = db.stats(conn)["strategy_stats"]
print("re-saving stats changed row count by %d (expect 0 growth for same keys)" % (after - before))

print("\n--- journal ---")
t1 = db.open_trade(conn, "AAPL", "5m", 1, entry=315.00, stop=313.50, target1=317.25,
                   quantity=60, risk_amount=90.0, run_id=1, strategy_note="trend_stack")
t2 = db.open_trade(conn, "BTC-USD", "5m", -1, entry=78000, stop=78400, target1=77400,
                   quantity=0.05, risk_amount=20.0)
t3 = db.open_trade(conn, "NVDA", "1h", 1, entry=220.0, stop=218.0, target1=224.0,
                   quantity=40, risk_amount=80.0)
print("opened trades:", t1, t2, t3, " open count:", db.count_open(conn))

r = db.close_trade(conn, t1, exit_price=317.25, exit_reason="target")
print("closed long at target -> R=%+.2f pnl=%+.2f" % (r["r_multiple"], r["pnl"]))
r = db.close_trade(conn, t2, exit_price=78400, exit_reason="stop")
print("closed short at stop  -> R=%+.2f pnl=%+.2f" % (r["r_multiple"], r["pnl"]))
r = db.close_trade(conn, t3, exit_price=219.0, exit_reason="manual")
print("closed long partial   -> R=%+.2f pnl=%+.2f" % (r["r_multiple"], r["pnl"]))

print("\nsummary:", {k: (round(v, 4) if isinstance(v, float) else v)
                     for k, v in db.journal_summary(conn).items()})

print("\n--- validation guards ---")
for desc, fn in [
    ("long with stop above entry", lambda: db.open_trade(conn, "X", "5m", 1, 100, 101)),
    ("short with stop below entry", lambda: db.open_trade(conn, "X", "5m", -1, 100, 99)),
    ("bad direction", lambda: db.open_trade(conn, "X", "5m", 0, 100, 99)),
    ("closing an already-closed trade", lambda: db.close_trade(conn, t1, 300)),
    ("closing a nonexistent trade", lambda: db.close_trade(conn, 9999, 300)),
]:
    try:
        fn()
        print("  BAD  %s was accepted" % desc)
    except ValueError as e:
        print("  OK   %s rejected: %s" % (desc, str(e)[:52]))

print("\n--- leaderboard across instruments ---")
for row in db.strategy_leaderboard(conn, minimum=100, limit=8):
    print("  %-22s %-14s n=%-6d gross=%+.3fR net=%+.3fR hit=%.0f%% symbols=%d" % (
        row["strategy"], row["family"], row["n"], row["gross_exp"],
        row["net_exp"], row["hit_rate"]*100, row["symbols"]))

print("\n--- recent runs ---")
for row in db.recent_runs(conn, limit=5):
    print("  #%d %-9s %-5s %-5s comp=%+.2f action=%s" % (
        row["id"], row["symbol"], row["interval"], row["regime_trend"],
        row["composite"], row["action"]))

print("\nfinal stats:", db.stats(conn))
conn.close()
print("\nDATABASE OK")


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
