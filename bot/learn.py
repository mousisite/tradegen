"""The Learn pages: each price-action idea from TJR's video, beside what Orenth measured.

/learn and /learn/<slug> explain the ideas a free day-trading course teaches
(TJR, "How To Start Day Trading In 2026"), in Orenth's own words, and set
each one beside what the Setup lab, the claims and the daily strategy test
measured. The copy here says what the idea is, what the course teaches (with
the video's timestamps, never its words), the exact rule Orenth codes, with
the app's own choices marked as such, and the limits of the data.

Every number on these pages comes from bot/labstore.py (the Setup lab and the
claims) or from the daily strategy test (bot/public.py). None is typed here:
the copy below never states a result. Until the lab has run, a page says it
is being tested and asks search engines to wait.

Verdicts are said with labstore.words() and labstore.claim_words(), so a
setup that lost money says so in the same words everywhere.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import charts, clock, labstore, public, setups
from .market import format_price

INTERVAL_WORDS = {"5m": "5-minute", "15m": "15-minute"}
RULE_WORDS = {"2r": "a target twice the risk",
              "level": "a target at the next old high or low"}
SYMBOL_NAMES = {sym: name for sym, name, _ in setups.LAB_SYMBOLS}
UNIVERSE = [name for _, name, _ in setups.LAB_SYMBOLS]
SOURCE = ("Rules as taught in TJR's free video “%s”. Orenth is not connected "
          "with TJR or the video; the descriptions here are Orenth's own words."
          % setups.VIDEO["title"])

# How the Setup lab measures, said once and shown on the hub and every topic.
METHOD = (
    "Every time a setup appears, the lab takes a pretend trade at the close of "
    "that bar, in the direction the course would trade it.",
    "The stop goes where the course puts it for that setup, usually just past the "
    "high or low that was taken.",
    "Two targets are measured, each on its own: twice the risk, and the next old "
    "high or low that is at least as far away as the risk (twice the risk when "
    "there is none).",
    "A trade ends at its stop, at its target, or at the 16:00 New York close, "
    "whichever comes first. A bar that touches both the stop and the target counts "
    "as a loss, and a price that jumps past the stop fills where it opened.",
    "A setup whose stop would sit closer than a tenth of a usual bar's range is "
    "skipped, and counted.",
    "Trading costs come off every trade. A cost is counted against the trade's "
    "risk, so the same cost takes more off a trade whose stop is close, and can take "
    "more than the whole risk. “Before costs” is the same trades with "
    "nothing taken off. Hit rate is the share of trades that kept money after costs.",
    "Each trade is set beside random trades: five bars from other days at the same "
    "half hour of New York time, on the same side, with a stop and target the same "
    "size against the usual bar's range then, and the same exits and costs. A setup "
    "that only rides the market's own drift at that time of day looks no better "
    "than these.",
    "Results are in R, the amount risked: +0.10R means 10 cents kept for every "
    "dollar risked, on average, after costs.",
    "The margin of error counts the trades of one day together, over all the "
    "instruments added up, because they win or lose together, and is wider when the "
    "days are few. A result is called "
    "only when its whole margin is on one "
    "side of zero, with at least %d trades on at least %d separate days."
    % (labstore.MIN_TRADES, labstore.MIN_DAYS),
    "The last third of the days is also read on its own, to see whether the "
    "result held later in the period.",
    "Re-run every night on the newest prices. A trade still open when the prices "
    "end is left for a later night.",
)

LIMITS = (
    "About 60 days of 5- and 15-minute prices: the most the free data source keeps. "
    "That is one stretch of market conditions, and a setup can behave differently "
    "in another.",
    "US stocks and funds have prices here for the day session only (9:30 to "
    "16:00), so they have no Asia or London ranges.",
    "Days with too few bars, and the futures' contract-change days, are left out, "
    "and counted.",
    "The S&P 500 futures and the S&P 500 fund follow the same index, as do the "
    "Nasdaq 100 futures and fund, so eight instruments are less evidence than eight "
    "separate markets would be.",
    "Who traded cannot be seen in price bars. These pages say what price did, "
    "never who did it or why.",
)


# --- the copy ------------------------------------------------------------------------
#
# what:   the idea in everyday words.
# course: what the course teaches, in our words, each with the video's timestamp.
# rule:   the exact rule Orenth codes, each marked "course" (as the video
#         teaches it) or "app" (a detail the video leaves open, chosen here).
# limits: what this idea cannot tell you, beyond the lab's general limits.

CONCEPTS: Dict[str, Dict] = {
    "liquidity-sweep": {
        "what": "An old high is a price the market rose to and turned back from. "
                "Many people who sold there put their stop orders just above it, and "
                "many waiting to buy a breakout put orders there too. A sweep is a bar "
                "that trades past such an old high (or low) and then closes back on "
                "the other side of it.",
        "course": (
            ("1:21:32-1:44:37", "Old highs and lows are where orders wait. The course "
             "calls those waiting orders liquidity, and says price is drawn to them."),
            ("1:36:14-1:44:37", "It teaches waiting for price to run those orders and "
             "turn, rather than trading at the old level itself."),
            ("1:40:27-1:43:34", "It also says a run past an old high is not a reason "
             "to trade on its own: what comes after it matters."),
        ),
        "rule": (
            ("app", "A swing high is a bar whose high is above the two highs before it "
             "and at least as high as the two after it (mirror for a swing low). It is "
             "only known two bars later, and is used from then on."),
            ("course", "A swing high is untaken until a later bar trades above it."),
            ("app", "Only swings from the last 300 bars can be taken."),
            ("course", "A sweep is a bar that trades past one or more untaken swing "
             "highs (or lows). Every level it trades past counts as taken, and cannot be "
             "taken again."),
            ("course", "Sweep and reclaim: the same bar closes back on the near side of "
             "every level it took. A bar that takes highs and lows at once is left out."),
            ("course", "After lows are taken the setup expects a rise, after highs a "
             "fall."),
        ),
        "limits": ("Stop orders cannot be seen in price bars. Where they sit is the "
                   "course's reasoning, not something measured here.",),
    },
    "previous-day-levels": {
        "what": "Yesterday's highest and lowest prices. The course marks them before "
                "the open, as places where orders wait, and watches for price to trade "
                "past one and come back.",
        "course": (
            ("2:05:49-2:08:58", "Mark yesterday's high and low before the session, and "
             "read a move past one that closes back inside yesterday's range as those "
             "orders being taken."),
            ("2:06:52-2:07:57", "After one side of yesterday's range is taken, the "
             "course often looks for price to travel to the other side."),
        ),
        "rule": (
            ("course", "Yesterday is the last whole trading day before today. For "
             "futures and coins a trading day starts at 18:00 New York time; for "
             "stocks it is the day session."),
            ("app", "A day with under 80% of its usual bars is not used for a range."),
            ("app", "Taken: the first bar today whose low goes below yesterday's low, or "
             "whose high goes above yesterday's high. A bar that does both is left out."),
            ("course", "Swept: that bar closes back inside yesterday's range."),
        ),
        "limits": (),
    },
    "sessions": {
        "what": "Futures and coins trade almost around the clock. The course splits "
                "the day into three sessions in New York time: Asia from 18:00 to "
                "03:00, London from 03:00 to 08:30, then New York. It watches New York "
                "trade past the high or low an earlier session left.",
        "course": (
            ("0:44:23-0:45:27", "Every time is read in New York time, around the 9:30 "
             "stock market open."),
            ("1:47:58-2:05:49", "Each session leaves a high and a low; a later session "
             "often trades past one of them."),
            ("4:29:07-4:37:38", "The course watches 9:30 to 9:50, takes its entries "
             "between 9:50 and 10:10, and stops looking for new trades at 10:30."),
        ),
        "rule": (
            ("course", "Asia runs 18:00 to 03:00, London 03:00 to 08:30, New York from "
             "08:30, all New York time, which changes with daylight saving."),
            ("app", "A session with under 80% of its usual bars has no range, and is "
             "counted as left out."),
            ("app", "New York's take counts from 08:30 to 12:00: the first bar past "
             "London's high or low (Asia's when London's bars are incomplete). A bar "
             "past both sides is left out."),
            ("course", "Session sweep: that bar closes back inside the range it traded "
             "past."),
        ),
        "limits": ("US stocks and funds have no Asia or London prices here, so this "
                   "setup is measured on futures and coins only.",),
    },
    "equal-highs-lows": {
        "what": "Two or more old highs that stopped at almost the same price. People "
                "see them as a ceiling and put their stops just above, so the course "
                "treats them as an obvious target for price.",
        "course": (
            ("1:37:16", "Highs at the same level are a clear pool of orders."),
            ("2:20:42-2:23:53", "Equal highs or lows, exact or just under, draw price "
             "to them, and price often turns once it has been there."),
        ),
        "rule": (
            ("app", "Equal means two or more untaken swing highs within a tenth of a "
             "usual bar's range (the average range of the last 14 bars) of each other, "
             "with no bar between them above the higher one. Their price is the "
             "highest of them. Mirror for lows."),
            ("course", "Equal levels swept: a bar trades past them and closes back on "
             "the near side, as a sweep and reclaim."),
        ),
        "limits": (),
    },
    "break-of-structure": {
        "what": "A rise is a run of higher highs and higher lows. The course says the "
                "rise is over only when a bar closes below its last low; a fall, when a "
                "bar closes above its last high. That close is a break of structure.",
        "course": (
            ("1:01:21-1:12:00", "Highs and lows, and the trend they make."),
            ("2:33:23-2:43:56", "Only a close past the last swing changes the trend. A "
             "wick past it does not."),
            ("2:42:53-2:43:56", "The turn it looks for comes in two parts: an old high "
             "or low is taken, then price breaks its last swing the other way."),
        ),
        "rule": (
            ("app", "Until a bar first closes past a swing point there is no trend."),
            ("course", "In a rise, the swing low to watch is the latest one confirmed, "
             "even if it is lower than the one before."),
            ("course", "The trend turns down on the first close below that low. A wick "
             "below it, or a close exactly on it, does not count. Mirror in a fall."),
            ("app", "Sweep, then break: the break comes within 24 bars of an old high or "
             "low being taken (10 on daily bars), in the opposite direction, and before "
             "price goes past the extreme of that take."),
        ),
        "limits": (),
    },
    "fair-value-gap": {
        "what": "Three bars in a row where the middle one moves fast: in a rise, the "
                "first bar's high stays below the third bar's low. The prices in between "
                "were traded only by the one fast bar. The course calls that stretch a "
                "fair value gap and expects price to come back to it.",
        "course": (
            ("2:46:04-3:02:46", "How a gap forms: three candles, the middle one moving."),
            ("2:51:15-2:58:34", "Price tends to come back into a gap, and the course "
             "enters when it does so in the trend's direction."),
            ("3:02:46", "Gaps one after another form a stack."),
            ("3:08:00-3:15:21", "A gap that has been used, or skipped past, is deleted."),
        ),
        "rule": (
            ("course", "Rising gap: the third bar's low is above the first bar's high and "
             "the middle bar closed up. Mirror for a falling gap."),
            ("app", "The first and third bars' colours do not matter, and any size "
             "counts."),
            ("course", "A wick through a gap keeps it; a close through it ends it."),
            ("app", "A rising gap also ends at a close above a swing high confirmed "
             "after it formed, or after 150 bars."),
            ("course", "Gaps with no down bar between their middle bars are one stack."),
            ("app", "Retest: the first time a bar trades back into the newest gap without "
             "closing through it, in the trend's direction, and that bar closes in the "
             "trend's direction. One gap gives one retest."),
        ),
        "limits": (),
    },
    "inverse-fair-value-gap": {
        "what": "In a rise, a bar closes below the bottom of a gap the rise left. The "
                "course reads that close as an early sign the rise has turned, before "
                "price breaks its last low.",
        "course": (
            ("3:42:47-3:58:38", "A gap that price closes through changes its role, and "
             "that close is an early sign of a turn."),
            ("3:50:08", "It often comes before the break of structure, at a better "
             "price."),
        ),
        "rule": (
            ("course", "In a rise, an inverse gap is a close below the bottom of the "
             "rise's newest gap, or the bottom of its stack when gaps are stacked. "
             "Mirror in a fall."),
            ("course", "Only gaps the current rise made count."),
            ("app", "The trend itself still turns only on a close past the last swing. "
             "The inverse gap is a separate, earlier sign."),
        ),
        "limits": (),
    },
    "premium-discount": {
        "what": "Take the last move, from its low to its high. Halfway between them is "
                "the 50% line. The course calls the lower half of a rise the cheap half "
                "and the upper half the dear half.",
        "course": (
            ("3:58:38-4:13:16", "Buy only in the cheap half of a rise, and sell only in "
             "the dear half of a fall."),
        ),
        "rule": (
            ("course", "In a rise, the move runs from the latest confirmed swing low to "
             "the highest high since it. The line is halfway between them."),
            ("app", "The line is fixed at the first down bar after the high. A new high "
             "frees it again, until the next down bar."),
            ("app", "One touch per move: the first bar whose low reaches the line and "
             "whose close stays above the move's low."),
            ("course", "The setup also needs that bar to close up (down, in a fall)."),
        ),
        "limits": (),
    },
    "sweep-break-retest": {
        "what": "The course's main model, in four steps: an old high or low is taken, "
                "price breaks its last swing the other way, it pulls back a little into "
                "a gap the new move left or to the new move's 50% line, and a bar turns "
                "back in the new direction there.",
        "course": (
            ("2:44:58", "Three stages: the setup's potential, its confirmation, and its "
             "continuation."),
            ("4:02:52-4:11:09", "The pullback into the gap or the 50% line is where the "
             "trade is taken."),
            ("6:24:12-6:30:28", "The stop goes past the high or low that was taken, and "
             "targets are old highs and lows on the other side."),
        ),
        "rule": (
            ("course", "Step 1: an old high or low is taken (a swing point, yesterday's "
             "high or low, or an earlier session's range). A later bar going further "
             "moves the extreme."),
            ("app", "Step 2 within 24 bars (10 on daily bars): a break of structure, or "
             "an inverse gap, in the opposite direction, with price still inside the "
             "extreme."),
            ("course", "Step 3: the zone is a gap formed after the take, or the 50% line "
             "of the move from the extreme."),
            ("app", "Step 4 within 36 bars (15 on daily bars): the first bar that reaches "
             "the zone, closes back above it (below, for a fall), closes up, and stays "
             "inside the extreme."),
            ("app", "It is called off when price goes past the extreme, the trend turns "
             "back, or time runs out. One take gives at most one setup."),
            ("app", "The lab also re-runs it with wider swings (three bars each side), "
             "to see whether the result depends on that choice."),
        ),
        "limits": (),
    },
    "opening-gaps": {
        "what": "Futures pause from 17:00 to 18:00 New York time each weekday, and "
                "over the weekend. The price they reopen at can differ from where they "
                "stopped. That jump is an opening gap, and the course expects price to "
                "trade back across it.",
        "course": (
            ("3:16:24-3:23:47", "The jump between the 17:00 close and the 18:00 reopen "
             "tends to get filled."),
            ("3:19:32", "So does the weekend's jump, between Friday's close and "
             "Sunday's reopen."),
            ("3:25:57", "Often by Monday."),
        ),
        "rule": (
            ("course", "New day's gap: the last close before 17:00 against the first "
             "open from 18:00. Weekend gap: Friday's close against Sunday's 18:00 open."),
            ("app", "Filled means price traded back to the close before the gap."),
            ("app", "Days when the futures switch to the next contract are left out: "
             "the jump there is the switch, not the market."),
        ),
        "limits": ("Coins never pause, so they have no opening gap. A stock's gap "
                   "(the 9:30 open against the day before's close) is not measured "
                   "here.",),
    },
    "smt-divergence": {
        "what": "Two markets that usually move together, like the S&P 500 and the "
                "Nasdaq 100. At a high, one makes a higher high and the other does not. "
                "The course reads that disagreement as a warning that both may turn.",
        "course": (
            ("4:13:16-4:28:04", "When related markets disagree at a high that was "
             "taken, the course expects both to fall; the mirror at lows."),
        ),
        "rule": (
            ("app", "Pairs: the S&P 500 and Nasdaq 100 futures, their two funds, and "
             "Bitcoin with Ether, matched bar by bar on 5-minute prices."),
            ("app", "Swing highs of the two are paired when they come within 2 bars of "
             "each other. It counts when one is higher than the one before and the other "
             "is lower, and the higher one took an old high."),
            ("app", "It worked when the market that made the higher high falls one usual "
             "bar's range before it rises one, within 36 bars."),
        ),
        "limits": ("It needs two markets at once, so it is a measured claim here, not "
                   "a strategy scored on a single chart.",),
    },
    "daily-bias": {
        "what": "Before the open, which way the course's method would lean for the "
                "day: up, down, or unclear. It reads the 4-hour and hourly charts, top "
                "down.",
        "course": (
            ("4:58:43-5:26:19", "Decide a daily lean top-down, from the 4-hour chart, "
             "then the hourly."),
            ("5:07:09", "An example: a fall, the lows taken before the open, then an "
             "hourly close back above a gap the fall left, so the lean is up."),
            ("6:27:20-6:28:23", "A lean can be overruled by what price does next."),
        ),
        "rule": (
            ("app", "The video shows the method on examples rather than as a rule. "
             "This is Orenth's version of it."),
            ("app", "1. If an old high or low was taken on the hourly since the trading "
             "day began, and the hourly's most recent break of structure (or close "
             "through a gap) went the other way, lean that way."),
            ("app", "2. Else, if the newest 4-hour gap in the 4-hour trend's direction "
             "was closed through, lean against that trend."),
            ("app", "3. Else, if the hourly trend agrees with the 4-hour trend, lean with "
             "it."),
            ("app", "4. Else, unclear."),
            ("app", "4-hour bars are built from hourly ones; the data source has none."),
            ("app", "Measured on about two years of hourly prices: read from the bars "
             "closed by 9:00 New York time (futures and coins) or 9:30 (stocks), and "
             "right when the 16:00 close is on the lean's side of the price then."),
        ),
        "limits": (),
    },
    "breakouts": {
        "what": "Buying the first close above the latest high in a rise, with the stop "
                "under the last low: the way most people are taught to trade a breakout.",
        "course": (
            ("1:18:20-1:23:38", "This is the trade most beginners take."),
            ("1:29:56", "Their stops under the last low become what the next move runs "
             "on."),
            ("6:30:28", "Bouncing off support and resistance is a coin flip."),
        ),
        "rule": (
            ("app", "In a rise, the first close above the latest confirmed swing high, a "
             "buy at that close. Mirror in a fall."),
            ("course", "The stop goes under the last swing low."),
            ("app", "Measured in the Setup lab only, not in the daily strategy test."),
        ),
        "limits": (),
    },
    "risk-and-sizing": {
        "what": "How much of your account to risk on one trade, and how big a position "
                "that means. The size comes from the risk, not the other way round: the "
                "distance to your stop sets how many shares or contracts fit.",
        "course": (
            ("0:33:48-0:34:53", "Judge a method by its hit rate and how much a win "
             "makes against a loss, not by single trades."),
            ("5:34:43", "Risk little enough per trade to survive a long losing run."),
            ("5:39:53-5:44:07", "The course trades a fixed number of contracts and "
             "keeps the risk to about 1 to 3% of the account."),
            ("5:40:57", "Never risk 5, 10 or 20% on one trade."),
        ),
        "rule": (
            ("app", "Orenth's plans size each position so the stop risks your risk "
             "setting (1% of the account unless you change it)."),
            ("app", "A trade that wins R times its risk needs to win more than 1 in "
             "(1 + R) of the time to break even before costs: 1 in 3 for a target twice "
             "the risk."),
        ),
        "limits": ("Losing runs come in clusters more than a coin would make them, so "
                   "plan for longer ones than a calculation suggests.",),
    },
    "journal": {
        "what": "A record of every trade: why you took it, how you felt, whether you "
                "followed your plan, and what you learned. Over many trades it shows "
                "which of your setups and habits make money and which do not.",
        "course": (
            ("0:27:33-0:28:35", "Review both your winning and your losing trades."),
            ("1:44:37", "Keep a notebook of what you see."),
            ("5:31:34", "Stick to your plan; the journal shows whether you did."),
            ("5:57:46", "Write down the lesson of each trade."),
        ),
        "rule": (
            ("app", "Orenth's journal is paper only: it never places an order."),
            ("app", "Results are counted after the same trading costs as everything "
             "else here, in R, the amount risked."),
        ),
        "limits": ("Under 30 closed trades, nothing in a journal separates skill from "
                   "luck.",),
    },
}


# --- small helpers ---------------------------------------------------------------------

def _finite(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _diff_words(v: Optional[int]) -> str:
    """A setup against its random entries, in words."""
    return {1: "beat random entries by more than its margin of error",
            -1: "did worse than random entries by more than its margin of error",
            0: "cannot be told apart from random entries"}.get(v, "too few to compare")


def interval_words(interval: str) -> str:
    return INTERVAL_WORDS.get(interval, interval)


def symbol_name(symbol: str) -> str:
    """An instrument's name, or both names of a pair ("ES=F/NQ=F")."""
    return " and ".join(SYMBOL_NAMES.get(part, part) for part in symbol.split("/"))


