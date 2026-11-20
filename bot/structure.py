"""Price structure as the course draws it, as series a back-test can replay.

TJR's video ("How To Start Day Trading In 2026") reads a chart through a few
shapes: swing highs and lows, the trend they make, a close that breaks the
last swing (a break of structure), a run past an old high or low that held
before (a sweep), highs or lows at the same price (equal highs and lows),
three-candle gaps (fair value gaps), a close through one of them (an inverse
gap) and the halfway line of the last move (the 50% line). Pages, alerts, the
strategy library and the Setup lab all need one exact meaning of each, so it
is written once, here.

Every value at bar i is the state after bar i closed and depends on bars 0..i
only: series(bars cut after bar i) gives the same value at i as series(bars).
tests/test_structure.py checks that for every key. A swing point is only known
k bars after it, and is used from then on, never from the bar it formed on.

Each rule is marked in the code: "video:" with the timestamp where the course
teaches it, or "app choice:" where the video leaves a detail open and this
module had to pick one. The choices are the app's, not the course's.

One pass over the bars, with short sorted lists for the levels not yet taken
(bisect), so a long intraday history costs well under a second.
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Dict, List, Tuple

import numpy as np

from . import indicators as ind

# app choice: a swing needs 2 bars each side. The video's two-candle rule
# (1:01:21-1:12:00) marks every wiggle on a 1-minute chart; two each side keeps
# the swings a person would draw on 5-minute bars and up.
K = 2
# app choice: equal highs or lows lie within 0.1 ATR(14) of each other (video
# 2:20:42-2:23:53: "exact or just under", about two ticks on ES 5-minute bars).
EQUAL_TOL_ATR = 0.10
# app choice: a gap older than 150 bars is dropped.
GAP_MAX_AGE = 150
# app choice: only swings from the last 300 bars can be taken.
SWEEP_LOOKBACK = 300

NAN = float("nan")

# Series keys by kind. Events are int8 (+1 means "expects up", -1 down, 0
# nothing); bar positions are int64 with -1 for none; prices are float64 with
# NaN for none.
EVENT_KEYS = ("st_trend", "st_flip", "st_ext", "st_sweep", "st_sweep_both",
              "st_sweep_back", "st_sweep_eq", "st_fvg_new", "st_fvg_touch",
              "st_ifvg", "st_eq_touch")
COUNT_KEYS = ("st_sweep_n",)
INDEX_KEYS = ("st_hi_at", "st_lo_at", "st_fvg_up_at", "st_fvg_dn_at", "st_trend_at")
PRICE_KEYS = ("st_hi", "st_lo", "st_above1", "st_above2", "st_above3",
              "st_below1", "st_below2", "st_below3", "st_ref_hi", "st_ref_lo",
              "st_sweep_lvl", "st_sweep_far", "st_eqh", "st_eql",
              "st_fvg_up_top", "st_fvg_up_bot", "st_fvg_up_stack_bot",
              "st_fvg_dn_top", "st_fvg_dn_bot", "st_fvg_dn_stack_top",
              "st_ifvg_lvl", "st_eq", "st_leg_lo", "st_leg_hi", "st_atr")
KEYS = EVENT_KEYS + COUNT_KEYS + INDEX_KEYS + PRICE_KEYS


def pivots(high, low, k: int = K) -> Tuple[List[int], List[int]]:
    """Swing highs and lows, as bar positions.

    A swing high at p is a high above each of the k highs before it and at
    least as high as each of the k after it (levels.swing_pivots' rule: of
    equal highs side by side the first is the swing). Mirror for lows. It is
    known at p + k, once the k bars after it have closed.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = len(high)
    if k < 1 or n < 2 * k + 1:
        return [], []
    mid_h, mid_l = high[k:n - k], low[k:n - k]
    is_hi = np.ones(n - 2 * k, dtype=bool)
    is_lo = np.ones(n - 2 * k, dtype=bool)
    for j in range(1, k + 1):
        # video 1:01:21-1:12:00: a swing high has lower highs either side of it.
        is_hi &= mid_h > high[k - j:n - k - j]
        is_hi &= mid_h >= high[k + j:n - k + j]
        is_lo &= mid_l < low[k - j:n - k - j]
        is_lo &= mid_l <= low[k + j:n - k + j]
    return ((np.flatnonzero(is_hi) + k).tolist(), (np.flatnonzero(is_lo) + k).tolist())


