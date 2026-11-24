"""Every price-action setup, claim and Learn topic from TJR's video, in one place.

The strategy library, the Setup lab, the alerts, the journal and the Learn
pages all name these setups and claims. Their names, plain descriptions, what
the course teaches (in Orenth's own words, never the video's), the video
timestamps and the journal's lists are typed here once, and read from here
everywhere else.

`plain` and `statement` are for anyone: everyday words, no trading jargon,
and the concept's own name only in its label. `course_says` reports the
course; it never says the setup works. Whether it does is what the Setup lab
and the daily strategy test measure.
"""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from typing import Dict, List, Tuple

VIDEO = {"title": "How To Start Day Trading In 2026", "creator": "TJR",
         "url": "https://youtu.be/yiuFUp0kFz8", "length": "6:40:00"}


@dataclass(frozen=True)
class Setup:
    name: str                 # the strategy's name in strategies.REGISTRY
    label: str                # at most 40 characters
    plain: str                # what it is, in everyday words
    course_says: str          # what the course teaches about it, in our words
    video: Tuple[str, ...]    # timestamps, "h:mm:ss" or "h:mm:ss-h:mm:ss"
    learn: str                # the Learn topic's slug
    registry: bool            # also a strategy in strategies.REGISTRY, measured daily
    intraday_only: bool       # needs intraday bars (sessions)
    stop_rule: str            # a key of STOP_RULES: where the Setup lab puts its stop


# Where the Setup lab puts each setup's stop, as the course teaches it
# (6:24:12-6:30:28: beyond the high or low that was taken).
STOP_RULES = {
    "sweep_extreme": "beyond the high or low that was taken",
    "leg_extreme": "beyond the far end of the move that the break ended",
    "gap_far_edge": "beyond the far edge of the gap, or the bar's own low or high if that is further",
    "reaction_bar": "beyond the bar that turned at the line",
    "last_swing_low": "under the last swing low (over the last swing high for a short)",
}

