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

import io
import json
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
    UNIQUE (symbol, day)
);
"""


class ShareError(RuntimeError):
    """A share that could not be made, phrased for the person."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


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
    # The page leads with the counts, so the plain explanation's own opening
    # sentence saying the same thing is dropped rather than read twice.
    sure = plain.get("sure", "")
    said = ("The app looked at %d times this same setup happened before. "
            "It worked %d of those times." % (samples, worked))
    if sure.startswith(said):
        sure = sure[len(said):].strip()
    return {"symbol": bars.symbol,
            "name": NAMES.get(bars.symbol) or getattr(bars, "name", "") or "",
            "action": (plan.action or "").upper(),
            "headline": plain.get("headline", ""),
            "sure": sure,
            "points": list(plain.get("points") or [])[:3],
            "samples": samples, "worked": worked,
            "reliable": bool(plan.prob_reliable),
            "price": float(bars.last_price),
            "currency": getattr(bars, "currency", "") or "USD",
            "interval": bars.interval,
            "stop": price - stop_atr * atr if atr > 0 else None,
            "target": price + target_atr * atr if atr > 0 else None,
            "horizon": int(horizon),
            "at": int(time.time())}


def tests(report) -> Dict[str, List]:
    """Each strategy's back-test on one instrument: [times, worked, R kept].

    Kept with the popular snapshot rather than read from strategy_stats,
    because that table also takes the numbers from anyone's own run, under
    their own stop, target and costs. A public page has to show one method.
    """
    if report is None:
        return {}
    return {s.name: [s.n, s.wins, round(s.sum_r, 3)]
            for s in report.overall.values()}


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


def strategy_results(rows: List[Dict], name: str) -> List[Dict]:
    """How one strategy did on each popular instrument, best first."""
    out = []
    for v in rows:
        n, wins, kept = (v.get("tests") or {}).get(name, (0, 0, 0.0))
        if n >= MIN_TRADES:
            out.append({"symbol": v["symbol"], "name": v["name"], "n": n,
                        "wins": wins, "kept": kept / n})
    return sorted(out, key=lambda r: -r["kept"])


def strategy_summary(results: List[Dict]) -> Dict:
    """One strategy across every instrument it was tested on.

    Kept is pooled over trades, not averaged over instruments, so a symbol
    with twenty trades does not count as much as one with three hundred.
    """
    n = sum(r["n"] for r in results)
    return {"tested": len(results),
            "paid": sum(1 for r in results if r["kept"] > 0),
            "n": n, "wins": sum(r["wins"] for r in results),
            "kept": sum(r["kept"] * r["n"] for r in results) / n if n else 0.0}


def symbol_results(snap: Optional[Dict]) -> List[Dict]:
    """Every strategy tested on one instrument, best first."""
    out = [{"strategy": name, "n": n, "wins": wins, "kept": kept / n}
           for name, (n, wins, kept) in ((snap or {}).get("tests") or {}).items()
           if n >= MIN_TRADES]
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

    Each check also writes the day's call down and, given a way to fetch the
    daily bars, scores this symbol's earlier calls whose time has come.
    """
    checked, errors = 0, []
    for n, symbol in enumerate(due(conn, now)):
        if n:
            sleep(pause)
        try:
            snap = analyse(symbol)
            save_verdict(conn, snap)
            checked += 1
            record_call(conn, snap)
            if bars is not None:
                settle_calls(conn, symbol, bars(symbol))
        except Exception as exc:           # a delisted ticker must not stop the run
            errors.append("%s: %s" % (symbol, exc))
    return {"checked": checked, "errors": errors}


# --- the track record --------------------------------------------------------
#
# Every public call is written down and, once its horizon has passed, scored
# against what the price did, by the same pretend trade the back-tests use: a
# buy at the call's price with the house stop and target. A BUY was right if
# that trade paid; a WAIT or AVOID was right if it did not. Then it is left
# alone. A record that can be edited is a story.

HOW_WORDS = {"target": "rose to the target", "stop": "fell to the stop",
             "up": "ended higher", "down": "ended lower", "flat": "went nowhere"}


def record_call(conn: sqlite3.Connection, snap: Dict) -> bool:
    """Write today's call down, once per symbol per day. True if it was new."""
    if not (snap.get("action") and snap.get("price") and snap.get("stop")
            and snap.get("target")):
        return False
    ensure_schema(conn)
    day = time.strftime("%Y-%m-%d", time.gmtime(snap["at"]))
    cur = conn.execute(
        "INSERT OR IGNORE INTO calls (symbol, day, action, price, stop, target, "
        "horizon, at) VALUES (?,?,?,?,?,?,?,?)",
        (snap["symbol"], day, snap["action"], float(snap["price"]),
         float(snap["stop"]), float(snap["target"]), int(snap.get("horizon") or 10),
         int(snap["at"])))
    conn.commit()
    return cur.rowcount == 1


def _walk(bars, start: int, entry: float, stop: float, target: float,
          horizon: int):
    """What a buy at entry did over the bars from start: (how, exit price).

    Same rule as the back-tests: the stop is checked before the target inside
    a bar, because the order within a day is unknown and guessing the kind
    order flatters the result. (None, None) while the bars run out first.
    """
    n = len(bars.close)
    for k in range(start, min(start + horizon, n)):
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
    """Score this symbol's open calls whose horizon has passed. Returns how many."""
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM calls WHERE symbol=? AND outcome IS NULL "
                        "ORDER BY at", (symbol,)).fetchall()
    done = 0
    n = len(bars.timestamp)
    for row in rows:
        # The bar the call fell inside is skipped: part of it was already over.
        start = next((k for k in range(n) if int(bars.timestamp[k]) > row["at"]), n)
        how, exit_price = _walk(bars, start, row["price"], row["stop"], row["target"],
                                row["horizon"])
        if how is None:
            continue
        conn.execute("UPDATE calls SET outcome=?, how=?, exit_price=?, settled=? "
                     "WHERE id=?", (judge(row["action"], how), how, exit_price,
                                    int(time.time()), row["id"]))
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


def record(conn: sqlite3.Connection, limit: int = 40, now: Optional[float] = None) -> Dict:
    """The whole record: tallies for everyone, for buys, for the rest, and the
    latest settled calls."""
    ensure_schema(conn)
    now = now or time.time()
    rows = conn.execute("SELECT * FROM calls ORDER BY at").fetchall()
    settled = [dict(r) for r in rows if r["outcome"]]
    settled.sort(key=lambda r: -r["settled"])
    return {"total": len(rows),
            "first_at": rows[0]["at"] if rows else None,
            "all": _tally(rows),
            "buy": _tally([r for r in rows if r["action"] == "BUY"]),
            "hold": _tally([r for r in rows if r["action"] in ("WAIT", "AVOID")]),
            "month": _tally([r for r in rows if r["at"] >= now - 30 * 86400]),
            "latest": settled[:limit]}


def symbol_record(conn: sqlite3.Connection, symbol: str, limit: int = 6) -> Dict:
    """One stock's latest settled calls, and how many are still waiting."""
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM calls WHERE symbol=? ORDER BY at DESC",
                        (symbol,)).fetchall()
    return {"latest": [dict(r) for r in rows if r["outcome"]][:limit],
            "open": sum(1 for r in rows if not r["outcome"]),
            "tally": _tally(rows)}


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


def counts_line(snap: Dict) -> str:
    if not snap.get("samples"):
        return "Too few past examples to count."
    return ("Found this setup %d times before. It worked %d of them."
            % (snap["samples"], snap["worked"]))


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
    pen.text((MARGIN, max(y + 30, 470)), counts_line(snap),
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
