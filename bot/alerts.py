"""Alerts: conditions worth being told about.

Checked on demand, and by the server's background loop (web.start_alert_loop),
which keeps going when nobody has the page open. Nothing here starts a thread
or sends mail itself: the loop calls check() and check_stops(), and deliver()
emails what fired, through whatever sender it is handed. The loop asks them to
leave alone what cannot have changed, a stock whose market has been closed
since its last good check (sessions.py); a check someone asks for checks all.

Each alert states its condition in plain terms and, when it fires, records the
value that triggered it. An alert that says only "AAPL alert triggered" is
useless an hour later.

An alert fires when its condition becomes true, not every time it is checked
while it stays true: the last answer is kept on the alert (its state), so one
that keeps watching speaks again only after the condition has cleared.
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np

from . import indicators as ind
from . import sessions
from .market import (DataError, fetch_bars, format_price, split_factor_since,
                     split_words, splits_since)

KINDS: Dict[str, Dict] = {
    "price_above": {"label": "Price rises above", "needs_threshold": True,
                    "unit": "price"},
    "price_below": {"label": "Price falls below", "needs_threshold": True,
                    "unit": "price"},
    "pct_up": {"label": "Rises by at least", "needs_threshold": True,
               "unit": "percent"},
    "pct_down": {"label": "Falls by at least", "needs_threshold": True,
                 "unit": "percent"},
    "rsi_above": {"label": "RSI goes above", "needs_threshold": True,
                  "unit": "level"},
    "rsi_below": {"label": "RSI falls below", "needs_threshold": True,
                  "unit": "level"},
    "near_52w_high": {"label": "Comes within", "needs_threshold": True,
                      "unit": "percent", "suffix": "of its 52-week high"},
    "near_52w_low": {"label": "Comes within", "needs_threshold": True,
                     "unit": "percent", "suffix": "of its 52-week low"},
    "volume_spike": {"label": "Trades on more than", "needs_threshold": True,
                     "unit": "multiple", "suffix": "its normal volume"},
    "crosses_ma": {"label": "Closes above its 50-bar average",
                   "needs_threshold": False, "unit": ""},
    "loses_ma": {"label": "Closes below its 50-bar average",
                 "needs_threshold": False, "unit": ""},
    # Calls rather than prices: the app's own BUY / WAIT / AVOID for the
    # instrument, worked out with the standard settings.
    "call_changes": {"label": "Call changes (for example WAIT to BUY)",
                     "needs_threshold": False, "unit": "", "call": True},
    "call_buy": {"label": "Call turns BUY", "needs_threshold": False, "unit": "",
                 "call": True},
}

CALL_KINDS = tuple(k for k, spec in KINDS.items() if spec.get("call"))

# Tested on daily bars whatever bar size the alert was set on. A 52-week high
# is a year of trading, and 252 five-minute bars are three days.
DAILY_ONLY = ("near_52w_high", "near_52w_low")

# How much history each check downloads: enough for every reading, no more. A
# year of daily bars holds the 52-week range, the 50-bar average and a settled
# 14-bar RSI; on shorter bars a few days or weeks hold over a hundred bars.
# Without it every check, every five minutes, pulled the source's whole range:
# five years of daily bars, or sixty days of five-minute ones.
LOOKBACK = {"1d": "1y", "1h": "1mo", "60m": "1mo", "30m": "1mo",
            "15m": "5d", "5m": "5d", "2m": "5d", "1m": "5d"}


def problem(kind: str, threshold: Optional[float]) -> Optional[str]:
    """Why a value cannot work for this kind of alert, or None if it can.

    Percents are fractions here: 0.05 is 5%. Checked when an alert is saved,
    and before one is tested, so an alert saved before these rules says what
    is wrong with it instead of firing on a value that means nothing.
    """
    spec = KINDS.get(kind)
    if spec is None:
        return "Unknown alert type %r." % kind
    if not spec["needs_threshold"]:
        return None
    if threshold is None:
        return "%s needs a value to compare against." % spec["label"]
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return "The alert value must be a number."
    if not math.isfinite(value):
        return "The alert value must be an ordinary number."
    unit = spec["unit"]
    if unit == "price" and value <= 0:
        return "A price alert needs a positive price."
    if unit == "percent" and not 0 < value < 1:
        return "A percent needs to be more than 0 and less than 100."
    if unit == "level" and not 0 < value < 100:
        return "RSI runs from 0 to 100, so the level needs to be between them."
    if unit == "multiple" and value <= 1:
        return "A volume multiple needs to be more than 1, which is normal volume."
    return None


def _fraction(threshold) -> float:
    """A stored percent as a fraction.

    Alerts store 0.05 for 5%. A few saved before that rule hold the whole
    number, and anything from 1 up has always been read that way.
    """
    value = float(threshold or 0)
    return value if value < 1 else value / 100.0


def _bars_for(row) -> str:
    """The bar size an alert is tested on."""
    return "1d" if row["kind"] in DAILY_ONLY else (row["interval"] or "1d")


def _state(row) -> Optional[str]:
    return row["state"] if "state" in row.keys() else None


@dataclass
class Fired:
    """One alert whose condition was met."""
    alert_id: int
    symbol: str
    kind: str
    threshold: Optional[float]
    value: float
    message: str
    note: str = ""
    user_id: Optional[int] = None
    as_of: str = ""
    trade_id: Optional[int] = None
    event_id: Optional[int] = None


def describe(kind: str, threshold: Optional[float], symbol: str) -> str:
    """Plain sentence for an alert, used everywhere it is displayed."""
    spec = KINDS.get(kind)
    if not spec:
        return "%s: unknown condition" % symbol
    if not spec["needs_threshold"]:
        return "%s %s" % (symbol, spec["label"].lower())
    unit = spec["unit"]
    if unit == "percent":
        shown = "%.1f%%" % (threshold * 100 if threshold and threshold < 1 else threshold or 0)
    elif unit == "multiple":
        shown = "%.1fx" % (threshold or 0)
    elif unit == "price":
        shown = format_price(threshold)
    else:
        shown = "%.0f" % (threshold or 0)
    text = "%s %s %s" % (symbol, spec["label"].lower(), shown)
    if spec.get("suffix"):
        text += " " + spec["suffix"]
    # "rsi" reads wrong in lower case; it is an abbreviation, not a word.
    return text.replace(" rsi ", " RSI ")


def as_of(bars) -> str:
    """When the price behind a message was set, said with the message.

    An alert that says "above 200" without saying when that was true is
    unreadable an hour later, and on a closed market it may be Friday's.
    """
    meta = getattr(bars, "meta", None) or {}
    when = meta.get("regularMarketTime")
    if when:
        stamp = time.strftime("%d %b %H:%M UTC", time.gmtime(int(when))).lstrip("0")
    else:
        stamp = getattr(bars, "last_bar_time", "") or ""
    if stamp and not getattr(bars, "market_open", True):
        stamp += ", market closed"
    return stamp


def _day(ts: int) -> str:
    return time.strftime("%d %b", time.gmtime(ts)).lstrip("0")


def _bar(bars, at_close: bool = False) -> str:
    """The last completed bar in words, so a reading says which bar it is.

    RSI, volume and the moving average are read from completed bars only. On
    a daily chart that can be yesterday's session, so a message in the
    present tense with no date would describe the wrong day.
    """
    try:
        start = int(bars.timestamp[-1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return ""
    if (getattr(bars, "interval", "") or "1d") == "1d":
        # A daily bar is a trading day, named in the exchange's own calendar.
        day = _day(start + int(getattr(bars, "gmt_offset", 0) or 0))
        return ("at the %s close" if at_close else "on %s") % day
    when = "%s UTC bar on %s" % (time.strftime("%H:%M", time.gmtime(start)), _day(start))
    return ("at the close of the " if at_close else "in the ") + when


def _year_range(bars):
    """The highest high and lowest low of the last year of daily bars.

    A year of dates, not 252 bars: crypto trades every day of the week. Also
    returns the date the window starts, which is later than a year ago when
    the data does not go back that far.
    """
    close = np.asarray(bars.close, dtype=float)
    highs = np.asarray(getattr(bars, "high", close), dtype=float)
    lows = np.asarray(getattr(bars, "low", close), dtype=float)
    stamps = getattr(bars, "timestamp", None)
    if stamps is not None and len(stamps) == len(close):
        stamps = np.asarray(stamps, dtype=float)
        keep = stamps >= stamps[-1] - 365 * 86400
        start = int(stamps[keep][0])
        whole_year = start <= stamps[-1] - 350 * 86400 or not keep.all()
    else:
        keep = np.arange(len(close)) >= len(close) - 252
        start, whole_year = None, len(close) >= 252
    return (float(np.nanmax(highs[keep])), float(np.nanmin(lows[keep])),
            whole_year, start)


def _owner(row) -> Optional[int]:
    try:
        return int(row["user_id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _evaluate(row, bars, call: Optional[str] = None) -> Optional[Fired]:
    """Test one alert against fresh bars. Returns None when not triggered.

    call is the instrument's current BUY / WAIT / AVOID, for the call kinds.
    """
    kind = row["kind"]
    threshold = row["threshold"]
    symbol = row["symbol"]
    price = bars.last_price
    close = np.asarray(bars.close, dtype=float)

    def fire(value, message):
        return Fired(int(row["id"]), symbol, kind, threshold, value, message,
                     row["note"] or "", _owner(row), as_of(bars))

    if kind in CALL_KINDS:
        was = _state(row)
        # The first look only learns the call. A change needs a before.
        if not call or not was or call == was:
            return None
        if kind == "call_buy" and call != "BUY":
            return None
        return fire(price, "%s's call changed from %s to %s, at %s."
                    % (symbol, was, call, format_price(price)))

    if kind == "price_above" and price > threshold:
        return fire(price, "%s is %s, above the %s you set."
                    % (symbol, format_price(price), format_price(threshold)))

    if kind == "price_below" and price < threshold:
        return fire(price, "%s is %s, below the %s you set."
                    % (symbol, format_price(price), format_price(threshold)))

    if kind in ("pct_up", "pct_down"):
        # Measured against the previous session close, which is what "up today"
        # means to everybody except a chart with a different anchor.
        base = bars.prev_close
        if not base:
            return None
        move = (price - base) / base
        pct = _fraction(threshold)
        if kind == "pct_up" and move >= pct:
            return fire(move, "%s is up %.2f%% since the previous close."
                        % (symbol, move * 100))
        if kind == "pct_down" and move <= -pct:
            return fire(move, "%s is down %.2f%% since the previous close."
                        % (symbol, abs(move) * 100))
        return None

    if kind in ("rsi_above", "rsi_below"):
        rsi = ind.rsi(close, 14)
        if not len(rsi) or not np.isfinite(rsi[-1]):
            return None
        value = float(rsi[-1])
        when = _bar(bars, at_close=True)
        when = " " + when if when else ""
        if kind == "rsi_above" and value > threshold:
            return fire(value, "%s RSI is %.0f%s, above %.0f."
                        % (symbol, value, when, threshold))
        if kind == "rsi_below" and value < threshold:
            return fire(value, "%s RSI is %.0f%s, below %.0f."
                        % (symbol, value, when, threshold))
        return None

    if kind in ("near_52w_high", "near_52w_low"):
        # check() hands these daily bars whatever the alert's own bar size.
        if len(close) < 20:
            return None
        high, low, whole_year, start = _year_range(bars)
        pct = _fraction(threshold)
        extreme = high if kind == "near_52w_high" else low
        if not extreme:
            return None
        if whole_year:
            where = "its 52-week %s of %s" % ("high" if kind == "near_52w_high" else "low",
                                             format_price(extreme))
        else:
            where = "%s, its %s since %s (the data goes back no further)" % (
                format_price(extreme), "high" if kind == "near_52w_high" else "low",
                time.strftime("%d %b %Y", time.gmtime(start)).lstrip("0")
                if start else "the data starts")
        if kind == "near_52w_high":
            if price > high:
                return fire((high - price) / high, "%s at %s is above %s: a new high."
                            % (symbol, format_price(price), where))
            gap = (high - price) / high
        else:
            if price < low:
                return fire((price - low) / low, "%s at %s is below %s: a new low."
                            % (symbol, format_price(price), where))
            gap = (price - low) / low
        if gap <= pct:
            return fire(gap, "%s at %s is within %.1f%% of %s."
                        % (symbol, format_price(price), gap * 100, where))
        return None

    if kind == "volume_spike":
        volume = np.asarray(bars.volume, dtype=float)
        if len(volume) < 21 or not np.isfinite(volume[-1]):
            return None
        # The last completed bar against the 20 before it. An average that
        # includes the bar itself needs a bigger spike to say the same thing.
        normal = float(np.nanmean(volume[-21:-1]))
        if not np.isfinite(normal) or normal <= 0:
            return None
        value = float(volume[-1]) / normal
        if value >= threshold:
            when = _bar(bars)
            daily = (getattr(bars, "interval", "") or "1d") == "1d"
            return fire(value, "%s traded %.1f times its normal volume%s, against "
                               "the average of the 20 %s before."
                        % (symbol, value, " " + when if when else "",
                           "sessions" if daily else "bars"))
        return None

    if kind in ("crosses_ma", "loses_ma"):
        ma = ind.sma(close, 50)
        if len(ma) < 2 or not np.isfinite(ma[-1]) or not np.isfinite(ma[-2]):
            return None
        # A cross, not a state: the alert is about the moment it happened, so
        # it requires the previous bar to have been on the other side.
        was_above = close[-2] > ma[-2]
        now_above = close[-1] > ma[-1]
        when = _bar(bars)
        when = " " + when if when else ""
        if kind == "crosses_ma" and now_above and not was_above:
            return fire(float(close[-1]),
                        "%s closed at %s%s, back above its 50-bar average of %s."
                        % (symbol, format_price(close[-1]), when, format_price(ma[-1])))
        if kind == "loses_ma" and was_above and not now_above:
            return fire(float(close[-1]),
                        "%s closed at %s%s, losing its 50-bar average of %s."
                        % (symbol, format_price(close[-1]), when, format_price(ma[-1])))
        return None

    return None


def _within_plan(conn, rows, limit_of: Callable[[int], Optional[int]]):
    """Keep each person's alerts within what their plan checks, oldest first.

    The free plan's limit is enforced when an alert is switched on, but a
    paid plan can end while many are running. Those past the limit stay
    switched on, so they start again if the plan does, and each says why it
    was not checked rather than going quiet.
    """
    from . import database as db

    mine: Dict[Optional[int], List] = {}
    for row in rows:
        mine.setdefault(_owner(row), []).append(row)
    kept = []
    for owner, theirs in mine.items():
        try:
            cap = limit_of(owner) if owner is not None else None
        except Exception:
            cap = None          # checking too many beats silently checking none
        if cap is None or len(theirs) <= cap:
            kept.extend(theirs)
            continue
        theirs.sort(key=lambda r: (r["created_ts"] or 0, int(r["id"])))
        kept.extend(theirs[:cap])
        for row in theirs[cap:]:
            db.alert_failed(conn, int(row["id"]),
                            "the free plan checks only your %s. Pause or delete "
                            "the others, or Pro checks them all."
                            % ("oldest alert" if cap == 1 else "%d oldest alerts" % cap))
    return kept


def _waits(row, now: float) -> bool:
    """Whether an alert can be left until its market opens.

    Only when its last check worked and nothing it reads can have changed
    since. One never checked, or not since it was set, edited or re-armed
    (it has no state yet), asks a new question; one whose last check failed
    has no answer yet. Both are checked whatever the hour.
    """
    if "last_error" in row.keys() and row["last_error"]:
        return False
    if not row["last_checked"] or _state(row) is None:
        return False
    return not sessions.can_change(row["symbol"], int(row["last_checked"]), now)


def check(conn, only_active: bool = True, user=None, call_of=None,
          limit_of: Optional[Callable[[int], Optional[int]]] = None,
          skip_closed: bool = False, now: Optional[float] = None) -> Dict:
    """Check alerts against fresh data and record anything that fired.

    With no user this checks every alert in the database, which is what the
    background loop wants. Pass one to check only that person's, which is what
    a page acting on someone's behalf must do.

    call_of(symbol, interval) returns today's call for the call kinds; without
    it those alerts are left alone. limit_of(user_id) is how many alerts that
    person's plan checks, or None for all of them. Every fire is also written
    to the person's alert history, and a check that fails is kept on the
    alert with its reason.

    skip_closed leaves alone an alert whose market has been closed since its
    last good check, which is what the loop wants; how many is "skipped". A
    person who asks for a check gets every one. `now` is the time the round is
    judged at, the present unless given.
    """
    from . import database as db

    now = time.time() if now is None else now
    rows = [r for r in db.alert_list(conn, active_only=only_active, user=user)]
    if call_of is None:
        # Nothing here can work out a call, so a call alert is not checked at
        # all, rather than marked as failing when nothing went wrong.
        rows = [r for r in rows if r["kind"] not in CALL_KINDS]
    if limit_of is not None:
        rows = _within_plan(conn, rows, limit_of)
    usable = []
    for row in rows:
        threshold = row["threshold"]
        if threshold is not None and KINDS.get(row["kind"], {}).get("unit") == "percent":
            threshold = _fraction(threshold)
        why = problem(row["kind"], threshold)
        if why:
            # Saved before values were checked, e.g. a negative percent that
            # would fire at once. Said on the alert instead of acted on.
            db.alert_failed(conn, int(row["id"]),
                            "its value cannot work. %s Edit or delete it." % why)
        else:
            usable.append(row)
    rows = usable
    skipped = 0
    if skip_closed:
        # After the plan's limit and the values are seen to: an alert left
        # out of either is told why, open market or not.
        due = [r for r in rows if not _waits(r, now)]
        skipped, rows = len(rows) - len(due), due
    if not rows:
        return {"checked": 0, "fired": [], "errors": [], "when": int(time.time()),
                "skipped": skipped}

    calls: Dict = {}
    if call_of is not None:
        for key in {(r["symbol"], _bars_for(r)) for r in rows
                    if r["kind"] in CALL_KINDS}:
            try:
                calls[key] = call_of(*key)
            except Exception:
                calls[key] = None

    # One fetch per symbol and bar size, shared by every alert on it.
    wanted = {(r["symbol"], _bars_for(r)) for r in rows}
    data, errors = {}, []

    def grab(key):
        symbol, interval = key
        try:
            return key, fetch_bars(symbol, interval, lookback=LOOKBACK.get(interval))
        except (DataError, Exception) as exc:
            return key, exc

    failed: Dict = {}
    with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
        for key, result in pool.map(grab, wanted):
            if isinstance(result, Exception):
                errors.append({"symbol": key[0], "error": str(result)[:120]})
                failed[key] = str(result)[:120]
            else:
                data[key] = result
                sessions.note(key[0], result, now)

    fired: List[Fired] = []
    for row in rows:
        key = (row["symbol"], _bars_for(row))
        bars = data.get(key)
        if bars is None:
            db.alert_failed(conn, int(row["id"]), "No price: %s" % failed.get(
                key, "the data source did not answer"))
            continue
        try:
            row = _after_splits(conn, row, bars)
        except DataError as exc:
            # A split cannot be ruled out just now, and a level read in the
            # wrong shares fires falsely: this alert waits for the next round.
            db.alert_failed(conn, int(row["id"]),
                            "the share split history could not be read just now.")
            errors.append({"symbol": row["symbol"],
                           "error": "split history: %s" % str(exc)[:100]})
            continue
        is_call = row["kind"] in CALL_KINDS
        call = calls.get(key) if is_call else None
        if is_call and call is None:
            db.alert_failed(conn, int(row["id"]), "The call could not be worked out just now.")
            continue
        try:
            hit = _evaluate(row, bars, call)
        except Exception as exc:
            errors.append({"symbol": row["symbol"], "error": str(exc)[:120]})
            db.alert_failed(conn, int(row["id"]), str(exc)[:120])
            continue

        was = _state(row)
        if is_call:
            state = call
        else:
            state = "met" if hit else "clear"
            # Still true since the last look, so not news: an alert that keeps
            # watching speaks again only after its condition has cleared.
            if hit and was == "met":
                hit = None
        if hit is None:
            db.alert_checked(conn, int(row["id"]), state=state)
            continue
        # Two checks can overlap (the loop, "Check them all now", a routine).
        # Only the one that moves the alert on from what it read tells anyone.
        if not db.alert_fire(conn, hit.alert_id, hit.value, hit.message,
                             state=state, was=was):
            continue
        if hit.user_id is not None:
            hit.event_id = db.event_add(conn, hit.user_id, hit.kind, hit.symbol,
                                        hit.message, hit.value, hit.as_of,
                                        alert_id=hit.alert_id)
        fired.append(hit)

    return {"checked": len(rows), "fired": fired, "errors": errors,
            "when": int(time.time()), "skipped": skipped}


def _after_splits(conn, row, bars):
    """A price alert set before a split, with its level moved into today's shares.

    Yahoo rescales the history for a split, so a "falls below 900" set before
    a ten-for-one fired at once at 101, and a "rises above 1,100" could never
    fire. The level is divided by the split once: the split's time is kept on
    the alert so it is never applied twice, and the owner is told in the
    alert history. The splits come from the symbol's whole history when the
    alert's own bars do not reach back to when its level was set, so one left
    paused or unchecked past its short window is still moved. Any other kind
    of alert is in percent or points and needs nothing.
    """
    from . import database as db

    if row["kind"] not in ("price_above", "price_below") or not row["threshold"]:
        return row
    try:
        done = int(row["split_ts"] or 0)
    except (KeyError, IndexError):
        done = 0
    since = max(int(row["created_ts"] or 0), done)
    later = splits_since(row["symbol"], since, bars)
    if not later:
        return row
    factor = 1.0
    for _when, ratio in later:
        factor *= ratio
    old = float(row["threshold"])
    new = old / factor
    if db.alert_split(conn, int(row["id"]), old, new, max(w for w, _r in later)):
        owner = _owner(row)
        if owner is not None:
            db.event_add(conn, owner, "split", row["symbol"],
                         "%s split %s. Your alert's level moved from %s to %s, "
                         "the same price in today's shares."
                         % (row["symbol"], split_words(factor), format_price(old),
                            format_price(new)), new, as_of(bars),
                         alert_id=int(row["id"]))
    # Moved here or by a check running alongside: read it as it is now.
    fresh = conn.execute("SELECT * FROM alerts WHERE id=?", (int(row["id"]),)).fetchone()
    return fresh if fresh is not None else row


# When each open position's stop was last checked with prices that could be
# read, by position id. Kept in memory: after a restart every stop is checked
# once more, which is the safe way to forget.
_STOPS_LOOKED: Dict[int, float] = {}
_STOPS_LOCK = threading.Lock()


def check_stops(conn, user=None, skip_closed: bool = False,
                now: Optional[float] = None) -> Dict:
    """Tell the owner, once, when an open journal position crosses its stop.

    A stop in the journal is a plan, not an order: nothing closes the trade.
    So the background loop watches every open position and says so the first
    time the price is at or past the stop, with the time of that price. It is
    written to the person's alert history like any alert, and never repeats
    for the same position.

    skip_closed leaves alone a position whose stop was checked and whose
    market has been closed since, as check() does for alerts.
    """
    from . import database as db

    now = time.time() if now is None else now
    sql, args = db._own("SELECT * FROM trades WHERE status='open' "
                        "AND breach_noticed IS NULL", [], user)
    rows = list(conn.execute(sql, args))
    with _STOPS_LOCK:
        if user is None:
            # Everybody's open positions: forget those closed or already told.
            still = {int(r["id"]) for r in rows}
            for gone in [k for k in _STOPS_LOOKED if k not in still]:
                del _STOPS_LOOKED[gone]
        looked = dict(_STOPS_LOOKED)
    skipped = 0
    if skip_closed:
        due = [r for r in rows
               if sessions.can_change(r["symbol"], looked.get(int(r["id"])), now)]
        skipped, rows = len(rows) - len(due), due
    if not rows:
        return {"checked": 0, "fired": [], "errors": [], "when": int(time.time()),
                "skipped": skipped}

    def bar_size(row) -> str:
        # The trade's own bars, as the journal reads it, so a stop touched
        # and recovered inside an intraday trade's day is seen here too.
        interval = row["interval"] or "1d"
        return interval if interval in LOOKBACK else "1d"

    data, errors = {}, []
    for key in sorted({(r["symbol"], bar_size(r)) for r in rows}):
        try:
            # The same history as an alert's on that bar size, so a symbol
            # with both an alert and a position is one request, not two.
            data[key] = fetch_bars(key[0], key[1], lookback=LOOKBACK[key[1]])
            sessions.note(key[0], data[key], now)
        except Exception as exc:
            errors.append({"symbol": key[0], "error": str(exc)[:120]})

    fired: List[Fired] = []
    for row in rows:
        bars = data.get((row["symbol"], bar_size(row)))
        if bars is None:
            continue
        # The stop is in the shares of the day it opened; after a split the
        # quote is in today's, and read as is a long looked far past its stop.
        # Over the trade's whole life: a year of bars can miss an older split.
        opened = int(row["ts_opened"])
        try:
            split = split_factor_since(row["symbol"], opened, bars)
        except DataError as exc:
            # A split cannot be ruled out just now. Told now, a position
            # opened before one reads ten times past its stop, and the real
            # crossing would then never be told: it waits for the next round.
            errors.append({"symbol": row["symbol"],
                           "error": "split history: %s" % str(exc)[:100]})
            continue
        # Read in full from here on, so a closed market can wait for its open.
        with _STOPS_LOCK:
            _STOPS_LOOKED[int(row["id"])] = now
        quote = float(bars.last_price)
        price, stop, side = quote * split, float(row["stop"]), int(row["direction"])
        # Past the stop now, or traded through it since opening and come back:
        # the journal marks both as stopped (portfolio.stop_touched), so the
        # owner is told of both.
        from .portfolio import stop_touched
        touched_at, touched = stop_touched(bars, opened, stop / split, side)
        past_now = (price - stop) * side <= 0
        if not past_now and touched_at is None:
            continue
        said = format_price(price)
        if split != 1.0:
            said = ("%s (%s in the shares you opened with, after a %s split)"
                    % (format_price(quote), format_price(price), split_words(split)))
        side_word = "long" if side > 0 else "short"
        if past_now:
            message = ("%s is at %s, past the stop of %s on your %s position. "
                       "The journal does not close it for you: close it, or set a "
                       "new stop on purpose." % (row["symbol"], said,
                                                 format_price(stop), side_word))
        else:
            when = time.strftime("%d %b", time.gmtime(touched_at)).lstrip("0")
            message = ("%s traded at %s on %s, past the stop of %s on your %s "
                       "position, and is at %s now. The journal counts the stop "
                       "as hit and does not close it for you: close it, or set a "
                       "new stop on purpose." % (row["symbol"],
                                                 format_price(touched * split), when,
                                                 format_price(stop), side_word, said))
        hit = Fired(0, row["symbol"], "stop_breach", stop, price, message, "",
                    _owner(row), as_of(bars), int(row["id"]))
        # Two checks at once (the loop and "Check them all now") both read the
        # trade before either marked it. Only the one that marks it tells.
        cur = conn.execute("UPDATE trades SET breach_noticed=? WHERE id=? "
                           "AND breach_noticed IS NULL",
                           (int(time.time()), int(row["id"])))
        conn.commit()
        if cur.rowcount == 0:
            continue
        if hit.user_id is not None:
            hit.event_id = db.event_add(conn, hit.user_id, "stop_breach", hit.symbol,
                                        message, price, hit.as_of, trade_id=hit.trade_id)
        fired.append(hit)
    return {"checked": len(rows), "fired": fired, "errors": errors,
            "when": int(time.time()), "skipped": skipped}


def deliver(conn, fired: List[Fired], send, address_of) -> Dict:
    """Email each person what fired for them this round, in one message.

    send(to, subject, body) is the mail function; address_of(user_id) returns
    an address for someone who asked for alert emails, or None. The outcome,
    sent or the error, is written against each event, so a failure is on
    record rather than silent.
    """
    from . import database as db

    by_user: Dict[int, List[Fired]] = {}
    for hit in fired:
        if hit.user_id is not None:
            by_user.setdefault(hit.user_id, []).append(hit)
    sent, failed = 0, 0
    for user_id, hits in by_user.items():
        to = address_of(user_id)
        if not to:
            continue
        events = [h.event_id for h in hits if h.event_id is not None]
        subject = ("Orenth: %s" % hits[0].message[:60] if len(hits) == 1
                   else "Orenth: %d alerts" % len(hits))
        body = "\n\n".join("%s\n(price as of %s)" % (h.message, h.as_of or "unknown")
                            for h in hits)
        body += ("\n\nSee them all on your Monitor page. You get these because "
                 "you asked for alert emails; switch them off on the Monitor page.\n")
        try:
            send(to, subject, body)
            db.event_emailed(conn, events)
            sent += 1
        except Exception as exc:
            db.event_emailed(conn, events, error=str(exc) or type(exc).__name__)
            failed += 1
    return {"sent": sent, "failed": failed}


def suggest_for(symbol: str, plan=None, bars=None) -> List[Dict]:
    """Alerts worth setting for an instrument, given a plan if there is one.

    Suggestions are concrete rather than generic: if a plan says wait for a
    price, the obvious alert is that exact price.
    """
    out: List[Dict] = []
    if plan is not None and getattr(plan, "entry", None):
        if plan.direction > 0:
            out.append({"kind": "price_below", "threshold": plan.entry,
                        "why": "Tells you when price reaches the entry the plan "
                               "is waiting for."})
        else:
            out.append({"kind": "price_above", "threshold": plan.entry,
                        "why": "Tells you when price reaches the short entry."})
        if getattr(plan, "stop", None):
            out.append({"kind": "price_below" if plan.direction > 0 else "price_above",
                        "threshold": plan.stop,
                        "why": "Tells you if the level that invalidates the idea "
                               "is breached."})

    if bars is not None:
        out.append({"kind": "pct_down", "threshold": 0.05,
                    "why": "A 5% single-day fall usually means news you have "
                           "not read yet."})
        out.append({"kind": "volume_spike", "threshold": 3.0,
                    "why": "Three times normal volume means something changed."})
        out.append({"kind": "near_52w_high", "threshold": 0.02,
                    "why": "Approaching the 52-week high, where breakouts and "
                           "rejections both happen."})
    for item in out:
        # What the form sends: a percent as the whole number a person would
        # type, since the form reads 5 as 5%.
        item["form_value"] = (round(item["threshold"] * 100, 6)
                              if KINDS[item["kind"]]["unit"] == "percent"
                              else item["threshold"])
    return out