SETUPS: Tuple[Setup, ...] = (
    Setup("sweep_reclaim", "Liquidity sweep and reclaim",
          "Price trades past an old high or low that held before, then closes back "
          "on the other side of it in the same bar.",
          "The course says stop orders gather just past old highs and lows, and that "
          "price often runs those orders before it turns. It teaches waiting for that "
          "run and the turn, not trading the old level itself.",
          ("1:36:14-1:44:37",), "liquidity-sweep", True, False, "sweep_extreme"),
    Setup("prior_day_sweep", "Yesterday's high or low swept",
          "Price trades past yesterday's high or low, then closes back inside "
          "yesterday's range in the same bar.",
          "The course marks yesterday's high and low before the open, as places where "
          "orders wait, and reads a move past one that closes back inside as those "
          "orders being taken.",
          ("2:05:49-2:08:58",), "previous-day-levels", True, False, "sweep_extreme"),
    Setup("equal_levels_sweep", "Equal highs or lows swept",
          "Two or more old highs (or lows) sat at almost the same price. Price trades "
          "past them, then closes back on the other side in the same bar.",
          "The course says highs or lows at the same price are an obvious place for "
          "people to put their stops, so price is drawn to them and often turns once "
          "it has been there.",
          ("2:20:42-2:23:53",), "equal-highs-lows", True, False, "sweep_extreme"),
    Setup("session_sweep", "Session high or low swept",
          "Between 08:30 and 12:00 New York time, price trades past the high or low "
          "that London made earlier that morning (Asia's when London's prices are "
          "missing), then closes back inside it.",
          "The course splits the day into the Asia, London and New York sessions and "
          "watches New York take out a high or low that an earlier session left.",
          ("1:50:03-2:04:44", "4:31:11"), "sessions", True, True, "sweep_extreme"),
    Setup("structure_flip", "Break of structure",
          "In a rise, price closes below the last low the rise left; in a fall, above "
          "the last high. A wick past it does not count, only a close.",
          "The course defines a trend by its swing highs and lows, and says the trend "
          "has changed only when a candle closes past the last swing the other way.",
          ("2:34:26-2:43:56",), "break-of-structure", True, False, "leg_extreme"),
    Setup("sweep_then_flip", "Sweep, then break of structure",
          "Price first runs past an old high or low, and soon after closes past its "
          "last swing in the other direction.",
          "The course says the turn it looks for comes in two parts: an old high or "
          "low is taken, then price breaks its last swing the opposite way.",
          ("2:42:53-2:43:56",), "break-of-structure", True, False, "sweep_extreme"),
    Setup("fvg_retest", "Fair value gap retest",
          "A fast move leaves a stretch of prices that the bars on either side of one "
          "big candle never shared. Later price comes back into that stretch, and the "
          "bar closes in the direction of the trend.",
          "The course calls these stretches gaps that price tends to come back to, and "
          "teaches entering when price returns to one in the direction of the trend.",
          ("2:51:15-2:58:34",), "fair-value-gap", True, False, "gap_far_edge"),
    Setup("ifvg_flip", "Inverse fair value gap",
          "In a rise, price closes below the bottom of a stretch of prices the rise "
          "jumped over quickly; in a fall, above the top of one.",
          "The course says a gap that price closes through changes its role, and reads "
          "that close as an early sign that the move has turned.",
          ("3:42:47-3:58:38",), "inverse-fair-value-gap", True, False, "leg_extreme"),
    Setup("premium_discount", "The 50% line",
          "Take the last move from its low to its high. Halfway between them is the "
          "line. In a rise, price dips to the line or below it and the bar closes up.",
          "The course calls the lower half of a rise the cheap half and the upper half "
          "the dear half, and teaches buying only in the cheap half of a rise (and "
          "selling only in the dear half of a fall).",
          ("3:58:38-4:13:16",), "premium-discount", True, False, "reaction_bar"),
    Setup("liquidity_model", "Sweep, break and retest",
          "Four steps in order: an old high or low is taken, price breaks its last "
          "swing the other way, comes back a little into a stretch the new move jumped "
          "over or to the halfway line of the new move, and a bar turns back in the "
          "new direction.",
          "This is the course's main model: orders resting past an old high or low are "
          "taken, the trend shifts, price pulls back, and the trade is taken on the "
          "pullback with the stop past the high or low that was taken.",
          ("4:08:04", "6:24:12-6:30:28"), "sweep-break-retest", True, False,
          "sweep_extreme"),
    Setup("crowd_breakout", "Buying the breakout",
          "In a rise, buying the first close above the latest swing high, with the stop "
          "under the last swing low: the way most people are taught to trade a "
          "breakout.",
          "The course says this is the trade most beginners take, and that their stops "
          "under the last low become what the next move runs on.",
          ("1:18:20-1:23:38", "1:29:56"), "breakouts", False, False, "last_swing_low"),
)

BY_NAME: Dict[str, Setup] = {s.name: s for s in SETUPS}

# Each strategy's name as it reads in a question ("Does a sweep of yesterday's
# high or low work?"), where the label alone does not make an English sentence.
ASKED: Dict[str, str] = {
    "sweep_reclaim": "a liquidity sweep and reclaim",
    "prior_day_sweep": "a sweep of yesterday's high or low",
    "equal_levels_sweep": "a sweep of equal highs or lows",
    "session_sweep": "a sweep of a session's high or low",
    "structure_flip": "a break of structure",
    "sweep_then_flip": "a sweep then a break of structure",
    "fvg_retest": "a fair value gap retest",
    "ifvg_flip": "an inverse fair value gap",
    "premium_discount": "the 50% line",
    "liquidity_model": "the sweep, break and retest model",
}

REGISTRY_NAMES = tuple(s.name for s in SETUPS if s.registry)


@dataclass(frozen=True)
class Claim:
    id: str
    statement: str            # the claim, in everyday words
    video: Tuple[str, ...]    # timestamps
    unit: str                 # what is counted: "days", "trades", "gaps"...
    how: str                  # what is measured and when it counts
    control: str              # what it is compared with
    learn: str                # the Learn topic's slug
    must: bool                # measured from the first release; False: later
    versus: str = ""          # what it is compared with, in a few words beside the figure


