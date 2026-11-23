"""What price is doing, for the analysis page: structure, key levels, the clock and the lean.

TJR's video ("How To Start Day Trading In 2026") reads a chart before the open
by marking levels (6:08:18-6:11:28): yesterday's high and low, the Asia and
London ranges, old highs and lows price has not traded through, gaps and the
halfway line of the last move, all in New York time. This module says the
same things about the bars an analysis already has, in plain words, beside
what Orenth measured about them.

Nothing here is a signal and nothing here changes the call. Every sentence
says what the price did or where a level is; the course's reading of it is
said as the course's ("the course reads this as ..."), and the daily lean is
shown beside its measured record (the bias study), never alone.

for_analysis() is the one entry point. It reads the structure series from
the analysis's own context when the strategy library already worked them out,
and works them out once (cached) otherwise. Hourly bars for the lean, and a
related market's bars for the pair check, are fetched with a five-minute
cache per symbol; any failure there becomes one plain sentence, never a
blank or an error on the page.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import charts, clock, labstore, liquidity, setups, structure
from . import sessions as sessions_mod
from .market import format_price

# Hourly bars for the lean, and a related market's bars, are kept this long.
CACHE_SECONDS = 300
# A fetch that failed is not tried again for this long.
FAIL_SECONDS = 60
# app choice: two months of hourly bars is about 1,000 to 1,400 bars, far more
# than the read needs (it looks back at most 300 hourly bars for an old high
# or low, and the 4-hour trend settles within a few weeks), and a quarter of a
# full download.
HOURLY_LOOKBACK = "60d"
WIDE_BARS, NARROW_BARS = 90, 40
MAX_MARKS = 8
# app choice: how many recent bars the pair check compares.
PAIR_BARS = 300

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")
_LOCK = threading.Lock()
_SERIES: "OrderedDict[tuple, Dict[str, np.ndarray]]" = OrderedDict()
_SERIES_KEEP = 6
_FETCHED: Dict[Tuple[str, str], Tuple[float, object]] = {}

UNAVAILABLE = ("The hourly prices this read needs could not be fetched just now, so "
               "there is no read of the day's lean here. Try again in a few minutes.")
TOO_FEW = "There are too few hourly prices yet to read the day's lean from."


# --- small helpers ---------------------------------------------------------------

def _ok(value) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


def _p(value) -> str:
    return format_price(float(value))


def _last_nonzero(arr: np.ndarray, end: int) -> int:
    hits = np.flatnonzero(np.asarray(arr[:end + 1]) != 0)
    return int(hits[-1]) if hits.size else -1


def _day_words(ordinal: int) -> str:
    d = date.fromordinal(int(ordinal))
    return "%d %s" % (d.day, _MONTHS[d.month - 1])


def _when(bars, s: Dict[str, np.ndarray], i: int, daily: bool) -> str:
    """When bar i was, as a person says it: '19 Nov at 10:05' (New York time),
    or '19 Nov' for a daily bar."""
    if daily:
        return _day_words(int(s["ck_day"][i]))
    day, _, hm = clock.label_ny(int(bars.timestamp[i])).rpartition(" ")
    return "%s at %s" % (day, hm)


def ny_day_label(ts) -> str:
    """A candle's name on an intraday chart: its start in New York time."""
    return clock.label_ny(int(ts))


def ny_as_of(bars) -> str:
    """When the latest price was set, in New York time ('19 Nov 10:02 New York
    time, market closed'), for intraday charts. Falls back to the alert
    module's wording when the source gave no time."""
    from . import alerts as alerts_mod
    meta = getattr(bars, "meta", None) or {}
    when = meta.get("regularMarketTime")
    if not when:
        return alerts_mod.as_of(bars)
    stamp = "%s New York time" % clock.label_ny(int(when))
    if not getattr(bars, "market_open", True):
        stamp += ", market closed"
    return stamp


def _prior_words(bars) -> Tuple[str, str]:
    """('Yesterday's', 'Yest.') for intraday and daily bars; for longer bars
    the bar before is not yesterday."""
    if clock.bar_seconds(bars) > clock.DAY:
        return "The previous bar's", "Prev."
    return "Yesterday's", "Yest."


# --- the series -----------------------------------------------------------------------

_WANTED = structure.KEYS + clock.KEYS + liquidity.KEYS


def series_for(result) -> Dict[str, np.ndarray]:
    """liquidity.series of the analysis's bars: from its context when the
    strategy library already worked them out, else worked out here once."""
    bars = result.bars
    n = len(bars.close)
    s = getattr(getattr(result, "context", None), "s", None)
    if isinstance(s, dict) and all(k in s and len(s[k]) == n for k in _WANTED):
        return s
    key = (bars.symbol, bars.interval, n, int(bars.timestamp[-1]) if n else 0,
           float(bars.close[-1]) if n else 0.0)
    with _LOCK:
        hit = _SERIES.get(key)
        if hit is not None:
            _SERIES.move_to_end(key)
            return hit
    s = liquidity.series(bars)
    with _LOCK:
        _SERIES[key] = s
        while len(_SERIES) > _SERIES_KEEP:
            _SERIES.popitem(last=False)
    return s


