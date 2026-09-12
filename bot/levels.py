"""Price structure: swing pivots, support/resistance zones, session ranges.

This module answers the question the bot needs when it says "wait": *wait for
what price?* A number invented from a round-number guess is worthless. Levels
here are derived from places the market actually turned, then clustered so a
zone tested four times outranks a pivot touched once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .market import Bars


@dataclass
class Level:
    """A horizontal price zone with evidence behind it."""
    price: float
    kind: str          # "support" | "resistance"
    touches: int       # how many separate pivots formed it
    source: str        # human-readable provenance
    strength: float    # 0-1, blends touch count and recency

    def label(self) -> str:
        return "%s (%s, %d touch%s)" % (
            self.source, self.kind, self.touches, "" if self.touches == 1 else "es")


def swing_pivots(high: np.ndarray, low: np.ndarray, width: int = 3):
    """Find fractal swing highs and lows.

    A swing high at bar i is a high greater than the `width` highs on each
    side. Larger `width` yields fewer, more significant pivots.
    """
    highs: List[int] = []
    lows: List[int] = []
    n = len(high)
    for i in range(width, n - width):
        window_h = high[i - width:i + width + 1]
        window_l = low[i - width:i + width + 1]
        if high[i] == np.max(window_h) and np.argmax(window_h) == width:
            highs.append(i)
        if low[i] == np.min(window_l) and np.argmin(window_l) == width:
            lows.append(i)
    return highs, lows


def cluster_levels(prices: List[float], weights: List[float], tolerance: float):
    """Merge nearby pivot prices into zones.

    Two pivots within `tolerance` of each other are the same level as far as
    the market is concerned. Returns (centre_price, total_weight, count),
    where the centre is weighted toward the more recent touches.
    """
    if not prices:
        return []
    order = np.argsort(prices)
    ps = [prices[i] for i in order]
    ws = [weights[i] for i in order]

    clusters = []
    cur_p, cur_w = [ps[0]], [ws[0]]
    for p, w in zip(ps[1:], ws[1:]):
        if abs(p - cur_p[-1]) <= tolerance:
            cur_p.append(p)
            cur_w.append(w)
        else:
            clusters.append((cur_p, cur_w))
            cur_p, cur_w = [p], [w]
    clusters.append((cur_p, cur_w))

    out = []
    for pl, wl in clusters:
        tw = float(sum(wl)) or 1e-9
        centre = float(sum(p * w for p, w in zip(pl, wl)) / tw)
        out.append((centre, tw, len(pl)))
    return out


def session_slice(bars: Bars, sessions_back: int = 0):
    """Boolean mask for one trading session, counting back from the latest."""
    uniq = np.unique(bars.session_id)
    if sessions_back >= len(uniq):
        return None
    target = uniq[-1 - sessions_back]
    return bars.session_id == target


def opening_range(bars: Bars, minutes: int = 30):
    """High/low of the first `minutes` of the current session.

    The opening range is the reference every breakout day-trader watches. For
    crypto there is no opening bell, so the session boundary is the UTC day
    rollover, which is still where a lot of algorithmic activity resets.
    """
    mask = session_slice(bars, 0)
    if mask is None or not mask.any():
        return None
    idx = np.where(mask)[0]
    start_ts = int(bars.timestamp[idx[0]])
    window = idx[bars.timestamp[idx] < start_ts + minutes * 60]
    if len(window) < 2:
        return None
    return {
        "high": float(np.max(bars.high[window])),
        "low": float(np.min(bars.low[window])),
        "bars": int(len(window)),
        "complete": bool(len(window) < len(idx)),
    }


def prior_session(bars: Bars):
    """High, low and close of the previous completed session."""
    mask = session_slice(bars, 1)
    if mask is None or not mask.any():
        return None
    return {
        "high": float(np.max(bars.high[mask])),
        "low": float(np.min(bars.low[mask])),
        "close": float(bars.close[mask][-1]),
    }


def build_levels(bars: Bars, atr_value: float, lookback: int = 800,
                 pivot_width: int = 3, max_levels: int = 6) -> List[Level]:
    """Derive the ranked support and resistance zones around the current price.

    Recency is weighted deliberately: a level formed 600 bars ago carries less
    weight than one formed yesterday, because intraday structure decays.
    """
    n = len(bars)
    start = max(0, n - lookback)
    high = bars.high[start:]
    low = bars.low[start:]
    price = bars.last_price
    tol = max(atr_value * 0.5, price * 0.0008)

    hi_idx, lo_idx = swing_pivots(high, low, pivot_width)
    span = max(len(high), 1)

    def recency(i: int) -> float:
        # Linear decay from 0.35 (oldest) to 1.0 (newest).
        return 0.35 + 0.65 * (i / span)

    res_raw = cluster_levels([float(high[i]) for i in hi_idx],
                             [recency(i) for i in hi_idx], tol)
    sup_raw = cluster_levels([float(low[i]) for i in lo_idx],
                             [recency(i) for i in lo_idx], tol)

    levels: List[Level] = []
    max_w = max([w for _, w, _ in res_raw + sup_raw] or [1.0])

    for centre, weight, count in res_raw:
        if centre > price:
            levels.append(Level(centre, "resistance", count, "swing high",
                                min(1.0, weight / max_w)))
    for centre, weight, count in sup_raw:
        if centre < price:
            levels.append(Level(centre, "support", count, "swing low",
                                min(1.0, weight / max_w)))

    prior = prior_session(bars)
    if prior:
        for key, kind, name in (("high", "resistance", "prior session high"),
                                ("low", "support", "prior session low"),
                                ("close", "", "prior session close")):
            val = prior[key]
            k = kind or ("resistance" if val > price else "support")
            if (k == "resistance" and val > price) or (k == "support" and val < price):
                levels.append(Level(val, k, 1, name, 0.75))

    orb = opening_range(bars, 30)
    if orb:
        if orb["high"] > price:
            levels.append(Level(orb["high"], "resistance", 1, "opening range high", 0.7))
        if orb["low"] < price:
            levels.append(Level(orb["low"], "support", 1, "opening range low", 0.7))

    # Collapse duplicates that different sources produced at the same price.
    levels.sort(key=lambda x: x.price)
    merged: List[Level] = []
    for lv in levels:
        if merged and abs(lv.price - merged[-1].price) <= tol and lv.kind == merged[-1].kind:
            prev = merged[-1]
            best = prev if prev.strength >= lv.strength else lv
            merged[-1] = Level(best.price, prev.kind, prev.touches + lv.touches,
                               best.source, min(1.0, prev.strength + lv.strength * 0.3))
        else:
            merged.append(lv)

    supports = sorted([x for x in merged if x.kind == "support"],
                      key=lambda x: -x.price)[:max_levels]
    resistances = sorted([x for x in merged if x.kind == "resistance"],
                         key=lambda x: x.price)[:max_levels]
    return supports + resistances


def nearest_support(levels: List[Level], price: float) -> Optional[Level]:
    below = [x for x in levels if x.kind == "support" and x.price < price]
    return max(below, key=lambda x: x.price) if below else None


def nearest_resistance(levels: List[Level], price: float) -> Optional[Level]:
    above = [x for x in levels if x.kind == "resistance" and x.price > price]
    return min(above, key=lambda x: x.price) if above else None
