"""The Setup lab: each of the course's setups, traded on past intraday prices.

TJR's video ("How To Start Day Trading In 2026") teaches a handful of setups
with their own stops and targets. The daily strategy test (bot/public.py)
measures them with the house stop and target on daily bars; this measures
each one the way the course trades it, on 5- and 15-minute bars, and asks the
question that matters: did it keep money after costs, by more than luck
would, and did it do better than random trades taken at the same time of day?

For every instrument in setups.LAB_SYMBOLS and every interval in
setups.LAB_INTERVALS:

- every event of every setup is a trade (no thinning), entered at the close
  of the bar it appears on;
- the stop is the setup's own (setups.Setup.stop_rule, as the course places
  it: past the high or low that was taken, and so on); a stop closer than a
  tenth of a usual bar's range is not tradable and is counted, not taken;
- two targets are kept apart: twice the risk ("2r"), and the nearest old high
  or low not yet traded through, at least the risk away ("level");
- the trade ends at the stop, the target, or the next 16:00 New York time, at
  that bar's close, by the rules calibrate.simulate_trade uses (the stop is
  checked first on a bar that reaches both, and a bar opening past the stop
  fills at its open); one whose 16:00 is not in the prices yet is counted as
  still open, whatever it has done so far, and joins on a later run;
- the round-trip cost is charged in units of risk, at the house cost for the
  instrument (config.cost_bps_for_symbol);
- each trade gets a matched random control: up to five bars on other trading
  days in the same New York half hour, taken the same way with a stop and
  target the same size against each bar's own usual range, and the same exit
  and costs. The control's result is their average.

The margin of error clusters trades by trading day (trades on one day share
its news and win or lose together); the last third of the days is kept apart
as a check that the first two thirds' sign holds. A cell needs 30 trades on
10 separate days before it gets a verdict (labstore.verdict).

Futures roll days and days with too few bars are left out, and counted.
Every value a trade's entry, stop or target reads is as of its entry bar:
the setups come from liquidity.series, whose value at bar i depends on bars
0..i only, and the targets read the same series at i. tests/test_setuplab.py
checks it by cutting the bars at the entry.

Writes only through labstore. bot/claims.py measures the video's claims from
the same downloads; the web server runs both nightly (due()).

    python -m bot.setuplab --refresh [--symbols ES=F,SPY] [--intervals 5m]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import clock, labstore, liquidity, market, setups, structure
from . import config as config_mod

log = logging.getLogger(__name__)

DAY = 86400
# app choice: five random entries per trade make its control.
CONTROLS = 5
# app choice: a stop closer than a tenth of the usual bar's range (ATR 14) is
# inside the bar-to-bar noise: no one could hold it, so the trade is counted
# as skipped rather than scored as a huge win or loss in units of risk.
MIN_STOP_ATR = 0.1
TARGET_R = 2.0                     # video 6:24:12-6:30:28: at least twice the risk
EXIT_MINUTE = 16 * 60              # video 4:33:23: done by the New York afternoon
# The job runs once a day after the futures' 17:00 pause has begun, so the
# day's bars are all in.
DUE_AGE = 20 * 3600
DUE_MINUTE = 17 * 60 + 5
# app choice: a run that wrote nothing (the source refused every download) is
# tried again after two hours rather than the next day.
RETRY_AGE = 2 * 3600
EXAMPLE_BARS = 70
SENSITIVITY_K = 3                  # the swing width the four-step model is also tried with

SETUP_NAMES: Tuple[str, ...] = tuple(s.name for s in setups.SETUPS)
# The price-structure series bot/claims.py reads, handed over so that it does
# not work them out again.
CLAIM_SERIES = ("st_atr", "st_fvg_new", "st_sweep", "st_sweep_back")


# --- trades ---------------------------------------------------------------------

@dataclass
class Trade:
    """One trade of one setup under one target rule (or a control's)."""
    setup: str
    rule: str
    i: int                     # entry bar
    exit_i: int                # the bar it ended on
    day: int                   # trading day (date ordinal) of the entry
    direction: int             # +1 long, -1 short
    entry: float
    stop: float
    target: float
    risk: float
    r: float                   # kept, after costs
    r_gross: float
    cost_r: float
    bars: int                  # bars held
    hit: bool                  # the target was reached
    window: str                # clock.LAB_WINDOWS key of the entry
    target_r: float            # the target's distance in units of risk
    fallback: bool = False     # "level" rule with no level far enough: twice the risk
    control_r: Optional[float] = None
    controls: Tuple[int, ...] = ()


@dataclass
class Prep:
    """What every setup's trades on one series share, worked out once."""
    symbol: str
    interval: str
    instrument: str
    step: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ts: np.ndarray
    atr: np.ndarray
    day: np.ndarray            # trading day ordinal per bar
    minute: np.ndarray         # New York minute per bar
    exit_i: np.ndarray         # the bar each entry would leave on, -1 when that is not in yet
    good: np.ndarray           # the bar's trading day is whole and not a roll day
    roll_cum: np.ndarray       # running count of roll bars, for window checks
    window: np.ndarray         # index into clock.LAB_WINDOWS per bar
    days_used: List[int]
    skipped_days: Dict[str, int]
    cost_bps: float
    untaken: Dict[str, np.ndarray]
    buckets: Dict[int, np.ndarray]


def _window_index(minute: np.ndarray) -> np.ndarray:
    out = np.full(len(minute), len(clock.LAB_WINDOWS) - 1, dtype=np.int64)
    for k, (_, start, end) in enumerate(clock.LAB_WINDOWS):
        if start < end:
            mask = (minute >= start) & (minute < end)
        else:
            mask = (minute >= start) | (minute < end)
        out[mask] = k
    return out


def _exit_index(ts: np.ndarray, step: int) -> np.ndarray:
    """For each bar, the last bar ending by the next 16:00 New York time after
    it closes, or -1 when the bars end before that moment, so what would
    happen by then is not known yet. A bar closing at 16:00 exactly exits on
    itself: no time is left to trade, so trades on it are skipped."""
    end = ts + step
    local = end + clock.offsets(end)
    lday = local // DAY
    lmin = (local % DAY) // 60
    exit_day = lday + (lmin > EXIT_MINUTE)
    moments = {}
    for d in np.unique(exit_day).tolist():
        dt = date.fromordinal(clock.EPOCH + int(d))
        moments[d] = clock.ny(dt.year, dt.month, dt.day, 16, 0)
    x = np.array([moments[d] for d in exit_day.tolist()], dtype=np.int64)
    out = np.searchsorted(end, x, side="right").astype(np.int64) - 1
    return np.where(x > end[-1], -1, out)


def _running_untaken(h, l, s, days, key: str, high: bool) -> np.ndarray:
    """True at bars where today's level under `key` is known and no bar since it
    became known (this one included) has traded past it."""
    level = s[key]
    out = np.zeros(len(level), dtype=bool)
    for d in days:
        a, b = d.first, d.last + 1
        known = np.flatnonzero(np.isfinite(level[a:b]))
        if not known.size:
            continue
        s0 = a + int(known[0])
        if high:
            run = np.maximum.accumulate(h[s0:b])
            out[s0:b] = run < level[s0:b]
        else:
            run = np.minimum.accumulate(l[s0:b])
            out[s0:b] = run > level[s0:b]
    return out


def prepare(bars, s: Dict[str, np.ndarray], cost_bps: float, symbol: Optional[str] = None,
            interval: Optional[str] = None) -> Prep:
    """Work out once what every setup's trades on these bars share."""
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    n = len(c)
    step = clock.bar_seconds(bars)
    days = clock.day_table(bars)
    good = np.zeros(n, dtype=bool)
    used: List[int] = []
    incomplete = roll = 0
    for d in days:
        if d.roll:
            roll += 1
        elif not d.complete:
            incomplete += 1
        else:
            good[d.first:d.last + 1] = True
            used.append(d.day.toordinal())
    roll_bar = s["ck_roll"].astype(np.int64) if "ck_roll" in s else np.zeros(n, dtype=np.int64)
    roll_cum = np.concatenate([[0], np.cumsum(roll_bar)])
    minute = np.asarray(s["ck_min"], dtype=np.int64)
    atr = np.asarray(s["st_atr"], dtype=float)
    exit_i = _exit_index(ts, step) if n else np.zeros(0, dtype=np.int64)
    untaken = {
        "asia_hi": _running_untaken(h, l_, s, days, "ck_asia_hi", True),
        "asia_lo": _running_untaken(h, l_, s, days, "ck_asia_lo", False),
        "lon_hi": _running_untaken(h, l_, s, days, "ck_lon_hi", True),
        "lon_lo": _running_untaken(h, l_, s, days, "ck_lon_lo", False),
        "pre_hi": _running_untaken(h, l_, s, days, "ck_pre_hi", True),
        "pre_lo": _running_untaken(h, l_, s, days, "ck_pre_lo", False),
    }
    p = Prep(symbol=symbol or getattr(bars, "symbol", ""), interval=interval or bars.interval,
             instrument=clock.instrument_of(bars), step=step, o=o, h=h, l=l_, c=c, ts=ts,
             atr=atr, day=np.asarray(s["ck_day"], dtype=np.int64), minute=minute,
             exit_i=exit_i, good=good, roll_cum=roll_cum, window=_window_index(minute),
             days_used=used, skipped_days={"incomplete": incomplete, "roll": roll},
             cost_bps=float(cost_bps), untaken=untaken, buckets={})
    # Bars a control may be taken at: a whole day, a usual range, time to
    # trade before 16:00, and no roll inside the trade.
    idx = np.arange(n)
    ok = good & np.isfinite(atr) & (atr > 0) & (exit_i > idx)
    if n:
        ok &= (roll_cum[np.maximum(exit_i, idx) + 1] - roll_cum[idx]) == 0
    half = minute // 30
    for hh in np.unique(half[ok]).tolist():
        p.buckets[int(hh)] = idx[ok & (half == hh)]
    return p


def walk(o, h, l, c, i: int, last: int, direction: int, entry: float, stop: float,
         targets: Sequence[float]) -> List[Tuple[float, bool, int]]:
    """Walk bars i+1..last for each target: (exit price, target reached, exit bar).

    calibrate.simulate_trade's rules: a bar that opens past the stop fills at
    its open; one that opens past the target fills at the target; on a bar
    reaching both, the stop counts first (the order inside a bar is unknown,
    and the favourable order would flatter the result); a trade reaching
    neither ends at the close of `last`.
    """
    out: List[Tuple[float, bool, int]] = []
    if last <= i:
        return [(float(c[i]), False, i) for _ in targets]
    seg_o = o[i + 1:last + 1]
    if direction > 0:
        stop_hit = l[i + 1:last + 1] <= stop
    else:
        stop_hit = h[i + 1:last + 1] >= stop
    ks = int(np.argmax(stop_hit)) if stop_hit.any() else -1
    for target in targets:
        if direction > 0:
            t_hit = h[i + 1:last + 1] >= target
        else:
            t_hit = l[i + 1:last + 1] <= target
        kt = int(np.argmax(t_hit)) if t_hit.any() else -1
        if ks < 0 and kt < 0:
            out.append((float(c[last]), False, last))
            continue
        if kt < 0 or (ks >= 0 and ks <= kt):
            k = ks
            op = float(seg_o[k])
            gapped_stop = op <= stop if direction > 0 else op >= stop
            if gapped_stop:
                out.append((op, False, i + 1 + k))
            elif k == kt and (op >= target if direction > 0 else op <= target):
                out.append((float(target), True, i + 1 + k))
            else:
                out.append((float(stop), False, i + 1 + k))
        else:
            out.append((float(target), True, i + 1 + kt))
    return out


def events(name: str, s: Dict[str, np.ndarray], bars) -> Tuple[np.ndarray, np.ndarray]:
    """(bar positions, directions) where a setup appears, by its F9 rule."""
    o = np.asarray(bars.open, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    turn = np.sign(c - o).astype(np.int64)
    if name in ("sweep_reclaim", "equal_levels_sweep"):
        d = s["st_sweep"].astype(np.int64)
        mask = (d != 0) & (s["st_sweep_back"] == 1)
        if name == "equal_levels_sweep":
            mask &= s["st_sweep_eq"] == 1
    elif name == "prior_day_sweep":
        d = s["ck_pd_take"].astype(np.int64)
        mask = (d != 0) & (s["ck_pd_back"] == 1)
    elif name == "session_sweep":
        d = s["ck_sess_take"].astype(np.int64)
        mask = (d != 0) & (s["ck_sess_back"] == 1)
    elif name == "structure_flip":
        d = s["st_flip"].astype(np.int64)
        mask = d != 0
    elif name == "sweep_then_flip":
        d = s["lq_asf"].astype(np.int64)
        mask = d != 0
    elif name == "fvg_retest":
        d = s["st_fvg_touch"].astype(np.int64)
        trend_prev = np.concatenate([[0], s["st_trend"][:-1].astype(np.int64)])
        mask = (d != 0) & (d == trend_prev) & (turn == d)
    elif name == "ifvg_flip":
        d = s["st_ifvg"].astype(np.int64)
        mask = d != 0
    elif name == "premium_discount":
        d = s["st_eq_touch"].astype(np.int64)
        mask = (d != 0) & (turn == d)
    elif name == "liquidity_model":
        d = s["lq_chain"].astype(np.int64)
        mask = d != 0
    elif name == "crowd_breakout":
        # app choice: the crowd's breakout both ways, a first close past the
        # latest swing in the trend's own direction (structure's st_ext).
        d = s["st_ext"].astype(np.int64)
        mask = d != 0
    else:
        raise KeyError(name)
    idx = np.flatnonzero(mask)
    return idx, d[idx]


def stop_for(name: str, s: Dict[str, np.ndarray], p: Prep, i: int, d: int) -> float:
    """The setup's own stop for an entry at bar i (setups.STOP_RULES), from
    bars 0..i only."""
    rule = setups.setup(name).stop_rule
    if name == "liquidity_model":
        return float(s["lq_chain_stop"][i])
    if name == "sweep_then_flip":
        return float(s["lq_asf_stop"][i])
    if rule in ("sweep_extreme", "reaction_bar"):
        # video 6:24:12-6:30:28: past the high or low that was taken; for the
        # 50% line, past the bar that turned there.
        return float(p.l[i] if d > 0 else p.h[i])
    if rule == "leg_extreme":
        # The far end of the move the break ended: from the latest swing on
        # the other side (as of the bar before) to this bar.
        if i < 1:
            return math.nan
        a = int(s["st_hi_at"][i - 1] if d > 0 else s["st_lo_at"][i - 1])
        if a < 0:
            a = max(0, int(s["st_trend_at"][i - 1]))
        return float(np.min(p.l[a:i + 1]) if d > 0 else np.max(p.h[a:i + 1]))
    if rule == "gap_far_edge":
        if i < 1:
            return math.nan
        if d > 0:
            return float(min(p.l[i], s["st_fvg_up_bot"][i - 1]))
        return float(max(p.h[i], s["st_fvg_dn_top"][i - 1]))
    if rule == "last_swing_low":
        return float(s["st_lo"][i] if d > 0 else s["st_hi"][i])
    raise KeyError(rule)


def level_target(s: Dict[str, np.ndarray], p: Prep, i: int, d: int, entry: float,
                 risk: float) -> Optional[float]:
    """The nearest old high (long) or low (short) not yet traded through as of
    bar i, at least `risk` away: untaken swing points, equal highs or lows,
    yesterday's high or low, and today's session ranges. None when none is."""
    found: List[float] = []
    if d > 0:
        keys = ("st_above1", "st_above2", "st_above3", "st_eqh")
        for key in keys:
            found.append(float(s[key][i]))
        pdh = float(s["ck_pdh"][i])
        if pdh == pdh and float(s["ck_day_hi"][i]) < pdh:
            found.append(pdh)
        for key, name in (("ck_asia_hi", "asia_hi"), ("ck_lon_hi", "lon_hi"),
                          ("ck_pre_hi", "pre_hi")):
            if p.untaken[name][i]:
                found.append(float(s[key][i]))
        far = [x for x in found if x == x and x >= entry + risk]
        return min(far) if far else None
    keys = ("st_below1", "st_below2", "st_below3", "st_eql")
    for key in keys:
        found.append(float(s[key][i]))
    pdl = float(s["ck_pdl"][i])
    if pdl == pdl and float(s["ck_day_lo"][i]) > pdl:
        found.append(pdl)
    for key, name in (("ck_asia_lo", "asia_lo"), ("ck_lon_lo", "lon_lo"),
                      ("ck_pre_lo", "pre_lo")):
        if p.untaken[name][i]:
            found.append(float(s[key][i]))
    far = [x for x in found if x == x and x <= entry - risk]
    return max(far) if far else None


def plan_for(name: str, s: Dict[str, np.ndarray], p: Prep, i: int, d: int):
    """(entry, stop, risk, {rule: (target, fallback)}) for an entry at bar i, or
    a reason string when the trade is not taken. Reads bars 0..i only."""
    a = float(p.atr[i])
    if not (a == a and a > 0):
        return "warmup"
    entry = float(p.c[i])
    stop = stop_for(name, s, p, i, d)
    if stop != stop:
        return "tight_stop"
    risk = (entry - stop) if d > 0 else (stop - entry)
    if risk < MIN_STOP_ATR * a:
        return "tight_stop"
    lvl = level_target(s, p, i, d, entry, risk)
    targets = {"2r": (entry + d * TARGET_R * risk, False),
               "level": ((lvl, False) if lvl is not None
                         else (entry + d * TARGET_R * risk, True))}
    return entry, stop, risk, targets


def _seed(*parts) -> int:
    return zlib.crc32("|".join(str(x) for x in parts).encode("utf-8")) & 0x7FFFFFFF


def controls(p: Prep, setup: str, i: int, count: int = CONTROLS) -> List[int]:
    """Up to `count` random bars on other trading days in the same New York half
    hour as bar i, the same on every run (seeded by symbol, interval, setup and i)."""
    pool = p.buckets.get(int(p.minute[i]) // 30)
    if pool is None or not pool.size:
        return []
    pool = pool[p.day[pool] != p.day[i]]
    if not pool.size:
        return []
    rng = np.random.RandomState(_seed(p.symbol, p.interval, setup, i))
    k = min(count, int(pool.size))
    return sorted(int(j) for j in rng.choice(pool, size=k, replace=False))


def _control_r(p: Prep, js: List[int], i: int, d: int, risk: float,
               distances: Sequence[float]) -> List[Optional[float]]:
    """Each target's average control result: the same side, a stop and target
    the same size against each bar's usual range, the same exit and costs."""
    if not js:
        return [None for _ in distances]
    totals = [0.0 for _ in distances]
    a_i = float(p.atr[i])
    for j in js:
        scale = float(p.atr[j]) / a_i
        e = float(p.c[j])
        rk = risk * scale
        stop = e - d * rk
        targets = [e + d * dist * scale for dist in distances]
        cost_r = e * p.cost_bps / 10000.0 / rk
        for k, (px, _, _) in enumerate(walk(p.o, p.h, p.l, p.c, j, int(p.exit_i[j]), d,
                                            e, stop, targets)):
            totals[k] += d * (px - e) / rk - cost_r
    return [t / len(js) for t in totals]


def trades_for(bars, s: Dict[str, np.ndarray], setup: str, rule: Optional[str] = None,
               prep: Optional[Prep] = None, cost_bps: Optional[float] = None,
               with_controls: bool = True, skipped: Optional[Dict[str, int]] = None):
    """Every trade of one setup on these bars.

    With `rule` ("2r" or "level") a list of that rule's trades; without, a
    dict of both rules' lists. Pure: no network, no database. `skipped`, when
    given, counts the events not traded and why.
    """
    if prep is None:
        if cost_bps is None:
            cost_bps = config_mod.cost_bps_for_symbol(
                getattr(bars, "symbol", ""), getattr(bars, "asset_class", ""),
                config_mod.DEFAULTS)
        prep = prepare(bars, s, cost_bps)
    p = prep
    counts = skipped if skipped is not None else {}
    for key in ("tight_stop", "no_control", "at_close", "left_out_day", "warmup",
                "still_open"):
        counts.setdefault(key, 0)
    out: Dict[str, List[Trade]] = {r: [] for r in labstore.RULES}
    idx, dirs = events(setup, s, bars)
    windows = [w[0] for w in clock.LAB_WINDOWS]
    for i, d in zip(idx.tolist(), dirs.tolist()):
        if not p.good[i]:
            counts["left_out_day"] += 1
            continue
        last = int(p.exit_i[i])
        if last < 0:
            counts["still_open"] += 1
            continue
        if last <= i:
            counts["at_close"] += 1
            continue
        if p.roll_cum[last + 1] - p.roll_cum[i]:
            counts["left_out_day"] += 1
            continue
        plan = plan_for(setup, s, p, i, d)
        if isinstance(plan, str):
            counts[plan] += 1
            continue
        entry, stop, risk, targets = plan
        rules = list(labstore.RULES)
        prices = [targets[r][0] for r in rules]
        walked = walk(p.o, p.h, p.l, p.c, i, last, d, entry, stop, prices)
        cost_r = entry * p.cost_bps / 10000.0 / risk
        js: List[int] = []
        ctrl: List[Optional[float]] = [None, None]
        if with_controls:
            js = controls(p, setup, i)
            if not js:
                counts["no_control"] += 1
            ctrl = _control_r(p, js, i, d, risk, [abs(t - entry) for t in prices])
        for k, r in enumerate(rules):
            px, hit, at = walked[k]
            gross = d * (px - entry) / risk
            out[r].append(Trade(
                setup=setup, rule=r, i=i, exit_i=at, day=int(p.day[i]), direction=int(d),
                entry=entry, stop=stop, target=float(prices[k]), risk=risk,
                r=gross - cost_r, r_gross=gross, cost_r=cost_r, bars=at - i, hit=bool(hit),
                window=windows[int(p.window[i])], target_r=abs(prices[k] - entry) / risk,
                fallback=bool(targets[r][1]), control_r=ctrl[k], controls=tuple(js)))
    return out[rule] if rule is not None else out


# --- figures ---------------------------------------------------------------------

def clustered_se(values: Sequence[float], groups: Sequence[int]) -> Optional[float]:
    """The standard error of the mean of `values`, with values sharing a group
    (a trading day) counted together:
    sqrt(sum over groups of (sum of (value - mean))^2) / n.
    None below two values or two groups."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    if n < 2:
        return None
    _, codes = np.unique(np.asarray(groups), return_inverse=True)
    if codes.max() < 1:
        return None
    sums = np.bincount(codes, weights=v - v.mean())
    return float(math.sqrt(float(np.sum(sums * sums))) / n)


def _r(x, places: int = 6):
    return None if x is None else round(float(x), places)


def aggregate(trades: List[Trade], days_used: Sequence[int], level: bool = False) -> Dict:
    """One rule's figures, in labstore's shape."""
    n = len(trades)
    rec: Dict = {"n": n, "wins": 0, "sum_r": 0.0, "sum_r_gross": 0.0, "se": None, "days": 0,
                 "longs": 0, "avg_bars": None, "control": {"n": 0, "sum_r": 0.0},
                 "diff": None, "diff_se": None, "holdout": {"n": 0, "mean_r": None},
                 "stable": None, "by_window": {}, "green_days": [0, 0], "by_day": []}
    if level:
        rec["avg_target_r"] = None
        rec["fallback_2r"] = 0
    if not n:
        return rec
    r = np.array([t.r for t in trades], dtype=float)
    day = np.array([t.day for t in trades], dtype=np.int64)
    rec["wins"] = int(np.sum(r > 0))
    rec["sum_r"] = _r(r.sum())
    rec["sum_r_gross"] = _r(sum(t.r_gross for t in trades))
    rec["se"] = _r(clustered_se(r, day))
    uniq = np.unique(day)
    rec["days"] = int(uniq.size)
    rec["longs"] = int(sum(1 for t in trades if t.direction > 0))
    rec["avg_bars"] = _r(sum(t.bars for t in trades) / float(n), 3)
    paired = [t for t in trades if t.control_r is not None]
    if paired:
        rec["control"] = {"n": len(paired), "sum_r": _r(sum(t.control_r for t in paired))}
        diff = np.array([t.r - t.control_r for t in paired], dtype=float)
        rec["diff"] = _r(diff.mean())
        rec["diff_se"] = _r(clustered_se(diff, [t.day for t in paired]))
    # Each date's results, for adding instruments up (labstore.pooled).
    dates: Dict[int, List] = {}
    for t in trades:
        row = dates.setdefault(t.day, [t.day, 0, 0.0, 0, 0.0])
        row[1] += 1
        row[2] += t.r
        if t.control_r is not None:
            row[3] += 1
            row[4] += t.r - t.control_r
    rec["by_day"] = [[d, k, _r(total, 4), m, _r(gap, 4)]
                     for d, k, total, m, gap in (dates[d] for d in sorted(dates))]
    # The last third of the trading days, kept apart.
    used = sorted(days_used)
    if used:
        cut = used[min(len(used) - 1, (2 * len(used)) // 3)]
        late = r[day >= cut]
        early = r[day < cut]
        rec["holdout"] = {"n": int(late.size),
                          "mean_r": _r(late.mean()) if late.size else None}
        if late.size >= 10 and early.size >= 10:
            rec["stable"] = bool((early.mean() > 0) == (late.mean() > 0))
    windows: Dict[str, List] = {}
    for t in trades:
        cell = windows.setdefault(t.window, [0, 0, 0.0])
        cell[0] += 1
        cell[1] += 1 if t.r > 0 else 0
        cell[2] += t.r
    rec["by_window"] = {k: [v[0], v[1], _r(v[2])] for k, v in windows.items()}
    per_day = np.bincount(np.unique(day, return_inverse=True)[1], weights=r)
    rec["green_days"] = [int(np.sum(per_day > 0)), int(uniq.size)]
    if level:
        rec["avg_target_r"] = _r(sum(t.target_r for t in trades) / float(n), 3)
        rec["fallback_2r"] = int(sum(1 for t in trades if t.fallback))
    return rec


def example(p: Prep, trade: Trade) -> Dict:
    """About 70 bars around one trade, for the Learn pages to draw."""
    n = len(p.c)
    start = max(0, trade.i - 45)
    end = min(n, start + EXAMPLE_BARS)
    if trade.exit_i >= end:
        end = min(n, trade.exit_i + 6)
    start = max(0, min(start, end - EXAMPLE_BARS))
    sl = slice(start, end)
    return {"t": [int(x) for x in p.ts[sl]],
            "o": [_r(x) for x in p.o[sl]], "h": [_r(x) for x in p.h[sl]],
            "l": [_r(x) for x in p.l[sl]], "c": [_r(x) for x in p.c[sl]],
            "entry_i": trade.i - start, "exit_i": trade.exit_i - start,
            "entry": _r(trade.entry), "stop": _r(trade.stop), "target": _r(trade.target),
            "r": _r(trade.r, 4), "direction": int(trade.direction)}


def _group_of(symbol: str, bars) -> str:
    for sym, _, group in setups.LAB_SYMBOLS:
        if sym == symbol:
            return group
    return {"future": "index", "coin": "coin"}.get(clock.instrument_of(bars), "stock")


def measure(bars, symbol: Optional[str] = None, interval: Optional[str] = None,
            cfg: Optional[Dict] = None, group: Optional[str] = None,
            keep: Optional[Dict[str, np.ndarray]] = None):
    """Every setup on one series: (labstore record, {setup: {rule: [Trade]}}).
    keep, when given, gets CLAIM_SERIES of it, for the claims."""
    cfg = cfg if cfg is not None else config_mod.DEFAULTS
    symbol = symbol or bars.symbol
    interval = interval or bars.interval
    bps = config_mod.cost_bps_for_symbol(symbol, getattr(bars, "asset_class", ""), cfg)
    s = liquidity.series(bars)
    if keep is not None:
        keep.update({key: s[key] for key in CLAIM_SERIES})
    p = prepare(bars, s, bps, symbol, interval)
    instrument = p.instrument
    result: Dict = {
        "symbol": symbol, "interval": interval, "group": group or _group_of(symbol, bars),
        "from": int(p.ts[0]) if len(p.ts) else 0, "to": int(p.ts[-1]) if len(p.ts) else 0,
        "days": len(p.days_used), "bars": int(len(p.c)), "cost_bps": float(bps),
        "k": structure.K, "skipped_days": dict(p.skipped_days),
        "setups": {}}
    all_trades: Dict[str, Dict[str, List[Trade]]] = {}
    not_tested = {}
    for name in SETUP_NAMES:
        if setups.setup(name).intraday_only and instrument == "stock":
            not_tested[name] = ("%s trades only in the day session, so it has no Asia or "
                                "London range for New York to take." % symbol)
            continue
        skipped: Dict[str, int] = {}
        both = trades_for(bars, s, name, prep=p, skipped=skipped)
        all_trades[name] = both
        cell: Dict = {"rules": {"2r": aggregate(both["2r"], p.days_used),
                                "level": aggregate(both["level"], p.days_used, level=True)},
                      "skipped": skipped}
        if both["2r"]:
            # The latest trade whose exit fits beside it, else the latest.
            fits = [t for t in both["2r"] if t.exit_i - t.i <= EXAMPLE_BARS - 10]
            cell["example"] = example(p, (fits or both["2r"])[-1])
        result["setups"][name] = cell
    if not_tested:
        result["not_tested"] = not_tested
    # Sensitivity: the four-step model with wider swings (3 bars each side).
    s3 = liquidity.series(bars, k=SENSITIVITY_K)
    p3 = prepare(bars, s3, bps, symbol, interval)
    k3 = trades_for(bars, s3, "liquidity_model", prep=p3, with_controls=False)
    wider = {rule: aggregate(k3[rule], p3.days_used) for rule in labstore.RULES}
    result["setups"]["liquidity_model"]["k3"] = {
        rule: {key: wider[rule][key] for key in ("n", "wins", "sum_r", "se")}
        for rule in labstore.RULES}
    labstore.validate_result(result)
    return result, all_trades


# --- the run ----------------------------------------------------------------------

def _symbols(symbols) -> List[Tuple[str, str]]:
    """(symbol, group) pairs from LAB_SYMBOLS rows or plain symbols."""
    out = []
    for item in symbols:
        if isinstance(item, (tuple, list)):
            out.append((item[0], item[2] if len(item) > 2 else ""))
        else:
            sym = str(item).strip()
            group = next((g for s_, _, g in setups.LAB_SYMBOLS if s_ == sym), "")
            out.append((sym, group))
    return out


def run(conn, fetch: Callable = market.fetch_bars, cfg: Optional[Dict] = None,
        symbols: Iterable = setups.LAB_SYMBOLS, intervals: Iterable[str] = setups.LAB_INTERVALS,
        pause: float = 3.0, sleep: Callable[[float], None] = time.sleep,
        now: Optional[float] = None, with_claims: Optional[bool] = None) -> Dict:
    """Download, measure and store every (symbol, interval), then the claims.

    One failing download is recorded and the rest carry on; a refusal from the
    data source stops the downloads (asking again at once would be refused
    too). The claims (bot/claims.py) run only on a full run in which every
    download came, so they are never replaced with a part of the evidence;
    pass with_claims to choose. Returns a summary with "checked" and "errors",
    and records the run (labstore.save_run).
    """
    from . import claims as claims_mod
    started = int(now if now is not None else time.time())
    cfg = cfg if cfg is not None else dict(config_mod.DEFAULTS)
    pairs = _symbols(symbols)
    intervals = list(intervals)
    if with_claims is None:
        with_claims = (set(sym for sym, _ in pairs) == set(s[0] for s in setups.LAB_SYMBOLS)
                       and set(intervals) == set(setups.LAB_INTERVALS))
    cells: List[Dict] = []
    errors: List[str] = []
    results: List[Dict] = []
    bars_by_key: Dict[Tuple[str, str], object] = {}
    series_by_key: Dict[Tuple[str, str], Dict] = {}
    trades_5m: Dict[str, Dict] = {}
    throttled = False
    first = True
    for sym, group in pairs:
        for interval in intervals:
            if throttled:
                errors.append("%s %s: not fetched, the data source asked to wait" % (sym, interval))
                continue
            if not first and pause:
                sleep(pause)
            first = False
            try:
                bars = fetch(sym, interval)
                kept: Dict[str, np.ndarray] = {}
                res, trades = measure(bars, sym, interval, cfg, group or None, keep=kept)
                labstore.save_result(conn, res, now=now)
            except market.DataThrottled as exc:
                throttled = True
                errors.append("%s %s: %s" % (sym, interval, exc))
                continue
            except Exception as exc:          # one instrument must not stop the run
                log.warning("Setup lab: %s %s failed: %s", sym, interval, exc)
                errors.append("%s %s: %s" % (sym, interval, exc))
                continue
            results.append(res)
            cells.append({"symbol": sym, "interval": interval,
                          "trades": sum(c["rules"]["2r"]["n"] for c in res["setups"].values())})
            if with_claims:
                bars_by_key[(sym, interval)] = bars
                series_by_key[(sym, interval)] = kept
                if interval == "5m":
                    trades_5m[sym] = trades
    claimed: Dict = {"claims": [], "bias": [], "errors": []}
    if with_claims and errors:
        # The claims pool every instrument; measured on part of them they
        # would replace the whole with a part. The stored ones stay.
        errors.append("claims: left as they were, %d download(s) failed" % len(errors))
    elif with_claims and bars_by_key:
        try:
            claimed = claims_mod.run(conn, bars_by_key, fetch=fetch, trades=trades_5m,
                                     pause=pause, sleep=sleep, now=now,
                                     series=series_by_key)
        except Exception as exc:
            log.exception("Setup lab: the claims failed")
            claimed = {"claims": [], "bias": [], "errors": ["claims: %s" % exc]}
        errors.extend(claimed.get("errors") or [])
    finished = int(time.time()) if now is None else started
    summary = {"cells": cells, "errors": errors, "checked": len(cells),
               "claims": list(claimed.get("claims") or []),
               "bias": list(claimed.get("bias") or [])}
    try:
        labstore.save_run(conn, started, finished, summary)
    except Exception as exc:
        errors.append("run record: %s" % exc)
    out = dict(summary)
    out["results"] = results
    return out


def due(conn, now: Optional[float] = None) -> bool:
    """Whether the nightly run should go now.

    Yes when it never ran; else when the last run is over 20 hours old and it
    is 17:05 or later in New York (the day's bars are in by then). A run that
    stored nothing (every download refused) is tried again after two hours.
    """
    now = int(now if now is not None else time.time())
    last = labstore.last_run(conn)
    if last is None:
        return True
    age = now - int(last["started"])
    if not (last.get("summary") or {}).get("checked") and age >= RETRY_AGE:
        return True
    if age <= DUE_AGE:
        return False
    _, minute, _ = clock.ny_clock(now)
    return minute >= DUE_MINUTE


# --- the command line ---------------------------------------------------------------

def table(results: Sequence[Dict]) -> str:
    """The results as a text table: setup, rule, trades, kept per trade with
    its margin, the random entries' result, and the verdict in words."""
    lines = []
    for res in results:
        lines.append("")
        lines.append("%s %s: %d trading days, %d bars, cost %.2f bp a round trip"
                     % (res["symbol"], res["interval"], res["days"], res["bars"], res["cost_bps"]))
        lines.append("  %-30s %-5s %5s  %-18s %-9s %s"
                     % ("setup", "rule", "n", "kept R per trade", "random", "verdict"))
        for name, cell in res["setups"].items():
            for rule in labstore.RULES:
                rec = cell["rules"][rule]
                n = rec["n"]
                mean = rec["sum_r"] / n if n else None
                se = rec["se"]
                kept = ("%+.3f +/- %.3f" % (mean, 2 * se) if mean is not None and se is not None
                        else ("%+.3f" % mean if mean is not None else "-"))
                ctrl = rec["control"]
                rand = "%+.3f" % (ctrl["sum_r"] / ctrl["n"]) if ctrl["n"] else "-"
                v = labstore.verdict(mean, se, n, days=rec["days"])
                lines.append("  %-30s %-5s %5d  %-18s %-9s %s"
                             % (setups.setup(name).label[:30], rule, n, kept, rand,
                                labstore.words(v)))
        for name, why in (res.get("not_tested") or {}).items():
            lines.append("  %-30s not tested: %s" % (setups.setup(name).label[:30], why))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.setuplab",
        description="Measure the course's setups on intraday prices and store the results.")
    parser.add_argument("--refresh", action="store_true",
                        help="download prices and measure again (otherwise show what is stored)")
    parser.add_argument("--symbols", default="",
                        help="comma-separated symbols (default: the lab's eight)")
    parser.add_argument("--intervals", default="", help="comma-separated, from 5m and 15m")
    parser.add_argument("--claims", action="store_true",
                        help="also measure the claims on a partial run")
    parser.add_argument("--pause", type=float, default=3.0,
                        help="seconds between downloads")
    args = parser.parse_args(argv)
    from . import database as db_mod
    conn = db_mod.connect(None)
    try:
        if not args.refresh:
            print(table(labstore.results(conn)) or "Nothing stored yet. Run with --refresh.")
            return 0
        symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] or setups.LAB_SYMBOLS
        intervals = [x.strip() for x in args.intervals.split(",") if x.strip()] \
            or setups.LAB_INTERVALS
        cfg = config_mod.load()
        summary = run(conn, cfg=cfg, symbols=symbols, intervals=intervals, pause=args.pause,
                      with_claims=True if args.claims else None)
        print(table(summary["results"]))
        print()
        print("Stored %d result(s); claims: %s; bias records: %s"
              % (summary["checked"], ", ".join(summary["claims"]) or "none",
                 ", ".join(summary["bias"]) or "none"))
        for err in summary["errors"]:
            print("  could not: %s" % err)
        return 0 if summary["checked"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
