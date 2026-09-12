"""Persistent memory: every analysis, every signal, every trade.

SQLite, single file, no server. The database answers three questions:

* **What did the bot say, and when?** Every run is recorded with its full
  signal breakdown, so a call can be reviewed after the fact rather than
  remembered.
* **What did each strategy actually do on this instrument?** Measured
  performance is snapshotted per symbol, interval, strategy and regime.
* **What happened to real trades?** The journal tracks paper and live
  positions from entry to exit, with realised R.

A deliberate choice on strategy statistics: each measurement *replaces* the
previous snapshot for that key rather than accumulating into it. Re-running the
analyser twice on overlapping history would otherwise double-count the same
bars and manufacture confidence that was never earned.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per analysis run.
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT,
    asset_class     TEXT,
    interval        TEXT NOT NULL,
    price           REAL,
    market_open     INTEGER,
    regime_trend    TEXT,
    regime_vol      TEXT,
    regime_dir      INTEGER,
    adx             REAL,
    composite       REAL,
    conviction      REAL,
    sentiment       REAL,
    action          TEXT,
    headline        TEXT,
    entry           REAL,
    stop            REAL,
    target1         REAL,
    target2         REAL,
    reward_risk     REAL,
    breakeven       REAL,
    probability     REAL,
    prob_samples    INTEGER,
    expectancy_r    REAL,
    cost_r          REAL,
    source          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_symbol ON runs(symbol, interval, ts);

-- Per-strategy opinion captured at each run.
CREATE TABLE IF NOT EXISTS run_signals (
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    family   TEXT,
    score    REAL,
    weight   REAL,
    reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_run ON run_signals(run_id);
CREATE INDEX IF NOT EXISTS idx_sig_strategy ON run_signals(strategy);

-- Latest measured performance per instrument/strategy/regime.
CREATE TABLE IF NOT EXISTS strategy_stats (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    family      TEXT,
    regime      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    wins        INTEGER NOT NULL,
    sum_r       REAL NOT NULL,
    sum_r_gross REAL NOT NULL DEFAULT 0,
    sum_bars    REAL NOT NULL,
    cost_r      REAL,
    updated_ts  INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, strategy, regime)
);

-- The trade journal: paper and live.
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    account       TEXT NOT NULL DEFAULT 'paper',
    symbol        TEXT NOT NULL,
    interval      TEXT,
    direction     INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    ts_opened     INTEGER NOT NULL,
    entry         REAL NOT NULL,
    stop          REAL NOT NULL,
    target1       REAL,
    target2       REAL,
    quantity      REAL,
    risk_amount   REAL,
    ts_closed     INTEGER,
    exit_price    REAL,
    exit_reason   TEXT,
    r_multiple    REAL,
    pnl           REAL,
    strategy_note TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, account);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

-- Instruments you want kept an eye on.
-- Keyed on the person as well as the symbol: two people watching AAPL are two
-- rows, not a collision.
CREATE TABLE IF NOT EXISTS watchlist (
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL DEFAULT '1d',
    added_ts   INTEGER NOT NULL,
    note       TEXT,
    last_run   INTEGER,
    user_id    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, symbol)
);

-- Conditions to watch for. Checked on demand or by a scheduled run.
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- price_above | price_below | pct_move |
                                    -- rsi_above | rsi_below | signal_flip |
                                    -- near_stop | near_target
    threshold   REAL,
    interval    TEXT NOT NULL DEFAULT '1d',
    note        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_ts  INTEGER NOT NULL,
    last_checked INTEGER,
    triggered_ts INTEGER,
    trigger_value REAL,
    trigger_note TEXT,
    repeat      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active, symbol);

-- Written investment theses, so a view can be judged later rather than
-- remembered as having been right.
CREATE TABLE IF NOT EXISTS theses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    created_ts  INTEGER NOT NULL,
    price       REAL NOT NULL,
    balance     REAL,
    verdict     TEXT,
    bull_json   TEXT,
    bear_json   TEXT,
    falsifiers  TEXT,
    sources     TEXT,
    user_note   TEXT,
    closed_ts   INTEGER,
    closed_price REAL,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS idx_theses_symbol ON theses(symbol, created_ts);

-- Headlines and posts seen at each run, for later post-mortem.
CREATE TABLE IF NOT EXISTS run_news (
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind      TEXT,
    source    TEXT,
    age_hours REAL,
    score     REAL,
    text      TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_run ON run_news(run_id);
"""


