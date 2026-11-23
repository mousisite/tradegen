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

The price-action kinds (the levels and shapes TJR's day-trading video
teaches: yesterday's high and low, opening gaps, breaks of the last swing,
gaps a move left, the London session's range) say what the price did, with
the price and the New York time, and never what it will do. Those about an
event on a finished bar keep the start time of the last bar they looked at
as their state ("bar:<time>"), so each bar is looked at once: an event is
told once, and one that happened between two checks is not missed.
"""
from __future__ import annotations

import dataclasses
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import clock, liquidity
from . import indicators as ind
from . import sessions
from .market import (DataError, fetch_bars, format_price, split_factor_since,
                     split_words, splits_since)

# The groups the Monitor form lists the kinds under, in this order.
GROUPS = ("Price", "Indicators", "Price action", "Calls")

KINDS: Dict[str, Dict] = {
    "price_above": {"label": "Price rises above", "needs_threshold": True,
                    "unit": "price", "group": "Price"},
    "price_below": {"label": "Price falls below", "needs_threshold": True,
                    "unit": "price", "group": "Price"},
    "pct_up": {"label": "Rises by at least", "needs_threshold": True,
               "unit": "percent", "group": "Price"},
    "pct_down": {"label": "Falls by at least", "needs_threshold": True,
                 "unit": "percent", "group": "Price"},
    "rsi_above": {"label": "RSI goes above", "needs_threshold": True,
                  "unit": "level", "group": "Indicators"},
    "rsi_below": {"label": "RSI falls below", "needs_threshold": True,
                  "unit": "level", "group": "Indicators"},
    "near_52w_high": {"label": "Comes within", "needs_threshold": True,
                      "unit": "percent", "suffix": "of its 52-week high",
                      "group": "Price"},
    "near_52w_low": {"label": "Comes within", "needs_threshold": True,
                     "unit": "percent", "suffix": "of its 52-week low",
                     "group": "Price"},
    "volume_spike": {"label": "Trades on more than", "needs_threshold": True,
                     "unit": "multiple", "suffix": "its normal volume",
                     "group": "Indicators"},
    "crosses_ma": {"label": "Closes above its 50-bar average",
                   "needs_threshold": False, "unit": "", "group": "Indicators"},
    "loses_ma": {"label": "Closes below its 50-bar average",
                 "needs_threshold": False, "unit": "", "group": "Indicators"},
    # Price action, as the course reads a chart. None needs a value. "say"
    # is the condition in the middle of a sentence, its capitals kept.
    # "event": about something a finished bar did, told once per bar.
    "takes_prior_high": {"label": "Trades above yesterday's high",
                         "needs_threshold": False, "unit": "", "group": "Price action",
                         "say": "trades above yesterday's high"},
    "takes_prior_low": {"label": "Trades below yesterday's low",
                        "needs_threshold": False, "unit": "", "group": "Price action",
                        "say": "trades below yesterday's low"},
    "sweeps_prior_high": {"label": "Takes yesterday's high, then closes back below it",
                          "needs_threshold": False, "unit": "", "group": "Price action",
                          "say": "takes yesterday's high, then closes back below it",
                          "event": True},
    "sweeps_prior_low": {"label": "Takes yesterday's low, then closes back above it",
                         "needs_threshold": False, "unit": "", "group": "Price action",
                         "say": "takes yesterday's low, then closes back above it",
                         "event": True},
    "gap_fills": {"label": "Today's opening gap fills", "needs_threshold": False,
                  "unit": "", "group": "Price action",
                  "say": "fills the gap its day opened with", "event": True},
    "structure_breaks": {"label": "Closes past its last swing the other way "
                                  "(a break of structure)",
                         "needs_threshold": False, "unit": "", "group": "Price action",
                         "say": "closes past its last swing the other way "
                                "(a break of structure)", "event": True},
    "back_in_gap": {"label": "Pulls back into its newest gap", "needs_threshold": False,
                    "unit": "", "group": "Price action",
                    "say": "pulls back into the newest gap its move left", "event": True},
    "takes_session_range": {"label": "Trades outside London's range, 08:30 to 12:00 "
                                     "New York",
                            "needs_threshold": False, "unit": "", "group": "Price action",
                            "say": "trades outside London's range between 08:30 and "
                                   "12:00 New York time", "event": True},
    # Calls rather than prices: the app's own BUY / WAIT / AVOID for the
    # instrument, worked out with the standard settings.
    "call_changes": {"label": "Call changes (for example WAIT to BUY)",
                     "needs_threshold": False, "unit": "", "call": True,
                     "group": "Calls"},
    "call_buy": {"label": "Call turns BUY", "needs_threshold": False, "unit": "",
                 "call": True, "group": "Calls"},
}

CALL_KINDS = tuple(k for k, spec in KINDS.items() if spec.get("call"))
EVENT_KINDS = tuple(k for k, spec in KINDS.items() if spec.get("event"))
PRICE_ACTION_KINDS = tuple(k for k, spec in KINDS.items()
                           if spec.get("group") == "Price action")
# Said at the end of every price-action message: what it is, and is not.
FACT = "A fact about price, not a forecast."


class NotApplicable(Exception):
    """An alert that cannot mean anything on its instrument or bar size.

    Its words are kept on the alert in place of an answer, as a failed check
    is, so the Monitor page says why it never fires.
    """


def grouped() -> List[Tuple[str, List[Tuple[str, Dict]]]]:
    """The kinds under their GROUPS, for the Monitor form's lists."""
    return [(g, [(k, spec) for k, spec in KINDS.items() if spec.get("group") == g])
            for g in GROUPS]

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


def _exact(row) -> bool:
    """Whether the alert was saved for Yahoo's own symbol, read as written."""
    return bool(row["exact"]) if "exact" in row.keys() else False


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
    if spec.get("say"):
        # Lower-cased, "London" and "New York" would read wrong.
        return "%s %s" % (symbol, spec["say"])
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

    if kind in PRICE_ACTION_KINDS:
        return _price_action(row, bars)[0]

    return None


# --- Price action -----------------------------------------------------------------

def _finite(*values) -> bool:
    try:
        return all(v is not None and math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


def _row_value(row, key):
    try:
        return row[key] if key in row.keys() else None
    except (AttributeError, IndexError, KeyError):
        return None


def _live_moment(bars) -> Optional[int]:
    """When the live price was set, if the data says."""
    meta = getattr(bars, "meta", None) or {}
    when = meta.get("regularMarketTime")
    try:
        return int(when) if when else None
    except (TypeError, ValueError):
        return None


def _yesterday(bars, s: Dict[str, np.ndarray]) -> Tuple[float, float]:
    """(high, low) of the last whole trading day before the live price's.

    The series give it for the day of the last finished bar. When the live
    price is already in a newer bar of a newer day (a daily bar split off
    while the market trades, or the first bar of a session), that last
    finished day is yesterday itself, and the series would reach a day too
    far back. NaN when the data holds no whole day before.
    """
    nan = float("nan")
    n = len(bars.close)
    if n == 0:
        return nan, nan
    live = _live_moment(bars)
    kind = clock.kind_of(bars)
    start = int(bars.timestamp[-1])
    newer = live is not None and live >= start + clock.bar_seconds(bars)
    if newer and kind == "daily":
        return float(bars.high[-1]), float(bars.low[-1])
    if newer and clock.trading_day(live, kind, bars).toordinal() > int(s["ck_day"][-1]):
        last = clock.day_table(bars)[-1]
        if last.complete:
            return float(last.high), float(last.low)
    return float(s["ck_pdh"][-1]), float(s["ck_pdl"][-1])


def _bar_when(bars, s: Dict[str, np.ndarray], j: int) -> str:
    """'in the bar that opened at 10:00 New York time on 19 Nov', or 'on 19
    Nov' for a daily bar (its trading day, whatever time it is stamped at)."""
    if clock.kind_of(bars) == "daily":
        day = date.fromordinal(int(s["ck_day"][j]))
        return "on %d %s" % (day.day, clock._MONTHS[day.month - 1])
    stamp = clock.label_ny(int(bars.timestamp[j]))
    day, _, hm = stamp.rpartition(" ")
    return "in the bar that opened at %s New York time on %s" % (hm, day)


def _since(row, bars) -> Tuple[int, bool]:
    """Where an event alert's last look ended: (time, True) for the start of
    the last bar looked at, from its state, or (time, False) for the moment
    it was set, or last checked, when it has no state yet (a new alert, or
    one re-armed or edited)."""
    state = _state(row) or ""
    if state.startswith("bar:"):
        try:
            return int(state[4:]), True
        except ValueError:
            pass
    created = int(_row_value(row, "created_ts") or 0)
    checked = int(_row_value(row, "last_checked") or 0)
    return max(created, checked), False


def _first_of_day(cond: np.ndarray, day: np.ndarray) -> np.ndarray:
    """True at the first bar of each trading day where cond holds."""
    out = np.zeros(len(cond), dtype=bool)
    seen = None
    for j in np.flatnonzero(cond):
        if day[j] != seen:
            out[j] = True
            seen = day[j]
    return out


def _price_action(row, bars, series: Optional[Dict[str, np.ndarray]] = None
                  ) -> Tuple[Optional[Fired], Optional[str]]:
    """A price-action alert against fresh bars: (what fired or None, the
    state to keep). The state is None for the kinds that read the live price
    (met and clear are kept by check()), "bar:<start time>" of the newest
    finished bar for the others. Raises NotApplicable for a kind its
    instrument or bar size cannot have. `series` is liquidity.series(bars),
    when the caller has it already."""
    kind = row["kind"]
    symbol = row["symbol"]
    threshold = row["threshold"]
    instrument = clock.instrument_of(bars)
    daily = clock.kind_of(bars) == "daily"
    if kind == "takes_session_range":
        if instrument == "stock":
            raise NotApplicable("%s trades only in the day session, so it has no "
                                "London session." % symbol)
        if daily:
            raise NotApplicable("sessions need bars shorter than a day: set this "
                                "alert on 5-minute, 15-minute or hourly bars.")
    if kind == "gap_fills" and instrument == "coin":
        raise NotApplicable("%s trades round the clock, so its day opens with no "
                            "gap." % symbol)

    def fire(value, message):
        return Fired(int(row["id"]), symbol, kind, threshold, value,
                     "%s %s" % (message, FACT), row["note"] or "", _owner(row), as_of(bars))

    n = len(bars.close)
    if n < 3:
        return None, None
    s = series if series is not None else liquidity.series(bars)
    fp = format_price

    if kind in ("takes_prior_high", "takes_prior_low"):
        high, low = _yesterday(bars, s)
        price = float(bars.last_price)
        up = kind == "takes_prior_high"
        level = high if up else low
        if not _finite(level, price):
            return None, None
        if (price > level) if up else (price < level):
            return fire(price, "%s is at %s, %s yesterday's %s of %s."
                        % (symbol, fp(price), "above" if up else "below",
                           "high" if up else "low", fp(level))), None
        return None, None

    ts = np.asarray(bars.timestamp, dtype=np.int64)
    step = clock.bar_seconds(bars)
    since, from_state = _since(row, bars)
    if from_state:
        first = int(np.searchsorted(ts, since, side="right"))
        # Never back: bars that end earlier than the last look (a source
        # answering with stale data) must not make it look again at bars
        # it has told about.
        state = "bar:%d" % max(int(ts[-1]), since)
    else:
        # A first look: bars finished after the alert was set (or last
        # looked at), so an event from before is not news.
        first = int(np.searchsorted(ts + step, since, side="right"))
        state = "bar:%d" % int(ts[-1])

    close = np.asarray(bars.close, dtype=float)
    day = s["ck_day"]
    with np.errstate(invalid="ignore"):
        if kind == "sweeps_prior_high":
            cond = _first_of_day((s["ck_day_hi"] > s["ck_pdh"]) & (close < s["ck_pdh"]), day)
        elif kind == "sweeps_prior_low":
            cond = _first_of_day((s["ck_day_lo"] < s["ck_pdl"]) & (close > s["ck_pdl"]), day)
        elif kind == "gap_fills":
            filled = s["ck_gap_filled"] == 1
            prev = np.concatenate([[True], filled[:-1]])
            new_day = np.concatenate([[False], day[1:] != day[:-1]])
            cond = filled & (~prev | new_day) & np.isfinite(s["ck_gap_lo"])
            cond[0] = False             # filled before the data starts, or on it
        elif kind == "structure_breaks":
            cond = s["st_flip"] != 0
        elif kind == "back_in_gap":
            trend = np.concatenate([[0], s["st_trend"][:-1]])
            cond = (s["st_fvg_touch"] != 0) & (s["st_fvg_touch"] == trend)
        elif kind == "takes_session_range":
            cond = (s["ck_sess_take"] != 0) | (s["ck_sess_both"] == 1)
        else:
            return None, state
    hits = [j for j in np.flatnonzero(cond) if j >= first]
    if not hits:
        return None, state
    j = int(hits[-1])
    when = _bar_when(bars, s, j)
    c = float(close[j])

    if kind in ("sweeps_prior_high", "sweeps_prior_low"):
        up = kind == "sweeps_prior_high"
        level = float(s["ck_pdh"][j] if up else s["ck_pdl"][j])
        message = ("%s took yesterday's %s of %s and closed back %s it, at %s, %s."
                   % (symbol, "high" if up else "low", fp(level),
                      "below" if up else "above", fp(c), when))
        value = c
    elif kind == "gap_fills":
        d = int(s["ck_gap_dir"][j])
        level = float(s["ck_gap_lo"][j] if d > 0 else s["ck_gap_hi"][j])
        message = ("%s traded back to the previous close of %s %s, filling the gap "
                   "its day opened with." % (symbol, fp(level), when))
        value = level
    elif kind == "structure_breaks":
        d = int(s["st_flip"][j])
        level = float(s["st_ref_hi" if d > 0 else "st_ref_lo"][j - 1]) if j > 0 else float("nan")
        message = ("%s closed at %s, %s its last swing %s%s, %s: on these bars the "
                   "trend turned %s." % (symbol, fp(c), "above" if d > 0 else "below",
                                         "high" if d > 0 else "low",
                                         " of %s" % fp(level) if _finite(level) else "",
                                         when, "up" if d > 0 else "down"))
        value = c
    elif kind == "back_in_gap":
        d = int(s["st_fvg_touch"][j])
        top = float(s["st_fvg_up_top" if d > 0 else "st_fvg_dn_top"][j - 1])
        bot = float(s["st_fvg_up_bot" if d > 0 else "st_fvg_dn_bot"][j - 1])
        zone = (" between %s and %s" % (fp(bot), fp(top))) if _finite(top, bot) else ""
        message = ("%s came back into the gap%s that its %s left, %s, and closed at %s."
                   % (symbol, zone, "rise" if d > 0 else "fall", when, fp(c)))
        value = c
    else:                                   # takes_session_range
        src = clock.SESS_SOURCES[int(s["ck_sess_src"][j])] or "london"
        hi = float(s["ck_lon_hi" if src == "london" else "ck_asia_hi"][j])
        lo = float(s["ck_lon_lo" if src == "london" else "ck_asia_lo"][j])
        which = "the London session's" if src == "london" else "the Asia session's"
        d = int(s["ck_sess_take"][j])
        if d == 0:
            what = "past both ends of %s range, %s and %s," % (which, fp(lo), fp(hi))
        elif d > 0:
            what = "below %s low of %s" % (which, fp(lo))
        else:
            what = "above %s high of %s" % (which, fp(hi))
        message = "%s traded %s %s, and closed at %s." % (symbol, what, when, fp(c))
        if src != "london":
            message += " London's prices were incomplete that day, so Asia's range was used."
        value = c
    if len(hits) > 1:
        message += " It was the latest of %d since the last look." % len(hits)
    return fire(value, message), state


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

    calls: Dict = {}
    if call_of is not None:
        # Asked after the fetch, which notes each market's hours: a call
        # worked out before the last close settled is not used once it has,
        # and just after a restart only this round's download says when that
        # was. Asked before it, a popular stock's public call from before
        # Friday's close came back that evening as a change, and again on
        # Monday. The price is fetched as stored either way. The call is an analysis,
        # which reads a bare LINK as the Chainlink coin, so an alert saved for
        # Interlink Electronics asks for it exactly; only then, so a call_of
        # taking a symbol and a bar size alone serves every other alert. One
        # with no price fails for that below, so its call is not asked for.
        for key in {(r["symbol"], _bars_for(r), _exact(r)) for r in rows
                    if r["kind"] in CALL_KINDS and (r["symbol"], _bars_for(r)) in data}:
            symbol, interval, exact = key
            try:
                calls[key] = (call_of(symbol, interval, exact=True) if exact
                              else call_of(symbol, interval))
            except Exception:
                calls[key] = None

    fired: List[Fired] = []
    structure: Dict = {}
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
        call = calls.get(key + (_exact(row),)) if is_call else None
        if is_call and call is None:
            db.alert_failed(conn, int(row["id"]), "The call could not be worked out just now.")
            continue
        is_event = row["kind"] in EVENT_KINDS
        try:
            if row["kind"] in PRICE_ACTION_KINDS:
                # The structure is read once per download, however many
                # alerts look at it.
                if key not in structure:
                    structure[key] = liquidity.series(bars)
                hit, seen = _price_action(row, bars, structure[key])
            else:
                hit = _evaluate(row, bars, call)
        except NotApplicable as exc:
            # Nothing went wrong: the condition cannot happen on this
            # instrument or bar size, and the alert says so instead of firing.
            db.alert_failed(conn, int(row["id"]), str(exc)[:200])
            continue
        except Exception as exc:
            errors.append({"symbol": row["symbol"], "error": str(exc)[:120]})
            db.alert_failed(conn, int(row["id"]), str(exc)[:120])
            continue

        was = _state(row)
        if is_call:
            state = call
        elif is_event:
            # The last bar looked at: a bar is looked at once, so an event is
            # told once, and none between two checks is missed.
            state = seen or was
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
        side = int(row["direction"])
        # Past the stop now, or traded through it since opening and come back:
        # the journal marks both as stopped, by the one rule they all read a
        # position through (portfolio.read_stop), trail and split included.
        from .portfolio import read_trade_stop
        read = read_trade_stop(row, bars, split)
        if read.trail is not None:
            # The highs this round saw hold the trail up for every reader.
            db.trail_seen(conn, int(row["id"]), read.best, read.best_at)
        # A crossing it saw stays one once that bar is finished, and once the
        # bar is older than any reader's bars reach.
        crossed = read.crossing(now)
        if crossed is not None:
            db.stop_crossed(conn, int(row["id"]), *crossed)
        if not read.hit:
            continue
        price, stop = read.price, read.stop
        said = format_price(price)
        if split != 1.0:
            said = ("%s (%s in the shares you opened with, after a %s split)"
                    % (format_price(quote), format_price(price), split_words(split)))
        side_word = "long" if side > 0 else "short"
        kind = "the stop"
        if read.trail is not None:
            kind = "the trailing stop"
        if read.past:
            moved = ""
            if read.moved:
                moved = (" It had trailed %s behind the best price, %s from %s."
                         % (read.trail.words(), "up" if side > 0 else "down",
                            format_price(read.initial)))
            message = ("%s is at %s, past %s of %s on your %s position.%s "
                       "The journal does not close it for you: close it, or set a "
                       "new stop on purpose." % (row["symbol"], said, kind,
                                                 format_price(stop), side_word, moved))
        else:
            when = time.strftime("%d %b", time.gmtime(read.touched_at)).lstrip("0")
            message = ("%s traded at %s on %s, past %s of %s on your %s "
                       "position, and is at %s now. The journal counts the stop "
                       "as hit and does not close it for you: close it, or set a "
                       "new stop on purpose." % (row["symbol"],
                                                 format_price(read.touched), when, kind,
                                                 format_price(read.touched_stop),
                                                 side_word, said))
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
        out.extend(_price_action_suggestions(bars))
    for item in out:
        # What the form sends: a percent as the whole number a person would
        # type, since the form reads 5 as 5%; nothing for a kind with no value.
        if item["threshold"] is None:
            item["form_value"] = ""
        else:
            item["form_value"] = (round(item["threshold"] * 100, 6)
                                  if KINDS[item["kind"]]["unit"] == "percent"
                                  else item["threshold"])
    return out


# The most bars suggest_for reads the levels from: enough for yesterday and
# the swings that matter now, and quick on sixty days of five-minute bars.
SUGGEST_BARS = 1500


def _price_action_suggestions(bars) -> List[Dict]:
    """The course's levels worth being told about, for the bars an analysis
    is on: a take of the nearer of yesterday's high and low, a break of the
    last swing the other way, and today's opening gap filling while it is
    open. Nothing when the bars cannot be read that way."""
    try:
        n = len(bars.close)
        if n < 30:
            return []
        tail = bars
        if n > SUGGEST_BARS:
            k = SUGGEST_BARS
            tail = dataclasses.replace(
                bars, timestamp=bars.timestamp[-k:], open=bars.open[-k:],
                high=bars.high[-k:], low=bars.low[-k:], close=bars.close[-k:],
                volume=bars.volume[-k:], session_id=bars.session_id[-k:],
                dividend_factor=None)
        s = liquidity.series(tail)
        high, low = _yesterday(tail, s)
        price = float(bars.last_price)
    except Exception:
        return []
    out: List[Dict] = []
    if _finite(high, low, price):
        up = abs(high - price) <= abs(price - low)
        out.append({"kind": "sweeps_prior_high" if up else "sweeps_prior_low",
                    "threshold": None,
                    "why": "Yesterday's %s, %s, is the nearer of its high and low. This "
                           "tells you if price trades past it and then closes back "
                           "inside, the move the course watches for. It says what "
                           "happened, not what comes next."
                           % ("high" if up else "low", format_price(high if up else low))})
    out.append({"kind": "structure_breaks", "threshold": None,
                "why": "Tells you when a bar closes past the last swing the other way, "
                       "which the course reads as the trend on these bars turning."})
    if clock.kind_of(tail) != "daily" and _finite(s["ck_gap_lo"][-1], s["ck_gap_hi"][-1]) \
            and not s["ck_gap_filled"][-1]:
        live = _live_moment(bars)
        today = (live is None or clock.trading_day(live, clock.kind_of(tail), tail).toordinal()
                 == int(s["ck_day"][-1]))
        if today:
            d = int(s["ck_gap_dir"][-1])
            prev = float(s["ck_gap_lo"][-1] if d > 0 else s["ck_gap_hi"][-1])
            out.append({"kind": "gap_fills", "threshold": None,
                        "why": "Today opened with a gap %s the previous close of %s. "
                               "This tells you if price trades back to it."
                               % ("up from" if d > 0 else "down from", format_price(prev))})
    return out