def _cell(raw: Dict) -> Optional[Dict]:
    """One stored rule cell (or a labstore.pooled() cell) as page figures.

    Every figure is a number or None; the templates print None as nothing
    rather than as a word.
    """
    if not raw:
        return None
    n = int(raw.get("n") or 0)
    if n <= 0:
        return None
    wins = int(raw.get("wins") or 0)
    days = raw.get("days")
    mean = _finite(raw.get("mean"))
    if mean is None:
        mean = (_finite(raw.get("sum_r")) or 0.0) / n
    se = _finite(raw.get("se"))
    gross = _finite(raw.get("sum_r_gross"))
    if "control_mean" in raw:
        control = _finite(raw.get("control_mean"))
        control_n = int(raw.get("control_n") or 0)
    else:
        c = raw.get("control") or {}
        control_n = int(c.get("n") or 0)
        control = (_finite(c.get("sum_r")) or 0.0) / control_n if control_n else None
    diff, diff_se = _finite(raw.get("diff")), _finite(raw.get("diff_se"))
    verdict = labstore.verdict(mean, se, n, days=days)
    # The comparison is judged on the trades that have random entries, and
    # their days: the others are no part of it.
    diff_days = raw["diff_days"] if "diff_days" in raw else labstore.paired_days(raw)
    diff_verdict = labstore.verdict(diff, diff_se, control_n, days=diff_days)
    return {"n": n, "wins": wins, "hit": wins / float(n), "mean": mean, "se": se,
            "margin": labstore.reach(days) * se if se is not None else None,
            "gross": gross / n if gross is not None else None,
            "days": int(days) if days is not None else None,
            "verdict": verdict, "words": labstore.words(verdict),
            "control": control, "control_n": control_n,
            "diff": diff,
            "diff_margin": labstore.reach(diff_days) * diff_se if diff_se is not None else None,
            "diff_verdict": diff_verdict, "diff_words": _diff_words(diff_verdict),
            "stable": raw.get("stable"),
            "symbols": raw.get("symbols")}


