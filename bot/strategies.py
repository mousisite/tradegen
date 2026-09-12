"""The strategy library.

Roughly thirty independent strategies, grouped into families. Each reads the
same precomputed indicator context and returns a directional score in
[-1, +1] plus a plain-language reading.

Two design rules run through the whole module:

1. **Every strategy is parameterised by bar index `i`**, never "the latest
   value". The backtester replays these exact functions over history, so the
   win rate it measures belongs to the same logic that produces the live call.

2. **Family membership is tracked.** Six correlated trend strategies all firing
   is one piece of evidence, not six. The composite discounts within-family
   agreement so a crowded family cannot drown out the rest of the evidence.

Theoretical regime hints on each strategy are only priors. Actual weighting
comes from measured per-instrument performance in `calibrate.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import indicators as ind
from .market import Bars

# Families. Used to discount correlated agreement inside the composite.
TREND = "trend"
REVERSION = "mean_reversion"
BREAKOUT = "breakout"
VOLUME = "volume"
STRUCTURE = "structure"
PATTERN = "pattern"

TRENDING = "trending"
RANGING = "ranging"
TRANSITIONAL = "transitional"


@dataclass
class Signal:
    """One strategy's opinion at one bar."""
    name: str
    score: float          # -1 (strong short) .. +1 (strong long)
    weight: float         # final importance after all adjustments
    reason: str
    family: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass
class Strategy:
    """A strategy plus the metadata the router and the report need."""
    name: str
    family: str
    fn: Callable
    description: str
    suits: Tuple[str, ...] = ()      # regimes it is theorised to suit
    base_weight: float = 1.0


@dataclass
class Regime:
    """Market state on two independent axes."""
    trend: str                # trending | ranging | transitional
    volatility: str           # high | normal | low
    direction: int            # +1 up, -1 down, 0 sideways
    adx: float
    detail: str = ""

    @property
    def key(self) -> str:
        """Bucket key used for per-regime performance statistics."""
        return self.trend

    def describe(self) -> str:
        arrow = {1: "up", -1: "down", 0: "sideways"}[self.direction]
        return "%s / %s volatility / %s" % (self.trend, self.volatility, arrow)


class Context:
    """Every indicator series for one instrument, computed once and reused.

    Series live in a dict so the library can grow without touching a schema.
    The handful used outside this module keep named properties.
    """

    __slots__ = ("bars", "s", "_warmup", "_session_cache", "vwap_is_session")

    def __init__(self, bars: Bars, series: Dict[str, np.ndarray], warmup: int,
                 vwap_is_session: bool = True):
        self.bars = bars
        self.s = series
        self._warmup = warmup
        # False when the interval is too coarse for session VWAP to mean
        # anything, in which case a trailing-window VWAP was used instead.
        self.vwap_is_session = vwap_is_session
        # Prior-session statistics are reused across every bar in a session and
        # recomputed on every backtest step otherwise, so they are cached.
        self._session_cache: Dict[int, Optional[Tuple[float, float, float]]] = {}

    def prior_session_hlc(self, i: int):
        """High, low and close of the session before bar i, or None.

        Cached per session id because the backtester asks for this on every
        bar, and scanning the whole history each time is wasteful.
        """
        sess = int(self.bars.session_id[i])
        if sess in self._session_cache:
            return self._session_cache[sess]

        ids = self.bars.session_id[:i + 1]
        uniq = np.unique(ids)
        result = None
        if len(uniq) >= 2:
            target = uniq[-2] if uniq[-1] == sess else uniq[-1]
            mask = self.bars.session_id == target
            if mask.any():
                result = (float(np.max(self.bars.high[mask])),
                          float(np.min(self.bars.low[mask])),
                          float(self.bars.close[mask][-1]))
        self._session_cache[sess] = result
        return result

    @property
    def warmup(self) -> int:
        return self._warmup

    def at(self, key: str, i: int) -> float:
        """Value of a series at bar i, or NaN when unavailable."""
        arr = self.s.get(key)
        if arr is None or i < 0 or i >= len(arr):
            return float("nan")
        return float(arr[i])

    # Series used by decision.py and report.py.
    @property
    def atr(self):
        return self.s["atr"]

    @property
    def vwap(self):
        return self.s["vwap"]

    @property
    def ema21(self):
        return self.s["ema21"]

    @property
    def adx(self):
        return self.s["adx"]


