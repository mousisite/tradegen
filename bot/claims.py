"""The video's checkable claims, each set beside something to compare it with.

TJR's video says many things about how price behaves: London takes one side
of Asia's range and then reaches the other, the 9:50-10:10 window is the
best time to enter, a close past a swing carries on where a wick does not,
the gap after the 17:00 pause fills, and so on (setups.CLAIMS lists them in
Orenth's words, with the video's timestamps). Each is measured here on the
prices the Setup lab downloaded, against a control: the same test on a band
or window of the same size, or on random bars, because "London reached the
other side on 60% of days" means nothing until it is set beside how often a
band that size is crossed anyway. A control must have the same chance as the
claim on prices with no pattern in them (tests/test_claims.py tries several on
random walks): a band is the real range mirrored around the price (a
band centred on the price has its first side further away), a later window
has ranges that ended just as lately, a gap is set beside the level the same
distance the other way, and a setup's trades are judged against their own
random entries (a close stop makes the same cost more units of risk).

How a claim is judged (labstore):
- a rate against its control: the difference must be more than two standard
  errors either way (labstore.rate_verdict), with 30 trials on each side;
- an average (claims about the lab's trades, or bar sizes by time of day),
  and a gap against the level the other way: the same two-margins rule on the
  difference, with everything on one date, whatever the instrument, counted
  together.

The verdict's sign always means the same thing: +1 the claim was seen, by
more than its margin; -1 the opposite was seen, by more than its margin; 0
the two cannot be told apart; None too few. So for "buying the breakout
loses", +1 means it lost money, and did worse than its random entries, each
by more than its margin. For claims measured by an average, "hits" count the
trades (or days) that kept money (or moved less), so the page can say "on 41
of 90". A change to how a claim is measured raises its number in
labstore.CLAIM_METHODS, so that an old figure is never shown under new words.

Days with too few bars and futures roll days are left out and counted
("skipped"). Instruments that follow the same index (ES and SPY, NQ and
QQQ) are pooled together, which overstates the evidence; each record's notes
say so. Every count reads only the bars of its own day (and the day before
for yesterday's levels); nothing is chosen with hindsight except leaving out
bad data.

The bias study (the bias_works claim) lives here too: it reads the daily lean
(bot/bias.py) as of 09:00 New York (09:30 for stocks) on each of about two
years of days, from hourly bars closed by then, and checks it against the
16:00 close.
"""
from __future__ import annotations

import bisect
import dataclasses
import logging
import math
import time
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import bias as bias_mod
from . import clock, labstore, market, setups, structure
from .setuplab import Trade, clustered_se, shuffled, walk, _seed

log = logging.getLogger(__name__)

DAY = 86400
NAN = float("nan")

# app choice: how far a move must go to count, in a usual bar's range (ATR 14),
# and within how many bars (video 2:39:42-2:43:56 and 4:13:16-4:28:04 give no
# number).
FOLLOW_ATR = 1.0
FOLLOW_BARS = 20
SMT_BARS = 36
SMT_PAIR_GAP = 2
FVG_BARS = 20

_POOL_NOTE = ("The S&P 500 and Nasdaq 100 futures follow the same indexes as their funds, "
              "so counting both overstates how much evidence there is.")
_WEEKEND_NOTE = ("Coins trade at the weekend too, but New York does not open then, so their "
                 "Saturdays and Sundays are left out.")
_NAMES = {sym: name for sym, name, _ in setups.LAB_SYMBOLS}
_COINS = frozenset(sym for sym, _, group in setups.LAB_SYMBOLS if group == "coin")
_ONE_REGIME = ("Five-minute prices go back about 60 days at the data source, so this is "
               "one stretch of the market, not every kind of market.")


# --- reading days -------------------------------------------------------------------

class _View:
    """One series' bars placed in New York time, with its trading days."""

    def __init__(self, bars, series=None):
        self.bars = bars
        self.symbol = bars.symbol
        self.interval = bars.interval
        self.instrument = clock.instrument_of(bars)
        self.round_clock = self.instrument in ("future", "coin")
        self.o = np.asarray(bars.open, dtype=float)
        self.h = np.asarray(bars.high, dtype=float)
        self.l = np.asarray(bars.low, dtype=float)
        self.c = np.asarray(bars.close, dtype=float)
        self.ts = np.asarray(bars.timestamp, dtype=np.int64)
        self.n = len(self.c)
        self.step = clock.bar_seconds(bars)
        self.days = clock.day_table(bars)
        local = self.ts + clock.offsets(self.ts) if self.n else self.ts
        self.minute = (local % DAY) // 60
        self.since = self.at(self.minute)
        self.roll = np.zeros(self.n, dtype=bool)
        self.usable_bar = np.zeros(self.n, dtype=bool)
        for d in self.days:
            if d.roll:
                self.roll[max(d.first, d.roll_at):d.last + 1] = True
            if self.usable(d):
                self.usable_bar[d.first:d.last + 1] = True
        self.roll_cum = np.concatenate([[0], np.cumsum(self.roll.astype(np.int64))])
        # structure.series of these bars, or setuplab.CLAIM_SERIES of it.
        self._s = series

    def at(self, minute):
        """New York minutes as minutes since the trading day began (18:00 for
        futures and coins, midnight for stocks), so times compare in order."""
        if self.round_clock:
            return (np.asarray(minute) - 18 * 60) % (24 * 60)
        return np.asarray(minute)

    @staticmethod
    def usable(d) -> bool:
        return bool(d.complete and not d.roll)

    def weekend(self, d) -> bool:
        """A coin's Saturday or Sunday: a trading day with no New York open."""
        return self.instrument == "coin" and d.weekday >= 5

    def rows(self, d, start: int, end: int) -> np.ndarray:
        """The day's bars starting at or after New York minute `start` and
        before `end`."""
        since = self.since[d.first:d.last + 1]
        a, b = int(self.at(start)), int(self.at(end))
        return d.first + np.flatnonzero((since >= a) & (since < b))

    def rolls_in(self, a: int, b: int) -> bool:
        """A roll (or a break in the data) among bars a..b."""
        a, b = max(0, a), min(self.n - 1, b)
        return b >= a and bool(self.roll_cum[b + 1] - self.roll_cum[a])

    @property
    def s(self) -> Dict[str, np.ndarray]:
        if self._s is None:
            self._s = structure.series(self.bars)
        return self._s


def _skip(counts: Dict[str, int], key: str, by: int = 1) -> None:
    counts[key] = counts.get(key, 0) + by


def _first_breach(v: _View, rows: np.ndarray, hi: float, lo: float):
    """The first of `rows` past a range: (bar, +1 above / -1 below / 0 both)."""
    if not rows.size:
        return None
    above = v.h[rows] > hi
    below = v.l[rows] < lo
    hit = np.flatnonzero(above | below)
    if not hit.size:
        return None
    j = int(rows[hit[0]])
    if above[hit[0]] and below[hit[0]]:
        return j, 0
    return j, (1 if above[hit[0]] else -1)


def _range_test(v: _View, rows: np.ndarray, hi: float, lo: float, until: np.ndarray):
    """A range's test: the first bar of `rows` past one side, then whether a
    later bar among `until` reaches the other. (trial?, hit?, both?)."""
    got = _first_breach(v, rows, hi, lo)
    if got is None:
        return False, False, False
    j, side = got
    if side == 0:
        return False, False, True
    later = until[until > j]
    if not later.size:
        return True, False, False
    reached = bool(np.any(v.l[later] <= lo)) if side == 1 else bool(np.any(v.h[later] >= hi))
    return True, reached, False


