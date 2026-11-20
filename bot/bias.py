"""Which way the course's method would lean for the day, read from hourly prices.

TJR's video (4:58:43-5:26:19, and the read before the open at 6:08:18-
6:13:34) decides a daily bias top-down: the 4-hour trend, then what the
hourly did lately, especially whether an old high or low was taken and price
then turned. The video shows the method on examples rather than as a rule,
so the rule here is the app's codification of it, said on the page beside
its measured record (the bias study in the Setup lab):

1. If, since the current trading day began, an old high or low was taken on
   the hourly bars and the hourly's most recent break of structure (or close
   through a gap) went the other way, lean that way. The video's case at
   5:07:09: a fall, the lows taken before the open, then an hourly close back
   above a gap the fall left, so up.
2. Else, if the newest 4-hour gap in the 4-hour trend's direction was closed
   through, lean against that trend.
3. Else, if the hourly trend agrees with the 4-hour trend, lean with it. When
   price had come back to the 4-hour gap or halfway line and held before the
   hourly broke structure, the reason says so.
4. Else, unclear.

The video also says a bias can be overruled by what price does next
(6:27:20-6:28:23), so the hourly's most recent break is the one that counts
(app choice), and flip_level is the hourly close that would overrule the lean.

read() uses only the hourly bars closed by `at`, so a read made at 09:00 is
the read anyone could have made at 09:00. 4-hour bars are built from the
hourly ones (clock.resample): the data source has none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import clock, liquidity, structure
from .market import format_price

# app choice: fewer closed hourly bars than this (about three days of a
# future) is too little to read a 4-hour trend from.
MIN_BARS = 60
HOUR = 3600


@dataclass
class BiasRead:
    lean: int                          # +1 up, -1 down, 0 unclear
    reasons: List[str]                 # one plain sentence per check
    checks: Dict                       # trend_4h, trend_1h, sweep, key, rule
    targets: List[Tuple[str, float]]   # nearest untaken levels the lean's way, up to 3
    flip_level: Optional[float]        # an hourly close beyond it changes the hourly trend
    as_of: int                         # start time of the last closed hourly bar used
    extra: Dict = field(default_factory=dict)


def _trend_words(t: int, where: str) -> str:
    if t == 1:
        return ("On %s the last swing broken was a high, so the trend there counts "
                "as up." % where)
    if t == -1:
        return ("On %s the last swing broken was a low, so the trend there counts "
                "as down." % where)
    return "On %s no swing has been broken yet, so there is no trend there." % where


def _last(arr: np.ndarray, value: int, start: int = 0, end: Optional[int] = None) -> int:
    """The last position in [start, end] where arr equals value, or -1."""
    end = len(arr) - 1 if end is None else end
    if end < start:
        return -1
    hits = np.flatnonzero(arr[start:end + 1] == value)
    return int(hits[-1]) + start if hits.size else -1


@dataclass
class Prepared:
    """Everything read() works out from an hourly series, done once for all of it.

    Every series here is the same at bar i whether the bars stop at i or go
    on, so a read as of any time can slice these instead of starting again.
    """
    bars: object
    kind: str
    step: int
    s1: Dict[str, np.ndarray]             # liquidity.series of the hourly bars
    days: List                            # clock.day_table of them
    buckets: List[Tuple[int, int, int, int, int]]   # clock.buckets, 4 hours
    b4: object                            # the 4-hour bars over by the last hourly bar
    s4: Dict[str, np.ndarray]             # structure.series of those


def prepare(bars_1h, k: int = structure.K) -> Optional[Prepared]:
    """Work out once what reads of this hourly series at many times share.

    A study reading every day of two years prepares once and passes the
    result to read(); the read is the same as without it.
    """
    if bars_1h is None or len(bars_1h.close) == 0:
        return None
    kind = clock.kind_of(bars_1h)
    if kind == "daily":
        return None
    b4 = clock.resample(bars_1h, 4)
    s4 = structure.series(b4, k) if len(b4.close) else structure.empty(0)
    return Prepared(bars=bars_1h, kind=kind, step=clock.bar_seconds(bars_1h),
                    s1=liquidity.series(bars_1h, k), days=clock.day_table(bars_1h),
                    buckets=clock.buckets(bars_1h, 4), b4=b4, s4=s4)


def read(bars_1h, at: Optional[int] = None, k: int = structure.K,
         prepared: Optional[Prepared] = None) -> Optional[BiasRead]:
    """The lean as of `at` (UTC seconds; default: just after the last bar),
    from the hourly bars that had closed by then. None when there are too few.

    `prepared`, from prepare() on these same bars, saves working everything out
    again for each read; bars_1h may then be None.
    """
    if prepared is not None:
        bars_1h = prepared.bars
    if bars_1h is None or len(bars_1h.close) == 0:
        return None
    kind = clock.kind_of(bars_1h)
    if kind == "daily":
        return None
    step = clock.bar_seconds(bars_1h)
    ts_all = np.asarray(bars_1h.timestamp, dtype=np.int64)
    if at is None:
        at = int(ts_all[-1]) + step
    at = int(at)
    m = int(np.searchsorted(ts_all + step, at, side="right"))
    if m < MIN_BARS:
        return None
    if prepared is None:
        prepared = prepare(clock.head(bars_1h, m), k)
    return _read(prepared, m, at)


def _read(p: Prepared, m: int, at: int) -> BiasRead:
    """The read from the first m hourly bars, with everything else as of `at`."""
    kind, step = p.kind, p.step
    bars = clock.head(p.bars, m)
    s1 = {key: arr[:m] for key, arr in p.s1.items()}
    last = m - 1
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    close = float(bars.close[last])

    # The 4-hour buckets over by then: those that ended before the last bar's
    # bucket, and that one when its time was up by the last bar's close.
    done_by = int(ts[last]) + step
    n4 = sum(1 for (_, _, e, end, _) in p.buckets if e < m or (e == m and end <= done_by))
    n4 = min(n4, len(p.b4.close))
    s4 = {key: arr[:n4] for key, arr in p.s4.items()}
    ts4 = np.asarray(p.b4.timestamp[:n4], dtype=np.int64)
    t1 = int(s1["st_trend"][last])
    t4 = int(s4["st_trend"][-1]) if n4 else 0
    trend_at4 = int(s4["st_trend_at"][-1]) if n4 else -1

    def when(t: int) -> str:
        stamp = clock.label_ny(int(t))
        day, _, hm = stamp.rpartition(" ")
        return "%s at %s New York time" % (day, hm)

    reasons = [_trend_words(t4, "4-hour bars (made from hourly ones)"),
               _trend_words(t1, "hourly bars")]
    checks: Dict = {"trend_4h": t4, "trend_1h": t1, "sweep": None, "key": None, "rule": 4}
    lean: Optional[int] = None

    # --- 1: taken today, then turned -------------------------------------------
    day_now = clock.trading_day(at, kind).toordinal()
    today_start = -1
    if int(s1["ck_day"][last]) == day_now:
        today_start = last
        while today_start > 0 and int(s1["ck_day"][today_start - 1]) == day_now:
            today_start -= 1
    if today_start >= 0:
        # app choice: the day's most recent break decides (video 6:27:20: a
        # bias can be overruled), and it counts when something on its side
        # was taken earlier the same day.
        breaks = np.flatnonzero((s1["st_flip"][today_start:last + 1] != 0)
                                | (s1["st_ext"][today_start:last + 1] != 0)
                                | (s1["st_ifvg"][today_start:last + 1] != 0))
        mm = int(breaks[-1]) + today_start if breaks.size else -1
        dirs = ({int(s1["st_flip"][mm]), int(s1["st_ext"][mm]), int(s1["st_ifvg"][mm])} - {0}
                if mm >= 0 else set())
        d = dirs.pop() if len(dirs) == 1 else 0
        j = _last(s1["lq_take"], d, today_start, mm) if d else -1
        if j >= 0:
            lean = d
            name = liquidity.level_name(s1, j)
            how = ("closed back through a gap the %s had left" % ("fall" if d == 1 else "rise")
                   if int(s1["st_ifvg"][mm]) == d and not int(s1["st_flip"][mm])
                   and not int(s1["st_ext"][mm])
                   else "broke its last swing %s" % ("high" if d == 1 else "low"))
            checks["sweep"] = {"dir": d, "level_name": name, "at": int(ts[j]),
                               "price": float(s1["lq_take_lvl"][j])}
            checks["rule"] = 1
            reasons.append(
                "Since this trading day began, price took %s at %s (%s), then %s (%s). "
                "The course reads that as a turn, so it leans %s."
                % (name, format_price(s1["lq_take_lvl"][j]), when(ts[j]), how, when(ts[mm]),
                   "up" if d == 1 else "down"))
    if checks["sweep"] is None and today_start >= 0:
        j = int(np.flatnonzero(s1["lq_take"][today_start:last + 1] != 0)[-1]) + today_start \
            if np.any(s1["lq_take"][today_start:last + 1] != 0) else -1
        if j >= 0:
            d = int(s1["lq_take"][j])
            checks["sweep"] = {"dir": d, "level_name": liquidity.level_name(s1, j),
                               "at": int(ts[j]), "price": float(s1["lq_take_lvl"][j])}

    # --- 2: the 4-hour gap closed through ------------------------------------------
    if lean is None and t4 != 0 and n4:
        m4 = _last(s4["st_ifvg"], -t4)
        g4 = _last(s4["st_fvg_new"], t4)
        if m4 > trend_at4 and m4 > g4 and m4 >= 1:
            lean = -t4
            edge = float(s4["st_ifvg_lvl"][m4])
            checks["rule"] = 2
            top = s4["st_fvg_up_top" if t4 == 1 else "st_fvg_dn_top"][m4 - 1]
            bot = s4["st_fvg_up_bot" if t4 == 1 else "st_fvg_dn_bot"][m4 - 1]
            checks["key"] = {"kind": "gap", "top": float(top), "bottom": float(bot),
                             "status": "closed through"}
            reasons.append(
                "The 4-hour %s left a gap, and a 4-hour bar then closed back through it, "
                "past %s (%s). The course reads that as a turn against the 4-hour "
                "trend, so it leans %s."
                % ("rise" if t4 == 1 else "fall", format_price(edge), when(ts4[m4]),
                   "down" if t4 == 1 else "up"))

    # --- 3: with the 4-hour trend ---------------------------------------------------
    # The plan's rule also leans with the 4-hour trend when price came back to
    # its gap or halfway line, held, and the hourly then broke structure its
    # way. app choice: the hourly's most recent break decides, as in rule 1, so
    # that case is the hourly agreeing; the touch is said as the reason.
    if lean is None and t4 != 0 and t1 == t4:
        lean = t4
        checks["rule"] = 3
        why = "The hourly trend agrees with the 4-hour trend, so it leans %s." % (
            "up" if t4 == 1 else "down")
        hits = np.flatnonzero(((s4["st_fvg_touch"] == t4) | (s4["st_eq_touch"] == t4))
                              & (np.arange(n4) > trend_at4)) if n4 else np.zeros(0, int)
        if hits.size:
            m4t = int(hits[-1])
            after = int(ts4[m4t]) + 4 * HOUR
            broke = np.flatnonzero(((s1["st_flip"] == t4) | (s1["st_ext"] == t4))
                                   & (ts >= after))
            if broke.size and m4t >= 1:
                if int(s4["st_fvg_touch"][m4t]) == t4:
                    top = float(s4["st_fvg_up_top" if t4 == 1 else "st_fvg_dn_top"][m4t - 1])
                    bot = float(s4["st_fvg_up_bot" if t4 == 1 else "st_fvg_dn_bot"][m4t - 1])
                    checks["key"] = {"kind": "gap", "top": top, "bottom": bot,
                                     "status": "touched and held"}
                    place = "the 4-hour gap between %s and %s" % (format_price(bot),
                                                                  format_price(top))
                else:
                    line = float(s4["st_eq"][m4t - 1])
                    checks["key"] = {"kind": "half", "top": line, "bottom": line,
                                     "status": "touched and held"}
                    place = "the 4-hour halfway line at %s" % format_price(line)
                why = ("Price came back to %s (%s), held there, and the hourly then broke "
                       "its last swing %s (%s), so it leans %s."
                       % (place, when(ts4[m4t]), "high" if t4 == 1 else "low",
                          when(ts[int(broke[-1])]), "up" if t4 == 1 else "down"))
        reasons.append(why)

    if lean is None:
        lean = 0
        checks["rule"] = 4
        reasons.append("The checks found no clear lean, so the read is unclear.")

    # --- what it would aim at, and what would change its mind -------------------------
    # Whole days before today only: today's own entry in the table may hold
    # bars after the read.
    past = [d for d in p.days if d.day.toordinal() < day_now and d.last < m]
    targets = _targets(bars, s1, last, lean, day_now, kind, past, today_start) if lean else []
    flip = None
    side = lean or t1
    if side == 1:
        flip = float(s1["st_ref_lo"][last])
        if flip != flip:
            flip = float(s1["st_lo"][last])
    elif side == -1:
        flip = float(s1["st_ref_hi"][last])
        if flip != flip:
            flip = float(s1["st_hi"][last])
    if flip is not None and flip != flip:
        flip = None
    return BiasRead(lean=int(lean), reasons=reasons, checks=checks, targets=targets,
                    flip_level=flip, as_of=int(ts[last]),
                    extra={"close": close, "kind": kind})


def _targets(bars, s1, last: int, lean: int, day_now: int, kind: str, past: List,
             today_start: int) -> List[Tuple[str, float]]:
    """The nearest old highs (or lows) the lean points at, not yet traded through.

    From the series at the last bar, today's bars, and `past`: the whole
    trading days before today.
    """
    close = float(bars.close[last])
    up = lean == 1
    found: List[Tuple[str, float]] = []

    def add(name: str, price) -> None:
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p != p:
            return
        if (up and p > close) or (not up and p < close):
            found.append((name, p))

    side = "high" if up else "low"
    now_week = date.fromordinal(day_now).isocalendar()[:2]
    if today_start >= 0:
        today_hi, today_lo = float(s1["ck_day_hi"][last]), float(s1["ck_day_lo"][last])
        pd = float(s1["ck_pdh" if up else "ck_pdl"][last])
        if pd == pd and ((up and today_hi < pd) or (not up and today_lo > pd)):
            add("yesterday's %s" % side, pd)
        this_week = [d for d in past if tuple(d.week) == tuple(now_week)]
        wk_hi = max([d.high for d in this_week] + [today_hi])
        wk_lo = min([d.low for d in this_week] + [today_lo])
        pw = float(s1["ck_pwh" if up else "ck_pwl"][last])
        if pw == pw and ((up and wk_hi < pw) or (not up and wk_lo > pw)):
            add("last week's %s" % side, pw)
        if kind in ("future", "coin"):
            # A session's range is in the series from the first bar after it,
            # once it has ended with enough of its bars.
            for keys, label in ((("ck_asia_hi", "ck_asia_lo"), "the Asia session's"),
                                (("ck_lon_hi", "ck_lon_lo"), "the London session's")):
                arr = s1[keys[0] if up else keys[1]]
                level = float(arr[last])
                if level != level:
                    continue
                known = np.flatnonzero(np.isfinite(arr[today_start:last + 1]))
                after = today_start + int(known[0])
                if (up and float(np.max(bars.high[after:last + 1])) < level) or \
                        (not up and float(np.min(bars.low[after:last + 1])) > level):
                    add("%s %s" % (label, side), level)
    elif past:
        prev = past[-1]
        if prev.complete:
            add("yesterday's %s" % side, prev.high if up else prev.low)
        if tuple(prev.week) != tuple(now_week):
            wk = [d for d in past if d.week == prev.week]
            add("last week's %s" % side, max(d.high for d in wk) if up else min(d.low for d in wk))
        else:
            add("last week's %s" % side, s1["ck_pwh" if up else "ck_pwl"][last])
    add("equal %ss" % side, s1["st_eqh" if up else "st_eql"][last])
    for key in (("st_above1", "st_above2", "st_above3") if up else
                ("st_below1", "st_below2", "st_below3")):
        add("an earlier swing %s" % side, s1[key][last])

    found.sort(key=lambda x: abs(x[1] - close))
    out: List[Tuple[str, float]] = []
    for name, price in found:
        if any(abs(price - p) <= 1e-9 * max(1.0, abs(p)) for _, p in out):
            continue
        out.append((name, price))
        if len(out) == 3:
            break
    return out