def build_context(bars: Bars) -> Context:
    """Compute every indicator series once for the whole history."""
    o, h, l, c, v = bars.open, bars.high, bars.low, bars.close, bars.volume
    s: Dict[str, np.ndarray] = {}

    s["ema9"] = ind.ema(c, 9)
    s["ema21"] = ind.ema(c, 21)
    s["ema50"] = ind.ema(c, 50)
    s["ema200"] = ind.ema(c, 200)
    s["ema21_slope"] = ind.slope_pct(s["ema21"], 5)

    macd_line, macd_signal, macd_hist = ind.macd(c)
    s["macd"] = macd_line
    s["macd_signal"] = macd_signal
    s["macd_hist"] = macd_hist

    s["rsi"] = ind.rsi(c, 14)
    s["atr"] = ind.atr(h, l, c, 14)
    s["cci"] = ind.cci(h, l, c, 20)
    s["roc"] = ind.roc(c, 10)
    s["cmo"] = ind.cmo(c, 14)
    s["tsi"] = ind.tsi(c)
    s["zscore"] = ind.zscore(c, 20)
    s["willr"] = ind.williams_r(h, l, c, 14)
    s["hv"] = ind.historical_volatility(c, 20)

    k, d = ind.stochastic(h, l, c, 14, 3)
    s["stoch_k"], s["stoch_d"] = k, d

    adx_v, pdi, mdi = ind.adx(h, l, c, 14)
    s["adx"], s["plus_di"], s["minus_di"] = adx_v, pdi, mdi

    au, ad_ = ind.aroon(h, l, 25)
    s["aroon_up"], s["aroon_dn"] = au, ad_

    bb_u, bb_m, bb_l, pct_b, bw = ind.bollinger(c, 20, 2.0)
    s["bb_upper"], s["bb_mid"], s["bb_lower"] = bb_u, bb_m, bb_l
    s["pct_b"], s["bandwidth"] = pct_b, bw

    kc_u, kc_m, kc_l = ind.keltner(h, l, c, 20, 1.5)
    s["kc_upper"], s["kc_mid"], s["kc_lower"] = kc_u, kc_m, kc_l

    dc_u, dc_m, dc_l = ind.donchian(h, l, 20)
    s["dc_upper"], s["dc_mid"], s["dc_lower"] = dc_u, dc_m, dc_l

    st_line, st_dir = ind.supertrend(h, l, c, 10, 3.0)
    s["supertrend"], s["supertrend_dir"] = st_line, st_dir

    sar, sar_dir = ind.parabolic_sar(h, l)
    s["sar"], s["sar_dir"] = sar, sar_dir

    tk, kj, sa, sb = ind.ichimoku(h, l)
    s["tenkan"], s["kijun"], s["senkou_a"], s["senkou_b"] = tk, kj, sa, sb

    lr_slope, lr_r2 = ind.linreg(c, 20)
    s["linreg_slope"], s["linreg_r2"] = lr_slope, lr_r2

    # Session VWAP only means something when a session holds many bars. At
    # daily resolution every bar is its own session, so the average collapses
    # onto that bar's own typical price and the bands collapse to a constant.
    # Switch to a trailing-window VWAP there, which is what traders use on
    # those charts anyway.
    sessions = len(np.unique(bars.session_id))
    vwap_is_session = (len(c) / max(sessions, 1)) >= 3.0
    if vwap_is_session:
        vw, vb_u, vb_l = ind.vwap_bands(h, l, c, v, bars.session_id, 1.0)
    else:
        vw, vb_u, vb_l = ind.rolling_vwap(h, l, c, v, 20)
    s["vwap"], s["vwap_upper"], s["vwap_lower"] = vw, vb_u, vb_l

    s["rvol"] = ind.relative_volume(v, 20)
    s["obv"] = ind.obv(c, v)
    s["obv_slope"] = ind.slope_pct(s["obv"], 10)
    s["mfi"] = ind.money_flow_index(h, l, c, v, 14)
    s["ad"] = ind.accum_dist(h, l, c, v)
    s["ad_slope"] = ind.slope_pct(s["ad"], 10)

    body, upper_wick, lower_wick = ind.body_and_wicks(o, h, l, c)
    s["body"], s["upper_wick"], s["lower_wick"] = body, upper_wick, lower_wick

    # Bandwidth percentile turns "is the band narrow?" into something
    # comparable across instruments: a squeeze is relative to that symbol's
    # own recent history, not to an absolute number.
    n = len(c)
    bw_pct = np.full(n, np.nan)
    win = 120
    for i in range(win, n):
        window = bw[i - win:i + 1]
        window = window[np.isfinite(window)]
        if len(window) > 10 and np.isfinite(bw[i]):
            bw_pct[i] = float((window < bw[i]).sum()) / len(window)
    s["bw_pctile"] = bw_pct

    return Context(bars, s, warmup=210, vwap_is_session=vwap_is_session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(*vals) -> bool:
    """True when every value is a usable number."""
    for v in vals:
        if v is None:
            return False
        try:
            if math.isnan(float(v)) or math.isinf(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


def _idle(name: str, family: str, why: str = "not enough history") -> Signal:
    """A strategy with nothing to say. Zero weight keeps it out of the average."""
    return Signal(name, 0.0, 0.0, why, family)


# ---------------------------------------------------------------------------
# Trend family
# ---------------------------------------------------------------------------

def trend_stack(ctx: Context, i: int) -> Signal:
    """EMA alignment plus slope. The bread-and-butter intraday trend read."""
    e9, e21, e50 = ctx.at("ema9", i), ctx.at("ema21", i), ctx.at("ema50", i)
    slope, price = ctx.at("ema21_slope", i), float(ctx.bars.close[i])
    if not _ok(e9, e21, e50, slope):
        return _idle("trend_stack", TREND)

    score = 0.0
    if e9 > e21 > e50:
        score += 0.6
        shape = "EMA 9>21>50 stacked bullish"
    elif e9 < e21 < e50:
        score -= 0.6
        shape = "EMA 9<21<50 stacked bearish"
    else:
        shape = "EMAs tangled, no clean stack"
    score += _clamp(slope / 0.35) * 0.4
    score += 0.12 if price > e50 else -0.12
    return Signal("trend_stack", _clamp(score), 1.0,
                  "%s; EMA21 slope %+.2f%%" % (shape, slope), TREND)


def macd_momentum(ctx: Context, i: int) -> Signal:
    """Histogram direction, expansion and zero-line side."""
    hist, line = ctx.at("macd_hist", i), ctx.at("macd", i)
    prev = ctx.at("macd_hist", i - 1)
    atr_v = ctx.at("atr", i)
    if not _ok(hist, line, prev, atr_v) or atr_v <= 0:
        return _idle("macd_momentum", TREND)

    norm = _clamp(hist / (atr_v * 0.6))
    expanding = abs(hist) > abs(prev)
    score = norm * (1.0 if expanding else 0.55)
    score += 0.1 if line > 0 else -0.1
    return Signal("macd_momentum", _clamp(score), 1.0,
                  "MACD hist %+.4f and %s, line %s zero" % (
                      hist, "expanding" if expanding else "fading",
                      "above" if line > 0 else "below"), TREND)


def supertrend_follow(ctx: Context, i: int) -> Signal:
    """Supertrend direction, scaled by how far price has run from the band.

    The band ratchets in the trend direction and only flips on a close through
    it, which is what stops it whipsawing on ordinary pullbacks.
    """
    direction, line = ctx.at("supertrend_dir", i), ctx.at("supertrend", i)
    atr_v, price = ctx.at("atr", i), float(ctx.bars.close[i])
    if not _ok(direction, line, atr_v) or atr_v <= 0 or direction == 0:
        return _idle("supertrend_follow", TREND)

    dist = abs(price - line) / atr_v
    # A fresh flip is the highest-information moment; a long-running trend
    # already far from its band is more likely to be late than strong.
    bars_since = 0
    for k in range(i, max(0, i - 40), -1):
        if ctx.at("supertrend_dir", k) != direction:
            break
        bars_since += 1
    freshness = 1.0 if bars_since <= 3 else max(0.45, 1.0 - (bars_since - 3) / 30.0)
    score = direction * min(0.45 + dist * 0.35, 1.0) * freshness
    return Signal("supertrend_follow", _clamp(score), 1.0,
                  "supertrend %s for %d bars, %.1f ATR from band" % (
                      "up" if direction > 0 else "down", bars_since, dist), TREND)


def adx_di_cross(ctx: Context, i: int) -> Signal:
    """Directional Index spread, gated by ADX trend strength.

    +DI over -DI only means something when ADX confirms a trend exists. Below
    about 20 the spread is noise.
    """
    pdi, mdi, adx_v = ctx.at("plus_di", i), ctx.at("minus_di", i), ctx.at("adx", i)
    if not _ok(pdi, mdi, adx_v):
        return _idle("adx_di_cross", TREND)

    spread = (pdi - mdi) / max(pdi + mdi, 1e-9)
    strength = _clamp((adx_v - 18.0) / 22.0)
    if strength <= 0:
        return Signal("adx_di_cross", 0.0, 0.35,
                      "ADX %.0f, too weak for a directional read" % adx_v, TREND)
    return Signal("adx_di_cross", _clamp(spread * strength * 1.4), 1.0,
                  "+DI %.0f vs -DI %.0f with ADX %.0f" % (pdi, mdi, adx_v), TREND)


def ichimoku_cloud(ctx: Context, i: int) -> Signal:
    """Price against the cloud, plus the tenkan/kijun relationship.

    The cloud used here is the forward-shifted one actually in effect at this
    bar, which is the whole point of the indicator.
    """
    price = float(ctx.bars.close[i])
    tk, kj = ctx.at("tenkan", i), ctx.at("kijun", i)
    sa, sb = ctx.at("senkou_a", i), ctx.at("senkou_b", i)
    if not _ok(tk, kj, sa, sb):
        return _idle("ichimoku_cloud", TREND)

    top, bottom = max(sa, sb), min(sa, sb)
    if price > top:
        base, where = 0.55, "above the cloud"
    elif price < bottom:
        base, where = -0.55, "below the cloud"
    else:
        base, where = 0.0, "inside the cloud, no trend"
    cross = 0.35 if tk > kj else -0.35
    thickness = "thin" if (top - bottom) < abs(price) * 0.001 else "thick"
    return Signal("ichimoku_cloud", _clamp(base + cross), 1.0,
                  "price %s (%s), tenkan %s kijun" % (
                      where, thickness, "above" if tk > kj else "below"), TREND)


def parabolic_sar_flip(ctx: Context, i: int) -> Signal:
    """SAR direction, weighted toward recent flips."""
    direction, sar = ctx.at("sar_dir", i), ctx.at("sar", i)
    atr_v, price = ctx.at("atr", i), float(ctx.bars.close[i])
    if not _ok(direction, sar, atr_v) or atr_v <= 0 or direction == 0:
        return _idle("parabolic_sar_flip", TREND)

    bars_since = 0
    for k in range(i, max(0, i - 30), -1):
        if ctx.at("sar_dir", k) != direction:
            break
        bars_since += 1
    dist = abs(price - sar) / atr_v
    freshness = 1.0 if bars_since <= 2 else max(0.35, 1.0 - (bars_since - 2) / 20.0)
    return Signal("parabolic_sar_flip", _clamp(direction * min(0.5 + dist * 0.3, 1.0) * freshness),
                  1.0, "SAR %s for %d bars" % (
                      "below price" if direction > 0 else "above price", bars_since), TREND)


def linreg_channel(ctx: Context, i: int) -> Signal:
    """Regression slope scaled by fit quality.

    A steep but noisy drift and a shallow but orderly one are different trades.
    Multiplying slope by r-squared expresses that directly.
    """
    slope, r2 = ctx.at("linreg_slope", i), ctx.at("linreg_r2", i)
    if not _ok(slope, r2):
        return _idle("linreg_channel", TREND)
    quality = max(0.0, r2)
    return Signal("linreg_channel", _clamp((slope / 0.5) * quality), 1.0,
                  "regression slope %+.3f%%/bar with r2 %.2f" % (slope, r2), TREND)


def aroon_trend(ctx: Context, i: int) -> Signal:
    """How recently the 25-bar high and low were set."""
    up, dn = ctx.at("aroon_up", i), ctx.at("aroon_dn", i)
    if not _ok(up, dn):
        return _idle("aroon_trend", TREND)
    return Signal("aroon_trend", _clamp((up - dn) / 100.0), 1.0,
                  "Aroon up %.0f vs down %.0f" % (up, dn), TREND)


def tsi_trend(ctx: Context, i: int) -> Signal:
    """True Strength Index: double-smoothed momentum, slow but clean."""
    t, prev = ctx.at("tsi", i), ctx.at("tsi", i - 3)
    if not _ok(t, prev):
        return _idle("tsi_trend", TREND)
    rising = t > prev
    score = _clamp(t / 35.0) * (1.0 if rising == (t > 0) else 0.55)
    return Signal("tsi_trend", score, 1.0,
                  "TSI %+.1f and %s" % (t, "rising" if rising else "falling"), TREND)


# ---------------------------------------------------------------------------
# Mean reversion family
# ---------------------------------------------------------------------------

def rsi_reversion(ctx: Context, i: int) -> Signal:
    """RSI and Bollinger %B extremes. Contrarian by construction."""
    r, pb = ctx.at("rsi", i), ctx.at("pct_b", i)
    if not _ok(r, pb):
        return _idle("rsi_reversion", REVERSION)
    rsi_score = _clamp((50.0 - r) / 22.0)
    bb_score = _clamp((0.5 - pb) / 0.45)
    state = ("RSI %.0f %s" % (r, "oversold" if r < 30 else "overbought")
             if (r < 30 or r > 70) else "RSI %.0f neutral" % r)
    return Signal("rsi_reversion", _clamp(0.55 * rsi_score + 0.45 * bb_score), 1.0,
                  "%s, %%B %.2f" % (state, pb), REVERSION)


def bollinger_fade(ctx: Context, i: int) -> Signal:
    """Fade a tag of the outer band, but only when the bands are wide.

    A band tag during a squeeze is usually the start of a breakout, not a
    reversion opportunity. Bandwidth percentile separates the two cases.
    """
    pb, bwp = ctx.at("pct_b", i), ctx.at("bw_pctile", i)
    if not _ok(pb, bwp):
        return _idle("bollinger_fade", REVERSION)
    if bwp < 0.35:
        return Signal("bollinger_fade", 0.0, 0.3,
                      "bands are compressed, a tag here favours breakout not fade",
                      REVERSION)
    if pb > 1.0:
        score, note = -min((pb - 1.0) * 3.0, 1.0), "closed above the upper band"
    elif pb < 0.0:
        score, note = min((0.0 - pb) * 3.0, 1.0), "closed below the lower band"
    else:
        score, note = _clamp((0.5 - pb) * 1.2) * 0.5, "%%B %.2f, inside the bands" % pb
    return Signal("bollinger_fade", _clamp(score), 1.0, note, REVERSION)


def keltner_reversion(ctx: Context, i: int) -> Signal:
    """Price outside the Keltner channel, measured in ATR."""
    up, lo, mid = ctx.at("kc_upper", i), ctx.at("kc_lower", i), ctx.at("kc_mid", i)
    atr_v, price = ctx.at("atr", i), float(ctx.bars.close[i])
    if not _ok(up, lo, mid, atr_v) or atr_v <= 0:
        return _idle("keltner_reversion", REVERSION)
    if price > up:
        return Signal("keltner_reversion", _clamp(-min((price - up) / atr_v, 1.0)), 1.0,
                      "%.2f ATR above the Keltner upper band" % ((price - up) / atr_v),
                      REVERSION)
    if price < lo:
        return Signal("keltner_reversion", _clamp(min((lo - price) / atr_v, 1.0)), 1.0,
                      "%.2f ATR below the Keltner lower band" % ((lo - price) / atr_v),
                      REVERSION)
    return Signal("keltner_reversion", 0.0, 0.4, "inside the Keltner channel", REVERSION)


def zscore_reversion(ctx: Context, i: int) -> Signal:
    """Statistical distance from the 20-bar mean."""
    z = ctx.at("zscore", i)
    if not _ok(z):
        return _idle("zscore_reversion", REVERSION)
    return Signal("zscore_reversion", _clamp(-z / 2.2), 1.0,
                  "price %+.2f standard deviations from its 20-bar mean" % z, REVERSION)


def stochastic_reversal(ctx: Context, i: int) -> Signal:
    """%K crossing %D inside an extreme zone.

    The cross matters more than the level. Stochastic can sit pinned above 80
    for the whole of a strong trend, so the level alone is a poor short signal.
    """
    k, d = ctx.at("stoch_k", i), ctx.at("stoch_d", i)
    pk, pd = ctx.at("stoch_k", i - 1), ctx.at("stoch_d", i - 1)
    if not _ok(k, d, pk, pd):
        return _idle("stochastic_reversal", REVERSION)

    crossed_up = pk <= pd and k > d
    crossed_dn = pk >= pd and k < d
    if crossed_up and k < 35:
        return Signal("stochastic_reversal", _clamp(0.55 + (35 - k) / 70.0), 1.0,
                      "%%K crossed up through %%D at %.0f, oversold" % k, REVERSION)
    if crossed_dn and k > 65:
        return Signal("stochastic_reversal", _clamp(-0.55 - (k - 65) / 70.0), 1.0,
                      "%%K crossed down through %%D at %.0f, overbought" % k, REVERSION)
    return Signal("stochastic_reversal", _clamp((50.0 - k) / 120.0), 0.5,
                  "%%K %.0f, no cross" % k, REVERSION)


def williams_reversal(ctx: Context, i: int) -> Signal:
    """Williams %R extremes with a turn back inside."""
    w, pw = ctx.at("willr", i), ctx.at("willr", i - 1)
    if not _ok(w, pw):
        return _idle("williams_reversal", REVERSION)
    score = _clamp((-50.0 - w) / 35.0)
    turning = (w < -80 and w > pw) or (w > -20 and w < pw)
    return Signal("williams_reversal", score * (1.0 if turning else 0.6), 1.0,
                  "Williams %%R %.0f%s" % (w, ", turning back inside" if turning else ""),
                  REVERSION)


def cci_reversion(ctx: Context, i: int) -> Signal:
    """CCI beyond the conventional +-100 bands."""
    c = ctx.at("cci", i)
    if not _ok(c):
        return _idle("cci_reversion", REVERSION)
    if abs(c) < 100:
        return Signal("cci_reversion", _clamp(-c / 300.0), 0.5,
                      "CCI %.0f, inside the normal band" % c, REVERSION)
    return Signal("cci_reversion", _clamp(-c / 220.0), 1.0,
                  "CCI %.0f, %s extreme" % (c, "upper" if c > 0 else "lower"), REVERSION)


def vwap_band_fade(ctx: Context, i: int) -> Signal:
    """Fade a stretch to the session VWAP bands.

    These bands are where intraday mean-reversion desks actually work, which
    makes them more useful than raw distance from VWAP.
    """
    vw, up, lo = ctx.at("vwap", i), ctx.at("vwap_upper", i), ctx.at("vwap_lower", i)
    price = float(ctx.bars.close[i])
    if not _ok(vw, up, lo) or up <= vw:
        return _idle("vwap_band_fade", REVERSION)
    band = up - vw
    if band <= 0:
        return _idle("vwap_band_fade", REVERSION, "VWAP bands have no width")

    dev = (price - vw) / band
    anchor = "session VWAP" if ctx.vwap_is_session else "20-bar VWAP"
    if abs(dev) < 1.0:
        return Signal("vwap_band_fade", 0.0, 0.4,
                      "%.1f bands from %s, not stretched" % (abs(dev), anchor),
                      REVERSION)
    return Signal("vwap_band_fade", _clamp(-dev / 2.2), 1.0,
                  "%.1f bands %s %s" % (abs(dev), "above" if dev > 0 else "below",
                                        anchor), REVERSION)


# ---------------------------------------------------------------------------
# Breakout family
# ---------------------------------------------------------------------------

def squeeze_breakout(ctx: Context, i: int) -> Signal:
    """Volatility compression resolving into an expansion move.

    Uses the classic Bollinger-inside-Keltner squeeze test. Compression alone
    is a setup, not a signal: the break and the volume are what make it one.
    """
    bwp, rv, atr_v = ctx.at("bw_pctile", i), ctx.at("rvol", i), ctx.at("atr", i)
    bb_u, bb_l = ctx.at("bb_upper", i), ctx.at("bb_lower", i)
    kc_u, kc_l = ctx.at("kc_upper", i), ctx.at("kc_lower", i)
    if not _ok(bwp, atr_v, bb_u, kc_u) or i < 25 or atr_v <= 0:
        return _idle("squeeze_breakout", BREAKOUT)

    in_squeeze = (bb_u < kc_u and bb_l > kc_l)
    recent = np.nanmin(ctx.s["bw_pctile"][max(0, i - 12):i + 1])
    if not in_squeeze and not (_ok(recent) and recent <= 0.30):
        return Signal("squeeze_breakout", 0.0, 0.3,
                      "no recent volatility squeeze", BREAKOUT)

    hi = float(np.max(ctx.bars.high[i - 20:i]))
    lo = float(np.min(ctx.bars.low[i - 20:i]))
    price = float(ctx.bars.close[i])
    vol_ok = _ok(rv) and rv > 1.2

    if price > hi:
        score, desc = 0.55 + 0.45 * min((price - hi) / atr_v, 1.0), "breaking the 20-bar high out of a squeeze"
    elif price < lo:
        score, desc = -(0.55 + 0.45 * min((lo - price) / atr_v, 1.0)), "breaking the 20-bar low out of a squeeze"
    else:
        return Signal("squeeze_breakout", 0.0, 0.5,
                      "coiled in a squeeze, no break yet", BREAKOUT)
    if not vol_ok:
        score *= 0.45
        desc += " but volume is not confirming"
    return Signal("squeeze_breakout", _clamp(score), 1.0, desc, BREAKOUT)


def donchian_breakout(ctx: Context, i: int) -> Signal:
    """A close beyond the 20-bar channel. The original turtle signal."""
    up, lo = ctx.at("dc_upper", i - 1), ctx.at("dc_lower", i - 1)
    atr_v, price = ctx.at("atr", i), float(ctx.bars.close[i])
    rv = ctx.at("rvol", i)
    if not _ok(up, lo, atr_v) or atr_v <= 0:
        return _idle("donchian_breakout", BREAKOUT)

    conviction = 1.0 if (_ok(rv) and rv > 1.1) else 0.55
    if price > up:
        return Signal("donchian_breakout",
                      _clamp(min(0.6 + (price - up) / atr_v, 1.0) * conviction), 1.0,
                      "closed above the 20-bar channel high", BREAKOUT)
    if price < lo:
        return Signal("donchian_breakout",
                      _clamp(-min(0.6 + (lo - price) / atr_v, 1.0) * conviction), 1.0,
                      "closed below the 20-bar channel low", BREAKOUT)
    span = up - lo
    pos = (price - lo) / span if span > 0 else 0.5
    return Signal("donchian_breakout", _clamp((pos - 0.5) * 0.5), 0.5,
                  "inside the channel, %.0f%% of the way up" % (pos * 100), BREAKOUT)


def opening_range_break(ctx: Context, i: int) -> Signal:
    """Break of the first 30 minutes of the session.

    The reference every breakout day-trader watches. Only meaningful once the
    opening range is complete and while the session is still young.
    """
    bars = ctx.bars
    sess = bars.session_id[i]
    idx = np.flatnonzero(bars.session_id[:i + 1] == sess)
    if len(idx) < 8:
        return _idle("opening_range_break", BREAKOUT, "session too young")

    start_ts = int(bars.timestamp[idx[0]])
    window = idx[bars.timestamp[idx] < start_ts + 30 * 60]
    if len(window) < 3 or len(window) >= len(idx):
        return _idle("opening_range_break", BREAKOUT, "opening range not yet complete")

    orb_hi = float(np.max(bars.high[window]))
    orb_lo = float(np.min(bars.low[window]))
    atr_v, price = ctx.at("atr", i), float(bars.close[i])
    if not _ok(atr_v) or atr_v <= 0:
        return _idle("opening_range_break", BREAKOUT)

    # An opening-range break loses meaning late in the session.
    age = (len(idx) - len(window)) / max(len(window) * 6.0, 1.0)
    decay = max(0.3, 1.0 - age * 0.5)
    if price > orb_hi:
        return Signal("opening_range_break",
                      _clamp(min(0.6 + (price - orb_hi) / atr_v, 1.0) * decay), 1.0,
                      "above the opening range high", BREAKOUT)
    if price < orb_lo:
        return Signal("opening_range_break",
                      _clamp(-min(0.6 + (orb_lo - price) / atr_v, 1.0) * decay), 1.0,
                      "below the opening range low", BREAKOUT)
    return Signal("opening_range_break", 0.0, 0.4,
                  "still inside the opening range", BREAKOUT)


def volatility_expansion(ctx: Context, i: int) -> Signal:
    """A range expansion bar after a contraction, in the direction of the bar."""
    atr_v = ctx.at("atr", i)
    if not _ok(atr_v) or atr_v <= 0 or i < 12:
        return _idle("volatility_expansion", BREAKOUT)
    rng = float(ctx.bars.high[i] - ctx.bars.low[i])
    prior = float(np.mean(ctx.bars.high[i - 10:i] - ctx.bars.low[i - 10:i]))
    if prior <= 0:
        return _idle("volatility_expansion", BREAKOUT)
    ratio = rng / prior
    if ratio < 1.5:
        return Signal("volatility_expansion", 0.0, 0.35,
                      "range %.1fx the recent average, no expansion" % ratio, BREAKOUT)
    body = ctx.at("body", i)
    if not _ok(body):
        return _idle("volatility_expansion", BREAKOUT)
    return Signal("volatility_expansion", _clamp(body * min(ratio / 2.5, 1.0) * 1.5), 1.0,
                  "range %.1fx the recent average with a %s body" % (
                      ratio, "bullish" if body > 0 else "bearish"), BREAKOUT)


def inside_bar_break(ctx: Context, i: int) -> Signal:
    """Break of a coiling inside bar, a classic short-term continuation trigger."""
    if i < 3:
        return _idle("inside_bar_break", BREAKOUT)
    h1, l1 = float(ctx.bars.high[i - 1]), float(ctx.bars.low[i - 1])
    h2, l2 = float(ctx.bars.high[i - 2]), float(ctx.bars.low[i - 2])
    price = float(ctx.bars.close[i])
    if not (h1 <= h2 and l1 >= l2):
        return Signal("inside_bar_break", 0.0, 0.3, "no inside bar to break", BREAKOUT)
    if price > h1:
        return Signal("inside_bar_break", 0.7, 1.0, "broke the inside bar high", BREAKOUT)
    if price < l1:
        return Signal("inside_bar_break", -0.7, 1.0, "broke the inside bar low", BREAKOUT)
    return Signal("inside_bar_break", 0.0, 0.5, "inside bar still unresolved", BREAKOUT)


# ---------------------------------------------------------------------------
# Volume family
# ---------------------------------------------------------------------------

def volume_confirmation(ctx: Context, i: int) -> Signal:
    """Does volume back the direction of recent price movement?"""
    rv, atr_v = ctx.at("rvol", i), ctx.at("atr", i)
    if not _ok(rv, atr_v) or i < 5 or atr_v <= 0:
        return _idle("volume_confirmation", VOLUME)
    move = (float(ctx.bars.close[i]) - float(ctx.bars.close[i - 3])) / atr_v
    conviction = _clamp((rv - 1.0) / 1.5)
    if conviction <= 0:
        return Signal("volume_confirmation", 0.0, 0.4,
                      "volume %.2fx average, below normal" % rv, VOLUME)
    return Signal("volume_confirmation",
                  _clamp(math.copysign(min(abs(move), 1.0), move) * conviction), 1.0,
                  "volume %.2fx average backing a %+.2f ATR move" % (rv, move), VOLUME)


def obv_divergence(ctx: Context, i: int) -> Signal:
    """On-Balance Volume disagreeing with price.

    Price making a new high that volume flow does not confirm is the classic
    warning that a move is running out of participation.
    """
    if i < 25:
        return _idle("obv_divergence", VOLUME)
    price_now = float(ctx.bars.close[i])
    price_then = float(ctx.bars.close[i - 20])
    obv_now, obv_then = ctx.at("obv", i), ctx.at("obv", i - 20)
    if not _ok(obv_now, obv_then) or price_then == 0:
        return _idle("obv_divergence", VOLUME)

    price_chg = (price_now - price_then) / abs(price_then)
    scale = max(abs(obv_then), 1.0)
    obv_chg = (obv_now - obv_then) / scale

    if price_chg > 0.001 and obv_chg < -0.02:
        return Signal("obv_divergence", -0.65, 1.0,
                      "price up %.2f%% but volume flow is negative" % (price_chg * 100), VOLUME)
    if price_chg < -0.001 and obv_chg > 0.02:
        return Signal("obv_divergence", 0.65, 1.0,
                      "price down %.2f%% but volume flow is positive" % (price_chg * 100), VOLUME)
    return Signal("obv_divergence", _clamp(obv_chg * 2.0) * 0.4, 0.5,
                  "volume flow broadly agrees with price", VOLUME)


def mfi_extremes(ctx: Context, i: int) -> Signal:
    """Money Flow Index: an RSI that knows about volume."""
    m = ctx.at("mfi", i)
    if not _ok(m):
        return _idle("mfi_extremes", VOLUME)
    label = "neutral"
    if m > 80:
        label = "overbought on heavy flow"
    elif m < 20:
        label = "oversold on heavy flow"
    return Signal("mfi_extremes", _clamp((50.0 - m) / 30.0), 1.0,
                  "MFI %.0f, %s" % (m, label), VOLUME)


def accumulation_trend(ctx: Context, i: int) -> Signal:
    """Slope of the Accumulation/Distribution line: quiet buying or selling."""
    slope = ctx.at("ad_slope", i)
    if not _ok(slope):
        return _idle("accumulation_trend", VOLUME)
    return Signal("accumulation_trend", _clamp(slope / 3.0), 1.0,
                  "A/D line %s (%+.2f%% over 10 bars)" % (
                      "accumulating" if slope > 0 else "distributing", slope), VOLUME)


# ---------------------------------------------------------------------------
# Structure family
# ---------------------------------------------------------------------------

def vwap_position(ctx: Context, i: int) -> Signal:
    """Where price sits relative to session VWAP, measured in ATR units.

    Deliberately non-monotonic: the score peaks around one ATR and decays past
    2.5, where being stretched becomes a reversion risk rather than strength.
    """
    vw, atr_v = ctx.at("vwap", i), ctx.at("atr", i)
    price = float(ctx.bars.close[i])
    if not _ok(vw, atr_v) or atr_v <= 0:
        return _idle("vwap_position", STRUCTURE, "no VWAP yet")

    dist = (price - vw) / atr_v
    mag = min(abs(dist), 1.0)
    if abs(dist) > 2.5:
        mag *= max(0.0, 1.0 - (abs(dist) - 2.5) / 2.0)
    note = ", stretched" if abs(dist) > 2.5 else ""
    anchor = "session VWAP" if ctx.vwap_is_session else "20-bar VWAP"
    return Signal("vwap_position", _clamp(math.copysign(mag, dist)), 1.0,
                  "price %.2f ATR %s %s%s" % (
                      abs(dist), "above" if dist > 0 else "below", anchor, note),
                  STRUCTURE)


def market_structure(ctx: Context, i: int) -> Signal:
    """Higher highs and higher lows, or the reverse.

    The most basic definition of trend, and one that survives when indicators
    disagree. Compares two consecutive 10-bar windows.
    """
    if i < 25:
        return _idle("market_structure", STRUCTURE)
    h_recent = float(np.max(ctx.bars.high[i - 9:i + 1]))
    h_prior = float(np.max(ctx.bars.high[i - 19:i - 9]))
    l_recent = float(np.min(ctx.bars.low[i - 9:i + 1]))
    l_prior = float(np.min(ctx.bars.low[i - 19:i - 9]))

    hh, hl = h_recent > h_prior, l_recent > l_prior
    lh, ll = h_recent < h_prior, l_recent < l_prior
    if hh and hl:
        return Signal("market_structure", 0.75, 1.0, "higher highs and higher lows", STRUCTURE)
    if lh and ll:
        return Signal("market_structure", -0.75, 1.0, "lower highs and lower lows", STRUCTURE)
    if hh or hl:
        return Signal("market_structure", 0.3, 0.7, "partially bullish structure", STRUCTURE)
    if lh or ll:
        return Signal("market_structure", -0.3, 0.7, "partially bearish structure", STRUCTURE)
    return Signal("market_structure", 0.0, 0.4, "structure is flat", STRUCTURE)


def pivot_levels(ctx: Context, i: int) -> Signal:
    """Price against classic floor-trader pivots from the prior session.

    Distance is measured in units of the prior session's range, not intraday
    ATR. A daily pivot naturally sits many 5-minute ATRs away, so quoting that
    ratio is a unit mismatch that produces impressive-looking nonsense. The
    session range is the scale these levels actually live on, and it happens to
    equal the R1-to-S1 span exactly.
    """
    hlc = ctx.prior_session_hlc(i)
    if hlc is None:
        return _idle("pivot_levels", STRUCTURE, "no prior session")
    ph, pl, pc = hlc
    span = ph - pl
    if span <= 0:
        return _idle("pivot_levels", STRUCTURE)

    p = ind.pivot_points(ph, pl, pc)
    price = float(ctx.bars.close[i])
    norm = (price - p["pivot"]) / span

    # Beyond the prior session's whole range the move is stretched rather than
    # simply strong, so conviction is trimmed the same way VWAP distance is.
    mag = min(abs(norm) * 1.5, 1.0)
    if abs(norm) > 1.0:
        mag *= max(0.35, 1.0 - (abs(norm) - 1.0) * 0.5)

    nearest_dist, nearest_name = min(((abs(price - v), k) for k, v in p.items()),
                                     key=lambda t: t[0])
    sitting_on = nearest_dist < 0.08 * span
    if sitting_on:
        mag *= 0.5          # right at a level, direction is genuinely uncertain
    return Signal("pivot_levels", _clamp(math.copysign(mag, norm)), 1.0,
                  "%.2f of the prior range %s the pivot%s" % (
                      abs(norm), "above" if norm > 0 else "below",
                      ", sitting on %s" % nearest_name if sitting_on else ""),
                  STRUCTURE)


def gap_behaviour(ctx: Context, i: int) -> Signal:
    """How price is treating a session-opening gap.

    A gap that holds is continuation. A gap filling back through its own open
    is one of the more reliable intraday reversal tells.
    """
    bars = ctx.bars
    sess = bars.session_id[i]
    idx = np.flatnonzero(bars.session_id[:i + 1] == sess)
    hlc = ctx.prior_session_hlc(i)
    if len(idx) < 2 or hlc is None:
        return _idle("gap_behaviour", STRUCTURE, "no gap reference")

    ph, pl, prev_close = hlc
    span = ph - pl
    open_px, price = float(bars.open[idx[0]]), float(bars.close[i])
    if span <= 0 or prev_close <= 0:
        return _idle("gap_behaviour", STRUCTURE)

    # Measured against the prior session's range, for the same reason pivots
    # are: an overnight gap is a daily-scale event, not an intraday one.
    gap = (open_px - prev_close) / span
    if abs(gap) < 0.10:
        return Signal("gap_behaviour", 0.0, 0.3, "no meaningful gap", STRUCTURE)

    pct = abs(gap) * 100
    if (gap > 0 and price <= prev_close) or (gap < 0 and price >= prev_close):
        return Signal("gap_behaviour", _clamp(-math.copysign(0.6, gap)), 1.0,
                      "%s gap of %.0f%% of the prior range has filled" % (
                          "up" if gap > 0 else "down", pct), STRUCTURE)
    holding = (gap > 0 and price > open_px) or (gap < 0 and price < open_px)
    return Signal("gap_behaviour",
                  _clamp(math.copysign(0.55 if holding else 0.2, gap)), 1.0,
                  "%s gap of %.0f%% of the prior range, %s" % (
                      "up" if gap > 0 else "down", pct,
                      "holding above the open" if holding else "fading"), STRUCTURE)


# ---------------------------------------------------------------------------
# Candlestick pattern family
# ---------------------------------------------------------------------------

def engulfing_pattern(ctx: Context, i: int) -> Signal:
    """Bullish or bearish engulfing, weighted by relative size and volume."""
    if i < 2:
        return _idle("engulfing_pattern", PATTERN)
    o1, c1 = float(ctx.bars.open[i]), float(ctx.bars.close[i])
    o0, c0 = float(ctx.bars.open[i - 1]), float(ctx.bars.close[i - 1])
    body_now, body_prev = abs(c1 - o1), abs(c0 - o0)
    if body_prev <= 0 or body_now <= body_prev:
        return Signal("engulfing_pattern", 0.0, 0.3, "no engulfing bar", PATTERN)

    rv = ctx.at("rvol", i)
    boost = 1.0 if (_ok(rv) and rv > 1.2) else 0.65
    size = min(body_now / body_prev / 2.0, 1.0)
    if c1 > o1 and c0 < o0 and c1 >= o0 and o1 <= c0:
        return Signal("engulfing_pattern", _clamp(0.55 + 0.45 * size) * boost, 1.0,
                      "bullish engulfing, %.1fx the prior body" % (body_now / body_prev),
                      PATTERN)
    if c1 < o1 and c0 > o0 and c1 <= o0 and o1 >= c0:
        return Signal("engulfing_pattern", -_clamp(0.55 + 0.45 * size) * boost, 1.0,
                      "bearish engulfing, %.1fx the prior body" % (body_now / body_prev),
                      PATTERN)
    return Signal("engulfing_pattern", 0.0, 0.3, "no engulfing bar", PATTERN)


def pin_bar(ctx: Context, i: int) -> Signal:
    """A long rejection wick: buyers or sellers defended a level hard."""
    body, up_w, lo_w = ctx.at("body", i), ctx.at("upper_wick", i), ctx.at("lower_wick", i)
    if not _ok(body, up_w, lo_w):
        return _idle("pin_bar", PATTERN)
    if lo_w > 0.55 and abs(body) < 0.35:
        return Signal("pin_bar", _clamp(lo_w * 1.3), 1.0,
                      "long lower wick, %.0f%% of the bar rejected downside" % (lo_w * 100),
                      PATTERN)
    if up_w > 0.55 and abs(body) < 0.35:
        return Signal("pin_bar", _clamp(-up_w * 1.3), 1.0,
                      "long upper wick, %.0f%% of the bar rejected upside" % (up_w * 100),
                      PATTERN)
    return Signal("pin_bar", 0.0, 0.3, "no rejection wick", PATTERN)


def momentum_thrust(ctx: Context, i: int) -> Signal:
    """Three consecutive bars in one direction with expanding participation."""
    if i < 4:
        return _idle("momentum_thrust", PATTERN)
    bodies = [ctx.at("body", i - k) for k in range(3)]
    if not _ok(*bodies):
        return _idle("momentum_thrust", PATTERN)
    if all(b > 0.25 for b in bodies):
        return Signal("momentum_thrust", _clamp(0.5 + sum(bodies) / 6.0), 1.0,
                      "three consecutive bullish bodies", PATTERN)
    if all(b < -0.25 for b in bodies):
        return Signal("momentum_thrust", _clamp(-0.5 + sum(bodies) / 6.0), 1.0,
                      "three consecutive bearish bodies", PATTERN)
    return Signal("momentum_thrust", 0.0, 0.3, "no sustained thrust", PATTERN)


def roc_momentum(ctx: Context, i: int) -> Signal:
    """Plain rate of change, normalised by the instrument's own volatility."""
    r, hv = ctx.at("roc", i), ctx.at("hv", i)
    if not _ok(r, hv) or hv <= 0:
        return _idle("roc_momentum", TREND)
    return Signal("roc_momentum", _clamp(r / (hv * 3.0)), 1.0,
                  "10-bar rate of change %+.2f%%" % r, TREND)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: List[Strategy] = [
    # Trend
    Strategy("trend_stack", TREND, trend_stack,
             "EMA 9/21/50 alignment and slope", (TRENDING, TRANSITIONAL), 1.00),
    Strategy("macd_momentum", TREND, macd_momentum,
             "MACD histogram direction and expansion", (TRENDING, TRANSITIONAL), 0.85),
    Strategy("supertrend_follow", TREND, supertrend_follow,
             "ATR-banded trend follower with ratcheting stop", (TRENDING,), 0.95),
    Strategy("adx_di_cross", TREND, adx_di_cross,
             "Directional index spread gated by ADX", (TRENDING,), 0.85),
    Strategy("ichimoku_cloud", TREND, ichimoku_cloud,
             "Price against the forward-shifted cloud", (TRENDING,), 0.80),
    Strategy("parabolic_sar_flip", TREND, parabolic_sar_flip,
             "Parabolic SAR direction and flip recency", (TRENDING,), 0.70),
    Strategy("linreg_channel", TREND, linreg_channel,
             "Regression slope scaled by fit quality", (TRENDING, TRANSITIONAL), 0.85),
    Strategy("aroon_trend", TREND, aroon_trend,
             "Recency of the 25-bar high versus low", (TRENDING,), 0.70),
    Strategy("tsi_trend", TREND, tsi_trend,
             "Double-smoothed momentum", (TRENDING, TRANSITIONAL), 0.75),
    Strategy("roc_momentum", TREND, roc_momentum,
             "Rate of change normalised by volatility", (TRENDING,), 0.65),

    # Mean reversion
    Strategy("rsi_reversion", REVERSION, rsi_reversion,
             "RSI and Bollinger %B extremes", (RANGING,), 0.85),
    Strategy("bollinger_fade", REVERSION, bollinger_fade,
             "Fade band tags when bands are wide", (RANGING,), 0.85),
    Strategy("keltner_reversion", REVERSION, keltner_reversion,
             "Price outside the Keltner channel", (RANGING,), 0.75),
    Strategy("zscore_reversion", REVERSION, zscore_reversion,
             "Statistical distance from the 20-bar mean", (RANGING,), 0.80),
    Strategy("stochastic_reversal", REVERSION, stochastic_reversal,
             "%K/%D cross inside an extreme zone", (RANGING,), 0.80),
    Strategy("williams_reversal", REVERSION, williams_reversal,
             "Williams %R extremes turning back inside", (RANGING,), 0.70),
    Strategy("cci_reversion", REVERSION, cci_reversion,
             "CCI beyond the +-100 bands", (RANGING,), 0.70),
    Strategy("vwap_band_fade", REVERSION, vwap_band_fade,
             "Fade a stretch to the session VWAP bands", (RANGING, TRANSITIONAL), 0.85),

    # Breakout
    Strategy("squeeze_breakout", BREAKOUT, squeeze_breakout,
             "Bollinger-inside-Keltner squeeze resolving", (TRANSITIONAL, TRENDING), 0.90),
    Strategy("donchian_breakout", BREAKOUT, donchian_breakout,
             "Close beyond the 20-bar channel", (TRENDING, TRANSITIONAL), 0.85),
    Strategy("opening_range_break", BREAKOUT, opening_range_break,
             "Break of the first 30 minutes", (TRANSITIONAL, TRENDING), 0.80),
    Strategy("volatility_expansion", BREAKOUT, volatility_expansion,
             "Range expansion after contraction", (TRANSITIONAL,), 0.75),
    Strategy("inside_bar_break", BREAKOUT, inside_bar_break,
             "Break of a coiling inside bar", (TRANSITIONAL,), 0.60),

    # Volume
    Strategy("volume_confirmation", VOLUME, volume_confirmation,
             "Whether volume backs the recent move", (TRENDING, TRANSITIONAL), 0.60),
    Strategy("obv_divergence", VOLUME, obv_divergence,
             "Volume flow disagreeing with price", (RANGING, TRANSITIONAL), 0.75),
    Strategy("mfi_extremes", VOLUME, mfi_extremes,
             "Volume-weighted overbought and oversold", (RANGING,), 0.70),
    Strategy("accumulation_trend", VOLUME, accumulation_trend,
             "Slope of the accumulation/distribution line", (TRENDING,), 0.65),

    # Structure
    Strategy("vwap_position", STRUCTURE, vwap_position,
             "Distance from session VWAP in ATR", (TRENDING, TRANSITIONAL), 0.90),
    Strategy("market_structure", STRUCTURE, market_structure,
             "Higher highs and higher lows, or the reverse", (TRENDING,), 0.85),
    Strategy("pivot_levels", STRUCTURE, pivot_levels,
             "Price against classic floor-trader pivots", (RANGING, TRANSITIONAL), 0.70),
    Strategy("gap_behaviour", STRUCTURE, gap_behaviour,
             "Whether an opening gap holds or fills", (TRANSITIONAL,), 0.70),

    # Patterns
    Strategy("engulfing_pattern", PATTERN, engulfing_pattern,
             "Bullish or bearish engulfing bar", (RANGING, TRANSITIONAL), 0.60),
    Strategy("pin_bar", PATTERN, pin_bar,
             "Long rejection wick at a level", (RANGING, TRANSITIONAL), 0.60),
    Strategy("momentum_thrust", PATTERN, momentum_thrust,
             "Three consecutive bodies in one direction", (TRENDING,), 0.60),
]

BY_NAME: Dict[str, Strategy] = {s.name: s for s in REGISTRY}
FAMILIES: Tuple[str, ...] = (TREND, REVERSION, BREAKOUT, VOLUME, STRUCTURE, PATTERN)
BASE_WEIGHTS: Dict[str, float] = {s.name: s.base_weight for s in REGISTRY}


def strategy_names() -> List[str]:
    return [s.name for s in REGISTRY]


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------

def classify_regime(ctx: Context, i: int) -> Regime:
    """Classify market state on trend and volatility axes.

    Trend uses ADX, which measures strength without direction. Volatility uses
    the instrument's own bandwidth percentile, so "high volatility" means high
    *for this symbol* rather than against some universal number.
    """
    adx_v = ctx.at("adx", i)
    bwp = ctx.at("bw_pctile", i)
    slope = ctx.at("ema21_slope", i)
    pdi, mdi = ctx.at("plus_di", i), ctx.at("minus_di", i)

    if not _ok(adx_v):
        trend = TRANSITIONAL
    elif adx_v >= 25:
        trend = TRENDING
    elif adx_v <= 17:
        trend = RANGING
    else:
        trend = TRANSITIONAL

    if not _ok(bwp):
        vol = "normal"
    elif bwp >= 0.75:
        vol = "high"
    elif bwp <= 0.25:
        vol = "low"
    else:
        vol = "normal"

    direction = 0
    if _ok(pdi, mdi) and abs(pdi - mdi) > 4:
        direction = 1 if pdi > mdi else -1
    elif _ok(slope) and abs(slope) > 0.05:
        direction = 1 if slope > 0 else -1

    return Regime(trend=trend, volatility=vol, direction=direction,
                  adx=adx_v if _ok(adx_v) else float("nan"),
                  detail="ADX %.0f, bandwidth percentile %s" % (
                      adx_v if _ok(adx_v) else 0.0,
                      "%.0f" % (bwp * 100) if _ok(bwp) else "n/a"))


# Prior multipliers applied when a strategy matches the current regime. These
# are only a starting point; measured per-instrument performance overrides them
# once enough samples exist.
_REGIME_BONUS = 1.30
_REGIME_PENALTY = 0.55


def evaluate(ctx: Context, i: int, weights: Optional[Dict[str, float]] = None,
             learned: Optional[Dict[str, float]] = None,
             regime: Optional[Regime] = None):
    """Run every strategy at bar `i` and return (signals, composite, regime).

    The composite is a weighted mean in [-1, +1]. It is a *score*, not a
    probability; converting it into one is the calibrator's job.

    Agreement inside a single family is discounted. Ten trend strategies
    nodding along is one observation about trend, not ten independent
    confirmations, and treating it as ten is how a composite becomes
    overconfident.
    """
    base = dict(BASE_WEIGHTS)
    if weights:
        base.update(weights)
    reg = regime or classify_regime(ctx, i)

    signals: List[Signal] = []
    for strat in REGISTRY:
        sig = strat.fn(ctx, i)
        w = base.get(strat.name, 1.0)
        if strat.suits:
            w *= _REGIME_BONUS if reg.trend in strat.suits else _REGIME_PENALTY
        if learned and strat.name in learned:
            w *= max(0.0, learned[strat.name])
        sig.weight *= w
        sig.family = strat.family
        signals.append(sig)

    # Discount within-family agreement: the first voice in a family counts
    # fully, later ones progressively less.
    per_family: Dict[str, List[Signal]] = {}
    for sig in signals:
        if sig.weight > 0 and abs(sig.score) > 0.05:
            per_family.setdefault(sig.family, []).append(sig)

    total_w = 0.0
    total_c = 0.0
    for fam, group in per_family.items():
        group.sort(key=lambda s: -abs(s.contribution))
        for rank, sig in enumerate(group):
            damp = 1.0 / (1.0 + 0.45 * rank)
            total_c += sig.contribution * damp
            total_w += sig.weight * damp

    composite = (total_c / total_w) if total_w > 0 else 0.0
    return signals, _clamp(composite), reg
