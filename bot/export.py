"""Getting your data back out.

Two reasons this is not optional. The obvious one is tax: a closed trade log
that cannot leave the app is useless in April. The less obvious one is trust.
A tool that holds your record hostage is a tool you cannot leave, and the whole
argument this app makes is that you should check things rather than believe
them. That applies to the app itself.

Everything here writes plain CSV or JSON with no dependencies, and every column
is one a person can read. Times are in UTC, and the columns that hold one end
in _utc to say so.

Given a person, each dataset is theirs alone. Given none, it is everybody's,
which is what the copies kept with each backup are, and each row then says
whose it is.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional

from . import database as db


def _when(stamp) -> str:
    """A timestamp a spreadsheet will parse, or empty rather than 1970."""
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(int(stamp), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _cell(value):
    """A value as a CSV cell: text a spreadsheet would run is marked as text.

    A note such as "-2R, stopped out" or "=HYPERLINK(...)" is a formula to a
    spreadsheet, which shows an error for the first and runs the second. The
    quote in front is how a spreadsheet is told the cell is words. Numbers are
    left alone: a loss begins with a minus sign too.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _write(fh, rows: Iterable[Dict], columns: List[str]) -> int:
    writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow({key: _cell(value) for key, value in row.items()})
        count += 1
    return count


def _rows_to_csv(rows: Iterable[Dict], columns: List[str]) -> str:
    buf = io.StringIO()
    _write(buf, rows, columns)
    return buf.getvalue()


def _owned(out: Dict, row, user: Optional[int]) -> Dict:
    """The row, with whose it is when it is one of everybody's."""
    if user is None:
        out["user_id"] = db._column(row, "user_id")
    return out


def _yours(sql: str, user: Optional[int]):
    """The query and its arguments, for one person or for everybody."""
    if user is None:
        return sql, []
    return sql + " WHERE user_id = ?", [int(user)]


# --- trades -----------------------------------------------------------------

TRADE_COLUMNS = [
    "id", "symbol", "interval", "side", "status", "opened_utc", "closed_utc",
    "entry", "stop", "target", "quantity", "quantity_open", "exits",
    "exit_price", "exit_reason", "r_multiple", "pnl", "risk_amount",
    "trail", "account", "note",
    # The journal's tags and review, in words, and the New York session the
    # trade was entered in, worked out from its entry time.
    "setup", "emotion", "checklist", "broke_rule", "followed_plan", "lesson",
    "reviewed_utc", "session",
]


def _trail_said(row) -> str:
    """A trade's trailing stop in words, or empty when it has none."""
    from .portfolio import trail_of
    trail = trail_of(row)
    return "" if trail is None else "%s behind the best price" % trail.words()


