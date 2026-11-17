"""Trading sessions: when an instrument's price can move, so alerts ask only then.

(Market sessions, not sign-in ones: those are accounts.py's.)

The background loop checks every alert and every open position's stop on a
timer, every five minutes unless the operator changes it. A stock's price
cannot change while its market is closed, and a US stock trades about six and
a half hours a weekday, under a fifth of the week, so most of those requests
asked a closed market again for the price it had already given.

This answers one question for the loop: could this instrument's price have
changed between the last check that worked and now? Whenever the answer is not
certain, it is yes. Skipping must never hide a change, so whatever this module
does not know for sure is checked as it always was:

- A stock or exchange-traded fund listed in the US trades from 9:30 to 16:00
  New York time on weekdays, except on the NYSE's full holidays, and stops at
  13:00 on its early-close days. All of that is worked out by rule, for any
  year, and checked against the hours the stock's own data gives.
- A listing elsewhere is judged by the hours its own market data gave at the
  last download: the session it described, and the next one once the data
  names it. Holidays there are not known, so they are checked like any day.
- Coins trade all the time. Mutual funds, whose price is set hours after the
  close, indices, whose final values also come in late, and anything whose
  data has not been read yet are checked every time.

What the data said is kept per symbol by note(), which the alert checks call
with every download, so nothing here asks the data source for anything.

A close is not the end of a day's prices: the closing auction, late reports
and sources that show a market some minutes late can still change the day's
bar after it, and daily alerts, changes of call and stop notices all read that
bar. So a market counts as open until SETTLE after its close, and the checks
go on until one has looked after that. The next check is at the open.

In the US only the regular session counts: every price an alert reads is a
regular-session one (market.fetch_bars asks for no pre- or after-market
prices), so trading outside those hours changes nothing an alert sees.
Elsewhere a session is taken to last until its after-market ends, because
Yahoo's regular hours there can stop short of the market's own close.
"""
from __future__ import annotations

import calendar
import functools
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

DAY = 86400

# How long after a close the day's prices may still be written. London's last
# price of the day came 19 minutes after its regular close in November 2026;
# a mutual fund's comes hours later, which is why funds are never skipped.
SETTLE = 3600

# The NYSE's and Nasdaq's regular session, in New York time.
_OPEN, _CLOSE, _EARLY = (9, 30), (16, 0), (13, 0)
NEW_YORK = "America/New_York"
_MONDAY, _THURSDAY, _SUNDAY = 0, 3, 6

# Any week holds a US session, so a check older than this never waits, and
# the days between two times are never counted out past it.
_WEEK = 7 * DAY


# --- The US calendar --------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _zone():
    """New York from the time zone database, or None when there is none.

    Windows ships no time zone database, and Python finds one there only when
    the tzdata package is installed, which nothing else here needs.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(NEW_YORK)
    except Exception:
        return None


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The nth weekday of a month (Monday is 0); n of -1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def new_york_offset(day: date) -> int:
    """New York's offset from UTC on a day, in seconds: four or five hours behind.

    From the time zone database when there is one. Without it, by the rule in
    force since 2007: summer time from the second Sunday in March to the first
    Sunday in November. The clocks change at 2am on a Sunday, when no market
    is open, so one offset holds for the whole of any trading day.
    """
    zone = _zone()
    if zone is not None:
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
        return int(noon.utcoffset().total_seconds())
    summer = _nth(day.year, 3, _SUNDAY, 2) <= day < _nth(day.year, 11, _SUNDAY, 1)
    return -4 * 3600 if summer else -5 * 3600


def easter(year: int) -> date:
    """Easter Sunday in the Gregorian calendar, by the anonymous algorithm
    (Meeus, Jones and Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    to_sunday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * to_sunday) // 451
    month, day = divmod(h + to_sunday - 7 * m + 114, 31)
    return date(year, month, day + 1)


@functools.lru_cache(maxsize=32)
def _holidays(year: int) -> Dict[date, str]:
    def moved(day: date, saturday: bool = True) -> Optional[date]:
        # A holiday on a Saturday is taken on the Friday before it, and one
        # on a Sunday on the Monday after.
        if day.weekday() == 5:
            return day - timedelta(days=1) if saturday else None
        if day.weekday() == 6:
            return day + timedelta(days=1)
        return day

    found = [
        # New Year's Day on a Saturday moves nowhere: the Friday before it
        # closes the year's books, and the exchange opens (31 December 2021).
        (moved(date(year, 1, 1), saturday=False), "New Year's Day"),
        (_nth(year, 1, _MONDAY, 3), "Martin Luther King Jr. Day"),
        (_nth(year, 2, _MONDAY, 3), "Washington's Birthday"),
        (easter(year) - timedelta(days=2), "Good Friday"),
        (_nth(year, 5, _MONDAY, -1), "Memorial Day"),
        (moved(date(year, 6, 19)) if year >= 2022 else None, "Juneteenth"),
        (moved(date(year, 7, 4)), "Independence Day"),
        (_nth(year, 9, _MONDAY, 1), "Labor Day"),
        (_nth(year, 11, _THURSDAY, 4), "Thanksgiving Day"),
        (moved(date(year, 12, 25)), "Christmas Day"),
    ]
    return {day: name for day, name in found if day is not None}


