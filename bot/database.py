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
import threading
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


# Files this process has already given their schema, keyed by path. Per file,
# because tests and scripts open several databases in one process.
_PREPARED: set = set()
_PREPARE_LOCK = threading.Lock()


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open the database, creating it and its schema when absent.

    The schema is created and brought up to date the first time this process
    opens a file, not on every call. Every request opens a connection, and the
    migrations write, so running them each time made every page and the health
    check wait for the write lock behind whatever else was writing.
    """
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # The web server is threaded, so two requests can want the database at once.
    # Without a busy timeout the loser raises "database is locked" immediately
    # instead of waiting the fraction of a second the other write needs.
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _prepared(path):
        try:
            with _PREPARE_LOCK:
                if not _prepared(path):
                    _prepare(conn)
                    _PREPARED.add(_key(path))
        except BaseException:
            conn.close()
            raise
    return conn


def migrate(path: Optional[str] = None) -> None:
    """Create the schema and bring an older file up to date, now.

    serve.py calls this once at start-up, before any other thread exists, so a
    new column is added by one thread rather than raced for by the first few
    requests. Scripts and tests need not call it: connect() does the same work
    the first time it meets a file.
    """
    path = path or default_path()
    with _PREPARE_LOCK:
        _PREPARED.discard(_key(path))
    connect(path).close()


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _prepared(path: str) -> bool:
    # An empty file is a new database, or one deleted and opened again since,
    # and needs its schema whatever this process remembers about the path.
    try:
        return _key(path) in _PREPARED and os.path.getsize(path) > 0
    except OSError:
        return False


def _prepare(conn: sqlite3.Connection) -> None:
    """Create every table and add every column this version expects.

    Safe to run again, and safe when two processes start at the same moment:
    each step creates what is missing or finds it already there.
    """
    # WAL keeps a long analysis run from blocking a concurrent journal read. It
    # is a property of the file, so it is set here rather than per connection.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    # Accounts come after the main schema, because giving existing rows an
    # owner means altering tables the script above has just made sure exist.
    from . import accounts as accounts_mod
    from . import public as public_mod
    _allowing_race(conn, accounts_mod.ensure_schema)
    # The public pages add columns of their own, and doing it here means they
    # exist before the first request rather than being added by several at once.
    _allowing_race(conn, public_mod.ensure_schema)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


def _allowing_race(conn: sqlite3.Connection, ensure) -> None:
    """Run another module's schema step, allowing for a process racing it.

    Those steps look for a column and add it when it is missing. When another
    process starting at the same moment adds it in between, SQLite calls the
    new column a duplicate, which means the work is done: the step is run again
    and finds it there.
    """
    for attempt in range(5):
        try:
            ensure(conn)
            return
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower() or attempt == 4:
                raise
            conn.rollback()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently leaves an older table untouched, so new
    columns have to be added explicitly or every insert against an existing
    file starts failing.
    """
    wanted = {
        "strategy_stats": [("sum_r_gross", "REAL NOT NULL DEFAULT 0")],
        # state: the last call seen by a call alert, e.g. WAIT, so a change
        # can be noticed; for any other alert, whether its condition was met
        # at the last look ("met" or "clear"), so one that keeps watching
        # fires when it becomes true rather than on every check.
        # last_error: why the last check of it failed.
        "alerts": [("state", "TEXT"), ("last_error", "TEXT"),
                   ("last_error_ts", "INTEGER")],
        # When the background check last told the owner this position had
        # crossed its stop, so it is said once, not every five minutes.
        "trades": [("breach_noticed", "INTEGER")],
    }
    # Looking and adding happen inside one write transaction, so a second
    # process starting at the same moment waits here and then finds the
    # columns already there instead of adding them a second time.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, columns in wanted.items():
            existing = {row[1] for row in
                        conn.execute("PRAGMA table_info(%s)" % table)}
            for column, spec in columns:
                if column in existing:
                    continue
                try:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                 % (table, column, spec))
                except sqlite3.OperationalError as exc:
                    # Added by someone else first: the outcome that was wanted.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
    import math
    if direction not in (1, -1):
        raise ValueError("direction must be +1 (long) or -1 (short)")
    for label, value in (("entry", entry), ("stop", stop), ("target", target1),
                         ("second target", target2), ("size", quantity)):
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("the %s must be a real number" % label)
    if entry <= 0:
        raise ValueError("entry price must be positive")
    if stop <= 0:
        raise ValueError("the stop must be a positive price")
    if quantity is not None and float(quantity) <= 0:
        raise ValueError("the size must be more than zero, or left empty")
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
    import math
    if not math.isfinite(float(exit_price)) or float(exit_price) <= 0:
        raise ValueError("the exit price must be a positive number")
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
                    user: Optional[int] = None,
                    costs: Optional[Dict[str, float]] = None) -> Dict:
    """Realised performance of the journal so far.

    Expectancy is reported in R rather than cash because R is comparable across
    instruments and position sizes, which is what makes it the honest measure
    of whether a process works.

    costs maps "equity" and "crypto" to a round-trip cost as a fraction of the
    price. Given them, every figure is after costs, which is what the account
    would really show; the before-cost expectancy is kept beside it. Trades are
    taken in the order they closed, which is the order the drawdown happened.
    """
    sql, args = _own("SELECT * FROM trades WHERE status='closed' AND account=?",
                     [account], user)
    rows = list(conn.execute(sql + " ORDER BY ts_closed, id", args))
    n = len(rows)
    if n == 0:
        return {"trades": 0, "open": count_open(conn, account, user)}

    costs = costs or {}
    gross = [float(r["r_multiple"] or 0.0) for r in rows]
    rs, pnl = [], 0.0
    for r, row in zip(gross, rows):
        cost = float(costs.get("crypto" if row["symbol"].endswith("-USD") else "equity", 0.0))
        risk = abs(float(row["entry"]) - float(row["stop"]))
        cost_r = (float(row["entry"]) * cost / risk) if risk > 0 else 0.0
        rs.append(r - cost_r)
        pnl += float(row["pnl"] or 0.0) - float(row["entry"]) * float(row["quantity"] or 0.0) * cost
    # A scratch (exactly even) is neither a win nor a loss.
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]

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
        "scratches": n - len(wins) - len(losses),
        "after_costs": bool(costs),
        "gross_expectancy_r": sum(gross) / n,
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