def _fetched(symbol: str, interval: str, fetch: Optional[Callable]):
    """Bars of another symbol or interval, kept CACHE_SECONDS. Raises what the
    fetch raised (kept FAIL_SECONDS, so a broken source is not asked again on
    every page)."""
    key = (symbol, interval)
    now = time.time()
    with _LOCK:
        hit = _FETCHED.get(key)
    if hit is not None:
        at, value = hit
        if isinstance(value, Exception):
            if now - at < FAIL_SECONDS:
                raise value
        elif now - at < CACHE_SECONDS:
            return value
    if fetch is None:
        from . import market as market_mod
        lookback = HOURLY_LOOKBACK if interval == "1h" else None
        fetch = lambda sym, iv: market_mod.fetch_bars(sym, iv, lookback)  # noqa: E731
    try:
        value = fetch(symbol, interval)
    except Exception as exc:
        with _LOCK:
            _FETCHED[key] = (now, exc)
        raise
    with _LOCK:
        if len(_FETCHED) > 200:
            _FETCHED.clear()
        _FETCHED[key] = (now, value)
    return value


def _open(db_path: Optional[str]):
    if not db_path:
        return None
    from . import database as db_mod
    return db_mod.connect(db_path)


# --- the whole panel ----------------------------------------------------------------------

def for_analysis(result, cfg: Dict, db_path: Optional[str] = None,
                 fetch: Optional[Callable] = None, now: Optional[float] = None) -> Dict:
    """Everything the "What price is doing" section shows, as plain data.

    {"kind", "intraday", "symbol", "interval", "words", "close",
     "clock": {...} | None, "structure": {"trend", "since", "flip_level",
     "last_flip", "last_take", "gaps", "eq", "points": [{"text", "help"}]},
     "levels": [{"name", "price", "side", "pct", "atr", "taken", "taken_at",
     "source"}], "bias": {...} | {"unavailable": "..."} | None,
     "chart": {"wide", "narrow", ...} | {}, "calm", "notes", "pair"}.

    fetch(symbol, interval) gets other bars (default market.fetch_bars); now
    is the time for the clock strip (default: the time now). Daily analyses
    get the structure and the levels only.
    """
    bars = result.bars
    kind = clock.kind_of(bars)
    intraday = kind != "daily"
    out: Dict = {"kind": kind, "intraday": intraday, "symbol": bars.symbol,
                 "interval": bars.interval, "words": charts.bar_words(bars.interval),
                 "close": None, "clock": None, "structure": None, "levels": [],
                 "bias": None, "chart": {}, "calm": bool((cfg or {}).get("calm_candles")),
                 "notes": [], "pair": None, "video": dict(setups.VIDEO)}
    n = len(bars.close)
    if n < 20:
        return out
    s = series_for(result)
    last = n - 1
    out["close"] = float(bars.close[last])
    days = clock.day_table(bars) if intraday else []
    out["structure"] = _structure(bars, s, kind)
    out["levels"] = _levels(bars, s, kind)
    out["notes"] = _notes(bars, s, kind, days)
    try:
        out["chart"] = _chart(bars, s, kind, days, out["calm"])
    except Exception:
        out["chart"] = {}
    if not intraday:
        return out

    now = time.time() if now is None else float(now)
    conn = None
    try:
        conn = _open(db_path)
    except Exception:
        conn = None
    try:
        try:
            out["clock"] = clock_strip(bars, kind, now, conn)
        except Exception:
            out["clock"] = None
        out["bias"] = _bias(bars, fetch, conn)
        try:
            out["pair"] = pair_check(bars, fetch, conn)
        except Exception:
            out["pair"] = None
    finally:
        if conn is not None:
            conn.close()
    return out


# --- what price is doing ------------------------------------------------------------------

def _event_at(bars, s: Dict[str, np.ndarray], i: int, kinds: Tuple[str, ...]) -> Optional[Dict]:
    """The event of one of these kinds on bar i, from liquidity.events, read on
    the bars up to i (so it is found however many came after)."""
    if i < 0:
        return None
    part = clock.head(bars, i + 1)
    cut = {key: arr[:i + 1] for key, arr in s.items()}
    for ev in liquidity.events(part, cut, limit=12):
        if ev["i"] == i and ev["kind"] in kinds:
            return ev
    return None


