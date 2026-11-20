"""Where the Setup lab's measurements are kept, and the one way they are judged.

The lab (bot/setuplab.py, bot/claims.py) writes here; the Learn pages, the
strategy test pages and the analysis page read from here. The shapes below
are a contract between them, so every record is checked before it is written:
a record that does not fit raises ValueError naming the key, rather than
reaching a page as a blank or a "None".

Every verdict comes from verdict() (the same two-margins rule as the strategy
tests, public._verdict) and is said by words(): one rule, one set of words.

Shared, not personal: these tables hold measurements of prices, nothing about
anyone, so account deletion and export do not touch them.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Dict, List, Optional, Sequence

from . import clock, setups

_TABLES = (
    "CREATE TABLE IF NOT EXISTS lab_results ("
    " symbol TEXT NOT NULL, interval TEXT NOT NULL, data TEXT NOT NULL,"
    " updated INTEGER NOT NULL, PRIMARY KEY (symbol, interval))",
    "CREATE TABLE IF NOT EXISTS lab_claims ("
    " claim TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS bias_record ("
    " symbol TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS lab_runs ("
    " id INTEGER PRIMARY KEY, started INTEGER NOT NULL, finished INTEGER,"
    " summary TEXT NOT NULL DEFAULT '{}')",
)

# A cell needs this many trades, on this many separate trading days, before
# it gets a verdict at all.
MIN_TRADES = 30
MIN_DAYS = 10

RULES = ("2r", "level")
WINDOW_KEYS = tuple(w[0] for w in clock.LAB_WINDOWS)
POOL_ALL = "_all"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the four tables when missing. No commit: a read through here
    must not commit whatever the caller has under way (the writes commit)."""
    for statement in _TABLES:
        conn.execute(statement)


# --- the one rule ---------------------------------------------------------------

def verdict(mean: Optional[float], se: Optional[float], n: int,
            minimum: int = MIN_TRADES, days: Optional[int] = None,
            min_days: int = MIN_DAYS) -> Optional[int]:
    """+1 when the whole range of mean +/- 2 standard errors is above zero, -1
    when it is all below, 0 when it straddles zero, None when there are fewer
    than `minimum` or no error was measured (public._verdict's rule), or,
    when `days` is given, when the trades came on fewer than `min_days`
    separate trading days."""
    if mean is None or se is None or n is None or n < minimum:
        return None
    if days is not None and days < min_days:
        return None
    try:
        mean, se = float(mean), float(se)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(mean) and math.isfinite(se)):
        return None
    return 1 if mean - 2 * se > 0 else -1 if mean + 2 * se < 0 else 0


_WORDS = {1: "kept money by more than its margin of error",
          -1: "lost money by more than its margin of error",
          0: "cannot be told apart from luck",
          None: "too few to say"}

_CLAIM_WORDS = {1: "happens more often than usual",
                -1: "happens less often than usual",
                0: "cannot be told apart from what usually happens",
                None: "too few to say"}


def words(v: Optional[int]) -> str:
    """A setup's verdict in words."""
    return _WORDS.get(v, _WORDS[None])


def claim_words(v: Optional[int]) -> str:
    """A claim's verdict in words: against what usually happens, not zero."""
    return _CLAIM_WORDS.get(v, _CLAIM_WORDS[None])


def wilson(hits: int, n: int, z: float = 1.96):
    """The 95% range of a rate (Wilson's), as (low, high); None for no trials."""
    if not n:
        return None
    p = hits / float(n)
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_verdict(hits: int, n: int, base_hits: int, base_n: int,
                 minimum: int = MIN_TRADES) -> Optional[int]:
    """Whether a rate is above (+1) or below (-1) its control by more than two
    standard errors of the difference, 0 when not, None under `minimum` trials
    on either side."""
    if n < minimum or base_n < minimum:
        return None
    p, q = hits / float(n), base_hits / float(base_n)
    se = math.sqrt(p * (1 - p) / n + q * (1 - q) / base_n)
    return verdict(p - q, se, n, minimum)


# --- checking a record ------------------------------------------------------------

