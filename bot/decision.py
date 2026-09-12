"""Turn analysis into an actionable plan.

Produces one of four calls:

  BUY    - conditions line up now, enter at market
  WAIT   - the thesis is valid but the price is not; here is the level to wait for
  AVOID  - no demonstrated edge, or the evidence points the other way
  SHORT  - bearish setup, only when shorts are enabled in config

The measured expectancy from the calibrator can veto a bullish technical read.
That is intentional. If setups scoring like this one have historically lost
money on this instrument, a tidy EMA stack is not a reason to buy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .calibrate import Calibration
from .config import cost_bps_for
from .levels import Level, nearest_resistance, nearest_support
from .market import Bars, format_price
from .sentiment import Sentiment
from .strategies import Context, RANGING, Regime, Signal, TRENDING


@dataclass
class Plan:
    """The final, actionable output."""
    action: str                     # BUY | WAIT | AVOID | SHORT
    direction: int                  # +1 long, -1 short, 0 none
    headline: str
    conviction: float               # -1..1 blended technical + sentiment
    entry: Optional[float] = None
    entry_type: str = ""            # market | limit
    entry_rationale: str = ""
    trigger: str = ""
    stop: Optional[float] = None
    stop_rationale: str = ""
    target1: Optional[float] = None
    target2: Optional[float] = None
    target_rationale: str = ""
    reward_risk: Optional[float] = None
    breakeven_rate: Optional[float] = None
    probability: Optional[float] = None
    prob_low: Optional[float] = None
    prob_high: Optional[float] = None
    prob_samples: int = 0
    prob_reliable: bool = False
    expectancy_r: Optional[float] = None
    edge_verdict: str = ""
    position: Dict = field(default_factory=dict)
    invalidation: str = ""
    risks: List[str] = field(default_factory=list)
    confirmations: List[str] = field(default_factory=list)


def _round_price(value: float, reference: float) -> float:
    """Round to a sensible tick for the instrument's price magnitude."""
    if reference >= 1000:
        return round(value, 1)
    if reference >= 100:
        return round(value, 2)
    if reference >= 1:
        return round(value, 3)
    return round(value, 6)


def blend_conviction(composite: float, sentiment: Sentiment,
                     weight: float) -> tuple:
    """Combine the technical score with news and social sentiment.

    Sentiment modulates rather than decides. It is the least reliable input
    and the easiest to manipulate, so it never gets the majority share.
    Directional disagreement is surfaced instead of being averaged away.
    """
    tech_w = 1.0 - weight
    blended = tech_w * composite + weight * sentiment.score
    conflict = (composite > 0.15 and sentiment.score < -0.25) or \
               (composite < -0.15 and sentiment.score > 0.25)
    if conflict:
        blended *= 0.6          # disagreement is a reason for less size
    return max(-1.0, min(1.0, blended)), conflict


def choose_pullback_entry(ctx: Context, bars: Bars, levels: List[Level],
                          direction: int, atr_value: float):
    """Pick the price to wait for when the thesis is right but price is not.

    Candidate levels are the places intraday buyers actually defend: the
    nearest structural support, session VWAP, and the 21-EMA. The nearest
    candidate that sits a meaningful distance away wins, because the closest
    valid level is the one price is most likely to reach.

    This is the aggressive-versus-patient trade-off in one function. Taking
    the *nearest* candidate fills more often at a worse price; taking the
    furthest fills rarely at an excellent price. Adjust `min_gap` to move
    along that spectrum.
    """
    price = bars.last_price
    i = len(bars) - 1
    min_gap = 0.30 * atr_value          # ignore levels that are basically here
    candidates = []

    if direction > 0:
        support = nearest_support(levels, price)
        if support and price - support.price >= min_gap:
            candidates.append((support.price, support.label()))
        vwap = ctx.vwap[i]
        if np.isfinite(vwap) and price - vwap >= min_gap:
            candidates.append((float(vwap), "session VWAP"))
        ema21 = ctx.ema21[i]
        if np.isfinite(ema21) and price - ema21 >= min_gap:
            candidates.append((float(ema21), "21-EMA"))
        if not candidates:
            return price - 0.5 * atr_value, "0.5 ATR below current price"
        best = max(candidates, key=lambda c: c[0])     # nearest below
    else:
        resistance = nearest_resistance(levels, price)
        if resistance and resistance.price - price >= min_gap:
            candidates.append((resistance.price, resistance.label()))
        vwap = ctx.vwap[i]
        if np.isfinite(vwap) and vwap - price >= min_gap:
            candidates.append((float(vwap), "session VWAP"))
        ema21 = ctx.ema21[i]
        if np.isfinite(ema21) and ema21 - price >= min_gap:
            candidates.append((float(ema21), "21-EMA"))
        if not candidates:
            return price + 0.5 * atr_value, "0.5 ATR above current price"
        best = min(candidates, key=lambda c: c[0])     # nearest above
    return best


