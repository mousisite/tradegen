"""What people actually do, rather than what you hope they do.

Before pricing anything, the question worth answering is which parts of this
get used. The database already records every analysis, so this asks it rather
than adding tracking: no third-party analytics, no scripts on the page, nothing
that leaves the server. The privacy policy promises that, and it stays true.

Read-only and honest about small numbers: with a handful of users almost
nothing here is significant, and it says so instead of drawing a trend through
four points.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Dict, List, Optional

DAY = 86400


def _rows(conn, sql, args=()):
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error:
        return []


def _count(conn, sql, args=()) -> int:
    rows = _rows(conn, sql, args)
    return int(rows[0][0]) if rows else 0


def overview(conn: sqlite3.Connection, days: int = 30) -> Dict:
    """Who used it, what they looked at, and what they did about it."""
    since = int(time.time()) - days * DAY

    people = _count(conn, "SELECT COUNT(*) FROM users WHERE is_local = 0")
    active = _count(conn,
                    "SELECT COUNT(DISTINCT user_id) FROM runs WHERE ts >= ?",
                    (since,))
    analyses = _count(conn, "SELECT COUNT(*) FROM runs WHERE ts >= ?", (since,))
    instruments = _count(conn,
                         "SELECT COUNT(DISTINCT symbol) FROM runs WHERE ts >= ?",
                         (since,))

    # The question that decides positioning: do people come back?
    returning = _count(conn, """
        SELECT COUNT(*) FROM (
            SELECT user_id FROM runs WHERE ts >= ?
            GROUP BY user_id HAVING COUNT(DISTINCT ts / 86400) >= 2)""", (since,))

    trades = _count(conn, "SELECT COUNT(*) FROM trades WHERE ts_opened >= ?",
                    (since,))
    theses = _count(conn, "SELECT COUNT(*) FROM theses WHERE created_ts >= ?",
                    (since,))
    alerts = _count(conn, "SELECT COUNT(*) FROM alerts WHERE created_ts >= ?",
                   (since,))
    watched = _count(conn, "SELECT COUNT(*) FROM watchlist WHERE added_ts >= ?",
                     (since,))

    return {
        "days": days,
        "accounts": people,
        "active": active,
        "returning": returning,
        "analyses": analyses,
        "instruments": instruments,
        "trades": trades,
        "theses": theses,
        "alerts": alerts,
        "watched": watched,
        "per_active": (analyses / active) if active else 0.0,
    }


def top_symbols(conn: sqlite3.Connection, days: int = 30,
                limit: int = 12) -> List[Dict]:
    since = int(time.time()) - days * DAY
    rows = _rows(conn, """
        SELECT symbol, COUNT(*) AS n, COUNT(DISTINCT user_id) AS people,
               MAX(ts) AS last
        FROM runs WHERE ts >= ?
        GROUP BY symbol ORDER BY n DESC LIMIT ?""", (since, limit))
    return [{"symbol": r[0], "runs": r[1], "people": r[2], "last": r[3]}
            for r in rows]


def calls_made(conn: sqlite3.Connection, days: int = 30) -> List[Dict]:
    """The distribution of verdicts.

    Worth watching: if almost everything is Avoid, people came for signals and
    are being told no, which is honest and will not retain them.
    """
    since = int(time.time()) - days * DAY
    rows = _rows(conn, """
        SELECT action, COUNT(*) FROM runs WHERE ts >= ?
        GROUP BY action ORDER BY COUNT(*) DESC""", (since,))
    total = sum(r[1] for r in rows) or 1
    return [{"call": r[0] or "unknown", "count": r[1],
             "share": r[1] / total} for r in rows]


def by_day(conn: sqlite3.Connection, days: int = 30) -> List[Dict]:
    since = int(time.time()) - days * DAY
    rows = _rows(conn, """
        SELECT ts / 86400 AS bucket, COUNT(*), COUNT(DISTINCT user_id)
        FROM runs WHERE ts >= ? GROUP BY bucket ORDER BY bucket""", (since,))
    return [{"day": int(r[0]) * DAY, "runs": r[1], "people": r[2]} for r in rows]


def intervals(conn: sqlite3.Connection, days: int = 30) -> List[Dict]:
    """Which timeframes people choose, which says which product they want."""
    since = int(time.time()) - days * DAY
    rows = _rows(conn, """
        SELECT interval, COUNT(*) FROM runs WHERE ts >= ?
        GROUP BY interval ORDER BY COUNT(*) DESC""", (since,))
    total = sum(r[1] for r in rows) or 1
    return [{"interval": r[0] or "?", "count": r[1], "share": r[1] / total}
            for r in rows]


def read(conn: sqlite3.Connection, days: int = 30) -> Dict:
    """Everything, plus a plain reading of what it does and does not show."""
    stats = overview(conn, days)
    symbols = top_symbols(conn, days)
    calls = calls_made(conn, days)
    daily = by_day(conn, days)
    spans = intervals(conn, days)

    notes: List[str] = []
    if stats["analyses"] < 25:
        notes.append(
            "Too little use to read anything into. Percentages below are "
            "arithmetic, not evidence, until there are a few hundred analyses.")
    if stats["active"] and stats["returning"] == 0:
        notes.append(
            "Nobody has come back on a second day yet. That is the number that "
            "decides whether this is a product or a demo.")
    elif stats["active"]:
        notes.append(
            "%d of %d people used it on more than one day."
            % (stats["returning"], stats["active"]))

    intraday = sum(s["share"] for s in spans
                   if s["interval"] in ("1m", "2m", "5m", "15m", "30m", "1h", "60m"))
    if stats["analyses"] >= 25:
        if intraday > 0.6:
            notes.append(
                "Most analyses are intraday, so people are here for trading "
                "signals. Worth knowing, because the back-tests say that is "
                "the harder thing to sell honestly.")
        elif intraday < 0.25:
            notes.append(
                "Almost all analyses are on daily bars or longer, which points "
                "at research rather than day trading.")

    avoid = next((c["share"] for c in calls if (c["call"] or "").upper() == "AVOID"), 0)
    if stats["analyses"] >= 25 and avoid > 0.7:
        notes.append(
            "%.0f%% of calls were Avoid. That is the honest answer most of the "
            "time, and it is also the answer people who came for signals will "
            "not pay for twice." % (avoid * 100))

    return {"stats": stats, "symbols": symbols, "calls": calls,
            "daily": daily, "intervals": spans, "notes": notes}