CLAIMS: Tuple[Claim, ...] = (
    Claim("london_takes_asia",
          "London trades past one side of the Asia session's range, then reaches the "
          "other side before noon New York time.",
          ("1:52:08", "1:57:26", "2:02:39"), "days",
          "Futures and coins, 5-minute bars: the first London bar beyond the Asia range "
          "(days where one bar goes past both sides are left out). It counts when the "
          "other side trades by 12:00 New York time.",
          "The same range mirrored around the price at 03:00: its high as far above the "
          "price as Asia's low was below it, and its low as far below as Asia's high was "
          "above, measured the same way.", "sessions", True, "the range mirrored"),
    Claim("ny_takes_london",
          "New York trades past one side of London's range, then reaches the other side "
          "the same day.",
          ("2:03:41", "2:18:35", "4:31:11"), "days",
          "Futures and coins, 5-minute bars: the first bar from 08:30 beyond London's "
          "range. It counts when the other side trades by 16:00.",
          "The same range mirrored around the price at 08:30: its high as far above the "
          "price as London's low was below it, and its low as far below as London's high "
          "was above.", "sessions", True, "the range mirrored"),
    Claim("pd_sweep_reverse",
          "After yesterday's high is taken, price reaches yesterday's low the same day, "
          "and the other way round.",
          ("2:06:52-2:07:57",), "days",
          "Every instrument, 5-minute bars: the first time today trades past yesterday's "
          "high or low. It counts when the other one trades by 16:00.",
          "The same range mirrored around today's opening price: its high as far above "
          "the open as yesterday's low was below it, and its low as far below as "
          "yesterday's high was above.", "previous-day-levels", True, "the range mirrored"),
    Claim("open_manipulation",
          "Between 9:30 and 9:50 price trades past a level from before the open, then "
          "closes back inside within half an hour.",
          ("4:33:23", "4:35:34"), "days",
          "Futures and coins, 5-minute bars: a bar that starts from 9:30 to 9:45, so is "
          "over by 9:50, trades past the Asia, London, before-the-open or yesterday's high "
          "or low, and a close back inside follows within 30 minutes.",
          "The same test with every time an hour later: bars that start from 10:30 to "
          "10:45, past yesterday's high or low or the ranges from 19:00 to 4:00, 4:00 to "
          "9:30 and 9:30 to 10:30.", "sessions", True, "an hour later"),
    Claim("macro_window",
          "Trades entered between 9:50 and 10:10 do better than trades entered at other "
          "times.",
          ("4:33:23", "4:37:38"), "trades",
          "The Setup lab's sweep, break and retest trades, grouped by the time they were "
          "entered.",
          "The same trades entered at other times.", "sessions", True, "other times"),
    Claim("after_1030_slower",
          "The market moves less after 10:30 than in the first hour.",
          ("4:33:23",), "days",
          "Every instrument, 5-minute bars: the average bar's size from 10:30 to 12:00 "
          "against 9:30 to 10:30, each over that day's own average bar, each day "
          "counted once.",
          "The 9:30 to 10:30 window.", "sessions", True, "9:30 to 10:30"),
    Claim("fvg_gets_retested",
          "Price comes back into a fresh stretch of prices that a fast move jumped over.",
          ("2:51:15",), "gaps",
          "Every instrument, 5- and 15-minute bars: a new gap counts when price trades "
          "back into it within 20 bars, before closing through it.",
          "A zone of the same size, the same distance from the price, at a random bar "
          "(the same random bars on every run).", "fair-value-gap", True, "random zones"),
    Claim("close_beats_wick",
          "A close past a swing point carries on; a wick past it that closes back "
          "does not.",
          ("2:39:42", "2:41:50", "2:43:56"), "breaches",
          "Every instrument, 5-minute bars: the first time price goes past an old swing "
          "point. It counts when price goes one usual bar's range further before it "
          "comes one usual bar's range back, within 20 bars.",
          "The times only a wick went past.", "break-of-structure", True, "only a wick past"),
    Claim("bias_works",
          "The lean read before the open calls the day's direction more often than "
          "chance.",
          ("4:59:46-5:24:14",), "days",
          "Every instrument, hourly bars over about two years: the lean read from the "
          "bars closed by 9:00 (futures and coins) or 9:30 (stocks), against the 16:00 "
          "close.",
          "Always saying up, and a coin flip.", "daily-bias", True, "always saying up"),
    Claim("crowd_breakout_loses",
          "Buying the break of a high, with the stop under the last low, loses money.",
          ("1:19:23-1:29:56",), "trades",
          "The Setup lab's breakout buys, with a target twice the risk.",
          "Zero, and each buy's own random entries: buys at the same time of day on other "
          "days, with the same costs.",
          "breakouts", True, "random entries"),
    Claim("sweep_alone_loses",
          "Trading against every run past an old high or low loses; the full four-step "
          "model does better.",
          ("1:40:27-1:43:34",), "trades",
          "The Setup lab's sweep-and-reclaim trades, and the four-step model's trades, "
          "with a target twice the risk.",
          "Zero, and each trade's own random entries at the same time of day on other "
          "days, with the same costs; and the four-step model, against its own.",
          "liquidity-sweep", True, "random entries"),
    Claim("not_every_day_green",
          "Even a method that works has losing days.",
          ("0:32:45",), "days",
          "The share of trading days on which the Setup lab's four-step model made "
          "money, and the longest run of losing days in a row.",
          "None: this describes what happened, it is not a test.",
          "risk-and-sizing", True),
    Claim("ndog_fills",
          "The jump between the 17:00 close and the 18:00 reopen gets filled.",
          ("3:16:24-3:23:47",), "days",
          "Futures, 5-minute bars: each day's opening jump (roll days left out), and "
          "whether price traded back to the close before it within 1 hour, 4 hours, "
          "and by 9:30.",
          "The level the same distance the other way, past the open, by the same time.",
          "opening-gaps", True, "the same distance the other way"),
    Claim("nwog_fills",
          "The jump between Friday's close and Sunday's reopen gets filled, often by "
          "Monday.",
          ("3:19:32", "3:25:57"), "weeks",
          "Futures, hourly bars: Friday's close against the Sunday 18:00 open, filled by "
          "Monday 16:00, and by Friday 16:00.",
          "The level the same distance the other way, past the open, by the same time.",
          "opening-gaps", True, "the same distance the other way"),
    Claim("smt_at_sweep",
          "When two related markets disagree at a high (one makes a higher high, the "
          "other does not), both fall next, and the other way round at lows.",
          ("4:13:16-4:28:04",), "highs and lows",
          "S&P and Nasdaq futures, their two funds, and Bitcoin with Ether, 5-minute "
          "bars: swing highs paired within 2 bars, one higher and one lower, the higher "
          "one past an old high. It counts when the leader falls one usual bar's range "
          "before it rises one, within 36 bars.",
          "Old highs taken where both made higher highs.", "smt-divergence", True,
          "when both agreed"),
    Claim("equal_highs_magnet",
          "Price is drawn to equal highs and lows more than to single ones.",
          ("2:21:45",), "levels",
          "Equal highs and lows against single swing points at the same distance from "
          "the price: how often each is reached within 20 bars.",
          "Single swing points at the same distance.", "equal-highs-lows", False),
    Claim("ifvg_before_bos",
          "A close through a gap comes before the trend's turn and gives an earlier, "
          "better price.",
          ("3:50:08",), "turns",
          "The share of trend changes that a close through a gap came before, how many "
          "bars earlier, and how much better the price was.",
          "None: this describes what happened.", "inverse-fair-value-gap", False),
    Claim("sr_coin_flip",
          "Bouncing off support and resistance is a coin flip.",
          ("6:30:28",), "trades",
          "Bounces off the app's support and resistance zones, with the four-step "
          "model's stop and target rules.",
          "Zero.", "breakouts", False),
    Claim("fvg_holds_after_retest",
          "Once price has come back into a stretch a fast move jumped over, it holds "
          "and the move carries on.",
          ("2:51:15-2:58:34",), "gaps",
          "Gaps price came back into: how often price then moved away in the gap's "
          "direction before closing through it.",
          "Zones of the same size at random bars.", "fair-value-gap", False),
)