def nyse_holidays(year: int) -> Dict[date, str]:
    """The weekdays the NYSE is shut all day in a year, by the rules it publishes.

    Juneteenth has been one since 2022. Closures no rule foresees, a national
    day of mourning or a storm, are not here: on those days the loop checks a
    closed market, which costs a few requests and hides nothing.
    """
    return dict(_holidays(year))


def early_close(day: date) -> bool:
    """Whether the NYSE stops at 13:00 on a day: the day before Independence
    Day, the day after Thanksgiving and Christmas Eve, each when it trades.

    So 3 July 2025 and 24 December 2026 did, and 3 July 2026 is a holiday
    instead: Independence Day fell on a Saturday.
    """
    if day.weekday() >= 5 or day in _holidays(day.year):
        return False
    return (day == _nth(day.year, 11, _THURSDAY, 4) + timedelta(days=1)
            or (day.month, day.day) in ((7, 3), (12, 24)))


def us_session(day: date) -> Optional[Tuple[int, int]]:
    """The regular session on a New York date as (open, close) in UTC seconds,
    or None when the market does not open that day."""
    if day.weekday() >= 5 or day in _holidays(day.year):
        return None
    midnight = calendar.timegm(day.timetuple()) - new_york_offset(day)
    shut = _EARLY if early_close(day) else _CLOSE
    return (midnight + _OPEN[0] * 3600 + _OPEN[1] * 60,
            midnight + shut[0] * 3600 + shut[1] * 60)


def _days(since: float, now: float):
    """Every date that a US session between two times could fall on.

    A New York session runs from 13:30 to 21:00 UTC at the widest, so its UTC
    date is its New York date; one day either side is a margin.
    """
    day = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=1)
    last = datetime.fromtimestamp(now, timezone.utc).date() + timedelta(days=1)
    while day <= last:
        yield day
        day += timedelta(days=1)


# --- What each instrument's data said ---------------------------------------

@dataclass
class Hours:
    """What an instrument's own market data last said about when it trades."""
    kind: str                     # "us", "elsewhere", "coin" or "other"
    start: Optional[int] = None   # the regular session it described, in UTC
    end: Optional[int] = None     # seconds: the current one or the next
    until: Optional[int] = None   # the end of the after-market, when later
    offset: int = 0               # the exchange's offset from UTC, in seconds
    first_read: float = 0.0       # when that session was first described
    last_read: float = 0.0        # and when it was last


# Bounded: one entry per symbol that has an alert or a position.
KEPT = 5000
_SEEN: Dict[str, Hours] = {}
_SEEN_LOCK = threading.Lock()


def _kind(symbol: str, meta: dict) -> str:
    """Which rules an instrument trades by, from what its data says it is."""
    sort = str(meta.get("instrumentType") or "").upper()
    if sort == "CRYPTOCURRENCY" or symbol.endswith("-USD"):
        return "coin"
    # A fund's price is set hours after the close, an index's final value
    # comes in late too, and futures and currencies trade through the night.
    if sort not in ("EQUITY", "ETF"):
        return "other"
    # Yahoo gives a listing elsewhere a suffix (VOD.L) and keeps ^ and = for
    # indices, futures and currencies, so a bare ticker in New York's time
    # zone is a US listing.
    if meta.get("exchangeTimezoneName") == NEW_YORK and not any(
            mark in symbol for mark in ".^="):
        return "us"
    return "elsewhere"


def note(symbol: str, bars, at: Optional[float] = None) -> None:
    """Keep what a fresh download said about its instrument's trading hours.

    Called with every download the alert checks make, at the time it was read.
    Yahoo's chart answer names the instrument's type and time zone, and the
    regular session of the trading day it is on, or of the next one once that
    day is over, in UTC. Never raises: an answer it cannot read leaves that
    instrument checked every time, which is what not knowing means here.
    """
    try:
        meta = getattr(bars, "meta", None)
        if isinstance(meta, dict):
            _note((symbol or "").upper(), meta,
                  time.time() if at is None else float(at))
            return
    except Exception:
        pass
    with _SEEN_LOCK:
        _SEEN.pop((symbol or "").upper(), None)


