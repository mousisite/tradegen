"""Exercise the database: schema, runs, signals, stats, journal, invariants.

No network: the bars are scripted ones from tests/scenarios.py.
"""
import sys, os, tempfile
import sqlite3
import threading
import time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness
import scenarios
from bot import config as config_mod, database as db
from bot.strategies import build_context, evaluate
from bot.levels import build_levels
from bot.calibrate import calibrate
from bot.learning import measure_strategies
from bot.sentiment import Sentiment
from bot.decision import decide

# A folder of its own, so two copies of this suite at once do not share a file.
tmp, _cfg_path = harness.isolate("database")
fails = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) < tol


print("--- runs, signals and statistics ---")
conn = db.connect(tmp)
check("a new file starts empty", not any(db.stats(conn).values()), db.stats(conn))

cfg = config_mod.load()
flat = Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped")
reports, signals, saved = {}, 0, 0

for sym, bars in (("AAPL", scenarios.stock_series(2000)),
                  ("BTC-USD", scenarios.coin_series(2000))):
    bars.symbol = sym
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
    wrote = db.save_strategy_stats(conn, bars.symbol, bars.interval, rep)
    reports[sym] = rep
    signals += len(sigs)
    saved += wrote
    row = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    check("the run of %s is kept as it was made" % sym,
          row["symbol"] == sym and row["action"] == plan.action and row["source"] == "test"
          and row["interval"] == "5m" and row["regime_trend"] == reg.trend
          and close(row["composite"], float(getattr(comp, "score", comp)))
          and close(row["price"], bars.last_price), dict(row))
    check("with a row for each strategy measured", wrote > 30, wrote)

now = db.stats(conn)
check("both runs, every signal and every statistic are counted",
      now["runs"] == 2 and now["run_signals"] == signals
      and now["strategy_stats"] == saved and now["symbols_seen"] == 2, (now, signals, saved))
conn.execute("UPDATE runs SET ts=ts-60 WHERE symbol='AAPL'")
conn.commit()
check("the newest run is listed first", [r["symbol"] for r in db.recent_runs(conn, limit=5)]
      == ["BTC-USD", "AAPL"]
      and [r["symbol"] for r in db.recent_runs(conn, symbol="aapl")] == ["AAPL"])

# Saving the same snapshot again must not inflate the sample count.
samples = conn.execute("SELECT SUM(n) FROM strategy_stats").fetchone()[0]
again = db.save_strategy_stats(conn, "AAPL", "5m", reports["AAPL"])
check("saving the same report again adds no row and no sample",
      again > 0 and db.stats(conn)["strategy_stats"] == saved
      and conn.execute("SELECT SUM(n) FROM strategy_stats").fetchone()[0] == samples)

print("\n--- leaderboard across instruments ---")
board = db.strategy_leaderboard(conn, minimum=40, limit=8)
check("it ranks on skill before costs, best first, from enough examples",
      0 < len(board) <= 8 and all(r["n"] >= 40 for r in board)
      and [r["gross_exp"] for r in board] == sorted((r["gross_exp"] for r in board),
                                                    reverse=True),
      [dict(r) for r in board])
# The stored statistics of one strategy, added up by hand.
if board:
    top = board[0]
    parts = conn.execute("SELECT * FROM strategy_stats WHERE strategy=? AND regime='all'",
                         (top["strategy"],)).fetchall()
    check("its figures are the stored ones added up over instruments",
          top["n"] == sum(r["n"] for r in parts) and top["symbols"] == len(parts)
          and close(top["gross_exp"], sum(r["sum_r_gross"] for r in parts) / top["n"])
          and close(top["net_exp"], sum(r["sum_r"] for r in parts) / top["n"])
          and close(top["hit_rate"], sum(r["wins"] for r in parts) / top["n"]), dict(top))

print("\n--- journal ---")
t1 = db.open_trade(conn, "AAPL", "5m", 1, entry=315.00, stop=313.50, target1=317.25,
                   quantity=60, risk_amount=90.0, run_id=1, strategy_note="trend_stack")
t2 = db.open_trade(conn, "BTC-USD", "5m", -1, entry=78000, stop=78400, target1=77400,
                   quantity=0.05, risk_amount=20.0)
t3 = db.open_trade(conn, "NVDA", "1h", 1, entry=220.0, stop=218.0, target1=224.0,
                   quantity=40, risk_amount=80.0)
check("three trades are open", db.count_open(conn) == 3 and len({t1, t2, t3}) == 3)

r = db.close_trade(conn, t1, exit_price=317.25, exit_reason="target")
check("a long closed at its target makes its planned R, 1.5, and 135 cash",
      close(r["r_multiple"], 1.5) and close(r["pnl"], 135.0), r)
r = db.close_trade(conn, t2, exit_price=78400, exit_reason="stop")
check("a short closed at its stop loses 1R, 20 cash",
      close(r["r_multiple"], -1.0) and close(r["pnl"], -20.0), r)