def empty(n: int) -> Dict[str, np.ndarray]:
    """Every key, for n bars, with nothing in it."""
    out: Dict[str, np.ndarray] = {}
    for key in EVENT_KEYS:
        out[key] = np.zeros(n, dtype=np.int8)
    for key in COUNT_KEYS:
        out[key] = np.zeros(n, dtype=np.int16)
    for key in INDEX_KEYS:
        out[key] = np.full(n, -1, dtype=np.int64)
    for key in PRICE_KEYS:
        out[key] = np.full(n, np.nan, dtype=float)
    return out


class _Levels:
    """Swing highs (or lows) not yet traded through, sorted by price.

    Each level carries the cluster it belongs to; a cluster of two or more is
    a set of equal highs (or lows). Untaken highs never rise from one to the
    next in time (a later, higher one would have traded through the earlier
    one), so the earliest member is a cluster's highest.
    """

    __slots__ = ("highs", "prices", "items", "alive", "order", "clusters", "_next")

    def __init__(self, highs: bool):
        self.highs = highs
        self.prices: List[float] = []          # ascending
        self.items: List[list] = []            # [price, p, cluster id], same order
        self.alive: Dict[int, list] = {}       # p -> item
        self.order = deque()                   # p in confirmation order
        self.clusters: Dict[int, List[list]] = {}
        self._next = 0

    def add(self, price: float, p: int, tol: float) -> None:
        # video 2:20:42-2:23:53: two highs at (nearly) the same price are equal
        # highs. The nearest untaken high at or above this one is the one it
        # could be equal to; app choice: it joins that one's cluster when the
        # cluster's own price (its highest) is within tol, the ATR-based
        # tolerance at this swing's confirmation.
        cid = None
        if self.highs:
            j = bisect.bisect_left(self.prices, price)
            if tol == tol and j < len(self.items):
                other = self.items[j][2]
                if self.top(other) - price <= tol:
                    cid = other
        else:
            j = bisect.bisect_right(self.prices, price)
            if tol == tol and j > 0:
                other = self.items[j - 1][2]
                if price - self.top(other) <= tol:
                    cid = other
        if cid is None:
            cid = self._next
            self._next += 1
            self.clusters[cid] = []
        item = [price, p, cid]
        self.clusters[cid].append(item)
        self.prices.insert(j, price)
        self.items.insert(j, item)
        self.alive[p] = item
        self.order.append(p)

    def top(self, cid: int) -> float:
        """A cluster's price: its highest member for highs, lowest for lows."""
        members = self.clusters[cid]
        return (max(m[0] for m in members) if self.highs
                else min(m[0] for m in members))

    def _forget(self, item: list) -> None:
        del self.alive[item[1]]
        members = self.clusters[item[2]]
        members.remove(item)
        if not members:
            del self.clusters[item[2]]

    def age(self, cutoff: int) -> None:
        """Drop levels whose swing formed before `cutoff`."""
        order, alive = self.order, self.alive
        while order and order[0] < cutoff:
            p = order.popleft()
            item = alive.get(p)
            if item is None:
                continue                      # already taken
            j = bisect.bisect_left(self.prices, item[0])
            while self.items[j] is not item:
                j += 1
            del self.prices[j]
            del self.items[j]
            self._forget(item)

    def take(self, extreme: float):
        """Remove and return every level this bar's high (or low) went past.

        Returns (taken items in price order, whether one was part of equal
        highs or lows). Past means strictly: a high equal to a level has not
        traded beyond it.
        """
        prices = self.prices
        if not prices:
            return None, False
        if self.highs:
            if extreme <= prices[0]:
                return None, False
            j = bisect.bisect_left(prices, extreme)
            taken = self.items[:j]
            del self.items[:j]
            del prices[:j]
        else:
            if extreme >= prices[-1]:
                return None, False
            j = bisect.bisect_right(prices, extreme)
            taken = self.items[j:]
            del self.items[j:]
            del prices[j:]
        equal = any(len(self.clusters[it[2]]) >= 2 for it in taken)
        for it in taken:
            self._forget(it)
        return taken, equal

    def nearest(self, close: float, count: int = 3) -> List[float]:
        """Up to `count` levels beyond the close, nearest first."""
        if self.highs:
            j = bisect.bisect_right(self.prices, close)
            return self.prices[j:j + count]
        j = bisect.bisect_left(self.prices, close)
        return self.prices[max(0, j - count):j][::-1]

    def nearest_cluster(self, close: float) -> float:
        """The price of the nearest equal highs (or lows) beyond the close."""
        if self.highs:
            j = bisect.bisect_right(self.prices, close)
            for it in self.items[j:]:
                if len(self.clusters[it[2]]) >= 2:
                    return self.top(it[2])
        else:
            j = bisect.bisect_left(self.prices, close)
            for it in reversed(self.items[:j]):
                if len(self.clusters[it[2]]) >= 2:
                    return self.top(it[2])
        return NAN


