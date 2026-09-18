"""Turn a composite score into a measured probability.

The strategy engine outputs a score. A score is not a probability, and quoting
one as if it were is the central dishonesty in most retail trading tools.

This module replays the exact same strategy code across the instrument's own
recent history, records whether each setup reached its target before its stop,
and reports the observed hit rate with a confidence interval and a sample size.
When there is not enough evidence, it says so instead of inventing a number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .strategies import Context, evaluate


@dataclass
class Outcome:
    """Result of one simulated trade.

    `r_multiple` is the return expressed in units of risk: -1.0 is a full stop
    out, +reward_risk is a target hit, and a timed-out trade carries whatever
    it was actually worth at exit.
    """
    score: float
    won: bool
    r_multiple: float
    bars_held: int
    index: int = 0
    cost_r: float = 0.0


@dataclass
class Calibration:
    """Measured performance of the scoring model on this instrument."""
    samples: int
    bucket_low: float
    bucket_high: float
    bucket_samples: int
    hit_rate: Optional[float]
    ci_low: Optional[float]
    ci_high: Optional[float]
    breakeven: float
    expectancy_r: Optional[float]
    horizon: int
    reward_risk: float
    baseline_rate: Optional[float]
    reliable: bool
    note: str
    curve: List[Dict] = field(default_factory=list)
    avg_bars_held: Optional[float] = None
    holdout_hit_rate: Optional[float] = None
    holdout_expectancy: Optional[float] = None
    holdout_samples: int = 0
    stable: Optional[bool] = None
    cost_r: float = 0.0          # round-trip cost expressed in units of risk
    cost_bps: float = 0.0
    # What a win and a loss were actually worth in this bucket, net of costs.
    # A loss is rarely the full stop: most trades that do not reach target are
    # closed at the horizon somewhere in between, and that is the difference
    # between a break-even line of 22% and one of 49%.
    avg_win_r: Optional[float] = None
    avg_loss_r: Optional[float] = None


def wilson_interval(wins: int, n: int, z: float = 1.96):
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it stays sensible at small
    n and near 0 or 1, which is exactly where trading samples live.
    """
    if n == 0:
        return None, None
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def simulate_trade(ctx: Context, i: int, direction: int, stop_atr: float,
                   target_atr: float, horizon: int, cost_bps: float = 0.0):
    """Walk forward from bar i and decide whether target or stop came first.

    Returns (won, r_multiple, bars_held, cost_r), or None when the horizon runs
    past the data. Ambiguous bars, where both levels sit inside the same bar's
    range, are scored as stop-outs because the tick sequence inside a bar is
    unknown and assuming the favourable order would flatter the result.

    A trade that reaches neither level is closed at the horizon and scored at
    its real mark-to-market value rather than as a full loss.

    Round-trip cost is charged to every trade. This matters far more than it
    sounds: on a 5-minute crypto bar, 20bp of exchange fees can exceed the
    entire stop distance, which makes the strategy unprofitable regardless of
    how well it predicts direction.
    """
    n = len(ctx.bars)
    if i + horizon >= n:
        return None
    atr_v = float(ctx.atr[i])
    if not np.isfinite(atr_v) or atr_v <= 0:
        return None

    entry = float(ctx.bars.close[i])
    risk = stop_atr * atr_v
    if risk <= 0:
        return None
    reward_risk = target_atr / stop_atr
    cost_r = (entry * cost_bps / 10000.0) / risk

    if direction > 0:
        stop, target = entry - risk, entry + target_atr * atr_v
    else:
        stop, target = entry + risk, entry - target_atr * atr_v

    last = min(i + horizon, n - 1)
    for k in range(i + 1, last + 1):
        hi, lo = float(ctx.bars.high[k]), float(ctx.bars.low[k])
        if direction > 0:
            hit_stop, hit_target = lo <= stop, hi >= target
        else:
            hit_stop, hit_target = hi >= stop, lo <= target
        if hit_stop:                 # checked first: pessimistic tie-break
            return False, -1.0 - cost_r, k - i, cost_r
        if hit_target:
            return True, reward_risk - cost_r, k - i, cost_r

    exit_price = float(ctx.bars.close[last])
    move = (exit_price - entry) if direction > 0 else (entry - exit_price)
    return False, move / risk - cost_r, last - i, cost_r