def default_path() -> str:
    """Where the database lives unless a caller says otherwise.

    STOCKBOT_DB overrides it, which is how you keep separate journals for a
    paper account and a live one, and how the tests avoid writing to yours.
    """
    override = os.environ.get("STOCKBOT_DB")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "stockbot.db")


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open the database, creating it and its schema when absent."""
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # The web server is threaded, so two requests can want the database at once.
    # Without a busy timeout the loser raises "database is locked" immediately
    # instead of waiting the fraction of a second the other write needs.
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps a long analysis run from blocking a concurrent journal read.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    # Accounts come after the main schema, because giving existing rows an
    # owner means altering tables the script above has just made sure exist.
    from . import accounts as accounts_mod
    accounts_mod.ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently leaves an older table untouched, so new
    columns have to be added explicitly or every insert against an existing
    file starts failing.
    """
    wanted = {
        "strategy_stats": [("sum_r_gross", "REAL NOT NULL DEFAULT 0")],
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in
                    conn.execute("PRAGMA table_info(%s)" % table)}
        for column, spec in columns:
            if column not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, spec))


# ---------------------------------------------------------------------------
# Ownership
#
# Every personal table carries user_id. The functions below take an optional
# `user` argument: pass one and the query is confined to that person, omit it
# and the whole table is visible. Omitting it is correct for the command line
# tools, which run as whoever is at the keyboard, and is never correct in the
# web layer, where a missing filter means one person reading another's trades.
# test_accounts.py asserts that separation directly rather than trusting it.
# ---------------------------------------------------------------------------


def _own(sql: str, args: list, user, first: bool = False) -> tuple:
    """Append a user filter to a query when one was asked for."""
    if user is None:
        return sql, args
    joiner = " WHERE " if first else " AND "
    return sql + joiner + "user_id = ?", args + [int(user)]


# ---------------------------------------------------------------------------
# Recording analysis runs
# ---------------------------------------------------------------------------