def _structure(bars, s: Dict[str, np.ndarray], kind: str) -> Dict:
    daily = kind == "daily"
    last = len(bars.close) - 1
    close = float(bars.close[last])
    words = charts.bar_words(bars.interval)
    points: List[Dict[str, str]] = []

    def say(text: str, key: str = "") -> None:
        points.append({"text": text, "help": key})

    t = int(s["st_trend"][last])
    since = ""
    flip_level = None
    if t:
        at = int(s["st_trend_at"][last])
        since = _when(bars, s, at, daily) if at >= 0 else ""
        say("On these %s bars the trend counts as %s%s: a close %s a swing %s set it, "
            "and only a close past the swing on the other side changes it."
            % (words, "up" if t == 1 else "down", (" since %s" % since) if since else "",
               "above" if t == 1 else "below", "high" if t == 1 else "low"), "structure")
        ref = s["st_ref_lo" if t == 1 else "st_ref_hi"][last]
        if _ok(ref):
            flip_level = float(ref)
            say("A close %s %s, the last swing %s, would turn it %s."
                % ("below" if t == 1 else "above", _p(ref), "low" if t == 1 else "high",
                   "down" if t == 1 else "up"), "flip_level")
    else:
        say("No close past a swing point has set a trend on these %s bars yet." % words,
            "structure")
        hi, lo = s["st_hi"][last], s["st_lo"][last]
        if _ok(hi) and _ok(lo):
            say("A close above %s, the last swing high, would set it up; a close below "
                "%s, the last swing low, would set it down." % (_p(hi), _p(lo)),
                "flip_level")

    # The last break of structure, either way, and the last old level taken.
    jb = _last_nonzero((s["st_flip"] != 0) | (s["st_ext"] != 0), last)
    last_flip = _event_at(bars, s, jb, ("flip", "break"))
    if last_flip:
        say(last_flip["words"], "structure")
    jt = _last_nonzero(s["lq_take"], last)
    last_take = _event_at(bars, s, jt, ("take",))
    if last_take:
        d = int(s["lq_take"][jt])
        level = float(s["lq_take_lvl"][jt])
        after = np.asarray(bars.close[jt + 1:last + 1], dtype=float)
        if bool(s["lq_take_back"][jt]):
            since_words = ""
        elif after.size and ((d == 1 and np.any(after > level)) or
                             (d == -1 and np.any(after < level))):
            since_words = " Price has closed back %s it since." % ("above" if d == 1 else "below")
        else:
            since_words = " Price has not closed back %s it since." % (
                "above" if d == 1 else "below")
        say(last_take["words"] + since_words, "sweep")

    # Gaps still standing, each way.
    gaps = []
    for side, top_k, bot_k, at_k in ((1, "st_fvg_up_top", "st_fvg_up_bot", "st_fvg_up_at"),
                                     (-1, "st_fvg_dn_top", "st_fvg_dn_bot", "st_fvg_dn_at")):
        top, bot, at = s[top_k][last], s[bot_k][last], int(s[at_k][last])
        if not (_ok(top) and _ok(bot)) or at < 0:
            continue
        top, bot = float(top), float(bot)
        start = max(0, at - 1)
        inside = bot <= close <= top
        gaps.append({"dir": side, "top": top, "bottom": bot, "at": int(bars.timestamp[start]),
                     "i": start, "inside": inside})
        say("A gap the %s left on %s, between %s and %s, still stands: price has not "
            "closed %s it.%s" % ("rise" if side == 1 else "fall",
                                 _when(bars, s, start, daily), _p(bot), _p(top),
                                 "below" if side == 1 else "above",
                                 " Price is inside it now." if inside else ""), "fvg")

    # A close through a gap since the trend began (the course's inverse gap).
    ji = _last_nonzero(s["st_ifvg"], last)
    if ji >= 0 and t and ji > int(s["st_trend_at"][last]):
        ev = _event_at(bars, s, ji, ("inverse_gap",))
        if ev:
            say(ev["words"] + " The trend still counts as %s: only a close past the last "
                "swing changes it." % ("up" if t == 1 else "down"), "ifvg")

    # The halfway line of the last move.
    eq = None
    line = s["st_eq"][last]
    if t and _ok(line):
        lo, hi = float(s["st_leg_lo"][last]), float(s["st_leg_hi"][last])
        upper = close > float(line)
        eq = {"price": float(line), "low": lo, "high": hi,
              "half": "upper" if upper else "lower"}
        say("The halfway line of the last %s, from %s to %s, is at %s. Price is in the %s "
            "half, which the course calls %s: it buys in the cheap half of a rise and sells "
            "in the dear half of a fall."
            % ("rise" if t == 1 else "fall", _p(lo if t == 1 else hi),
               _p(hi if t == 1 else lo), _p(line), "upper" if upper else "lower",
               "dear" if upper else "cheap"), "half_line")

    # Today's opening gap (not a coin's: it never shuts).
    if kind in ("future", "stock") and int(s["ck_gap_dir"][last]) != 0:
        d = int(s["ck_gap_dir"][last])
        lo, hi = float(s["ck_gap_lo"][last]), float(s["ck_gap_hi"][last])
        prev, opened = (lo, hi) if d == 1 else (hi, lo)
        start = _today_start(s, last)
        filled = np.flatnonzero(s["ck_gap_filled"][start:last + 1] != 0)
        when = ("price traded back to it on %s" % _when(bars, s, start + int(filled[0]), daily)
                if filled.size else "price has not traded back to it yet")
        say("This trading day opened at %s%s, %s the previous close of %s; %s."
            % (_p(opened), " at the 18:00 reopen" if kind == "future" else "",
               "above" if d == 1 else "below", _p(prev), when), "opening_gap")

    return {"trend": t, "since": since, "flip_level": flip_level, "last_flip": last_flip,
            "last_take": last_take, "gaps": gaps, "eq": eq, "points": points}


