"""The example account a visitor can look around before signing up.

Someone arriving from a video will not hand an unknown app their Google
account just to find out what it does. So a visitor who is not signed in is
shown the real app -- the real pages, rendered by the same templates -- filled
with an example account's trades, watchlist, alert and saved theses. Anything
that would change something, or would make the server analyse an instrument
nobody has pre-computed, sends them to sign up instead.

The example account is an ordinary row in the users table, so every page
renders exactly as it does for a real person. It can never be signed in to:
it has no provider identity, and nothing ever starts a session for it. Only
GET requests are ever served as it, so a visitor can look but not write.

It is refilled once a week at the market's prices by the hourly background
job (refresh), never while a visitor waits. A visitor's request (user) only
ever fills an example that is empty, from prices kept here, and fetches
nothing; the next background run then replaces that with the market's.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from typing import Dict, List, NamedTuple, Optional

from . import accounts as accounts_mod
from . import database as db_mod

EMAIL = "example@orenth.invalid"      # .invalid can never be a real address
NAME = "Example account"

# The instruments a visitor may open an analysis or research page for. A fixed
# list, so a crawler walking through every ticker cannot make the server
# analyse every ticker: each of these is computed once and shared.
SYMBOLS = ("NVDA", "AAPL", "TSLA", "MSFT", "SPY", "BTC-USD", "ETH-USD")

# Used only to fill an example that is empty, so that a visitor who comes
# before the first background run does not find it bare.
_FALLBACK = {"NVDA": 180.0, "AAPL": 230.0, "TSLA": 330.0, "MSFT": 500.0,
             "BTC-USD": 100000.0}

_lock = threading.Lock()

# How old the example's positions may get before it is refilled at today's
# prices.
RESEED_AFTER = 7 * 86400

# The key in the meta table that holds when the example was last filled, all
# the way through, at the market's prices. Missing, it is refilled next time.
_FILLED = "demo_filled"

# How far back the example's trades may go. The journal takes an entry at most
# 30 days old.
_DAYS_BACK = 28

log = logging.getLogger(__name__)


class _Day(NamedTuple):
    """One finished daily bar, with a moment inside it and the moment it closed."""
    during: int
    closed: int
    open: float
    high: float
    low: float
    close: float


class _History(NamedTuple):
    price: float
    days: List[_Day]


def _history(symbol: str) -> Optional[_History]:
    """The last four weeks of the market's daily bars, or None without them."""
    try:
        from .market import fetch_bars
        bars = fetch_bars(symbol, "1d")
        since = time.time() - _DAYS_BACK * 86400
        # Bars before a split are already in today's shares, and a trade
        # entered at one would be adjusted for that split a second time.
        if any(when >= since for when, _ratio in bars.splits):
            return None
        # A coin's day lasts until midnight, a stock's session six and a half
        # hours. A minute less, so the close falls inside its own bar.
        length = 86400 if bars.is_crypto else int(6.5 * 3600)
        days = []
        for k in range(len(bars.close)):
            start = int(bars.timestamp[k])
            if since < start and start + length <= time.time() + 60:
                days.append(_Day(start + length // 2, start + length - 60,
                                 float(bars.open[k]), float(bars.high[k]),
                                 float(bars.low[k]), float(bars.close[k])))
        price = float(bars.last_price)
        numbers = [price] + [x for d in days for x in d[2:]]
        if len(days) < 10 or not all(math.isfinite(x) and x > 0 for x in numbers):
            return None
        return _History(price, days)
    except Exception as exc:
        log.warning("The example account could not read %s: %s", symbol, exc)
        return None


def _stand_in(symbol: str) -> _History:
    """A gentle wave round the price kept here, when there are no bars to use."""
    price, now, days = _FALLBACK[symbol], int(time.time()), []
    last = price
    for k in range(20, 0, -1):
        close = price * (1 + 0.06 * math.sin((20 - k) / 2.0))
        days.append(_Day(now - k * 86400 - 43200, now - k * 86400, last,
                         max(last, close) * 1.01, min(last, close) * 0.99, close))
        last = close
    return _History(last, days)


def _point(headline: str, detail: str) -> Dict:
    return {"headline": headline, "detail": detail, "evidence": []}


# A thesis here never states a fact about a real company: it is written once
# and shown for a week, and the next filing can say otherwise.
_BULL = [_point("Example: a point for buying",
                "A real thesis lists figures from the company's filings here, "
                "each with its source.")]
_BEAR = [_point("Example: a point against",
                "A real thesis lists what could go wrong here, from the same filings.")]
_FALSIFIERS = ["Example: the number that, if it fell, would mean the idea was wrong"]


