"""The same answer, in words a beginner can act on.

The rest of the app talks in expectancy, R-multiples and confidence intervals,
which is right for someone who already knows what those mean and useless to
someone who does not. Nothing here is a second opinion: every sentence is built
from the figures already computed, so the plain version and the technical
version can never disagree.

Rules held to deliberately:

  No jargon. If a word needs a glossary it does not belong here.
  No new claims. Only a restatement of what was already measured.
  Say the uncomfortable part. "It does not know" is the most useful sentence
  this app produces, and it is the one a beginner most needs spelled out.
"""
from __future__ import annotations

from typing import Dict, List, Optional


def _money(value: Optional[float], currency: str = "") -> str:
    if value is None:
        return "?"
    symbol = "$" if currency in ("", "USD") else ""
    if abs(value) >= 1000:
        return "%s%s" % (symbol, format(value, ",.0f"))
    if abs(value) >= 1:
        return "%s%.2f" % (symbol, value)
    return "%s%.4f" % (symbol, value)


def _big(value: Optional[float]) -> str:
    """A large amount as a person would say it out loud."""
    if value is None:
        return "?"
    for limit, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if abs(value) >= limit:
            return "$%.1f %s" % (value / limit, word)
    return _money(value)


# --- the call ---------------------------------------------------------------

_WHAT_IT_MEANS = {
    "BUY": "The app thinks this is worth buying now.",
    "WAIT": "Do not buy yet. The app likes the idea but not today's price.",
    "AVOID": "Leave this one alone.",
    "SHORT": "The app thinks this is more likely to fall than rise.",
}

# A WAIT on the short side is waiting to bet on a fall. Explained as a buy, it
# told a beginner to buy at a price the app meant to sell at, with the stop on
# the wrong side.
_SHORT_WAIT = "Do not short yet. The app expects a fall, but not from today's price."
_FALLING_AVOID = "Leave this one alone. The signals point to a fall."


def counted(samples: int, worked: int) -> str:
    """The past examples behind a call, said the same way everywhere.

    They are pretend trades in a test on past prices, not trades anyone made,
    and "worked" is what the test measured: the trade reached its target
    before its stop. The public pages lead with this sentence and drop it
    from the explanation below (public.snapshot), so it must not vary.
    """
    return ("In tests on past prices, this same setup came up %d times and "
            "worked %d of them, reaching its target before its stop."
            % (samples, worked))


