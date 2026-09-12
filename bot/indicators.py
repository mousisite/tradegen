"""Technical indicators, implemented in pure NumPy.

Every function takes and returns 1-D float arrays of the same length as the
input, front-padded with NaN where the indicator is not yet defined. Keeping
the lengths aligned means the backtester can index bar `i` across every
indicator without bookkeeping.

Rolling window operations use `sliding_window_view` rather than Python loops.
With two dozen strategies each needing their own back-test over tens of
thousands of bars, that choice is the difference between a responsive tool and
one that stalls on every run.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _nan(n: int) -> np.ndarray:
    return np.full(n, np.nan, dtype=float)


def _win(x: np.ndarray, n: int):
    """Sliding windows of width n, or None when the series is too short."""
    x = np.asarray(x, dtype=float)
    if n <= 0 or len(x) < n:
        return None
    return sliding_window_view(x, n)


# ---------------------------------------------------------------------------
# Moving averages and smoothing
# ---------------------------------------------------------------------------

def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if len(x) < n or n <= 0:
        return out
    c = np.cumsum(np.insert(np.nan_to_num(x, nan=0.0), 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Exponential moving average, seeded with an SMA of the first n values.

    Leading NaNs are skipped rather than averaged. That matters whenever one
    smoother is chained onto another, as TSI does: seeding from an all-NaN
    window yields a NaN seed that silently poisons the entire output.
    """
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 0:
        return out
    finite = np.flatnonzero(np.isfinite(x))
    if len(finite) == 0:
        return out
    start = int(finite[0])
    if len(x) - start < n:
        return out

    k = 2.0 / (n + 1.0)
    seed_idx = start + n - 1
    prev = float(np.nanmean(x[start:start + n]))
    out[seed_idx] = prev
    for i in range(seed_idx + 1, len(x)):
        v = x[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = v * k + prev * (1.0 - k)
        out[i] = prev
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI, ATR and ADX). Alpha = 1/n, not 2/(n+1)."""
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 0:
        return out
    finite = np.flatnonzero(np.isfinite(x))
    if len(finite) == 0:
        return out
    start = int(finite[0])
    if len(x) - start < n:
        return out

    seed_idx = start + n - 1
    prev = float(np.nanmean(x[start:start + n]))
    out[seed_idx] = prev
    for i in range(seed_idx + 1, len(x)):
        v = 0.0 if np.isnan(x[i]) else x[i]
        prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    out = _nan(len(np.asarray(x)))
    w = _win(x, n)
    if w is not None:
        out[n - 1:] = w.max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    out = _nan(len(np.asarray(x)))
    w = _win(x, n)
    if w is not None:
        out[n - 1:] = w.min(axis=1)
    return out


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    out = _nan(len(np.asarray(x)))
    w = _win(x, n)
    if w is not None:
        out[n - 1:] = w.std(axis=1)
    return out


# ---------------------------------------------------------------------------
# Oscillators
# ---------------------------------------------------------------------------

def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's RSI. 0-100; >70 conventionally overbought, <30 oversold."""
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    if len(close) < n + 1:
        return out
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = wilder(gain, n)
    al = wilder(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(al > 0, ag / al, np.inf)
        out = 100.0 - (100.0 / (1.0 + rs))
    out[np.isnan(ag)] = np.nan
    out[np.isinf(rs) & ~np.isnan(ag)] = 100.0
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    line = ema(close, fast) - ema(close, slow)
    valid = ~np.isnan(line)
    sig = _nan(len(close))
    if valid.sum() >= signal:
        idx = np.where(valid)[0]
        sig[idx] = ema(line[valid], signal)
    return line, sig, line - sig


def stochastic(high, low, close, n: int = 14, d: int = 3):
    """Returns (%K, %D). Where price sits inside its recent range."""
    hh = rolling_max(high, n)
    ll = rolling_min(low, n)
    rng = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where(rng > 0, 100.0 * (np.asarray(close, float) - ll) / rng, 50.0)
    k[np.isnan(hh)] = np.nan
    return k, sma(k, d)


def williams_r(high, low, close, n: int = 14) -> np.ndarray:
    """Williams %R: -100 at the bottom of the range, 0 at the top."""
    hh = rolling_max(high, n)
    ll = rolling_min(low, n)
    rng = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(rng > 0, -100.0 * (hh - np.asarray(close, float)) / rng, -50.0)
    out[np.isnan(hh)] = np.nan
    return out


def cci(high, low, close, n: int = 20) -> np.ndarray:
    """Commodity Channel Index. Beyond +-100 signals a strong move."""
    tp = (np.asarray(high, float) + np.asarray(low, float) + np.asarray(close, float)) / 3.0
    ma = sma(tp, n)
    out = _nan(len(tp))
    w = _win(tp, n)
    if w is None:
        return out
    # Mean absolute deviation, which is what CCI uses rather than std.
    mad = np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n - 1:] = np.where(mad > 0, (tp[n - 1:] - ma[n - 1:]) / (0.015 * mad), 0.0)
    return out


