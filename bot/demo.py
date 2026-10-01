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
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Dict, Optional

from . import accounts as accounts_mod
from . import database as db_mod

EMAIL = "example@orenth.invalid"      # .invalid can never be a real address
NAME = "Example account"

# The instruments a visitor may open an analysis or research page for. A fixed
# list, so a crawler walking through every ticker cannot make the server
# analyse every ticker: each of these is computed once and shared.
SYMBOLS = ("NVDA", "AAPL", "TSLA", "MSFT", "SPY", "BTC-USD", "ETH-USD")

# Used only when a live price cannot be fetched at the moment of seeding, so
# that a bad minute at the data source does not leave the example empty.
_FALLBACK = {"NVDA": 180.0, "AAPL": 230.0, "TSLA": 330.0, "MSFT": 500.0,
             "BTC-USD": 100000.0}

_lock = threading.Lock()


def _price(symbol: str) -> float:
    try:
        from .market import fetch_bars
        return float(fetch_bars(symbol, "1d").last_price)
    except Exception:
        return _FALLBACK[symbol]


def _point(headline: str, detail: str) -> Dict:
    return {"headline": headline, "detail": detail, "evidence": []}


def _seed(conn: sqlite3.Connection, user_id: int) -> None:
    """Fill the example account. Prices are live, so the numbers look current."""
    px = {s: _price(s) for s in ("NVDA", "AAPL", "TSLA", "MSFT", "BTC-USD")}

    # Two positions still open, priced so they read as a normal working book.
    nvda = px["NVDA"] * 0.97
    db_mod.open_trade(conn, "NVDA", "1d", 1, round(nvda, 2),
                      round(nvda * 0.93, 2), round(nvda * 1.12, 2),
                      quantity=20, risk_amount=round(nvda * 0.07 * 20, 2),
                      notes="Example: bought the pullback to the 21-day average.",
                      user=user_id)
    btc = px["BTC-USD"] * 0.98
    db_mod.open_trade(conn, "BTC-USD", "1d", 1, round(btc, 2),
                      round(btc * 0.92, 2), round(btc * 1.15, 2),
                      quantity=0.05, risk_amount=round(btc * 0.08 * 0.05, 2),
                      notes=("Example: kept small, because a bad day on Bitcoin "
                             "costs several times what the stop budgets."),
                      user=user_id)

    # Three closed: a target hit, a stop hit, and a small win taken early.
    def closed(symbol, entry, stop, target, exit_price, reason, note, qty):
        tid = db_mod.open_trade(conn, symbol, "1d", 1, round(entry, 2),
                                round(stop, 2), round(target, 2), quantity=qty,
                                risk_amount=round((entry - stop) * qty, 2),
                                notes=note, user=user_id)
        db_mod.close_trade(conn, tid, round(exit_price, 2), exit_reason=reason,
                           user=user_id)

    a = px["AAPL"] * 0.92
    closed("AAPL", a, a * 0.95, a * 1.08, a * 1.08, "target",
           "Example: target hit. The plan said take it, so it did.", 30)
    t = px["TSLA"] * 1.04
    closed("TSLA", t, t * 0.94, t * 1.12, t * 0.94, "stop",
           "Example: stopped out. That is the point where the idea was wrong.", 10)
    m = px["MSFT"] * 0.97
    closed("MSFT", m, m * 0.95, m * 1.10, m * 1.03, "manual",
           "Example: closed early ahead of earnings.", 12)

    for symbol in ("NVDA", "AAPL", "SPY", "ETH-USD"):
        db_mod.watch_add(conn, symbol, "1d", note="Example", user=user_id)
    db_mod.alert_add(conn, "NVDA", "price_above",
                     threshold=round(px["NVDA"] * 1.10, 2),
                     note="Example alert", user=user_id)

    # Two saved theses: one open, and one closed as right for the wrong
    # reason, so the luck column has something in it.
    db_mod.thesis_save(
        conn, "NVDA", round(px["NVDA"], 2), 0.32,
        "The evidence leans bullish, with real arguments on both sides. "
        "The bear points are the ones to monitor.",
        [_point("Revenue is still growing quickly",
                "Data-centre sales grew again in the latest filing.")],
        [_point("The price already assumes years of growth",
                "A slowdown would hit the valuation hard.")],
        ["Two quarters in a row of shrinking gross margin"], [],
        "Example thesis", user=user_id)
    tid = db_mod.thesis_save(
        conn, "TSLA", round(px["TSLA"] * 0.9, 2), 0.2,
        "The evidence leans bullish, with real arguments on both sides. "
        "The bear points are the ones to monitor.",
        [_point("Deliveries were expected to recover", "")],
        [_point("Margins were falling", "")],
        ["Another quarter of falling deliveries"], [],
        "Example thesis", user=user_id)
    db_mod.thesis_close(conn, tid, round(px["TSLA"], 2), "luck", user=user_id)


def user(conn: sqlite3.Connection) -> accounts_mod.User:
    """The example account, created and filled the first time it is needed."""
    with _lock:
        row = conn.execute("SELECT * FROM users WHERE email=?", (EMAIL,)).fetchone()
        if row is None:
            import time
            now = int(time.time())
            conn.execute(
                "INSERT INTO users (email, name, provider, provider_id, "
                "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,0)",
                (EMAIL, NAME, "demo", "", now, now))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE email=?",
                               (EMAIL,)).fetchone()
        found = accounts_mod.get_user(conn, int(row["id"]))
        has_data = conn.execute("SELECT 1 FROM trades WHERE user_id=? LIMIT 1",
                                (found.id,)).fetchone()
        if has_data is None:
            _seed(conn, found.id)
        return found


def is_demo(found: Optional[accounts_mod.User]) -> bool:
    return found is not None and found.provider == "demo"