def explain_plan(plan, bars, hold_text: str = "") -> Dict:
    """The trade plan, said plainly.

    Returns a headline, a few short points, and one honest line about how sure
    the app actually is.
    """
    action = (plan.action or "").upper()
    currency = getattr(bars, "currency", "USD")
    points: List[str] = []
    short_side = (getattr(plan, "direction", 0) or 0) < 0 and action in ("WAIT", "SHORT")
    why_avoid = getattr(plan, "avoid_reason", "") or ""
    # The odds are measured on the side the signals lean. When that is down and
    # the plan is not a short, every count below is a record of betting on a
    # fall, and must not read as a record of buying.
    fall_odds = ((getattr(plan, "odds_direction", 0) or 0) < 0
                 and not short_side and action != "SHORT")

    headline = _WHAT_IT_MEANS.get(action, "The app has no clear view here.")
    if action == "WAIT" and short_side:
        headline = _SHORT_WAIT
    elif action == "AVOID" and why_avoid == "bearish_no_shorts":
        headline = _FALLING_AVOID

    # What to do, concretely.
    if action == "WAIT" and plan.entry and short_side:
        points.append(
            "Watch for the price to rise to %s. That is where the app would "
            "bet on a fall." % _money(plan.entry, currency))
    elif action == "WAIT" and plan.entry:
        points.append(
            "Watch for the price to reach %s. That is the app's buy price."
            % _money(plan.entry, currency))
    elif action == "BUY" and plan.entry:
        points.append("The app's buy price is %s." % _money(plan.entry, currency))
    elif action == "SHORT" and plan.entry:
        points.append(
            "Shorting means betting the price falls. It is riskier than buying "
            "because losses have no limit. Most beginners should skip it.")
    elif action == "AVOID" and why_avoid == "bearish_no_shorts":
        points.append(
            "The signals point down. The record below is how often a bet on "
            "the price falling worked here. Buying is the opposite bet, and "
            "shorting is switched off, so the answer is: do not buy.")
    elif action == "AVOID" and why_avoid == "negative_record":
        points.append(
            "Setups like this one have lost money here once trading fees are "
            "paid. That is a normal answer, not a broken one.")
    elif action == "AVOID":
        points.append(
            "There is no setup here worth the trading fees right now. That is "
            "a normal answer, not a broken one.")

    if plan.stop and plan.entry and short_side:
        points.append(
            "If the price kept rising to %s instead, the plan says get out. "
            "That is the point where the idea was wrong."
            % _money(plan.stop, currency))
    elif plan.stop and plan.entry:
        points.append(
            "If you bought and the price fell to %s, the plan says get out. "
            "That is the point where the idea was wrong."
            % _money(plan.stop, currency))

    if plan.target1:
        points.append("If it works, the app expects around %s."
                      % _money(plan.target1, currency))

    # How sure, in counted events rather than percentages alone.
    sure = ""
    if plan.probability is not None and plan.prob_samples:
        worked = int(round(plan.probability * plan.prob_samples))
        sure = counted(plan.prob_samples, worked)
        if fall_odds:
            sure += (" Here, worked means a bet on the price falling paid off, "
                     "because that is the way the signals lean.")

        # Saying only how often it worked invites the wrong conclusion, because
        # a loss here is usually not the whole stop: most trades that miss the
        # target are closed part-way rather than stopped out. Winning a third
        # of the time is fine when the wins are three times the size of the
        # losses, and that is the sentence a person needs in order to judge it.
        avg_win = getattr(plan, "avg_win_r", None)
        avg_loss = getattr(plan, "avg_loss_r", None)
        if avg_win and avg_loss:
            win_x = avg_win
            lose_x = abs(avg_loss)
            sure += (" When it worked it made about %.1f times what you put at "
                     "risk. When it did not, it usually lost about %.0f%% of "
                     "what you put at risk, not all of it, because the trade "
                     "is closed rather than left to run." % (win_x, lose_x * 100))
            # Those wins were made with the tested target. A plan whose target
            # was pulled in to a level in the way, or whose stop sits further
            # off, aims for something else, and a win of "1.5 times" would be
            # a promise this plan does not make.
            mine = getattr(plan, "reward_risk", None)
            tested = getattr(plan, "tested_reward_risk", None)
            if (not fall_odds and mine and tested
                    and abs(mine / tested - 1.0) > 0.10):
                sure += (" This plan's own target is %s, about %.1f times what "
                         "it puts at risk, so a win would be %s than those were."
                         % ("nearer" if mine < tested else "further",
                            mine, "smaller" if mine < tested else "bigger"))

        if plan.breakeven_rate:
            needed = int(round(plan.breakeven_rate * plan.prob_samples))
            if fall_odds:
                sure += (" A bet on a fall needed to work %d of those %d times "
                         "to come out even. That is the record for a fall, not "
                         "a reason to buy." % (needed, plan.prob_samples))
            elif worked <= needed:
                sure += (" To come out even it needed to work %d of those %d "
                         "times, so this has not been shown to make money."
                         % (needed, plan.prob_samples))
            elif (getattr(plan, "prob_low", None) is not None
                  and plan.prob_low <= plan.breakeven_rate):
                # Ahead of the line, but by less than chance alone moves a
                # count this size. Saying only "it cleared that bar" next to a
                # call to wait made the wait look like a mistake.
                sure += (" To come out even it only needed to work %d of those "
                         "%d times. It cleared that bar, but not by enough to "
                         "rule out luck." % (needed, plan.prob_samples))
            else:
                sure += (" To come out even it only needed to work %d of those "
                         "%d times, so it cleared that bar."
                         % (needed, plan.prob_samples))
        if not plan.prob_reliable:
            sure += " That is too few times to be confident about."

    # What a bad day costs, which is the number beginners never see.
    position = plan.position or {}
    risk = ""
    if position.get("risk_amount"):
        risk = ("Buying %s of these would put %s at risk."
                % (_trim(position.get("quantity")),
                   _money(position["risk_amount"], currency)))
        if position.get("gap_loss") and position.get("gap_multiple", 0) > 1.4:
            risk += (" But if the price jumped down overnight, which happens, "
                     "you could lose about %s instead."
                     % _money(position["gap_loss"], currency))

    hold = ""
    if hold_text:
        hold = "Trades like this usually last %s." % hold_text

    return {"headline": headline, "points": [p for p in points if p],
            "sure": sure, "risk": risk, "hold": hold}