def record_run(conn: sqlite3.Connection, bars, plan, signals, composite,
               regime, calib, sentiment, source: str = "cli",
               user: int = 1) -> int:
    """Store one analysis run and everything it produced. Returns the run id."""
    cur = conn.execute(
        """INSERT INTO runs (ts, symbol, name, asset_class, interval, price,
               market_open, regime_trend, regime_vol, regime_dir, adx,
               composite, conviction, sentiment, action, headline, entry, stop,
               target1, target2, reward_risk, breakeven, probability,
               prob_samples, expectancy_r, cost_r, source, user_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time()), bars.symbol, bars.name, bars.asset_class, bars.interval,
         float(bars.last_price), 1 if bars.market_open else 0,
         regime.trend, regime.volatility, regime.direction,
         None if regime.adx != regime.adx else float(regime.adx),
         float(composite), float(plan.conviction), float(sentiment.score),
         plan.action, plan.headline, plan.entry, plan.stop, plan.target1,
         plan.target2, plan.reward_risk, plan.breakeven_rate, plan.probability,
         plan.prob_samples, plan.expectancy_r, calib.cost_r, source,
         int(user)))
    run_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO run_signals (run_id, strategy, family, score, weight, reason) "
        "VALUES (?,?,?,?,?,?)",
        [(run_id, s.name, s.family, float(s.score), float(s.weight), s.reason)
         for s in signals])

    if getattr(sentiment, "items", None):
        conn.executemany(
            "INSERT INTO run_news (run_id, kind, source, age_hours, score, text) "
            "VALUES (?,?,?,?,?,?)",
            [(run_id, it.kind, it.source, float(it.age_hours), float(it.score),
              it.text[:500]) for it in sentiment.items[:20]])
    conn.commit()
    return run_id


def save_strategy_stats(conn: sqlite3.Connection, symbol: str, interval: str,
                        report) -> int:
    """Snapshot measured per-strategy performance.

    Rows are replaced rather than summed. Two runs over overlapping history
    describe the same bars, and adding them together would invent evidence.
    """
    now = int(time.time())
    rows = []
    for st in report.overall.values():
        rows.append((symbol, interval, st.name, st.family, "all", st.n, st.wins,
                     st.sum_r, st.sum_r_gross, st.sum_bars, report.cost_r, now))
    for st in report.by_regime.values():
        rows.append((symbol, interval, st.name, st.family, st.regime, st.n,
                     st.wins, st.sum_r, st.sum_r_gross, st.sum_bars,
                     report.cost_r, now))
    conn.executemany(
        """INSERT OR REPLACE INTO strategy_stats
           (symbol, interval, strategy, family, regime, n, wins, sum_r,
            sum_r_gross, sum_bars, cost_r, updated_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


def load_strategy_stats(conn: sqlite3.Connection, symbol: str,
                        interval: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM strategy_stats WHERE symbol=? AND interval=? "
        "ORDER BY regime, strategy", (symbol, interval)))


# ---------------------------------------------------------------------------
# The trade journal
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """A trade as stored. Mirrors the trades table."""
    id: int
    symbol: str
    direction: int
    status: str
    entry: float
    stop: float
    target1: Optional[float]
    quantity: Optional[float]
    ts_opened: int
    r_multiple: Optional[float] = None
    pnl: Optional[float] = None


def open_trade(conn: sqlite3.Connection, symbol: str, interval: str,
               direction: int, entry: float, stop: float,
               target1: Optional[float] = None, target2: Optional[float] = None,
               quantity: Optional[float] = None, risk_amount: Optional[float] = None,
               run_id: Optional[int] = None, account: str = "paper",
               strategy_note: str = "", notes: str = "", user: int = 1) -> int:
    """Record a newly opened position."""
    if direction not in (1, -1):
        raise ValueError("direction must be +1 (long) or -1 (short)")
    if entry <= 0:
        raise ValueError("entry price must be positive")
    # A stop on the wrong side is almost always a typo, and silently accepting
    # it would corrupt every R calculation that follows.
    if direction > 0 and stop >= entry:
        raise ValueError("for a long, the stop must sit below the entry")
    if direction < 0 and stop <= entry:
        raise ValueError("for a short, the stop must sit above the entry")

    cur = conn.execute(
        """INSERT INTO trades (run_id, account, symbol, interval, direction,
               status, ts_opened, entry, stop, target1, target2, quantity,
               risk_amount, strategy_note, notes, user_id)
           VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?)""",
        (run_id, account, symbol.upper(), interval, direction, int(time.time()),
         float(entry), float(stop), target1, target2, quantity, risk_amount,
         strategy_note, notes, int(user)))
    conn.commit()
    return int(cur.lastrowid)


def close_trade(conn: sqlite3.Connection, trade_id: int, exit_price: float,
                exit_reason: str = "manual", notes: str = "",
                user: Optional[int] = None) -> Dict:
    """Close a position and compute its realised R multiple and cash result."""
    sql, args = _own("SELECT * FROM trades WHERE id=?", [trade_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("no trade with id %d" % trade_id)
    if row["status"] != "open":
        raise ValueError("trade %d is already %s" % (trade_id, row["status"]))

    direction = int(row["direction"])
    entry, stop = float(row["entry"]), float(row["stop"])
    risk_per_unit = abs(entry - stop)
    move = (exit_price - entry) * direction
    r_multiple = (move / risk_per_unit) if risk_per_unit > 0 else 0.0
    qty = float(row["quantity"] or 0.0)
    pnl = move * qty

    conn.execute(
        """UPDATE trades SET status='closed', ts_closed=?, exit_price=?,
               exit_reason=?, r_multiple=?, pnl=?,
               notes = CASE WHEN ?='' THEN notes ELSE COALESCE(notes,'') || ' ' || ? END
           WHERE id=?""",
        (int(time.time()), float(exit_price), exit_reason, r_multiple, pnl,
         notes, notes, trade_id))
    conn.commit()
    return {"id": trade_id, "r_multiple": r_multiple, "pnl": pnl,
            "symbol": row["symbol"], "direction": direction,
            "entry": entry, "exit": exit_price}


def delete_trade(conn: sqlite3.Connection, trade_id: int,
                 user: Optional[int] = None) -> Dict:
    """Remove a trade entirely.

    Needed because a mistyped entry otherwise corrupts every statistic the
    journal produces, with no way to take it back. Returns what was deleted so
    the caller can confirm it to the user.
    """
    sql, args = _own("SELECT * FROM trades WHERE id=?", [trade_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("no trade with id %d" % trade_id)
    sql, args = _own("DELETE FROM trades WHERE id=?", [trade_id], user)
    conn.execute(sql, args)
    conn.commit()
    return {"id": trade_id, "symbol": row["symbol"], "status": row["status"],
            "direction": int(row["direction"]), "entry": float(row["entry"])}


def list_trades(conn: sqlite3.Connection, status: Optional[str] = None,
                account: Optional[str] = None, symbol: Optional[str] = None,
                limit: int = 100, user: Optional[int] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM trades WHERE 1=1"
    args: List = []
    sql, args = _own(sql, args, user)
    if status:
        sql += " AND status=?"
        args.append(status)
    if account:
        sql += " AND account=?"
        args.append(account)
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    sql += " ORDER BY ts_opened DESC LIMIT ?"
    args.append(limit)
    return list(conn.execute(sql, args))


def journal_summary(conn: sqlite3.Connection, account: str = "paper",
                    user: Optional[int] = None) -> Dict:
    """Realised performance of the journal so far.

    Expectancy is reported in R rather than cash because R is comparable across
    instruments and position sizes, which is what makes it the honest measure
    of whether a process works.
    """
    sql, args = _own("SELECT * FROM trades WHERE status='closed' AND account=?",
                     [account], user)
    rows = list(conn.execute(sql, args))
    n = len(rows)
    if n == 0:
        return {"trades": 0, "open": count_open(conn, account, user)}

    rs = [float(r["r_multiple"] or 0.0) for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pnl = sum(float(r["pnl"] or 0.0) for r in rows)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # Peak-to-trough of the cumulative R curve: the number that decides whether
    # a strategy is survivable, not just profitable.
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trades": n,
        "open": count_open(conn, account, user),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate": len(wins) / n,
        "expectancy_r": sum(rs) / n,
        "total_r": sum(rs),
        "avg_win_r": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_r": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_r": max_dd,
        "pnl": pnl,
    }


def count_open(conn: sqlite3.Connection, account: str = "paper",
               user: Optional[int] = None) -> int:
    sql, args = _own("SELECT COUNT(*) AS c FROM trades WHERE status='open' "
                     "AND account=?", [account], user)
    row = conn.execute(sql, args).fetchone()
    return int(row["c"]) if row else 0


def strategy_leaderboard(conn: sqlite3.Connection, symbol: Optional[str] = None,
                         regime: str = "all", minimum: int = 40,
                         limit: int = 15, rank_by: str = "gross") -> List[sqlite3.Row]:
    """Rank strategies by measured expectancy across everything recorded.

    Ranks on *gross* expectancy by default. Costs vary enormously between
    instruments, so a net-ranked cross-instrument table mostly sorts by how
    expensive each symbol is to trade rather than by strategy quality. Gross
    asks the question this table exists to answer: does the strategy predict
    direction? Net belongs in the per-instrument view, where the cost is real
    and specific.
    """
    order = "gross_exp" if rank_by == "gross" else "net_exp"
    sql = """SELECT strategy, family,
                    SUM(n) AS n, SUM(wins) AS wins,
                    SUM(sum_r) AS sum_r,
                    SUM(sum_r_gross) AS sum_r_gross,
                    SUM(sum_r) * 1.0 / SUM(n) AS net_exp,
                    SUM(sum_r_gross) * 1.0 / SUM(n) AS gross_exp,
                    SUM(wins) * 1.0 / SUM(n) AS hit_rate,
                    COUNT(DISTINCT symbol) AS symbols
             FROM strategy_stats WHERE regime=?"""
    args: List = [regime]
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    sql += (" GROUP BY strategy, family HAVING SUM(n) >= ? "
            "ORDER BY %s DESC LIMIT ?" % order)
    args.extend([minimum, limit])
    return list(conn.execute(sql, args))


def recent_runs(conn: sqlite3.Connection, symbol: Optional[str] = None,
                limit: int = 20,
                user: Optional[int] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM runs WHERE 1=1"
    args: List = []
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    sql, args = _own(sql, args, user)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return list(conn.execute(sql, args))


def stats(conn: sqlite3.Connection, user: Optional[int] = None) -> Dict:
    """Row counts, for a quick sense of how much the bot has recorded.

    The shared tables are counted whole even when a user is given: the strategy
    record describes instruments rather than people, and showing someone a
    smaller number for it would misrepresent what the bot actually knows.
    """
    shared = ("strategy_stats",)
    out = {}
    for table in ("runs", "run_signals", "strategy_stats", "trades", "run_news",
                  "watchlist", "alerts", "theses"):
        if table in shared or user is None:
            sql, args = "SELECT COUNT(*) AS c FROM %s" % table, []
        elif table in ("run_signals", "run_news"):
            sql = ("SELECT COUNT(*) AS c FROM %s WHERE run_id IN "
                   "(SELECT id FROM runs WHERE user_id=?)" % table)
            args = [int(user)]
        else:
            sql, args = "SELECT COUNT(*) AS c FROM %s WHERE user_id=?" % table, [int(user)]
        out[table] = int(conn.execute(sql, args).fetchone()["c"])

    sql, args = _own("SELECT COUNT(DISTINCT symbol) AS c FROM runs", [], user,
                     first=True)
    out["symbols_seen"] = int(conn.execute(sql, args).fetchone()["c"])
    return out


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def watch_add(conn: sqlite3.Connection, symbol: str, interval: str = "1d",
              note: str = "", user: int = 1) -> None:
    sym, uid, now = symbol.upper(), int(user), int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO watchlist "
        "(symbol, interval, added_ts, note, last_run, user_id) VALUES "
        "(?,?,COALESCE((SELECT added_ts FROM watchlist WHERE symbol=? AND user_id=?),?),"
        "?,(SELECT last_run FROM watchlist WHERE symbol=? AND user_id=?),?)",
        (sym, interval, sym, uid, now, note, sym, uid, uid))
    conn.commit()


def watch_remove(conn: sqlite3.Connection, symbol: str,
                 user: Optional[int] = None) -> None:
    sql, args = _own("DELETE FROM watchlist WHERE symbol=?",
                     [symbol.upper()], user)
    conn.execute(sql, args)
    conn.commit()


def watchlist(conn: sqlite3.Connection,
              user: Optional[int] = None) -> List[sqlite3.Row]:
    sql, args = _own("SELECT * FROM watchlist", [], user, first=True)
    return list(conn.execute(sql + " ORDER BY symbol", args))


def watch_touch(conn: sqlite3.Connection, symbol: str,
                user: Optional[int] = None) -> None:
    sql, args = _own("UPDATE watchlist SET last_run=? WHERE symbol=?",
                     [int(time.time()), symbol.upper()], user)
    conn.execute(sql, args)
    conn.commit()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def alert_add(conn: sqlite3.Connection, symbol: str, kind: str,
              threshold: Optional[float] = None, interval: str = "1d",
              note: str = "", repeat: bool = False, user: int = 1) -> int:
    """Create an alert. Validation lives here so every caller gets it."""
    from .alerts import KINDS
    if kind not in KINDS:
        raise ValueError("Unknown alert type %r." % kind)
    if KINDS[kind]["needs_threshold"] and threshold is None:
        raise ValueError("%s needs a value to compare against." % KINDS[kind]["label"])
    if threshold is not None and KINDS[kind]["unit"] == "price" and threshold <= 0:
        raise ValueError("A price alert needs a positive price.")

    cur = conn.execute(
        "INSERT INTO alerts (symbol, kind, threshold, interval, note, active, "
        "created_ts, repeat, user_id) VALUES (?,?,?,?,?,1,?,?,?)",
        (symbol.upper(), kind, threshold, interval, note, int(time.time()),
         1 if repeat else 0, int(user)))
    conn.commit()
    return int(cur.lastrowid)


def alert_delete(conn: sqlite3.Connection, alert_id: int,
                 user: Optional[int] = None) -> None:
    sql, args = _own("SELECT id FROM alerts WHERE id=?", [alert_id], user)
    if conn.execute(sql, args).fetchone() is None:
        raise ValueError("No alert with id %d" % alert_id)
    sql, args = _own("DELETE FROM alerts WHERE id=?", [alert_id], user)
    conn.execute(sql, args)
    conn.commit()


def alert_list(conn: sqlite3.Connection, active_only: bool = False,
               user: Optional[int] = None) -> List[sqlite3.Row]:
    sql, args = "SELECT * FROM alerts WHERE 1=1", []
    if active_only:
        sql += " AND active=1"
    sql, args = _own(sql, args, user)
    sql += " ORDER BY triggered_ts IS NULL DESC, created_ts DESC"
    return list(conn.execute(sql, args))


def alert_fire(conn: sqlite3.Connection, alert_id: int, value: float,
               note: str, user: Optional[int] = None) -> None:
    """Record that an alert's condition was met."""
    row = conn.execute("SELECT repeat FROM alerts WHERE id=?", (alert_id,)).fetchone()
    keep_active = bool(row and row["repeat"])
    conn.execute(
        "UPDATE alerts SET triggered_ts=?, trigger_value=?, trigger_note=?, "
        "active=?, last_checked=? WHERE id=?",
        (int(time.time()), value, note, 1 if keep_active else 0,
         int(time.time()), alert_id))
    conn.commit()


def alert_checked(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute("UPDATE alerts SET last_checked=? WHERE id=?",
                 (int(time.time()), alert_id))
    conn.commit()


def alert_reset(conn: sqlite3.Connection, alert_id: int,
                user: Optional[int] = None) -> None:
    """Re-arm a fired alert."""
    sql, args = _own("UPDATE alerts SET active=1, triggered_ts=NULL, "
                     "trigger_value=NULL, trigger_note=NULL WHERE id=?",
                     [alert_id], user)
    conn.execute(sql, args)
    conn.commit()


# ---------------------------------------------------------------------------
# Thesis memory
# ---------------------------------------------------------------------------

def thesis_save(conn: sqlite3.Connection, symbol: str, price: float,
                balance: float, verdict: str, bull: List[Dict],
                bear: List[Dict], falsifiers: List[str], sources: List[Dict],
                note: str = "", user: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO theses (symbol, created_ts, price, balance, verdict, "
        "bull_json, bear_json, falsifiers, sources, user_note, user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (symbol.upper(), int(time.time()), float(price), float(balance), verdict,
         json.dumps(bull), json.dumps(bear), json.dumps(falsifiers),
         json.dumps(sources), note, int(user)))
    conn.commit()
    return int(cur.lastrowid)


def thesis_list(conn: sqlite3.Connection, symbol: Optional[str] = None,
                limit: int = 50, user: Optional[int] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM theses WHERE 1=1"
    args: List = []
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    sql, args = _own(sql, args, user)
    sql += " ORDER BY created_ts DESC LIMIT ?"
    args.append(limit)
    return list(conn.execute(sql, args))


def thesis_get(conn: sqlite3.Connection, thesis_id: int,
               user: Optional[int] = None) -> Optional[sqlite3.Row]:
    sql, args = _own("SELECT * FROM theses WHERE id=?", [thesis_id], user)
    return conn.execute(sql, args).fetchone()


def thesis_close(conn: sqlite3.Connection, thesis_id: int, price: float,
                 outcome: str, user: Optional[int] = None) -> None:
    sql, args = _own("SELECT id FROM theses WHERE id=?", [thesis_id], user)
    if conn.execute(sql, args).fetchone() is None:
        raise ValueError("No thesis with id %d" % thesis_id)
    sql, args = _own("UPDATE theses SET closed_ts=?, closed_price=?, outcome=? "
                     "WHERE id=?",
                     [int(time.time()), float(price), outcome, thesis_id], user)
    conn.execute(sql, args)
    conn.commit()


def thesis_reopen(conn: sqlite3.Connection, thesis_id: int,
                  user: Optional[int] = None) -> None:
    """Undo a closing. The record of having closed it is not worth keeping."""
    sql, args = _own("SELECT id FROM theses WHERE id=?", [thesis_id], user)
    if conn.execute(sql, args).fetchone() is None:
        raise ValueError("No thesis with id %d" % thesis_id)
    sql, args = _own("UPDATE theses SET closed_ts=NULL, closed_price=NULL, "
                     "outcome=NULL WHERE id=?", [thesis_id], user)
    conn.execute(sql, args)
    conn.commit()


def thesis_delete(conn: sqlite3.Connection, thesis_id: int,
                  user: Optional[int] = None) -> None:
    sql, args = _own("SELECT id FROM theses WHERE id=?", [thesis_id], user)
    if conn.execute(sql, args).fetchone() is None:
        raise ValueError("No thesis with id %d" % thesis_id)
    sql, args = _own("DELETE FROM theses WHERE id=?", [thesis_id], user)
    conn.execute(sql, args)
    conn.commit()
