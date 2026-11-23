"""Risk: how much this can hurt, measured rather than asserted.

Everything here is computed from actual return history, with the sample size
attached. A volatility figure from thirty days of data and one from three years
look identical on a screen and mean very different things, so the period is
always reported.

A deliberate omission: there is no single "risk score". Volatility, drawdown,
tail loss and correlation measure different dangers, and collapsing them into
one number destroys the only useful information they carry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .market import Bars, DataError, fetch_bars

TRADING_DAYS = 252
COIN_DAYS = 365           # a coin trades every day, round the clock
_STILL = 1e-12            # returns closer together than this are rounding, not moves


@dataclass
class RiskProfile:
    """Measured risk characteristics of one instrument."""
    symbol: str
    samples: int
    period: str
    volatility: Optional[float] = None          # annualised
    downside_volatility: Optional[float] = None
    max_drawdown: Optional[float] = None
    drawdown_days: Optional[int] = None
    current_drawdown: Optional[float] = None
    var_95: Optional[float] = None              # daily, historical
    var_99: Optional[float] = None
    expected_shortfall: Optional[float] = None
    beta: Optional[float] = None
    correlation_spy: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    best_day: Optional[float] = None
    worst_day: Optional[float] = None
    up_days: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    reliable: bool = False
    days_per_year: int = TRADING_DAYS           # what the volatility is annualised over


def _closes(bars: Bars) -> np.ndarray:
    """The closes to measure returns on, with dividends counted.

    A share drops by its dividend on the day it goes ex, and whoever holds it
    is paid that much. Left uncounted, every such drop was a loss nobody had,
    and a 5% payer's Sharpe ratio read below zero.
    """
    close = np.asarray(bars.close, dtype=float)
    factor = getattr(bars, "dividend_factor", None)
    if factor is None or len(factor) != len(close):
        return close
    return close * np.asarray(factor, dtype=float)


def daily_returns(bars: Bars) -> np.ndarray:
    """Simple returns from close to close, with non-finite values removed."""
    close = _closes(bars)
    if len(close) < 3:
        return np.array([])
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(close) / close[:-1]
    return rets[np.isfinite(rets)]


def _closes_by_day(bars) -> Dict[int, float]:
    """The last close of each trading day, keyed by the bar's own day number."""
    out: Dict[int, float] = {}
    for day, close in zip(bars.session_id, _closes(bars)):
        if np.isfinite(close):
            out[int(day)] = float(close)
    return out