def _trim(value) -> str:
    if value is None:
        return "some"
    if float(value) >= 1:
        return "%g" % float(value)
    return "%.4f" % float(value)


# --- help beside each figure ---------------------------------------------------

# One or two sentences on each figure of the analysis page that a first-time
# investor may not know, said the way this code works the figure out rather
# than the way a textbook would. The page shows each behind a "?" beside its
# figure (templates/_help.html). name finishes "What ... means"; text is the
# answer. A word in braces is a setting, filled in by help_for, so the words
# match the numbers on the page.
HELP: Dict[str, Dict[str, str]] = {
    # The call, and the market it was made in.
    "call": {"name": "the call", "text": (
        "BUY means setups like this one made money on this instrument before, "
        "after costs, by more than luck and more than buying on any day did, and "
        "the price is not stretched or just under a level; SHORT is the same for "
        "a fall, and appears only when shorting is on in Settings. WAIT means the "
        "signals lean one way but that record, or the price, is not there yet, "
        "and AVOID means there is no setup worth the costs, the signals point "
        "down with shorting off, or this setup has lost money here.")},
    "regime": {"name": "the market's state", "text": (
        "How the price has been moving lately: trending when the ADX reading is "
        "25 or more, ranging at 17 or less, transitional in between. Volatility "
        "is high, normal or low against this instrument's own last 120 bars, and "
        "up, down or sideways is the way it leans.")},
    # What price is doing (the course's reading of a chart, bot/levelview.py).
    "structure": {"name": "the trend on these bars", "text": (
        "A swing high is a high above the two highs before it and not below the two "
        "after it (a swing low is the mirror). The trend turns up when a bar closes above "
        "the last swing high and down when one closes below the last swing low; a wick "
        "past it, or a close exactly on it, does not count.")},
    "flip_level": {"name": "the price that would turn it", "text": (
        "The last swing point on the other side of the trend. A bar closing beyond it "
        "would turn the trend, so it is the price past which the reading is wrong.")},
    "sweep": {"name": "an old high or low taken", "text": (
        "Price trading past an earlier swing high or low, yesterday's high or low, or an "
        "earlier session's. The course reads a run past one that closes back inside as "
        "stops being taken before a turn; Orenth tests that reading in its Setup lab.")},
    "draw": {"name": "the key levels", "text": (
        "Prices the course expects traders to watch: yesterday's and last week's highs "
        "and lows, the session ranges, and old swing highs and lows price has not traded "
        "through yet. Equal highs or lows are two or more within a tenth of a typical "
        "bar's range of each other.")},
    "prior_day": {"name": "yesterday's high and low", "text": (
        "The high, low and close of the last whole trading day before today; for futures "
        "and coins a trading day runs from 18:00 to 17:00 New York time. Taken means price "
        "has traded past it today (this week, for last week's).")},
    "typical_bar": {"name": "the distance in typical bars", "text": (
        "How far the level is from the last close, counted in the average range of one "
        "bar over the last 14 bars. 2 means about two ordinary bars' moves away.")},
    "fvg": {"name": "a gap a fast move left", "text": (
        "Three candles where the first's high and the third's low do not overlap (the "
        "mirror in a fall) and the middle one moved that way: the course calls the "
        "stretch between them a fair value gap and expects price to come back into it. It "
        "stands until a bar closes through it, price closes past a newer swing point, or "
        "it is 150 bars old.")},
    "ifvg": {"name": "a close through a gap", "text": (
        "A close back through the bottom of the gaps the current rise left (the top, in a "
        "fall), which the course calls an inverse fair value gap and reads as an early "
        "sign of a turn. The trend itself changes only on a close past the last swing "
        "point.")},
    "half_line": {"name": "the halfway line", "text": (
        "The middle of the last move, from its swing low to the highest high since (the "
        "mirror in a fall), fixed once a candle closes against the move. The course buys a "
        "rise in the lower, cheap half and sells a fall in the upper, dear half.")},
    "opening_gap": {"name": "the opening gap", "text": (
        "The jump between the previous close and the day's first price: the 17:00 close "
        "and the 18:00 reopen for futures, the 16:00 close and the 9:30 open for stocks. "
        "It fills when price trades back to the previous close.")},
    "session_ny": {"name": "the sessions", "text": (
        "The course splits the day in New York time: Asia from 18:00 to 3:00, London "
        "from 3:00 to 8:30, then New York. A session's range counts only when the data "
        "has at least 80% of its bars.")},
    "window_ny": {"name": "the course's times", "text": (
        "The course watches 9:30 to 9:50, takes its entries from 9:50 to 10:10 and stops "
        "looking for new trades at 10:30, New York time. Orenth's Setup lab measures how "
        "trades entered at each time of day did.")},
    "bias": {"name": "the day's lean", "text": (
        "Orenth's own rules for how the course reads the day: the 4-hour trend (built "
        "from hourly bars), then whether the hourly took an old high or low today and "
        "broke its last swing the other way. It is shown beside how often it called the "
        "day before, and it never changes the call.")},
    "pair_check": {"name": "two related markets", "text": (
        "The course compares markets that usually move together, such as the S&P 500 "
        "and Nasdaq 100 futures, and reads one making a higher high while the other does "
        "not as a warning. Here their last two swing points are compared over the last "
        "300 bars both traded.")},

    # The plan.
    "entry": {"name": "the entry", "text": (
        "Where the plan gets in. A BUY or SHORT gets in at today's price "
        "(market); a WAIT names a better price to wait for (limit): the nearest "
        "support level (resistance, for a short), volume-weighted average or "
        "21-bar average at least 0.3 ATR away, or 0.5 ATR off when there is "
        "none.")},
    "stop": {"name": "the stop", "text": (
        "Where the plan says the idea was wrong and gets out. It is {stop} ATR "
        "from the entry, or a little past the nearest support level (resistance, "
        "for a short) when that is further off but within 2.5 ATR.")},
    "target": {"name": "the first target", "text": (
        "Where the plan takes its profit: {target} ATR from the entry, or just "
        "short of the first level in the way that still leaves at least as much "
        "to gain as the stop risks. The odds on this page were measured with the "
        "target {target} ATR away.")},
    "runner": {"name": "the runner", "text": (
        "A second, further target for part of the position, at least {runner} "
        "ATR from the entry. Nothing on this page measures how often it is "
        "reached.")},
    "reward_risk": {"name": "reward to risk", "text": (
        "How far the first target is from the entry, against how far the stop "
        "is: 2.00 : 1 means twice as far. Needs is how often trades like this "
        "had to reach their target to come out even after costs, worked out from "
        "what their wins and losses here were really worth.")},
    "size": {"name": "the size", "text": (
        "How many shares or coins the plan would buy: enough that reaching the "
        "stop loses {risk}% of the {account} account set in Settings, rounded "
        "down to whole shares for a stock, and never more than {cap}% of the "
        "account in one position.")},
    "risk": {"name": "the amount at risk", "text": (
        "What reaching the stop would lose: the size times the distance from "
        "entry to stop, before trading costs, and that as a share of the "
        "account.")},
    "gap": {"name": "the gap risk", "text": (
        "A stop sells at the first price there is, which can be well past it "
        "after a gap. This is the loss if the price moved against you from just "
        "short of the stop by as much as this instrument's worst single bar, "
        "close to close, in the history read.")},

    # The odds.
    "hit_rate": {"name": "reached target first", "text": (
        "How often past trades scored about as strongly as today's, on this "
        "instrument and on the same side, reached the target before the stop "
        "within {window}. One that reached neither counts as a miss, and a bar "
        "that touched both counts as the stop.")},
    "range": {"name": "the 95% range", "text": (
        "Where the true rate probably lies: if the future behaves like the past, "
        "19 times in 20 it is inside this range. It is wider for fewer trades, "
        "and wider again because trades close together share most of their bars "
        "and count as fewer separate tests.")},
    "expectancy": {"name": "expectancy", "text": (
        "The average result of these past trades in R, the amount each one "
        "risked between entry and stop, after a round trip of costs: +0.10R "
        "means 10 cents kept for every dollar risked. A trade that reached "
        "neither stop nor target counts at what it was worth when its time ran "
        "out.")},
    "baseline": {"name": "taking the same side any day", "text": (
        "The same trade taken at every point the test looked at, whatever the "
        "signals said: what the market's own drift paid here. A setup shows "
        "skill only if it beats this, not just zero.")},
    "breakeven": {"name": "the break-even rate", "text": (
        "How often a trade like this has to reach its target to come out even "
        "after costs, from what the wins and losses here were really worth. Most "
        "misses are closed before the stop, so it is usually lower than the "
        "stop and target alone suggest.")},
    "sample": {"name": "the sample", "text": (
        "How many past trades these figures come from: setups scored about as "
        "strongly as today's, on the same side, and fewer than {minimum}, or a "
        "history too short to test well, is called too few to trust. Trades a "
        "few bars apart share most of their bars, so they count as fewer "
        "separate tests when the 95% range is worked out.")},
    "hold": {"name": "the typical hold", "text": (
        "The average number of bars these past trades lasted: until the target "
        "or the stop, or until the {bars}-bar limit, when they were closed at "
        "the price then.")},
    "holdout": {"name": "the most recent third", "text": (
        "The average result of only the newest third of these trades. If it and "
        "the whole record disagree, one making money and the other losing it, "
        "the edge may be fading, and the app does not say BUY.")},
    "costs": {"name": "trading costs", "text": (
        "Every past trade is charged a round trip of costs: {equity}% of the "
        "price for a stock or fund and {crypto}% for a coin, as set in "
        "Settings, {futures}% for a futures contract, or {other}% for anything "
        "else (a basis point is a hundredth "
        "of a percent). In R that is the cost divided by the distance to the "
        "stop, so the closer the stop, the more of each trade it eats.")},
    "atr": {"name": "ATR", "text": (
        "The average true range: how far the price usually moves in one bar, "
        "gaps included, smoothed over the last 14 bars. Stops and targets are "
        "set in ATR so they suit how much this instrument moves.")},
    "curve": {"name": "this table", "text": (
        "Each row holds the past trades whose conviction, how strongly the "
        "strategies leaned from 0 to 1 whichever way, fell in that band: how "
        "many there were, and how often they reached the target first. If the "
        "rate does not climb from row to row, how strongly the signals lean says "
        "nothing here.")},

    # Where the signal comes from.
    "family": {"name": "family", "text": (
        "A group of strategies that read the chart the same kind of way: trend, "
        "mean reversion, breakout, volume, structure or pattern.")},
    "lean": {"name": "lean", "text": (
        "The family's weighted opinion today, from -1 (down) to +1 (up).")},
    "active": {"name": "active", "text": (
        "How many of the family's strategies have an opinion today, out of all "
        "of them.")},
    "combined": {"name": "combined", "text": (
        "All the strategies together, a later voice in the same family counting "
        "less than the first.")},
    "with_news": {"name": "with news", "text": (
        "The combined reading with the headlines and posts given {news}% of the "
        "say, and cut back when the two disagree. It is the conviction the call "
        "was made on.")},

    # Each strategy, and the words its notes use.
    "score": {"name": "score", "text": (
        "The strategy's opinion today, from -1 (strongly down) to +1 (strongly "
        "up).")},
    "weight": {"name": "weight", "text": (
        "How much its opinion counts: its starting weight, raised when it suits "
        "today's kind of market and lowered when it does not, scaled by its "
        "record on this instrument, and by how sure the strategy is of today's "
        "reading (many count half or less when they see nothing clear, such as "
        "no cross or breakout).")},
    "sees": {"name": "what it sees", "text": (
        "What it read on the chart, in traders' shorthand. The common words "
        "follow.")},
    "rsi": {"name": "RSI", "text": (
        "Relative strength: how hard the price rose against how hard it fell "
        "over the last 14 bars, from 0 to 100. Above 70 is often called "
        "stretched up, below 30 stretched down.")},
    "ema": {"name": "EMA", "text": (
        "A moving average of the last 9, 21, 50 or 200 bars that counts recent "
        "bars more. EMA 9>21>50 means the shorter averages sit above the longer "
        "ones, as they do in a rise.")},
    "macd": {"name": "MACD", "text": (
        "The gap between a 12-bar and a 26-bar moving average; its histogram is "
        "that gap against its own 9-bar average, and a growing one means the "
        "move is speeding up.")},
    "adx": {"name": "ADX", "text": (
        "How strong the trend has been over 14 bars, whichever way it points, "
        "with +DI and -DI saying which way. Here 25 or more counts as "
        "trending.")},
    "vwap": {"name": "VWAP", "text": (
        "The average price weighted by how much traded at each price: the "
        "session's own on intraday bars, the last 20 bars' on daily ones.")},
    "bollinger": {"name": "Bollinger %B", "text": (
        "Where the price sits in a band two standard deviations either side of "
        "its 20-bar average: 0 is the bottom edge and 1 the top.")},
    "volume": {"name": "OBV and A/D", "text": (
        "Running totals of volume: OBV adds a bar's volume when it closed up and "
        "takes it off when it closed down, and A/D weighs it by where the bar "
        "closed in its range. Here their change over the last 20 and 10 bars is "
        "read as a share of the volume traded then.")},

    # What this instrument taught the bot.
    "setups": {"name": "setups", "text": (
        "How many pretend trades the strategy made over this instrument's "
        "history: one each time it had a clear opinion at a point the test "
        "looked at.")},
    "strategy_hit": {"name": "hit rate", "text": (
        "How often those trades reached the target before the stop.")},
    "net_r": {"name": "net R", "text": (
        "Their average result after costs, in R: what each dollar risked made or "
        "lost, on average.")},
    "weight_given": {"name": "weight given", "text": (
        "The multiplier its record gave it in today's call, from 0.25 to 1.9: "
        "its record in all conditions blended with its record in markets like "
        "today's, and pulled toward 1 while the record is short.")},

    # The news.
    "news": {"name": "the news reading", "text": (
        "The recent headlines and posts read from -1 (bad news) to +1 (good "
        "news), headlines counting about twice as much as posts and newer items "
        "more than older ones. It has {news}% of the say in the call's "
        "conviction.")},
}


