"""Pages anyone can read, and a picture of each to share.

Two kinds, drawn the same way:

  popular   the most-searched stocks and coins, checked once a day in the
            background and shown at /stock/<symbol>. These are for search
            engines and for people who have not signed up, so a visitor never
            makes the server run an analysis: the page reads what is stored.
  shared    what someone saw when they pressed Share, frozen with its date, at
            /s/<id>. Frozen on purpose: a link that changed its answer after
            it was posted would make the person who shared it look wrong.

Both get the same 1200x630 card, which is what Discord, iMessage, X and the
rest show when the link is pasted. A share carries the call, the counts and
the date, never who shared it.
"""
from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import secrets
import sqlite3
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

# What people search for, roughly in that order. Names are here rather than
# fetched so a page title never depends on a data source being up.
POPULAR = [
    ("NVDA", "NVIDIA"), ("AAPL", "Apple"), ("TSLA", "Tesla"),
    ("MSFT", "Microsoft"), ("AMZN", "Amazon"), ("META", "Meta"),
    ("GOOGL", "Alphabet (Google)"), ("AMD", "AMD"), ("PLTR", "Palantir"),
    ("NFLX", "Netflix"), ("AVGO", "Broadcom"), ("COIN", "Coinbase"),
    ("MSTR", "Strategy (MicroStrategy)"), ("SOFI", "SoFi"),
    ("HOOD", "Robinhood"), ("INTC", "Intel"), ("SMCI", "Super Micro Computer"),
    ("ARM", "Arm Holdings"), ("UBER", "Uber"), ("SHOP", "Shopify"),
    ("RIVN", "Rivian"), ("GME", "GameStop"), ("AMC", "AMC Entertainment"),
    ("DIS", "Disney"), ("NKE", "Nike"), ("BA", "Boeing"),
    ("JPM", "JPMorgan Chase"), ("BAC", "Bank of America"), ("WMT", "Walmart"),
    ("COST", "Costco"), ("KO", "Coca-Cola"), ("F", "Ford"), ("PYPL", "PayPal"),
    ("CRWD", "CrowdStrike"), ("SNOW", "Snowflake"), ("RDDT", "Reddit"),
    ("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq 100 ETF"),
    ("VOO", "Vanguard S&P 500 ETF"),
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
    ("XRP-USD", "XRP"), ("DOGE-USD", "Dogecoin"), ("ADA-USD", "Cardano"),
    ("AVAX-USD", "Avalanche"), ("LINK-USD", "Chainlink"), ("LTC-USD", "Litecoin"),
]
NAMES = dict(POPULAR)

# Look-alikes, so a page links to what its reader will check next: NVDA to
# AMD, BTC to ETH. A symbol in no group just gets the list order.
PEERS = [
    ("NVDA", "AMD", "AVGO", "INTC", "SMCI", "ARM"),
    ("AAPL", "MSFT", "AMZN", "META", "GOOGL", "NFLX"),
    ("TSLA", "RIVN", "F", "UBER"),
    ("PLTR", "CRWD", "SNOW", "SHOP", "RDDT"),
    ("COIN", "HOOD", "SOFI", "PYPL", "MSTR", "JPM", "BAC"),
    ("GME", "AMC"),
    ("DIS", "NKE", "BA", "WMT", "COST", "KO"),
    ("SPY", "QQQ", "VOO"),
    ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD",
     "AVAX-USD", "LINK-USD", "LTC-USD"),
]

# A strategy that fired fewer times than this on one instrument is left off
# its tables: a handful of trades is luck, and a page ranking luck would be
# telling people something it does not know.
MIN_TRADES = 40

# Daily bars close once a day, but the live bar moves all day with it, and a
# page a day old can contradict the front door's example from ten minutes
# ago. A few checks a day keeps them in step without leaning on the data
# source: 48 symbols, four times a day. The pause between checks keeps a run
# from looking like a burst to it.
MAX_AGE = 6 * 3600
PAUSE = 3.0

SHARES_PER_DAY = 30
SHARE_REUSE = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS public_verdicts (
    symbol   TEXT PRIMARY KEY,
    data     TEXT NOT NULL,
    updated  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
    id       TEXT PRIMARY KEY,
    user_id  INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    data     TEXT NOT NULL,
    created  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shares_user ON shares(user_id, created);
-- One public call a day per symbol, scored once its horizon has passed.
-- Never edited after that: a record you can change is a story.
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    day         TEXT NOT NULL,
    action      TEXT NOT NULL,
    price       REAL NOT NULL,
    stop        REAL NOT NULL,
    target      REAL NOT NULL,
    horizon     INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    outcome     TEXT,
    how         TEXT,
    exit_price  REAL,
    settled     INTEGER,
    price_day   INTEGER,
    excluded    TEXT,
    UNIQUE (symbol, day)
);
"""

# Columns added after the table first shipped. Existing databases get them by
# ALTER, never by rebuilding the table: the record is not to be rewritten.
#   price_day  the exchange-local trading day the quoted price belongs to,
#              so one market quote is one call however many days look at it.
#   excluded   why a call is left out of the scores. Set, never deleted.
_CALL_COLUMNS = (("price_day", "INTEGER"), ("excluded", "TEXT"))


class ShareError(RuntimeError):
    """A share that could not be made, phrased for the person."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    for name, kind in _CALL_COLUMNS:
        if name not in have:
            conn.execute("ALTER TABLE calls ADD COLUMN %s %s" % (name, kind))
    _session_key(conn)
    conn.commit()


