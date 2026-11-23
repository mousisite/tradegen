"""New York time, trading days, sessions and the levels a day trader marks.

The course times everything in New York (video 0:44:23-0:45:27): the 9:30
open, the Asia, London and New York sessions (1:47:58-2:05:49, 4:29:07-
4:32:20), yesterday's high and low (2:05:49-2:08:58), the 9:30-9:50, 9:50-10:10
and 10:30 marks (4:31:11-4:37:38) and the gaps a new day and a new week open
with (3:16:24-3:25:57). This module turns bar times into those, for any
instrument, as series a back-test can replay.

Times are kept in UTC seconds, as the bars have them, and read in New York time
by the rule sessions.new_york_offset uses: there is no time zone database on
Windows. The clocks change at 02:00 on a Sunday, and that hour is placed
exactly, which matters only for coins: futures and stocks are shut then.

A trading day for futures and coins starts at 18:00 New York time, the futures
reopen, so a bar at 18:30 belongs to the next date. A US stock or fund trades
09:30 to 16:00 only, and its data has nothing outside that (market.fetch_bars
asks for no pre- or after-market prices), so it has no Asia or London ranges. A
listing on another exchange (elsewhere()) keeps that exchange's own dates: cut
at New York's midnight, one Tokyo session was parts of two days.

A session counts only when at least 80% of its bars are there; a day or
session with fewer is left out of every level (NaN) and can be counted by the
caller from day_table(). Every value at bar i depends on bars 0..i only.
"""
from __future__ import annotations

import calendar
import dataclasses
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import sessions as sessions_mod

DAY = 86400
EPOCH = date(1970, 1, 1).toordinal()
NAN = float("nan")

# New York minutes after midnight. Asia runs over midnight (start > end).
# video 1:47:58-2:05:49 and 4:29:07-4:32:20: Asia from the 18:00 reopen to
# 03:00, London to 08:30, then New York. app choice: New York is split at
# noon and 13:00 (lunch), and the hour after the stock close is its own.
SESSIONS = (("asia", 18 * 60, 3 * 60), ("london", 3 * 60, 8 * 60 + 30),
            ("ny_pre", 8 * 60 + 30, 9 * 60 + 30), ("ny_am", 9 * 60 + 30, 12 * 60),
            ("ny_lunch", 12 * 60, 13 * 60), ("ny_pm", 13 * 60, 16 * 60),
            ("ny_late", 16 * 60, 17 * 60), ("halt", 17 * 60, 18 * 60))
SESSION_NAMES = tuple(s[0] for s in SESSIONS)
SESSION_LABELS = {
    "asia": "Asia session (18:00 to 03:00)",
    "london": "London session (03:00 to 08:30)",
    "ny_pre": "New York, before the open (08:30 to 09:30)",
    "ny_am": "New York morning (09:30 to 12:00)",
    "ny_lunch": "New York lunch (12:00 to 13:00)",
    "ny_pm": "New York afternoon (13:00 to 16:00)",
    "ny_late": "After the stock market closes (16:00 to 17:00)",
    "halt": "The futures' daily pause (17:00 to 18:00)",
}

# video 4:31:11-4:37:38: the course watches 9:30-9:50, enters 9:50-10:10 and
# stops looking at 10:30.
WINDOWS = (("open_drive", 9 * 60 + 30, 9 * 60 + 50), ("macro", 9 * 60 + 50, 10 * 60 + 10),
           ("late_morning", 10 * 60 + 10, 10 * 60 + 30), ("after_cutoff", 10 * 60 + 30, 16 * 60))
WINDOW_LABELS = {
    "open_drive": "9:30 to 9:50",
    "macro": "9:50 to 10:10",
    "late_morning": "10:10 to 10:30",
    "after_cutoff": "after 10:30",
}

# The Setup lab groups trades by when they were entered, over the whole day.
LAB_WINDOWS = (("asia", 18 * 60, 3 * 60), ("london", 3 * 60, 8 * 60 + 30),
               ("ny_pre", 8 * 60 + 30, 9 * 60 + 30),
               ("open_drive", 9 * 60 + 30, 9 * 60 + 50), ("macro", 9 * 60 + 50, 10 * 60 + 10),
               ("late_morning", 10 * 60 + 10, 10 * 60 + 30),
               ("midday", 10 * 60 + 30, 12 * 60), ("afternoon", 12 * 60, 16 * 60),
               ("after", 16 * 60, 18 * 60))

# app choice: a session (or day) with under 80% of its bars is not a range;
# the data source leaves holes, and a range with a hole in it is too narrow.
COMPLETE_SHARE = 0.8
# app choice: how a futures contract roll (or a break in the data) is told from
# a market move. Yahoo's continuous ES=F and NQ=F switch contract inside a day,
# at 16:00 on the quarterly expiry Friday (ES 55 to 155 points, 4 to 8 hourly
# ATRs, in 2024-2025), and sometimes at the Sunday reopen in that week (NQ
# +620 on 23 December 2024). Between two bars of a running session prices
# hardly move (median 0.01 ATR), so a jump there of more than ROLL_JUMP of a
# usual day's range is a roll or bad data. An opening gap counts only when
# over ROLL_GAP of a usual day's range inside the roll window, because weekend
# news moves the reopen as much (7 April 2025, 9 March 2026). A usual day's
# range is the median of up to ROLL_DAYS whole days before; ROLL_MIN_DAYS of
# them are needed. The opening-gap rule the plan proposed (4 times the usual
# gap and 1.5 ATR) marked every Monday and missed the real switches.
ROLL_JUMP = 0.5
ROLL_GAP = 1.0
ROLL_DAYS = 20
ROLL_MIN_DAYS = 5
ROLL_WINDOW = (10, 3)       # days before and after the third Friday of Mar, Jun, Sep, Dec