def _num(where: str, value, none_ok: bool = False):
    if value is None and none_ok:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(float(value)):
        raise ValueError("%s must be a finite number%s, not %r"
                         % (where, " or null" if none_ok else "", value))


def _int(where: str, value, low: Optional[int] = 0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be a whole number, not %r" % (where, value))
    if low is not None and value < low:
        raise ValueError("%s must be at least %d, not %r" % (where, low, value))


def _str(where: str, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string, not %r" % (where, value))


def _dict(where: str, value) -> Dict:
    if not isinstance(value, dict):
        raise ValueError("%s must be an object, not %r" % (where, type(value).__name__))
    return value


def _need(where: str, data: Dict, keys: Sequence[str]) -> None:
    for key in keys:
        if key not in data:
            raise ValueError("%s.%s is missing" % (where, key))


def _rule(where: str, r) -> None:
    _dict(where, r)
    _need(where, r, ("n", "wins", "sum_r", "sum_r_gross", "se", "days", "longs",
                     "avg_bars", "control", "diff", "diff_se", "holdout", "stable",
                     "by_window", "green_days"))
    _int(where + ".n", r["n"])
    _int(where + ".wins", r["wins"])
    if r["wins"] > r["n"]:
        raise ValueError("%s.wins is more than %s.n" % (where, where))
    _num(where + ".sum_r", r["sum_r"])
    _num(where + ".sum_r_gross", r["sum_r_gross"])
    _num(where + ".se", r["se"], none_ok=True)
    _int(where + ".days", r["days"])
    _int(where + ".longs", r["longs"])
    if r["longs"] > r["n"]:
        raise ValueError("%s.longs is more than %s.n" % (where, where))
    _num(where + ".avg_bars", r["avg_bars"], none_ok=True)
    control = _dict(where + ".control", r["control"])
    _need(where + ".control", control, ("n", "sum_r"))
    _int(where + ".control.n", control["n"])
    _num(where + ".control.sum_r", control["sum_r"])
    _num(where + ".diff", r["diff"], none_ok=True)
    _num(where + ".diff_se", r["diff_se"], none_ok=True)
    hold = _dict(where + ".holdout", r["holdout"])
    _need(where + ".holdout", hold, ("n", "mean_r"))
    _int(where + ".holdout.n", hold["n"])
    _num(where + ".holdout.mean_r", hold["mean_r"], none_ok=True)
    if r["stable"] is not None and not isinstance(r["stable"], bool):
        raise ValueError("%s.stable must be true, false or null" % where)
    windows = _dict(where + ".by_window", r["by_window"])
    for key, cell in windows.items():
        if key not in WINDOW_KEYS:
            raise ValueError("%s.by_window.%s is not one of %s"
                             % (where, key, ", ".join(WINDOW_KEYS)))
        if not isinstance(cell, list) or len(cell) != 3:
            raise ValueError("%s.by_window.%s must be [trades, wins, sum of R]" % (where, key))
        _int("%s.by_window.%s[0]" % (where, key), cell[0])
        _int("%s.by_window.%s[1]" % (where, key), cell[1])
        _num("%s.by_window.%s[2]" % (where, key), cell[2])
    green = r["green_days"]
    if not isinstance(green, list) or len(green) != 2:
        raise ValueError("%s.green_days must be [green days, days]" % where)
    _int(where + ".green_days[0]", green[0])
    _int(where + ".green_days[1]", green[1])
    if green[0] > green[1]:
        raise ValueError("%s.green_days has more green days than days" % where)
    if "avg_target_r" in r:
        _num(where + ".avg_target_r", r["avg_target_r"], none_ok=True)


def _example(where: str, ex) -> None:
    _dict(where, ex)
    _need(where, ex, ("t", "o", "h", "l", "c", "entry_i", "exit_i", "entry", "stop",
                      "target", "r", "direction"))
    size = None
    for key in ("t", "o", "h", "l", "c"):
        if not isinstance(ex[key], list):
            raise ValueError("%s.%s must be a list" % (where, key))
        if size is None:
            size = len(ex[key])
        elif len(ex[key]) != size:
            raise ValueError("%s.%s is not as long as %s.t" % (where, key, where))
        for k, v in enumerate(ex[key]):
            (_int if key == "t" else _num)("%s.%s[%d]" % (where, key, k), v)
    for key in ("entry_i", "exit_i"):
        _int(where + "." + key, ex[key])
        if size is not None and ex[key] >= size:
            raise ValueError("%s.%s is past the bars stored" % (where, key))
    for key in ("entry", "stop", "target", "r"):
        _num(where + "." + key, ex[key])
    if ex["direction"] not in (1, -1):
        raise ValueError("%s.direction must be 1 or -1" % where)


def validate_result(data) -> Dict:
    """Check one (symbol, interval) record of the Setup lab. Returns it."""
    _dict("result", data)
    _need("result", data, ("symbol", "interval", "group", "from", "to", "days", "bars",
                           "cost_bps", "k", "skipped_days", "method", "setups"))
    _str("result.symbol", data["symbol"])
    _str("result.interval", data["interval"])
    _str("result.group", data["group"])
    for key in ("from", "to", "days", "bars", "k"):
        _int("result." + key, data[key])
    _num("result.cost_bps", data["cost_bps"])
    skipped = _dict("result.skipped_days", data["skipped_days"])
    _need("result.skipped_days", skipped, ("incomplete", "roll"))
    for key, value in skipped.items():
        _int("result.skipped_days." + key, value)
    method = _dict("result.method", data["method"])
    _need("result.method", method, ("entry", "exit", "targets", "control"))
    for key in ("entry", "exit", "control"):
        _str("result.method." + key, method[key])
    if not isinstance(method["targets"], list) or not method["targets"]:
        raise ValueError("result.method.targets must be a list of words")
    for name, cell in _dict("result.setups", data["setups"]).items():
        where = "result.setups.%s" % name
        if name not in setups.BY_NAME:
            raise ValueError("%s is not a setup in setups.SETUPS" % where)
        _dict(where, cell)
        _need(where, cell, ("rules", "skipped"))
        rules = _dict(where + ".rules", cell["rules"])
        for rule in RULES:
            if rule not in rules:
                raise ValueError("%s.rules.%s is missing" % (where, rule))
            _rule("%s.rules.%s" % (where, rule), rules[rule])
        for rule in rules:
            if rule not in RULES:
                raise ValueError("%s.rules.%s is not one of %s" % (where, rule, ", ".join(RULES)))
        skip = _dict(where + ".skipped", cell["skipped"])
        _need(where + ".skipped", skip, ("tight_stop", "no_control"))
        for key, value in skip.items():
            _int("%s.skipped.%s" % (where, key), value)
        if cell.get("k3") is not None:
            k3 = _dict(where + ".k3", cell["k3"])
            for rule, r in k3.items():
                if rule not in RULES:
                    raise ValueError("%s.k3.%s is not one of %s" % (where, rule, ", ".join(RULES)))
                _dict("%s.k3.%s" % (where, rule), r)
                _need("%s.k3.%s" % (where, rule), r, ("n", "wins", "sum_r", "se"))
                _int("%s.k3.%s.n" % (where, rule), r["n"])
                _int("%s.k3.%s.wins" % (where, rule), r["wins"])
                _num("%s.k3.%s.sum_r" % (where, rule), r["sum_r"])
                _num("%s.k3.%s.se" % (where, rule), r["se"], none_ok=True)
        if cell.get("example") is not None:
            _example(where + ".example", cell["example"])
    return data


_COUNTS = ("trials", "hits", "base_trials", "base_hits")
_EXTRA_FIGURES = ("mean", "se", "base_mean", "base_se", "diff", "diff_se", "rate",
                  "base_rate", "low", "high")


def _counts(where: str, cell) -> None:
    _dict(where, cell)
    _need(where, cell, _COUNTS)
    for key in _COUNTS:
        _int("%s.%s" % (where, key), cell[key])
    if cell["hits"] > cell["trials"]:
        raise ValueError("%s.hits is more than %s.trials" % (where, where))
    if cell["base_hits"] > cell["base_trials"]:
        raise ValueError("%s.base_hits is more than %s.base_trials" % (where, where))
    for key in _EXTRA_FIGURES:
        if key in cell:
            _num("%s.%s" % (where, key), cell[key], none_ok=True)


def validate_claim(data) -> Dict:
    """Check one claim's record. Returns it.

    Counts are always there (trials and hits, and the control's). A claim
    measured as an average rather than a rate also carries mean and se (and
    base_mean, base_se) in its cells; "measure" says which ("rate" when absent).
    """
    _dict("claim", data)
    _need("claim", data, ("claim", "unit", "interval", "pooled", "by_symbol", "notes",
                          "verdict"))
    _str("claim.claim", data["claim"])
    if data["claim"] not in setups.CLAIMS_BY_ID:
        raise ValueError("claim.claim %r is not a claim in setups.CLAIMS" % data["claim"])
    _str("claim.unit", data["unit"])
    _str("claim.interval", data["interval"])
    _counts("claim.pooled", data["pooled"])
    for symbol, cell in _dict("claim.by_symbol", data["by_symbol"]).items():
        _counts("claim.by_symbol.%s" % symbol, cell)
    if not isinstance(data["notes"], list) or not all(isinstance(x, str) for x in data["notes"]):
        raise ValueError("claim.notes must be a list of sentences")
    if data["verdict"] not in (1, 0, -1, None) or isinstance(data["verdict"], bool):
        raise ValueError("claim.verdict must be 1, 0, -1 or null")
    if "measure" in data and data["measure"] not in ("rate", "mean", "descriptive"):
        raise ValueError("claim.measure must be rate, mean or descriptive")
    if "skipped" in data:
        for key, value in _dict("claim.skipped", data["skipped"]).items():
            _int("claim.skipped." + key, value)
    return data


_BIAS_KEYS = ("days", "called", "right", "up_days", "unclear", "target_reached", "from", "to")


def validate_bias(data) -> Dict:
    """Check one instrument's (or "_all") record of the bias study. Returns it."""
    _dict("bias", data)
    _need("bias", data, _BIAS_KEYS)
    for key in _BIAS_KEYS:
        _int("bias." + key, data[key])
    if data["right"] > data["called"]:
        raise ValueError("bias.right is more than bias.called")
    if data["called"] + data["unclear"] > data["days"]:
        raise ValueError("bias.called and bias.unclear add up to more than bias.days")
    if data["up_days"] > data["days"]:
        raise ValueError("bias.up_days is more than bias.days")
    if data["target_reached"] > data["called"]:
        raise ValueError("bias.target_reached is more than bias.called")
    return data


# --- reading and writing ---------------------------------------------------------------

def _dumps(data: Dict) -> str:
    return json.dumps(data, allow_nan=False, sort_keys=True)


def save_result(conn: sqlite3.Connection, data: Dict, now: Optional[int] = None) -> None:
    validate_result(data)
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO lab_results (symbol, interval, data, updated) "
                 "VALUES (?, ?, ?, ?)", (data["symbol"], data["interval"], _dumps(data),
                                         int(now if now is not None else time.time())))
    conn.commit()


def result(conn: sqlite3.Connection, symbol: str, interval: str) -> Optional[Dict]:
    ensure_schema(conn)
    row = conn.execute("SELECT data, updated FROM lab_results WHERE symbol=? AND interval=?",
                       (symbol, interval)).fetchone()
    return _row(row)


def results(conn: sqlite3.Connection, interval: Optional[str] = None) -> List[Dict]:
    """Every stored (symbol, interval) record, in setups.LAB_SYMBOLS order."""
    ensure_schema(conn)
    if interval:
        rows = conn.execute("SELECT data, updated FROM lab_results WHERE interval=?",
                            (interval,)).fetchall()
    else:
        rows = conn.execute("SELECT data, updated FROM lab_results").fetchall()
    out = [r for r in (_row(row) for row in rows) if r is not None]
    order = {sym: k for k, (sym, _, _) in enumerate(setups.LAB_SYMBOLS)}
    out.sort(key=lambda r: (order.get(r["symbol"], 99), r["symbol"],
                            setups.LAB_INTERVALS.index(r["interval"])
                            if r["interval"] in setups.LAB_INTERVALS else 99))
    return out


def save_claim(conn: sqlite3.Connection, data: Dict, now: Optional[int] = None) -> None:
    validate_claim(data)
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO lab_claims (claim, data, updated) VALUES (?, ?, ?)",
                 (data["claim"], _dumps(data), int(now if now is not None else time.time())))
    conn.commit()