def _rate(hits: int, trials: int) -> Optional[Dict]:
    if not trials:
        return None
    band = labstore.wilson(hits, trials)
    return {"hits": hits, "trials": trials, "rate": hits / float(trials),
            "low": band[0], "high": band[1]}


# Counts a claim also carries, by an earlier time than its verdict's.
CLAIM_EXTRAS = (("hit_1h", "Within 1 hour"), ("hit_4h", "Within 4 hours"),
                ("hit_monday", "By Monday 16:00"))


def claim_view(cl: setups.Claim, rec: Optional[Dict]) -> Dict:
    """One claim and its record as figures for a page.

    measure "rate" (the default) is hits out of trials against the control's;
    "mean" is an average against the control's average, each with its margin,
    in R for the lab's trades (in_r) and else a share of that day's usual bar;
    "descriptive" is a rate with no control. Verdict words come from
    labstore.claim_words, except a description, which is not a test.
    """
    control = cl.control
    if control.startswith("None: "):
        # The catalog says "None: ..." for a claim with no control; on a page
        # that reads as a value that went missing.
        control = "Nothing: " + control[len("None: "):]
    view = {"id": cl.id, "statement": cl.statement, "video": cl.video, "how": cl.how,
            "control_words": control, "versus": cl.versus, "unit": cl.unit, "learn": cl.learn,
            "in_r": cl.unit == "trades", "extras": [],
            "title": setups.topic(cl.learn).title if cl.learn in setups.TOPICS_BY_SLUG
            else "", "measured": False, "no_cases": False, "must": cl.must}
    if not rec:
        view.update(words="being tested", verdict=None)
        return view
    pooled = rec.get("pooled") or {}
    measure = rec.get("measure") or "rate"
    trials, hits = int(pooled.get("trials") or 0), int(pooled.get("hits") or 0)
    base_trials, base_hits = int(pooled.get("base_trials") or 0), int(pooled.get("base_hits") or 0)
    verdict = rec.get("verdict")
    view.update(measured=trials > 0, no_cases=trials == 0, measure=measure,
                interval=rec.get("interval") or "", verdict=verdict, notes=[n for n in rec.get("notes") or [] if n],
                trials=trials, skipped=rec.get("skipped") or {})
    if measure == "mean":
        mean, se = _finite(pooled.get("mean")), _finite(pooled.get("se"))
        base, base_se = _finite(pooled.get("base_mean")), _finite(pooled.get("base_se"))
        view.update(mean=mean, margin=_margin(se, pooled.get("days")),
                    base=base, base_margin=_margin(base_se, pooled.get("base_days")))
        if mean is None:
            view["measured"] = False
    else:
        view.update(rate=_rate(hits, trials), base=_rate(base_hits, base_trials))
        view["extras"] = [{"label": label, "rate": _rate(int(pooled[key]), trials),
                           "base": _rate(int(pooled["base_" + key]), base_trials)}
                          for key, label in CLAIM_EXTRAS
                          if key in pooled and "base_" + key in pooled]
    if measure == "descriptive":
        view["words"] = ("holds" if verdict == 1 else "does not hold" if verdict == -1
                         else "cannot be told either way" if verdict == 0
                         else "describes what happened")
    else:
        view["words"] = labstore.claim_words(verdict)
    by_symbol = []
    for sym, cell in sorted((rec.get("by_symbol") or {}).items(),
                            key=lambda kv: _order(kv[0])):
        t = int(cell.get("trials") or 0)
        if not t:
            continue
        row = {"symbol": sym, "name": symbol_name(sym), "trials": t}
        if measure == "mean":
            row.update(mean=_finite(cell.get("mean")), base=_finite(cell.get("base_mean")))
        else:
            row.update(rate=_rate(int(cell.get("hits") or 0), t),
                       base=_rate(int(cell.get("base_hits") or 0),
                                  int(cell.get("base_trials") or 0)))
        by_symbol.append(row)
    view["by_symbol"] = by_symbol
    return view