_INTERVAL_SEC = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
                 "60m": 3600, "90m": 5400, "1h": 3600, "2h": 7200, "4h": 14400,
                 "1d": DAY, "5d": 5 * DAY, "1wk": 7 * DAY, "1mo": 30 * DAY,
                 "3mo": 91 * DAY}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")

# Each key's type. Bar positions and days are int64, events int8, prices float64.
INT_KEYS = ("ck_day", "ck_min")
EVENT_KEYS = ("ck_sess", "ck_pd_take", "ck_pd_both", "ck_pd_back", "ck_sess_take",
              "ck_sess_both", "ck_sess_back", "ck_sess_src", "ck_gap_dir",
              "ck_gap_filled", "ck_wgap_filled", "ck_roll")
PRICE_KEYS = ("ck_pdh", "ck_pdl", "ck_pdc", "ck_pwh", "ck_pwl", "ck_open",
              "ck_day_hi", "ck_day_lo", "ck_asia_hi", "ck_asia_lo", "ck_lon_hi",
              "ck_lon_lo", "ck_pre_hi", "ck_pre_lo", "ck_sess_lvl", "ck_gap_lo",
              "ck_gap_hi", "ck_wgap_lo", "ck_wgap_hi")
KEYS = INT_KEYS + EVENT_KEYS + PRICE_KEYS

# ck_sess_src: which earlier session's range New York took.
SESS_SOURCES = ("", "london", "asia")


# --- New York time ------------------------------------------------------------

_OFFSETS: Dict[int, int] = {}


def _day_offset(day_number: int) -> int:
    """New York's offset at noon on a day (days since 1970), cached."""
    off = _OFFSETS.get(day_number)
    if off is None:
        off = sessions_mod.new_york_offset(date.fromordinal(EPOCH + day_number))
        if len(_OFFSETS) > 20000:
            _OFFSETS.clear()
        _OFFSETS[day_number] = off
    return off


def offsets(ts) -> np.ndarray:
    """New York's offset from UTC, in seconds, at each of these instants.

    The day's offset (sessions.new_york_offset, which says what noon is),
    except before 02:00 on the Sunday the clocks change, when the day before's
    still holds.
    """
    ts = np.asarray(ts, dtype=np.int64)
    if ts.size == 0:
        return np.zeros(0, dtype=np.int64)
    utc_day = ts // DAY
    lo, hi = int(utc_day.min()) - 2, int(utc_day.max()) + 2
    table = np.array([_day_offset(d) for d in range(lo, hi + 1)], dtype=np.int64)
    guess = table[utc_day - lo]
    local_day = (ts + guess) // DAY
    cur = table[local_day - lo]
    prev = table[local_day - 1 - lo]
    switch = local_day * DAY + 7200 - prev          # 02:00 there, on the old clock
    return np.where((cur != prev) & (ts < switch), prev, cur)


def ny_offset(ts) -> int:
    """New York's offset from UTC at one instant, in seconds (-14400 or -18000)."""
    return int(offsets(np.array([int(ts)], dtype=np.int64))[0])