# Longer than any real ticker, and short enough that a pasted paragraph is
# refused. Notes are cut rather than refused: a long note is not a mistake.
MAX_SYMBOL = 20
MAX_NOTE = 200


def _symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("No instrument given.")
    if len(sym) > MAX_SYMBOL:
        raise ValueError("That is too long for a ticker. Type the ticker, for example AAPL.")
    return sym


def watch_add(conn: sqlite3.Connection, symbol: str, interval: str = "1d",
              note: str = "", user: int = 1) -> None:
    sym, uid, now = _symbol(symbol), int(user), int(time.time())
    note = (note or "")[:MAX_NOTE]
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

# The most alerts one account can keep, on any plan. Each is checked every
# few minutes and each fire is stored, so there has to be a ceiling somewhere.
MAX_ALERTS = 200


def alert_add(conn: sqlite3.Connection, symbol: str, kind: str,
              threshold: Optional[float] = None, interval: str = "1d",
              note: str = "", repeat: bool = False, user: int = 1) -> int:
    """Create an alert. Validation lives here so every caller gets it.

    A percent threshold is a fraction: 0.05 is 5%.
    """
    from .alerts import DAILY_ONLY, KINDS, problem
    why = problem(kind, threshold)
    if why:
        raise ValueError(why)
    sym = _symbol(symbol)
    held = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id=?",
                        (int(user),)).fetchone()[0]
    if held >= MAX_ALERTS:
        raise ValueError("You have %d alerts, the most one account can keep. "
                         "Delete some you no longer need first." % held)
    if not KINDS[kind]["needs_threshold"]:
        threshold = None
    if kind in DAILY_ONLY:
        interval = "1d"

    cur = conn.execute(
        "INSERT INTO alerts (symbol, kind, threshold, interval, note, active, "
        "created_ts, repeat, user_id) VALUES (?,?,?,?,?,1,?,?,?)",
        (sym, kind, threshold, interval or "1d", (note or "")[:MAX_NOTE],
         int(time.time()), 1 if repeat else 0, int(user)))
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
               note: str, user: Optional[int] = None,
               state: Optional[str] = None, was: Optional[str] = None) -> bool:
    """Record that an alert's condition was met. True if this call recorded it.

    A one-off alert switches itself off; one set to keep watching stays on.
    Given a state, the update goes through only while the alert is still on
    and still in the state the caller read (was), so when two checks overlap
    one fire is recorded and told once, not twice.
    """
    now = int(time.time())
    sql = ("UPDATE alerts SET triggered_ts=?, trigger_value=?, trigger_note=?, "
           "active=CASE WHEN repeat=1 THEN 1 ELSE 0 END, last_checked=?, "
           "last_error=NULL, last_error_ts=NULL, state=COALESCE(?, state) "
           "WHERE id=? AND active=1")
    args = [now, value, note, now, state, alert_id]
    if state is not None:
        sql += " AND COALESCE(state, '') = ?"
        args.append(was or "")
    sql, args = _own(sql, args, user)
    fired = conn.execute(sql, args).rowcount == 1
    conn.commit()
    return fired