def _margin(se: Optional[float], days: Optional[int]) -> Optional[float]:
    """A margin of error from a standard error and the days behind it."""
    return labstore.reach(days) * se if se is not None else None


def _order(symbol: str) -> Tuple[int, str]:
    for k, (sym, _, _) in enumerate(setups.LAB_SYMBOLS):
        if sym == symbol.split("/")[0]:
            return k, symbol
    return 99, symbol


# --- reading the store -------------------------------------------------------------------

def _load(conn: sqlite3.Connection) -> Dict:
    """Everything the lab stored, read once per page."""
    return {"results": labstore.results(conn), "claims": labstore.claims(conn)}


# app choice: a figure this old on a page that says it is measured every night
# has missed several nights, and the page says so.
STALE_AFTER = 3 * 86400


def freshness(store: Dict, now: Optional[float] = None) -> Optional[Dict]:
    """When the figures on the page were measured: the newest and the oldest of
    the stored records themselves. A run that could get no prices measures
    nothing and changes neither."""
    now = now if now is not None else time.time()
    times = [int(r["updated"]) for r in list(store["results"]) + list(store["claims"].values())
             if r.get("updated")]
    if not times:
        return None
    return {"newest": max(times), "oldest": min(times), "stale": now - min(times) > STALE_AFTER}