def run_backtest(ctx: Context, stop_atr: float, target_atr: float,
                 horizon: int, stride: int = 3,
                 weights: Optional[Dict[str, float]] = None,
                 max_samples: int = 2500, cost_bps: float = 0.0) -> List[Outcome]:
    """Replay the scoring model across history and collect trade outcomes.

    `stride` skips bars so that consecutive, near-identical setups do not get
    counted as independent evidence.
    """
    n = len(ctx.bars)
    start = ctx.warmup
    end = n - horizon - 1
    if end <= start:
        return []

    indices = list(range(start, end, max(1, stride)))
    if len(indices) > max_samples:                   # keep the most recent
        indices = indices[-max_samples:]

    outcomes: List[Outcome] = []
    for i in indices:
        _, score, _ = evaluate(ctx, i, weights)
        if abs(score) < 0.02:
            continue                                  # no directional opinion
        direction = 1 if score > 0 else -1
        res = simulate_trade(ctx, i, direction, stop_atr, target_atr, horizon,
                             cost_bps)
        if res is None:
            continue
        won, r_mult, held, cost_r = res
        outcomes.append(Outcome(abs(score), won, r_mult, held, i, cost_r))
    return outcomes


def calibrate(ctx: Context, current_score: float, stop_atr: float,
              target_atr: float, horizon: int, stride: int = 3,
              weights: Optional[Dict[str, float]] = None,
              min_bucket: int = 25, cost_bps: float = 0.0) -> Calibration:
    """Measure the hit rate for setups scored like the current one.

    Buckets on the *absolute* score, treating a strong long and a strong short
    as the same strength of conviction. The direction is applied when the trade
    is simulated, so this stays a measure of how much the score is worth.
    """
    # The horizon comes from config, where it may legitimately be the string
    # "auto". Callers are expected to resolve it with config.horizon_for first;
    # fail loudly here rather than letting a string flow into range().
    if not isinstance(horizon, int) or isinstance(horizon, bool):
        try:
            horizon = int(horizon)
        except (TypeError, ValueError):
            raise TypeError(
                "horizon must be an integer number of bars, got %r. Resolve it "
                "with config.horizon_for(interval, cfg) before calling." % (horizon,))

    reward_risk = target_atr / stop_atr if stop_atr > 0 else 0.0

    outcomes = run_backtest(ctx, stop_atr, target_atr, horizon, stride, weights,
                            cost_bps=cost_bps)
    total = len(outcomes)

    # Break-even must include cost. Solving p*(R-c) = (1-p)*(1+c) gives
    # p = (1+c)/(1+R). Ignoring c is how a strategy looks viable on paper and
    # bleeds money in an account.
    avg_cost = float(np.mean([o.cost_r for o in outcomes])) if outcomes else 0.0
    breakeven = ((1.0 + avg_cost) / (1.0 + reward_risk)) if reward_risk > 0 else 1.0

    if total == 0:
        return Calibration(0, 0, 0, 0, None, None, None, breakeven, None,
                           horizon, reward_risk, None, False,
                           "No historical setups available to measure.")

    baseline = sum(1 for o in outcomes if o.won) / total

    # Build a coarse curve so the report can show how the hit rate scales with
    # conviction. If it does not rise with score, the model has no edge.
    edges = [0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 1.01]
    curve = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        grp = [o for o in outcomes if lo <= o.score < hi]
        if grp:
            wins = sum(1 for o in grp if o.won)
            curve.append({"low": lo, "high": min(hi, 1.0), "n": len(grp),
                          "hit_rate": wins / len(grp)})

    # Locate the bucket holding the current score, widening it until it holds
    # enough samples to say anything. An honest wide bucket beats a precise
    # number computed from six trades.
    target = abs(current_score)
    width = 0.10
    chosen = None
    while width <= 0.55:
        lo, hi = max(0.0, target - width), min(1.0, target + width)
        grp = [o for o in outcomes if lo <= o.score <= hi]
        if len(grp) >= min_bucket:
            chosen = (lo, hi, grp)
            break
        width += 0.05
    if chosen is None:
        lo, hi = max(0.0, target - 0.55), min(1.0, target + 0.55)
        grp = [o for o in outcomes if lo <= o.score <= hi]
        chosen = (lo, hi, grp)

    lo, hi, grp = chosen
    bn = len(grp)
    if bn == 0:
        return Calibration(total, lo, hi, 0, None, None, None, breakeven, None,
                           horizon, reward_risk, baseline, False,
                           "No comparable historical setups at this score.")

    wins = sum(1 for o in grp if o.won)
    rate = wins / bn
    ci_lo, ci_hi = wilson_interval(wins, bn)
    # Expectancy comes from realised R multiples, not from the hit rate.
    # Timed-out trades carry their true mark-to-market value, so this is what
    # the setup actually returned per unit of risk.
    expectancy = float(np.mean([o.r_multiple for o in grp]))
    avg_held = float(np.mean([o.bars_held for o in grp]))

    # The break-even hit rate, measured rather than assumed.
    #
    # (1 + cost) / (1 + reward_risk) is only the answer if every trade that
    # fails resolves at the full stop. It does not: a trade that reaches
    # neither target nor stop inside the horizon is closed at what it is worth,
    # and across instruments the average loss lands between -0.4R and -0.7R
    # rather than -1.0R. Assuming the full stop roughly doubles the hit rate
    # the setup appears to need, which put a positive expectancy next to a
    # break-even line it looked like it was failing -- the page contradicting
    # itself in the one place a reader checks the arithmetic.
    won_r = [o.r_multiple for o in grp if o.won]
    lost_r = [o.r_multiple for o in grp if not o.won]
    avg_win = float(np.mean(won_r)) if won_r else None
    avg_loss = float(np.mean(lost_r)) if lost_r else None
    if avg_win is not None and avg_loss is not None and avg_win > avg_loss             and avg_loss < 0:
        breakeven = -avg_loss / (avg_win - avg_loss)

    # Hold out the most recent third of comparable setups. If the edge exists
    # only in older data, the model has decayed and should not be trusted.
    grp_sorted = sorted(grp, key=lambda o: o.index)
    holdout = grp_sorted[int(len(grp_sorted) * 0.67):]
    h_rate = h_exp = stable = None
    if len(holdout) >= 15:
        h_rate = sum(1 for o in holdout if o.won) / len(holdout)
        h_exp = float(np.mean([o.r_multiple for o in holdout]))
        stable = (expectancy > 0) == (h_exp > 0)

    reliable = bn >= min_bucket and total >= 120

    if not reliable:
        note = ("Only %d comparable setups in history. Treat this figure as "
                "indicative, not measured." % bn)
    elif ci_lo is not None and ci_lo <= breakeven <= (ci_hi or 1.0):
        note = ("Confidence interval straddles the %.0f%% break-even line, so "
                "no edge is demonstrated at this score." % (breakeven * 100))
    elif rate > breakeven:
        note = "Hit rate clears break-even on %d comparable setups." % bn
    else:
        note = "Hit rate sits below break-even on %d comparable setups." % bn

    if stable is False:
        note += " The most recent third of samples disagrees with the full period."
    if avg_cost >= 0.5:
        note += (" Round-trip cost alone is %.2fR here, so fees consume most of "
                 "the risk budget on every trade." % avg_cost)

    return Calibration(total, lo, hi, bn, rate, ci_lo, ci_hi, breakeven,
                       expectancy, horizon, reward_risk, baseline, reliable,
                       note, curve, avg_bars_held=avg_held,
                       holdout_hit_rate=h_rate, holdout_expectancy=h_exp,
                       holdout_samples=len(holdout), stable=stable,
                       cost_r=avg_cost, cost_bps=cost_bps,
                       avg_win_r=avg_win, avg_loss_r=avg_loss)