def _today_start(s: Dict[str, np.ndarray], last: int) -> int:
    day = s["ck_day"][:last + 1]
    same = np.flatnonzero(day != day[last])
    return int(same[-1]) + 1 if same.size else 0


def _week_start(s: Dict[str, np.ndarray], last: int) -> int:
    # Date ordinals count from Monday 1 January of year 1, so (ordinal - 1) // 7
    # is the same number for every day of one Monday-to-Sunday week.
    week = (np.asarray(s["ck_day"][:last + 1], dtype=np.int64) - 1) // 7
    other = np.flatnonzero(week != week[last])
    return int(other[-1]) + 1 if other.size else 0


def _first_past(bars, start: int, last: int, price: float, side: int) -> int:
    """The first bar from start to last that traded past a level: above it for
    a high (side +1), below it for a low (-1), through it for 0. -1 if none."""
    if start > last:
        return -1
    h = np.asarray(bars.high[start:last + 1], dtype=float)
    l_ = np.asarray(bars.low[start:last + 1], dtype=float)
    if side > 0:
        hits = np.flatnonzero(h > price)
    elif side < 0:
        hits = np.flatnonzero(l_ < price)
    else:
        hits = np.flatnonzero((l_ <= price) & (h >= price))
    return start + int(hits[0]) if hits.size else -1


