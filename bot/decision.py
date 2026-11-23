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
from .config import cost_bps_for_symbol
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
    # What a win and a loss were actually worth, so the plain-words layer can
    # say "when it worked it made 1.5x, when it did not it lost about half"
    # rather than leaving a reader to assume every loss was the whole stop.
    avg_win_r: Optional[float] = None
    avg_loss_r: Optional[float] = None
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
    # Why an AVOID is an AVOID, because the plain words differ: "no_signal"
    # (nothing to go on), "bearish_no_shorts" (the signals point down and
    # shorting is off) or "negative_record" (this setup has lost money here).
    avoid_reason: str = ""
    # The side the odds above were measured on: +1 longs, -1 shorts. On an
    # AVOID that leans down they are the record of betting on a fall, which a
    # reader must not take for a reason to buy.
    odds_direction: int = 0
    # What the market's own drift paid: the same side taken at every bar.
    baseline_expectancy: Optional[float] = None
    # The reward to risk of the trade the odds were measured on, which can
    # differ from this plan's own once its stop sits behind structure or its
    # target is trimmed to a level in the way.
    tested_reward_risk: Optional[float] = None


def _round_price(value: float, reference: float, move: float = 0.0) -> float:
    """Round to a sensible tick for the instrument's price magnitude.

    Under 1, at least four figures are kept however small the price: cut to
    six decimals, a coin at 0.0000024 had its stop, entry and target rounded
    onto each other. `move` is what a bar usually moves; a tick that is more
    than a tenth of it (a currency on 5-minute bars, a cash fund) is made
    finer for the same reason.
    """
    if reference >= 1000:
        digits = 1
    elif reference >= 100:
        digits = 2
    elif reference >= 1:
        digits = 3
    elif reference > 0:
        digits = max(6, 3 - math.floor(math.log10(reference)))
    else:
        digits = 6
    if move > 0 and 10.0 ** -digits > move / 10.0:
        digits = max(digits, 2 - math.floor(math.log10(move)))
    return round(value, digits)


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


def vwap_label(ctx: Context) -> str:
    """What the VWAP in use is. On daily bars a session is one bar, so the
    context falls back to a 20-bar VWAP, and calling that the session's would
    send someone to look for a line their chart does not draw."""
    return "session VWAP" if getattr(ctx, "vwap_is_session", True) else "20-bar VWAP"