def trades(conn: sqlite3.Connection, status: Optional[str] = None,
           user: Optional[int] = None) -> List[Dict]:
    """The journal as flat rows, with the codes turned into words.

    quantity is the size the trade was opened with and quantity_open what is
    still open of it. For a trade closed in parts, exit_price is the average
    of its exits, and r_multiple and pnl are the whole position's; for one
    still open, they are what its exits have taken so far. The exits
    dataset lists each one, with its own date and price.
    """
    from . import clock
    from . import journal

    rows = db.list_trades(conn, status=status, limit=100000, user=user)
    exits = db.exits_for(conn, [row["id"] for row in rows])
    out = []
    for row in rows:
        parts = db.trade_parts(row, exits.get(row["id"], []))
        ticked = journal.checklist_of(row)
        out.append(_owned({
            "id": row["id"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "side": "long" if row["direction"] > 0 else "short",
            "status": row["status"],
            "opened_utc": _when(row["ts_opened"]),
            "closed_utc": _when(row["ts_closed"]),
            "entry": row["entry"],
            "stop": row["stop"],
            "target": row["target1"],
            "quantity": row["quantity"],
            "quantity_open": parts["left"],
            "exits": len(parts["exits"]),
            "exit_price": parts["avg_exit"],
            "exit_reason": row["exit_reason"] or "",
            "r_multiple": parts["r"],
            "pnl": parts["pnl"] if parts["exits"] else row["pnl"],
            "risk_amount": row["risk_amount"],
            "trail": _trail_said(row),
            "account": row["account"],
            "note": (row["notes"] or "") or (row["strategy_note"] or ""),
            "setup": journal.SETUP_LABELS.get(db._column(row, "setup") or "", ""),
            "emotion": journal.EMOTION_LABELS.get(db._column(row, "emotion") or "", ""),
            "checklist": ("" if ticked is None else "%d of %d ticked%s" % (
                len(ticked), len(journal.CHECK_KEYS),
                (": " + ", ".join(ticked)) if ticked else "")),
            "broke_rule": "; ".join(journal.broke_rules_of(row)),
            "followed_plan": journal.FOLLOWED_LABELS.get(
                db._column(row, "followed_plan") or "", ""),
            "lesson": db._column(row, "lesson") or "",
            "reviewed_utc": _when(db._column(row, "reviewed_ts")),
            "session": clock.SESSION_LABELS.get(journal.session_of_trade(row), ""),
        }, row, user))
    return out


# --- every exit, a part or the whole ------------------------------------------

EXIT_COLUMNS = [
    "trade_id", "symbol", "side", "when_utc", "quantity", "price", "reason",
    "r_multiple", "pnl", "part", "entry", "stop", "split", "quantity_traded",
    "price_traded",
]


def exits(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    """Every exit from every trade, oldest first: what was sold, when, at what price.

    A trade closed in one go is one row; one closed in parts is a row per
    part, each with its own date and price, which is what a tax return asks
    for. quantity and price are in the shares the trade was opened with, as
    the journal keeps them. A broker lists a sale after a split in that
    day's shares: split is the shares then per share opened with, kept when
    the exit was written, and quantity_traded and price_traded the sale in
    them. The three are empty where the split was not known.
    """
    rows = db.list_trades(conn, limit=100000, user=user)
    held = db.exits_for(conn, [row["id"] for row in rows])
    out = []
    for row in rows:
        for part in db.trade_parts(row, held.get(row["id"], []))["exits"]:
            split = part["split"]
            out.append(_owned({
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "side": "long" if row["direction"] > 0 else "short",
                "when_utc": _when(part["ts"]),
                "quantity": part["quantity"],
                "price": part["price"],
                "reason": part["reason"] or "",
                "r_multiple": part["r"],
                "pnl": part["pnl"],
                "part": "yes" if part["partial"] else "no",
                "entry": row["entry"],
                "stop": row["stop"],
                "split": split,
                # Rounded only to drop the dust of turning shares back and
                # forth: 7 of today's shares are 7.000000000000001 otherwise.
                "quantity_traded": (round(part["quantity"] * split, 8)
                                    if split and part["quantity"] is not None else None),
                "price_traded": round(part["price"] / split, 8) if split else None,
                "_ts": int(part["ts"] or 0),
            }, row, user))
    out.sort(key=lambda r: (r["_ts"], r["trade_id"]))
    for r in out:
        del r["_ts"]
    return out


# --- theses -----------------------------------------------------------------

THESIS_COLUMNS = [
    "id", "symbol", "saved_utc", "price_then", "balance", "verdict",
    "closed_utc", "price_at_close", "outcome", "move_pct", "note",
]


def theses(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    out = []
    for row in db.thesis_list(conn, limit=100000, user=user):
        then, close = row["price"], row["closed_price"]
        move = ((close - then) / then * 100.0) if (then and close) else None
        out.append(_owned({
            "id": row["id"],
            "symbol": row["symbol"],
            "saved_utc": _when(row["created_ts"]),
            "price_then": then,
            "balance": row["balance"],
            "verdict": row["verdict"],
            "closed_utc": _when(row["closed_ts"]),
            "price_at_close": close,
            "outcome": row["outcome"] or "",
            "move_pct": round(move, 2) if move is not None else None,
            "note": row["user_note"] or "",
        }, row, user))
    return out


# --- what is being watched -----------------------------------------------------

WATCH_COLUMNS = ["symbol", "interval", "note", "added_utc", "last_analysed_utc"]


def watchlist(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    return [_owned({
        "symbol": row["symbol"],
        "interval": row["interval"],
        "note": row["note"] or "",
        "added_utc": _when(row["added_ts"]),
        "last_analysed_utc": _when(row["last_run"]),
    }, row, user) for row in db.watchlist(conn, user=user)]


ALERT_COLUMNS = [
    "id", "symbol", "condition", "interval", "note", "on", "keeps_watching",
    "created_utc", "last_checked_utc", "last_fired_utc", "value_when_fired",
    "what_it_said",
]


def alerts(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    """Each alert, with its condition in the words the Monitor page uses."""
    from .alerts import describe
    return [_owned({
        "id": row["id"],
        "symbol": row["symbol"],
        "condition": describe(row["kind"], row["threshold"], row["symbol"]),
        "interval": row["interval"],
        "note": row["note"] or "",
        "on": "yes" if row["active"] else "no",
        "keeps_watching": "yes" if row["repeat"] else "no",
        "created_utc": _when(row["created_ts"]),
        "last_checked_utc": _when(row["last_checked"]),
        "last_fired_utc": _when(row["triggered_ts"]),
        "value_when_fired": row["trigger_value"],
        "what_it_said": row["trigger_note"] or "",
    }, row, user) for row in db.alert_list(conn, user=user)]


EVENT_COLUMNS = ["id", "when_utc", "symbol", "message", "value", "price_as_of"]


def alert_history(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    """What the background check has told people, newest first."""
    sql, args = _yours("SELECT * FROM alert_events", user)
    try:
        rows = list(conn.execute(sql + " ORDER BY ts DESC, id DESC", args))
    except sqlite3.OperationalError as exc:
        # Made the first time it is needed, so a database may not have it, and
        # a backup's copy is only read.
        if "no such table" not in str(exc).lower():
            raise
        return []
    return [_owned({
        "id": row["id"],
        "when_utc": _when(row["ts"]),
        "symbol": row["symbol"],
        "message": row["message"],
        "value": row["value"],
        "price_as_of": row["as_of"] or "",
    }, row, user) for row in rows]


# --- measured strategy record -----------------------------------------------

STRATEGY_COLUMNS = [
    "strategy", "family", "symbol", "interval", "regime", "setups", "wins",
    "hit_rate", "expectancy_gross_r", "expectancy_net_r",
]


def strategies(conn: sqlite3.Connection,
               user: Optional[int] = None) -> Iterator[Dict]:
    """Every measurement the bot has made, not just the leaderboard's top rows.

    This is the part worth keeping if the app is ever thrown away: it is the
    accumulated result of back-testing across everything that has been looked
    at, and it cost real time to produce.
    """
    # `user` is accepted and ignored on purpose: this record describes
    # instruments, not people, and is shared by everyone using the install.
    sql = ("SELECT strategy, family, symbol, interval, regime, n, wins, "
           "sum_r, sum_r_gross FROM strategy_stats ORDER BY strategy, symbol")
    for row in conn.execute(sql):
        n = row["n"] or 0
        yield {
            "strategy": row["strategy"],
            "family": row["family"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "regime": row["regime"],
            "setups": n,
            "wins": row["wins"],
            "hit_rate": round(row["wins"] / n, 4) if n else None,
            "expectancy_gross_r": round(row["sum_r_gross"] / n, 5) if n else None,
            "expectancy_net_r": round(row["sum_r"] / n, 5) if n else None,
        }


# --- run history ------------------------------------------------------------

RUN_COLUMNS = [
    "id", "when_utc", "symbol", "interval", "call", "conviction", "probability",
    "entry", "stop", "target", "regime_trend", "regime_volatility", "source",
]


def runs(conn: sqlite3.Connection,
         user: Optional[int] = None) -> Iterator[Dict]:
    """Every call, newest first, a row at a time: there can be a great many."""
    sql, args = _yours("SELECT * FROM runs", user)
    for row in conn.execute(sql + " ORDER BY ts DESC", args):
        keys = row.keys()
        yield _owned({
            "id": row["id"],
            "when_utc": _when(row["ts"]),
            "symbol": row["symbol"],
            "interval": row["interval"],
            "call": row["action"] if "action" in keys else "",
            "conviction": row["conviction"] if "conviction" in keys else None,
            "probability": row["probability"] if "probability" in keys else None,
            "entry": row["entry"] if "entry" in keys else None,
            "stop": row["stop"] if "stop" in keys else None,
            "target": row["target1"] if "target1" in keys else None,
            "regime_trend": row["regime_trend"] if "regime_trend" in keys else "",
            "regime_volatility": row["regime_vol"] if "regime_vol" in keys else "",
            "source": row["source"] if "source" in keys else "",
        }, row, user)


# --- how many rows each would hold, without building it ------------------------

def _rows_in(table: str, owned: bool = True):
    def count(conn: sqlite3.Connection, user: Optional[int] = None) -> int:
        sql, args = _yours("SELECT COUNT(*) FROM %s" % table, user if owned else None)
        return int(conn.execute(sql, args).fetchone()[0])
    return count


def _exit_count(conn: sqlite3.Connection, user: Optional[int] = None) -> int:
    """As db.trade_parts counts a trade's exits: each part kept, a trade closed
    in one go, and what a trade closed in parts had left when it was closed."""
    total = 0
    for sql in (
            "SELECT COUNT(*) FROM trade_exits e JOIN trades t ON t.id = e.trade_id "
            "WHERE 1=1",
            "SELECT COUNT(*) FROM trades t WHERE t.status = 'closed' "
            "AND t.exit_price IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM trade_exits e WHERE e.trade_id = t.id)",
            "SELECT COUNT(*) FROM trades t JOIN (SELECT trade_id, SUM(quantity) AS gone "
            "FROM trade_exits GROUP BY trade_id) e ON e.trade_id = t.id "
            "WHERE t.status = 'closed' AND t.exit_price IS NOT NULL "
            "AND t.quantity - e.gone > %r * MAX(1.0, t.quantity)" % db._DUST):
        args = []
        if user is not None:
            sql, args = sql + " AND t.user_id = ?", [int(user)]
        total += int(conn.execute(sql, args).fetchone()[0])
    return total


# --- the dispatcher the web layer uses --------------------------------------

DATASETS = {
    "trades": {
        "label": "Trade journal",
        "why": "Every position opened and closed, with its R multiple. This is "
               "the one you need at tax time. Its prices and sizes are in the "
               "shares each trade was opened with. Times are in UTC, so a late "
               "evening in New York is already the next day.",
        "rows": trades,
        "count": _rows_in("trades"),
        "columns": TRADE_COLUMNS,
    },
    "exits": {
        "label": "Trade exits",
        "why": "Each sale or cover with its own date, size and price, parts of "
               "a position included. A tax return asks for each one. After a "
               "share split, quantity_traded and price_traded give it in the "
               "shares of that day, as your broker lists it.",
        "rows": exits,
        "count": _exit_count,
        "columns": EXIT_COLUMNS,
    },
    "theses": {
        "label": "Saved theses",
        "why": "What you argued, when, at what price, and how it turned out.",
        "rows": theses,
        "count": _rows_in("theses"),
        "columns": THESIS_COLUMNS,
    },
    "watchlist": {
        "label": "Watchlist",
        "why": "The instruments you are watching, and when you added each.",
        "rows": watchlist,
        "count": _rows_in("watchlist"),
        "columns": WATCH_COLUMNS,
    },
    "alerts": {
        "label": "Alerts",
        "why": "Each alert you have set, its condition in words, and when it last "
               "fired.",
        "rows": alerts,
        "count": _rows_in("alerts"),
        "columns": ALERT_COLUMNS,
    },
    "alert_history": {
        "label": "Alert history",
        "why": "What your alerts, and the stops of your open trades, have told you.",
        "rows": alert_history,
        "count": _rows_in("alert_events"),
        "columns": EVENT_COLUMNS,
    },
    "strategies": {
        "label": "Measured strategy record",
        "why": "Every back-test result the bot has accumulated. The slowest "
               "thing here to rebuild from scratch.",
        "rows": strategies,
        "count": _rows_in("strategy_stats", owned=False),
        "columns": STRATEGY_COLUMNS,
        # About instruments, not people, so no row of it is anybody's.
        "shared": True,
    },
    "runs": {
        "label": "Analysis history",
        "why": "Every call the bot has made, so you can check it against what "
               "happened afterwards.",
        "rows": runs,
        "count": _rows_in("runs"),
        "columns": RUN_COLUMNS,
    },
}


def _columns(spec: Dict, user: Optional[int]) -> List[str]:
    """The dataset's columns, with whose each row is when it is everybody's."""
    return (["user_id"] if user is None and not spec.get("shared") else []) + spec["columns"]


def write_csv(conn: sqlite3.Connection, dataset: str, fh,
              user: Optional[int] = None) -> int:
    """One dataset written to an open file as CSV; how many rows it held.

    For the copies kept with a backup, which are everybody's: row by row, so a
    long history is never all in memory at once.
    """
    spec = DATASETS[dataset]
    return _write(fh, spec["rows"](conn, user=user), _columns(spec, user))


def build(conn: sqlite3.Connection, dataset: str, fmt: str = "csv",
          user: Optional[int] = None) -> Dict:
    """One dataset in one format, ready to be sent as a file."""
    spec = DATASETS.get(dataset)
    if spec is None:
        raise ValueError("There is nothing called %r to export." % dataset)
    if fmt not in ("csv", "json"):
        raise ValueError("Exports are CSV or JSON, not %r." % fmt)

    rows = list(spec["rows"](conn, user=user))
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    name = "orenth-%s-%s.%s" % (dataset.replace("_", "-"), stamp, fmt)

    if fmt == "csv":
        body = _rows_to_csv(rows, _columns(spec, user))
        # No charset: the response that carries it adds its own.
        mime = "text/csv"
    else:
        body = json.dumps({
            "dataset": dataset,
            "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(rows),
            "rows": rows,
        }, indent=2, default=str)
        mime = "application/json"

    return {"filename": name, "body": body, "mimetype": mime,
            "count": len(rows), "label": spec["label"]}


def counts(conn: sqlite3.Connection,
           user: Optional[int] = None) -> Dict[str, int]:
    """How many rows each dataset holds, so the page can say before you click."""
    out = {}
    for key, spec in DATASETS.items():
        try:
            out[key] = spec["count"](conn, user=user)
        except Exception:
            out[key] = 0
    return out
