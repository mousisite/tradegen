"""Per-strategy performance measurement and weight learning.

This is what lets the bot answer "which strategy should I trust here?" with
evidence instead of assertion. Every strategy is back-tested independently on
the instrument in front of it, bucketed by market regime, and the resulting
expectancy adjusts its weight.

Two safeguards keep this from becoming curve-fitting:

* **Shrinkage.** A weight moves away from its prior only in proportion to how
  much evidence supports it. Three lucky wins change almost nothing.
* **Bounded multipliers.** No strategy can be amplified or suppressed without
  limit, so one noisy measurement cannot dominate the composite.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .calibrate import simulate_trade, wilson_interval
from .strategies import (BY_NAME, Context, RANGING, REGISTRY, Regime,
                         TRANSITIONAL, TRENDING, classify_regime, evaluate)

# Below this absolute score a strategy is treated as having no opinion, so its
# performance is not credited or blamed for the outcome.
MIN_OPINION = 0.15

# Shrinkage constant. At n == PRIOR_N the learned weight sits halfway between
# its prior and what the data alone would suggest.
PRIOR_N = 60.0

MIN_MULT, MAX_MULT = 0.25, 1.90


@dataclass
class StrategyStat:
    """Measured performance of one strategy, optionally within one regime.

    Gross and net expectancy are tracked separately because they answer
    different questions. Gross asks whether the strategy predicts direction at
    all. Net asks whether that skill survives this instrument's trading costs.
    A strategy can be genuinely good and still untradable on expensive
    instruments, and averaging the two together hides both facts.
    """
    name: str
    family: str
    regime: str            # "all" or a regime key
    n: int = 0
    wins: int = 0
    sum_r: float = 0.0         # net of costs
    sum_r_gross: float = 0.0   # before costs
    sum_bars: float = 0.0

    @property
    def hit_rate(self) -> Optional[float]:
        return (self.wins / self.n) if self.n else None

    @property
    def expectancy(self) -> Optional[float]:
        return (self.sum_r / self.n) if self.n else None

    @property
    def expectancy_gross(self) -> Optional[float]:
        return (self.sum_r_gross / self.n) if self.n else None

    @property
    def avg_bars(self) -> Optional[float]:
        return (self.sum_bars / self.n) if self.n else None

    def interval(self):
        return wilson_interval(self.wins, self.n) if self.n else (None, None)

    def multiplier(self) -> float:
        """Weight multiplier implied by this record, shrunk toward 1.0.

        Expectancy is in units of risk, so a strategy averaging +0.2R is worth
        meaningfully more than one averaging -0.2R. The shrinkage term is what
        stops a thin sample from swinging the weight.
        """
        exp = self.expectancy
        if exp is None or self.n <= 0:
            return 1.0
        raw = 1.0 + 1.6 * exp
        shrink = self.n / (self.n + PRIOR_N)
        return max(MIN_MULT, min(MAX_MULT, 1.0 + (raw - 1.0) * shrink))


@dataclass
class StrategyReport:
    """Everything measured about the strategy library on one instrument."""
    overall: Dict[str, StrategyStat] = field(default_factory=dict)
    by_regime: Dict[Tuple[str, str], StrategyStat] = field(default_factory=dict)
    samples: int = 0
    regime_counts: Dict[str, int] = field(default_factory=dict)
    cost_r: float = 0.0
    reward_risk: float = 0.0
    breakeven: float = 0.0

    def best(self, regime: Optional[str] = None, minimum: int = 40,
             limit: int = 8) -> List[StrategyStat]:
        """Top strategies by expectancy, with enough samples to mean something."""
        pool = ([s for (n, r), s in self.by_regime.items() if r == regime]
                if regime else list(self.overall.values()))
        pool = [s for s in pool if s.n >= minimum and s.expectancy is not None]
        return sorted(pool, key=lambda s: -s.expectancy)[:limit]

    def worst(self, regime: Optional[str] = None, minimum: int = 40,
              limit: int = 8) -> List[StrategyStat]:
        pool = ([s for (n, r), s in self.by_regime.items() if r == regime]
                if regime else list(self.overall.values()))
        pool = [s for s in pool if s.n >= minimum and s.expectancy is not None]
        return sorted(pool, key=lambda s: s.expectancy)[:limit]


def _stat(store: Dict, key, name: str, family: str, regime: str) -> StrategyStat:
    if key not in store:
        store[key] = StrategyStat(name=name, family=family, regime=regime)
    return store[key]


def measure_strategies(ctx: Context, stop_atr: float, target_atr: float,
                       horizon: int, stride: int = 3, cost_bps: float = 0.0,
                       max_samples: int = 1500,
                       weights: Optional[Dict[str, float]] = None) -> StrategyReport:
    """Back-test every strategy independently across the instrument's history.

    A trade's outcome depends only on the bar and the direction taken, never on
    which strategy proposed it, so simulations are cached per (bar, direction).
    That turns tens of thousands of walk-forwards into a few thousand.
    """
    # Same guard as the calibrator: "auto" must be resolved by the caller.
    if not isinstance(horizon, int) or isinstance(horizon, bool):
        try:
            horizon = int(horizon)
        except (TypeError, ValueError):
            raise TypeError(
                "horizon must be an integer number of bars, got %r. Resolve it "
                "with config.horizon_for(interval, cfg) first." % (horizon,))

    report = StrategyReport()
    n_bars = len(ctx.bars)
    start, end = ctx.warmup, n_bars - horizon - 1
    if end <= start:
        return report

    indices = list(range(start, end, max(1, stride)))
    if len(indices) > max_samples:
        indices = indices[-max_samples:]

    sim_cache: Dict[Tuple[int, int], Optional[tuple]] = {}

    def simulate(i: int, direction: int):
        key = (i, direction)
        if key not in sim_cache:
            sim_cache[key] = simulate_trade(ctx, i, direction, stop_atr,
                                            target_atr, horizon, cost_bps)
        return sim_cache[key]

    cost_total, cost_n = 0.0, 0
    for i in indices:
        signals, _composite, reg = evaluate(ctx, i, weights)
        report.regime_counts[reg.trend] = report.regime_counts.get(reg.trend, 0) + 1

        for sig in signals:
            if sig.weight <= 0 or abs(sig.score) < MIN_OPINION:
                continue
            direction = 1 if sig.score > 0 else -1
            res = simulate(i, direction)
            if res is None:
                continue
            won, r_mult, held, cost_r = res
            cost_total += cost_r
            cost_n += 1

            for key, regime_label in (((sig.name, "all"), "all"),
                                      ((sig.name, reg.trend), reg.trend)):
                store = report.overall if regime_label == "all" else report.by_regime
                store_key = sig.name if regime_label == "all" else key
                st = _stat(store, store_key, sig.name, sig.family, regime_label)
                st.n += 1
                st.wins += 1 if won else 0
                st.sum_r += r_mult
                st.sum_r_gross += r_mult + cost_r
                st.sum_bars += held

        report.samples += 1

    report.cost_r = (cost_total / cost_n) if cost_n else 0.0
    report.reward_risk = target_atr / stop_atr if stop_atr > 0 else 0.0
    report.breakeven = ((1.0 + report.cost_r) / (1.0 + report.reward_risk)
                        if report.reward_risk > 0 else 1.0)
    return report


def learned_weights(report: StrategyReport, regime: Optional[Regime] = None,
                    blend_regime: float = 0.65) -> Dict[str, float]:
    """Turn measured performance into per-strategy weight multipliers.

    Regime-specific evidence is preferred but blended with the strategy's
    all-conditions record. A strategy measured over 40 trending bars alone is
    weak evidence; combining it with its 400-sample overall record is steadier
    than trusting either in isolation.
    """
    out: Dict[str, float] = {}
    key = regime.trend if regime else None

    for strat in REGISTRY:
        overall = report.overall.get(strat.name)
        mult = overall.multiplier() if overall else 1.0

        if key is not None:
            scoped = report.by_regime.get((strat.name, key))
            if scoped and scoped.n >= 25:
                # Confidence in the regime-specific figure grows with its own
                # sample count, capped by blend_regime.
                share = blend_regime * (scoped.n / (scoped.n + PRIOR_N))
                mult = mult * (1.0 - share) + scoped.multiplier() * share
        out[strat.name] = max(MIN_MULT, min(MAX_MULT, mult))
    return out


def summarise(report: StrategyReport, regime: Optional[Regime] = None) -> str:
    """One-line human summary of what the library learned on this instrument."""
    if not report.overall:
        return "No per-strategy history was measurable."
    ranked = [s for s in report.overall.values() if s.n >= 40 and s.expectancy is not None]
    if not ranked:
        return "Too few comparable setups to rank strategies."
    ranked.sort(key=lambda s: -s.expectancy)
    positive = [s for s in ranked if s.expectancy > 0]
    return ("%d of %d strategies measured positive expectancy on this "
            "instrument; best is %s at %+.3fR over %d setups." % (
                len(positive), len(ranked), ranked[0].name,
                ranked[0].expectancy, ranked[0].n))