def build_stop(entry: float, direction: int, atr_value: float,
               levels: List[Level], cfg: Dict):
    """Place the stop beyond structure, but not so far that R:R collapses."""
    atr_stop = entry - direction * cfg["stop_atr_multiple"] * atr_value
    buffer = 0.30 * atr_value

    if direction > 0:
        support = nearest_support(levels, entry)
        structural = (support.price - buffer) if support else None
        rationale = "%.4g ATR below entry" % cfg["stop_atr_multiple"]
        if structural is not None and structural < atr_stop:
            if (entry - structural) <= 2.5 * atr_value:
                return structural, "below %s" % support.label()
            return atr_stop, rationale + " (structure too far to use)"
        return atr_stop, rationale

    resistance = nearest_resistance(levels, entry)
    structural = (resistance.price + buffer) if resistance else None
    rationale = "%.4g ATR above entry" % cfg["stop_atr_multiple"]
    if structural is not None and structural > atr_stop:
        if (structural - entry) <= 2.5 * atr_value:
            return structural, "above %s" % resistance.label()
        return atr_stop, rationale + " (structure too far to use)"
    return atr_stop, rationale


def build_targets(entry: float, stop: float, direction: int, atr_value: float,
                  levels: List[Level], cfg: Dict, min_rr: float = 1.0):
    """Set targets at ATR distance, clipped only to obstacles worth respecting.

    Naively clipping to the *nearest* level in the path is a trap. When that
    level sits a third of an ATR away the trade becomes a 0.5R target needing a
    67% win rate, which no intraday setup sustains. Minor resistance gets
    traded through.

    So blockers are walked outward and the first one that still leaves at least
    `min_rr` is used. If none qualifies, the ATR target stands and the report
    says resistance lies in the path.
    """
    risk = abs(entry - stop)
    t1 = entry + direction * cfg["target_atr_multiple"] * atr_value
    t2 = entry + direction * cfg["runner_atr_multiple"] * atr_value
    note = "%.4g and %.4g ATR from entry" % (cfg["target_atr_multiple"],
                                             cfg["runner_atr_multiple"])
    if risk <= 0:
        return t1, t2, note

    if direction > 0:
        blockers = sorted([lv for lv in levels
                           if lv.kind == "resistance" and entry < lv.price < t1],
                          key=lambda lv: lv.price)
    else:
        blockers = sorted([lv for lv in levels
                           if lv.kind == "support" and t1 < lv.price < entry],
                          key=lambda lv: -lv.price)

    if not blockers:
        return t1, t2, note

    for lv in blockers:
        capped = lv.price - direction * 0.15 * atr_value
        gain = (capped - entry) * direction
        if gain / risk >= min_rr:
            far = lv.price + direction * atr_value
            t2_adj = max(t2, far) if direction > 0 else min(t2, far)
            return capped, t2_adj, "trimmed to %s, the first level that still pays" % lv.label()

    return t1, t2, ("%s; note %d minor level(s) sit in the path, so expect "
                    "resistance on the way" % (note, len(blockers)))