def _session_key(conn: sqlite3.Connection) -> None:
    """One call per symbol per trading session, kept by the database itself.

    A unique index rather than a look before each write, so two checks that
    race cannot both write. Calls written before price_day existed have none,
    and SQLite lets any number of those through. The index cannot be built
    over two calls that already share a session, and they are never changed
    to make room for it; record_call's own look is then all there is.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_calls_session'").fetchone():
        return
    if conn.execute("SELECT 1 FROM calls WHERE price_day IS NOT NULL "
                    "GROUP BY symbol, price_day HAVING COUNT(*) > 1 LIMIT 1").fetchone():
        return
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_session "
                     "ON calls(symbol, price_day)")
    except sqlite3.IntegrityError:
        pass            # a second call for a session arrived in between


def short(symbol: str) -> str:
    """BTC-USD reads as BTC everywhere a person looks at it."""
    return symbol[:-4] if symbol.endswith("-USD") else symbol


def slug(symbol: str) -> str:
    return symbol.lower()


def from_slug(text: str) -> Optional[str]:
    symbol = (text or "").upper()
    return symbol if symbol in NAMES else None


def snapshot(result, plain: Dict, stop_atr: float = 1.5, target_atr: float = 2.25,
             horizon: int = 10) -> Dict:
    """The parts of an analysis worth showing a stranger, as plain data.

    The stop and target are where the house method would put them for a buy
    at this price. They are kept so the call can be scored later against what
    the price then did, by the same rule the back-tests use.
    """
    plan, bars = result.plan, result.bars
    ctx = getattr(result, "context", None)
    atr = 0.0
    if ctx is not None and len(getattr(ctx, "atr", ())):
        atr = float(ctx.atr[len(ctx.atr) - 1])
        if atr != atr:                      # NaN: no range to measure
            atr = 0.0
    price = float(bars.last_price)
    samples = int(plan.prob_samples or 0)
    worked = (int(round(plan.probability * samples))
              if plan.probability is not None and samples else 0)
    # How many separate tests those examples are worth: ones a few days apart
    # share most of their bars and mostly settle alike.
    n_eff = getattr(getattr(result, "calibration", None), "effective_samples", None)
    separate = int(round(n_eff)) if n_eff and samples and n_eff < samples - 0.5 else None
    # The page leads with the counts, so the plain explanation's own opening
    # sentence saying the same thing is dropped rather than read twice.
    from .plain import counted
    sure = plain.get("sure", "")
    said = counted(samples, worked)
    if sure.startswith(said):
        sure = sure[len(said):].strip()
    return {"symbol": bars.symbol,
            "name": NAMES.get(bars.symbol) or getattr(bars, "name", "") or "",
            "action": (plan.action or "").upper(),
            "headline": plain.get("headline", ""),
            "sure": sure,
            "points": list(plain.get("points") or [])[:3],
            "samples": samples, "worked": worked, "separate": separate,
            "reliable": bool(plan.prob_reliable),
            "price": float(bars.last_price),
            "currency": getattr(bars, "currency", "") or "USD",
            "interval": bars.interval,
            "stop": price - stop_atr * atr if atr > 0 else None,
            "target": price + target_atr * atr if atr > 0 else None,
            "horizon": int(horizon),
            "price_day": _price_day(bars),
            # Whether the price was still moving: record_call writes a
            # stock's call down only once its session is over.
            "market_open": getattr(bars, "market_open", None),
            "at": int(time.time())}


def _price_day(bars) -> Optional[int]:
    """The exchange-local trading day the quoted price belongs to.

    Not the calendar day the check ran: on a Saturday, a Sunday and the early
    hours (UTC) of a Monday an American stock's quote is still Friday's close.
    """
    meta = getattr(bars, "meta", None) or {}
    gmt = int(getattr(bars, "gmt_offset", 0) or 0)
    when = meta.get("regularMarketTime")
    if when:
        return (int(when) + gmt) // 86400
    ids = getattr(bars, "session_id", None)
    return int(ids[-1]) if ids is not None and len(ids) else None


def tests(report) -> Dict[str, List]:
    """Each strategy's back-test on one instrument:
    [times, worked, R kept, variance of R kept, separate trades].

    Kept with the popular snapshot rather than read from strategy_stats,
    because that table also takes the numbers from anyone's own run, under
    their own stop, target and costs. A public page has to show one method.
    """
    if report is None:
        return {}
    # Three figures, the shape the version before this one reads: rolled
    # back to, it still opens the strategy pages. The spread is kept apart.
    return {s.name: _test_entry(s)[:3] for s in report.overall.values()}


def spreads(report) -> Dict[str, List]:
    """Each strategy's [variance of R kept, separate trades], where measured,
    kept beside tests() in the popular snapshot under "spread"."""
    if report is None:
        return {}
    out = {}
    for s in report.overall.values():
        entry = _test_entry(s)
        if len(entry) > 3:
            out[s.name] = entry[3:5]
    return out


def _test_entry(stat) -> List:
    """One back-test as stored: the counts, then the spread when measured.

    The variance of R kept and the separate trades allow for trades that
    overlap in time (learning.StrategyStat.spread), and are what the margin
    of error on a strategy page comes from.
    """
    entry = [stat.n, stat.wins, round(stat.sum_r, 3)]
    spread = stat.spread() if hasattr(stat, "spread") else None
    if spread is not None:
        entry += [round(spread[0], 3), round(spread[1], 1)]
    return entry


def buy_every_day(ctx, stop_atr: float, target_atr: float, horizon: int,
                  stride: int = 3, cost_bps: float = 0.0,
                  max_samples: int = 1500) -> Optional[List]:
    """A buy on every day the strategies were tested on, whatever they said.

    The same days, stop, target, holding time and costs as
    learning.measure_strategies (whose cap on days is also 1500), stored like
    a strategy's test. Over years when a stock rose, buying it on any day
    kept money too, so a strategy that mostly bought shows skill only by
    beating this, not by beating zero.
    """
    from .calibrate import sample_points, simulate_trade
    from .learning import StrategyStat
    stat = StrategyStat(name="buy_every_day", family="", regime="all")
    for i in sample_points(ctx, horizon, stride, max_samples):
        res = simulate_trade(ctx, i, 1, stop_atr, target_atr, horizon, cost_bps)
        if res is None:
            continue
        stat.n += 1
        stat.wins += 1 if res[0] else 0
        stat.sum_r += res[1]
        stat.note(i, res[1], horizon)
    return _test_entry(stat) if stat.n else None


# --- the popular list --------------------------------------------------------

def save_verdict(conn: sqlite3.Connection, snap: Dict) -> None:
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO public_verdicts (symbol, data, updated) "
                 "VALUES (?, ?, ?)", (snap["symbol"], json.dumps(snap), snap["at"]))
    conn.commit()


def verdict(conn: sqlite3.Connection, symbol: str) -> Optional[Dict]:
    ensure_schema(conn)
    row = conn.execute("SELECT data FROM public_verdicts WHERE symbol=?",
                       (symbol,)).fetchone()
    return json.loads(row["data"]) if row else None


def verdicts(conn: sqlite3.Connection) -> List[Dict]:
    """Every popular instrument in list order, checked or not yet."""
    ensure_schema(conn)
    stored = {r["symbol"]: json.loads(r["data"]) for r in
              conn.execute("SELECT symbol, data FROM public_verdicts")}
    return [stored.get(sym) or {"symbol": sym, "name": name, "action": ""}
            for sym, name in POPULAR]


def related(symbol: str, rows: List[Dict], limit: int = 12) -> List[Dict]:
    """Other popular rows, look-alikes first."""
    group = next((g for g in PEERS if symbol in g), ())
    rest = [v for v in rows if v["symbol"] != symbol]
    return sorted(rest, key=lambda v: v["symbol"] not in group)[:limit]


# --- strategy tests ----------------------------------------------------------

_CAPS = {"rsi", "macd", "adx", "di", "sar", "tsi", "roc", "cci", "vwap",
         "obv", "mfi"}
_LABELS = {"zscore_reversion": "Z-score reversion",
           "linreg_channel": "Linear regression channel"}


def strategy_label(name: str) -> str:
    """rsi_reversion reads as RSI reversion."""
    if name in _LABELS:
        return _LABELS[name]
    words = [w.upper() if w in _CAPS else w for w in name.split("_")]
    return " ".join(words)[:1].upper() + " ".join(words)[1:]


def strategy_slug(name: str) -> str:
    return name.replace("_", "-")


def strategy_from_slug(text: str) -> Optional[str]:
    from .strategies import BY_NAME
    name = (text or "").lower().replace("-", "_")
    return name if name in BY_NAME else None


def _verdict(kept: float, se: Optional[float]) -> Optional[int]:
    """1 when the whole 95% range is above zero, -1 when it is all below,
    0 when it straddles zero, None when no range was measured."""
    if se is None:
        return None
    return 1 if kept - 2 * se > 0 else -1 if kept + 2 * se < 0 else 0


def _figures(entry, spread=None) -> Dict:
    """One stored back-test, [times, worked, R kept], as figures.

    spread is its [variance, separate trades], stored apart (spreads()); an
    entry of five figures, as buying every day is stored, carries its own.
    Tests stored without one show no range, and count every trade as
    separate, until the next check stores them again.
    """
    n, wins, kept_sum = int(entry[0]), int(entry[1]), float(entry[2])
    extra = list(spread) if spread else list(entry[3:5])
    var = float(extra[0]) if len(extra) > 0 and extra[0] is not None else None
    separate = float(extra[1]) if len(extra) > 1 and extra[1] is not None else float(n)
    kept = kept_sum / n if n else 0.0
    se = math.sqrt(var) / n if var is not None and n else None
    return {"n": n, "wins": wins, "kept": kept, "kept_sum": kept_sum, "var": var,
            "se": se, "verdict": _verdict(kept, se), "separate": separate}


def strategy_results(rows: List[Dict], name: str) -> List[Dict]:
    """How one strategy did on each popular instrument, best first, with
    what buying on every tested day kept there ("base")."""
    out = []
    for v in rows:
        entry = (v.get("tests") or {}).get(name)
        if not entry:
            continue
        fig = _figures(entry, (v.get("spread") or {}).get(name))
        if fig["separate"] < MIN_TRADES:
            continue
        base = _figures(v["baseline"]) if v.get("baseline") else None
        out.append(dict(fig, symbol=v["symbol"], name=v["name"],
                        base=base["kept"] if base else None))
    return sorted(out, key=lambda r: -r["kept"])


def baseline_results(rows: List[Dict]) -> List[Dict]:
    """Buying on every tested day, on each popular instrument."""
    out = []
    for v in rows:
        if v.get("baseline"):
            fig = _figures(v["baseline"])
            if fig["separate"] >= MIN_TRADES:
                out.append(dict(fig, symbol=v["symbol"], name=v["name"]))
    return out


def strategy_summary(results: List[Dict]) -> Dict:
    """One strategy across every instrument it was tested on.

    Kept is pooled over trades, not averaged over instruments, so a symbol
    with twenty trades does not count as much as one with three hundred. Its
    margin of error adds up the instruments' as if they moved apart; stocks
    that move together make the true one wider. "base" is what buying on
    every tested day kept on the same instruments, weighted the same way.
    """
    n = sum(r["n"] for r in results)
    kept = sum(r["kept_sum"] for r in results) / n if n else 0.0
    var = (sum(r["var"] for r in results)
           if results and all(r["var"] is not None for r in results) else None)
    se = math.sqrt(var) / n if var is not None and n else None
    based = [r for r in results if r.get("base") is not None]
    base = (sum(r["base"] * r["n"] for r in based) / n
            if n and based and len(based) == len(results) else None)
    return {"tested": len(results),
            "paid": sum(1 for r in results if r["kept"] > 0),
            "n": n, "wins": sum(r["wins"] for r in results), "kept": kept,
            "se": se, "verdict": _verdict(kept, se), "base": base,
            "separate": sum(r["separate"] for r in results)}


def symbol_results(snap: Optional[Dict]) -> List[Dict]:
    """Every strategy tested on one instrument, best first."""
    out = []
    spread = (snap or {}).get("spread") or {}
    for name, entry in ((snap or {}).get("tests") or {}).items():
        fig = _figures(entry, spread.get(name))
        if fig["separate"] >= MIN_TRADES:
            out.append(dict(fig, strategy=name))
    return sorted(out, key=lambda r: -r["kept"])


def due(conn: sqlite3.Connection, now: Optional[float] = None) -> List[str]:
    """Symbols to check, never-checked first, then the oldest."""
    ensure_schema(conn)
    now = now or time.time()
    when = {r["symbol"]: r["updated"] for r in
            conn.execute("SELECT symbol, updated FROM public_verdicts")}
    late = [s for s, _ in POPULAR if now - when.get(s, 0) >= MAX_AGE]
    return sorted(late, key=lambda s: when.get(s, 0))


def refresh(conn: sqlite3.Connection, analyse: Callable[[str], Dict],
            now: Optional[float] = None, pause: float = PAUSE,
            sleep: Callable[[float], None] = time.sleep,
            bars: Optional[Callable[[str], object]] = None) -> Dict:
    """Check everything that is due, one at a time. One failure skips one row.

    Each check also writes the session's call down when it is the first
    check after the close and, given a way to fetch the daily bars, marks
    any calls that cannot count and scores this symbol's earlier calls whose
    time has come.

    A coin's call is not written while the day before it is one the source
    left blank and market.py rebuilt (Bars.rebuilt): its range and volume
    are estimates, and the call would read them. The next check writes it,
    six hours on. Whether a call is skipped depends only on the data, never
    on the price, so it cannot pick the calls that flatter the record.
    """
    checked, errors = 0, []
    for n, symbol in enumerate(due(conn, now)):
        if n:
            sleep(pause)
        try:
            snap = analyse(symbol)
            save_verdict(conn, snap)
            checked += 1
            daily = bars(symbol) if bars is not None else None
            if daily is None or not _blank_yesterday(daily):
                record_call(conn, snap)
            if daily is not None:
                mark_calls(conn, symbol, daily)
                settle_calls(conn, symbol, daily)
        except Exception as exc:           # a delisted ticker must not stop the run
            errors.append("%s: %s" % (symbol, exc))
    return {"checked": checked, "errors": errors}


def blank_day(bars, now: Optional[float] = None) -> Optional[int]:
    """The newest finished day, when it is one the source left blank; else None.

    A coin's daily bar starts at midnight UTC, so its day is over a day
    later. fetch_bars splits the forming day off into the live price, which
    makes the newest bar the newest finished day; checked by the clock, so
    the answer is right whether the forming day was split off or not. Only
    that day counts: an older blank day no longer holds a call back.
    """
    blank = {int(b) for b in getattr(bars, "rebuilt", None) or ()}
    stamps = getattr(bars, "timestamp", None)
    if not blank or stamps is None or not len(stamps):
        return None
    now = time.time() if now is None else now
    for t in reversed([int(x) for x in stamps[-2:]]):
        if t + 86400 <= now:
            return t if t in blank else None
    return None


def _blank_yesterday(bars) -> bool:
    """True when the newest finished day is one the source left blank."""
    return blank_day(bars) is not None


# --- the track record --------------------------------------------------------
#
# Every public call is written down and scored against what the price did, by
# the same pretend trade the back-tests use: a buy at the call's price with
# the house stop and target, held until one is touched or the horizon ends.
# A BUY was right if that trade paid; a WAIT or AVOID was right if it did
# not. Then it is left alone. A record that can be edited is a story.

HOW_WORDS = {"target": "rose to the target", "stop": "fell to the stop",
             "up": "ended higher", "down": "ended lower", "flat": "went nowhere"}

# A call counts in the scores only once this many days have passed since it
# was made, by when every call made that day has had its full ten trading
# days, with room for two holidays and the next check. Before that, the calls
# already settled are mostly quick stop-outs, because the stop sits nearer
# than the target: on random prices, a daily AVOID read eleven days into the
# record was right on 62% of the calls settled by then, and on 58% once every
# call had settled.
MATURE_DAYS = 17

# Instruments that are one test, not two: both hold the S&P 500, and a call
# on one settles exactly as the call on the other does.
SAME_AS = {"VOO": "SPY"}

# A coin day the source left blank is rebuilt with its true open and close
# but only an estimate of its range (see market.py). A call whose window
# reaches one waits this long for the source to fill it in, usually an hour
# or two. If it never does, every call whose window holds that day is marked
# as not counting, scored or not, with the reason (mark_calls): marking only
# the ones still open kept those an early stop or target had settled and
# dropped the rest, so which calls counted depended on what the price did.
BLANK_WAIT_DAYS = 3


def _blank_reason(when) -> str:
    return ("The source never filled in the prices for %s, a day inside its "
            "window, so no call over that day counts, however it turned out."
            % _date(when))


def _blank_error(bars, row, start: int, now: float) -> Optional[str]:
    """Why a call cannot count: a day in its window the source never filled."""
    blank = {int(b) for b in getattr(bars, "rebuilt", None) or ()}
    if not blank:
        return None
    stamps = bars.timestamp
    for k in range(start, min(start + int(row["horizon"] or 10), len(stamps))):
        when = int(stamps[k])
        if when in blank and now - when >= BLANK_WAIT_DAYS * 86400:
            return _blank_reason(when)
    return None


def _is_coin(symbol: str) -> bool:
    return symbol.endswith("-USD")


def record_call(conn: sqlite3.Connection, snap: Dict) -> bool:
    """Write a call down, once per symbol per trading session. True if it was new.

    Keyed on the session the price belongs to (price_day), not the calendar
    day of the check: a weekend or a holiday sees the last session's close
    again, and writing it down again counted one observation two or three
    times. A unique index keeps it so.

    A stock's call is written only while its market is closed, so the call on
    record for a session is the first check after its close: entered at the
    official close and scored from the next session, the back-tests' own
    rule. Written during the session, it took a price still moving, and the
    scoring then skipped the rest of that session. A coin never closes; its
    call is the first check of each UTC day, at the price then.
    """
    if not (snap.get("action") and snap.get("price") and snap.get("stop")
            and snap.get("target")):
        return False
    symbol, price = snap["symbol"], float(snap["price"])
    if snap.get("market_open") and not _is_coin(symbol):
        return False
    ensure_schema(conn)
    price_day = snap.get("price_day")
    if price_day is not None and conn.execute(
            "SELECT 1 FROM calls WHERE symbol=? AND price_day=?",
            (symbol, int(price_day))).fetchone():
        return False
    # The calendar day of the check is still kept, and still unique. So when
    # a session's first check after the close falls on a UTC date that
    # already holds a call (the session before's, written just after
    # midnight, or on the day this rule took over, the morning call written
    # under the old one), the call waits for the next check after midnight:
    # still before the next open, at the same close.
    day = time.strftime("%Y-%m-%d", time.gmtime(snap["at"]))
    cur = conn.execute(
        "INSERT OR IGNORE INTO calls (symbol, day, action, price, stop, target, "
        "horizon, at, price_day) VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, day, snap["action"], price, float(snap["stop"]),
         float(snap["target"]), int(snap.get("horizon") or 10), int(snap["at"]),
         int(price_day) if price_day is not None else None))
    conn.commit()
    return cur.rowcount == 1


def _date(ts) -> str:
    return time.strftime("%d %b %Y", time.gmtime(int(ts))).lstrip("0")


def _bar_session(bars, k: int) -> int:
    """The exchange-local trading day of bar k."""
    ids = getattr(bars, "session_id", None)
    if ids is not None and len(ids) > k:
        return int(ids[k])
    return (int(bars.timestamp[k]) + int(getattr(bars, "gmt_offset", 0) or 0)) // 86400


def _split_factor(bars, at: int) -> float:
    """How many of today's shares one share was when the call was made.

    Yahoo rescales the prices before a split by its ratio, new shares per
    old (10.0 for a ten-for-one), so a price written down before a split is
    divided by the ratios of every split since to set it beside today's
    history. Bars without split events (market data before it carried them)
    give 1.0.
    """
    factor = 1.0
    for when, ratio in getattr(bars, "splits", None) or ():
        if int(when) > int(at) and ratio and float(ratio) > 0:
            factor *= float(ratio)
    return factor


def _split_error(bars, row, start: int) -> Optional[str]:
    """Why a scored call's stored result cannot count, if a split broke it.

    Calls scored before splits were allowed for compared their own prices
    with history Yahoo had already rescaled. Only a call with a split
    between its writing and its scoring can have been hit, and it is marked
    only when its stored result differs from the one allowing for the split.
    """
    if not row["outcome"] or not row["settled"] or not row["exit_price"]:
        return None
    inside = [int(when) for when, ratio in getattr(bars, "splits", None) or ()
              if ratio and row["at"] < int(when) <= row["settled"]]
    if not inside:
        return None
    f = _split_factor(bars, row["at"])
    how, exit_price = _walk(bars, start, row["price"] / f, row["stop"] / f,
                            row["target"] / f, row["horizon"])
    if how is None:
        return None
    if how == row["how"] and abs(exit_price * f / row["exit_price"] - 1.0) <= 0.01:
        return None
    return ("A stock split on %s fell inside its window and was not allowed for "
            "when it was scored. Allowing for it, the price %s."
            % (_date(min(inside)), HOW_WORDS[how]))


def mark_calls(conn: sqlite3.Connection, symbol: str, bars) -> int:
    """Mark, with the reason, this symbol's calls that cannot count. Returns how many.

    Run with the daily bars at hand, before scoring, for two reasons:

    * Scored on the same bars as an earlier call. Before calls were keyed by
      session, an American stock's Friday close was written down again on
      Saturday, Sunday and early Monday (UTC), and a holiday did the same:
      calls with no session between them, scored from the same next bar. The
      bars say this exactly where comparing prices did not, since the same
      close was stored as 675.99 one day and 675.989990234375 the next, and a
      new session can close at the price of an old one. The earliest call of
      each run is kept.
    * Scored across a stock split before splits were allowed for (see
      _split_error).
    * Its window holds a coin day the source never filled in, once it has had
      BLANK_WAIT_DAYS to (see _blank_error). Scored or not, by the data
      alone, so which calls count does not depend on how they turned out.

    A call whose next bar has not happened yet is left for a later run.
    Marked calls stay in place with their result, and the page lists them.
    Running it again marks nothing new.

    It also fills in price_day on calls written before it existed, from the
    bar the call's price came from, so every call can be dated by its
    session. Nothing else about a call is ever changed.
    """
    ensure_schema(conn)
    stamps = [int(t) for t in bars.timestamp]
    n = len(stamps)
    first: Dict[int, sqlite3.Row] = {}
    marked = 0
    now = time.time()
    for row in conn.execute("SELECT * FROM calls WHERE symbol=? AND excluded IS NULL "
                            "ORDER BY at, id", (symbol,)).fetchall():
        start = bisect.bisect_right(stamps, int(row["at"]))   # settle_calls' first bar
        if start >= n:
            continue
        earlier = first.get(start)
        reason = None
        if earlier is not None:
            reason = ("Scored on the same bars as the call written down on %s: "
                      "no market session between them." % _date(earlier["at"]))
        else:
            first[start] = row
            reason = _split_error(bars, row, start) or _blank_error(bars, row,
                                                                     start, now)
        if reason:
            conn.execute("UPDATE calls SET excluded=? WHERE id=? AND excluded IS NULL",
                         (reason, row["id"]))
            marked += 1
        elif row["price_day"] is None and start > 0:
            session = _bar_session(bars, start - 1)
            if not conn.execute("SELECT 1 FROM calls WHERE symbol=? AND price_day=?",
                                (symbol, session)).fetchone():
                conn.execute("UPDATE calls SET price_day=? WHERE id=? "
                             "AND price_day IS NULL", (session, row["id"]))
    conn.commit()
    return marked


def _walk(bars, start: int, entry: float, stop: float, target: float,
          horizon: int):
    """What a buy at entry did over the bars from start: (how, exit price).

    Same rule as the back-tests: the stop is checked before the target inside
    a bar, because the order within a day is unknown and guessing the kind
    order flatters the result. The open is the one price whose place is known,
    since it came first: a day that opened below the stop sold there, not at
    the stop, and one that opened above the target reached it. (None, None)
    while the bars run out first.
    """
    n = len(bars.close)
    opens = getattr(bars, "open", None)
    for k in range(start, min(start + horizon, n)):
        if opens is not None:
            first = float(opens[k])
            if first <= stop:
                return "stop", first
            if first >= target:
                return "target", target
        if float(bars.low[k]) <= stop:
            return "stop", stop
        if float(bars.high[k]) >= target:
            return "target", target
    if n < start + horizon:
        return None, None
    close = float(bars.close[start + horizon - 1])
    risk = entry - stop
    r = (close - entry) / risk if risk > 0 else 0.0
    return ("up" if r >= 0.5 else "down" if r <= -0.5 else "flat"), close


def judge(action: str, how: str) -> str:
    """right, wrong or flat, from what the call said and what a buy did."""
    if how == "flat":
        return "flat"
    worked = how in ("target", "up")
    if action == "BUY":
        return "right" if worked else "wrong"
    return "wrong" if worked else "right"      # WAIT, AVOID and SHORT all said: not a buy


def settle_calls(conn: sqlite3.Connection, symbol: str, bars) -> int:
    """Score this symbol's open calls whose horizon has passed. Returns how many.

    A call made before a stock split is walked with its prices divided by the
    split, since Yahoo has rescaled the history it is walked over, and its
    exit price is stored in its own prices again, beside its entry.
    """
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM calls WHERE symbol=? AND outcome IS NULL "
                        "ORDER BY at", (symbol,)).fetchall()
    done = 0
    n = len(bars.timestamp)
    blank = {int(b) for b in getattr(bars, "rebuilt", None) or ()}
    now = time.time()
    for row in rows:
        # The bar the call fell inside is skipped: part of it was already over.
        start = next((k for k in range(n) if int(bars.timestamp[k]) > row["at"]), n)
        f = _split_factor(bars, row["at"])
        entry, stop, target = row["price"] / f, row["stop"] / f, row["target"] / f
        how, exit_price = _walk(bars, start, entry, stop, target, row["horizon"])
        if how is None:
            continue
        excluded = None
        first_blank = next((k for k in range(start, min(start + row["horizon"], n))
                            if int(bars.timestamp[k]) in blank), None)
        if first_blank is not None:
            # A stop or target touched before the blank day is certain. From
            # it on, the estimated range can miss a touch the real one had.
            sure = (_walk(bars, start, entry, stop, target, first_blank - start)
                    if first_blank > start else (None, None))
            if sure[0] in ("stop", "target"):
                how, exit_price = sure
            elif now - int(bars.timestamp[first_blank]) < BLANK_WAIT_DAYS * 86400:
                continue
            else:
                excluded = _blank_reason(bars.timestamp[first_blank])
        exit_price *= f
        conn.execute("UPDATE calls SET outcome=?, how=?, exit_price=?, settled=? "
                     "WHERE id=?", (judge(row["action"], how), how, exit_price,
                                    int(now), row["id"]))
        if excluded:
            conn.execute("UPDATE calls SET excluded=? WHERE id=? AND excluded IS NULL",
                         (excluded, row["id"]))
        done += 1
    conn.commit()
    return done


def _tally(rows) -> Dict:
    out = {"right": 0, "wrong": 0, "flat": 0, "open": 0}
    for r in rows:
        out[r["outcome"] or "open"] += 1
    out["decided"] = out["right"] + out["wrong"]
    out["rate"] = out["right"] / out["decided"] if out["decided"] else None
    return out


def _cost(symbol: str, costs: Dict[str, float]) -> float:
    """Round-trip trading cost as a fraction of the price."""
    kind = "crypto" if symbol.endswith("-USD") else "equity"
    return float(costs.get(kind, 0.0))


def _buy_result(row, costs: Dict[str, float]) -> Dict[str, float]:
    """What the pretend buy behind a scored call returned, after costs."""
    cost = _cost(row["symbol"], costs)
    move = row["exit_price"] / row["price"] - 1.0 - cost
    risk = row["price"] - row["stop"]
    r = (row["exit_price"] - row["price"] - row["price"] * cost) / risk if risk > 0 else 0.0
    return {"move": move, "r": r}


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _separate_tests(rows) -> int:
    """How many of these calls are separate tests of the method.

    A call is scored over the next ten trading days, and the next day's call
    on the same stock shares nine of them: the two settle together far more
    often than not. Per stock, a call counts only once the last counted one's
    days have run out, and SPY and VOO count as one stock. Different stocks
    are still counted apart, though on a day the whole market moves they
    settle alike, so the true range is wider still.
    """
    last: Dict[str, int] = {}
    count = 0
    for r in sorted(rows, key=lambda r: r["at"]):
        days = int(r["horizon"] or 10)
        if not _is_coin(r["symbol"]):
            days = (days * 7 + 4) // 5          # trading days in calendar time
        key = SAME_AS.get(r["symbol"], r["symbol"])
        prev = last.get(key)
        if prev is None or int(r["at"]) - prev >= days * 86400:
            last[key] = int(r["at"])
            count += 1
    return count


def _stats(rows, costs: Dict[str, float]) -> Dict:
    """One kind of call: counts, right rate with its 95% range, and what a buy
    after these calls did, after costs.

    The range is computed on the calls that are separate tests, not on every
    call: counted as independent, a run of daily calls on one stock made the
    range one and a half to two times too narrow, and "beyond the margin of
    error" came too easily. The right rate itself is still over every call."""
    from .calibrate import wilson_interval
    out = _tally(rows)
    out["total"] = len(rows)
    out["scored"] = out["right"] + out["wrong"] + out["flat"]
    decided = [r for r in rows if r["outcome"] in ("right", "wrong")]
    out["separate"] = _separate_tests(decided)
    if out["decided"]:
        n_eff = float(max(1, min(out["separate"], out["decided"])))
        out["lo"], out["hi"] = wilson_interval(out["rate"] * n_eff, n_eff)
    else:
        out["lo"], out["hi"] = None, None
    results = [_buy_result(r, costs) for r in rows if r["outcome"]]
    moves = [x["move"] for x in results]
    rs = [x["r"] for x in results]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    out.update(mean_move=sum(moves) / len(moves) if moves else None,
               median_move=_median(moves),
               mean_r=sum(rs) / len(rs) if rs else None,
               avg_win_r=sum(wins) / len(wins) if wins else None,
               avg_loss_r=sum(losses) / len(losses) if losses else None)
    return out