CLAIMS_BY_ID: Dict[str, Claim] = {c.id: c for c in CLAIMS}

Topic = namedtuple("Topic", "slug title setups claims")

TOPICS: Tuple[Topic, ...] = (
    Topic("liquidity-sweep", "Liquidity sweeps", ("sweep_reclaim",),
          ("sweep_alone_loses",)),
    Topic("previous-day-levels", "Yesterday's high and low", ("prior_day_sweep",),
          ("pd_sweep_reverse",)),
    Topic("sessions", "Asia, London and New York sessions", ("session_sweep",),
          ("london_takes_asia", "ny_takes_london", "open_manipulation", "macro_window",
           "after_1030_slower")),
    Topic("equal-highs-lows", "Equal highs and lows", ("equal_levels_sweep",),
          ("equal_highs_magnet",)),
    Topic("break-of-structure", "Break of structure", ("structure_flip", "sweep_then_flip"),
          ("close_beats_wick",)),
    Topic("fair-value-gap", "Fair value gaps", ("fvg_retest",),
          ("fvg_gets_retested", "fvg_holds_after_retest")),
    Topic("inverse-fair-value-gap", "Inverse fair value gaps", ("ifvg_flip",),
          ("ifvg_before_bos",)),
    Topic("premium-discount", "The 50% line", ("premium_discount",), ()),
    Topic("sweep-break-retest", "Sweep, break and retest", ("liquidity_model",),
          ("sweep_alone_loses", "not_every_day_green")),
    Topic("opening-gaps", "Opening gaps", (), ("ndog_fills", "nwog_fills")),
    Topic("smt-divergence", "When related markets disagree", (), ("smt_at_sweep",)),
    Topic("daily-bias", "The daily lean", (), ("bias_works",)),
    Topic("breakouts", "Buying breakouts", ("crowd_breakout",),
          ("crowd_breakout_loses", "sr_coin_flip")),
    Topic("risk-and-sizing", "Risk and position size", (), ("not_every_day_green",)),
    Topic("journal", "Keeping a trading journal", (), ()),
)