def _note(symbol: str, meta: dict, at: float) -> None:
    periods = meta.get("currentTradingPeriod") or {}
    try:
        regular = periods["regular"]
        start, end = int(regular["start"]), int(regular["end"])
        offset = int(regular.get("gmtoffset", meta.get("gmtoffset")) or 0)
    except (KeyError, TypeError, ValueError):
        start = end = None
        offset = 0
    if start is None or not 0 < start < end:
        start = end = None
    # Yahoo's regular hours can stop short of a market's own close: Seoul's
    # end at 15:00 there, and its closing auction runs to 15:30. Outside the
    # US the session is taken to last until its after-market ends.
    until = end
    try:
        after = int(periods["post"]["end"])
        if end is not None and end < after < start + DAY:
            until = after
    except (KeyError, TypeError, ValueError):
        pass
    with _SEEN_LOCK:
        old = _SEEN.get(symbol)
        same = (old is not None and start is not None
                and (old.start, old.end) == (start, end))
        if old is None and len(_SEEN) >= KEPT:
            del _SEEN[min(_SEEN, key=lambda k: _SEEN[k].last_read)]
        _SEEN[symbol] = Hours(_kind(symbol, meta), start, end, until, offset,
                              old.first_read if same else at, at)


def _seen(symbol: str) -> Optional[Hours]:
    with _SEEN_LOCK:
        return _SEEN.get((symbol or "").upper())


def _calendar_agrees(hours: Hours) -> bool:
    """Whether the session the data described fits inside the calendar's.

    The calendar is the rule and the data is the check on it. If the data has
    the market trading when the calendar has it shut, a clock an hour out or
    hours the rule does not know, the calendar is not used for that instrument
    until a later download agrees with it again. A shorter session in the
    data, an early close the rule missed, needs nothing: the loop only checks
    for longer than it had to.
    """
    if hours.start is None:
        return False
    day = datetime.fromtimestamp(hours.start, timezone.utc).date()
    session = us_session(day)
    return (session is not None and session[0] <= hours.start
            and hours.end <= session[1])


def _us_can_change(since: float, now: float) -> bool:
    """Whether a US session, or the hour settling one, lies between two times."""
    if now - since > _WEEK:
        return True
    for day in _days(since, now):
        session = us_session(day)
        if session and session[0] <= now and since < session[1] + SETTLE:
            return True
    return False


def _elsewhere_can_change(hours: Hours, since: float, now: float) -> bool:
    """The same for a listing elsewhere, from the hours its data gave."""
    if hours.start is None:
        return True
    if hours.start <= hours.last_read:
        # A session that had begun when it was read. Its prices are settled
        # SETTLE after its close, and no market opens twice in one day of its
        # own, so nothing moves before the next midnight where it trades.
        midnight = ((hours.until + hours.offset) // DAY + 1) * DAY - hours.offset
        return not (since >= hours.until + SETTLE and now < midnight)
    # The next session, not begun when it was read: the market was shut from
    # that read until the session opens. Whenever the session before it ended,
    # its prices were settled SETTLE after that first read.
    return not (since >= hours.first_read + SETTLE and now < hours.start)


def can_change(symbol: str, since: Optional[float], now: Optional[float] = None) -> bool:
    """Could this instrument's price have changed between `since`, the time of
    the last check that worked, and now?

    False only when that is certain: a stock whose market has been closed,
    and settled, the whole time. True for a coin, a fund, an index, anything
    whose data has not been read yet, and whenever there was no good check.
    """
    now = time.time() if now is None else float(now)
    if not since or now <= since:
        return True
    hours = _seen(symbol)
    if hours is None:
        return True
    if hours.kind == "us":
        return not _calendar_agrees(hours) or _us_can_change(float(since), now)
    if hours.kind == "elsewhere":
        return _elsewhere_can_change(hours, float(since), now)
    return True


def settled_between(symbol: str, since: float, now: Optional[float] = None) -> bool:
    """Whether a session of this instrument's market finished settling after
    `since` and by now, as far as is known.

    Something worked out from its prices before then read a day that was not
    over yet. False when that is not known, which leaves a caller doing what
    it always did.
    """
    now = time.time() if now is None else float(now)
    hours = _seen(symbol)
    if hours is None or not since or now <= since:
        return False
    if hours.kind == "us" and _calendar_agrees(hours):
        if now - since > _WEEK:
            return True
        for day in _days(since, now):
            session = us_session(day)
            if session and since < session[1] + SETTLE <= now:
                return True
        return False
    if (hours.kind == "elsewhere" and hours.start is not None
            and hours.start <= hours.last_read):
        return since < hours.until + SETTLE <= now
    return False