CLASSES = (("BUY", "When it said BUY"), ("WAIT", "When it said WAIT"),
           ("AVOID", "When it said AVOID"), ("SHORT", "When it said SHORT"))


def record(conn: sqlite3.Connection, limit: int = 40, now: Optional[float] = None,
           costs: Optional[Dict[str, float]] = None) -> Dict:
    """The whole record, each kind of call scored on its own.

    There is no single headline "% right": a WAIT or AVOID is right whenever a
    buy would have lost, and with the stop nearer than the target a buy loses
    more often than it wins, so pooling them flatters the total. Instead every
    kind is set against the baseline, how often a buy at any of these calls
    worked, which is what saying the same thing every day would have scored.

    The scores count only calls made at least MATURE_DAYS ago, by when every
    one has had its full window. Newer calls already settled are counted
    apart, as "early": they are mostly the ones whose stop was touched first.
    costs are round-trip fractions by asset kind, e.g. {"equity": 0.0002}.
    """
    ensure_schema(conn)
    costs = costs or {}
    cutoff = int(now or time.time()) - MATURE_DAYS * 86400
    rows = [_dated(r) for r in conn.execute("SELECT * FROM calls ORDER BY at, id")]
    counted = [r for r in rows if not r["excluded"]]
    excluded = [r for r in rows if r["excluded"]]
    mature = [r for r in counted if r["at"] <= cutoff]
    scored = [r for r in mature if r["outcome"]]
    decided = [r for r in scored if r["how"] != "flat"]
    buy_worked = sum(1 for r in decided if r["how"] in ("target", "up"))
    # The baseline scores every call as if it had said BUY.
    base = _stats([dict(r, outcome=(None if r["how"] is None else
                                    "right" if r["how"] in ("target", "up") else
                                    "flat" if r["how"] == "flat" else "wrong"))
                   for r in mature], costs)
    classes = []
    for action, label in CLASSES:
        group = [r for r in mature if r["action"] == action]
        waiting = sum(1 for r in counted if r["action"] == action
                      and not (r["at"] <= cutoff and r["outcome"]))
        if group or waiting:
            stats = _stats(group, costs)
            # Chance level for this kind of call: what saying it every time
            # would have scored over the same calls.
            stats["chance"] = ((buy_worked / len(decided)) if action == "BUY" else
                               (1 - buy_worked / len(decided))) if decided else None
            classes.append(dict(stats, action=action, label=label, waiting=waiting))
    settled = [r for r in counted if r["outcome"]]
    latest = sorted(settled, key=lambda r: -(r["settled"] or 0))
    first_at = counted[0]["at"] if counted else None
    return {"total": len(counted),
            "first_at": first_at,
            "all": _tally(counted),
            "scored": len(scored),
            "early": sum(1 for r in settled if r["at"] > cutoff),
            "mature_days": MATURE_DAYS,
            "counts_from": first_at + MATURE_DAYS * 86400 if first_at else None,
            "classes": classes,
            "baseline": base,
            "buy_worked": (buy_worked / len(decided)) if decided else None,
            "excluded": excluded,
            "latest": latest[:limit]}