def _mirrored(mid: float, hi: float, lo: float) -> Tuple[float, float]:
    """A range's mirror around a price, as (high, low): its high as far above
    the price as the range's low is below it, and the other way round. Prices
    with no pattern treat the two alike. (A band centred on the price would
    not be: a real range usually has the price nearer one end, so its first
    side goes sooner and leaves more of the day to reach the other.)"""
    return mid + (mid - lo), mid - (hi - mid)


def _cell() -> Dict:
    return {"trials": 0, "hits": 0, "base_trials": 0, "base_hits": 0}


def _rates(cell: Dict) -> Dict:
    """Add each rate and the 95% range of the claim's own rate."""
    if cell["trials"]:
        cell["rate"] = round(cell["hits"] / float(cell["trials"]), 6)
        lo, hi = labstore.wilson(cell["hits"], cell["trials"])
        cell["low"], cell["high"] = round(lo, 6), round(hi, 6)
    if cell["base_trials"]:
        cell["base_rate"] = round(cell["base_hits"] / float(cell["base_trials"]), 6)
    return cell


def _pool(by_symbol: Dict[str, Dict]) -> Dict:
    pooled = _cell()
    for cell in by_symbol.values():
        for key in ("trials", "hits", "base_trials", "base_hits"):
            pooled[key] += int(cell[key])
    return pooled


def _record(cid: str, interval: str, by_symbol: Dict[str, Dict], notes: List[str],
            skipped: Dict[str, int], measure: str = "rate", pooled: Optional[Dict] = None,
            verdict="rate") -> Dict:
    """One claim's record, in labstore's shape, with its verdict."""
    for cell in by_symbol.values():
        _rates(cell)
    pooled = pooled if pooled is not None else _pool(by_symbol)
    _rates(pooled)
    if verdict == "rate":
        verdict = labstore.rate_verdict(pooled["hits"], pooled["trials"],
                                        pooled["base_hits"], pooled["base_trials"])
    rec = {"claim": cid, "unit": setups.claim(cid).unit, "interval": interval,
           "pooled": pooled, "by_symbol": by_symbol, "notes": notes, "verdict": verdict,
           "measure": measure, "skipped": {k: int(v) for k, v in skipped.items()}}
    labstore.validate_claim(rec)
    return rec


def _index_note(symbols: Sequence[str]) -> List[str]:
    have = set(symbols)
    if ({"ES=F", "SPY"} <= have) or ({"NQ=F", "QQQ"} <= have):
        return [_POOL_NOTE]
    return []


def _intraday(bars_list, interval: str = "5m", kinds=("future", "coin", "stock")):
    """Views of the series of this bar size and these kinds, from bars or views."""
    out = []
    for item in bars_list:
        b = item.bars if isinstance(item, _View) else item
        if b is None or b.interval != interval or len(b.close) < 50:
            continue
        if clock.kind_of(b) == "daily" or clock.instrument_of(b) not in kinds:
            continue
        out.append(item if isinstance(item, _View) else _View(b))
    return out


# --- sessions --------------------------------------------------------------------------

def london_takes_asia(bars_list) -> Optional[Dict]:
    """video 1:52:08, 1:57:26, 2:02:39: London takes one side of the Asia range,
    then reaches the other by noon New York time."""
    views = _intraday(bars_list, "5m", ("future", "coin"))
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        for d in v.days:
            if v.weekend(d):
                _skip(skipped, "weekend")
                continue
            if d.roll:
                _skip(skipped, "roll")
                continue
            asia, lon = d.sessions.get("asia"), d.sessions.get("london")
            if not d.complete or asia is None or lon is None or not (asia.complete and lon.complete):
                _skip(skipped, "incomplete")
                continue
            rows = np.arange(lon.first, lon.last + 1)
            until = v.rows(d, 3 * 60, 12 * 60)
            trial, hit, both = _range_test(v, rows, asia.high, asia.low, until)
            if both:
                _skip(skipped, "both_sides")
            cell["trials"] += trial
            cell["hits"] += hit
            hi, lo = _mirrored(float(v.o[lon.first]), asia.high, asia.low)
            trial, hit, _ = _range_test(v, rows, hi, lo, until)
            cell["base_trials"] += trial
            cell["base_hits"] += hit
    notes = [_ONE_REGIME, "Days where one London bar went past both sides of the Asia range "
             "are left out.", _WEEKEND_NOTE]
    return _record("london_takes_asia", "5m", by, notes, skipped)


def ny_takes_london(bars_list) -> Optional[Dict]:
    """video 2:03:41, 2:18:35, 4:31:11: New York takes one side of London's range
    from 08:30, then reaches the other by 16:00."""
    views = _intraday(bars_list, "5m", ("future", "coin"))
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        for d in v.days:
            if v.weekend(d):
                _skip(skipped, "weekend")
                continue
            if d.roll:
                _skip(skipped, "roll")
                continue
            lon = d.sessions.get("london")
            if not d.complete or lon is None or not lon.complete:
                _skip(skipped, "incomplete")
                continue
            rows = v.rows(d, 8 * 60 + 30, 16 * 60)
            if not rows.size:
                _skip(skipped, "incomplete")
                continue
            trial, hit, both = _range_test(v, rows, lon.high, lon.low, rows)
            if both:
                _skip(skipped, "both_sides")
            cell["trials"] += trial
            cell["hits"] += hit
            hi, lo = _mirrored(float(v.o[rows[0]]), lon.high, lon.low)
            trial, hit, _ = _range_test(v, rows, hi, lo, rows)
            cell["base_trials"] += trial
            cell["base_hits"] += hit
    notes = [_ONE_REGIME, _WEEKEND_NOTE]
    return _record("ny_takes_london", "5m", by, notes, skipped)


def pd_sweep_reverse(bars_list) -> Optional[Dict]:
    """video 2:06:52-2:07:57: once yesterday's high is taken, price reaches
    yesterday's low the same day (and the other way round)."""
    views = _intraday(bars_list, "5m")
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        for b, d in enumerate(v.days):
            if b == 0:
                continue
            prev = v.days[b - 1]
            if d.roll or prev.roll:
                _skip(skipped, "roll")
                continue
            if not (d.complete and prev.complete):
                _skip(skipped, "incomplete")
                continue
            rows = v.rows(d, 18 * 60 if v.round_clock else 0, 16 * 60)
            trial, hit, both = _range_test(v, rows, prev.high, prev.low, rows)
            if both:
                _skip(skipped, "both_sides")
            cell["trials"] += trial
            cell["hits"] += hit
            hi, lo = _mirrored(d.open, prev.high, prev.low)
            trial, hit, _ = _range_test(v, rows, hi, lo, rows)
            cell["base_trials"] += trial
            cell["base_hits"] += hit
    notes = [_ONE_REGIME] + _index_note(by)
    return _record("pd_sweep_reverse", "5m", by, notes, skipped)