def choose_pullback_entry(ctx: Context, bars: Bars, levels: List[Level],
                          direction: int, atr_value: float):
    """Pick the price to wait for when the thesis is right but price is not.

    Candidate levels are the places buyers actually defend: the nearest
    structural support, VWAP (the session's intraday, a 20-bar one on daily
    bars, where a session is a single bar), and the 21-EMA. The nearest
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
    vwap_name = vwap_label(ctx)

    if direction > 0:
        support = nearest_support(levels, price)
        if support and price - support.price >= min_gap:
            candidates.append((support.price, support.label()))
        vwap = ctx.vwap[i]
        if np.isfinite(vwap) and price - vwap >= min_gap:
            candidates.append((float(vwap), vwap_name))
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
            candidates.append((float(vwap), vwap_name))
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


# Currencies quoted in hundredths, named so the size beside them makes sense.
_HUNDREDTHS = {"GBp": "pence (GBp)", "GBX": "pence (GBX)", "ZAc": "South African cents (ZAc)",
               "ILA": "Israeli agorot (ILA)"}


def _foreign(bars: Bars) -> str:
    """The currency a price is in, when it is not US dollars, else nothing.

    The account is one number with no currency of its own, so a size is in
    whatever the price is in. Nothing is converted; the page says so.
    """
    currency = getattr(bars, "currency", "") or ""
    return "" if currency == "USD" else _HUNDREDTHS.get(currency, currency)


def size_futures(symbol: str, entry: float, stop: float, cfg: Dict) -> Dict:
    """Whole futures contracts for the risk set, as the size calculator counts them.

    One E-mini S&P contract gains or loses 50 dollars a point. Sized like a
    share, at a dollar a point, "4 units, risk 200" was ten thousand dollars
    at risk in four contracts. There is no cap by the position's full value:
    a contract is held on a deposit, and its full value is many times that.
    """
    from . import journal                   # journal reads the setups module

    root = (symbol or "").upper()[:-2]
    if root not in journal.CONTRACTS:
        return {"error": "Futures are sized in whole contracts, and what one point of "
                         "this contract is worth is not known here. The size calculator "
                         "can work it out from your contract's dollars a point."}
    try:
        sized = journal.size(float(cfg["account_size"]), float(cfg["risk_per_trade_pct"]),
                             entry, stop, "futures", root)
    except ValueError as exc:
        return {"error": str(exc)}
    if not sized["fits"]:
        return {"error": sized["why_none"], "risk_amount": 0.0,
                "per_unit_risk": sized["per_unit"]}
    many = "contract" if sized["quantity"] == 1 else "contracts"
    return {"quantity": sized["quantity"], "quantity_text": sized["quantity_text"],
            "unit_text": "%s %s" % (root, many), "multiplier": sized["multiplier"],
            "notional": sized["notional"], "risk_amount": sized["at_risk"],
            "risk_pct_of_account": sized["pct"], "per_unit_risk": sized["per_unit"],
            "capped_by_max_position": False}


def size_words(qty: float, coins: bool) -> Dict:
    """A size as a person writes it, and the word after it: 27 units, 1 unit,
    0.0125 units of a coin."""
    return {"quantity_text": ("{:,.6f}".format(qty).rstrip("0").rstrip(".") if coins
                              else "{:,.0f}".format(qty)),
            "unit_text": "unit" if qty == 1 else "units"}


def size_position(entry: float, stop: float, bars: Bars, cfg: Dict) -> Dict:
    """Risk-based sizing: never risk more than the configured share of capital."""
    account = float(cfg["account_size"])
    risk_amount = account * float(cfg["risk_per_trade_pct"]) / 100.0
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return {"error": "stop equals entry, cannot size"}
    if (bars.symbol or "").upper().endswith("=F"):
        out = size_futures(bars.symbol, entry, stop, cfg)
        if "error" not in out:
            out.update(_gap_risk(entry, stop, out["quantity"] * out["multiplier"],
                                 account, out["risk_amount"], bars))
        return out
    foreign = _foreign(bars)

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
            return {"error": "account too small to take one share at this stop distance"
                             + (", counting the account as %s" % foreign if foreign else ""),
                    "risk_amount": risk_amount, "per_unit_risk": per_unit}

    out = {
        "quantity": qty,
        **size_words(qty, bars.is_crypto),
        "account_in": foreign,
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

    # The bad morning that matters starts with the price resting just above
    # the stop, not at the entry: from there the worst day opens straight
    # through it. Measured from the entry it came out about 1R too small.
    gap_fill = stop * (1 - adverse) if long_side else stop * (1 + adverse)
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
        return format_price(_round_price(float(value), price, atr_value))

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

    # The odds have to be for the trade this call would take. The engine
    # measures them on the call's own side; a calibration from the other side
    # (or pooled across both) neither promotes a call nor vetoes one.
    calib_side = int(getattr(calib, "direction", 0) or 0)
    matched = direction != 0 and calib_side == direction
    # Whether the record shows an edge at all, rather than a positive guess:
    # the hit rate clears break-even at the bottom of its range, the recent
    # third agrees, and it beats taking the same side every day.
    edge = matched and bool(getattr(calib, "demonstrated", False))
    baseline = getattr(calib, "baseline_expectancy", None)
    tested_rr = calib.reward_risk or None

    def odds(**kw) -> Dict:
        """The measured figures every plan carries, whatever it decides."""
        out = dict(probability=calib.hit_rate, prob_low=calib.ci_low,
                   prob_high=calib.ci_high, prob_samples=calib.bucket_samples,
                   prob_reliable=calib.reliable, expectancy_r=calib.expectancy_r,
                   breakeven_rate=calib.breakeven,
                   avg_win_r=calib.avg_win_r, avg_loss_r=calib.avg_loss_r,
                   odds_direction=calib_side, baseline_expectancy=baseline,
                   tested_reward_risk=tested_rr)
        out.update(kw)
        return out

    # --- gate 1: is there any directional opinion at all? -----------------
    #
    # A demonstrated record buys a hearing. Conviction below the floor
    # normally ends the analysis here, which is right when there is nothing
    # else to go on. But conviction does not predict expectancy -- 0.010 across
    # 149 instruments -- so a setup whose record clearly pays is sent on to the
    # real gates below, which can still refuse it on its own evidence. A merely
    # positive average is not that: on random prices it let through setups
    # with conviction as low as 0.006.
    if abs(conviction) < float(cfg["min_conviction"]) and not edge:
        return Plan(
            action="AVOID", direction=0,
            headline="No setup. Conviction %.3f is below the %.3f threshold." % (
                abs(conviction), cfg["min_conviction"]),
            conviction=conviction,
            edge_verdict="Signals are mixed. Sitting out is the correct trade.",
            avoid_reason="no_signal",
            risks=risks,
            confirmations=["Re-run when price breaks the session range or a "
                           "catalyst lands."],
            **odds())

    # --- gate 2: shorts disabled ------------------------------------------
    if direction < 0 and not cfg.get("allow_shorts", False):
        support = nearest_support(levels, price)
        where = format_price(support.price) if support else format_price(price - 2 * atr_value)
        return Plan(
            action="AVOID", direction=0,
            headline="Bearish lean, and shorting is disabled. Do not buy here.",
            conviction=conviction,
            edge_verdict=("Evidence points down. A long entry would be fighting it."
                          + (" The odds shown are for a bet on the price falling, the "
                             "side these signals take, not for a buy."
                             if calib.hit_rate is not None else "")),
            avoid_reason="bearish_no_shorts",
            invalidation="Reconsider a long only if price reclaims %s and "
                         "holds it on a closing basis." % fp(ctx.vwap[i] if np.isfinite(ctx.vwap[i]) else price),
            risks=risks,
            confirmations=["Watch %s for a bounce and a reclaim of the %s before "
                           "any long is worth considering."
                           % (where, vwap_label(ctx))],
            **odds())

    # --- construct the trade ---------------------------------------------
    extension = (price - float(ctx.vwap[i])) / atr_value if (
        np.isfinite(ctx.vwap[i]) and atr_value > 0) else 0.0
    over_extended = (direction > 0 and extension > float(cfg["max_extension_atr"])) or \
                    (direction < 0 and extension < -float(cfg["max_extension_atr"]))

    blocker = nearest_resistance(levels, price) if direction > 0 else \
        nearest_support(levels, price)
    blocked = blocker is not None and abs(blocker.price - price) < 0.6 * atr_value

    strong = abs(conviction) >= float(cfg["strong_conviction"])

    # The measured record is allowed to promote a call, not only to veto one,
    # and it is the only thing that can.
    #
    # Conviction's correlation with expectancy after costs is 0.010 across 149
    # instruments on daily bars, -0.349 on crypto: it does not predict whether
    # a setup makes money, so on its own it never makes a market entry. Gate 3
    # below downgrades to AVOID on a negative record; a record that clearly
    # pays is the same class of evidence pointing the other way. "Clearly" is
    # the point. A positive average over enough cases was promoted once, and
    # on random prices with no edge in them it called about one in fourteen a
    # buy, most of them under a verdict saying no edge was proven.
    side_word = "long" if direction > 0 else "short"
    if over_extended:
        mode = "WAIT"
        why = "Price is %.1f ATR from VWAP, too extended to chase." % abs(extension)
    elif blocked:
        mode = "WAIT"
        why = "Price is sitting into %s, only %s away." % (
            blocker.label(), format_price(abs(blocker.price - price)))
    elif edge:
        # Direction decides the label. Calling a short "BUY" while building a
        # trade whose target sits below the entry is how someone clicks the
        # wrong button, so the two are kept in lockstep.
        mode = "BUY" if direction > 0 else "SHORT"
        why = ("Past %s setups scoring like this one made %+.2fR a trade after "
               "costs across %d cases here, enough that the bottom of the range "
               "still clears break-even, and more than taking the same side on "
               "every day did. Price is not extended and nothing is in the way."
               % (side_word, calib.expectancy_r, calib.bucket_samples))
    else:
        mode = "WAIT"
        if strong:
            why = ("Conviction %.2f is strong, but %s, so this waits for a better "
                   "price." % (abs(conviction), _no_edge_reason(calib, matched)))
        else:
            why = ("Conviction %.2f is directional, but %s, so this waits for a "
                   "better price rather than entering now."
                   % (abs(conviction), _no_edge_reason(calib, matched)))

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
        trigger = ("Wait for %s. Only take it if %s candle closes back in your "
                   "favour there, on volume that backs the move. A level the "
                   "price goes straight through is not %s." % (
                       fp(entry), _a_bar(bars.interval),
                       "support" if direction > 0 else "resistance"))

    # Rounded before anything is worked out from them, so the size, the money
    # at risk and the reward to risk are those of the prices shown, which are
    # also the ones the review page and the journal size from.
    entry = _round_price(entry, price, atr_value)
    stop, stop_rationale = build_stop(entry, direction, atr_value, levels, cfg)
    stop = _round_price(stop, price, atr_value)
    target1, target2, target_rationale = build_targets(entry, stop, direction,
                                                       atr_value, levels, cfg)
    target1 = _round_price(target1, price, atr_value)
    target2 = _round_price(target2, price, atr_value)

    risk_per_unit = abs(entry - stop)
    reward = abs(target1 - entry)
    rr = (reward / risk_per_unit) if risk_per_unit > 0 else 0.0

    # Break-even has to carry the round trip. p*(R-c) = (1-p)*(1+c) gives
    # p = (1+c)/(1+R). On tight intraday stops this is not a rounding detail:
    # it can move the required win rate by tens of percentage points.
    cost_bps = cost_bps_for_symbol(bars.symbol, bars.asset_class, cfg)
    cost_r = ((entry * cost_bps / 10000.0) / risk_per_unit) if risk_per_unit > 0 else 0.0
    # The measured break-even where there is a sample to measure, because
    # (1 + cost) / (1 + rr) assumes every loss resolves at the full stop and
    # they do not: the average loss on this instrument runs nearer -0.5R.
    # Assuming the full stop roughly doubles the hit rate a setup looks like it
    # needs, and printed a positive expectancy beside a break-even line it
    # appeared to fail -- the page arguing with itself where a reader checks.
    #
    # It is the break-even of the trade that was measured, the house stop and
    # target entered at the close, because that is the trade the hit rate it
    # is compared with belongs to. This plan's own levels can differ; the
    # verdict says so rather than pairing one trade's odds with another's.
    if (calib.avg_win_r is not None and calib.avg_loss_r is not None
            and calib.avg_loss_r < 0 < calib.avg_win_r - calib.avg_loss_r):
        breakeven = -calib.avg_loss_r / (calib.avg_win_r - calib.avg_loss_r)
    elif calib.hit_rate is not None:
        breakeven = calib.breakeven
    else:
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
    avoid_reason = ""
    if calib.hit_rate is None and calib_side * (calib.score or 0.0) < 0:
        verdict_bits.append(calib.note)         # the two readings disagree
    elif calib.hit_rate is None:
        verdict_bits.append("No historical sample was available to measure this "
                            "setup, so the odds are unverified.")
    else:
        verdict_bits.append(
            "%s scoring like this one reached target before stop %.0f%% of the "
            "time across %d comparable case%s (95%% range %.0f-%.0f%%)." % (
                "Past %s setups" % ("long" if calib_side > 0 else "short")
                if calib_side else "Setups",
                calib.hit_rate * 100, calib.bucket_samples,
                "" if calib.bucket_samples == 1 else "s",
                (calib.ci_low or 0) * 100, (calib.ci_high or 0) * 100))
        # The odds belong to the house trade: entered at the close, stop and
        # target a fixed number of ATR away. Pairing its break-even with this
        # plan's own reward printed "needs 22% at 1.12R" when the 22% was
        # measured at 1.50R.
        verdict_bits.append(
            "That trade, entered at the close with its stop %.4g ATR and its "
            "target %.4g ATR away (%.2fR), needs %.0f%% to break even." % (
                cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
                calib.reward_risk, breakeven * 100))
        if calib.reward_risk and rr and abs(rr / calib.reward_risk - 1.0) > 0.10:
            verdict_bits.append(
                "This plan's own stop and target give %.2fR, so its odds are not "
                "exactly the ones measured." % rr)

        if matched and calib.reliable and calib.expectancy_r is not None and \
                calib.expectancy_r <= 0 and cfg.get("require_positive_expectancy", True):
            action = "AVOID"
            avoid_reason = "negative_record"
            verdict_bits.append(
                "Measured expectancy is %+.2fR per trade, so this setup has lost "
                "money historically on this instrument. The technical picture does "
                "not override that." % calib.expectancy_r)
        elif calib.ci_low is not None and calib.ci_low <= breakeven <= (calib.ci_high or 1.0):
            verdict_bits.append("The 95% range takes in the break-even rate, so no "
                                "edge is proven. Treat this as a small or paper trade.")
        elif calib.hit_rate > breakeven:
            verdict_bits.append("Measured hit rate clears the break-even line.")

        if matched and baseline is not None and calib.expectancy_r is not None:
            if calib.expectancy_r > baseline:
                verdict_bits.append(
                    "%s on every day of the same history made %+.2fR a trade, so "
                    "this setup did %+.2fR better than the market's own drift."
                    % ("Buying" if direction > 0 else "Shorting", baseline,
                       calib.expectancy_r - baseline))
            else:
                verdict_bits.append(
                    "%s on every day of the same history made %+.2fR a trade, at "
                    "least as much as this setup, so the signals add nothing over "
                    "the market's own drift."
                    % ("Buying" if direction > 0 else "Shorting", baseline))

    if action != "AVOID" and _foreign(bars):
        risks.append("This is priced in %s, and the size treats your account as if "
                     "it were in %s too. If your account is in another currency, "
                     "convert it before trading the size shown."
                     % (_foreign(bars), _foreign(bars)))

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
        "Price on the correct side of the %s (%s)." % (
            vwap_label(ctx), fp(ctx.vwap[i] if np.isfinite(ctx.vwap[i]) else price)),
        "No scheduled news for this instrument inside your holding window.",
    ])
    if regime.trend == TRENDING:
        confirmations.append("Trend is confirmed by ADX %.0f. Favour holding for "
                             "the runner target." % ctx.adx[i])

    invalidation = ("The idea is wrong on %s close beyond %s. Exit, do not average down."
                    % (_a_bar(bars.interval), fp(stop)))

    return Plan(
        action=action, direction=direction, headline=headline,
        conviction=conviction,
        entry=entry, entry_type=entry_type,
        entry_rationale=("%s. Level used: %s." % (why.rstrip(". "), entry_rationale)
                         if entry_type == "limit" else why),
        trigger=trigger,
        stop=stop, stop_rationale=stop_rationale,
        target1=target1, target2=target2,
        target_rationale=target_rationale,
        reward_risk=rr,
        edge_verdict=" ".join(verdict_bits),
        avoid_reason=avoid_reason,
        position=position, invalidation=invalidation,
        risks=risks, confirmations=confirmations,
        **odds(breakeven_rate=breakeven),
    )


def _a_bar(interval: str) -> str:
    """"a daily", "an hourly" or "a 15-minute", to go before candle or close."""
    from .charts import bar_words
    words = bar_words(interval)
    return "%s %s" % ("an" if words[:1] in "aeh" else "a", words)


def _no_edge_reason(calib: Calibration, matched: bool) -> str:
    """Why the record does not support entering now, in a clause."""
    if not matched:
        return "no record was measured for this side"
    if not calib.reliable or calib.expectancy_r is None:
        return "the measured record for this setup is too thin to act on"
    if calib.expectancy_r <= 0:
        return "the measured record for this setup is not positive"
    if calib.ci_low is None or calib.ci_low <= calib.breakeven:
        return "its record is positive but not clear of chance"
    if calib.stable is False:
        return "the most recent third of its record disagrees with the rest"
    base = getattr(calib, "baseline_expectancy", None)
    if base is not None and calib.expectancy_r <= base:
        return ("it has not beaten taking the same side on every day, which "
                "made %+.2fR a trade" % base)
    return "the measured record for this setup is not strong enough to act on"