def _size(entry: float, stop: float, crypto: bool = False) -> float:
    """The size the app's own rule gives: the settings' risk per trade,
    capped at the largest position the settings allow. Never nothing: one
    share at least, because a trade of no size cannot be logged."""
    from . import config as config_mod
    cfg = config_mod.load()
    account = float(cfg["account_size"])
    qty = account * float(cfg["risk_per_trade_pct"]) / 100.0 / abs(entry - stop)
    qty = min(qty, account * float(cfg["max_position_pct"]) / 100.0 / entry)
    return max(round(qty, 6), 0.000001) if crypto else float(max(1, int(qty)))


def _finished(days: List[_Day], outcome: str) -> Optional[Dict]:
    """A trade these days would have closed at its stop, or at its target.

    Entered at one day's close and followed through the next ones' highs and
    lows, so the picture of it sits on the bars it names. The widest plan that
    ended the way asked for, or None.
    """
    for wide in (0.06, 0.05, 0.04, 0.03, 0.02, 0.01):
        for i in range(len(days) - 1):
            entry = days[i].close
            stop, target = round(entry * (1 - wide), 2), round(entry * (1 + 1.5 * wide), 2)
            for day in days[i + 1:]:
                if day.low <= stop:
                    ended, price = "stop", min(stop, day.open)
                elif day.high >= target:
                    ended, price = "target", max(target, day.open)
                else:
                    continue
                if ended == outcome:
                    return {"entry": entry, "stop": stop, "target": target,
                            "opened": days[i].closed, "closed": day.during,
                            "exit": price, "reason": ended}
                break
    return None


def _left_early(days: List[_Day]) -> Optional[Dict]:
    """A trade closed by choice after four days, with neither price reached."""
    for i in range(len(days) - 6, -1, -1):
        entry, after = days[i].close, days[i + 1:i + 5]
        stop, target = round(entry * 0.92, 2), round(entry * 1.12, 2)
        if all(d.low > stop and d.high < target for d in after):
            return {"entry": entry, "stop": stop, "target": target,
                    "opened": days[i].closed, "closed": after[-1].closed,
                    "exit": after[-1].close, "reason": "manual"}
    return None


def _still_open(history: _History, stop_at: float, target_at: float) -> Dict:
    """A trade entered at a recent close that has reached neither price since,
    or at the last close when every recent one has."""
    days = history.days
    for i in range(len(days) - 5, len(days)):
        entry = days[i].close
        stop, target = round(entry * stop_at, 2), round(entry * target_at, 2)
        if all(d.low > stop and d.high < target for d in days[i + 1:]) \
                and stop < history.price < target:
            break
    return {"entry": entry, "stop": stop, "target": target, "opened": days[i].closed}


def _log_trade(conn: sqlite3.Connection, user_id: int, symbol: str,
               plan: Optional[Dict], note: str) -> None:
    if plan is None:
        return
    entry, stop = round(plan["entry"], 2), plan["stop"]
    qty = _size(entry, stop, crypto=symbol.endswith("-USD"))
    tid = db_mod.open_trade(conn, symbol, "1d", 1, entry, stop, plan["target"],
                            quantity=qty, risk_amount=round((entry - stop) * qty, 2),
                            notes=note, user=user_id, opened_ts=plan["opened"])
    if "exit" in plan:
        # Not close_trade, which is a transaction of its own (see _Together).
        row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        db_mod._close_in_one(conn, row, tid, round(plan["exit"], 2), plan["reason"],
                             plan["closed"])