def help_for(cfg: Dict, interval: str = "1d") -> Dict[str, Dict[str, str]]:
    """HELP with the settings' own numbers in it, for one bar interval."""
    from .config import DEFAULTS, describe_horizon, horizon_for

    def setting(key):
        value = cfg.get(key)
        return DEFAULTS[key] if value is None else value

    bars = horizon_for(interval, cfg)
    filled = {
        "stop": "%g" % float(setting("stop_atr_multiple")),
        "target": "%g" % float(setting("target_atr_multiple")),
        "runner": "%g" % float(setting("runner_atr_multiple")),
        "risk": "%g" % float(setting("risk_per_trade_pct")),
        "cap": "%g" % float(setting("max_position_pct")),
        "account": "{:,.0f}".format(float(setting("account_size"))),
        "bars": bars,
        "window": describe_horizon(interval, bars),
        "minimum": int(setting("min_bucket_samples")),
        "equity": "%g" % (float(setting("cost_bps_equity")) / 100.0),
        "crypto": "%g" % (float(setting("cost_bps_crypto")) / 100.0),
        "other": "%g" % (float(setting("cost_bps_other")) / 100.0),
        "futures": "%g" % (float(setting("cost_bps_futures")) / 100.0),
        "news": "%d" % round(float(setting("sentiment_weight")) * 100),
    }
    return {key: {"name": h["name"], "text": h["text"].format(**filled)}
            for key, h in HELP.items()}


