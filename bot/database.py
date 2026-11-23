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
import logging
import os
import sqlite3
import threading
import time
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

-- Parts of a position closed while the rest stays open, in the trade's own
-- shares. The trades row keeps the size it was opened with. A trade closed in
-- one go has no row here: its exit is on the trade, as it always was. One
-- closed in parts has a row for every part, the last included, and its
-- trades row carries the totals, so a version that knows nothing of this
-- table reads it as one trade closed at the average price. Deleting a trade
-- takes its parts with it, in that version too.
CREATE TABLE IF NOT EXISTS trade_exits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id  INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    ts        INTEGER NOT NULL,
    quantity  REAL NOT NULL,
    price     REAL NOT NULL,
    reason    TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_trade_exits_trade ON trade_exits(trade_id, ts);

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

-- Each watched instrument's daily call and price, one row per trading session
-- (day: the exchange's own day number, as calls.price_day), so "since
-- yesterday" has the session before to compare with. Written only by the
-- background job, and shared by everyone watching the instrument: symbols,
-- calls and prices, nobody's data. action is empty when only the price was read.
CREATE TABLE IF NOT EXISTS watch_calls (
    symbol      TEXT NOT NULL,
    day         INTEGER NOT NULL,
    action      TEXT,
    price       REAL NOT NULL,
    prev_close  REAL,
    at          INTEGER NOT NULL,
    PRIMARY KEY (symbol, day)
);

-- Conditions to watch for. Checked on demand or by a scheduled run.
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- one of bot.alerts.KINDS
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
    # In full, so a bare file name (--db journal.db) is one in this folder.
    path = os.path.abspath(path or default_path())
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
    from . import apple_store as apple_store_mod
    from . import public as public_mod
    _allowing_race(conn, accounts_mod.ensure_schema)
    # The public pages add columns of their own, and doing it here means they
    # exist before the first request rather than being added by several at once.
    _allowing_race(conn, public_mod.ensure_schema)
    # Pro bought in the iPhone app: its tables, and each subscriber's Pro
    # worked out again under the current settings. After accounts, whose
    # users.apple_until it fills in.
    _allowing_race(conn, apple_store_mod.prepare)
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
        # split_ts: the time up to which every share split is reflected in a
        # price alert's level: the latest split it was moved for, or when its
        # level was last typed. So a split is applied once, and never to a
        # level typed after it.
        # exact: 1 when the symbol is Yahoo's own and means exactly that,
        # where a bare ticker would be read as something else: LINK saved
        # for Interlink Electronics, which a bare LINK, the Chainlink coin,
        # is not. An entry saved before is 0 and keeps the meaning it had.
        "alerts": [("state", "TEXT"), ("last_error", "TEXT"),
                   ("last_error_ts", "INTEGER"), ("split_ts", "INTEGER"),
                   ("exact", "INTEGER NOT NULL DEFAULT 0")],
        # When the background check last told the owner this position had
        # crossed its stop, so it is said once, not every five minutes.
        # trail_*: a trailing stop, as a share of the best price since opening
        # (trail_pct, 0.05 for 5%) or a distance in the trade's own shares
        # (trail_dist, from trail_atr times the ATR when it was set); when it
        # was set; and the best price seen since opening, with when, so every
        # reader trails from it whatever its bars reach back to. The stop
        # column keeps the stop the trade was opened with: R is measured from
        # it, and a version that knows nothing of trails reads it unchanged.
        # trail_hit_*: the first crossing of the stop, trailing or not, any
        # reader saw (when, the price, the stop in force then), so it stays a
        # crossing for every reader once that day's bar is finished, and once
        # it is older than the trade's bars reach back.
        # exit_split, and split on each part in trade_exits: the shares that
        # day per share the trade was opened with, when the exit was written
        # (10.0 after a ten-for-one in between), so the exits export can give
        # each sale in the shares a broker lists. Empty where it was not known.
        "trades": [("breach_noticed", "INTEGER"), ("trail_pct", "REAL"),
                   ("trail_dist", "REAL"), ("trail_atr", "REAL"),
                   ("trail_ts", "INTEGER"), ("trail_best", "REAL"),
                   ("trail_best_ts", "INTEGER"), ("trail_hit_ts", "INTEGER"),
                   ("trail_hit_price", "REAL"), ("trail_hit_stop", "REAL"),
                   ("exit_split", "REAL"),
                   # The journal's tags (bot/journal.py): which setup (a key of
                   # setups.JOURNAL_SETUPS), how the person felt (setups.EMOTIONS),
                   # the checklist ticked (a JSON list of setups.CHECKLIST keys),
                   # the daily rules it was logged against (comma-separated keys:
                   # loss, trades, time), and the review after it closed: whether
                   # the plan was followed (setups.FOLLOWED), the lesson, and when.
                   # Empty on every trade from before, which then counts as "not
                   # tagged" and nothing else about it changes.
                   ("setup", "TEXT"), ("emotion", "TEXT"), ("followed_plan", "TEXT"),
                   ("checklist", "TEXT"), ("lesson", "TEXT"), ("broke_rule", "TEXT"),
                   ("reviewed_ts", "INTEGER"),
                   # The random name of the log-a-trade form it came from, so a
                   # form sent twice is logged once. Empty for any other trade.
                   ("form_id", "TEXT")],
        "trade_exits": [("split", "REAL")],
        "watchlist": [("exact", "INTEGER NOT NULL DEFAULT 0")],
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
        _move_coin_alerts(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# Coin ids that alerts were saved under before market.normalise_symbol knew
# Yahoo's own (November 2026), and the id each coin has there. ARB-USD and
# XBT-USD are other coins at Yahoo (ARbit and XBIT); the rest hold no prices,
# so alerts on them could never be checked. A fixed list rather than one read
# from market.py, so this one-off change is a record of exactly what it does;
# test_market.py checks that the two agree.
COIN_IDS_MOVED = {
    "ARB-USD": "ARB11841-USD", "XBT-USD": "BTC-USD", "SUI-USD": "SUI20947-USD",
    "UNI-USD": "UNI7083-USD", "APT-USD": "APT21794-USD",
    "PEPE-USD": "PEPE24478-USD", "MATIC-USD": "POL28321-USD",
    "TON-USD": "GRAM-USD",
}


def _move_coin_alerts(conn: sqlite3.Connection) -> List[tuple]:
    """Point alerts saved under an old coin id at the coin's id. Idempotent.

    Each alert moved is paused and its remembered state cleared, and the
    alerts page says why: its level may have been set while the site showed
    another coin's price, so its owner checks it and resumes it. Nothing else
    about it changes, every move is logged, and once moved there is nothing
    left for a second run to find. Returns (id, old, new) for each move.

    Only alerts saved by ticker (exact=0) are moved. One saved exactly, such
    as ARB-USD picked from the suggestions as Yahoo's ARbit, means that very
    coin; this runs at every start, and moved it, paused it and told its
    owner something false each time.
    """
    moved = []
    marks = ",".join("?" * len(COIN_IDS_MOVED))
    for row in conn.execute("SELECT id, symbol FROM alerts WHERE exact=0 AND "
                            "symbol IN (%s)" % marks, list(COIN_IDS_MOVED)).fetchall():
        old, new = row[1], COIN_IDS_MOVED[row[1]]
        why = ("Moved from %s, which at Yahoo is another coin or no coin, to %s, "
               "Yahoo's id for the coin you named. Paused so you can check its "
               "level, then resume it." % (old, new))
        conn.execute("UPDATE alerts SET symbol=?, active=0, state=NULL, "
                     "last_error=?, last_error_ts=? WHERE id=? AND symbol=? AND exact=0",
                     (new, why, int(time.time()), row[0], old))
        logging.getLogger(__name__).warning(
            "alert %d moved from %s to %s and paused", row[0], old, new)
        moved.append((row[0], old, new))
    return moved


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
    """Store one analysis run. Returns the run id.

    The strategies' scores and the headlines behind it are not kept. They were
    the bulk of every run, up to sixty-odd rows of it, and nothing read them
    back: the disk filled with them and with other people's headlines. Rows
    stored by earlier versions are left as they are.
    """
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
    conn.commit()
    return run_id


def save_strategy_stats(conn: sqlite3.Connection, symbol: str, interval: str,
                        report) -> int:
    """Snapshot measured per-strategy performance.

    Rows are replaced rather than summed. Two runs over overlapping history
    describe the same bars, and adding them together would invent evidence.
    The instrument's whole record is replaced, so a strategy that was renamed
    or has stopped trading in one market state does not keep its old figures.
    A measurement with nothing in it replaces nothing.
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
    if not rows:
        return 0
    conn.execute("DELETE FROM strategy_stats WHERE symbol=? AND interval=?",
                 (symbol, interval))
    conn.executemany(
        """INSERT OR REPLACE INTO strategy_stats
           (symbol, interval, strategy, family, regime, n, wins, sum_r,
            sum_r_gross, sum_bars, cost_r, updated_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# The trade journal
# ---------------------------------------------------------------------------

# The most positions one account's journal may hold open at once. The background
# check reads the price of every open position's instrument each round, so there
# has to be a ceiling somewhere. The web layer asks for it; the command line,
# which is one person on their own machine, does not.
MAX_OPEN_TRADES = 200


def check_trail(pct: Optional[float], distance: Optional[float],
                atr: Optional[float]) -> None:
    """Refuse a trail that cannot work. Raises ValueError in words."""
    import math
    if pct is not None and distance is not None:
        raise ValueError("a trail is a percentage or a distance, not both")
    for label, value in (("trail", pct), ("trail", distance), ("ATR multiple", atr)):
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("the %s must be a real number" % label)
    if pct is not None and not 0 < float(pct) < 1:
        raise ValueError("a trail must be more than 0% and less than 100% of the price")
    if distance is not None and float(distance) <= 0:
        raise ValueError("a trail must be more than zero")
    if atr is not None and not 0 < float(atr) <= 50:
        raise ValueError("an ATR trail must be more than 0 and at most 50 ATR")


def open_trade(conn: sqlite3.Connection, symbol: str, interval: str,
               direction: int, entry: float, stop: float,
               target1: Optional[float] = None, target2: Optional[float] = None,
               quantity: Optional[float] = None, risk_amount: Optional[float] = None,
               run_id: Optional[int] = None, account: str = "paper",
               strategy_note: str = "", notes: str = "", user: int = 1,
               trail_pct: Optional[float] = None, trail_dist: Optional[float] = None,
               trail_atr: Optional[float] = None, setup: Optional[str] = None,
               emotion: Optional[str] = None, checklist=None,
               broke_rule: Optional[str] = None, opened_ts: Optional[int] = None,
               cap: Optional[int] = None, form_id: Optional[str] = None) -> int:
    """Record a newly opened position, with a trailing stop if one is given.

    trail_pct is a share of the best price (0.05 for 5%); trail_dist is a
    distance in price, with trail_atr the ATR multiple it was worked out from.

    setup, emotion and checklist are the journal's tags (keys from bot/setups'
    lists; checklist an iterable of the keys ticked, stored as a JSON list,
    None when the checklist was not used). broke_rule is the comma-separated
    daily rules it was logged against. opened_ts is when the person entered,
    when that was earlier than now: at most 30 days back and never in the
    future. Each raises ValueError in words when it is not right.

    cap is the most positions this account may hold open, None for no limit.
    form_id names the form it was typed into (already_logged).
    """
    import math
    from .journal import check_opened, check_tags
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
    # A target on the losing side reads as reached the moment it is logged.
    for label, value in (("target", target1), ("second target", target2)):
        if value is None:
            continue
        if float(value) <= 0:
            raise ValueError("the %s must be a positive price, or left empty" % label)
        if (float(value) - entry) * direction <= 0:
            raise ValueError("for a %s, the %s must sit %s the entry" % (
                ("long", label, "above") if direction > 0 else ("short", label, "below")))
    sym = _symbol(symbol)
    check_trail(trail_pct, trail_dist, trail_atr)
    trails = trail_pct is not None or trail_dist is not None
    ticked = None if checklist is None else sorted(set(checklist))
    check_tags(setup=setup, emotion=emotion, checklist=ticked)
    if broke_rule and not all(k in ("loss", "trades", "time") for k in broke_rule.split(",")):
        raise ValueError("unknown daily rule %r" % broke_rule)

    now = int(time.time())
    opened = check_opened(opened_ts, now)
    opened = now if opened is None else opened
    if cap is not None:
        held = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open' AND user_id=?",
                            (int(user),)).fetchone()[0]
        if held >= cap:
            raise ValueError("A journal can hold %d open positions at once, and yours has "
                             "%d. Close or delete one first." % (cap, held))
    cur = conn.execute(
        """INSERT INTO trades (run_id, account, symbol, interval, direction,
               status, ts_opened, entry, stop, target1, target2, quantity,
               risk_amount, strategy_note, notes, user_id, trail_pct, trail_dist,
               trail_atr, trail_ts, trail_best, trail_best_ts, setup, emotion,
               checklist, broke_rule, form_id)
           VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, account, sym, interval, direction, opened,
         float(entry), float(stop), target1, target2, quantity, risk_amount,
         strategy_note, notes, int(user), trail_pct, trail_dist,
         trail_atr if trail_dist is not None else None,
         now if trails else None, float(entry) if trails else None,
         now if trails else None, setup or None, emotion or None,
         None if ticked is None else json.dumps(ticked), broke_rule or None,
         form_id or None))
    conn.commit()
    return int(cur.lastrowid)


def already_logged(conn: sqlite3.Connection, form_id: Optional[str], symbol: str,
                   direction: int, entry: float, stop: float, target1: Optional[float],
                   quantity: Optional[float], opened_ts: Optional[int], user: int) -> bool:
    """Whether this very form, with these very numbers, was logged already.

    The numbers as well as the form: someone who goes back to a form they
    sent and changes it is logging another trade.
    """
    if not form_id:
        return False
    return conn.execute(
        "SELECT 1 FROM trades WHERE user_id=? AND form_id=? AND symbol=? AND direction=? "
        "AND entry=? AND stop=? AND target1 IS ? AND quantity IS ? "
        "AND (? IS NULL OR ts_opened=?) LIMIT 1",
        (int(user), form_id, _symbol(symbol), direction, float(entry), float(stop),
         target1, quantity, opened_ts, opened_ts)).fetchone() is not None


class TradeChanged(ValueError):
    """The trade is not as the page that asked to close it showed it.

    Most often a form sent twice: the first press already closed what the
    second asked for, and doing it again would close more than was meant.
    """


# What is left of a position below this share of it is nothing: coins are
# sized to six decimals, and adding parts up in floating point leaves dust.
_DUST = 1e-9


def _qty(value: float) -> str:
    return ("%.6f" % value).rstrip("0").rstrip(".")


def _close_in_one(conn: sqlite3.Connection, row, trade_id: int, price: float,
                  exit_reason: str, now: int, split: Optional[float] = None) -> Dict:
    """The whole position at one price, written as it always was."""
    direction = int(row["direction"])
    entry, stop = float(row["entry"]), float(row["stop"])
    risk_per_unit = abs(entry - stop)
    move = (price - entry) * direction
    r_multiple = (move / risk_per_unit) if risk_per_unit > 0 else 0.0
    pnl = move * float(row["quantity"] or 0.0)
    conn.execute("UPDATE trades SET status='closed', ts_closed=?, exit_price=?, "
                 "exit_reason=?, r_multiple=?, pnl=?, exit_split=? WHERE id=?",
                 (now, price, exit_reason, r_multiple, pnl, split, trade_id))
    return {"r_multiple": r_multiple, "pnl": pnl}


def exit_trade(conn: sqlite3.Connection, trade_id: int, exit_price: float,
               quantity: Optional[float] = None, exit_reason: str = "manual",
               notes: str = "", user: Optional[int] = None,
               expect_left: Optional[float] = None,
               expect_open: bool = False, split: Optional[float] = None) -> Dict:
    """Close a position, or part of it, at a price in the trade's own shares.

    quantity None closes whatever is still open. A part is written to
    trade_exits and the rest stays open; the trade is closed when nothing is
    left. Closed in parts, the trade's row then holds the totals: the average
    exit price, the R of the whole position (each part's R weighted by its
    size) and the cash. A trade logged without a size can only be closed
    whole, since a part of it means nothing.

    expect_left is how much the caller last saw still open, and expect_open
    that it saw it open: when either no longer holds (a form sent twice),
    nothing is written and TradeChanged says so. The look and the write are
    one transaction, so two exits at once cannot close more than there is.

    split, when known, is the shares that day per share the trade was
    opened with (10.0 after a ten-for-one since). It changes nothing here,
    and is kept with the exit so the export can give it in that day's shares.

    Returns what this exit did: quantity and exit, what is left, closed
    (whether the trade is now closed), parts (how many it has been closed
    in, 0 for one go), average (its average exit once closed), and
    r_multiple and pnl: this part's, or the whole position's once closed.
    """
    import math
    price = float(exit_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("the exit price must be a positive number")
    if quantity is not None:
        quantity = float(quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("the size to close must be more than zero, or left "
                             "empty to close all of it")
    if split is not None:
        split = float(split)
        if not math.isfinite(split) or split <= 0:
            split = None
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        sql, args = _own("SELECT * FROM trades WHERE id=?", [trade_id], user)
        row = conn.execute(sql, args).fetchone()
        if row is None:
            raise ValueError("no trade with id %d" % trade_id)
        if row["status"] != "open":
            if expect_open or expect_left is not None:
                raise TradeChanged("That %s trade was already closed, so nothing more "
                                   "was done." % row["symbol"])
            raise ValueError("trade %d is already %s" % (trade_id, row["status"]))
        earlier = exits_for(conn, [trade_id]).get(trade_id, [])
        size = float(row["quantity"]) if row["quantity"] is not None else None
        left = (size - sum(float(e["quantity"]) for e in earlier)) if size else None
        dust = _DUST * max(1.0, size or 0.0)
        if expect_left is not None and left is not None and abs(left - expect_left) > dust:
            raise TradeChanged("That %s trade changed since the page was drawn: %s of it "
                               "is open now, not %s. Nothing more was closed."
                               % (row["symbol"], _qty(left), _qty(expect_left)))
        if quantity is not None and left is None:
            raise ValueError("This trade was logged without a size, so it can only be "
                             "closed whole. Leave the size empty.")
        if quantity is not None and quantity > left + dust:
            raise ValueError("Only %s of it is still open." % _qty(left))

        now = int(time.time())
        whole = quantity is None or quantity >= left - dust
        direction, entry = int(row["direction"]), float(row["entry"])
        risk = abs(entry - float(row["stop"]))
        if not whole:
            conn.execute("INSERT INTO trade_exits (trade_id, ts, quantity, price, reason, "
                         "split) VALUES (?,?,?,?,?,?)",
                         (trade_id, now, quantity, price, exit_reason, split))
            move = (price - entry) * direction
            out = {"r_multiple": (move / risk) if risk > 0 else 0.0,
                   "pnl": move * quantity, "parts": len(earlier) + 1, "average": None}
        elif not earlier:
            out = dict(_close_in_one(conn, row, trade_id, price, exit_reason, now, split),
                       parts=0, average=price)
        else:
            # The last part: written like the others, then the whole
            # position's totals on the trade itself.
            conn.execute("INSERT INTO trade_exits (trade_id, ts, quantity, price, reason, "
                         "split) VALUES (?,?,?,?,?,?)",
                         (trade_id, now, left, price, exit_reason, split))
            parts = trade_parts(dict(row, status="closed", ts_closed=now),
                                exits_for(conn, [trade_id]).get(trade_id, []))
            conn.execute("UPDATE trades SET status='closed', ts_closed=?, exit_price=?, "
                         "exit_reason=?, r_multiple=?, pnl=? WHERE id=?",
                         (now, parts["avg_exit"], exit_reason, parts["r"], parts["pnl"],
                          trade_id))
            out = {"r_multiple": parts["r"], "pnl": parts["pnl"],
                   "parts": parts["parts"], "average": parts["avg_exit"]}
        if notes:
            conn.execute("UPDATE trades SET notes = COALESCE(notes,'') || ' ' || ? "
                         "WHERE id=?", (notes, trade_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    closed_now = left if (whole and left is not None) else quantity
    return dict(out, id=trade_id, symbol=row["symbol"], direction=direction,
                entry=entry, exit=price, closed=whole, quantity=closed_now,
                left=0.0 if whole else (left - quantity if left is not None else None))


def close_trade(conn: sqlite3.Connection, trade_id: int, exit_price: float,
                exit_reason: str = "manual", notes: str = "",
                user: Optional[int] = None) -> Dict:
    """Close all that is still open of a position, and compute its realised R and cash.

    For a trade closed in parts, r_multiple and pnl are the whole position's.
    """
    return exit_trade(conn, trade_id, exit_price, None, exit_reason, notes, user)


def exits_for(conn: sqlite3.Connection, trade_ids) -> Dict[int, List[sqlite3.Row]]:
    """Each trade's parts closed so far, oldest first, keyed by trade id.

    A file from before partial exits, read without being brought up to date
    (a backup's exports), has no such table, and so no parts.
    """
    ids = sorted({int(i) for i in trade_ids})
    out: Dict[int, List[sqlite3.Row]] = {}
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute("SELECT * FROM trade_exits WHERE trade_id IN (%s) "
                                    "ORDER BY ts, id" % ",".join("?" * len(chunk)), chunk):
                out.setdefault(int(row["trade_id"]), []).append(row)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
    return out


def _column(row, key):
    """A row's value for a column, or None for a row without that column.

    A copy read without being brought up to date (a backup's exports) lacks
    the columns added since it was made.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def trade_parts(row, exits) -> Dict:
    """What a trade's exits add up to, in its own shares.

    exits are its trade_exits rows. A trade closed in one go has none: its
    exit, R and cash are on the row, as stored. One closed in parts has a row
    for each; if a version that knew nothing of parts closed what they left,
    that last part went at the row's exit price, and is counted so.

    size is what it was opened with and left what is still open (None for a
    trade logged without a size). r is the R of what is closed so far,
    counted against the whole position: each part's R times its share of
    it, so a closed trade's r is the R of the whole trade. pnl is the cash
    from what is closed, before costs. Each exit's split is the shares that
    day per share opened with, as kept when it was written, or None.
    """
    direction, entry = int(row["direction"]), float(row["entry"])
    risk = abs(entry - float(row["stop"]))
    size = float(row["quantity"]) if row["quantity"] is not None else None

    def part(when, qty, price, reason, partial=True, split=None):
        move = (float(price) - entry) * direction
        return {"ts": when, "quantity": qty, "price": float(price), "reason": reason,
                "r": (move / risk) if risk > 0 else 0.0,
                "pnl": move * (qty or 0.0), "partial": partial,
                "split": float(split) if split else None}

    out = [part(e["ts"], float(e["quantity"]), e["price"], e["reason"],
                split=_column(e, "split")) for e in exits]
    done = sum(p["quantity"] for p in out)
    closed = row["status"] == "closed"
    if closed and not exits and row["exit_price"] is not None:
        whole = part(row["ts_closed"], size, row["exit_price"], row["exit_reason"], False,
                     _column(row, "exit_split"))
        whole["r"] = float(row["r_multiple"] or 0.0)
        whole["pnl"] = float(row["pnl"] or 0.0)
        out.append(whole)
    elif (closed and size is not None and row["exit_price"] is not None
          and size - done > _DUST * max(1.0, size)):
        out.append(part(row["ts_closed"], size - done, row["exit_price"],
                        row["exit_reason"]))
    shut = sum(p["quantity"] or 0.0 for p in out)
    if not exits:
        # In one go: the trade's own figures, exactly as stored.
        r = out[0]["r"] if out else None
        average = out[0]["price"] if out else None
    else:
        r = sum(p["r"] * p["quantity"] for p in out) / size if size else None
        average = sum(p["price"] * p["quantity"] for p in out) / shut if shut else None
    return {"size": size, "parts": len(exits), "exits": out,
            "closed": shut if size is not None else None,
            "left": 0.0 if closed else ((size - done) if size is not None else None),
            "avg_exit": average, "r": r, "pnl": sum(p["pnl"] for p in out)}


def open_positions(conn: sqlite3.Connection, account: Optional[str] = None,
                   limit: Optional[int] = 100, user: Optional[int] = None) -> List[Dict]:
    """Open trades as they are held now: quantity is what is still open.

    The portfolio and the routines size, value and risk what is held, so a
    position closed in part counts only the part left. quantity_opened keeps
    the size it was opened with.
    """
    rows = list_trades(conn, status="open", account=account, limit=limit, user=user)
    exits = exits_for(conn, [r["id"] for r in rows])
    out = []
    for row in rows:
        held = dict(row)
        held["quantity_opened"] = row["quantity"]
        if exits.get(row["id"]):
            held["quantity"] = trade_parts(row, exits[row["id"]])["left"]
        out.append(held)
    return out


def set_trail(conn: sqlite3.Connection, trade_id: int, pct: Optional[float] = None,
              distance: Optional[float] = None, atr: Optional[float] = None,
              best: Optional[float] = None, best_at: Optional[int] = None,
              user: Optional[int] = None) -> None:
    """Give an open trade a trailing stop, from now on.

    Whether the new trail loosens the stop is a question for the prices
    (portfolio.trail_problem), asked by the caller first. best is the best
    price since opening the caller read, kept if better than the one held.
    """
    check_trail(pct, distance, atr)
    if pct is None and distance is None:
        raise ValueError("No trail given.")
    sql, args = _own("UPDATE trades SET trail_pct=?, trail_dist=?, trail_atr=?, trail_ts=? "
                     "WHERE id=? AND status='open'",
                     [pct, distance, atr if distance is not None else None,
                      int(time.time()), trade_id], user)
    if conn.execute(sql, args).rowcount == 0:
        conn.rollback()
        raise ValueError("No open trade with id %d." % trade_id)
    conn.commit()
    if best is not None and best_at is not None:
        trail_seen(conn, trade_id, best, best_at)


def trail_seen(conn: sqlite3.Connection, trade_id: int, best: float,
               best_at: int) -> bool:
    """Keep the best price a trailing stop has seen, when it beats the one kept.

    Every reader trails from the best since opening, and their bars reach
    back different distances: the background check's a few days of
    five-minute bars, the journal's sixty. Kept here, a high seen by any of
    them holds the stop up for all of them. Only ever improves, so readers
    racing each other cannot pull it back. True when this call moved it.
    """
    cur = conn.execute(
        "UPDATE trades SET trail_best=?, trail_best_ts=? WHERE id=? AND status='open' "
        "AND (trail_pct IS NOT NULL OR trail_dist IS NOT NULL) AND (trail_best IS NULL "
        "OR (direction > 0 AND trail_best < ?) OR (direction < 0 AND trail_best > ?))",
        (float(best), int(best_at), trade_id, float(best), float(best)))
    conn.commit()
    return cur.rowcount == 1


def stop_crossed(conn: sqlite3.Connection, trade_id: int, when: int, price: float,
                 stop: float) -> bool:
    """Keep the first crossing of a trade's stop any reader saw, trailing or not.

    A finished bar is held to the stop in force when it started, because its
    high and low do not say which came first. So a crossing seen live, after
    that day's own high had trailed the stop up, is not in the finished
    bar's reading. And any crossing drops out of a reader's bars in time:
    five-minute bars reach back sixty days, one-minute bars seven. Kept
    here, it stays a crossing for every reader. Only the first is kept, in
    the trail_hit_ columns, which every stop uses. Prices are in the trade's
    own shares. True when this call kept it.
    """
    cur = conn.execute(
        "UPDATE trades SET trail_hit_ts=?, trail_hit_price=?, trail_hit_stop=? "
        "WHERE id=? AND status='open' AND trail_hit_ts IS NULL",
        (int(when), float(price), float(stop), trade_id))
    conn.commit()
    return cur.rowcount == 1


def delete_trade(conn: sqlite3.Connection, trade_id: int,
                 user: Optional[int] = None) -> Dict:
    """Remove a trade entirely, with any parts of it already closed.

    Needed because a mistyped entry otherwise corrupts every statistic the
    journal produces, with no way to take it back. Returns what was deleted so
    the caller can confirm it to the user.
    """
    sql, args = _own("SELECT * FROM trades WHERE id=?", [trade_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("no trade with id %d" % trade_id)
    # Parts first: they point at the trade.
    conn.execute("DELETE FROM trade_exits WHERE trade_id=?", (trade_id,))
    sql, args = _own("DELETE FROM trades WHERE id=?", [trade_id], user)
    conn.execute(sql, args)
    conn.commit()
    return {"id": trade_id, "symbol": row["symbol"], "status": row["status"],
            "direction": int(row["direction"]), "entry": float(row["entry"])}


def list_trades(conn: sqlite3.Connection, status: Optional[str] = None,
                account: Optional[str] = None, symbol: Optional[str] = None,
                limit: Optional[int] = 100, user: Optional[int] = None,
                by_closed: bool = False, offset: int = 0) -> List[sqlite3.Row]:
    """Trades, the latest opened first, or with by_closed the latest closed.

    limit None lists them all. offset leaves out that many from the start.
    """
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
    sql += (" ORDER BY ts_closed DESC, id DESC" if by_closed else " ORDER BY ts_opened DESC")
    sql += " LIMIT ? OFFSET ?"
    # SQLite reads a limit below zero as no limit.
    args.extend([-1 if limit is None else limit, offset])
    return list(conn.execute(sql, args))


# What Yahoo prices a coin in: the part of its symbol after the last dash, as
# in BTC-USD or ETH-EUR. A share's class or kind after a dash is never one of
# these (BRK-B, BAC-PL, REI-UN.TO).
_COIN_QUOTES = {"USD", "USDT", "USDC", "EUR", "GBP", "CAD", "AUD", "JPY", "KRW", "CNY",
                "INR", "BTC", "ETH"}


def _cost_kind(symbol: str) -> str:
    """Which cost an instrument pays, from its symbol: the analysis's own split.

    "futures" (=F), "other" (a currency pair =X, an index ^), "crypto" (a coin
    in any currency) or "equity". The journal keeps no asset class, only the
    symbol, and Yahoo's symbols say these plainly.
    """
    if symbol.endswith("=F"):
        return "futures"
    if symbol.endswith("=X") or symbol.startswith("^"):
        return "other"
    base, dash, quote = symbol.rpartition("-")
    return "crypto" if dash and base and quote in _COIN_QUOTES else "equity"


def _cost_of(row, costs: Dict[str, float]) -> float:
    """A trade's round-trip cost as a fraction of the price.

    A future, or a currency pair or an index, pays costs["futures"] or
    costs["other"] when the caller gives one, and the stock rate otherwise.
    """
    kind = _cost_kind(row["symbol"])
    if kind in ("futures", "other") and kind not in costs:
        kind = "equity"
    return float(costs.get(kind, 0.0))


# The longest lesson kept; a longer one is cut, not refused.
MAX_LESSON = 1000


def review_trade(conn: sqlite3.Connection, trade_id: int, followed_plan: Optional[str],
                 lesson: str, user: Optional[int] = None, setup: Optional[str] = None,
                 emotion: Optional[str] = None) -> None:
    """Keep the review of a closed trade: did the plan get followed, and the lesson.

    The setup and feeling can be set or corrected here too; one left None
    keeps what the trade has. Only a closed trade is reviewed. Raises
    ValueError in words.
    """
    from .journal import check_tags
    followed_plan = (followed_plan or "").strip() or None
    lesson = (lesson or "").strip()[:MAX_LESSON]
    check_tags(setup=setup or None, emotion=emotion or None, followed_plan=followed_plan)
    sql, args = _own("SELECT * FROM trades WHERE id=?", [trade_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("There is no such trade in your journal.")
    if row["status"] != "closed":
        raise ValueError("A trade is reviewed once it is closed.")
    # The form always sends the setup and the feeling, so they say something
    # only when changed. Emptying a review that had something in it is allowed.
    same = all(sent is None or (sent or "") == (row[key] or "")
               for key, sent in (("setup", setup), ("emotion", emotion)))
    emptied = (setup is not None or emotion is not None) and (
        row["followed_plan"] or row["lesson"])
    if followed_plan is None and not lesson and same and not emptied:
        raise ValueError("Say whether you followed your plan, or write what you learned.")
    sets, values = ["followed_plan=?", "lesson=?", "reviewed_ts=?"], [
        followed_plan, lesson or None, int(time.time())]
    if setup is not None:
        sets.append("setup=?")
        values.append(setup or None)
    if emotion is not None:
        sets.append("emotion=?")
        values.append(emotion or None)
    sql, args = _own("UPDATE trades SET %s WHERE id=?" % ", ".join(sets),
                     values + [trade_id], user)
    conn.execute(sql, args)
    conn.commit()


def _open_realised(conn: sqlite3.Connection, account: str, user: Optional[int],
                   costs: Dict[str, float]) -> Dict:
    """Cash taken from positions still open, by closing part of them, after costs."""
    sql, args = _own("SELECT * FROM trades WHERE status='open' AND account=?",
                     [account], user)
    rows = list(conn.execute(sql, args))
    exits = exits_for(conn, [row["id"] for row in rows])
    cash, count = 0.0, 0
    for row in rows:
        if not exits.get(row["id"]):
            continue
        parts = trade_parts(row, exits[row["id"]])
        cash += parts["pnl"] - float(row["entry"]) * parts["closed"] * _cost_of(row, costs)
        count += 1
    return {"open_realised": cash, "open_in_part": count}


def journal_summary(conn: sqlite3.Connection, account: str = "paper",
                    user: Optional[int] = None,
                    costs: Optional[Dict[str, float]] = None) -> Dict:
    """Realised performance of the journal so far.

    Expectancy is reported in R rather than cash because R is comparable across
    instruments and position sizes, which is what makes it the honest measure
    of whether a process works.

    costs maps "equity" and "crypto" (and "futures" and "other", where known)
    to a round-trip cost as a fraction of the price. Given them, every figure
    is after costs, which is what the account would really show; the before-cost expectancy is kept beside it.

    A trade counts once it is closed in full, at the R of the whole position:
    each part's R weighted by its share of it. The drawdown is walked exit by
    exit in the order they happened, each part when it was taken, which is
    the order the account really moved. Cash already taken from positions
    still open is kept apart (open_realised), since those trades are not
    finished and have no R yet.
    """
    sql, args = _own("SELECT * FROM trades WHERE status='closed' AND account=?",
                     [account], user)
    rows = list(conn.execute(sql + " ORDER BY ts_closed, id", args))
    n = len(rows)
    taken = _open_realised(conn, account, user, costs or {})
    if n == 0:
        return dict(taken, trades=0, open=count_open(conn, account, user))

    costs = costs or {}
    exits = exits_for(conn, [row["id"] for row in rows])
    gross, rs, pnl, steps = [], [], 0.0, []
    for order, row in enumerate(rows):
        parts = trade_parts(row, exits.get(row["id"], []))
        cost = _cost_of(row, costs)
        entry, size = float(row["entry"]), float(row["quantity"] or 0.0)
        risk = abs(entry - float(row["stop"]))
        cost_r = (entry * cost / risk) if risk > 0 else 0.0
        r = float(parts["r"] or 0.0)
        gross.append(r)
        rs.append(r - cost_r)
        pnl += parts["pnl"] - entry * size * cost
        for seq, part in enumerate(parts["exits"]):
            share = (part["quantity"] / size) if parts["parts"] and size > 0 else 1.0
            steps.append((int(part["ts"] or 0), order, seq,
                          share * (float(part["r"] or 0.0) - cost_r)))
    # A scratch (exactly even) is neither a win nor a loss.
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # Peak-to-trough of the cumulative R curve: the number that decides whether
    # a strategy is survivable, not just profitable.
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for _when, _order, _seq, r in sorted(steps):
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        **taken,
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


def _exact(symbol: str, exact: bool) -> int:
    """The exact column's value: 1 only where the bare ticker reads otherwise."""
    from .market import needs_exact
    return 1 if exact and needs_exact(symbol) else 0


def watch_add(conn: sqlite3.Connection, symbol: str, interval: str = "1d",
              note: str = "", user: int = 1, exact: bool = False) -> None:
    """Watch an instrument, or change the bar size and note of one watched.

    exact says the symbol is Yahoo's own, such as LINK for Interlink
    Electronics, where a bare LINK is the Chainlink coin. A person has one
    entry per symbol, so an entry saved before there was an exact column,
    which still means the coin, moves to the coin's own id rather than be
    replaced by the listing. If the coin is already watched under that id,
    the older entry is the same coin twice and goes.
    """
    sym, uid, now = _symbol(symbol), int(user), int(time.time())
    note = (note or "")[:MAX_NOTE]
    exact = _exact(sym, exact)
    if exact:
        from .market import normalise_symbol
        conn.execute("UPDATE OR IGNORE watchlist SET symbol=? WHERE user_id=? "
                     "AND symbol=? AND exact=0", (normalise_symbol(sym), uid, sym))
        conn.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=? AND exact=0",
                     (uid, sym))
    conn.execute(
        "INSERT OR REPLACE INTO watchlist "
        "(symbol, interval, added_ts, note, last_run, user_id, exact) VALUES "
        "(?,?,COALESCE((SELECT added_ts FROM watchlist WHERE symbol=? AND user_id=?),?),"
        "?,(SELECT last_run FROM watchlist WHERE symbol=? AND user_id=?),?,?)",
        (sym, interval, sym, uid, now, note, sym, uid, uid, exact))
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


def watch_call_save(conn: sqlite3.Connection, symbol: str, day: int,
                    action: Optional[str], price: float,
                    prev_close: Optional[float], at: int) -> None:
    """Keep the latest check of one watched instrument for its trading session.

    A later check in the same session replaces the price, and the call when it
    has one: a check that read only the price leaves the session's call alone.
    The session before is never touched, which is what "since yesterday"
    compares with.
    """
    conn.execute(
        "INSERT INTO watch_calls (symbol, day, action, price, prev_close, at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol, day) DO UPDATE SET "
        "action=COALESCE(excluded.action, watch_calls.action), "
        "price=excluded.price, prev_close=excluded.prev_close, at=excluded.at",
        (symbol.upper(), int(day), action or None, float(price),
         None if prev_close is None else float(prev_close), int(at)))
    conn.commit()


def watch_calls(conn: sqlite3.Connection, symbols,
                sessions: int = 2) -> Dict[str, List[sqlite3.Row]]:
    """The newest few sessions' rows of each instrument, newest first."""
    out: Dict[str, List[sqlite3.Row]] = {}
    symbols = sorted({s.upper() for s in symbols})
    if not symbols:
        return out
    for row in conn.execute("SELECT * FROM watch_calls WHERE symbol IN (%s) "
                            "ORDER BY symbol, day DESC" % ",".join("?" * len(symbols)),
                            symbols):
        kept = out.setdefault(row["symbol"], [])
        if len(kept) < sessions:
            kept.append(row)
    return out


def watch_calls_prune(conn: sqlite3.Connection, before: float) -> int:
    """Forget checks made before a time. Kept only to compare one session
    with the one before, so a fortnight is plenty."""
    cur = conn.execute("DELETE FROM watch_calls WHERE at < ?", (int(before),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

# The most alerts an account not on Pro can keep. Each is stored, and each
# fire, so there has to be a ceiling somewhere. Pro is promised as many as it
# likes on the pricing page, so the web layer passes no cap for it.
MAX_ALERTS = 200
_CEILING = object()


def alert_add(conn: sqlite3.Connection, symbol: str, kind: str,
              threshold: Optional[float] = None, interval: str = "1d",
              note: str = "", repeat: bool = False, user: int = 1,
              cap=_CEILING, exact: bool = False) -> int:
    """Create an alert. Validation lives here so every caller gets it.

    A percent threshold is a fraction: 0.05 is 5%. cap is the most alerts the
    account may hold: MAX_ALERTS unless given, None for no limit. exact says
    the symbol is Yahoo's own, as for watch_add.
    """
    if cap is _CEILING:
        cap = MAX_ALERTS
    from .alerts import DAILY_ONLY, KINDS, problem
    why = problem(kind, threshold)
    if why:
        raise ValueError(why)
    sym = _symbol(symbol)
    held = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id=?",
                        (int(user),)).fetchone()[0]
    if cap is not None and held >= cap:
        raise ValueError("An account not on Pro can keep %d alerts, and this one "
                         "has %d. Delete %d you no longer need first."
                         % (cap, held, held - cap + 1))
    if not KINDS[kind]["needs_threshold"]:
        threshold = None
    if kind in DAILY_ONLY:
        interval = "1d"

    cur = conn.execute(
        "INSERT INTO alerts (symbol, kind, threshold, interval, note, active, "
        "created_ts, repeat, user_id, exact) VALUES (?,?,?,?,?,1,?,?,?,?)",
        (sym, kind, threshold, interval or "1d", (note or "")[:MAX_NOTE],
         int(time.time()), 1 if repeat else 0, int(user), _exact(sym, exact)))
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


def alert_split(conn: sqlite3.Connection, alert_id: int, old: float, new: float,
                split_ts: int) -> bool:
    """Move a price alert's level for a share split. True if this call moved it.

    Only from the level read and only for a split newer than the last one
    applied, so two checks running at once cannot divide it twice.
    """
    cur = conn.execute("UPDATE alerts SET threshold=?, split_ts=? WHERE id=? "
                       "AND threshold=? AND (split_ts IS NULL OR split_ts < ?)",
                       (new, int(split_ts), alert_id, old, int(split_ts)))
    conn.commit()
    return cur.rowcount == 1


def alert_set_active(conn: sqlite3.Connection, alert_id: int, active: bool,
                     user: Optional[int] = None) -> None:
    """Pause or resume an alert without losing what it last saw.

    Resuming clears what the page kept from before the pause, a failed check
    or the note on an alert moved to a coin's own id: it asked to be resumed,
    and the next check says again if anything is wrong.
    """
    if active:
        sql, args = _own("UPDATE alerts SET active=1, last_error=NULL, "
                         "last_error_ts=NULL WHERE id=?", [alert_id], user)
    else:
        sql, args = _own("UPDATE alerts SET active=0 WHERE id=?", [alert_id], user)
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
    sql, args = _own("SELECT kind, threshold FROM alerts WHERE id=?", [alert_id], user)
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("No alert with id %d" % alert_id)
    spec = KINDS.get(row["kind"], {})
    why = problem(row["kind"], threshold)
    if why:
        raise ValueError(why)
    level = threshold if spec.get("needs_threshold") else None
    # A level typed now is in today's shares, so no split before now may be
    # applied to it: split_ts moves to now. A note alone leaves it, so a
    # split not yet applied to the old level still is.
    retyped = level is not None and (row["threshold"] is None
                                     or float(row["threshold"]) != float(level))
    sql, args = _own("UPDATE alerts SET threshold=?, note=?, state=CASE WHEN "
                     "? THEN state ELSE NULL END, split_ts=CASE WHEN ? THEN ? "
                     "ELSE split_ts END WHERE id=?",
                     [level, (note or "")[:MAX_NOTE], 1 if spec.get("call") else 0,
                      1 if retyped else 0, int(time.time()), alert_id], user)
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
                     "trigger_value=NULL, trigger_note=NULL, last_error=NULL, "
                     "last_error_ts=NULL, state=CASE WHEN kind IN "
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