def ny_clock(ts) -> Tuple[date, int, int]:
    """(New York date, minutes after midnight there, weekday with Monday 0)."""
    local = int(ts) + ny_offset(ts)
    day = date.fromordinal(EPOCH + local // DAY)
    return day, (local % DAY) // 60, day.weekday()


def label_ny(ts, intraday: bool = True) -> str:
    """'19 Nov 10:02' in New York time, or '19 Nov' for a day."""
    day, minute, _ = ny_clock(ts)
    text = "%d %s" % (day.day, _MONTHS[day.month - 1])
    return "%s %02d:%02d" % (text, minute // 60, minute % 60) if intraday else text


def fmt_ny(ts) -> str:
    """'10:02', the New York time of an instant."""
    _, minute, _ = ny_clock(ts)
    return "%02d:%02d" % (minute // 60, minute % 60)


def _in(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def session_of(ts) -> str:
    """Which of SESSIONS a time falls in, by New York time."""
    _, minute, _ = ny_clock(ts)
    for name, start, end in SESSIONS:
        if _in(minute, start, end):
            return name
    return "halt"


def window_of(ts) -> Optional[str]:
    """Which of the course's New York windows a time falls in, or None
    outside 09:30-16:00."""
    _, minute, _ = ny_clock(ts)
    for name, start, end in WINDOWS:
        if start <= minute < end:
            return name
    return None


def lab_window_of(ts) -> str:
    """Which of LAB_WINDOWS a time falls in: every minute of the day has one."""
    _, minute, _ = ny_clock(ts)
    for name, start, end in LAB_WINDOWS:
        if _in(minute, start, end):
            return name
    return "after"


# --- What kind of instrument -----------------------------------------------------

def interval_seconds(interval: str) -> int:
    """How long one bar of an interval lasts, or 0 when the interval is unknown."""
    return _INTERVAL_SEC.get((interval or "").lower(), 0)


def bar_seconds(bars) -> int:
    """How long one of these bars lasts, in seconds.

    From the interval the bars were fetched at. An interval this module does
    not know is read off the times: the shortest step between bars.
    """
    step = interval_seconds(getattr(bars, "interval", ""))
    if step:
        return step
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    diffs = np.diff(ts[:200])
    diffs = diffs[diffs > 0]
    return int(diffs.min()) if diffs.size else DAY


def instrument_of(bars) -> str:
    """"future", "coin" or "stock", whatever the bar size."""
    symbol = (getattr(bars, "symbol", "") or "").upper()
    meta = getattr(bars, "meta", None) or {}
    sort = str(meta.get("instrumentType") or "").upper()
    if symbol.endswith("=F") or sort == "FUTURE":
        return "future"
    if getattr(bars, "asset_class", "") == "crypto" or sort == "CRYPTOCURRENCY":
        return "coin"
    return "stock"


def kind_of(bars) -> str:
    """"daily", "future", "coin" or "stock".

    Daily for bars a day long or longer, whatever the instrument. Otherwise
    futures (a symbol ending =F, or the data says FUTURE) and coins trade
    round the clock with an 18:00 New York day; anything else is read as a
    stock, with a day session only.
    """
    if bar_seconds(bars) >= DAY:
        return "daily"
    return instrument_of(bars)


def elsewhere(bars) -> bool:
    """Whether this is a listing that does not trade New York's hours: a stock
    or index on an exchange abroad, or a currency.

    Told the way sessions._kind tells it: the time zone the data names, and
    the marks Yahoo keeps in symbols ("." before another exchange's suffix,
    "=" in a currency's). An index in New York's time zone (^GSPC) trades New
    York's hours. Futures and coins have rules of their own here.
    """
    if instrument_of(bars) != "stock":
        return False
    zone = (getattr(bars, "meta", None) or {}).get("exchangeTimezoneName")
    if zone and zone != sessions_mod.NEW_YORK:
        return True
    symbol = getattr(bars, "symbol", "") or ""
    return "." in symbol or "=" in symbol


def trading_day(ts, kind: str, bars=None) -> date:
    """The trading day a time belongs to: for futures and coins the day starts
    at 18:00 New York time; for a stock (and daily bars) it is the date there,
    or the exchange's own date when `bars` are of a listing elsewhere."""
    if bars is not None and elsewhere(bars):
        gmt = int(getattr(bars, "gmt_offset", 0) or 0)
        return date.fromordinal(EPOCH + (int(ts) + gmt) // DAY)
    day, minute, _ = ny_clock(ts)
    if kind in ("future", "coin") and minute >= 18 * 60:
        return day + timedelta(days=1)
    return day


def head(bars, n: int):
    """The first n bars, as Bars of their own, for replaying what was known.

    Nothing after them is kept: no live price, and only the splits and
    rebuilt days up to the last bar kept.
    """
    n = max(0, min(int(n), len(bars.close)))
    last = int(bars.timestamp[n - 1]) if n else 0
    factor = getattr(bars, "dividend_factor", None)
    return dataclasses.replace(
        bars, timestamp=bars.timestamp[:n], open=bars.open[:n], high=bars.high[:n],
        low=bars.low[:n], close=bars.close[:n], volume=bars.volume[:n],
        session_id=bars.session_id[:n], live_price=None,
        dividend_factor=(factor[:n] if factor is not None and len(factor) >= n else None),
        splits=[sp for sp in (getattr(bars, "splits", None) or []) if int(sp[0]) <= last],
        rebuilt=[t for t in (getattr(bars, "rebuilt", None) or []) if int(t) <= last])


def tail(bars, n: int):
    """The last n bars, as Bars of their own, for a read that needs no more."""
    n = max(1, int(n))
    if n >= len(bars.close):
        return bars
    first = int(bars.timestamp[-n])
    factor = getattr(bars, "dividend_factor", None)
    return dataclasses.replace(
        bars, timestamp=bars.timestamp[-n:], open=bars.open[-n:], high=bars.high[-n:],
        low=bars.low[-n:], close=bars.close[-n:], volume=bars.volume[-n:],
        session_id=bars.session_id[-n:],
        dividend_factor=(factor[-n:] if factor is not None and len(factor) >= n else None),
        splits=[sp for sp in (getattr(bars, "splits", None) or []) if int(sp[0]) >= first],
        rebuilt=[t for t in (getattr(bars, "rebuilt", None) or []) if int(t) >= first])


def live_day(bars) -> Optional[date]:
    """The trading day the live price is in, when that is a later one than the
    last finished bar's; None when it is the same day, or not known.

    A day's forming bar is kept apart from the finished ones
    (market.fetch_bars), so while a market trades the last finished daily bar
    is yesterday's, and so is the last intraday one until the day's first bar
    is over. What was yesterday as of that bar is then a day too old. Bars cut
    by head() have no live price and give None.
    """
    if not len(bars.close) or getattr(bars, "live_price", None) is None:
        return None
    try:
        when = int((getattr(bars, "meta", None) or {}).get("regularMarketTime") or 0)
    except (TypeError, ValueError):
        return None
    if when <= 0:
        return None
    two = np.array([int(bars.timestamp[-1]), when], dtype=np.int64)
    instrument = instrument_of(bars)
    last, live = _day_numbers(bars, two, instrument, kind_of(bars) == "daily").tolist()
    if instrument == "future":            # Sunday evening is Monday's, on daily bars too
        live = trading_day(when, instrument).toordinal() - EPOCH
    return date.fromordinal(EPOCH + live) if live > last else None


# --- Bars placed in New York time ------------------------------------------------

@dataclass
class _Frame:
    kind: str
    instrument: str
    step: int
    away: bool                    # elsewhere(): the exchange's own dates
    ts: np.ndarray
    minute: np.ndarray            # New York minutes after midnight, -1 for daily bars
    day: np.ndarray               # trading day, days since 1970
    blocks: List[Tuple[int, int, int]]   # (day, first bar, last bar + 1)


def _daily_days(bars, ts: np.ndarray, instrument: str) -> np.ndarray:
    """The date each daily bar is for.

    Yahoo stamps a coin's day at 00:00 UTC and the rest at their market's
    midnight or open. New York listings and US futures are read in New York
    time (by the day's own offset: the offset Yahoo gives is today's, an hour
    out for half the year, which put a summer futures day on the day before);
    the rest by their exchange's offset, as market.fetch_bars does.
    """
    gmt = int(getattr(bars, "gmt_offset", 0) or 0)
    meta = getattr(bars, "meta", None) or {}
    zone = meta.get("exchangeTimezoneName")
    new_york = (instrument != "coin"
                and (zone == sessions_mod.NEW_YORK or (not zone and gmt in (-18000, -14400))))
    if new_york:
        return (ts + offsets(ts)) // DAY
    return (ts + gmt) // DAY


def _day_numbers(bars, ts: np.ndarray, instrument: str, daily: bool) -> np.ndarray:
    """The trading day of each of these times, in days since 1970."""
    if daily:
        return _daily_days(bars, ts, instrument)
    if elsewhere(bars):
        return (ts + int(getattr(bars, "gmt_offset", 0) or 0)) // DAY
    local = ts + offsets(ts)
    day = local // DAY
    if instrument in ("future", "coin"):
        day = day + ((local % DAY) // 60 >= 18 * 60)
    return day


def _frame(bars) -> _Frame:
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    n = len(ts)
    instrument = instrument_of(bars)
    step = bar_seconds(bars) if n else DAY
    kind = "daily" if step >= DAY else instrument
    day = _day_numbers(bars, ts, instrument, kind == "daily")
    if kind == "daily":
        minute = np.full(n, -1, dtype=np.int64)
    else:
        minute = ((ts + offsets(ts)) % DAY) // 60
    blocks: List[Tuple[int, int, int]] = []
    if n:
        cuts = np.flatnonzero(np.diff(day)) + 1
        starts = [0] + cuts.tolist()
        ends = cuts.tolist() + [n]
        blocks = [(int(day[s]), s, e) for s, e in zip(starts, ends)]
    return _Frame(kind, instrument, int(step), kind != "daily" and elsewhere(bars),
                  ts, minute.astype(np.int64),
                  day.astype(np.int64), blocks)


def _session_minutes(start: int, end: int) -> int:
    return (end - start) % (24 * 60) or 24 * 60


def _inside(minute: np.ndarray, step_min: int, start: int, end: int) -> np.ndarray:
    """Bars lying wholly inside a session (start minute and end minute)."""
    if start < end:
        return (minute >= start) & (minute + step_min <= end)
    shifted = np.where(minute >= start, minute, minute + 24 * 60)
    return (shifted >= start) & (shifted + step_min <= end + 24 * 60)


def _expected_day(fr: _Frame, day_number: int) -> int:
    """How many bars a whole trading day has at this bar size."""
    if fr.instrument == "future":
        minutes = 23 * 60                       # 18:00 to 17:00
    elif fr.instrument == "coin":
        minutes = 24 * 60
    else:
        hours = sessions_mod.us_session(date.fromordinal(EPOCH + day_number))
        minutes = (hours[1] - hours[0]) // 60 if hours else 6 * 60 + 30
    return int(math.ceil(minutes * 60 / fr.step))


def _complete(count: int, expected: int) -> bool:
    return expected > 0 and count >= COMPLETE_SHARE * expected


def _reaches(ts: np.ndarray, step: int, day_number: int) -> bool:
    """Whether a US stock's bars reach from the open to the close with no
    stretch longer than a fifth of the session (1 - COMPLETE_SHARE) missing.

    The source has no bar where nothing traded, so a thinly traded stock can
    have two bars in three all day, and its high and low are still the day's.
    Counted, its days were all incomplete, and the levels called yesterday's
    were from weeks before. A hole in the data leaves one long stretch empty.
    """
    d = date.fromordinal(EPOCH + day_number)
    hours = sessions_mod.us_session(d)
    if hours is None:                     # trading on a day the calendar has shut
        opened = ny(d.year, d.month, d.day, 9, 30)
        hours = (opened, opened + 390 * 60)
    ends = np.concatenate([[hours[0]], ts + step])
    starts = np.concatenate([ts, [hours[1]]])
    return bool(np.max(starts - ends) <= (1 - COMPLETE_SHARE) * (hours[1] - hours[0]) + 1e-6)


# --- Days and sessions ---------------------------------------------------------------

@dataclass
class Session:
    """One session of one trading day: the bars lying wholly inside it."""
    name: str
    bars: int = 0
    expected: int = 0
    complete: bool = False
    first: int = -1               # bar positions, inclusive; -1 when none
    last: int = -1
    open: float = NAN
    high: float = NAN
    low: float = NAN
    close: float = NAN
    high_at: int = 0              # start times (UTC) of the bars that made them
    low_at: int = 0


@dataclass
class Day:
    """One trading day, as day_table() reads it."""
    day: date
    weekday: int
    week: Tuple[int, int]         # ISO year and week of the trading day
    first: int                    # bar positions, inclusive
    last: int
    bars: int
    expected: int
    complete: bool
    open: float
    high: float
    low: float
    close: float
    high_at: int
    low_at: int
    prev_close: float = NAN       # the day before's last close in the data
    gap: float = NAN              # open minus that close (NaN for coins)
    gap_filled_at: int = 0        # start time of the bar that traded back to it
    roll: bool = False            # a futures roll or a break in the data (see ROLL_JUMP)
    roll_at: int = -1             # the bar it was seen at (from there on it is known)
    sessions: Dict[str, Session] = field(default_factory=dict)


def _stats(o, h, l, c, ts, idx: np.ndarray, name: str, expected: int) -> Session:
    s = Session(name, int(idx.size), int(expected), _complete(int(idx.size), expected))
    if idx.size:
        hi_rel = int(np.argmax(h[idx]))
        lo_rel = int(np.argmin(l[idx]))
        s.first, s.last = int(idx[0]), int(idx[-1])
        s.open, s.close = float(o[idx[0]]), float(c[idx[-1]])
        s.high, s.low = float(h[idx[hi_rel]]), float(l[idx[lo_rel]])
        s.high_at, s.low_at = int(ts[idx[hi_rel]]), int(ts[idx[lo_rel]])
    return s


def _days(bars, fr: _Frame) -> List[Day]:
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    ts = fr.ts
    step_min = max(1, fr.step // 60)
    out: List[Day] = []
    ranges_seen: List[float] = []        # whole days' ranges, for the roll rule
    counts: List[int] = []               # bars of the days before, for a listing elsewhere
    for b, (day_number, s, e) in enumerate(fr.blocks):
        d = date.fromordinal(EPOCH + day_number)
        idx = np.arange(s, e)
        whole = _stats(o, h, l_, c, ts, idx, "day", 1)
        if fr.away:
            # Its exchange's hours are not known here: a whole day is told by
            # how many bars its earlier days had. The first has none to go by.
            expected = int(np.median(counts[-ROLL_DAYS:])) if counts else 0
            counts.append(e - s)
        else:
            expected = 1 if fr.kind == "daily" else _expected_day(fr, day_number)
        complete = _complete(e - s, expected)
        if not complete and fr.kind == "stock" and not fr.away:
            complete = _reaches(ts[s:e], fr.step, day_number)
        iso = d.isocalendar()
        day = Day(day=d, weekday=d.weekday(), week=(iso[0], iso[1]), first=s, last=e - 1,
                  bars=e - s, expected=expected, complete=complete,
                  open=whole.open, high=whole.high, low=whole.low, close=whole.close,
                  high_at=whole.high_at, low_at=whole.low_at)
        if fr.kind != "daily":
            minute = fr.minute[s:e]
            for name, start, end in SESSIONS:
                inside = idx[_inside(minute, step_min, start, end)]
                want = _session_minutes(start, end) // step_min
                if fr.away or (fr.instrument == "stock"
                               and not (9 * 60 + 30 <= start < end <= 16 * 60)):
                    want = 0                    # a stock's data has nothing outside the day session
                day.sessions[name] = _stats(o, h, l_, c, ts, inside, name, want)
        if b > 0 and fr.instrument != "coin":
            prev = float(c[s - 1])
            day.prev_close = prev
            day.gap = float(o[s]) - prev
            if day.gap != 0:
                hit = (l_[s:e] <= prev) if day.gap > 0 else (h[s:e] >= prev)
                where = np.flatnonzero(hit)
                if where.size:
                    day.gap_filled_at = int(ts[s + where[0]])
        if fr.instrument == "future" and len(ranges_seen) >= ROLL_MIN_DAYS:
            # Only whole days before this one are used, and a jump is known
            # from its own bar, so the flag never depends on a later bar.
            usual = float(np.median(ranges_seen[-ROLL_DAYS:]))
            if b > 0 and abs(day.gap) > ROLL_GAP * usual and _roll_window(d):
                day.roll, day.roll_at = True, s
            elif e - s > 1:
                jumps = np.abs(o[s + 1:e] - c[s:e - 1])
                where = np.flatnonzero(jumps > ROLL_JUMP * usual)
                if where.size:
                    day.roll, day.roll_at = True, s + 1 + int(where[0])
        if day.complete and not day.roll:
            ranges_seen.append(day.high - day.low)
        out.append(day)
    return out


def _roll_window(day: date) -> bool:
    """Whether a day is near a quarterly expiry: from ROLL_WINDOW[0] days before
    the third Friday of March, June, September or December to ROLL_WINDOW[1]
    after it. The equity index futures (ES, NQ, YM, RTY and their micros) roll
    then."""
    for month in (3, 6, 9, 12):
        third = date(day.year, month, 15)
        third += timedelta(days=(4 - third.weekday()) % 7)
        if -ROLL_WINDOW[1] <= (third - day).days <= ROLL_WINDOW[0]:
            return True
    return False


def day_table(bars) -> List[Day]:
    """Every trading day in the bars, oldest first, with its sessions, its
    completeness, its opening gap and whether it was a futures roll.

    The last day may still be under way; its completeness says so.
    """
    if len(bars.close) == 0:
        return []
    return _days(bars, _frame(bars))


# --- Series ------------------------------------------------------------------------

def empty(n: int) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}
    for key in INT_KEYS:
        out[key] = np.full(n, -1, dtype=np.int64)
    for key in EVENT_KEYS:
        out[key] = np.zeros(n, dtype=np.int8)
    out["ck_sess"][:] = -1
    for key in PRICE_KEYS:
        out[key] = np.full(n, np.nan, dtype=float)
    return out


def series(bars) -> Dict[str, np.ndarray]:
    """The clock and the day's levels at each bar, as of its close.

    ck_day (the trading day as a date ordinal), ck_min (New York minutes after
    midnight, -1 on daily bars), ck_sess (index into SESSIONS, -1 daily);
    ck_pdh, ck_pdl, ck_pdc: the previous complete trading day's high, low and
    close; ck_pwh, ck_pwl: the previous week's; ck_open, ck_day_hi, ck_day_lo:
    today's open, high and low so far; ck_asia_hi/lo, ck_lon_hi/lo,
    ck_pre_hi/lo: today's finished Asia, London and before-the-open ranges
    (futures and coins), from the first bar after each ends; ck_pd_take (+1 at
    the first bar today below yesterday's low, -1 above its high), ck_pd_both,
    ck_pd_back (closed back inside); ck_sess_take, ck_sess_both, ck_sess_back,
    ck_sess_lvl, ck_sess_src: the same for New York (08:30-12:00) and London's
    range, or Asia's when London's is incomplete; ck_gap_lo/hi, ck_gap_dir,
    ck_gap_filled: today's opening gap; ck_wgap_lo/hi, ck_wgap_filled: the
    week's (futures); ck_roll: 1 from the bar a futures roll or a break in the
    data was seen at, to the end of that day (see ROLL_JUMP).
    """
    n = len(bars.close)
    out = empty(n)
    if n == 0:
        return out
    fr = _frame(bars)
    out["ck_day"] = fr.day + EPOCH
    out["ck_min"] = fr.minute.copy()
    days = _days(bars, fr)
    if fr.kind == "daily":
        _daily(bars, fr, days, out)
    else:
        _intraday(bars, fr, days, out)
    return out


def _sess_index(minute: np.ndarray) -> np.ndarray:
    out = np.full(len(minute), len(SESSIONS) - 1, dtype=np.int8)
    for k, (_, start, end) in enumerate(SESSIONS):
        if start < end:
            mask = (minute >= start) & (minute < end)
        else:
            mask = (minute >= start) | (minute < end)
        out[mask] = k
    return out


def _weeks(days: List[Day]):
    """For each day, the previous week's (high, low, last close), or None.

    app choice: the first week in the data counts only when it starts on a
    Monday with a whole day: otherwise it is a part-week, and its high and
    low are not that week's.
    """
    by_week: Dict[Tuple[int, int], list] = {}
    order: List[Tuple[int, int]] = []
    for d in days:
        w = by_week.get(d.week)
        if w is None:
            w = by_week[d.week] = [d.high, d.low, d.close, d.first, d.open]
            order.append(d.week)
        else:
            w[0] = max(w[0], d.high)
            w[1] = min(w[1], d.low)
            w[2] = d.close
    first_ok = bool(days) and days[0].weekday == 0 and days[0].complete
    out = []
    for d in days:
        monday = d.day - timedelta(days=d.weekday)
        iso = (monday - timedelta(days=7)).isocalendar()
        key = (iso[0], iso[1])
        prev = by_week.get(key)
        if prev is None or (key == order[0] and not first_ok):
            out.append(None)
        else:
            out.append(prev)
    return out


def _daily(bars, fr: _Frame, days: List[Day], out: Dict[str, np.ndarray]) -> None:
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    n = len(c)
    out["ck_open"] = o.copy()
    out["ck_day_hi"] = h.copy()
    out["ck_day_lo"] = l_.copy()
    if n > 1:
        # Daily bars: yesterday is the bar before.
        out["ck_pdh"][1:] = h[:-1]
        out["ck_pdl"][1:] = l_[:-1]
        out["ck_pdc"][1:] = c[:-1]
        took_lo = l_[1:] < l_[:-1]
        took_hi = h[1:] > h[:-1]
        both = took_lo & took_hi
        take = np.where(both, 0, np.where(took_lo, 1, np.where(took_hi, -1, 0)))
        back = np.where(take == 1, c[1:] > l_[:-1], np.where(take == -1, c[1:] < h[:-1], False))
        out["ck_pd_take"][1:] = take
        out["ck_pd_both"][1:] = both
        out["ck_pd_back"][1:] = back
    weeks = _weeks(days)
    week_open: Dict[Tuple[int, int], Tuple[float, float]] = {}
    filled_week: Dict[Tuple[int, int], bool] = {}
    for b, d in enumerate(days):
        i = d.first
        prev = weeks[b]
        if prev is not None:
            out["ck_pwh"][i], out["ck_pwl"][i] = prev[0], prev[1]
        if fr.instrument != "coin" and b > 0 and d.gap == d.gap and d.gap != 0:
            out["ck_gap_lo"][i] = min(d.open, d.prev_close)
            out["ck_gap_hi"][i] = max(d.open, d.prev_close)
            out["ck_gap_dir"][i] = 1 if d.gap > 0 else -1
            out["ck_gap_filled"][i] = 1 if d.gap_filled_at else 0
        if fr.instrument == "future":
            out["ck_roll"][i] = 1 if d.roll else 0
            if b > 0 and days[b - 1].week != d.week and d.prev_close == d.prev_close \
                    and d.open != d.prev_close:
                week_open[d.week] = (d.open, d.prev_close)
                filled_week[d.week] = False
            wk = week_open.get(d.week)
            if wk is not None:
                out["ck_wgap_lo"][i], out["ck_wgap_hi"][i] = min(wk), max(wk)
                if not filled_week[d.week]:
                    hit = l_[i] <= wk[1] if wk[0] > wk[1] else h[i] >= wk[1]
                    filled_week[d.week] = bool(hit)
                out["ck_wgap_filled"][i] = 1 if filled_week[d.week] else 0


def _scan_take(o, h, l_, c, rows, hi_level: float, lo_level: float,
               take, both, back, lvl=None):
    """The first bar among rows past each side of a range: +1 below the low,
    -1 above the high, both on one bar 0 with both set."""
    low_done = not (lo_level == lo_level)
    high_done = not (hi_level == hi_level)
    for j in rows:
        if low_done and high_done:
            break
        tl = not low_done and l_[j] < lo_level
        th = not high_done and h[j] > hi_level
        if tl and th:
            both[j] = 1
            low_done = high_done = True
        elif tl:
            take[j] = 1
            back[j] = 1 if c[j] > lo_level else 0
            if lvl is not None:
                lvl[j] = lo_level
            low_done = True
        elif th:
            take[j] = -1
            back[j] = 1 if c[j] < hi_level else 0
            if lvl is not None:
                lvl[j] = hi_level
            high_done = True


def _intraday(bars, fr: _Frame, days: List[Day], out: Dict[str, np.ndarray]) -> None:
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    O, H, L, C = o.tolist(), h.tolist(), l_.tolist(), c.tolist()
    minute = fr.minute
    out["ck_sess"] = _sess_index(minute)
    round_clock = fr.instrument in ("future", "coin")
    weeks = _weeks(days)

    pd_take, pd_both, pd_back = [0] * len(C), [0] * len(C), [0] * len(C)
    st_take, st_both, st_back = [0] * len(C), [0] * len(C), [0] * len(C)
    st_lvl = [NAN] * len(C)
    st_src = out["ck_sess_src"]

    last_complete: Optional[Day] = None
    week_gap: Optional[Tuple[float, float]] = None   # (open, last week's close)
    week_filled = False
    for b, d in enumerate(days):
        s, e = d.first, d.last + 1
        mins = minute[s:e]
        out["ck_open"][s:e] = d.open
        out["ck_day_hi"][s:e] = np.maximum.accumulate(h[s:e])
        out["ck_day_lo"][s:e] = np.minimum.accumulate(l_[s:e])

        # video 2:05:49-2:08:58: yesterday's high and low, from the last whole day.
        if last_complete is not None:
            pdh, pdl = last_complete.high, last_complete.low
            out["ck_pdh"][s:e], out["ck_pdl"][s:e] = pdh, pdl
            out["ck_pdc"][s:e] = last_complete.close
            _scan_take(O, H, L, C, range(s, e), pdh, pdl, pd_take, pd_both, pd_back)
        prev_week = weeks[b]
        if prev_week is not None:
            out["ck_pwh"][s:e], out["ck_pwl"][s:e] = prev_week[0], prev_week[1]

        if round_clock:
            # Each range is known once its session is over: from the first
            # bar after it, to the end of the day.
            done_at = {"asia": 3 * 60, "london": 8 * 60 + 30, "ny_pre": 9 * 60 + 30}
            keys = {"asia": ("ck_asia_hi", "ck_asia_lo"), "london": ("ck_lon_hi", "ck_lon_lo"),
                    "ny_pre": ("ck_pre_hi", "ck_pre_lo")}
            for name, (k_hi, k_lo) in keys.items():
                sess = d.sessions[name]
                if not sess.complete:
                    continue
                after = np.flatnonzero((mins >= done_at[name]) & (mins < 18 * 60))
                if after.size:
                    out[k_hi][s + after[0]:e] = sess.high
                    out[k_lo][s + after[0]:e] = sess.low
            # video 2:02:39, 4:31:11: New York takes London's high or low
            # (app choice: Asia's when London's bars are incomplete).
            source = 1 if d.sessions["london"].complete else (
                2 if d.sessions["asia"].complete else 0)
            if source:
                rng = d.sessions["london"] if source == 1 else d.sessions["asia"]
                rows = [s + j for j in np.flatnonzero((mins >= 8 * 60 + 30) & (mins < 12 * 60))]
                _scan_take(O, H, L, C, rows, rng.high, rng.low, st_take, st_both, st_back,
                           st_lvl)
                for j in rows:
                    if st_take[j] or st_both[j]:
                        st_src[j] = source

        # video 3:16:24-3:23:47: the gap a new day opens with (futures: the
        # 18:00 reopen against the 17:00 close; stocks: 09:30 against the
        # close before). A coin never shuts, so it has none.
        if b > 0 and fr.instrument != "coin" and d.gap == d.gap and d.gap != 0:
            out["ck_gap_lo"][s:e] = min(d.open, d.prev_close)
            out["ck_gap_hi"][s:e] = max(d.open, d.prev_close)
            out["ck_gap_dir"][s:e] = 1 if d.gap > 0 else -1
            if d.gap_filled_at:
                j = int(np.searchsorted(fr.ts[s:e], d.gap_filled_at))
                out["ck_gap_filled"][s + j:e] = 1
        if fr.instrument == "future":
            if d.roll:
                out["ck_roll"][d.roll_at:e] = 1      # from the bar it was seen at
            # video 3:19:32, 3:25:57: the week's opening gap, Friday's close
            # against Sunday's 18:00 reopen, kept for the whole week.
            if b > 0 and days[b - 1].week != d.week:
                week_gap = None
                week_filled = False
                if d.prev_close == d.prev_close and d.open != d.prev_close:
                    week_gap = (d.open, d.prev_close)
            elif b == 0:
                week_gap = None
            if week_gap is not None:
                out["ck_wgap_lo"][s:e] = min(week_gap)
                out["ck_wgap_hi"][s:e] = max(week_gap)
                if week_filled:
                    out["ck_wgap_filled"][s:e] = 1
                else:
                    hit = (l_[s:e] <= week_gap[1]) if week_gap[0] > week_gap[1] \
                        else (h[s:e] >= week_gap[1])
                    where = np.flatnonzero(hit)
                    if where.size:
                        out["ck_wgap_filled"][s + where[0]:e] = 1
                        week_filled = True

        if d.complete:
            last_complete = d

    out["ck_pd_take"] = np.asarray(pd_take, dtype=np.int8)
    out["ck_pd_both"] = np.asarray(pd_both, dtype=np.int8)
    out["ck_pd_back"] = np.asarray(pd_back, dtype=np.int8)
    out["ck_sess_take"] = np.asarray(st_take, dtype=np.int8)
    out["ck_sess_both"] = np.asarray(st_both, dtype=np.int8)
    out["ck_sess_back"] = np.asarray(st_back, dtype=np.int8)
    out["ck_sess_lvl"] = np.asarray(st_lvl, dtype=float)


# --- Longer bars built from shorter ones -------------------------------------------

def buckets(bars, hours: int = 4) -> List[Tuple[int, int, int, int, int]]:
    """Every bucket of `hours` the bars fall in, still forming or not.

    Each is (start time, first bar, last bar + 1, the time it ends, trading
    day). It ends `hours` after it starts, or earlier at the futures' 17:00
    pause or a stock's close. resample() keeps the ones that are over.
    """
    fr = _frame(bars)
    if fr.kind == "daily":
        raise ValueError("resample needs bars shorter than a day")
    length = int(hours) * 60
    n = len(fr.ts)
    if n == 0:
        return []
    minute = fr.minute
    if fr.instrument in ("future", "coin"):
        since = (minute - 18 * 60) % (24 * 60)
    elif fr.away:
        # From the first bar of the exchange's own day.
        first = np.zeros(n, dtype=np.int64)
        for _, s, e in fr.blocks:
            first[s:e] = fr.ts[s]
        since = (fr.ts - first) // 60
    else:
        since = minute - (9 * 60 + 30)
    bucket = since // length
    key = fr.day * 64 + bucket
    cuts = np.flatnonzero(np.diff(key)) + 1
    out = []
    for s, e in zip([0] + cuts.tolist(), cuts.tolist() + [n]):
        b = int(bucket[s])
        start = int(fr.ts[s]) - int(since[s] - b * length) * 60
        end = start + length * 60
        if fr.instrument == "future":
            end = start + (min((b + 1) * length, 23 * 60) - b * length) * 60   # the 17:00 pause
        elif fr.instrument == "stock" and not fr.away:
            hours_day = sessions_mod.us_session(date.fromordinal(EPOCH + int(fr.day[s])))
            if hours_day:
                end = min(end, hours_day[1])
        out.append((start, s, e, end, int(fr.day[s])))
    return out


def resample(bars, hours: int = 4):
    """Bars `hours` long built from intraday ones, the way the course reads
    a 4-hour chart (Yahoo has no 4-hour bars).

    Futures and coins: buckets from the 18:00 New York reopen (18:00, 22:00,
    02:00, 06:00, 10:00, 14:00 for four hours; the last one ends at the 17:00
    pause for futures). Stocks: from 09:30 (09:30-13:30, 13:30-16:00), or from
    the day's first bar for a listing elsewhere. The
    bucket still forming at the end is dropped: a bucket counts once its time
    is up, or the market shut for the day, by the last bar given.
    """
    rows = buckets(bars, hours)
    if not rows:
        return head(bars, 0)
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    v = np.asarray(bars.volume, dtype=float)
    # The last bucket is kept only once its time is up.
    step = bar_seconds(bars)
    _, _, e, end, _ = rows[-1]
    if int(bars.timestamp[e - 1]) + step < end:
        rows = rows[:-1]
    rows = [(r[0], r[1], r[2], 0, r[4]) for r in rows]
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    return dataclasses.replace(
        bars, interval="%dh" % int(hours), timestamp=ts,
        open=np.array([o[r[1]] for r in rows], dtype=float),
        high=np.array([h[r[1]:r[2]].max() for r in rows], dtype=float),
        low=np.array([l_[r[1]:r[2]].min() for r in rows], dtype=float),
        close=np.array([c[r[2] - 1] for r in rows], dtype=float),
        volume=np.array([v[r[1]:r[2]].sum() for r in rows], dtype=float),
        session_id=np.array([r[4] for r in rows], dtype=np.int64),
        live_price=None, prev_close=None, dividend_factor=None, splits=[], rebuilt=[],
        last_bar_time="")


def utc(y: int, mo: int, d: int, hh: int = 0, mm: int = 0) -> int:
    """A UTC moment as epoch seconds; for tests and fixtures."""
    return calendar.timegm((y, mo, d, hh, mm, 0))


def ny(y: int, mo: int, d: int, hh: int = 0, mm: int = 0) -> int:
    """A New York wall-clock moment as epoch seconds (UTC)."""
    naive = calendar.timegm((y, mo, d, hh, mm, 0))
    guess = naive - _day_offset((naive // DAY))
    return naive - ny_offset(guess)