r = db.close_trade(conn, t3, exit_price=219.0, exit_reason="manual")
check("a long closed by hand half way to its stop loses 0.5R, 40 cash",
      close(r["r_multiple"], -0.5) and close(r["pnl"], -40.0), r)
kept = {row["id"]: row for row in db.list_trades(conn, status="closed")}
check("the closed rows keep it, with the price and the reason",
      len(kept) == 3 and close(kept[t1]["r_multiple"], 1.5)
      and close(kept[t2]["exit_price"], 78400) and kept[t3]["exit_reason"] == "manual"
      and all(row["ts_closed"] for row in kept.values()))

whole = db.journal_summary(conn)
check("the summary adds them up: one won, two lost, 0R and 75 cash in all",
      whole["trades"] == 3 and whole["open"] == 0 and whole["wins"] == 1
      and whole["losses"] == 2 and close(whole["hit_rate"], 1 / 3)
      and close(whole["expectancy_r"], 0.0) and close(whole["total_r"], 0.0)
      and close(whole["pnl"], 75.0) and close(whole["avg_win_r"], 1.5)
      and close(whole["avg_loss_r"], -0.75) and close(whole["profit_factor"], 1.0)
      and close(abs(whole["max_drawdown_r"]), 1.5), whole)
costly = db.journal_summary(conn, costs={"equity": 0.001, "crypto": 0.002})
paid = (315 * 0.001 / 1.5 + 78000 * 0.002 / 400 + 220 * 0.001 / 2) / 3
check("and after costs, each on its own kind of instrument",
      close(costly["expectancy_r"], -paid)
      and close(costly["pnl"], 75 - 315 * 60 * 0.001 - 78000 * 0.05 * 0.002 - 220 * 40 * 0.001),
      costly)

print("\n--- validation guards ---")
held = db.stats(conn)["trades"]
nan, inf = float("nan"), float("inf")
for desc, fn in [
    ("long with stop above entry", lambda: db.open_trade(conn, "X", "5m", 1, 100, 101)),
    ("short with stop below entry", lambda: db.open_trade(conn, "X", "5m", -1, 100, 99)),
    ("bad direction", lambda: db.open_trade(conn, "X", "5m", 0, 100, 99)),
    ("an entry of zero", lambda: db.open_trade(conn, "X", "5m", -1, 0, 99)),
    ("a stop below zero", lambda: db.open_trade(conn, "X", "5m", 1, 100, -1)),
    ("an entry that is no number", lambda: db.open_trade(conn, "X", "5m", 1, nan, 99)),
    ("a size of zero", lambda: db.open_trade(conn, "X", "5m", 1, 100, 99, quantity=0)),
    ("a target on the losing side", lambda: db.open_trade(conn, "X", "5m", 1, 100, 99,
                                                          target1=98)),
    ("closing an already-closed trade", lambda: db.close_trade(conn, t1, 300)),
    ("closing a nonexistent trade", lambda: db.close_trade(conn, 9999, 300)),
    ("deleting a nonexistent trade", lambda: db.delete_trade(conn, 9999)),
]:
    try:
        fn()
        check("%s is refused" % desc, False, "it was accepted")
    except ValueError as e:
        check("%s is refused" % desc, True, e)
t4 = db.open_trade(conn, "X", "5m", 1, 100, 99, quantity=2)
for desc, price in (("zero", 0), ("no number", nan), ("no end", inf)):
    try:
        db.close_trade(conn, t4, price)
        check("closing at a price of %s is refused" % desc, False, "it was accepted")
    except ValueError:
        check("closing at a price of %s is refused" % desc, True)
check("and nothing refused is kept", db.stats(conn)["trades"] == held + 1
      and conn.execute("SELECT status FROM trades WHERE id=?", (t4,)).fetchone()[0] == "open"
      and close(conn.execute("SELECT exit_price FROM trades WHERE id=?",
                             (t1,)).fetchone()[0], 317.25))
conn.close()

print("\n--- opening a connection, and migrating ---")

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
                          ("trades", "breach_noticed"), ("trades", "form_id"), ("calls", "excluded")):
        raw.execute("ALTER TABLE %s DROP COLUMN %s" % (table, column))
    raw.commit()
    raw.close()


def columns(path, table):
    raw = sqlite3.connect(path)
    try:
        return {r[1] for r in raw.execute("PRAGMA table_info(%s)" % table)}
    finally:
        raw.close()


old = os.path.join(os.path.dirname(tmp), "older.db")
old_file(old)
db.migrate(old)
check("migrating an older file adds what it lacks",
      {"digest_week", "weekly_email"} <= columns(old, "users")
      and "sum_r_gross" in columns(old, "strategy_stats")
      and "state" in columns(old, "alerts")
      and {"breach_noticed", "form_id"} <= columns(old, "trades")
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