def _has_setup(results: Sequence[Dict], name: str) -> bool:
    for res in results:
        rules = ((res.get("setups") or {}).get(name) or {}).get("rules") or {}
        if any(int((rules.get(r) or {}).get("n") or 0) > 0 for r in labstore.RULES):
            return True
    return False


# Why a setup that appeared was not traded, as the lab counts it.
LEFT_OUT = (("tight_stop", "with a stop too close to trade"),
            ("left_out_day", "on a day with too few bars or a futures contract change"),
            ("at_close", "on the last bar before 16:00, with no time left"),
            ("still_open", "still open when the prices end"),
            ("warmup", "too early in the prices to know a usual bar's range"))


def setup_lab(results: Sequence[Dict], name: str) -> Dict:
    """One setup across the lab: pooled per bar size and target rule, and per
    instrument, with what was skipped."""
    pooled, rows = [], []
    no_control = 0
    out = {key: 0 for key, _ in LEFT_OUT}
    k3 = []
    for interval in setups.LAB_INTERVALS:
        those = [r for r in results if r.get("interval") == interval]
        for rule in labstore.RULES:
            cell = _cell(labstore.pooled(those, name, rule) or {})
            if cell:
                pooled.append(dict(cell, interval=interval, rule=rule,
                                   interval_words=interval_words(interval),
                                   rule_words=RULE_WORDS[rule],
                                   instruments=len(cell["symbols"] or [])))
    for res in results:
        entry = (res.get("setups") or {}).get(name)
        if not entry:
            continue
        skipped = entry.get("skipped") or {}
        for key in out:
            out[key] += int(skipped.get(key) or 0)
        no_control += int(skipped.get("no_control") or 0)
        for rule in labstore.RULES:
            cell = _cell((entry.get("rules") or {}).get(rule) or {})
            if cell:
                rows.append(dict(cell, symbol=res["symbol"], name=symbol_name(res["symbol"]),
                                 interval=res.get("interval"), rule=rule,
                                 interval_words=interval_words(res.get("interval"))))
        # The wider swings with the one target, twice the risk: both would be
        # two figures for each instrument that nothing on the page tells apart.
        r = (entry.get("k3") or {}).get("2r") or {}
        n = int(r.get("n") or 0)
        if n:
            mean = (_finite(r.get("sum_r")) or 0.0) / n
            se = _finite(r.get("se"))
            k3.append({"symbol": res["symbol"], "name": symbol_name(res["symbol"]),
                       "interval_words": interval_words(res.get("interval")),
                       "n": n, "mean": mean,
                       "margin": _margin(se, r.get("days"))})
    return {"pooled": pooled, "rows": rows, "no_control": no_control,
            "left_out": ["{:,} {}".format(out[key], said) for key, said in LEFT_OUT if out[key]],
            "k3": k3, "measured": bool(pooled)}