TOPICS_BY_SLUG: Dict[str, Topic] = {t.slug: t for t in TOPICS}

# The journal's lists (group E). Keys are stored; labels are shown.
JOURNAL_SETUPS: List[Tuple[str, str]] = [(s.name, s.label) for s in SETUPS] + [
    ("orenth_call", "From an Orenth call"), ("own", "My own idea"),
    ("other", "Something else")]
EMOTIONS: List[Tuple[str, str]] = [
    ("calm", "Calm"), ("confident", "Confident"), ("unsure", "Unsure"),
    ("fomo", "Afraid of missing out"), ("revenge", "Wanting to win back a loss"),
    ("bored", "Bored"), ("tired", "Tired or distracted")]
FOLLOWED: List[Tuple[str, str]] = [("yes", "Yes"), ("partly", "Partly"), ("no", "No")]
CHECKLIST: List[Tuple[str, str]] = [
    ("bias", "I know which way I expect the day to go, and why"),
    ("liquidity", "An old high or low was taken first"),
    ("structure", "Price then broke its last swing the other way"),
    ("pullback", "I am entering on a pullback into a gap or the 50% line, not chasing"),
    ("stop", "My stop sits beyond the high or low that was taken"),
    ("target", "My target is an old high or low at least 1.5 times my risk away"),
    ("rules", "I am inside today's limits: losses, number of trades, time"),
    ("size", "My size risks no more than my risk setting")]

# The Setup lab's instruments: (symbol, name, group). Groups that follow the
# same index (ES and SPY, NQ and QQQ) are not independent evidence.
LAB_SYMBOLS: Tuple[Tuple[str, str, str], ...] = (
    ("ES=F", "S&P 500 futures", "index"), ("NQ=F", "Nasdaq 100 futures", "index"),
    ("SPY", "S&P 500 fund", "index"), ("QQQ", "Nasdaq 100 fund", "index"),
    ("BTC-USD", "Bitcoin", "coin"), ("ETH-USD", "Ether", "coin"),
    ("AAPL", "Apple", "stock"), ("NVDA", "NVIDIA", "stock"))
LAB_INTERVALS: Tuple[str, ...] = ("5m", "15m")
PAIRS: Tuple[Tuple[str, str], ...] = (("ES=F", "NQ=F"), ("SPY", "QQQ"),
                                      ("BTC-USD", "ETH-USD"))


def setup(name: str) -> Setup:
    """The setup by its strategy name; KeyError for one that is not here."""
    return BY_NAME[name]


def claim(cid: str) -> Claim:
    """The claim by its id; KeyError for one that is not here."""
    return CLAIMS_BY_ID[cid]


def topic(slug: str) -> Topic:
    """The Learn topic by its slug; KeyError for one that is not here."""
    return TOPICS_BY_SLUG[slug]


def seconds(stamp: str) -> int:
    """'1:36:14' as seconds into the video. ValueError for anything else."""
    parts = stamp.strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError("not h:mm:ss: %r" % stamp)
    h, m, s = (int(p) for p in parts)
    if len(parts[1]) != 2 or len(parts[2]) != 2 or m > 59 or s > 59:
        raise ValueError("not h:mm:ss: %r" % stamp)
    return h * 3600 + m * 60 + s
