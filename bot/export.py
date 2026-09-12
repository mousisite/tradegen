"""Getting your data back out.

Two reasons this is not optional. The obvious one is tax: a closed trade log
that cannot leave the app is useless in April. The less obvious one is trust.
A tool that holds your record hostage is a tool you cannot leave, and the whole
argument this app makes is that you should check things rather than believe
them. That applies to the app itself.

Everything here writes plain CSV or JSON with no dependencies, and every column
is one a person can read.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

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


def _rows_to_csv(rows: Iterable[Dict], columns: List[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# --- trades -----------------------------------------------------------------

TRADE_COLUMNS = [
    "id", "symbol", "interval", "side", "status", "opened", "closed",
    "entry", "stop", "target", "quantity", "exit_price", "exit_reason",
    "r_multiple", "pnl", "risk_amount", "account", "note",
]


def trades(conn: sqlite3.Connection, status: Optional[str] = None,
           user: Optional[int] = None) -> List[Dict]:
    """The journal as flat rows, with the codes turned into words."""
    out = []
    for row in db.list_trades(conn, status=status, limit=100000, user=user):
        out.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "side": "long" if row["direction"] > 0 else "short",
            "status": row["status"],
            "opened": _when(row["ts_opened"]),
            "closed": _when(row["ts_closed"]),
            "entry": row["entry"],
            "stop": row["stop"],
            "target": row["target1"],
            "quantity": row["quantity"],
            "exit_price": row["exit_price"],
            "exit_reason": row["exit_reason"] or "",
            "r_multiple": row["r_multiple"],
            "pnl": row["pnl"],
            "risk_amount": row["risk_amount"],
            "account": row["account"],
            "note": (row["notes"] or "") or (row["strategy_note"] or ""),
        })
    return out


# --- theses -----------------------------------------------------------------

THESIS_COLUMNS = [
    "id", "symbol", "saved", "price_then", "balance", "verdict",
    "closed", "price_at_close", "outcome", "move_pct", "note",
]


def theses(conn: sqlite3.Connection, user: Optional[int] = None) -> List[Dict]:
    out = []
    for row in db.thesis_list(conn, limit=1000, user=user):
        then, close = row["price"], row["closed_price"]
        move = ((close - then) / then * 100.0) if (then and close) else None
        out.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "saved": _when(row["created_ts"]),
            "price_then": then,
            "balance": row["balance"],
            "verdict": row["verdict"],
            "closed": _when(row["closed_ts"]),
            "price_at_close": close,
            "outcome": row["outcome"] or "",
            "move_pct": round(move, 2) if move is not None else None,
            "note": row["user_note"] or "",
        })
    return out


# --- measured strategy record -----------------------------------------------

STRATEGY_COLUMNS = [
    "strategy", "family", "symbol", "interval", "regime", "setups", "wins",
    "hit_rate", "expectancy_gross_r", "expectancy_net_r",
]


def strategies(conn: sqlite3.Connection,
               user: Optional[int] = None) -> List[Dict]:
    """Every measurement the bot has made, not just the leaderboard's top rows.

    This is the part worth keeping if the app is ever thrown away: it is the
    accumulated result of back-testing across everything that has been looked
    at, and it cost real time to produce.
    """
    # `user` is accepted and ignored on purpose: this record describes
    # instruments, not people, and is shared by everyone using the install.
    sql = ("SELECT strategy, family, symbol, interval, regime, n, wins, "
           "sum_r, sum_r_gross FROM strategy_stats ORDER BY strategy, symbol")
    out = []
    for row in conn.execute(sql):
        n = row["n"] or 0
        out.append({
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
        })
    return out


# --- run history ------------------------------------------------------------

RUN_COLUMNS = [
    "id", "when", "symbol", "interval", "call", "conviction", "probability",
    "entry", "stop", "target", "regime_trend", "regime_volatility", "source",
]


def runs(conn: sqlite3.Connection, limit: int = 5000,
         user: Optional[int] = None) -> List[Dict]:
    out = []
    sql, args = "SELECT * FROM runs", []
    if user is not None:
        sql += " WHERE user_id = ?"
        args.append(int(user))
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    for row in conn.execute(sql, args):
        keys = row.keys()
        out.append({
            "id": row["id"],
            "when": _when(row["ts"]),
            "symbol": row["symbol"],
            "interval": row["interval"],
            "call": row["action"] if "action" in keys else "",
            "conviction": row["conviction"] if "conviction" in keys else None,
            "probability": row["probability"] if "probability" in keys else None,
            "entry": row["entry"] if "entry" in keys else None,
            "stop": row["stop"] if "stop" in keys else None,
            "target": row["target1"] if "target1" in keys else None,
            "regime_trend": row["regime_trend"] if "regime_trend" in keys else "",
            "regime_volatility": (row["regime_volatility"]
                                  if "regime_volatility" in keys else ""),
            "source": row["source"] if "source" in keys else "",
        })
    return out


# --- the dispatcher the web layer uses --------------------------------------

DATASETS = {
    "trades": {
        "label": "Trade journal",
        "why": "Every position opened and closed, with its R multiple. This is "
               "the one you need at tax time.",
        "rows": trades,
        "columns": TRADE_COLUMNS,
    },
    "theses": {
        "label": "Saved theses",
        "why": "What you argued, when, at what price, and how it turned out.",
        "rows": theses,
        "columns": THESIS_COLUMNS,
    },
    "strategies": {
        "label": "Measured strategy record",
        "why": "Every back-test result the bot has accumulated. The slowest "
               "thing here to rebuild from scratch.",
        "rows": strategies,
        "columns": STRATEGY_COLUMNS,
    },
    "runs": {
        "label": "Analysis history",
        "why": "Every call the bot has made, so you can check it against what "
               "happened afterwards.",
        "rows": runs,
        "columns": RUN_COLUMNS,
    },
}


def build(conn: sqlite3.Connection, dataset: str, fmt: str = "csv",
          user: Optional[int] = None) -> Dict:
    """One dataset in one format, ready to be sent as a file."""
    spec = DATASETS.get(dataset)
    if spec is None:
        raise ValueError("There is nothing called %r to export." % dataset)
    if fmt not in ("csv", "json"):
        raise ValueError("Exports are CSV or JSON, not %r." % fmt)

    rows = spec["rows"](conn, user=user)
    stamp = time.strftime("%Y-%m-%d")
    name = "stockbot-%s-%s.%s" % (dataset, stamp, fmt)

    if fmt == "csv":
        body = _rows_to_csv(rows, spec["columns"])
        mime = "text/csv; charset=utf-8"
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
            out[key] = len(spec["rows"](conn, user=user))
        except Exception:
            out[key] = 0
    return out