def series(bars, k: int = K) -> Dict[str, np.ndarray]:
    """Every structure series for these bars, as of each bar's close.

    Keys (all len(bars)):
      st_hi, st_hi_at, st_lo, st_lo_at   latest confirmed swing high and low
      st_above1..3, st_below1..3         nearest untaken swing highs above the
                                         close and lows below it
      st_trend (+1/-1/0), st_trend_at    trend, and the bar it began on
      st_flip                            +1/-1 on a bar that turned the trend
      st_ext                             +1/-1 on a close past the latest swing
                                         in the trend's own direction
      st_ref_hi, st_ref_lo               the level whose close would flip it
      st_sweep (+1 lows taken, -1 highs), st_sweep_both, st_sweep_lvl (nearest
      level taken), st_sweep_far, st_sweep_n, st_sweep_back, st_sweep_eq
      st_eqh, st_eql                     nearest equal highs above, lows below
      st_fvg_up_top/bot/stack_bot/at, st_fvg_dn_top/bot/stack_top/at
                                         the most recent live gap each way
      st_fvg_new, st_fvg_touch, st_ifvg  gap events
      st_eq, st_leg_lo, st_leg_hi, st_eq_touch
                                         the 50% line of the last move
      st_atr                             ATR(14), the size every tolerance uses
    """
    o = np.asarray(bars.open, dtype=float)
    h = np.asarray(bars.high, dtype=float)
    l_ = np.asarray(bars.low, dtype=float)
    c = np.asarray(bars.close, dtype=float)
    n = len(c)
    out = empty(n)
    if n == 0:
        return out

    atr = ind.atr(h, l_, c, 14)
    out["st_atr"] = atr.astype(float)
    hi_idx, lo_idx = pivots(h, l_, k)
    is_ph = [False] * n
    is_pl = [False] * n
    for p in hi_idx:
        is_ph[p] = True
    for p in lo_idx:
        is_pl[p] = True
    O, H, L, C, A = o.tolist(), h.tolist(), l_.tolist(), c.tolist(), atr.tolist()
    # The last down (and up) candle at or before each bar, for gap stacks.
    last_dn = [-1] * n
    last_up = [-1] * n
    ld = lu = -1
    for i in range(n):
        if C[i] < O[i]:
            ld = i
        elif C[i] > O[i]:
            lu = i
        last_dn[i], last_up[i] = ld, lu

    # Output columns as plain lists; written to arrays once at the end.
    cols = {key: [0] * n for key in EVENT_KEYS + COUNT_KEYS}
    cols.update({key: [-1] * n for key in INDEX_KEYS})
    cols.update({key: [NAN] * n for key in PRICE_KEYS if key != "st_atr"})
    o_hi, o_hi_at, o_lo, o_lo_at = cols["st_hi"], cols["st_hi_at"], cols["st_lo"], cols["st_lo_at"]
    o_a1, o_a2, o_a3 = cols["st_above1"], cols["st_above2"], cols["st_above3"]
    o_b1, o_b2, o_b3 = cols["st_below1"], cols["st_below2"], cols["st_below3"]
    o_trend, o_trend_at, o_flip, o_ext = (cols["st_trend"], cols["st_trend_at"],
                                          cols["st_flip"], cols["st_ext"])
    o_rhi, o_rlo = cols["st_ref_hi"], cols["st_ref_lo"]
    o_sw, o_swb, o_swl, o_swf, o_swn, o_swback, o_sweq = (
        cols["st_sweep"], cols["st_sweep_both"], cols["st_sweep_lvl"], cols["st_sweep_far"],
        cols["st_sweep_n"], cols["st_sweep_back"], cols["st_sweep_eq"])
    o_eqh, o_eql = cols["st_eqh"], cols["st_eql"]
    o_ut, o_ub, o_us, o_ua = (cols["st_fvg_up_top"], cols["st_fvg_up_bot"],
                              cols["st_fvg_up_stack_bot"], cols["st_fvg_up_at"])
    o_dt, o_db, o_ds, o_da = (cols["st_fvg_dn_top"], cols["st_fvg_dn_bot"],
                              cols["st_fvg_dn_stack_top"], cols["st_fvg_dn_at"])
    o_fnew, o_ftouch, o_ifvg = cols["st_fvg_new"], cols["st_fvg_touch"], cols["st_ifvg"]
    o_ifl = cols["st_ifvg_lvl"]
    o_eq, o_llo, o_lhi, o_eqt = cols["st_eq"], cols["st_leg_lo"], cols["st_leg_hi"], cols["st_eq_touch"]

    highs, lows = _Levels(True), _Levels(False)

    sw_hi = sw_lo = NAN
    sw_hi_at = sw_lo_at = -1
    trend, trend_at = 0, -1
    ref_hi = ref_lo = NAN
    ext_hi_used = ext_lo_used = -1

    # Gaps: [c, top, bottom, stack id, touched, expiry level]. Bullish zone is
    # [high[c-2], low[c]]; bearish [high[c], low[c-2]].
    up_gaps: List[list] = []
    dn_gaps: List[list] = []
    stack_id = 0
    gov_up = gov_dn = None                     # as of the previous bar
    inv_up = inv_dn = NAN                      # inverse-gap levels, as of the previous bar

    # The two legs for the 50% line: up from the latest swing low, down from
    # the latest swing high. [anchor, anchor bar, extreme, extreme bar, locked]
    up_leg = [NAN, -1, NAN, -1, False]
    dn_leg = [NAN, -1, NAN, -1, False]
    eq_prev = lo_prev = hi_prev = NAN
    key_prev = None
    touched_key = None

    lookback = SWEEP_LOOKBACK
    for i in range(n):
        hi_i, lo_i, cl_i, op_i = H[i], L[i], C[i], O[i]
        trend_prev = trend

        # --- the 50% line touched (state as of the bar before) -------------
        # video 3:58:38-4:13:16: in a rise, buy in the cheap half; the line is
        # the halfway point of the last move. app choice: one touch per leg,
        # the first bar that reaches the line.
        if eq_prev == eq_prev and key_prev is not None and key_prev != touched_key:
            if trend_prev == 1 and lo_i <= eq_prev and cl_i > lo_prev:
                o_eqt[i] = 1
                touched_key = key_prev
            elif trend_prev == -1 and hi_i >= eq_prev and cl_i < hi_prev:
                o_eqt[i] = -1
                touched_key = key_prev

        # --- gaps touched, and closed through (state as of the bar before) --
        touch_up = touch_dn = 0
        if gov_up is not None and not gov_up[4] and lo_i <= gov_up[1]:
            # video 2:51:15-2:58:34: price comes back into the gap. It counts
            # unless the bar closes below the gap's bottom (a wick may go through).
            touch_up = 1 if cl_i >= gov_up[2] else 0
        if gov_dn is not None and not gov_dn[4] and hi_i >= gov_dn[2]:
            touch_dn = -1 if cl_i <= gov_dn[1] else 0
        if touch_up and touch_dn:
            o_ftouch[i] = trend_prev if trend_prev != 0 else 0
        else:
            o_ftouch[i] = touch_up or touch_dn
        # video 3:42:47-3:58:38: a close through the bottom of the rise's gaps
        # (the bottom of a stack) is an inverse gap.
        if trend_prev == 1 and inv_up == inv_up and cl_i < inv_up:
            o_ifvg[i], o_ifl[i] = -1, inv_up
        elif trend_prev == -1 and inv_dn == inv_dn and cl_i > inv_dn:
            o_ifvg[i], o_ifl[i] = 1, inv_dn

        # --- levels older than the lookback can no longer be taken ----------
        if i > lookback:
            highs.age(i - lookback)
            lows.age(i - lookback)

        # --- sweeps: untaken swings this bar traded past ---------------------
        # video 1:36:14-1:44:37: an old high or low that held is where stops
        # wait; trading past it takes them. Levels confirmed by the bar
        # before only (one confirmed on this bar cannot be past: see pivots).
        took_lo, eq_lo = lows.take(lo_i)
        took_hi, eq_hi = highs.take(hi_i)
        if took_lo and took_hi:
            o_swb[i] = 1
        elif took_lo:
            o_sw[i] = 1
            near = took_lo[-1][0]
            o_swl[i], o_swf[i], o_swn[i] = near, took_lo[0][0], len(took_lo)
            o_swback[i] = 1 if cl_i > near else 0
            o_sweq[i] = 1 if eq_lo else 0
        elif took_hi:
            o_sw[i] = -1
            near = took_hi[0][0]
            o_swl[i], o_swf[i], o_swn[i] = near, took_hi[-1][0], len(took_hi)
            o_swback[i] = 1 if cl_i < near else 0
            o_sweq[i] = 1 if eq_hi else 0

        # --- the legs for the 50% line, moved by this bar ---------------------
        if up_leg[0] == up_leg[0]:
            if hi_i > up_leg[2]:
                up_leg[2], up_leg[3], up_leg[4] = hi_i, i, False
            elif not up_leg[4] and cl_i < op_i:
                up_leg[4] = True            # locked on the first down candle after the high
        if dn_leg[0] == dn_leg[0]:
            if lo_i < dn_leg[2]:
                dn_leg[2], dn_leg[3], dn_leg[4] = lo_i, i, False
            elif not dn_leg[4] and cl_i > op_i:
                dn_leg[4] = True

        # --- swings confirmed on this bar (before the close test) -----------
        p = i - k
        if p >= k:
            atr_i = A[i]
            tol = EQUAL_TOL_ATR * atr_i if atr_i == atr_i else NAN
            if is_ph[p]:
                price = H[p]
                sw_hi, sw_hi_at = price, p
                if trend == -1:
                    ref_hi = price           # video 2:33:23: the latest swing high, even a higher one
                highs.add(price, p, tol)
                for g in up_gaps:
                    # video 3:08:00-3:15:21: delete a used or skipped gap. app
                    # choice: a bullish gap ends at a close above a swing high
                    # confirmed after it formed.
                    if price < g[5]:
                        g[5] = price
                seg = L[p:i + 1]
                m = min(seg)
                at = p + seg.index(m)
                dn_leg[:] = [price, p, m, at, any(C[j] > O[j] for j in range(at + 1, i + 1))]
            if is_pl[p]:
                price = L[p]
                sw_lo, sw_lo_at = price, p
                if trend == 1:
                    ref_lo = price
                lows.add(price, p, tol)
                for g in dn_gaps:
                    if price > g[5]:
                        g[5] = price
                seg = H[p:i + 1]
                m = max(seg)
                at = p + seg.index(m)
                up_leg[:] = [price, p, m, at, any(C[j] < O[j] for j in range(at + 1, i + 1))]

        # --- the trend: only a close past the last swing changes it ---------
        # video 2:33:23-2:43:56: a wick past the swing, or a close exactly on
        # it, is not a break; a close beyond it is.
        if trend == 0:
            up = sw_hi == sw_hi and cl_i > sw_hi
            dn = sw_lo == sw_lo and cl_i < sw_lo
            if up and not dn:
                trend, trend_at, ref_lo, ref_hi = 1, i, sw_lo, NAN
                ext_hi_used = sw_hi_at
            elif dn and not up:
                trend, trend_at, ref_hi, ref_lo = -1, i, sw_hi, NAN
                ext_lo_used = sw_lo_at
        elif trend == 1:
            if ref_lo == ref_lo and cl_i < ref_lo:
                o_flip[i] = -1
                trend, trend_at, ref_hi, ref_lo = -1, i, sw_hi, NAN
                ext_lo_used = sw_lo_at
            elif sw_hi == sw_hi and cl_i > sw_hi and sw_hi_at != ext_hi_used:
                o_ext[i] = 1                 # a higher high: the trend carries on
                ext_hi_used = sw_hi_at
        else:
            if ref_hi == ref_hi and cl_i > ref_hi:
                o_flip[i] = 1
                trend, trend_at, ref_lo, ref_hi = 1, i, sw_lo, NAN
                ext_hi_used = sw_hi_at
            elif sw_lo == sw_lo and cl_i < sw_lo and sw_lo_at != ext_lo_used:
                o_ext[i] = -1
                ext_lo_used = sw_lo_at

        # --- gaps: touched, closed through, used or too old -----------------
        if up_gaps:
            keep = []
            for g in up_gaps:
                if not g[4] and lo_i <= g[1]:
                    g[4] = True
                # video 3:08:00: a close through the gap ends it; a wick does not.
                if cl_i < g[2] or cl_i > g[5] or i - g[0] > GAP_MAX_AGE:
                    continue
                keep.append(g)
            up_gaps = keep
        if dn_gaps:
            keep = []
            for g in dn_gaps:
                if not g[4] and hi_i >= g[2]:
                    g[4] = True
                if cl_i > g[1] or cl_i < g[5] or i - g[0] > GAP_MAX_AGE:
                    continue
                keep.append(g)
            dn_gaps = keep

        # --- a new gap on this bar ---------------------------------------------
        # video 2:46:04-3:02:46: three candles, the middle one moving, the
        # first's high below the third's low. app choice: the colours of the
        # first and third candles do not matter, and any size counts.
        if i >= 2:
            if lo_i > H[i - 2] and C[i - 1] > O[i - 1]:
                prev = up_gaps[-1] if up_gaps else None
                # video 3:02:46: gaps one after another with no down candle
                # between their middle candles are one stack.
                if prev is not None and last_dn[i - 2] < prev[0]:
                    sid = prev[3]
                else:
                    stack_id += 1
                    sid = stack_id
                up_gaps.append([i, lo_i, H[i - 2], sid, False, math.inf])
                o_fnew[i] = 1
            elif hi_i < L[i - 2] and C[i - 1] < O[i - 1]:
                prev = dn_gaps[-1] if dn_gaps else None
                if prev is not None and last_up[i - 2] < prev[0]:
                    sid = prev[3]
                else:
                    stack_id += 1
                    sid = stack_id
                dn_gaps.append([i, L[i - 2], hi_i, sid, False, -math.inf])
                o_fnew[i] = -1

        # --- record the state after this bar ----------------------------------
        o_trend[i], o_trend_at[i] = trend, trend_at
        o_hi[i], o_hi_at[i], o_lo[i], o_lo_at[i] = sw_hi, sw_hi_at, sw_lo, sw_lo_at
        if trend == 1:
            o_rlo[i] = ref_lo
        elif trend == -1:
            o_rhi[i] = ref_hi

        near_hi = highs.nearest(cl_i)
        near_lo = lows.nearest(cl_i)
        if near_hi:
            o_a1[i] = near_hi[0]
            if len(near_hi) > 1:
                o_a2[i] = near_hi[1]
                if len(near_hi) > 2:
                    o_a3[i] = near_hi[2]
        if near_lo:
            o_b1[i] = near_lo[0]
            if len(near_lo) > 1:
                o_b2[i] = near_lo[1]
                if len(near_lo) > 2:
                    o_b3[i] = near_lo[2]
        if highs.clusters and len(highs.clusters) < len(highs.items):
            o_eqh[i] = highs.nearest_cluster(cl_i)
        if lows.clusters and len(lows.clusters) < len(lows.items):
            o_eql[i] = lows.nearest_cluster(cl_i)

        gov_up = up_gaps[-1] if up_gaps else None
        gov_dn = dn_gaps[-1] if dn_gaps else None
        inv_up = inv_dn = NAN
        if gov_up is not None:
            sid = gov_up[3]
            o_ut[i], o_ub[i], o_ua[i] = gov_up[1], gov_up[2], gov_up[0]
            o_us[i] = min(g[2] for g in up_gaps if g[3] == sid)
            # video 3:42:47: only gaps the current rise made count, and only
            # the bottom of their stack.
            if trend == 1 and gov_up[0] >= trend_at:
                inv_up = min(g[2] for g in up_gaps if g[3] == sid and g[0] >= trend_at)
        if gov_dn is not None:
            sid = gov_dn[3]
            o_dt[i], o_db[i], o_da[i] = gov_dn[1], gov_dn[2], gov_dn[0]
            o_ds[i] = max(g[1] for g in dn_gaps if g[3] == sid)
            if trend == -1 and gov_dn[0] >= trend_at:
                inv_dn = max(g[1] for g in dn_gaps if g[3] == sid and g[0] >= trend_at)

        # The 50% line: video 3:58:38-4:13:16. The leg runs from the latest
        # swing low to the highest high since it (mirror down), and the line is
        # fixed once a candle closes against the move.
        eq_prev = lo_prev = hi_prev = NAN
        key_prev = None
        if trend == 1 and up_leg[0] == up_leg[0]:
            lo_prev, hi_prev = up_leg[0], up_leg[2]
            o_llo[i], o_lhi[i] = lo_prev, hi_prev
            if up_leg[4]:
                eq_prev = (lo_prev + hi_prev) / 2.0
                o_eq[i] = eq_prev
                key_prev = (1, up_leg[1], up_leg[3])
        elif trend == -1 and dn_leg[0] == dn_leg[0]:
            hi_prev, lo_prev = dn_leg[0], dn_leg[2]
            o_llo[i], o_lhi[i] = lo_prev, hi_prev
            if dn_leg[4]:
                eq_prev = (lo_prev + hi_prev) / 2.0
                o_eq[i] = eq_prev
                key_prev = (-1, dn_leg[1], dn_leg[3])

    for key in EVENT_KEYS:
        out[key] = np.asarray(cols[key], dtype=np.int8)
    for key in COUNT_KEYS:
        out[key] = np.asarray(cols[key], dtype=np.int16)
    for key in INDEX_KEYS:
        out[key] = np.asarray(cols[key], dtype=np.int64)
    for key in PRICE_KEYS:
        if key != "st_atr":
            out[key] = np.asarray(cols[key], dtype=float)
    return out