def _window_hit(v: _View, d, levels, w0: int, w1: int):
    """The open-manipulation test in one window: (trial?, hit?).

    levels: (price, +1 a high / -1 a low, New York minute it is known from).
    Only levels no bar had traded past between being known and the window's
    start count. A bar starting from w0 to w1 (inclusive) past one, then a
    close back inside it within 30 minutes (that bar's own close included)."""
    start = int(v.at(w0))
    since = v.since[d.first:d.last + 1]
    live = []
    for price, side, known in levels:
        if price != price:
            continue
        before = d.first + np.flatnonzero((since >= int(v.at(known))) & (since < start))
        if side > 0 and before.size and float(np.max(v.h[before])) > price:
            continue
        if side < 0 and before.size and float(np.min(v.l[before])) < price:
            continue
        live.append((price, side))
    if not live:
        return False, False
    rows = d.first + np.flatnonzero((since >= start) & (since <= int(v.at(w1))))
    for j in rows.tolist():
        highs = [p for p, s_ in live if s_ > 0 and v.h[j] > p]
        lows = [p for p, s_ in live if s_ < 0 and v.l[j] < p]
        if not highs and not lows:
            continue
        if highs and lows:
            return True, False
        stop = int(np.searchsorted(v.ts, v.ts[j] + 1800, side="right"))
        back = np.arange(j, min(stop, d.last + 1))
        if highs:
            return True, bool(np.any(v.c[back] < min(highs)))
        return True, bool(np.any(v.c[back] > max(lows)))
    return True, False


# The ranges the open's test reads (start and end, New York minutes), and the
# last bar it counts: one that ends by 9:50.
OPEN_RANGES = ((18 * 60, 3 * 60), (3 * 60, 8 * 60 + 30), (8 * 60 + 30, 9 * 60 + 30))
OPEN_FROM, OPEN_TO = 9 * 60 + 30, 9 * 60 + 45
OPEN_LATER = 60                    # app choice: the control is the same, an hour on