def _dated(row) -> Dict:
    """A call as a dict, with session_at: midnight UTC of the session its
    price came from, for dating it, or when it was written down if that is
    not known."""
    out = dict(row)
    out["session_at"] = (out["price_day"] * 86400 if out.get("price_day") is not None
                         else out["at"])
    return out


CSV_COLUMNS = ("id", "symbol", "session", "written_utc", "action", "price", "stop",
               "target", "horizon_days", "outcome", "price_did", "exit_price",
               "scored_utc", "left_out_because")


def calls_csv(conn: sqlite3.Connection) -> str:
    """Every call ever written down, left-out ones included, as CSV text.

    The table holds nobody's data, only symbols, calls, prices and times, so
    all of it can be published as it is. Prices are the call's own, before
    any later stock split.
    """
    ensure_schema(conn)

    def when(ts) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts))) if ts else ""

    def num(x) -> str:
        return "" if x is None else "%.10g" % float(x)

    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    out.writerow(CSV_COLUMNS)
    for r in conn.execute("SELECT * FROM calls ORDER BY at, id"):
        out.writerow([
            r["id"], r["symbol"],
            time.strftime("%Y-%m-%d", time.gmtime(r["price_day"] * 86400))
            if r["price_day"] is not None else "",
            when(r["at"]), r["action"], num(r["price"]), num(r["stop"]),
            num(r["target"]), r["horizon"], r["outcome"] or "open",
            HOW_WORDS.get(r["how"] or "", ""), num(r["exit_price"]),
            when(r["settled"]), r["excluded"] or ""])
    return buf.getvalue()