def size_position(entry: float, stop: float, bars: Bars, cfg: Dict) -> Dict:
    """Risk-based sizing: never risk more than the configured share of capital."""
    account = float(cfg["account_size"])
    risk_amount = account * float(cfg["risk_per_trade_pct"]) / 100.0
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return {"error": "stop equals entry, cannot size"}

    qty = risk_amount / per_unit
    notional = qty * entry
    cap = account * float(cfg["max_position_pct"]) / 100.0
    capped = False
    if notional > cap:
        qty = cap / entry
        notional = qty * entry
        capped = True

    if bars.is_crypto:
        qty = round(qty, 6)
    else:
        qty = float(int(qty))          # whole shares only
        if qty < 1:
            return {"error": "account too small to take one share at this stop distance",
                    "risk_amount": risk_amount, "per_unit_risk": per_unit}

    out = {
        "quantity": qty,
        "notional": qty * entry,
        "risk_amount": qty * per_unit,
        "risk_pct_of_account": (qty * per_unit) / account * 100.0,
        "per_unit_risk": per_unit,
        "capped_by_max_position": capped,
    }
    out.update(_gap_risk(entry, stop, qty, account, out["risk_amount"], bars))
    return out


def _gap_risk(entry: float, stop: float, qty: float, account: float,
              at_risk: float, bars: Bars) -> Dict:
    """What a move the size of this instrument's worst day would actually cost.

    A stop is an instruction, not a guarantee. When price opens through it the
    fill is wherever the market reopened, which is the loss that ends accounts.
    Measured from this instrument's own history rather than an assumed number,
    using bars already in hand, so it costs nothing to know.
    """
    close = np.asarray(bars.close, dtype=float)
    if len(close) < 30 or entry <= 0 or qty <= 0 or account <= 0:
        return {}
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(close) / close[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 30:
        return {}

    long_side = entry > stop
    adverse = abs(float(np.min(rets))) if long_side else float(np.max(rets))
    adverse = abs(adverse)
    if adverse <= 0:
        return {}

    gap_fill = entry * (1 - adverse) if long_side else entry * (1 + adverse)
    # If such a move would not even reach the stop, the stop fills normally and
    # the loss stays what was budgeted.
    gap_loss = max(at_risk, abs(entry - gap_fill) * qty)
    return {
        "gap_move": adverse,
        "gap_price": gap_fill,
        "gap_loss": gap_loss,
        "gap_pct_of_account": gap_loss / account * 100.0,
        "gap_multiple": (gap_loss / at_risk) if at_risk > 0 else None,
        "gap_samples": int(len(rets)),
    }


def decide(ctx: Context, bars: Bars, signals: List[Signal], composite: float,
           regime: Regime, calib: Calibration, sentiment: Sentiment,
           levels: List[Level], cfg: Dict) -> Plan:
    """Assemble the final trade plan."""
    i = len(bars) - 1
    price = bars.last_price
    atr_value = float(ctx.atr[i])

    def fp(value) -> str:
        """Round to the instrument's tick, then render it readably."""
        return format_price(_round_price(float(value), price))

    conviction, conflict = blend_conviction(composite, sentiment,
                                            float(cfg["sentiment_weight"]))

    risks: List[str] = []
    confirmations: List[str] = []

    if conflict:
        risks.append("Technical signal and news sentiment disagree. "
                     "Conviction was reduced accordingly.")
    if not bars.market_open:
        risks.append("The market is closed. This reads the last completed "
                     "session, and the open can gap straight through these levels.")
    if sentiment.fresh_catalyst:
        risks.append("A fresh catalyst landed within three hours. Technical "
                     "levels hold poorly immediately after news.")
    if regime.trend == RANGING:
        risks.append("ADX indicates a ranging market. Breakout entries fail "
                     "more often in this regime.")
    for note in sentiment.notes:
        risks.append(note)

    direction = 1 if conviction > 0 else (-1 if conviction < 0 else 0)

    # --- gate 1: is there any directional opinion at all? -----------------
    if abs(conviction) < float(cfg["min_conviction"]):
        return Plan(
            action="AVOID", direction=0,
            headline="No setup. Conviction %.3f is below the %.3f threshold." % (
                abs(conviction), cfg["min_conviction"]),
            conviction=conviction,
            edge_verdict="Signals are mixed. Sitting out is the correct trade.",
            probability=calib.hit_rate, prob_low=calib.ci_low,
            prob_high=calib.ci_high, prob_samples=calib.bucket_samples,
            prob_reliable=calib.reliable, expectancy_r=calib.expectancy_r,
            risks=risks,
            confirmations=["Re-run when price breaks the session range or a "
                           "catalyst lands."])

    # --- gate 2: shorts disabled ------------------------------------------
    if direction < 0 and not cfg.get("allow_shorts", False):
        support = nearest_support(levels, price)
        where = format_price(support.price) if support else format_price(price - 2 * atr_value)
        return Plan(
            action="AVOID", direction=0,
            headline="Bearish lean, and shorting is disabled. Do not buy here.",
            conviction=conviction,
            edge_verdict="Evidence points down. A long entry would be fighting it.",
            probability=calib.hit_rate, prob_low=calib.ci_low,
            prob_high=calib.ci_high, prob_samples=calib.bucket_samples,
            prob_reliable=calib.reliable, expectancy_r=calib.expectancy_r,
            invalidation="Reconsider a long only if price reclaims %s and "
                         "holds it on a closing basis." % fp(ctx.vwap[i] if np.isfinite(ctx.vwap[i]) else price),
            risks=risks,
            confirmations=["Watch %s for a bounce and a reclaim of VWAP before "
                           "any long is worth considering." % where])

    # --- construct the trade ---------------------------------------------
    extension = (price - float(ctx.vwap[i])) / atr_value if (
        np.isfinite(ctx.vwap[i]) and atr_value > 0) else 0.0
    over_extended = (direction > 0 and extension > float(cfg["max_extension_atr"])) or \
                    (direction < 0 and extension < -float(cfg["max_extension_atr"]))

    blocker = nearest_resistance(levels, price) if direction > 0 else \
        nearest_support(levels, price)
    blocked = blocker is not None and abs(blocker.price - price) < 0.6 * atr_value

    strong = abs(conviction) >= float(cfg["strong_conviction"])
    if over_extended:
        mode = "WAIT"
        why = "Price is %.1f ATR from VWAP, too extended to chase." % abs(extension)
    elif blocked:
        mode = "WAIT"
        why = "Price is sitting into %s, only %.2f away." % (
            blocker.label(), abs(blocker.price - price))
    elif strong:
        # Direction decides the label. Calling a short "BUY" while building a
        # trade whose target sits below the entry is how someone clicks the
        # wrong button, so the two are kept in lockstep.
        mode = "BUY" if direction > 0 else "SHORT"
        why = "Conviction %.2f clears the %.2f market-entry threshold and price is not extended." % (
            abs(conviction), cfg["strong_conviction"])
    else:
        mode = "WAIT"
        why = "Conviction %.2f is directional but below the %.2f needed to pay the spread at market." % (
            abs(conviction), cfg["strong_conviction"])

    if mode in ("BUY", "SHORT"):
        entry, entry_rationale, entry_type = price, "current price", "market"
        if direction > 0:
            trigger = ("Enter now, while price holds above %s."
                       % fp(price - 0.4 * atr_value))
        else:
            trigger = ("Enter now, while price holds below %s."
                       % fp(price + 0.4 * atr_value))
    else:
        entry, entry_rationale = choose_pullback_entry(ctx, bars, levels,
                                                       direction, atr_value)
        entry_type = "limit"
        side = "holds" if direction > 0 else "rejects"
        trigger = ("Wait for %s. Only take it if a %s candle closes back in your "
                   "favour there and volume %s the move. A level that slices "
                   "straight through is not support." % (
                       fp(entry), bars.interval, side))

    stop, stop_rationale = build_stop(entry, direction, atr_value, levels, cfg)
    target1, target2, target_rationale = build_targets(entry, stop, direction,
                                                       atr_value, levels, cfg)

    risk_per_unit = abs(entry - stop)
    reward = abs(target1 - entry)
    rr = (reward / risk_per_unit) if risk_per_unit > 0 else 0.0

    # Break-even has to carry the round trip. p*(R-c) = (1-p)*(1+c) gives
    # p = (1+c)/(1+R). On tight intraday stops this is not a rounding detail:
    # it can move the required win rate by tens of percentage points.
    cost_bps = cost_bps_for(bars.asset_class, cfg)
    cost_r = ((entry * cost_bps / 10000.0) / risk_per_unit) if risk_per_unit > 0 else 0.0
    breakeven = ((1.0 + cost_r) / (1.0 + rr)) if rr > 0 else 1.0
    position = size_position(entry, stop, bars, cfg)

    if cost_r >= 0.25:
        # Say what would actually fix it. The stop distance needed to push cost
        # down to a tolerable 0.15R tells the trader directly whether this
        # instrument is tradeable on this timeframe at their fee level.
        needed_atr = (entry * cost_bps / 10000.0) / (0.15 * atr_value) if atr_value > 0 else 0.0
        risks.append(
            "Round-trip cost is %.2fR at %.0fbp. Fees alone lift break-even to "
            "%.0f%%. To get cost down to 0.15R you would need a stop of about "
            "%.1f ATR instead of %.1f, which on this timeframe is impractical. "
            "Move to a higher interval where ATR is larger, or trade a cheaper "
            "instrument." % (cost_r, cost_bps, breakeven * 100, needed_atr,
                             cfg["stop_atr_multiple"]))

    # --- gate 3: does the measured evidence support taking it? ------------
    verdict_bits = []
    action = mode
    if calib.hit_rate is None:
        verdict_bits.append("No historical sample was available to measure this "
                            "setup, so the odds are unverified.")
    else:
        verdict_bits.append(
            "Setups scoring like this one reached target before stop %.0f%% of the "
            "time across %d comparable cases (95%% CI %.0f-%.0f%%)." % (
                calib.hit_rate * 100, calib.bucket_samples,
                (calib.ci_low or 0) * 100, (calib.ci_high or 0) * 100))
        verdict_bits.append("This plan needs %.0f%% to break even at %.2fR." % (
            breakeven * 100, rr))

        if calib.reliable and calib.expectancy_r is not None and \
                calib.expectancy_r <= 0 and cfg.get("require_positive_expectancy", True):
            action = "AVOID"
            verdict_bits.append(
                "Measured expectancy is %+.2fR per trade, so this setup has lost "
                "money historically on this instrument. The technical picture does "
                "not override that." % calib.expectancy_r)
        elif calib.ci_low is not None and calib.ci_low <= breakeven <= (calib.ci_high or 1.0):
            verdict_bits.append("The confidence interval straddles break-even, so "
                                "no edge is proven. Treat this as a small or paper trade.")
        elif calib.hit_rate > breakeven:
            verdict_bits.append("Measured hit rate clears the break-even line.")

    if calib.stable is False:
        verdict_bits.append("The most recent third of samples disagrees with the "
                            "full period, which suggests the edge is decaying.")

    if action == "AVOID":
        headline = "Stand aside. The measured record for this setup is negative."
    elif action == "BUY":
        headline = "Buy now near %s." % fp(entry)
    elif action == "SHORT":
        headline = "Sell short now near %s." % fp(entry)
    elif direction > 0:
        headline = "Wait. Buy only if price reaches %s." % fp(entry)
    else:
        headline = "Wait. Short only if price reaches %s." % fp(entry)

    confirmations.extend([
        "Volume on the entry bar at or above 1.2x the 20-bar average.",
        "Price on the correct side of session VWAP (%s)." % fp(ctx.vwap[i] if np.isfinite(ctx.vwap[i]) else price),
        "No scheduled news for this instrument inside your holding window.",
    ])
    if regime.trend == TRENDING:
        confirmations.append("Trend is confirmed by ADX %.0f. Favour holding for "
                             "the runner target." % ctx.adx[i])

    invalidation = ("Thesis is dead on a %s close beyond %s. Exit, do not average down."
                    % (bars.interval, fp(stop)))

    return Plan(
        action=action, direction=direction, headline=headline,
        conviction=conviction,
        entry=_round_price(entry, price), entry_type=entry_type,
        entry_rationale=("%s. Level used: %s." % (why.rstrip(". "), entry_rationale)
                         if entry_type == "limit" else why),
        trigger=trigger,
        stop=_round_price(stop, price), stop_rationale=stop_rationale,
        target1=_round_price(target1, price), target2=_round_price(target2, price),
        target_rationale=target_rationale,
        reward_risk=rr, breakeven_rate=breakeven,
        probability=calib.hit_rate, prob_low=calib.ci_low, prob_high=calib.ci_high,
        prob_samples=calib.bucket_samples, prob_reliable=calib.reliable,
        expectancy_r=calib.expectancy_r,
        edge_verdict=" ".join(verdict_bits),
        position=position, invalidation=invalidation,
        risks=risks, confirmations=confirmations,
    )