def aligned_returns(bar_sets: List[Bars]) -> np.ndarray:
    """Returns for several instruments over the days they all traded.

    A row per instrument, a column per shared day. Trimming two series to the
    same length from the end pairs a coin's Sunday with a stock's Friday,
    which is how Bitcoin and NVIDIA came out uncorrelated: each return here is
    from one shared trading day's close to the next, so a weekend's crypto
    move lands on the Monday the stock market reopened.
    """
    maps = [_closes_by_day(b) for b in bar_sets]
    days = sorted(set.intersection(*[set(m) for m in maps])) if maps else []
    if len(days) < 3:
        return np.empty((len(maps), 0))
    closes = np.array([[m[d] for d in days] for m in maps], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(closes, axis=1) / closes[:, :-1]
    return rets[:, np.all(np.isfinite(rets), axis=0)]


def _has_days(bars) -> bool:
    ids = getattr(bars, "session_id", None)
    return ids is not None and len(ids) == len(bars.close)


def profile(symbol: str, interval: str = "1d", benchmark: str = "SPY") -> RiskProfile:
    """Measure an instrument's risk against its own history and the market."""
    bars = fetch_bars(symbol, interval)
    rets = daily_returns(bars)
    out = RiskProfile(symbol=bars.symbol, samples=len(rets),
                      period="%d %s bars" % (len(bars), interval))

    if len(rets) < 30:
        out.notes.append("Only %d usable returns. Too few to measure risk."
                         % len(rets))
        return out

    out.reliable = len(rets) >= 120
    # Annualisation factor depends on the bar size and on how long the thing
    # trades: a stock 6.5 hours on 252 days, a coin all 24 on all 365.
    coin = getattr(bars, "asset_class", "") == "crypto"
    days, hours = (COIN_DAYS, 24) if coin else (TRADING_DAYS, 6.5)
    out.days_per_year = days
    per_year = days * {"1h": hours, "30m": hours * 2, "15m": hours * 4,
                       "5m": hours * 12}.get(interval, 1)
    scale = math.sqrt(per_year)

    out.volatility = float(np.std(rets, ddof=1)) * scale
    downside = rets[rets < 0]
    if len(downside) > 5:
        out.downside_volatility = float(np.std(downside, ddof=1)) * scale

    out.best_day = float(np.max(rets))
    out.worst_day = float(np.min(rets))
    out.up_days = float(np.mean(rets > 0))

    # Historical value at risk: the actual loss exceeded 5% and 1% of the time.
    # Historical rather than parametric, because returns are not normal and the
    # normal assumption understates exactly the tail that matters.
    out.var_95 = float(np.percentile(rets, 5))
    out.var_99 = float(np.percentile(rets, 1))
    tail = rets[rets <= out.var_95]
    if len(tail):
        out.expected_shortfall = float(np.mean(tail))

    # From the first close, which can itself be the top of a fall.
    equity = np.concatenate([[1.0], np.cumprod(1.0 + rets)])
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    out.max_drawdown = float(np.min(drawdown))
    out.current_drawdown = float(drawdown[-1])

    trough = int(np.argmin(drawdown))
    prior_peak = int(np.argmax(equity[:trough + 1])) if trough > 0 else 0
    out.drawdown_days = trough - prior_peak

    mean_ret = float(np.mean(rets))
    if out.volatility and out.volatility > 0:
        excess = mean_ret * per_year - 0.042
        out.sharpe = excess / out.volatility
    if out.downside_volatility and out.downside_volatility > 0:
        out.sortino = (mean_ret * per_year - 0.042) / out.downside_volatility

    if benchmark and bars.symbol.upper() != benchmark.upper():
        try:
            bench = fetch_bars(benchmark, interval)
            if _has_days(bars) and _has_days(bench):
                a, b = aligned_returns([bars, bench])
            else:
                a, b = _align(rets, daily_returns(bench))
            if len(a) >= 30:
                var_b = float(np.var(b, ddof=1))
                if var_b > 0:
                    out.beta = float(np.cov(a, b, ddof=1)[0][1] / var_b)
                # A price that never moves goes up and down with nothing.
                if var_b > 0 and np.ptp(a) > _STILL:
                    out.correlation_spy = float(np.corrcoef(a, b)[0][1])
        except (DataError, Exception):
            out.notes.append("Could not compare against %s." % benchmark)

    _add_notes(out)
    return out


def _align(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Trim two return series to the same length from the most recent end.

    Crude but safe. Aligning on timestamps would be better; taking the common
    tail at least never pairs a Monday with a Wednesday for instruments that
    share a calendar.
    """
    n = min(len(a), len(b))
    return a[-n:], b[-n:]


def _add_notes(r: RiskProfile) -> None:
    if r.volatility:
        if r.volatility > 0.80:
            r.notes.append("Annualised volatility of %.0f%% is extreme. A position "
                           "sized as if this were a normal stock will not behave "
                           "like one." % (r.volatility * 100))
        elif r.volatility > 0.45:
            r.notes.append("Annualised volatility of %.0f%% is high."
                           % (r.volatility * 100))
    if r.max_drawdown is not None and r.max_drawdown < -0.4:
        r.notes.append("This has fallen %.0f%% peak to trough within the measured "
                       "period. Ask whether you would have held through that."
                       % (abs(r.max_drawdown) * 100))
    if r.var_95 is not None:
        r.notes.append("On the worst one day in twenty it lost %.1f%% or more."
                       % (abs(r.var_95) * 100))
    if r.beta is not None:
        if r.beta > 1.5:
            r.notes.append("Beta of %.2f means it tends to amplify market moves."
                           % r.beta)
        elif r.beta < 0.5:
            r.notes.append("Beta of %.2f means it moves largely independently of "
                           "the market." % r.beta)
    if not r.reliable:
        r.notes.append("Based on %d returns. Treat these figures as indicative."
                       % r.samples)


def _money(value: float) -> str:
    """Cash amounts the way a person would say them."""
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return "$%s" % format(int(round(value)), ",")
    return "$%.2f" % value


def position_risk(entry: float, stop: float, quantity: float,
                  account: float, profile_: Optional[RiskProfile] = None) -> Dict:
    """What a single position actually risks, in cash and in context."""
    per_unit = abs(entry - stop)
    at_risk = per_unit * quantity
    notional = entry * quantity
    out = {
        "per_unit_risk": per_unit,
        "cash_at_risk": at_risk,
        "notional": notional,
        "pct_of_account": (at_risk / account * 100) if account else None,
        "exposure_pct": (notional / account * 100) if account else None,
        "stop_distance_pct": (per_unit / entry * 100) if entry else None,
        "warnings": [],
    }

    if out["pct_of_account"] and out["pct_of_account"] > 2:
        out["warnings"].append(
            "This risks %.1f%% of the account on one trade. Most survivable "
            "approaches keep it under 2%%." % out["pct_of_account"])
    if out["exposure_pct"] and out["exposure_pct"] > 40:
        out["warnings"].append(
            "This position would be %.0f%% of the account. Concentration that "
            "high makes a single surprise decisive." % out["exposure_pct"])

    # A stop states what you intend to lose. A gap states what you actually lose
    # when the market reopens past it, and that is the loss that ends accounts.
    # Measured against this instrument's own worst day rather than an assumed
    # number, so the figure is defensible for whatever is being traded.
    if profile_ and profile_.worst_day is not None and account and entry:
        adverse = abs(profile_.worst_day)
        long_side = entry > stop
        gap_fill = entry * (1 - adverse) if long_side else entry * (1 + adverse)
        # If the gap does not reach the stop the stop fills normally, so the
        # loss cannot exceed what was already at risk.
        gap_loss = max(at_risk, abs(entry - gap_fill) * quantity)
        out["gap_loss"] = gap_loss
        out["gap_move"] = adverse
        out["gap_pct_of_account"] = gap_loss / account * 100
        out["gap_source"] = ("worst single move in %d measured returns"
                             % profile_.samples)
        if gap_loss > at_risk * 1.5 and out["gap_pct_of_account"] > 2:
            out["warnings"].append(
                "The stop caps this at %s, but a move the size of this "
                "instrument's worst day (%.1f%%) would open past it and cost "
                "about %s, or %.1f%% of the account. The stop is not a "
                "guarantee." % (_money(at_risk), adverse * 100,
                                _money(gap_loss), out["gap_pct_of_account"]))

    if profile_ and profile_.volatility and entry:
        daily_vol = profile_.volatility / math.sqrt(profile_.days_per_year)
        moves = out["stop_distance_pct"] / 100 / daily_vol if daily_vol else None
        if moves is not None:
            out["stop_in_daily_moves"] = moves
            if moves < 1.0:
                out["warnings"].append(
                    "The stop is only %.1f of a typical daily move away. Ordinary "
                    "noise will take you out before the idea has a chance." % moves)
            elif moves > 6:
                out["warnings"].append(
                    "The stop is %.1f typical daily moves away, so it may take a "
                    "long time and a large loss to be proven wrong." % moves)
    return out


def correlation_matrix(symbols: List[str], interval: str = "1d") -> Dict:
    """How together a set of instruments move.

    The number that matters for a portfolio. Eight positions that all correlate
    at 0.9 are one position held eight times, and will fall together on the day
    it matters.
    """
    loaded, kept, failed = {}, [], []
    for sym in symbols:
        try:
            bars = fetch_bars(sym, interval)
            if len(daily_returns(bars)) >= 30:
                loaded[sym] = bars
                kept.append(sym)
            else:
                failed.append(sym)
        except Exception:
            failed.append(sym)

    if len(kept) < 2:
        return {"usable": False, "symbols": kept, "failed": failed,
                "reason": "Need at least two instruments with enough history."}

    if all(_has_days(loaded[s]) for s in kept):
        returns = aligned_returns([loaded[s] for s in kept])
    else:
        series = [daily_returns(loaded[s]) for s in kept]
        n = min(len(r) for r in series)
        returns = np.vstack([r[-n:] for r in series])
    n = returns.shape[1]
    if n < 30:
        return {"usable": False, "symbols": kept, "failed": failed,
                "reason": "Fewer than 30 trading days in common."}

    # A price that never moves has no correlation with anything: the sum
    # divides nothing by nothing, and "nan" reached the page.
    moving = [k for k in range(len(kept)) if np.ptp(returns[k]) > _STILL]
    if len(moving) < len(kept):
        failed += [s for k, s in enumerate(kept) if k not in moving]
        kept, returns = [kept[k] for k in moving], returns[moving]
        if len(kept) < 2:
            return {"usable": False, "symbols": kept, "failed": failed,
                    "reason": "Need at least two instruments whose price moves."}
    matrix = np.corrcoef(returns)

    pairs = []
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            pairs.append({"a": kept[i], "b": kept[j], "value": float(matrix[i][j])})
    pairs.sort(key=lambda p: -abs(p["value"]))

    off_diagonal = [p["value"] for p in pairs]
    average = float(np.mean(off_diagonal)) if off_diagonal else 0.0

    notes = []
    if average > 0.7:
        notes.append("Average correlation of %.2f means these are close to the "
                     "same bet. Holding all of them is not diversification."
                     % average)
    elif average > 0.45:
        notes.append("Average correlation of %.2f. There is real overlap here; "
                     "expect them to fall together in a bad week." % average)
    else:
        notes.append("Average correlation of %.2f. These are genuinely different "
                     "exposures." % average)

    if pairs and abs(pairs[0]["value"]) > 0.85:
        notes.append("%s and %s move almost identically at %.2f. Holding both "
                     "adds risk without adding diversification."
                     % (pairs[0]["a"], pairs[0]["b"], pairs[0]["value"]))

    return {"usable": True, "symbols": kept, "failed": failed,
            "matrix": matrix.tolist(), "pairs": pairs, "average": average,
            "samples": n, "notes": notes}