def _opening_range(bars, s: Dict[str, np.ndarray], start: int, last: int):
    """Today's 9:30 to 10:00 New York range, once 10:00 has passed with at
    least 80% of its bars: (high, low, first bar after it), or None.

    app choice: the first 30 minutes, as the strategy library's opening range
    break uses; the course watches 9:30 to 9:50 (4:31:11), which the clock
    strip says.
    """
    step_min = clock.bar_seconds(bars) // 60
    if step_min <= 0 or step_min > 30:
        return None
    mins = np.asarray(s["ck_min"][start:last + 1], dtype=np.int64)
    inside = np.flatnonzero((mins >= 570) & (mins + step_min <= 600))
    after = np.flatnonzero((mins >= 600) & (mins < 17 * 60))
    if not after.size or inside.size < clock.COMPLETE_SHARE * (30 // step_min):
        return None
    idx = start + inside
    return (float(np.max(bars.high[idx])), float(np.min(bars.low[idx])),
            start + int(after[0]))


def _levels(bars, s: Dict[str, np.ndarray], kind: str) -> List[Dict]:
    """The key levels, each with its distance and whether it was taken."""
    daily = kind == "daily"
    last = len(bars.close) - 1
    close = float(bars.close[last])
    atr = float(s["st_atr"][last]) if _ok(s["st_atr"][last]) else 0.0
    today = _today_start(s, last)
    week = _week_start(s, last)
    prior, _ = _prior_words(bars)
    longer = clock.bar_seconds(bars) > clock.DAY
    rows: List[Dict] = []

    def add(name: str, price, source: str, start: Optional[int] = None, side: int = 0) -> None:
        if not _ok(price) or float(price) <= 0:
            return
        price = float(price)
        taken_i = _first_past(bars, start, last, price, side) if start is not None else -1
        rows.append({"name": name, "price": price, "source": source,
                     "side": "above" if price > close else "below" if price < close else "at",
                     "pct": (price - close) / close * 100.0 if close else 0.0,
                     "atr": abs(price - close) / atr if atr > 0 else None,
                     "taken": taken_i >= 0,
                     # A close is reached, not taken: there is nothing past it.
                     "taken_word": "Traded at" if side == 0 else "Taken",
                     "taken_at": _when(bars, s, taken_i, daily) if taken_i >= 0 else ""})

    add("%s high" % prior, s["ck_pdh"][last], "prior_day", today, 1)
    add("%s low" % prior, s["ck_pdl"][last], "prior_day", today, -1)
    add("%s close" % prior, s["ck_pdc"][last], "prior_day", today, 0)
    if not longer:
        add("Last week's high", s["ck_pwh"][last], "prior_week", week, 1)
        add("Last week's low", s["ck_pwl"][last], "prior_week", week, -1)
    if not daily:
        add("Today's high so far", s["ck_day_hi"][last], "today")
        add("Today's low so far", s["ck_day_lo"][last], "today")
    if kind in ("future", "coin"):
        for hi_k, lo_k, label in (("ck_asia_hi", "ck_asia_lo", "Asia session"),
                                  ("ck_lon_hi", "ck_lon_lo", "London session"),
                                  ("ck_pre_hi", "ck_pre_lo", "New York before the open")):
            known = np.flatnonzero(np.isfinite(s[hi_k][today:last + 1]))
            if not known.size:
                continue
            start = today + int(known[0])
            add("%s high" % label, s[hi_k][last], "session", start, 1)
            add("%s low" % label, s[lo_k][last], "session", start, -1)
    if not daily:
        rng = _opening_range(bars, s, today, last)
        if rng:
            add("Opening range high (9:30 to 10:00)", rng[0], "opening_range", rng[2], 1)
            add("Opening range low (9:30 to 10:00)", rng[1], "opening_range", rng[2], -1)
    swings = []
    for key in ("st_above1", "st_above2", "st_above3"):
        swings.append(("Old swing high", s[key][last]))
    for key in ("st_below1", "st_below2", "st_below3"):
        swings.append(("Old swing low", s[key][last]))
    for name, price in swings:
        add(name, price, "swing")
    # Equal highs (lows) are swing levels too: rename the matching row rather
    # than list the price twice.
    for key, name in (("st_eqh", "Equal highs"), ("st_eql", "Equal lows")):
        price = s[key][last]
        if not _ok(price):
            continue
        same = [r for r in rows if r["source"] == "swing" and abs(r["price"] - float(price))
                <= 1e-9 * max(1.0, abs(float(price)))]
        if same:
            same[0]["name"], same[0]["source"] = name, "equal"
        else:
            add(name, price, "equal")
    t = int(s["st_trend"][last])
    if t and _ok(s["st_eq"][last]):
        add("Halfway line of the last %s" % ("rise" if t == 1 else "fall"), s["st_eq"][last],
            "half")
    rows.sort(key=lambda r: -r["price"])
    return rows


def _notes(bars, s: Dict[str, np.ndarray], kind: str, days: List) -> List[str]:
    """What the levels leave out, and why, said where the numbers are."""
    notes: List[str] = []
    last = len(bars.close) - 1
    if kind == "stock":
        notes.append("A US stock's prices here cover 9:30 to 16:00 New York time only, so "
                      "it has no Asia or London ranges.")
    if kind in ("future", "coin", "stock") and len(days) >= 2:
        before = days[:-1]
        if not before[-1].complete:
            whole = [d for d in before if d.complete]
            if whole:
                notes.append("The price source is missing too many of %s's bars, so the "
                             "levels called yesterday's are from %s, the last whole day."
                             % (_day_words(before[-1].day.toordinal()),
                                _day_words(whole[-1].day.toordinal())))
            else:
                notes.append("No whole trading day before today is in the data, so "
                             "there are no levels from yesterday.")
    if kind in ("future", "coin") and days:
        today = days[-1]
        now_pos = (int(s["ck_min"][last]) - 18 * 60) % (24 * 60)
        for name, label, end in (("asia", "Asia", 3 * 60), ("london", "London", 8 * 60 + 30),
                                 ("ny_pre", "New York before-the-open", 9 * 60 + 30)):
            sess = today.sessions.get(name)
            if sess is None or sess.complete:
                continue
            if now_pos < (end - 18 * 60) % (24 * 60):
                continue                      # not over yet
            notes.append("The %s session's range is left out: the price source has %d of "
                         "its %d bars, under the 80%% a range needs."
                         % (label, sess.bars, sess.expected))
    if kind == "future" and int(s["ck_roll"][last]):
        notes.append("Today's prices jump where the futures contract switched to the next "
                     "one (or the data has a break), so levels from before the jump may not "
                     "line up with prices after it.")
    return notes


# --- the key-levels chart -------------------------------------------------------------------

def _chart(bars, s: Dict[str, np.ndarray], kind: str, days: List, calm: bool) -> Dict:
    from . import alerts as alerts_mod
    n = len(bars.close)
    last = n - 1
    daily = kind == "daily"
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    _, short = _prior_words(bars)
    t = int(s["st_trend"][last])

    def level(key):
        v = s[key][last]
        return float(v) if _ok(v) else None

    narrow_levels = {"%s high" % short: level("ck_pdh"), "%s low" % short: level("ck_pdl"),
                     "Old high": level("st_above1"), "Old low": level("st_below1")}
    wide_levels = dict(narrow_levels)
    wide_levels.update({"Equal highs": level("st_eqh"), "Equal lows": level("st_eql")})
    if t:
        wide_levels["Half line"] = level("st_eq")
    narrow_levels = {k: v for k, v in narrow_levels.items() if v is not None}
    wide_levels = {k: v for k, v in wide_levels.items() if v is not None}

    zones = []
    for side, top_k, bot_k, at_k in ((1, "st_fvg_up_top", "st_fvg_up_bot", "st_fvg_up_at"),
                                     (-1, "st_fvg_dn_top", "st_fvg_dn_bot", "st_fvg_dn_at")):
        top, bot, at = s[top_k][last], s[bot_k][last], int(s[at_k][last])
        if _ok(top) and _ok(bot) and at >= 1:
            zones.append((int(ts[at - 1]), None, float(top), float(bot),
                          "Gap the %s left, %s to %s" % ("rise" if side == 1 else "fall",
                                                         _p(bot), _p(top)), side))
    # The phone's one gap: the trend's own, else the one nearer the price.
    close = float(bars.close[last])
    narrow_zones = [z for z in zones if z[5] == t] or sorted(
        zones, key=lambda z: min(abs(close - z[2]), abs(close - z[3])))[:1]

    # Rings where an old high or low was taken: yesterday's, a session's or
    # equal ones, and any other the bar closed back inside (the course's
    # sweep), the latest MAX_MARKS. A ring on every swing taken in a steady
    # move would hide the candles.
    marks = []
    first = max(0, n - WIDE_BARS)
    for j in (np.flatnonzero(s["lq_take"][first:] != 0) + first)[::-1]:
        j = int(j)
        src = liquidity.SOURCES[int(s["lq_take_src"][j])]
        if not _ok(s["lq_take_lvl"][j]) or (src == "swing" and not s["lq_take_back"][j]):
            continue
        if len(marks) >= MAX_MARKS:
            break
        marks.append((int(ts[j]), float(s["lq_take_lvl"][j]), int(s["lq_take"][j]),
                      "Took %s at %s, %s" % (liquidity.level_name(s, j),
                                             _p(s["lq_take_lvl"][j]),
                                             _when(bars, s, j, daily))))

    bands = []
    if kind in ("future", "coin"):
        step = clock.bar_seconds(bars)
        for d in days:
            if d.last < first:
                continue
            for name, label in (("asia", "Asia"), ("london", "London")):
                sess = d.sessions.get(name)
                if sess is None or sess.first < 0:
                    continue
                bands.append((int(ts[sess.first]), int(ts[sess.last]) + step, label))

    day_label = None if daily else ny_day_label
    as_of = alerts_mod.as_of(bars) if daily else ny_as_of(bars)
    series = (bars.timestamp, bars.open, bars.high, bars.low, bars.close, bars.volume)
    common = dict(last=bars.last_price, as_of=as_of, day_label=day_label,
                  interval=bars.interval, calm=calm)
    wide = charts.trade_chart(*series, bars=WIDE_BARS, extra_levels=wide_levels, zones=zones,
                              marks=marks, bands=bands, **common)
    narrow = charts.trade_chart(*series, width=340, height=260, bars=NARROW_BARS,
                                extra_levels=narrow_levels, zones=narrow_zones, **common)
    if not wide:
        return {}
    # Told apart from the plan's chart on the same page by its own class.
    wide = wide.replace('class="chart trade"', 'class="chart trade keylevels"', 1)
    narrow = (narrow or wide).replace('class="chart trade"', 'class="chart trade keylevels"', 1)
    return {"wide": wide, "narrow": narrow, "as_of": as_of,
            "gaps": bool(zones), "marks": bool(marks), "bands": bool(bands),
            "short": short}


# --- the New York clock --------------------------------------------------------------------

def _hm(minute: int) -> str:
    return "%d:%02d" % (minute // 60, minute % 60)


_SESSION_WORDS = {
    "asia": "the Asia session (18:00 to 3:00)",
    "london": "the London session (3:00 to 8:30)",
    "ny_pre": "New York before the open (8:30 to 9:30)",
    "ny_am": "the New York morning (9:30 to 12:00)",
    "ny_lunch": "New York lunch (12:00 to 13:00)",
    "ny_pm": "the New York afternoon (13:00 to 16:00)",
    "ny_late": "the hour after the stock market closes (16:00 to 17:00)",
    "halt": "the futures' daily pause (17:00 to 18:00)",
}


def clock_strip(bars, kind: str, now: float, conn=None) -> Dict:
    """What time it is in New York, which session that is, and where the
    course's 9:30-10:30 windows stand; with what the Setup lab measured about
    entries at this time, when it has it."""
    day, minute, weekday = clock.ny_clock(now)
    session = clock.session_of(now)
    window = clock.window_of(now)
    words: List[str] = []
    open_now = True
    if kind == "future":
        shut = weekday == 5 or (weekday == 4 and minute >= 17 * 60) or \
            (weekday == 6 and minute < 18 * 60)
        if shut:
            open_now = False
            words.append("It is %s in New York. The futures market is shut for the "
                         "weekend and reopens on Sunday at 18:00." % _hm(minute))
        elif session == "halt":
            open_now = False
            words.append("It is %s in New York: the futures' daily pause, 17:00 to 18:00."
                         % _hm(minute))
    elif kind == "stock":
        hours = sessions_mod.us_session(day)
        if not hours:
            open_now = False
            words.append("It is %s in New York. The US stock market is shut today." % _hm(minute))
        elif not (hours[0] <= now < hours[1]):
            open_now = False
            words.append("It is %s in New York. The US stock market is shut now; it trades "
                         "from 9:30 New York time." % _hm(minute))
    if open_now:
        words.append("It is %s in New York: %s." % (_hm(minute), _SESSION_WORDS.get(
            session, "the trading day")))

    # video 4:31:11-4:37:38: the course's three marks after the open.
    if minute < 9 * 60 + 30 or minute >= 18 * 60:
        words.append("The course watches 9:30 to 9:50, looks for its entries from 9:50 to "
                     "10:10 and stops looking at 10:30.")
    elif window == "open_drive":
        words.append("This is 9:30 to 9:50, when the course watches for a run past an "
                     "earlier high or low and does not enter yet.")
    elif window == "macro":
        words.append("This is 9:50 to 10:10, the window the course takes its entries in.")
    elif window == "late_morning":
        words.append("This is 10:10 to 10:30, the last stretch before the course stops "
                     "looking at 10:30.")
    elif window == "after_cutoff":
        words.append("The course's 10:30 cut-off has passed: it does not look for new "
                     "trades after 10:30.")
    else:
        words.append("The New York session is over. The course's windows are 9:30 to 10:30 "
                     "New York time.")

    measured = _measured_window(bars, now, conn)
    return {"now": _hm(minute), "session": session,
            "session_label": clock.SESSION_LABELS.get(session, ""), "window": window,
            "window_label": clock.WINDOW_LABELS.get(window, "") if window else "",
            "open": open_now, "words": words, "measured": measured}


def _measured_window(bars, now: float, conn) -> List[str]:
    """What the Setup lab found about entries at this time of day."""
    if conn is None:
        return []
    out: List[str] = []
    lab = clock.lab_window_of(now)
    rec = labstore.result(conn, bars.symbol, bars.interval)
    cell = None
    if rec:
        rule = (((rec.get("setups") or {}).get("liquidity_model") or {}).get("rules")
                or {}).get("2r") or {}
        cell = (rule.get("by_window") or {}).get(lab)
    if cell:
        count, wins, sum_r = int(cell[0]), int(cell[1]), float(cell[2])
        where = "%s's %s bars" % (bars.symbol, charts.bar_words(bars.interval))
        when = _LAB_WHEN.get(lab, "at this time of day")
        if count < labstore.MIN_TRADES:
            out.append("In the Setup lab, %d sweep, break and retest trades on %s were "
                       "entered %s New York time: too few to say anything."
                       % (count, where, when))
        else:
            out.append("In the Setup lab, %d sweep, break and retest trades on %s were "
                       "entered %s New York time. %d kept money after costs, and on average "
                       "each kept %+.2f times its risk after costs. That is a count, not a "
                       "test: no margin of error is worked out for one window."
                       % (count, where, when, wins, sum_r / count))
    claim = labstore.claim(conn, "macro_window")
    if claim:
        out.append("Orenth tested the course's idea that %s%s Result: it %s."
                   % (setups.claim("macro_window").statement[:1].lower(),
                      setups.claim("macro_window").statement[1:],
                      labstore.claim_words(claim.get("verdict"))))
    return out


_LAB_WHEN = {"asia": "in the Asia session (18:00 to 03:00)",
             "london": "in the London session (03:00 to 08:30)",
             "ny_pre": "before the New York open (08:30 to 09:30)",
             "open_drive": "from 9:30 to 9:50", "macro": "from 9:50 to 10:10",
             "late_morning": "from 10:10 to 10:30", "midday": "from 10:30 to 12:00",
             "afternoon": "from 12:00 to 16:00", "after": "from 16:00 to 18:00"}


# --- the daily lean ----------------------------------------------------------------------------

def _bias(bars, fetch, conn) -> Dict:
    from . import bias as bias_mod
    try:
        if (bars.interval or "").lower() in ("1h", "60m"):
            hourly = bars
        else:
            hourly = _fetched(bars.symbol, "1h", fetch)
        read = bias_mod.read(hourly)
    except Exception:
        return {"unavailable": UNAVAILABLE}
    if read is None:
        return {"unavailable": TOO_FEW}
    lean = int(read.lean)
    flip_words = ""
    t1 = int(read.checks.get("trend_1h") or 0)
    side = lean or t1
    if read.flip_level is not None and _ok(read.flip_level) and side:
        flip_words = ("An hourly close %s %s would turn the hourly trend %s, and the course "
                      "would read the day again." % ("below" if side == 1 else "above",
                                                     _p(read.flip_level),
                                                     "down" if side == 1 else "up"))
    out = {"lean": lean,
           "word": "Leans up" if lean == 1 else "Leans down" if lean == -1 else "Unclear",
           "reasons": list(read.reasons),
           "targets": [{"name": name[:1].upper() + name[1:], "price": float(price)}
                       for name, price in read.targets if _ok(price)],
           "flip_level": float(read.flip_level) if _ok(read.flip_level) else None,
           "flip_words": flip_words,
           "as_of": clock.label_ny(int(read.as_of)),
           "record": None, "record_words": [], "pooled": False}
    out.update(bias_record(conn, bars.symbol))
    return out


def bias_record(conn, symbol: str) -> Dict:
    """How the read did on past days here (or pooled, when this instrument was
    not studied), in words, beside always guessing up and a coin flip."""
    if conn is None:
        return {"record": None, "record_words": [
            "The read has not been measured on past days yet, so take it as the course's "
            "method, not as a tested forecast."], "pooled": False}
    rec = labstore.bias(conn, symbol)
    pooled = False
    if rec is None:
        rec = labstore.bias(conn, labstore.POOL_ALL)
        pooled = rec is not None
    if rec is None or not rec.get("days"):
        return {"record": None, "record_words": [
            "The read has not been measured on past days yet, so take it as the course's "
            "method, not as a tested forecast."], "pooled": False}
    days, called, right = int(rec["days"]), int(rec["called"]), int(rec["right"])
    up = int(rec["up_days"])
    where = ("Across every instrument studied (this one was not)" if pooled
             else "For %s" % symbol)
    words = []
    if called:
        low, high = labstore.wilson(right, called)
        words.append("%s, on %d past trading days the read leaned one way on %d and was "
                     "right on %d (%d%%, with a 95%% range of %d%% to %d%%)."
                     % (where, days, called, right, round(100.0 * right / called),
                        round(100 * low), round(100 * high)))
    else:
        words.append("%s, on %d past trading days the read never leaned either way."
                     % (where, days))
    words.append("Always guessing up was right on %d of the %d days (%d%%); a coin flip is "
                  "right on half." % (up, days, round(100.0 * up / days)))
    v = labstore.rate_verdict(right, called, up, days)
    words.append({1: "So it called the day more often than always guessing up, by more than "
                     "its margin of error.",
                  -1: "So it called the day less often than always guessing up, by more "
                      "than its margin of error.",
                  0: "So it cannot be told apart from always guessing up."}.get(
                      v, "Too few days to say whether it beats guessing."))
    return {"record": {"days": days, "called": called, "right": right, "up_days": up,
                       "verdict": v}, "record_words": words, "pooled": pooled}


# --- two related markets ------------------------------------------------------------------------

def partner_of(symbol: str) -> Optional[str]:
    for a, b in setups.PAIRS:
        if symbol == a:
            return b
        if symbol == b:
            return a
    return None


def pair_check(bars, fetch=None, conn=None) -> Optional[Dict]:
    """Whether a related market (ES and NQ, SPY and QQQ, Bitcoin and Ether)
    made matching swing highs and lows lately, on the same bars. None when
    the instrument has no partner here, or its bars cannot be had."""
    partner = partner_of(bars.symbol)
    if partner is None or clock.kind_of(bars) == "daily":
        return None
    try:
        other = _fetched(partner, bars.interval, fetch)
    except Exception:
        return None
    a_ts = np.asarray(bars.timestamp, dtype=np.int64)
    b_ts = np.asarray(other.timestamp, dtype=np.int64)
    common, ia, ib = np.intersect1d(a_ts, b_ts, return_indices=True)
    if common.size < 30:
        return None
    ia, ib = ia[-PAIR_BARS:], ib[-PAIR_BARS:]
    rows = []
    for side, name in ((1, "high"), (-1, "low")):
        moves = []
        for sym, b, idx in ((bars.symbol, bars, ia), (partner, other, ib)):
            hi_idx, lo_idx = structure.pivots(np.asarray(b.high, float)[idx],
                                              np.asarray(b.low, float)[idx])
            pts = hi_idx if side == 1 else lo_idx
            if len(pts) < 2:
                moves = []
                break
            arr = np.asarray(b.high if side == 1 else b.low, float)[idx]
            moves.append((sym, float(arr[pts[-1]]) - float(arr[pts[-2]])))
        if len(moves) != 2:
            continue
        (sa, da), (sb, db_) = moves

        def what(delta):
            if delta > 0:
                return "a higher %s" % name
            if delta < 0:
                return "a lower %s" % name
            return "an equal %s" % name
        agree = (da > 0) == (db_ > 0) and (da < 0) == (db_ < 0)
        rows.append("At their last two swing %ss, %s made %s and %s made %s: %s."
                    % (name, sa, what(da), sb, what(db_),
                       "they agree" if agree else "they disagree"))
    if not rows:
        return None
    measured = "Orenth has not measured what follows when they disagree yet."
    if conn is not None:
        claim = labstore.claim(conn, "smt_at_sweep")
        if claim:
            measured = ("Orenth tested the course's idea that when two related markets "
                        "disagree at a high, both fall next (and the other way round at "
                        "lows): it %s." % labstore.claim_words(claim.get("verdict")))
    return {"partner": partner, "rows": rows, "measured": measured}