def roc(close: np.ndarray, n: int = 10) -> np.ndarray:
    """Rate of change, in percent."""
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    if len(close) <= n:
        return out
    prev = close[:-n]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n:] = np.where(prev != 0, (close[n:] - prev) / np.abs(prev) * 100.0, np.nan)
    return out


def cmo(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Chande Momentum Oscillator. Like RSI but unsmoothed and centred on zero."""
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    delta = np.diff(close, prepend=close[0])
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    wu, wd = _win(up, n), _win(dn, n)
    if wu is None:
        return out
    su, sd = wu.sum(axis=1), wd.sum(axis=1)
    tot = su + sd
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n - 1:] = np.where(tot > 0, 100.0 * (su - sd) / tot, 0.0)
    return out


def tsi(close: np.ndarray, long: int = 25, short: int = 13) -> np.ndarray:
    """True Strength Index: double-smoothed momentum, a cleaner trend read."""
    close = np.asarray(close, dtype=float)
    m = np.diff(close, prepend=close[0])
    smooth = ema(ema(m, long), short)
    absol = ema(ema(np.abs(m), long), short)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(absol > 0, 100.0 * smooth / absol, np.nan)


def zscore(close: np.ndarray, n: int = 20) -> np.ndarray:
    """How many standard deviations price sits from its own mean."""
    close = np.asarray(close, dtype=float)
    ma = sma(close, n)
    sd = rolling_std(close, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, (close - ma) / sd, 0.0)


# ---------------------------------------------------------------------------
# Volatility and channels
# ---------------------------------------------------------------------------

def true_range(high, low, close) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    return np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])


def atr(high, low, close, n: int = 14) -> np.ndarray:
    """Average True Range: the volatility unit every stop and target is sized in."""
    return wilder(true_range(high, low, close), n)


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0):
    """Returns (upper, middle, lower, percent_b, bandwidth)."""
    close = np.asarray(close, dtype=float)
    mid = sma(close, n)
    sd = rolling_std(close, n)
    upper = mid + k * sd
    lower = mid - k * sd
    width = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_b = np.where(width > 0, (close - lower) / width, 0.5)
        bandwidth = np.where(mid > 0, width / mid, np.nan)
    pct_b[np.isnan(mid)] = np.nan
    return upper, mid, lower, pct_b, bandwidth


def keltner(high, low, close, n: int = 20, mult: float = 2.0):
    """Keltner channels: an EMA with ATR bands. Returns (upper, mid, lower).

    Paired with Bollinger bands this detects the classic squeeze: Bollinger
    inside Keltner means volatility has compressed unusually far.
    """
    mid = ema(close, n)
    a = atr(high, low, close, n)
    return mid + mult * a, mid, mid - mult * a


def donchian(high, low, n: int = 20):
    """Donchian channel: the rolling high/low that defines a breakout."""
    up = rolling_max(high, n)
    dn = rolling_min(low, n)
    return up, (up + dn) / 2.0, dn


def supertrend(high, low, close, n: int = 10, mult: float = 3.0):
    """Supertrend: an ATR-banded trend follower. Returns (line, direction).

    Direction is +1 in an uptrend, -1 in a downtrend. The band ratchets: it
    only ever moves in the direction of the trend until price closes through
    it, which is what stops it whipsawing on every small pullback.
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    a = atr(high, low, close, n)
    hl2 = (high + low) / 2.0
    upper_basic = hl2 + mult * a
    lower_basic = hl2 - mult * a

    line = _nan(len(close))
    direction = np.zeros(len(close), dtype=float)
    fin_up = np.copy(upper_basic)
    fin_dn = np.copy(lower_basic)

    start = int(np.argmax(~np.isnan(a))) if np.any(~np.isnan(a)) else len(close)
    if start >= len(close) - 1:
        return line, direction

    direction[start] = 1.0
    line[start] = fin_dn[start]
    for i in range(start + 1, len(close)):
        if np.isnan(upper_basic[i]):
            direction[i] = direction[i - 1]
            line[i] = line[i - 1]
            continue
        fin_up[i] = (min(upper_basic[i], fin_up[i - 1])
                     if close[i - 1] <= fin_up[i - 1] else upper_basic[i])
        fin_dn[i] = (max(lower_basic[i], fin_dn[i - 1])
                     if close[i - 1] >= fin_dn[i - 1] else lower_basic[i])
        if direction[i - 1] > 0:
            direction[i] = -1.0 if close[i] < fin_dn[i] else 1.0
        else:
            direction[i] = 1.0 if close[i] > fin_up[i] else -1.0
        line[i] = fin_dn[i] if direction[i] > 0 else fin_up[i]
    return line, direction


def parabolic_sar(high, low, af_start: float = 0.02, af_step: float = 0.02,
                  af_max: float = 0.2):
    """Parabolic SAR. Returns (sar, direction).

    The acceleration factor increases each time the trend makes a new extreme,
    which is what makes the stop tighten as a move matures.
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    n = len(high)
    sar = _nan(n)
    direction = np.zeros(n, dtype=float)
    if n < 3:
        return sar, direction

    up = True
    af = af_start
    ep = high[0]
    sar[0] = low[0]
    direction[0] = 1.0

    for i in range(1, n):
        prev = sar[i - 1]
        cur = prev + af * (ep - prev)
        if up:
            cur = min(cur, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < cur:                       # trend flips down
                up = False
                cur = ep
                ep = low[i]
                af = af_start
            elif high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
        else:
            cur = max(cur, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > cur:                      # trend flips up
                up = True
                cur = ep
                ep = high[i]
                af = af_start
            elif low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
        sar[i] = cur
        direction[i] = 1.0 if up else -1.0
    return sar, direction


def historical_volatility(close: np.ndarray, n: int = 20) -> np.ndarray:
    """Standard deviation of log returns over n bars, as a percentage."""
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    with np.errstate(divide="ignore", invalid="ignore"):
        logret = np.diff(np.log(np.where(close > 0, close, np.nan)), prepend=np.nan)
    w = _win(np.nan_to_num(logret, nan=0.0), n)
    if w is None:
        return out
    out[n - 1:] = w.std(axis=1) * 100.0
    return out


# ---------------------------------------------------------------------------
# Trend strength and direction
# ---------------------------------------------------------------------------

def adx(high, low, close, n: int = 14):
    """Average Directional Index: trend *strength*, direction-agnostic.

    Returns (adx, plus_di, minus_di). ADX above ~20-25 means a trend is
    actually present, which is what separates a momentum setup from chop.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr_n = wilder(true_range(high, low, close), n)
    plus_n = wilder(plus_dm, n)
    minus_n = wilder(minus_dm, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(tr_n > 0, 100.0 * plus_n / tr_n, np.nan)
        minus_di = np.where(tr_n > 0, 100.0 * minus_n / tr_n, np.nan)
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    return wilder(np.nan_to_num(dx, nan=0.0), n), plus_di, minus_di


def aroon(high, low, n: int = 25):
    """Aroon up/down: how recently the n-bar extreme was set, as a percentage."""
    hw, lw = _win(high, n + 1), _win(low, n + 1)
    out_u = _nan(len(np.asarray(high)))
    out_d = _nan(len(np.asarray(low)))
    if hw is None or lw is None:
        return out_u, out_d
    since_high = n - hw.argmax(axis=1)
    since_low = n - lw.argmin(axis=1)
    out_u[n:] = 100.0 * (n - since_high) / n
    out_d[n:] = 100.0 * (n - since_low) / n
    return out_u, out_d


def ichimoku(high, low, tenkan_n: int = 9, kijun_n: int = 26, senkou_n: int = 52,
             shift: int = 26):
    """Ichimoku cloud. Returns (tenkan, kijun, senkou_a, senkou_b).

    The senkou spans are returned already shifted forward, so `senkou_a[i]` is
    the cloud value *in effect at bar i*. Comparing price to an unshifted span
    is a common and badly misleading implementation error.
    """
    tenkan = (rolling_max(high, tenkan_n) + rolling_min(low, tenkan_n)) / 2.0
    kijun = (rolling_max(high, kijun_n) + rolling_min(low, kijun_n)) / 2.0
    span_a_raw = (tenkan + kijun) / 2.0
    span_b_raw = (rolling_max(high, senkou_n) + rolling_min(low, senkou_n)) / 2.0

    n = len(np.asarray(high))
    span_a, span_b = _nan(n), _nan(n)
    if n > shift:
        span_a[shift:] = span_a_raw[:-shift]
        span_b[shift:] = span_b_raw[:-shift]
    return tenkan, kijun, span_a, span_b


def linreg(close: np.ndarray, n: int = 20):
    """Rolling linear regression. Returns (slope_pct_per_bar, r_squared).

    Slope is normalised by price so it is comparable across instruments, and
    r-squared says how orderly the trend is. A steep but noisy move and a
    shallow but clean one are very different trades.
    """
    close = np.asarray(close, dtype=float)
    slope = _nan(len(close))
    r2 = _nan(len(close))
    w = _win(close, n)
    if w is None:
        return slope, r2

    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    denom = float((xc ** 2).sum())
    ymean = w.mean(axis=1)
    yc = w - ymean[:, None]
    b = (yc * xc).sum(axis=1) / denom

    ss_tot = (yc ** 2).sum(axis=1)
    ss_res = ((yc - b[:, None] * xc) ** 2).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope[n - 1:] = np.where(ymean != 0, b / np.abs(ymean) * 100.0, np.nan)
        r2[n - 1:] = np.where(ss_tot > 0, 1.0 - ss_res / ss_tot, np.nan)
    return slope, r2


def slope_pct(x: np.ndarray, lookback: int = 5) -> np.ndarray:
    """Percent change of a series over `lookback` bars. Used to read EMA tilt."""
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if len(x) <= lookback:
        return out
    prev = x[:-lookback]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[lookback:] = np.where(prev != 0, (x[lookback:] - prev) / np.abs(prev) * 100.0, np.nan)
    return out


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def session_vwap(high, low, close, volume, session_ids: np.ndarray) -> np.ndarray:
    """Volume-weighted average price, reset at each new session.

    Anchoring matters: a VWAP that never resets is a slow moving average and
    useless intraday, which is the single most common way this indicator gets
    implemented wrong.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    typical = (high + low + close) / 3.0
    out = _nan(len(close))
    cum_pv = cum_v = 0.0
    current = None
    for i in range(len(close)):
        if session_ids[i] != current:
            current = session_ids[i]
            cum_pv = cum_v = 0.0
        v = 0.0 if np.isnan(volume[i]) else volume[i]
        p = typical[i]
        if not np.isnan(p):
            cum_pv += p * v
            cum_v += v
        out[i] = (cum_pv / cum_v) if cum_v > 0 else p
    return out


def vwap_bands(high, low, close, volume, session_ids: np.ndarray, k: float = 1.0):
    """Session VWAP with standard-deviation bands. Returns (vwap, upper, lower).

    The bands are where intraday mean-reversion traders actually fade a move,
    which makes them more useful than VWAP alone.
    """
    vw = session_vwap(high, low, close, volume, session_ids)
    close = np.asarray(close, dtype=float)
    dev = _nan(len(close))
    start = 0
    for i in range(1, len(close) + 1):
        if i == len(close) or session_ids[i] != session_ids[start]:
            seg = slice(start, i)
            diff = close[seg] - vw[seg]
            run = np.sqrt(np.cumsum(diff ** 2) / np.arange(1, len(diff) + 1))
            dev[seg] = run
            start = i
    return vw, vw + k * dev, vw - k * dev


def rolling_vwap(high, low, close, volume, n: int = 20):
    """Volume-weighted average price over a trailing window, with bands.

    Session VWAP is meaningless on bars at or above daily resolution: each bar
    is its own session, so the "average" collapses to that bar's own typical
    price and the deviation bands collapse to exactly one band every time.
    A trailing-window VWAP is what traders actually use on those charts.

    Returns (vwap, upper, lower).
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.nan_to_num(np.asarray(volume, dtype=float), nan=0.0)

    typical = (high + low + close) / 3.0
    pv = typical * volume
    sum_pv = _nan(len(close))
    sum_v = _nan(len(close))
    wp, wv = _win(pv, n), _win(volume, n)
    if wp is None:
        return _nan(len(close)), _nan(len(close)), _nan(len(close))
    sum_pv[n - 1:] = wp.sum(axis=1)
    sum_v[n - 1:] = wv.sum(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        # Zero-volume windows fall back to a plain average of typical price,
        # which keeps thinly traded instruments usable instead of NaN.
        vwap = np.where(sum_v > 0, sum_pv / sum_v, sma(typical, n))

    dev = _nan(len(close))
    wc = _win(close, n)
    if wc is not None:
        dev[n - 1:] = np.sqrt(((wc - vwap[n - 1:, None]) ** 2).mean(axis=1))
    return vwap, vwap + dev, vwap - dev


def relative_volume(volume: np.ndarray, n: int = 20) -> np.ndarray:
    """Current bar volume divided by its recent average. 1.0 == typical."""
    volume = np.asarray(volume, dtype=float)
    avg = sma(volume, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((avg > 0) & ~np.isnan(avg), volume / avg, np.nan)


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """On-Balance Volume: cumulative volume signed by the direction of close."""
    close = np.asarray(close, dtype=float)
    volume = np.nan_to_num(np.asarray(volume, dtype=float), nan=0.0)
    sign = np.sign(np.diff(close, prepend=close[0]))
    return np.cumsum(sign * volume)


def money_flow_index(high, low, close, volume, n: int = 14) -> np.ndarray:
    """Money Flow Index: RSI weighted by volume. Volume-aware overbought read."""
    tp = (np.asarray(high, float) + np.asarray(low, float) + np.asarray(close, float)) / 3.0
    raw = tp * np.nan_to_num(np.asarray(volume, float), nan=0.0)
    delta = np.diff(tp, prepend=tp[0])
    pos = np.where(delta > 0, raw, 0.0)
    neg = np.where(delta < 0, raw, 0.0)
    wp, wn = _win(pos, n), _win(neg, n)
    out = _nan(len(tp))
    if wp is None:
        return out
    sp, sn = wp.sum(axis=1), wn.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(sn > 0, sp / sn, np.inf)
        out[n - 1:] = np.where(np.isinf(ratio), 100.0, 100.0 - 100.0 / (1.0 + ratio))
    return out


def accum_dist(high, low, close, volume) -> np.ndarray:
    """Accumulation/Distribution line: where each bar closed within its range."""
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    volume = np.nan_to_num(np.asarray(volume, float), nan=0.0)
    rng = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        mult = np.where(rng > 0, ((close - low) - (high - close)) / rng, 0.0)
    return np.cumsum(mult * volume)


# ---------------------------------------------------------------------------
# Price structure helpers
# ---------------------------------------------------------------------------

def pivot_points(prev_high: float, prev_low: float, prev_close: float):
    """Classic floor-trader pivots from the prior session.

    Returns a dict of pivot, r1-r3 and s1-s3. Widely watched, which is much of
    why they work at all.
    """
    p = (prev_high + prev_low + prev_close) / 3.0
    rng = prev_high - prev_low
    return {
        "pivot": p,
        "r1": 2 * p - prev_low, "s1": 2 * p - prev_high,
        "r2": p + rng, "s2": p - rng,
        "r3": prev_high + 2 * (p - prev_low), "s3": prev_low - 2 * (prev_high - p),
    }


def body_and_wicks(open_, high, low, close):
    """Candle anatomy as fractions of range. Returns (body, upper_wick, lower_wick).

    Values are signed for body (positive means a close above the open) and
    always positive for wicks. Used by the candlestick pattern strategies.
    """
    open_ = np.asarray(open_, float)
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    rng = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        body = np.where(rng > 0, (close - open_) / rng, 0.0)
        upper = np.where(rng > 0, (high - np.maximum(open_, close)) / rng, 0.0)
        lower = np.where(rng > 0, (np.minimum(open_, close) - low) / rng, 0.0)
    return body, upper, lower