def _example(results: Sequence[Dict], names: Sequence[str]) -> Optional[Dict]:
    """The first stored example trade of these setups, drawn as a chart."""
    for name in names:
        for interval in setups.LAB_INTERVALS:
            for res in results:
                if res.get("interval") != interval:
                    continue
                ex = ((res.get("setups") or {}).get(name) or {}).get("example")
                drawn = example_view(ex, res.get("symbol", ""), interval, name)
                if drawn:
                    return drawn
    return None


def example_view(ex: Optional[Dict], symbol: str, interval: str, name: str) -> Optional[Dict]:
    """A stored example trade as a chart and the sentences that go with it."""
    if not ex:
        return None
    try:
        t = [int(x) for x in ex["t"]]
        o, h, l, c = ([float(x) for x in ex[key]] for key in ("o", "h", "l", "c"))
        entry_i, exit_i = int(ex["entry_i"]), int(ex["exit_i"])
        entry, stop, target = float(ex["entry"]), float(ex["stop"]), float(ex["target"])
        r = float(ex["r"])
        direction = int(ex["direction"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= entry_i < len(t) and 0 <= exit_i < len(t)):
        return None
    levels = {"Entry": entry, "Stop": stop, "Target": target}
    wide = charts.trade_chart(t, o, h, l, c, (), levels, day_label=clock.label_ny, width=720,
                              height=300, bars=len(t), interval=interval)
    if not wide:
        return None
    # A phone gets the trade itself, drawn for its width: shrunk to fit, the
    # wide drawing's labels are a few pixels tall.
    a, b = max(0, entry_i - 15), exit_i + 7
    narrow = charts.trade_chart(t[a:b], o[a:b], h[a:b], l[a:b], c[a:b], (), levels,
                                day_label=clock.label_ny, width=340, height=260,
                                bars=b - a, interval=interval)
    return {"wide": wide, "narrow": narrow or wide, "symbol": symbol, "name": symbol_name(symbol),
            "setup": setups.setup(name).label if name in setups.BY_NAME else name,
            "interval_words": interval_words(interval),
            "side": "buy" if direction == 1 else "short",
            "entry": format_price(entry), "stop": format_price(stop),
            "target": format_price(target), "r": r,
            "entry_at": clock.label_ny(t[entry_i]), "exit_at": clock.label_ny(t[exit_i])}


def _costs(results: Sequence[Dict]) -> List[Tuple[str, List[str]]]:
    """What each instrument was charged a round trip, as a percent, grouped."""
    by_cost: Dict[float, List[str]] = {}
    seen = set()
    for res in results:
        sym = res.get("symbol")
        cost = _finite(res.get("cost_bps"))
        if sym in seen or cost is None:
            continue
        seen.add(sym)
        by_cost.setdefault(cost, []).append(symbol_name(sym))
    return [("%.2f%%" % (bps / 100.0), names) for bps, names in sorted(by_cost.items())]


def _skipped_days(results: Sequence[Dict]) -> Dict[str, int]:
    """Days left out, added over the instruments. Each instrument's once: its
    two bar sizes cover the same days, so the larger of the two counts."""
    out = {"incomplete": 0, "roll": 0}
    for key in out:
        most: Dict[str, int] = {}
        for res in results:
            count = int((res.get("skipped_days") or {}).get(key) or 0)
            most[res.get("symbol", "")] = max(count, most.get(res.get("symbol", ""), 0))
        out[key] = sum(most.values())
    return out


def cells_tested(results: Sequence[Dict]) -> int:
    """How many setup x instrument x bar size x target cells got a verdict:
    the number of chances for one to look good by luck."""
    count = 0
    for res in results:
        for entry in (res.get("setups") or {}).values():
            for rule in labstore.RULES:
                cell = _cell(((entry or {}).get("rules") or {}).get(rule) or {})
                if cell and cell["verdict"] is not None:
                    count += 1
    return count


def daily_test(rows: List[Dict], name: str, registered: bool) -> Optional[Dict]:
    """The setup's record in the daily strategy test, when it is a strategy."""
    if not registered:
        return None
    summary = public.strategy_summary(public.strategy_results(rows, name))
    out = {"name": name, "slug": public.strategy_slug(name), "tested": summary["tested"]}
    if summary["tested"]:
        out.update(n=summary["n"], kept=summary["kept"], se=summary["se"],
                   margin=2 * summary["se"] if summary["se"] is not None else None,
                   verdict=summary["verdict"], words=labstore.words(summary["verdict"]),
                   paid=summary["paid"])
    return out


def bias_view(conn: sqlite3.Connection) -> Dict:
    """The bias study's record, pooled and per instrument."""
    rows = []
    pooled = None
    for sym in [labstore.POOL_ALL] + [s for s, _, _ in setups.LAB_SYMBOLS]:
        rec = labstore.bias(conn, sym)
        if not rec or not rec.get("days"):
            continue
        called, right = int(rec.get("called") or 0), int(rec.get("right") or 0)
        days, up = int(rec["days"]), int(rec.get("up_days") or 0)
        view = {"symbol": sym, "name": symbol_name(sym), "days": days, "called": called,
                "right": right, "unclear": int(rec.get("unclear") or 0),
                "rate": _rate(right, called), "up": _rate(up, days),
                "target": _rate(int(rec.get("target_reached") or 0), called)}
        if sym == labstore.POOL_ALL:
            pooled = view
        else:
            rows.append(view)
    return {"pooled": pooled, "rows": rows, "measured": bool(pooled or rows)}


# --- the pages ---------------------------------------------------------------------------

def _topic_measured(store: Dict, t: setups.Topic, rows: List[Dict], registered,
                    bias_measured: bool) -> bool:
    """Whether a topic's page has anything measured to show (else noindex).

    A topic with nothing of its own to measure (the journal) waits for the
    lab's first run, like the rest, rather than standing open with nothing.
    """
    if not t.setups and not t.claims:
        return bool(store["results"] or store["claims"] or bias_measured)
    if any(_has_setup(store["results"], n) for n in t.setups):
        return True
    if any(claim_view(setups.claim(cid), store["claims"].get(cid))["measured"]
           for cid in t.claims):
        return True
    if t.slug == "daily-bias" and bias_measured:
        return True
    return any((daily_test(rows, n, n in registered) or {}).get("tested")
               for n in t.setups)


def hub(conn: sqlite3.Connection, registered: Sequence[str] = ()) -> Dict:
    """The /learn page: the short answer, the claims, the topics, the method.

    registered names the setups that are strategies in this build (the
    daily test reads only those).
    """
    store = _load(conn)
    results = store["results"]
    verdict_rows = public.verdicts(conn)
    bias = bias_view(conn)

    # The short answer: every setup, pooled over the instruments, on 5-minute
    # prices with a target twice the risk (the plainest of the four cells).
    # 15-minute only when the lab has no 5-minute results at all, so every
    # row of the short answer is the same cell and the sentence is true of each.
    labs = [(s, setup_lab(results, s.name)) for s in setups.SETUPS]
    main_interval = "5m"
    if not any(p["interval"] == "5m" for _, lab in labs for p in lab["pooled"]):
        main_interval = "15m"
    tally = {1: 0, -1: 0, 0: 0, None: 0}
    setup_rows = []
    for s, lab in labs:
        main = next((p for p in lab["pooled"]
                     if p["interval"] == main_interval and p["rule"] == "2r"), None)
        if main:
            tally[main["verdict"]] += 1
        setup_rows.append({"setup": s, "main": main, "slug": s.learn,
                           "measured": lab["measured"]})
    measured_setups = sum(1 for r in setup_rows if r["main"])

    claims = [claim_view(c, store["claims"].get(c.id)) for c in setups.CLAIMS]
    claims = [c for c in claims if c["must"] or c["measured"]]
    claim_tally = {1: 0, -1: 0, 0: 0, None: 0}
    for c in claims:
        if c["measured"] and c.get("measure") != "descriptive":
            claim_tally[c["verdict"] if c["verdict"] in (1, -1, 0) else None] += 1
    measured_claims = sum(1 for c in claims if c["measured"])

    topics = []
    for t in setups.TOPICS:
        topics.append({"slug": t.slug, "title": t.title,
                       "what": CONCEPTS[t.slug]["what"],
                       # A topic with nothing of its own to measure gets no chip.
                       "own": bool(t.setups or t.claims),
                       "measured": _topic_measured(store, t, verdict_rows, registered,
                                                   bias["measured"])})
    measured = bool(any(r["measured"] for r in setup_rows) or measured_claims
                    or bias["measured"])
    return {"measured": measured, "setups": setup_rows, "tally": tally,
            "main_interval_words": interval_words(main_interval),
            "measured_setups": measured_setups, "claims": claims,
            "claim_tally": claim_tally, "measured_claims": measured_claims,
            "topics": topics, "cells": cells_tested(results),
            "costs": _costs(results), "skipped_days": _skipped_days(results),
            "fresh": freshness(store), "universe": UNIVERSE, "method": METHOD,
            "limits": LIMITS, "source": SOURCE, "video": setups.VIDEO}


def topic(conn: sqlite3.Connection, slug: str,
          registered: Sequence[str] = ()) -> Optional[Dict]:
    """One /learn/<slug> page, or None for a slug that is not a topic."""
    t = setups.TOPICS_BY_SLUG.get(slug)
    if t is None:
        return None
    store = _load(conn)
    results = store["results"]
    verdict_rows = public.verdicts(conn)
    copy = CONCEPTS[slug]
    setup_views = []
    for name in t.setups:
        s = setups.setup(name)
        setup_views.append({"setup": s, "stop": setups.STOP_RULES.get(s.stop_rule, ""),
                            "lab": setup_lab(results, name),
                            "daily": daily_test(verdict_rows, name, name in registered)})
    claims = [claim_view(setups.claim(cid), store["claims"].get(cid)) for cid in t.claims]
    claims = [c for c in claims if c["must"] or c["measured"]]
    bias = bias_view(conn) if slug == "daily-bias" else None
    measured = _topic_measured(store, t, verdict_rows, registered,
                               bool(bias and bias["measured"]))
    has_lab = any(v["lab"]["measured"] for v in setup_views)
    others = [x for x in setups.TOPICS if x.slug != slug]
    # "concept", not "copy": in a template page.copy is the dict's own method.
    return {"slug": slug, "title": t.title, "concept": copy, "setups": setup_views,
            "claims": claims, "bias": bias, "measured": measured, "has_lab": has_lab,
            "no_cases": bool(claims) and not t.setups and all(c["no_cases"] for c in claims),
            "example": _example(results, t.setups), "cells": cells_tested(results),
            "costs": _costs(results), "skipped_days": _skipped_days(results),
            "fresh": freshness(store), "method": METHOD, "limits": LIMITS, "source": SOURCE,
            "video": setups.VIDEO, "others": others, "universe": UNIVERSE}


def measured_slugs(conn: sqlite3.Connection, registered: Sequence[str] = ()) -> List[str]:
    """The topics with something measured, for the sitemap."""
    page = hub(conn, registered)
    return [t["slug"] for t in page["topics"] if t["measured"]] if page["measured"] else []


def lab_box(conn: sqlite3.Connection, strategy: str) -> Optional[Dict]:
    """The Setup lab's result for one strategy, for its /backtest page, or
    None when it is not a setup or nothing was measured yet."""
    s = setups.BY_NAME.get(strategy)
    if s is None:
        return None
    lab = setup_lab(labstore.results(conn), strategy)
    if not lab["measured"]:
        return None
    return {"setup": s, "topic": s.learn,
            "title": setups.topic(s.learn).title if s.learn in setups.TOPICS_BY_SLUG else "",
            "stop": setups.STOP_RULES.get(s.stop_rule, ""), "pooled": lab["pooled"]}


# --- practice: call the next move ----------------------------------------------------------

PRACTICE_AHEAD = 12      # app choice: the move is judged 12 bars after the cut
PRACTICE_PICKS = (("up", "Up"), ("down", "Down"), ("unclear", "Unclear"))


def practice_items(conn: sqlite3.Connection) -> List[Dict]:
    """Every stored example chart long enough to cut and reveal, in a fixed order."""
    out = []
    for res in labstore.results(conn):
        for name in sorted(res.get("setups") or {}):
            ex = ((res["setups"][name] or {}).get("example")) or None
            if not ex:
                continue
            try:
                t = [int(x) for x in ex["t"]]
                o, h, l, c = ([float(x) for x in ex[k]] for k in ("o", "h", "l", "c"))
                cut = int(ex["entry_i"])
            except (KeyError, TypeError, ValueError):
                continue
            if cut < 10 or cut + PRACTICE_AHEAD >= len(t):
                continue
            # The move must be one the chart shows. Across a night, a weekend or
            # the futures' pause, the jump between two bars would decide it.
            interval = res.get("interval", "")
            step = clock.interval_seconds(interval)
            ahead = t[cut:cut + PRACTICE_AHEAD + 1]
            if any(b - a > step for a, b in zip(ahead, ahead[1:])):
                continue
            # What tells this chart from the one the next run may put in its
            # place. Short, because the browser's cookie keeps those answered,
            # and it says nothing of the instrument before the answer.
            key = hashlib.sha256(("%s|%s|%s|%d" % (res["symbol"], interval, name, t[cut]))
                                 .encode("utf-8")).hexdigest()[:10]
            out.append({"symbol": res["symbol"], "interval": interval, "key": key,
                        "setup": name, "t": t, "o": o, "h": h, "l": l, "c": c, "cut": cut})
    return out


def practice_answer(item: Dict) -> str:
    """What the price did next: "up" when the close PRACTICE_AHEAD bars after
    the cut is higher by more than half a usual bar's range (the average
    high-to-low of the bars shown before the cut), "down" mirror, else
    "unclear"."""
    cut = item["cut"]
    ranges = [item["h"][k] - item["l"][k] for k in range(cut + 1)]
    usual = sum(ranges) / len(ranges) if ranges else 0.0
    move = item["c"][cut + PRACTICE_AHEAD] - item["c"][cut]
    if move > usual / 2:
        return "up"
    if move < -usual / 2:
        return "down"
    return "unclear"


PRACTICE_NARROW = 40     # app choice: the bars before the cut a phone's chart has room for


def practice_chart(item: Dict, reveal: bool) -> Dict[str, str]:
    """The chart up to the cut, or past it once answered: drawn wide, and
    narrow for a phone, where the wide drawing's labels would be a few pixels
    tall. The line at the cut is labelled in one word, to fit beside either."""
    end = item["cut"] + 1 + (PRACTICE_AHEAD if reveal else 0)
    series = [item[key][:end] for key in ("t", "o", "h", "l", "c")]
    drawn = dict(levels={"Cut": item["c"][item["cut"]]}, day_label=clock.label_ny,
                 interval=item["interval"])
    wide = charts.trade_chart(*series, width=720, height=300, bars=end, **drawn)
    narrow = charts.trade_chart(*series, width=340, height=260,
                                bars=PRACTICE_NARROW + (PRACTICE_AHEAD if reveal else 0),
                                **drawn)
    return {"wide": wide, "narrow": narrow or wide}