def claim(conn: sqlite3.Connection, cid: str) -> Optional[Dict]:
    ensure_schema(conn)
    row = conn.execute("SELECT data, updated FROM lab_claims WHERE claim=?", (cid,)).fetchone()
    return _row(row)


def claims(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Every stored claim record, by claim id."""
    ensure_schema(conn)
    out = {}
    for row in conn.execute("SELECT data, updated FROM lab_claims"):
        rec = _row(row)
        if rec is not None:
            out[rec["claim"]] = rec
    return out


def save_bias(conn: sqlite3.Connection, symbol: str, data: Dict,
              now: Optional[int] = None) -> None:
    validate_bias(data)
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO bias_record (symbol, data, updated) VALUES (?, ?, ?)",
                 (symbol, _dumps(data), int(now if now is not None else time.time())))
    conn.commit()


def bias(conn: sqlite3.Connection, symbol: str) -> Optional[Dict]:
    """One instrument's bias record, or "_all" (POOL_ALL) for every one together."""
    ensure_schema(conn)
    row = conn.execute("SELECT data, updated FROM bias_record WHERE symbol=?",
                       (symbol,)).fetchone()
    return _row(row)


def save_run(conn: sqlite3.Connection, started: int, finished: Optional[int],
             summary: Dict) -> int:
    """Record one run of the lab; returns its id."""
    ensure_schema(conn)
    cur = conn.execute("INSERT INTO lab_runs (started, finished, summary) VALUES (?, ?, ?)",
                       (int(started), None if finished is None else int(finished),
                        json.dumps(summary, allow_nan=False, sort_keys=True)))
    conn.commit()
    return int(cur.lastrowid)


def last_run(conn: sqlite3.Connection) -> Optional[Dict]:
    """The newest run: {"id", "started", "finished", "summary"}, or None."""
    ensure_schema(conn)
    row = conn.execute("SELECT id, started, finished, summary FROM lab_runs "
                       "ORDER BY started DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        summary = json.loads(row[3]) if row[3] else {}
    except (TypeError, ValueError):
        summary = {}
    return {"id": int(row[0]), "started": int(row[1]),
            "finished": None if row[2] is None else int(row[2]), "summary": summary}


def _row(row) -> Optional[Dict]:
    """A stored record with its time ("updated"), or None if it cannot be read."""
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        data["updated"] = int(row[1])
        return data
    return None


# --- pooling ------------------------------------------------------------------------

def pooled(results_: Sequence[Dict], setup: str, rule: str,
           group: Optional[str] = None) -> Optional[Dict]:
    """One setup and target rule over several instruments, as one cell.

    Trades are summed and the margin of error combines each instrument's
    (their errors are added as if independent: instruments that follow the
    same index are not, so a page showing this says so). None when no
    instrument has a trade.
    """
    n = wins = days = longs = cn = 0
    sum_r = gross = csum = 0.0
    var = dvar = 0.0
    dsum = 0.0
    dn = 0
    have_var = have_dvar = True
    symbols = []
    for res in results_:
        if group is not None and res.get("group") != group:
            continue
        cell = ((res.get("setups") or {}).get(setup) or {}).get("rules", {}).get(rule)
        if not cell or not cell.get("n"):
            continue
        k = int(cell["n"])
        symbols.append(res.get("symbol"))
        n += k
        wins += int(cell["wins"])
        days += int(cell["days"])
        longs += int(cell["longs"])
        sum_r += float(cell["sum_r"])
        gross += float(cell["sum_r_gross"])
        if cell.get("se") is None:
            have_var = have_var and k < 2
        else:
            var += (float(cell["se"]) * k) ** 2
        control = cell.get("control") or {}
        cn += int(control.get("n") or 0)
        csum += float(control.get("sum_r") or 0.0)
        if cell.get("diff") is not None:
            dsum += float(cell["diff"]) * k
            dn += k
            if cell.get("diff_se") is None:
                have_dvar = False
            else:
                dvar += (float(cell["diff_se"]) * k) ** 2
    if n == 0:
        return None
    mean = sum_r / n
    se = math.sqrt(var) / n if have_var and n >= 2 else None
    diff = dsum / dn if dn else None
    diff_se = math.sqrt(dvar) / dn if dn and have_dvar else None
    return {"n": n, "wins": wins, "days": days, "longs": longs, "sum_r": sum_r,
            "sum_r_gross": gross, "mean": mean, "se": se,
            "verdict": verdict(mean, se, n, days=days),
            "control_n": cn, "control_mean": (csum / cn) if cn else None,
            "diff": diff, "diff_se": diff_se,
            "diff_verdict": verdict(diff, diff_se, dn, days=days),
            "symbols": symbols}


# --- fixtures, for tests only ------------------------------------------------------------

def _sample_rule(n: int, wins: int, sum_r: float, se: float, days: int) -> Dict:
    return {"n": n, "wins": wins, "sum_r": sum_r, "sum_r_gross": sum_r + 0.05 * n,
            "se": se, "days": days, "longs": n // 2, "avg_bars": 9.5,
            "control": {"n": n, "sum_r": sum_r - 0.8},
            "diff": 0.02, "diff_se": 0.18,
            "holdout": {"n": n // 3, "mean_r": -0.1}, "stable": True,
            "by_window": {"open_drive": [3, 1, -0.4], "macro": [5, 2, 0.6]},
            "green_days": [days // 2, days]}


def sample_result(symbol: str = "ES=F", interval: str = "5m") -> Dict:
    """A record shaped as the lab writes it, for tests. Never written outside them."""
    level = _sample_rule(41, 14, -2.6, 0.15, 30)
    level["avg_target_r"] = 2.6
    t = [1795000000 + 300 * k for k in range(70)]
    c = [6000.0 + (k % 7) - 3 for k in range(70)]
    return {"symbol": symbol, "interval": interval, "group": "index",
            "from": 1789000000, "to": 1795000000, "days": 59, "bars": 16500,
            "cost_bps": 1.0, "k": 2, "skipped_days": {"incomplete": 2, "roll": 1},
            "method": {"entry": "the close of the bar the setup appears on",
                       "exit": "the stop, the target, or the 16:00 New York close",
                       "targets": ["twice the risk", "the next old high or low"],
                       "control": "five random entries at the same time of day on other days"},
            "setups": {"liquidity_model": {
                "rules": {"2r": _sample_rule(41, 15, -3.2, 0.14, 30), "level": level},
                "skipped": {"tight_stop": 3, "no_control": 0},
                "k3": {"2r": {"n": 30, "wins": 11, "sum_r": -1.0, "se": 0.2}},
                "example": {"t": t, "o": [x - 0.5 for x in c], "h": [x + 1.0 for x in c],
                            "l": [x - 1.0 for x in c], "c": c, "entry_i": 45, "exit_i": 52,
                            "entry": c[45], "stop": c[45] - 4.0, "target": c[45] + 8.0,
                            "r": 1.0, "direction": 1}}}}


def sample_claim(cid: str = "london_takes_asia") -> Dict:
    """A claim record shaped as the lab writes it, for tests."""
    return {"claim": cid, "unit": setups.claim(cid).unit, "interval": "5m",
            "pooled": {"trials": 120, "hits": 54, "base_trials": 120, "base_hits": 50},
            "by_symbol": {"ES=F": {"trials": 40, "hits": 19, "base_trials": 40,
                                   "base_hits": 16}},
            "notes": ["Two days were left out: their Asia session was incomplete."],
            "verdict": 0, "skipped": {"incomplete": 2, "roll": 0}}


def sample_bias() -> Dict:
    """A bias study record shaped as the lab writes it, for tests."""
    return {"days": 312, "called": 190, "right": 101, "up_days": 162, "unclear": 122,
            "target_reached": 77, "from": 1732000000, "to": 1795000000}