class _Together:
    """The connection, with the commit each write makes of its own left out.

    The whole fill is then one change: a visitor sees the book there was or
    the new one, never one with its trades and no watchlist yet, and a fill
    that breaks off changes nothing.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def commit(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _fill(real: sqlite3.Connection, user_id: int, past: Dict[str, _History],
          live: bool) -> None:
    """Replace what the example account holds, all at once. Only its own rows
    are touched.

    Every entry and exit is at a price its day really had, on the day it had
    it. Filled with stand-ins it carries no mark of being filled, so the next
    background run fills it again.
    """
    real.executescript(db_mod._EVENTS)
    conn = _Together(real)
    try:
        conn.execute("DELETE FROM meta WHERE key=?", (_FILLED,))
        # The history of alerts goes too: it names positions and alerts of the
        # example before.
        for table in accounts_mod.OWNED_TABLES + ("alert_events",):
            conn.execute("DELETE FROM %s WHERE user_id=?" % table, (user_id,))

        # Two positions still open, sized by the same rule the app applies to
        # everyone else's, and three closed: a target hit, a stop hit, and one
        # closed early.
        _log_trade(conn, user_id, "NVDA", _still_open(past["NVDA"], 0.93, 1.12),
                   "Example: still open, with the stop and the target it was entered with.")
        _log_trade(conn, user_id, "BTC-USD", _still_open(past["BTC-USD"], 0.92, 1.15),
                   "Example: sized by the 1% rule, and still the one to watch: a bad "
                   "day on Bitcoin costs several times what the stop budgets.")
        _log_trade(conn, user_id, "AAPL", _finished(past["AAPL"].days, "target"),
                   "Example: target hit. The plan said take it, so it did.")
        _log_trade(conn, user_id, "TSLA", _finished(past["TSLA"].days, "stop"),
                   "Example: stopped out. That is the point where the idea was wrong.")
        _log_trade(conn, user_id, "MSFT", _left_early(past["MSFT"].days),
                   "Example: closed early, before the stop or the target was reached.")

        for symbol in ("NVDA", "AAPL", "SPY", "ETH-USD"):
            db_mod.watch_add(conn, symbol, "1d", note="Example", user=user_id)
        db_mod.alert_add(conn, "NVDA", "price_above",
                         threshold=round(past["NVDA"].price * 1.10, 2),
                         note="Example alert", user=user_id)

        # Two saved theses: one open, and one closed as right for the wrong
        # reason, so the luck column has something in it. That one is saved
        # at the lowest close of these weeks, and closed at the highest since.
        verdict = ("The evidence leans bullish, with real arguments on both sides. "
                   "The bear points are the ones to monitor.")
        days = past["TSLA"].days
        low = min(range(len(days) - 1), key=lambda k: days[k].close)
        high = max(range(low + 1, len(days)), key=lambda k: days[k].close)
        start, end = days[low], days[high]
        tid = db_mod.thesis_save(conn, "TSLA", round(start.close, 2), 0.2, verdict,
                                 _BULL, _BEAR, _FALSIFIERS, [], "Example thesis",
                                 user=user_id)
        db_mod.thesis_close(conn, tid, round(end.close, 2),
                            "luck" if end.close > start.close else "wrong", user=user_id)
        conn.execute("UPDATE theses SET created_ts=?, closed_ts=? WHERE id=? AND user_id=?",
                     (start.closed, end.closed, tid, user_id))
        db_mod.thesis_save(conn, "NVDA", round(past["NVDA"].price, 2), 0.32, verdict,
                           _BULL, _BEAR, _FALSIFIERS, [], "Example thesis", user=user_id)
        if live:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (_FILLED, str(int(time.time()))))
        real.commit()
    except BaseException:
        real.rollback()
        raise


def _account(conn: sqlite3.Connection) -> Optional[accounts_mod.User]:
    row = conn.execute("SELECT id FROM users WHERE email=?", (EMAIL,)).fetchone()
    return None if row is None else accounts_mod.get_user(conn, int(row["id"]))


def _create(conn: sqlite3.Connection) -> accounts_mod.User:
    """The example account, made if there is none yet. Called under the lock."""
    found = _account(conn)
    if found is None:
        now = int(time.time())
        conn.execute(
            "INSERT INTO users (email, name, provider, provider_id, "
            "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,0)",
            (EMAIL, NAME, "demo", "", now, now))
        conn.commit()
        found = _account(conn)
    return found


def _empty(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM trades WHERE user_id=? LIMIT 1",
                        (user_id,)).fetchone() is None


def user(conn: sqlite3.Connection) -> accounts_mod.User:
    """The example account, as it stands. Fetches nothing.

    Made and filled here only when it is empty, from the prices kept here;
    the background job puts the market's in (refresh).
    """
    found = _account(conn)
    if found is not None and not _empty(conn, found.id):
        return found
    with _lock:
        found = _create(conn)
        if _empty(conn, found.id):
            try:
                _fill(conn, found.id, {s: _stand_in(s) for s in _FALLBACK}, live=False)
            except Exception:
                # An empty example is a worse example. A page that errors for
                # everyone looking around is worse than that.
                log.exception("The example account could not be filled")
        return found


def _due(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (_FILLED,)).fetchone()
    try:
        return row is None or time.time() - int(row["value"]) > RESEED_AFTER
    except ValueError:
        return True


def refresh(conn: sqlite3.Connection) -> bool:
    """Refill the example at the market's prices, if it is due. The background
    job's work: returns whether it was refilled.

    A few weeks on, the market has moved, and an example book that sits past
    its stops reads as the app's advice going wrong. Every price is read
    before anything is deleted and before the lock is taken: while one cannot
    be read the example stays as it is, and the next run tries again.
    """
    if not _due(conn):
        return False
    past: Dict[str, _History] = {}
    for symbol in _FALLBACK:
        got = _history(symbol)
        if got is None:
            return False
        past[symbol] = got
    with _lock:
        if not _due(conn):
            return False
        _fill(conn, _create(conn).id, past, live=True)
    return True


def is_demo(found: Optional[accounts_mod.User]) -> bool:
    return found is not None and found.provider == "demo"