def _open_levels(v: _View, d, prev, later: int) -> List[Tuple[float, int, int]]:
    """The levels the open's test reads, with every clock time `later` minutes
    on: yesterday's high and low, and each of OPEN_RANGES with enough bars.
    (The price, +1 a high or -1 a low, the minute it is known from.)"""
    out: List[Tuple[float, int, int]] = []
    if prev is not None:
        known = 18 * 60 + later
        out += [(prev.high, 1, known), (prev.low, -1, known)]
    step_min = max(1, v.step // 60)
    for start, end in OPEN_RANGES:
        idx = v.rows(d, start + later, end + later)
        want = ((end - start) % (24 * 60)) // step_min
        if idx.size and idx.size >= clock.COMPLETE_SHARE * want:
            out += [(float(np.max(v.h[idx])), 1, end + later),
                    (float(np.min(v.l[idx])), -1, end + later)]
    return out


def open_manipulation(bars_list) -> Optional[Dict]:
    """video 4:33:23, 4:35:34: between 9:30 and 9:50 price trades past a level
    set before the open and closes back inside within half an hour. Control:
    the same test with every time an hour later, so that it too has a range
    that ended the moment it starts; the 9:30 levels at 10:30 had no such
    range, and read as a pattern on prices that had none."""
    views = _intraday(bars_list, "5m", ("future", "coin"))
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        for b, d in enumerate(v.days):
            if v.weekend(d):
                _skip(skipped, "weekend")
                continue
            if d.roll:
                _skip(skipped, "roll")
                continue
            if not d.complete:
                _skip(skipped, "incomplete")
                continue
            prev = v.days[b - 1] if b > 0 and v.usable(v.days[b - 1]) else None
            trial, hit = _window_hit(v, d, _open_levels(v, d, prev, 0), OPEN_FROM, OPEN_TO)
            cell["trials"] += trial
            cell["hits"] += hit
            trial, hit = _window_hit(v, d, _open_levels(v, d, prev, OPEN_LATER),
                                     OPEN_FROM + OPEN_LATER, OPEN_TO + OPEN_LATER)
            cell["base_trials"] += trial
            cell["base_hits"] += hit
    notes = [_ONE_REGIME, "Levels: yesterday's high and low, and today's Asia, London and "
             "before-the-open ranges, if price had not already gone past them that day.",
             "The hour from 9:30 is usually the day's busiest, so the range an hour later is "
             "wider and harder to go past than the one before the open.", _WEEKEND_NOTE]
    return _record("open_manipulation", "5m", by, notes, skipped)


def after_1030_slower(bars_list) -> Optional[Dict]:
    """video 4:33:23: the market slows after 10:30. Each day: the average bar's
    size from 10:30 to 12:00, and from 9:30 to 10:30, each over that day's own
    average bar; a day counts once."""
    views = _intraday(bars_list, "5m")
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    xs: List[float] = []
    early_all: List[float] = []
    late_all: List[float] = []
    dates: List[int] = []
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        step_min = max(1, v.step // 60)
        for d in v.days:
            if v.weekend(d):
                _skip(skipped, "weekend")
                continue
            if d.roll:
                _skip(skipped, "roll")
                continue
            if not d.complete:
                _skip(skipped, "incomplete")
                continue
            early = v.rows(d, 9 * 60 + 30, 10 * 60 + 30)
            late = v.rows(d, 10 * 60 + 30, 12 * 60)
            if early.size < 0.8 * 60 / step_min or late.size < 0.8 * 90 / step_min:
                _skip(skipped, "incomplete")
                continue
            span = v.h[d.first:d.last + 1] - v.l[d.first:d.last + 1]
            avg = float(np.mean(span))
            if not avg > 0:
                _skip(skipped, "incomplete")
                continue
            e = float(np.mean(v.h[early] - v.l[early])) / avg
            lt = float(np.mean(v.h[late] - v.l[late])) / avg
            cell["trials"] += 1
            cell["hits"] += 1 if lt < e else 0
            xs.append(e - lt)
            early_all.append(e)
            late_all.append(lt)
            dates.append(d.day.toordinal())
    pooled = _pool(by)
    if xs:
        pooled["mean"] = round(float(np.mean(late_all)), 6)
        pooled["base_mean"] = round(float(np.mean(early_all)), 6)
        se_late = clustered_se(late_all, dates)
        se_early = clustered_se(early_all, dates)
        pooled["se"] = None if se_late is None else round(se_late, 6)
        pooled["base_se"] = None if se_early is None else round(se_early, 6)
        pooled["diff"] = round(float(np.mean(xs)), 6)
        dse = clustered_se(xs, dates)
        pooled["diff_se"] = None if dse is None else round(dse, 6)
        pooled["days"] = pooled["base_days"] = len(set(dates))
    v_ = labstore.verdict(pooled.get("diff"), pooled.get("diff_se"), len(xs),
                          days=len(set(dates)))
    notes = [_ONE_REGIME, "Bar sizes are compared within each day, so a busy day and a quiet "
             "day count the same. Days on the same date are counted together.", _WEEKEND_NOTE]
    notes += _index_note(by)
    return _record("after_1030_slower", "5m", by, notes, skipped, measure="mean",
                   pooled=pooled, verdict=v_)


# --- gaps and breaks ---------------------------------------------------------------

def _touch(v: _View, a: int, b: int, top: float, bottom: float, up: bool) -> bool:
    """Price came back into the zone between bars a..b, on a bar that did not
    close through its far side."""
    if up:
        idx = np.flatnonzero(v.l[a:b + 1] <= top)
        return bool(idx.size) and bool(v.c[a + idx[0]] >= bottom)
    idx = np.flatnonzero(v.h[a:b + 1] >= bottom)
    return bool(idx.size) and bool(v.c[a + idx[0]] <= top)


def fvg_gets_retested(bars_list) -> Optional[Dict]:
    """video 2:51:15: price comes back into a fresh gap. A gap counts when a bar
    within 20 bars trades back into it without closing through it. Control: a
    zone of the same size and distance below (above) the price, at a random
    bar, which stays the same from night to night while the prices hold it."""
    views = _intraday(bars_list, "5m") + _intraday(bars_list, "15m")
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        s = v.s
        atr = s["st_atr"]
        # Each bar's random number, in order: a gap takes the first bar past its
        # own, so its random bar does not move when a night slides the window.
        spots = np.arange(20, max(20, v.n - FVG_BARS - 1))
        marks = shuffled(v.ts[spots], _seed("fvg", v.symbol, v.interval))
        ring = np.argsort(marks, kind="stable")
        marks, spots = marks[ring], spots[ring]
        news = np.flatnonzero(s["st_fvg_new"] != 0)
        for c_ in news.tolist():
            if c_ + FVG_BARS >= v.n:
                continue
            if not v.usable_bar[c_] or v.rolls_in(c_ - 2, c_ + FVG_BARS):
                _skip(skipped, "left_out_day")
                continue
            up = s["st_fvg_new"][c_] > 0
            top, bottom = (v.l[c_], v.h[c_ - 2]) if up else (v.l[c_ - 2], v.h[c_])
            cell["trials"] += 1
            cell["hits"] += _touch(v, c_ + 1, c_ + FVG_BARS, top, bottom, up)
            a_c = atr[c_]
            if not (a_c == a_c and a_c > 0):
                continue
            dist = (v.c[c_] - top) if up else (bottom - v.c[c_])
            size = top - bottom
            for nth in range(3 if spots.size else 0):
                at = shuffled(v.ts[c_:c_ + 1], _seed("fvg gap", v.symbol, v.interval, nth))
                r = int(spots[int(np.searchsorted(marks, at[0])) % spots.size])
                a_r = atr[r]
                if not (a_r == a_r and a_r > 0) or v.rolls_in(r, r + FVG_BARS):
                    continue
                k = a_r / a_c
                if up:
                    t_ = v.c[r] - dist * k
                    b_ = t_ - size * k
                else:
                    b_ = v.c[r] + dist * k
                    t_ = b_ + size * k
                cell["base_trials"] += 1
                cell["base_hits"] += _touch(v, r + 1, r + FVG_BARS, t_, b_, up)
                break
    notes = [_ONE_REGIME, "Five- and fifteen-minute prices are counted together."]
    notes += _index_note(by)
    return _record("fvg_gets_retested", "5m and 15m", by, notes, skipped)


def _follows(v: _View, i: int, direction: int, size: float, bars: int) -> bool:
    """Price went `size` further in `direction` from bar i's close before it came
    `size` back, within `bars` bars (a bar reaching both counts as back)."""
    e = float(v.c[i])
    got = walk(v.o, v.h, v.l, v.c, i, min(v.n - 1, i + bars), direction, e,
               e - direction * size, [e + direction * size])
    return bool(got[0][1])


def close_beats_wick(bars_list) -> Optional[Dict]:
    """video 2:39:42, 2:41:50, 2:43:56: a close past a swing carries on; a wick
    past it that closes back does not. The first time price goes past an old
    swing point: does it go a usual bar's range further before it comes a
    usual bar's range back, within 20 bars? Closes past, against wicks."""
    views = _intraday(bars_list, "5m")
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        s = v.s
        atr = s["st_atr"]
        for i in np.flatnonzero(s["st_sweep"] != 0).tolist():
            if i + FOLLOW_BARS >= v.n:
                continue
            if not v.usable_bar[i] or v.rolls_in(i, i + FOLLOW_BARS):
                _skip(skipped, "left_out_day")
                continue
            a = atr[i]
            if not (a == a and a > 0):
                continue
            way = -int(s["st_sweep"][i])           # lows taken: price went down
            hit = _follows(v, i, way, FOLLOW_ATR * a, FOLLOW_BARS)
            if s["st_sweep_back"][i]:
                cell["base_trials"] += 1
                cell["base_hits"] += hit
            else:
                cell["trials"] += 1
                cell["hits"] += hit
    notes = [_ONE_REGIME, "A usual bar's range is the average size of the last 14 bars."]
    notes += _index_note(by)
    return _record("close_beats_wick", "5m", by, notes, skipped)


def _subset(bars, idx: np.ndarray):
    return dataclasses.replace(
        bars, timestamp=bars.timestamp[idx], open=bars.open[idx], high=bars.high[idx],
        low=bars.low[idx], close=bars.close[idx], volume=bars.volume[idx],
        session_id=bars.session_id[idx], live_price=None, dividend_factor=None)


def _part(item, idx: np.ndarray) -> _View:
    """These bars' view, kept to the bars at idx; the same view when that is all."""
    bars = item.bars if isinstance(item, _View) else item
    if isinstance(item, _View) and idx.size == item.n:
        return item
    return _View(_subset(bars, idx))


def smt_at_sweep(bars_by_symbol: Dict[str, object]) -> Optional[Dict]:
    """video 4:13:16-4:28:04: when two related markets disagree at a high (one
    makes a higher high past an old high, the other a lower high), the leader
    falls next; mirror at lows. Control: old highs taken where both made
    higher highs."""
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    k = structure.K
    for a_sym, b_sym in setups.PAIRS:
        A, B = bars_by_symbol.get(a_sym), bars_by_symbol.get(b_sym)
        if A is None or B is None:
            continue
        times = [np.asarray((x.bars if isinstance(x, _View) else x).timestamp) for x in (A, B)]
        _, ia, ib = np.intersect1d(times[0], times[1], return_indices=True)
        if ia.size < 200:
            continue
        va, vb = _part(A, ia), _part(B, ib)
        sa, sb = va.s, vb.s
        cell = by.setdefault("%s/%s" % (a_sym, b_sym), _cell())
        n = va.n
        for side in (1, -1):                       # 1: highs, -1: lows
            pa = structure.pivots(va.h, va.l, k)[0 if side == 1 else 1]
            pb = structure.pivots(vb.h, vb.l, k)[0 if side == 1 else 1]
            if len(pa) < 2 or len(pb) < 2:
                continue
            for x in range(1, len(pa)):
                p = pa[x]
                y = bisect.bisect_left(pb, p - SMT_PAIR_GAP)
                if y >= len(pb) or abs(pb[y] - p) > SMT_PAIR_GAP or y == 0:
                    continue
                q = pb[y]
                p0, q0 = pa[x - 1], pb[y - 1]
                if side == 1:
                    a_new, b_new = va.h[p] > va.h[p0], vb.h[q] > vb.h[q0]
                else:
                    a_new, b_new = va.l[p] < va.l[p0], vb.l[q] < vb.l[q0]
                # Taken: an old high (low) was swept on the way.
                want = -1 if side == 1 else 1
                a_took = bool(np.any(sa["st_sweep"][p0 + 1:p + 1] == want))
                b_took = bool(np.any(sb["st_sweep"][q0 + 1:q + 1] == want))
                t0 = max(p, q) + k                   # both swings are known here
                if t0 + SMT_BARS >= n:
                    continue
                if va.rolls_in(min(p0, q0), t0 + SMT_BARS) or vb.rolls_in(min(p0, q0), t0 + SMT_BARS) \
                        or not (va.usable_bar[t0] and vb.usable_bar[t0]):
                    _skip(skipped, "left_out_day")
                    continue
                if a_new != b_new:
                    lead, took = (va, a_took) if a_new else (vb, b_took)
                    base = False
                elif a_new and b_new:
                    if a_took:
                        lead, took = va, True
                    elif b_took:
                        lead, took = vb, True
                    else:
                        continue
                    base = True
                else:
                    continue
                if not took:
                    continue
                a = lead.s["st_atr"][t0]
                if not (a == a and a > 0):
                    continue
                hit = _follows(lead, t0, -side, FOLLOW_ATR * a, SMT_BARS)
                if base:
                    cell["base_trials"] += 1
                    cell["base_hits"] += hit
                else:
                    cell["trials"] += 1
                    cell["hits"] += hit
    if not by:
        return None
    notes = [_ONE_REGIME, "The result is measured on the market that made the new high (or "
             "low), from the bar both swing points were known.",
             "Pairs are matched bar by bar, on the times both markets have prices."]
    return _record("smt_at_sweep", "5m", by, notes, skipped)


# --- the lab's own trades -------------------------------------------------------------

def _lab(trades: Dict[str, Dict], setup: str, rule: str = "2r") -> List[Tuple[str, Trade]]:
    out = []
    for sym, by_setup in (trades or {}).items():
        for t in ((by_setup or {}).get(setup) or {}).get(rule, []):
            out.append((sym, t))
    return out


def _parts(rows: List[Tuple[str, Trade]], value) -> Dict[str, Tuple]:
    """Each instrument's (trades, average, {date: (trades, sum)}), as
    labstore.day_error reads them."""
    per: Dict[str, Dict[int, List[float]]] = {}
    for sym, t in rows:
        per.setdefault(sym, {}).setdefault(t.day, []).append(float(value(t)))
    out = {}
    for sym, days in per.items():
        vals = [x for got in days.values() for x in got]
        out[sym] = (len(vals), sum(vals) / len(vals),
                    {day: (len(got), sum(got)) for day, got in days.items()})
    return out


def _mean_se(rows: List[Tuple[str, Trade]], value=lambda t: t.r):
    """(mean, its standard error, n) of trades. Trades on one date count
    together whatever the instrument, as in the lab's own tables
    (labstore.pooled): one day's news moves them all."""
    if not rows:
        return None, None, 0
    vals = [value(t) for _, t in rows]
    return (float(np.mean(vals)), labstore.day_error(list(_parts(rows, value).values())),
            len(vals))


def _against_random(t: Trade) -> float:
    """A trade's result minus its own random entries' (see setuplab)."""
    return t.r - t.control_r


def _paired(rows: List[Tuple[str, Trade]]) -> List[Tuple[str, Trade]]:
    return [x for x in rows if x[1].control_r is not None]


def _dates(rows: List[Tuple[str, Trade]]) -> int:
    return len(set(t.day for _, t in rows))


def _said(mean: Optional[float], se: Optional[float], days: int) -> str:
    """A figure in R with its margin of error, for a note."""
    if mean is None:
        return "not measured"
    text = "%+.2fR" % (round(mean, 2) + 0.0)
    return text if se is None else "%s (±%.2f)" % (text, labstore.reach(days) * se)


def _round(x):
    return None if x is None else round(float(x), 6)


def _means(by: Dict[str, Dict], rows: List[Tuple[str, Trade]], base: bool = False,
           value=lambda t: t.r) -> None:
    """Each instrument's own average and margin, beside its counts."""
    per: Dict[str, List] = {}
    for sym, t in rows:
        per.setdefault(sym, []).append((sym, t))
    for sym, mine in per.items():
        m, se, _ = _mean_se(mine, value)
        cell = by.setdefault(sym, _cell())
        cell["base_mean" if base else "mean"] = _round(m)
        cell["base_se" if base else "se"] = _round(se)


def _within(rows_a: List[Tuple[str, Trade]], rows_b: List[Tuple[str, Trade]],
            value=lambda t: t.r):
    """The difference a minus b taken inside each instrument, then averaged
    with each instrument weighted by its count in a: (difference, its standard
    error, trades in a). Pooling the trades first would let the mix of
    instruments decide it: coins cost ten times what futures do and trade all
    night, so any window or setup with more coin trades would look worse. The
    error counts one date's trades together, both sides and every instrument."""
    by_a, by_b = _parts(rows_a, value), _parts(rows_b, value)
    total = 0.0
    weight = 0
    left: Dict[int, float] = {}
    for sym, (na, ma, days_a) in by_a.items():
        if sym not in by_b:
            continue
        nb, mb, days_b = by_b[sym]
        if len(days_a) < 2 or len(days_b) < 2:
            continue
        total += na * (ma - mb)
        weight += na
        for day, (k, s) in days_a.items():
            left[day] = left.get(day, 0.0) + (s - k * ma)
        for day, (k, s) in days_b.items():
            left[day] = left.get(day, 0.0) - (s - k * mb) * na / nb
    if not weight:
        return None, None, 0
    return total / weight, math.sqrt(sum(x * x for x in left.values())) / weight, weight


_WITHIN_NOTE = ("The difference is taken inside each instrument and then averaged, so an "
                "instrument that costs more to trade does not tilt it.")


def macro_window(trades: Dict[str, Dict]) -> Optional[Dict]:
    """video 4:33:23, 4:37:38: entries from 9:50 to 10:10 do best. The lab's
    sweep, break and retest trades (twice-the-risk target, 5-minute prices) by
    the time they were entered, against the same instrument's trades at other
    times. Coins' Saturdays and Sundays, with no New York open, are left out of
    both."""
    found = _lab(trades, "liquidity_model")
    rows = [(sym, t) for sym, t in found
            if not (sym in _COINS and date.fromordinal(t.day).weekday() >= 5)]
    if not rows:
        return None
    skipped = {"weekend": len(found) - len(rows)} if len(rows) < len(found) else {}
    by: Dict[str, Dict] = {}
    for sym, t in rows:
        cell = by.setdefault(sym, _cell())
        mine = t.window == "macro"
        cell["trials" if mine else "base_trials"] += 1
        cell["hits" if mine else "base_hits"] += 1 if t.r > 0 else 0
    macro = [x for x in rows if x[1].window == "macro"]
    other = [x for x in rows if x[1].window != "macro"]
    _means(by, macro)
    _means(by, other, base=True)
    m, se, n = _mean_se(macro)
    bm, bse, bn = _mean_se(other)
    diff, dse, dn = _within(macro, other)
    pooled = _pool(by)
    pooled.update({"mean": _round(m), "se": _round(se), "base_mean": _round(bm),
                   "base_se": _round(bse), "diff": _round(diff), "diff_se": _round(dse),
                   "days": _dates(macro), "base_days": _dates(other)})
    v = labstore.verdict(diff, dse, dn, days=_dates(macro)) if bn >= labstore.MIN_TRADES else None
    notes = [_ONE_REGIME, "Kept money per trade after costs, in units of risk, for trades "
             "entered from 9:50 to 10:10 New York time against the same instrument's "
             "trades at all other times.", _WITHIN_NOTE, _WEEKEND_NOTE]
    notes += _index_note(by)
    return _record("macro_window", "5m", by, notes, skipped, measure="mean", pooled=pooled,
                   verdict=v)


def crowd_breakout_loses(trades: Dict[str, Dict]) -> Optional[Dict]:
    """video 1:19:23-1:29:56: buying the break of a high with the stop under the
    last low loses. The lab's breakout buys (the claim is about buying; the
    lab measures the sells too) with a twice-the-risk target. Holds when they
    lost money and did worse than their own random entries, each by more than
    its margin; the opposite when both went the other way. Against zero alone
    the trading costs would decide it, whatever the setup did."""
    rows = [x for x in _lab(trades, "crowd_breakout") if x[1].direction > 0]
    if not rows:
        return None
    by: Dict[str, Dict] = {}
    for sym, t in rows:
        cell = by.setdefault(sym, _cell())
        cell["trials"] += 1
        cell["hits"] += 1 if t.r > 0 else 0
        if t.control_r is not None:
            cell["base_trials"] += 1
            cell["base_hits"] += 1 if t.control_r > 0 else 0
    m, se, n = _mean_se(rows)
    paired = _paired(rows)
    _means(by, rows)
    _means(by, paired, base=True, value=lambda t: t.control_r)
    bm, bse, _ = _mean_se(paired, lambda t: t.control_r)
    dm, dse, dn = _mean_se(paired, _against_random)
    pooled = _pool(by)
    pooled.update({"mean": _round(m), "se": _round(se), "base_mean": _round(bm),
                   "base_se": _round(bse), "diff": _round(dm), "diff_se": _round(dse),
                   "days": _dates(rows), "base_days": _dates(paired)})
    kept = labstore.verdict(m, se, n, days=_dates(rows))
    beat = labstore.verdict(dm, dse, dn, days=_dates(paired))
    if kept is None or beat is None:
        v = None
    else:
        v = -kept if kept == beat else 0
    notes = [_ONE_REGIME, "Only the buys are counted. A win here means a trade that kept "
             "money after costs. The claim holds when the trades lost money and did worse "
             "than their own random entries, each by more than its margin of error: costs "
             "alone make random trades lose too.",
             "Against their random entries: %s a trade." % _said(dm, dse, _dates(paired)),
             "Coins cost far more to trade on five-minute bars than futures or stocks, which "
             "weighs on the pooled figure; each instrument's own figure is kept beside it."]
    notes += _index_note(by)
    return _record("crowd_breakout_loses", "5m", by, notes, {}, measure="mean",
                   pooled=pooled, verdict=v)


def sweep_alone_loses(trades: Dict[str, Dict]) -> Optional[Dict]:
    """video 1:40:27-1:43:34: trading against every sweep loses, and the full
    four-step model does better. Each is judged against its own random entries
    as well: a sweep's stop is close, so the same cost is many more units of
    risk, and in units of risk the model would look better on prices with no
    pattern in them at all. Holds when the sweeps lost money and did worse
    than their random entries, and the model did better against its own than
    the sweeps did against theirs, each by more than its margin; fails when
    either half is contradicted by more than its margin."""
    sweeps = _lab(trades, "sweep_reclaim")
    chain = _lab(trades, "liquidity_model")
    if not sweeps:
        return None
    own, chain_own = _paired(sweeps), _paired(chain)
    by: Dict[str, Dict] = {}
    for sym, t in sweeps:
        cell = by.setdefault(sym, _cell())
        cell["trials"] += 1
        cell["hits"] += 1 if t.r > 0 else 0
        if t.control_r is not None:
            cell["base_trials"] += 1
            cell["base_hits"] += 1 if t.control_r > 0 else 0
    _means(by, sweeps)
    _means(by, own, base=True, value=lambda t: t.control_r)
    m, se, n = _mean_se(sweeps)
    bm, bse, _ = _mean_se(own, lambda t: t.control_r)
    om, ose, on = _mean_se(own, _against_random)
    cm, cse, _ = _mean_se(chain)
    co, cose, _ = _mean_se(chain_own, _against_random)
    diff, dse, dn = _within(chain_own, own, _against_random)
    kept = labstore.verdict(m, se, n, days=_dates(sweeps))
    beat = labstore.verdict(om, ose, on, days=_dates(own))
    better = labstore.verdict(diff, dse, dn, days=_dates(chain_own))
    if kept is None or beat is None or better is None:
        v = None
    elif kept == -1 and beat == -1 and better == 1:
        v = 1
    elif (kept == 1 and beat == 1) or better == -1:
        v = -1
    else:
        v = 0
    pooled = _pool(by)
    pooled.update({"mean": _round(m), "se": _round(se), "base_mean": _round(bm),
                   "base_se": _round(bse), "diff": _round(diff), "diff_se": _round(dse),
                   "days": _dates(sweeps), "base_days": _dates(own)})
    notes = [_ONE_REGIME, "The sweep-and-reclaim trades are the result shown. Against their "
             "own random entries: %s a trade." % _said(om, ose, _dates(own))]
    if chain:
        notes.append("The four-step model: %s a trade after costs, and %s against its own "
                     "random entries. Set against their random entries, the model minus the "
                     "sweeps: %s." % (_said(cm, cse, _dates(chain)), _said(co, cose, _dates(chain_own)),
                        _said(diff, dse, _dates(chain_own))))
    notes += ["The claim holds only when every part is clear: the sweeps lost money, did "
              "worse than random entries, and the model did better than they did. Random "
              "entries carry the same costs, which weigh more on a closer stop.", _WITHIN_NOTE]
    notes += _index_note(by)
    return _record("sweep_alone_loses", "5m", by, notes, {}, measure="mean", pooled=pooled,
                   verdict=v)


def not_every_day_green(trades: Dict[str, Dict]) -> Optional[Dict]:
    """video 0:32:45: red days come. The four-step model's days with trades
    that ended up (green), and the longest run of losing days in a row.
    It describes; it is not a test (no verdict)."""
    rows = _lab(trades, "liquidity_model")
    if not rows:
        return None
    by: Dict[str, Dict] = {}
    longest, where = 0, ""
    per: Dict[str, Dict[int, float]] = {}
    for sym, t in rows:
        per.setdefault(sym, {})
        per[sym][t.day] = per[sym].get(t.day, 0.0) + t.r
    for sym, days in per.items():
        cell = by.setdefault(sym, _cell())
        run = 0
        for day in sorted(days):
            total = days[day]
            cell["trials"] += 1
            if total > 0:
                cell["hits"] += 1
                run = 0
            else:
                run += 1
                if run > longest:
                    longest, where = run, sym
    pooled = _pool(by)
    pooled["longest_red"] = longest
    notes = ["Days with at least one trade; a green day is one whose trades kept money "
             "together, after costs."]
    if longest:
        notes.append("The longest run of losing days in a row was %d, on %s (counting only "
                     "days with trades)." % (longest, _NAMES.get(where, where)))
    return _record("not_every_day_green", "5m", by, notes, {}, measure="descriptive",
                   pooled=pooled, verdict=None)


# --- opening gaps -------------------------------------------------------------------

_GAP_NOTE = ("A small gap is crossed by ordinary movement, so beside it is how often price "
             "went as far the other way, past the open, by the same time. A pull back to the "
             "old close would show as more gaps filled than that.")


def _reached(v: _View, idx: np.ndarray, price: float, above: bool) -> bool:
    """Whether any of these bars traded at a price above (or below) them."""
    if not idx.size:
        return False
    return bool(np.any(v.h[idx] >= price)) if above else bool(np.any(v.l[idx] <= price))


def _gap_test(v: _View, d, idx: np.ndarray) -> Tuple[int, int]:
    """An opening gap against its mirror, over these bars: whether price traded
    back to the close before it, and whether it went as far the other way past
    the open. A gap that small price crosses anyway is crossed both ways."""
    up = d.gap > 0
    return (int(_reached(v, idx, d.prev_close, not up)),
            int(_reached(v, idx, d.open + d.gap, up)))


def _gap_verdict(pooled: Dict, diffs: Sequence[int], dates: Sequence[int]) -> Optional[int]:
    """Filled minus the other way, with one date's gaps (two futures that follow
    each other, usually) counted together; kept in the pooled figures."""
    if not diffs:
        return None
    mean, se = float(np.mean(diffs)), clustered_se(diffs, dates)
    pooled["diff"], pooled["diff_se"] = _round(mean), _round(se)
    return labstore.verdict(mean, se, len(diffs), days=len(set(dates)))


def ndog_fills(bars_list) -> Optional[Dict]:
    """video 3:16:24-3:23:47: the jump between the 17:00 close and the 18:00
    reopen gets filled. Each weekday's opening gap (Monday's is the weekend's,
    measured by nwog_fills): filled within an hour, four hours, and by 9:30,
    beside how often price went as far the other way in the same time."""
    views = _intraday(bars_list, "5m", ("future",))
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    diffs: List[int] = []
    dates: List[int] = []
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        for key in ("hit_1h", "base_hit_1h", "hit_4h", "base_hit_4h"):
            cell[key] = 0
        for b, d in enumerate(v.days):
            if b == 0 or d.weekday == 0:
                continue
            if d.roll:
                _skip(skipped, "roll")
                continue
            if not d.complete:
                _skip(skipped, "incomplete")
                continue
            if not (d.gap == d.gap) or d.gap == 0:
                continue
            rows = v.rows(d, 18 * 60, 9 * 60 + 30)
            waited = v.ts[rows] - int(v.ts[d.first])
            cell["trials"] += 1
            cell["base_trials"] += 1
            filled, other = _gap_test(v, d, rows)
            cell["hits"] += filled
            cell["base_hits"] += other
            diffs.append(filled - other)
            dates.append(d.day.toordinal())
            for key, limit in (("hit_1h", 3600), ("hit_4h", 4 * 3600)):
                filled, other = _gap_test(v, d, rows[waited < limit])
                cell[key] += filled
                cell["base_" + key] += other
    if not any(c["trials"] for c in by.values()):
        return None
    pooled = _pool(by)
    for key in ("hit_1h", "base_hit_1h", "hit_4h", "base_hit_4h"):
        pooled[key] = sum(c[key] for c in by.values())
    v_ = _gap_verdict(pooled, diffs, dates)
    notes = [_ONE_REGIME, "Filled means price traded back to the 17:00 close before the "
             "pause. The verdict is on the gaps filled by 9:30.", _GAP_NOTE,
             "Monday's opening gap is the weekend's, counted with the weekly gaps."]
    return _record("ndog_fills", "5m", by, notes, skipped, pooled=pooled, verdict=v_)


def nwog_fills(bars_list) -> Optional[Dict]:
    """video 3:19:32, 3:25:57: the gap between Friday's close and Sunday's 18:00
    reopen gets filled, often by Monday. Hourly prices: filled by Monday 16:00,
    and by Friday 16:00, beside how often price went as far the other way."""
    views = _intraday(bars_list, "1h", ("future",))
    if not views:
        return None
    by: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    diffs: List[int] = []
    dates: List[int] = []
    for v in views:
        cell = by.setdefault(v.symbol, _cell())
        cell["hit_monday"] = cell["base_hit_monday"] = 0
        for b, d in enumerate(v.days):
            if b == 0 or v.days[b - 1].week == d.week:
                continue                            # not the week's first day
            if d.roll:
                _skip(skipped, "roll")
                continue
            if not d.complete:
                _skip(skipped, "incomplete")
                continue
            if not (d.gap == d.gap) or d.gap == 0:
                continue
            week = [x for x in v.days[b:] if x.week == d.week]
            # A week still under way counts once it is over, filled or not:
            # counting it early only when filled would count more fills.
            over = any(x.weekday == 4 for x in week) or b + len(week) < len(v.days)
            if not over:
                _skip(skipped, "week_not_over")
                continue
            monday = v.rows(d, 18 * 60, 16 * 60)
            rows = np.concatenate([v.rows(x, 18 * 60, 16 * 60) for x in week])
            cell["trials"] += 1
            cell["base_trials"] += 1
            filled, other = _gap_test(v, d, rows)
            cell["hits"] += filled
            cell["base_hits"] += other
            diffs.append(filled - other)
            dates.append(d.day.toordinal())
            filled, other = _gap_test(v, d, monday)
            cell["hit_monday"] += filled
            cell["base_hit_monday"] += other
    if not any(c["trials"] for c in by.values()):
        return None
    pooled = _pool(by)
    for key in ("hit_monday", "base_hit_monday"):
        pooled[key] = sum(c[key] for c in by.values())
    v_ = _gap_verdict(pooled, diffs, dates)
    notes = ["Hourly prices, about two years. Filled means price traded back to Friday's "
             "close. The verdict is on the gaps filled by Friday 16:00; the share filled by "
             "Monday 16:00 is shown beside it.", _GAP_NOTE]
    return _record("nwog_fills", "1h", by, notes, skipped, pooled=pooled, verdict=v_)


# --- the bias study --------------------------------------------------------------------

def bias_study(bars_1h) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """The daily lean, read each day as of 09:00 New York (09:30 for stocks) from
    hourly bars closed by then, against the 16:00 close.

    Returns ({symbol: labstore bias record, "_all": the pooled one}, skipped).
    Also per symbol "up_called": up days among the days it leaned.
    """
    out: Dict[str, Dict] = {}
    skipped: Dict[str, int] = {}
    pool = {"days": 0, "called": 0, "right": 0, "up_days": 0, "unclear": 0,
            "target_reached": 0, "from": 0, "to": 0, "up_called": 0}
    for bars in bars_1h:
        if bars is None or bars.interval not in ("1h", "60m") or len(bars.close) < 100:
            continue
        v = _View(bars)
        prep = bias_mod.prepare(bars)
        if prep is None:
            continue
        decide = 9 * 60 + 30 if v.instrument == "stock" else 9 * 60
        rec = {"days": 0, "called": 0, "right": 0, "up_days": 0, "unclear": 0,
               "target_reached": 0, "from": 0, "to": 0, "up_called": 0}
        for d in v.days:
            if d.roll:
                _skip(skipped, "roll")
                continue
            if not d.complete:
                _skip(skipped, "incomplete")
                continue
            block = np.arange(d.first, d.last + 1)
            at_bar = block[v.minute[block] == decide]
            if not at_bar.size:
                _skip(skipped, "no_decision_bar")
                continue
            j = int(at_bar[0])
            ends = v.rows(d, decide, 16 * 60)
            if not ends.size:
                _skip(skipped, "incomplete")
                continue
            last = int(ends[-1])
            read = bias_mod.read(None, at=int(v.ts[j]), prepared=prep)
            if read is None:
                _skip(skipped, "too_little_history")
                continue
            price = float(v.o[j])
            move = float(v.c[last]) - price
            rec["days"] += 1
            rec["from"] = rec["from"] or int(v.ts[j])
            rec["to"] = int(v.ts[j])
            up = move > 0
            rec["up_days"] += 1 if up else 0
            if read.lean == 0:
                rec["unclear"] += 1
                continue
            rec["called"] += 1
            rec["up_called"] += 1 if up else 0
            if (read.lean > 0 and move > 0) or (read.lean < 0 and move < 0):
                rec["right"] += 1
            if read.targets:
                target = read.targets[0][1]
                span = np.arange(j, last + 1)
                reached = (np.max(v.h[span]) >= target) if read.lean > 0 \
                    else (np.min(v.l[span]) <= target)
                rec["target_reached"] += 1 if reached else 0
        if not rec["days"]:
            continue
        out[bars.symbol] = rec
        for key in ("days", "called", "right", "up_days", "unclear", "target_reached",
                    "up_called"):
            pool[key] += rec[key]
        pool["from"] = min(pool["from"] or rec["from"], rec["from"])
        pool["to"] = max(pool["to"], rec["to"])
    if out:
        out[labstore.POOL_ALL] = pool
    return out, skipped


def bias_works(study: Dict[str, Dict], skipped: Dict[str, int]) -> Optional[Dict]:
    """video 4:59:46-5:24:14: the lean calls the day. Right on the days it leaned,
    against always saying up on those same days."""
    by = {}
    for sym, rec in study.items():
        if sym == labstore.POOL_ALL:
            continue
        by[sym] = {"trials": rec["called"], "hits": rec["right"],
                   "base_trials": rec["called"], "base_hits": rec["up_called"]}
    if not by:
        return None
    pooled = _pool(by)
    notes = ["Hourly prices, about two years. The lean is read from the bars closed by "
             "9:00 New York time (9:30 for stocks); it is right when the 16:00 close is on "
             "its side of the price at that time."]
    if pooled["trials"] >= labstore.MIN_TRADES:
        lo, hi = labstore.wilson(pooled["hits"], pooled["trials"])
        notes.append("Against a coin flip: right on %s of %s days it leaned, a range of %d%% "
                     "to %d%%, %s one in two."
                     % ("{:,}".format(pooled["hits"]), "{:,}".format(pooled["trials"]), round(lo * 100), round(hi * 100),
                        "above" if lo > 0.5 else "below" if hi < 0.5 else "which includes"))
    notes += _index_note(by)
    return _record("bias_works", "1h", by, notes, skipped)


# --- the run ----------------------------------------------------------------------------

def run(conn, bars_by_key: Dict[Tuple[str, str], object], fetch: Callable = market.fetch_bars,
        trades: Optional[Dict[str, Dict]] = None, pause: float = 3.0,
        sleep: Callable[[float], None] = time.sleep, now: Optional[float] = None,
        series: Optional[Dict[Tuple[str, str], Dict]] = None) -> Dict:
    """Measure every must-have claim and the bias study, and store them.

    bars_by_key holds the lab's downloads by (symbol, interval); hourly prices
    for the bias study and the weekly gaps are fetched here. series holds what
    the lab kept of each one's price structure (setuplab.CLAIM_SERIES), which
    is worked out here for any that has none. A claim with nothing to measure
    is not written, so an earlier record stays. One failing claim is recorded
    and the rest carry on.
    """
    errors: List[str] = []
    written: List[str] = []
    views = {key: _View(b, (series or {}).get(key)) for key, b in sorted(bars_by_key.items())}
    five = [v for (sym, iv), v in views.items() if iv == "5m"]
    both = [v for (sym, iv), v in views.items() if iv in ("5m", "15m")]
    by_symbol_5m = {sym: v for (sym, iv), v in views.items() if iv == "5m"}
    symbols = []
    future = set()
    for (sym, _), v in views.items():
        if sym not in symbols:
            symbols.append(sym)
        if v.instrument == "future":
            future.add(sym)
    hourly = []
    came = set()
    for sym in symbols:
        if pause:
            sleep(pause)
        try:
            hourly.append(fetch(sym, "1h"))
            came.add(sym)
        except market.DataThrottled as exc:
            errors.append("%s 1h: %s" % (sym, exc))
            break
        except Exception as exc:
            errors.append("%s 1h: %s" % (sym, exc))
    jobs = [
        ("london_takes_asia", lambda: london_takes_asia(five)),
        ("ny_takes_london", lambda: ny_takes_london(five)),
        ("pd_sweep_reverse", lambda: pd_sweep_reverse(five)),
        ("open_manipulation", lambda: open_manipulation(five)),
        ("macro_window", lambda: macro_window(trades)),
        ("after_1030_slower", lambda: after_1030_slower(five)),
        ("fvg_gets_retested", lambda: fvg_gets_retested(both)),
        ("close_beats_wick", lambda: close_beats_wick(five)),
        ("crowd_breakout_loses", lambda: crowd_breakout_loses(trades)),
        ("sweep_alone_loses", lambda: sweep_alone_loses(trades)),
        ("not_every_day_green", lambda: not_every_day_green(trades)),
        ("ndog_fills", lambda: ndog_fills(five)),
        ("nwog_fills", lambda: nwog_fills(hourly)),
        ("smt_at_sweep", lambda: smt_at_sweep(by_symbol_5m)),
    ]
    for cid, job in jobs:
        if cid == "nwog_fills" and future - came:
            # Measured on some of the futures, it would replace the whole
            # with a part, as the pooled lean below.
            errors.append("claim nwog_fills: left as it was, no hourly prices for %s"
                          % ", ".join(sorted(future - came)))
            continue
        try:
            rec = job()
            if rec is not None and (rec["pooled"]["trials"] or rec["pooled"]["base_trials"]):
                labstore.save_claim(conn, rec, now=now)
                written.append(cid)
        except Exception as exc:
            log.exception("Claim %s failed", cid)
            errors.append("claim %s: %s" % (cid, exc))
    studied: List[str] = []
    # The pooled lean and its claim are replaced only when every instrument's
    # hourly prices came: a part of them would overwrite the whole.
    whole = len(hourly) == len(symbols)
    try:
        study, skipped = bias_study(hourly)
        for sym, rec in study.items():
            if sym == labstore.POOL_ALL and not whole:
                continue
            data = {k: int(rec[k]) for k in ("days", "called", "right", "up_days", "unclear",
                                             "target_reached", "from", "to")}
            labstore.save_bias(conn, sym, data, now=now)
            studied.append(sym)
        rec = bias_works(study, skipped)
        if whole and rec is not None and rec["pooled"]["trials"]:
            labstore.save_claim(conn, rec, now=now)
            written.append("bias_works")
    except Exception as exc:
        log.exception("The bias study failed")
        errors.append("bias study: %s" % exc)
    return {"claims": written, "bias": studied, "errors": errors}
