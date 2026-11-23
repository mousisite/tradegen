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

print("\n--- opening a connection, and migrating ---")
import sqlite3
import threading
import time

fails = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


# Every request opens a connection. Doing the migrations each time needed the
# write lock, so a long write made every page, and the health check, wait.
holder = sqlite3.connect(tmp, isolation_level=None, check_same_thread=False)
holder.execute("BEGIN IMMEDIATE")
holder.execute("INSERT INTO meta(key, value) VALUES('held', 'x')")
threading.Timer(1.5, lambda: holder.execute("ROLLBACK")).start()
began = time.time()
reader = db.connect(tmp)
reader.execute("SELECT COUNT(*) FROM runs").fetchone()
reader.close()
waited = time.time() - began
time.sleep(1.6)
holder.close()
check("a connection does not wait for someone else's write", waited < 0.5,
      "%.2fs" % waited)


def old_file(path):
    """A database as an older version left it, missing newer columns."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    db.connect(path).close()
    raw = sqlite3.connect(path)
    for table, column in (("users", "digest_week"), ("users", "weekly_email"),
                          ("strategy_stats", "sum_r_gross"), ("alerts", "state"),
                          ("trades", "breach_noticed"), ("calls", "excluded")):
        raw.execute("ALTER TABLE %s DROP COLUMN %s" % (table, column))
    raw.commit()
    raw.close()


def columns(path, table):
    raw = sqlite3.connect(path)
    try:
        return {r[1] for r in raw.execute("PRAGMA table_info(%s)" % table)}
    finally:
        raw.close()


old = os.path.join(tempfile.gettempdir(), "t_stockbot_old.db")
old_file(old)
db.migrate(old)
check("migrating an older file adds what it lacks",
      {"digest_week", "weekly_email"} <= columns(old, "users")
      and "sum_r_gross" in columns(old, "strategy_stats")
      and "state" in columns(old, "alerts")
      and "excluded" in columns(old, "calls"))
try:
    db.migrate(old)
    check("migrating twice changes nothing", True)
except Exception as exc:
    check("migrating twice changes nothing", False, exc)

# Several processes starting at once all migrate the same file. Each thread
# here has its own connection and skips this process's lock, which is what
# separate processes amount to. Looking first and then adding used to fail
# with "duplicate column name" for whoever came second.
errors = []
for trial in range(5):
    old_file(old)
    gate = threading.Barrier(8)

    def starts():
        own = sqlite3.connect(old, timeout=15.0)
        own.row_factory = sqlite3.Row
        try:
            gate.wait()
            db._prepare(own)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
        finally:
            own.close()

    racers = [threading.Thread(target=starts) for _ in range(8)]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
check("eight at once all succeed", not errors, errors[:2])
check("and the columns are there once",
      "digest_week" in columns(old, "users") and "excluded" in columns(old, "calls"))

# A file deleted while the process runs is new again, whatever was remembered.
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(old + suffix):
        os.remove(old + suffix)
again = db.connect(old)
try:
    again.execute("SELECT COUNT(*) FROM runs").fetchone()
    check("a file deleted and opened again gets its schema", True)
except sqlite3.OperationalError as exc:
    check("a file deleted and opened again gets its schema", False, exc)
finally:
    again.close()

# paper.py --db journal.db: a file name with no folder is one in this folder.
here = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="stockbot-bare-"))
try:
    bare = db.connect("bare.db")
    bare.execute("SELECT COUNT(*) FROM trades").fetchone()
    bare.close()
    check("a database named without a folder opens", os.path.exists("bare.db"))
except OSError as exc:
    check("a database named without a folder opens", False, exc)
finally:
    os.chdir(here)

print("\nDATABASE OK" if not fails else "\nFAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