def alert_checked(conn: sqlite3.Connection, alert_id: int,
                  state: Optional[str] = None) -> None:
    """A check that found nothing to say. Clears any earlier error."""
    if state is None:
        conn.execute("UPDATE alerts SET last_checked=?, last_error=NULL, "
                     "last_error_ts=NULL WHERE id=?", (int(time.time()), alert_id))
    else:
        conn.execute("UPDATE alerts SET last_checked=?, last_error=NULL, "
                     "last_error_ts=NULL, state=? WHERE id=?",
                     (int(time.time()), state, alert_id))
    conn.commit()


def alert_failed(conn: sqlite3.Connection, alert_id: int, error: str) -> None:
    """A check that could not be done, kept so the page can say why."""
    conn.execute("UPDATE alerts SET last_checked=?, last_error=?, last_error_ts=? "
                 "WHERE id=?", (int(time.time()), error[:200], int(time.time()),
                                alert_id))
    conn.commit()


def alert_set_active(conn: sqlite3.Connection, alert_id: int, active: bool,
                     user: Optional[int] = None) -> None:
    """Pause or resume an alert without losing what it last saw."""
    sql, args = _own("UPDATE alerts SET active=? WHERE id=?",
                     [1 if active else 0, alert_id], user)
    if conn.execute(sql, args).rowcount == 0:
        raise ValueError("No alert with id %d" % alert_id)
    conn.commit()