# --- the company ------------------------------------------------------------

def explain_company(research) -> Dict:
    """What the filings say, without the accounting words."""
    points: List[str] = []
    asked = getattr(research, "sec_history", None)
    history = asked or {}
    facts = getattr(research, "fundamentals", None)
    scores = getattr(research, "scores", None) or {}

    if asked is None:
        # The filings were asked for and did not come back. A company that
        # files may simply not have answered this time, and saying it does
        # not file at all would be false about NVIDIA.
        return {"available": False,
                "why": ("Its filed accounts could not be read just now, so there "
                        "are no company numbers to put into plain words here. "
                        "The price side of the app still works.")}
    if not history.get("available"):
        return {"available": False,
                "why": ("This is not a company that files accounts with the US "
                        "regulator, so there are no company numbers to read. "
                        "That is normal for crypto, funds and foreign listings. "
                        "The price side of the app still works.")}

    table = history.get("table") or {}
    currency = history.get("currency") or "USD"

    def latest(concept):
        rows = table.get(concept) or []
        return rows[-1]["value"] if rows else None

    revenue, profit = latest("revenue"), latest("net_income")
    if revenue:
        points.append("Last year it sold %s worth of things." % _big(revenue))
    if revenue and profit is not None:
        kept = profit / revenue
        if profit < 0:
            points.append("It lost money doing it.")
        else:
            points.append(
                "It kept about %.0f cents of every dollar as profit." % (kept * 100))

    # Turn each score into one sentence a person can act on.
    f = scores.get("scores", {}).get("piotroski") if scores.get("available") else None
    if f is not None and f.usable:
        if f.value >= 7:
            points.append("Its finances got better in most ways last year.")
        elif f.value <= 3:
            points.append("Its finances got worse in most ways last year.")
        else:
            points.append("Some parts got better last year, some got worse.")

    z = scores.get("scores", {}).get("altman") if scores.get("available") else None
    if z is not None and z.usable:
        if z.value < 1.81:
            points.append(
                "Warning: by one well-known measure this company is in the "
                "range where businesses get into serious money trouble.")
        elif z.value > 2.99:
            points.append("It does not look close to running out of money.")

    a = scores.get("scores", {}).get("accruals") if scores.get("available") else None
    if a is not None and a.usable and a.value > 0.10:
        points.append(
            "Its profit on paper is much bigger than the cash it actually "
            "collected. That gap often shrinks later.")

    value = getattr(research, "valuation", None) or {}
    if value.get("usable") and value.get("upside") is not None:
        if value["upside"] > 0.20:
            points.append(
                "One way of valuing it suggests it is cheaper than it looks. "
                "Valuations depend heavily on guesses about the future.")
        elif value["upside"] < -0.20:
            points.append(
                "One way of valuing it suggests today's price already assumes "
                "a lot of growth.")

    return {"available": True, "points": points, "currency": currency,
            "note": ("Every number here comes from a document the company filed "
                     "with the US regulator. You can open the filing and check "
                     "it yourself.")}