def symbol_record(conn: sqlite3.Connection, symbol: str, limit: int = 6) -> Dict:
    """One stock's latest settled calls, how many are still waiting, and the
    call on record for its latest session ("on_record").

    The page above the record is checked through the day and can say
    something else by the afternoon; on_record is the one being scored, with
    when it was written down ("written", in UTC).
    """
    ensure_schema(conn)
    rows = [_dated(r) for r in conn.execute(
        "SELECT * FROM calls WHERE symbol=? AND excluded IS NULL "
        "ORDER BY at DESC, id DESC", (symbol,))]
    on_record = rows[0] if rows else None
    if on_record is not None:
        on_record["written"] = time.strftime("%H:%M UTC on ", time.gmtime(
            on_record["at"])) + _date(on_record["at"])
    return {"latest": [r for r in rows if r["outcome"]][:limit],
            "open": sum(1 for r in rows if not r["outcome"]),
            "tally": _tally(rows),
            "on_record": on_record,
            "coin": _is_coin(symbol)}


# --- shares ------------------------------------------------------------------

def create_share(conn: sqlite3.Connection, user_id: int, snap: Dict,
                 now: Optional[float] = None) -> str:
    """Freeze what someone saw and return its id.

    Pressing Share twice on the same call gives the same link rather than a
    second page.
    """
    ensure_schema(conn)
    now = int(now or time.time())
    for row in conn.execute(
            "SELECT id, data FROM shares WHERE user_id=? AND symbol=? AND "
            "created>=? ORDER BY created DESC", (user_id, snap["symbol"],
                                                 now - SHARE_REUSE)):
        old = json.loads(row["data"])
        if (old.get("action"), old.get("interval")) == (snap["action"],
                                                       snap["interval"]):
            return row["id"]
    made = conn.execute("SELECT COUNT(*) FROM shares WHERE user_id=? AND "
                        "created>=?", (user_id, now - 86400)).fetchone()[0]
    if made >= SHARES_PER_DAY:
        raise ShareError("That is a lot of sharing for one day. Try again "
                         "tomorrow.")
    sid = secrets.token_urlsafe(6)
    conn.execute("INSERT INTO shares (id, user_id, symbol, data, created) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (sid, user_id, snap["symbol"], json.dumps(snap), now))
    conn.commit()
    return sid


def get_share(conn: sqlite3.Connection, sid: str) -> Optional[Dict]:
    ensure_schema(conn)
    row = conn.execute("SELECT data FROM shares WHERE id=?", (sid,)).fetchone()
    return json.loads(row["data"]) if row else None


# --- the card ----------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PAPER = (244, 243, 238)
INK = (25, 24, 23)
SOFT = (110, 106, 100)
FAINT = (150, 145, 138)
ACCENT = (201, 100, 66)
# The site's own chip colours: text and border, then the wash behind.
CHIPS = {"BUY": ((104, 119, 59), (237, 239, 226)),
         "SHORT": ((165, 62, 42), (247, 231, 226)),
         "WAIT": ((152, 101, 43), (246, 236, 221))}
MUTE = (SOFT, (232, 230, 224))

WIDTH, HEIGHT, MARGIN = 1200, 630, 84

_CARDS: "OrderedDict[str, bytes]" = OrderedDict()


def _font(name: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(os.path.join(HERE, "fonts", name), size)


def _wrap(text: str, font, width: int, lines: int) -> List[str]:
    out, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if font.getlength(trial) <= width or not line:
            line = trial
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    if len(out) > lines:
        out = out[:lines]
        while font.getlength(out[-1] + "…") > width and " " in out[-1]:
            out[-1] = out[-1].rsplit(" ", 1)[0]
        out[-1] += "…"
    return out


def counts_line(snap: Dict, short: bool = False) -> str:
    """The page's line about past examples, said as the test on past prices
    it is. short is for the picture card, which has room for one line of
    1,032 pixels: this fills 946 at 13 of 42, and at most 1,031 with four-figure
    counts, which is as large as a test of up to 2,500 sampled days goes."""
    if not snap.get("samples"):
        return "Too few past examples to count."
    if short:
        return ("Tested on past prices: it hit its target before its stop "
                "%d of %d times." % (snap["worked"], snap["samples"]))
    from .plain import counted
    line = counted(snap["samples"], snap["worked"])
    if snap.get("separate"):
        line += (" Many overlap in time, so together they are worth about %d "
                 "separate tests." % snap["separate"])
    return line


def render_card(snap: Dict) -> bytes:
    """The picture a pasted link unfurls to. Cached, since cards repeat."""
    key = json.dumps(snap, sort_keys=True)
    if key in _CARDS:
        _CARDS.move_to_end(key)
        return _CARDS[key]

    from PIL import Image, ImageDraw
    from .market import format_price

    card = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    pen = ImageDraw.Draw(card)
    pen.rectangle([0, 0, 10, HEIGHT], fill=ACCENT)

    mark = Image.open(os.path.join(ROOT, "static", "brand", "mark-1024.png"))
    mark = mark.convert("RGBA").resize((52, 52), Image.LANCZOS)
    card.paste(mark, (MARGIN, 52), mark)
    pen.text((MARGIN + 68, 50), "Orenth", font=_font("source-serif-4-600.ttf", 42),
             fill=INK)
    date = time.strftime("%d %b %Y", time.gmtime(snap.get("at") or time.time()))
    date = date.lstrip("0")
    small = _font("inter-400.ttf", 26)
    pen.text((WIDTH - MARGIN - small.getlength(date), 64), date, font=small,
             fill=FAINT)

    # The symbol, big, with the call beside it.
    big = _font("source-serif-4-600.ttf", 104)
    sym = short(snap["symbol"])
    pen.text((MARGIN, 140), sym, font=big, fill=INK)
    word = snap.get("action") or "?"
    ink, wash = CHIPS.get(word, MUTE)
    chip_font = _font("inter-600.ttf", 38)
    x = MARGIN + big.getlength(sym) + 36
    w = chip_font.getlength(word) + 56
    pen.rounded_rectangle([x, 176, x + w, 246], radius=35, fill=wash,
                          outline=ink, width=3)
    pen.text((x + 28, 187), word, font=chip_font, fill=ink)

    sub = snap.get("name") or ""
    if snap.get("price"):
        money = ("$" + format_price(snap["price"])
                 if snap.get("currency", "USD") == "USD"
                 else format_price(snap["price"], snap["currency"]))
        sub = "%s · %s" % (sub, money) if sub else money
    pen.text((MARGIN, 276), sub, font=_font("inter-400.ttf", 32), fill=SOFT)

    headline = _font("source-serif-4-600.ttf", 44)
    y = 344
    for line in _wrap(snap.get("headline", ""), headline, WIDTH - 2 * MARGIN, 2):
        pen.text((MARGIN, y), line, font=headline, fill=INK)
        y += 56
    pen.text((MARGIN, max(y + 30, 470)), counts_line(snap, short=True),
             font=_font("inter-400.ttf", 30), fill=INK)

    pen.text((MARGIN, 556), "orenth.app", font=_font("inter-600.ttf", 30),
             fill=ACCENT)
    note = "Not financial advice"
    pen.text((WIDTH - MARGIN - small.getlength(note), 562), note, font=small,
             fill=FAINT)

    buf = io.BytesIO()
    card.save(buf, "PNG", optimize=True)
    _CARDS[key] = buf.getvalue()
    while len(_CARDS) > 64:
        _CARDS.popitem(last=False)
    return _CARDS[key]