def alert_edit(conn: sqlite3.Connection, alert_id: int, threshold: Optional[float],
               note: str, user: Optional[int] = None) -> None:
    """Change an alert's value and note. Its condition stays what it was.

    A new value is a new question, so the alert forgets whether the old one
    was met: if the new one already is, the next check says so.
    """
    from .alerts import KINDS, problem
    sql, args = _own("SELECT kind FROM alerts WHERE id=?", [alert_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("No alert with id %d" % alert_id)
    spec = KINDS.get(row["kind"], {})
    why = problem(row["kind"], threshold)
    if why:
        raise ValueError(why)
    sql, args = _own("UPDATE alerts SET threshold=?, note=?, state=CASE WHEN "
                     "? THEN state ELSE NULL END WHERE id=?",
                     [threshold if spec.get("needs_threshold") else None,
                      (note or "")[:MAX_NOTE], 1 if spec.get("call") else 0,
                      alert_id], user)
    conn.execute(sql, args)
    conn.commit()


# ---------------------------------------------------------------------------
# What the background checks have told people
#
# Every fired alert and every crossed stop is written here, with the time of
# the price it was based on and whether an email went out. Kept per person;
# the page shows the latest.
# ---------------------------------------------------------------------------

_EVENTS = """
CREATE TABLE IF NOT EXISTS alert_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    message     TEXT NOT NULL,
    value       REAL,
    as_of       TEXT NOT NULL DEFAULT '',
    alert_id    INTEGER,
    trade_id    INTEGER,
    emailed     INTEGER,
    email_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_events_user ON alert_events(user_id, ts);
"""


def event_add(conn: sqlite3.Connection, user: int, kind: str, symbol: str,
              message: str, value: Optional[float] = None, as_of: str = "",
              alert_id: Optional[int] = None,
              trade_id: Optional[int] = None) -> int:
    conn.executescript(_EVENTS)
    cur = conn.execute(
        "INSERT INTO alert_events (user_id, ts, kind, symbol, message, value, "
        "as_of, alert_id, trade_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(user), int(time.time()), kind, symbol, message[:500], value, as_of,
         alert_id, trade_id))
    conn.commit()
    return int(cur.lastrowid)


def event_list(conn: sqlite3.Connection, user: int, limit: int = 20) -> List[sqlite3.Row]:
    conn.executescript(_EVENTS)
    return list(conn.execute("SELECT * FROM alert_events WHERE user_id=? "
                             "ORDER BY ts DESC, id DESC LIMIT ?", (int(user), limit)))


def events_after(conn: sqlite3.Connection, user: int, after: Optional[int] = None,
                 since: Optional[float] = None, limit: int = 20) -> List[sqlite3.Row]:
    """One person's events after a point, newest first, for a page to announce.

    after is the id of the last event a page has seen. Ids only grow, so it
    is a better place-marker than a time, which two events can share. since
    is a time, to the second, for a page that has no marker yet.
    """
    conn.executescript(_EVENTS)
    sql, args = "SELECT * FROM alert_events WHERE user_id=?", [int(user)]
    if after is not None:
        sql += " AND id > ?"
        args.append(int(after))
    if since is not None:
        sql += " AND ts >= ?"
        args.append(int(since))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args))


def event_cursor(conn: sqlite3.Connection, user: int) -> int:
    """The id of one person's newest event, or 0 when there is none."""
    conn.executescript(_EVENTS)
    row = conn.execute("SELECT MAX(id) FROM alert_events WHERE user_id=?",
                       (int(user),)).fetchone()
    return int(row[0] or 0)


def event_emailed(conn: sqlite3.Connection, event_ids: List[int],
                  error: str = "") -> None:
    """Record whether the email for these events went out."""
    conn.executescript(_EVENTS)
    for event_id in event_ids:
        conn.execute("UPDATE alert_events SET emailed=?, email_error=? WHERE id=?",
                     (None if error else int(time.time()), error[:200] or None,
                      event_id))
    conn.commit()


def alert_reset(conn: sqlite3.Connection, alert_id: int,
                user: Optional[int] = None) -> None:
    """Re-arm a fired alert, as if it had just been set.

    A call alert keeps the call it last saw, so the next change from that
    call fires. Any other forgets that its condition was met, so if it still
    is, the next check says so again.
    """
    from .alerts import CALL_KINDS
    sql, args = _own("UPDATE alerts SET active=1, triggered_ts=NULL, "
                     "trigger_value=NULL, trigger_note=NULL, state=CASE WHEN kind IN "
                     "(%s) THEN state ELSE NULL END WHERE id=?"
                     % ",".join("?" * len(CALL_KINDS)),
                     list(CALL_KINDS) + [alert_id], user)
    if conn.execute(sql, args).rowcount == 0:
        raise ValueError("No alert with id %d" % alert_id)
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
