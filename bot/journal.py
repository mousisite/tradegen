"""What a trading journal says back: tags, results by tag, daily rules, size and survival.

The journal already counts every closed trade in R after costs
(database.journal_summary). This module answers the next questions a person
keeping one asks, each from their own trades:

* **Which of my habits make money?** Results split by the setup tagged, the
  New York session and time of the morning it was entered in, the weekday,
  how the person felt, whether they followed their plan, whether they broke
  a daily rule, and whether the checklist was all ticked. A margin of error
  is given only from ten trades in a group, and the page says below thirty
  closed trades that nothing yet separates skill from luck.
* **Should I stop for today?** The daily rules a person sets (a loss limit in
  R, most trades a day, no new trades after a New York time), checked before
  a trade is logged. The person can still log it, and the trade is then
  marked as one that broke a rule, so the breakdown can show what that cost.
* **How big, and how long would I last?** A size calculator for shares, coins
  and futures contracts, and a survival check that reshuffles a record of
  wins and losses to show what different risks a trade do to an account.

The course this follows (TJR, "How To Start Day Trading In 2026") teaches
reviewing every trade (0:27:33-0:28:35), judging a method by its win rate and
its reward to risk (0:33:48-0:34:53), stopping for the day (4:33:23, 5:31:34),
and risking an amount that survives a losing run (5:34:43, 5:39:53-5:44:07).
Everything here is measured on the person's own journal or on numbers they
type; nothing here is advice about what to trade.

Every function is pure apart from reading the trades table, so the tests can
check exact numbers.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import clock, setups
from . import database as db

# --- the journal's lists, as labels ---------------------------------------------

SETUP_LABELS: Dict[str, str] = dict(setups.JOURNAL_SETUPS)
EMOTION_LABELS: Dict[str, str] = dict(setups.EMOTIONS)
FOLLOWED_LABELS: Dict[str, str] = dict(setups.FOLLOWED)
CHECK_KEYS: Tuple[str, ...] = tuple(k for k, _ in setups.CHECKLIST)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# app choice: a group's margin of error is shown only from 10 trades; below
# that two standard errors are wider than anything they could say.
MIN_MARGIN = 10
# The journal page's own minimum before any figure means something.
MIN_TRUST = 30
# app choice: the survival check reshuffles a journal only from 20 closed
# trades; fewer is too small a bag of results to draw a hundred from.
SURVIVAL_MIN = 20
# How far back an entry time may be typed.
BACKDATE_DAYS = 30
# A typed entry time a little in the future is the phone's clock, not a
# mistake: up to this many seconds it is read as now.
CLOCK_SLACK = 180

# What the breakdown splits by, in the order the page shows them.
BY: Tuple[Tuple[str, str], ...] = (
    ("setup", "By setup"),
    ("session", "By the session you entered in"),
    ("window", "By the time of the morning"),
    ("weekday", "By weekday"),
    ("emotion", "By how you felt"),
    ("followed_plan", "By whether you followed your plan"),
    ("broke_rule", "By whether you broke a daily rule"),
    ("checklist", "By the checklist"),
)
BY_KEYS = tuple(k for k, _ in BY)

UNTAGGED = "Not tagged"
OUTSIDE = "outside"
WINDOW_LABELS = dict(clock.WINDOW_LABELS, **{OUTSIDE: "Outside 9:30 to 16:00"})
RULE_NAMES = {"loss": "the daily loss limit", "trades": "the most trades a day",
              "time": "the last entry time"}


# --- tags --------------------------------------------------------------------------

def check_tags(setup: Optional[str] = None, emotion: Optional[str] = None,
               checklist: Optional[Iterable[str]] = None,
               followed_plan: Optional[str] = None) -> None:
    """Refuse a tag that is not on the journal's lists. ValueError in words."""
    if setup and setup not in SETUP_LABELS:
        raise ValueError("That setup is not one the journal knows. Pick one from the list.")
    if emotion and emotion not in EMOTION_LABELS:
        raise ValueError("That feeling is not one the journal knows. Pick one from the list.")
    if followed_plan and followed_plan not in FOLLOWED_LABELS:
        raise ValueError("Say yes, partly or no for whether you followed your plan.")
    for key in checklist or ():
        if key not in CHECK_KEYS:
            raise ValueError("That checklist item is not one the journal knows.")


def checklist_of(row) -> Optional[List[str]]:
    """The checklist keys a trade was logged with, or None when it was not used."""
    raw = db._column(row, "checklist")
    if raw in (None, ""):
        return None
    try:
        keys = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return [k for k in keys if k in CHECK_KEYS] if isinstance(keys, list) else None


def tags_of(row) -> List[str]:
    """A trade's tags as short words for its row in the journal."""
    out = []
    setup = db._column(row, "setup")
    if setup in SETUP_LABELS:
        out.append(SETUP_LABELS[setup])
    emotion = db._column(row, "emotion")
    if emotion in EMOTION_LABELS:
        out.append(EMOTION_LABELS[emotion])
    ticked = checklist_of(row)
    if ticked is not None:
        out.append("checklist %d of %d" % (len(ticked), len(CHECK_KEYS)))
    return out


def broke_rules_of(row) -> List[str]:
    """The daily rules a trade was logged against, in words."""
    raw = db._column(row, "broke_rule") or ""
    return [RULE_NAMES[k] for k in raw.split(",") if k in RULE_NAMES]


def session_of_trade(row) -> str:
    """The New York session the trade was entered in (clock.SESSIONS)."""
    return clock.session_of(int(row["ts_opened"]))


def window_of_trade(row) -> str:
    """The course's New York window the trade was entered in, or "outside"."""
    return clock.window_of(int(row["ts_opened"])) or OUTSIDE


# --- entry times ------------------------------------------------------------------

def entry_field(ts: int) -> str:
    """A time as a datetime-local field shows it, in New York time: 2026-11-19T10:02."""
    day, minute, _ = clock.ny_clock(int(ts))
    return "%04d-%02d-%02dT%02d:%02d" % (day.year, day.month, day.day,
                                         minute // 60, minute % 60)


def parse_entry(text: str) -> Optional[int]:
    """A datetime-local value, read as New York time, as a UTC timestamp.

    None for an empty field. ValueError in words for anything else that is
    not a date and a time.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        day, _, hm = text.replace(" ", "T").partition("T")
        y, mo, d = (int(p) for p in day.split("-"))
        hh, mm = (int(p) for p in hm.split(":")[:2])
        date(y, mo, d)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("The entry time must be a date and a time, New York time.")
    return clock.ny(y, mo, d, hh, mm)


def check_opened(opened_ts: Optional[int], now: Optional[int] = None) -> Optional[int]:
    """An entry time a trade may be logged with: not in the future, at most 30
    days back. Returns it (a moment within the clock slack is now), or None
    for none given. ValueError in words otherwise."""
    if opened_ts is None:
        return None
    now = int(now if now is not None else time.time())
    opened_ts = int(opened_ts)
    if opened_ts > now + CLOCK_SLACK:
        raise ValueError("The entry time is in the future. Give the time you entered, "
                         "New York time.")
    if opened_ts < now - BACKDATE_DAYS * 86400:
        raise ValueError("The entry time can be at most %d days ago." % BACKDATE_DAYS)
    return min(opened_ts, now)


# --- each closed trade's result after costs ----------------------------------------

def closed_results(conn, user: Optional[int], costs: Optional[Dict[str, float]],
                   account: str = "paper") -> List[Dict]:
    """Every closed trade with its R after costs, oldest close first.

    The R is worked out exactly as database.journal_summary does it: the R
    of the whole position, each part weighted by its size, less the round
    trip's cost in R. So the breakdown's figures add up to the journal's.
    """
    sql, args = db._own("SELECT * FROM trades WHERE status='closed' AND account=?",
                        [account], user)
    rows = list(conn.execute(sql + " ORDER BY ts_closed, id", args))
    exits = db.exits_for(conn, [row["id"] for row in rows])
    out = []
    for row in rows:
        parts = db.trade_parts(row, exits.get(row["id"], []))
        entry = float(row["entry"])
        risk = abs(entry - float(row["stop"]))
        cost = db._cost_of(row, costs or {})
        cost_r = (entry * cost / risk) if risk > 0 else 0.0
        out.append({"row": row, "r": float(parts["r"] or 0.0) - cost_r,
                    "opened": int(row["ts_opened"]),
                    "closed": int(row["ts_closed"] or row["ts_opened"])})
    return out


def _stats(rs: Sequence[float]) -> Dict:
    """Trades, wins, hit rate, average and total R, and a margin from 10 trades."""
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    total = float(sum(rs))
    mean = total / n if n else 0.0
    margin = None
    if n >= MIN_MARGIN:
        sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1))
        margin = 2.0 * sd / math.sqrt(n)
    return {"trades": n, "wins": wins, "hit_rate": (wins / n) if n else 0.0,
            "avg_r": mean, "total_r": total, "margin": margin,
            "verdict": _verdict(mean, margin)}


def _verdict(mean: float, margin: Optional[float]) -> str:
    """What a group's average says, in the words the rest of the site uses."""
    if margin is None:
        return "too few to tell"
    if mean - margin > 0:
        return "made money by more than its margin of error"
    if mean + margin < 0:
        return "lost money by more than its margin of error"
    return "cannot be told apart from luck"


def _group_of(item: Dict, by: str) -> Tuple[str, str]:
    """(key, label) of the group a closed trade falls in."""
    row = item["row"]
    if by == "setup":
        key = db._column(row, "setup") or ""
        return (key, SETUP_LABELS[key]) if key in SETUP_LABELS else ("", UNTAGGED)
    if by == "emotion":
        key = db._column(row, "emotion") or ""
        return (key, EMOTION_LABELS[key]) if key in EMOTION_LABELS else ("", UNTAGGED)
    if by == "followed_plan":
        key = db._column(row, "followed_plan") or ""
        return (key, FOLLOWED_LABELS[key]) if key in FOLLOWED_LABELS else ("", "Not reviewed")
    if by == "session":
        key = session_of_trade(row)
        return key, clock.SESSION_LABELS[key]
    if by == "window":
        key = window_of_trade(row)
        return key, WINDOW_LABELS[key]
    if by == "weekday":
        _, _, weekday = clock.ny_clock(item["opened"])
        return str(weekday), WEEKDAYS[weekday]
    if by == "broke_rule":
        return ("yes", "Broke a rule") if db._column(row, "broke_rule") else ("no", "Within the rules")
    if by == "checklist":
        ticked = checklist_of(row)
        if ticked is None:
            return "", "Checklist not used"
        if len(set(ticked)) == len(CHECK_KEYS):
            return "all", "All %d ticked" % len(CHECK_KEYS)
        return "some", "Not all ticked"
    raise ValueError("The journal cannot be split by %r." % by)


def _order(by: str) -> List[str]:
    """The order a split's groups are shown in; anything else goes last."""
    if by == "setup":
        return [k for k, _ in setups.JOURNAL_SETUPS] + [""]
    if by == "emotion":
        return [k for k, _ in setups.EMOTIONS] + [""]
    if by == "followed_plan":
        return [k for k, _ in setups.FOLLOWED] + [""]
    if by == "session":
        return list(clock.SESSION_NAMES)
    if by == "window":
        return [w[0] for w in clock.WINDOWS] + [OUTSIDE]
    if by == "weekday":
        return [str(d) for d in range(7)]
    if by == "broke_rule":
        return ["no", "yes"]
    return ["all", "some", ""]


def split(items: Sequence[Dict], by: str) -> List[Dict]:
    """The closed trades split by one tag: each group's stats, in a fixed order."""
    groups: Dict[str, Dict] = {}
    for item in items:
        key, label = _group_of(item, by)
        groups.setdefault(key, {"key": key, "label": label, "rs": []})["rs"].append(item["r"])
    order = _order(by)
    keys = sorted(groups, key=lambda k: (order.index(k) if k in order else len(order), k))
    return [dict(_stats(groups[k]["rs"]), key=k, label=groups[k]["label"]) for k in keys]


def breakdown(conn, user: Optional[int], costs: Optional[Dict[str, float]],
              by: str) -> List[Dict]:
    """The person's closed trades, after costs, split by one of BY_KEYS."""
    if by not in BY_KEYS:
        raise ValueError("The journal cannot be split by %r." % by)
    return split(closed_results(conn, user, costs), by)


def _days(items: Sequence[Dict]) -> Dict[date, float]:
    """R after costs per New York date, each trade on the day it closed."""
    out: Dict[date, float] = {}
    for item in items:
        day = clock.ny_clock(item["closed"])[0]
        out[day] = out.get(day, 0.0) + item["r"]
    return out


def green_days(items: Sequence[Dict]) -> Dict:
    """Days that ended up, down and even, each trade counted on the day it closed."""
    days = _days(items)
    green = sum(1 for v in days.values() if v > 1e-12)
    red = sum(1 for v in days.values() if v < -1e-12)
    return {"days": len(days), "green": green, "red": red, "even": len(days) - green - red,
            "share": (green / len(days)) if days else None}


def breakeven_rate(items: Sequence[Dict]) -> Optional[Dict]:
    """The hit rate the person's own average win and loss need to break even.

    With an average win of W R and an average loss of L R (after costs), a
    hit rate p breaks even when p W = (1 - p) L, so p = L / (W + L). Said
    beside the hit rate actually reached, over the trades that won or lost.
    None until there is at least one of each.
    """
    wins = [i["r"] for i in items if i["r"] > 0]
    losses = [-i["r"] for i in items if i["r"] < 0]
    if not wins or not losses:
        return None
    w, l = sum(wins) / len(wins), sum(losses) / len(losses)
    return {"avg_win": w, "avg_loss": l, "needed": l / (w + l),
            "reached": len(wins) / (len(wins) + len(losses)),
            "decided": len(wins) + len(losses)}


def best_day_share(items: Sequence[Dict]) -> Optional[Dict]:
    """The best day's R as a share of the total, when the total is a profit.

    Over 1 means the other days lost money between them: without the best
    day the journal would be down.
    """
    days = _days(items)
    total = sum(days.values())
    if not days or total <= 1e-12:
        return None
    day, best = max(days.items(), key=lambda kv: kv[1])
    if best <= 0:
        return None
    return {"day": day, "best_r": best, "total_r": total, "share": best / total}


def review(conn, user: Optional[int], costs: Optional[Dict[str, float]]) -> Dict:
    """Everything the breakdown page shows, from one read of the journal."""
    items = closed_results(conn, user, costs)
    return {"trades": len(items), "overall": _stats([i["r"] for i in items]),
            "splits": [(by, title, split(items, by)) for by, title in BY],
            "green": green_days(items), "breakeven": breakeven_rate(items),
            "best_day": best_day_share(items), "items": items}


# --- daily rules ---------------------------------------------------------------------

def costs_from(cfg: Dict) -> Dict[str, float]:
    """Round-trip costs as fractions of the price, from a person's settings."""
    return {"equity": float(cfg.get("cost_bps_equity", 2.0)) / 10000.0,
            "crypto": float(cfg.get("cost_bps_crypto", 20.0)) / 10000.0,
            "other": float(cfg.get("cost_bps_other", 5.0)) / 10000.0,
            "futures": float(cfg.get("cost_bps_futures", 1.0)) / 10000.0}


def _hhmm(minutes: int) -> str:
    return "%d:%02d" % (minutes // 60, minutes % 60)


def rules_check(conn, user: Optional[int], cfg: Dict, now: Optional[int] = None,
                costs: Optional[Dict[str, float]] = None,
                present: Optional[int] = None) -> List[Dict]:
    """The daily rules a new trade entered at `now` would break, in words.

    "Today" is the New York date of `now` (the entry time of the trade being
    logged, so a trade typed in for an earlier time is held to that day).
    The loss is the R after costs of trades closed that day by then; the
    count is the trades entered that day by then; the time rule compares
    New York's clock with the person's last entry time. A rule set to 0 or
    left empty is off. Each broken rule is {"rule", "words", "past"}.

    present is the time it really is, when that may be later than `now`: a
    trade from an earlier day is told "that day" (and is "past"), and one
    from earlier the same day the time it was entered.
    """
    from .config import clock_minutes
    now = int(now if now is not None else time.time())
    present = now if present is None else int(present)
    day, minute, _ = clock.ny_clock(now)
    past = clock.ny_clock(present)[0] != day
    when = "that day" if past else "today"
    out: List[Dict] = []

    limit = float(cfg.get("daily_loss_limit_r") or 0.0)
    if limit > 0:
        lost = -sum(i["r"] for i in closed_results(conn, user, costs or costs_from(cfg))
                    if i["closed"] <= now and clock.ny_clock(i["closed"])[0] == day)
        if lost >= limit - 1e-9:
            out.append({"rule": "loss", "past": past,
                        "words": "you %s lost %.1fR %s (limit %gR)"
                        % ("had" if past else "have", lost, when, limit)})

    most = int(cfg.get("max_trades_per_day") or 0)
    if most > 0:
        sql, args = db._own("SELECT ts_opened FROM trades WHERE account='paper' "
                            "AND ts_opened <= ? AND ts_opened >= ?",
                            [now, now - 2 * 86400], user)
        taken = sum(1 for (ts,) in conn.execute(sql, args)
                    if clock.ny_clock(int(ts))[0] == day)
        if taken >= most:
            out.append({"rule": "trades", "past": past,
                        "words": "you %s taken %d trade%s %s (most %d)"
                        % ("had" if past else "have", taken, "" if taken == 1 else "s",
                           when, most)})

    last = clock_minutes(str(cfg.get("last_entry_time") or ""))
    if last is not None and minute > last:
        earlier = past or present - now > CLOCK_SLACK
        out.append({"rule": "time", "past": past, "words": "%s %s New York time, after your "
                    "last entry time of %s" % ("you entered at" if earlier else "it is",
                                               _hhmm(minute), _hhmm(last))})
    return out


def rules_words(broken: Sequence[Dict]) -> str:
    """"Your rule says stop for today: you have lost 2.1R today (limit 2R)."."""
    if not broken:
        return ""
    said = [b["words"] for b in broken]
    joined = said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
    if broken[0].get("past"):
        return "Your rule%s said stop that day: %s." % ("s" if len(said) > 1 else "", joined)
    return "Your rule%s say%s stop for today: %s." % (
        "s" if len(said) > 1 else "", "" if len(said) > 1 else "s", joined)


def loss_limit_check(items: Sequence[Dict], limit_r: float,
                     target_r: Optional[float] = None) -> Dict:
    """Whether three kinds of loss limit, and a profit target, would have been hit.

    Walked trade by trade in the order they closed, in R after costs from a
    start of 0. static: the balance falls limit_r below the start. eod: it
    falls limit_r below the best balance any earlier day ended on (a
    trailing limit that moves at the end of each day). trailing: it falls
    limit_r below the best balance after any trade (one that moves at once).
    target: the balance reaches target_r. Each is the trade that did it
    ({"ts", "balance"}) or None. Only closed trades are seen, so a loss that
    was deeper while a trade was open is not.
    """
    out = {"static": None, "eod": None, "trailing": None, "target": None,
           "trades": len(items)}
    balance, peak, day_peak, today, today_end = 0.0, 0.0, 0.0, None, 0.0
    for item in sorted(items, key=lambda i: (i["closed"], i["row"]["id"])):
        day = clock.ny_clock(item["closed"])[0]
        if today is not None and day != today:
            day_peak = max(day_peak, today_end)
        today = day
        balance += item["r"]
        today_end = balance
        peak = max(peak, balance)
        hit = {"ts": item["closed"], "balance": balance}
        if out["static"] is None and balance <= -limit_r + 1e-9:
            out["static"] = hit
        if out["eod"] is None and balance <= day_peak - limit_r + 1e-9:
            out["eod"] = hit
        if out["trailing"] is None and balance <= peak - limit_r + 1e-9:
            out["trailing"] = hit
        if target_r and out["target"] is None and balance >= target_r - 1e-9:
            out["target"] = hit
    out["balance"] = balance
    return out


# --- size --------------------------------------------------------------------------

# Futures contracts the calculator knows: (name, dollars per point, tick size).
CONTRACTS: Dict[str, Tuple[str, float, float]] = {
    "ES": ("E-mini S&P 500", 50.0, 0.25), "MES": ("Micro E-mini S&P 500", 5.0, 0.25),
    "NQ": ("E-mini Nasdaq-100", 20.0, 0.25), "MNQ": ("Micro E-mini Nasdaq-100", 2.0, 0.25),
    "YM": ("E-mini Dow", 5.0, 1.0), "MYM": ("Micro E-mini Dow", 0.5, 1.0),
    "RTY": ("E-mini Russell 2000", 50.0, 0.1), "M2K": ("Micro E-mini Russell 2000", 5.0, 0.1),
    "CL": ("Crude oil", 1000.0, 0.01), "MCL": ("Micro crude oil", 100.0, 0.01),
    "GC": ("Gold", 100.0, 0.1), "MGC": ("Micro gold", 10.0, 0.1),
}
MICRO = {"ES": "MES", "NQ": "MNQ", "YM": "MYM", "RTY": "M2K", "CL": "MCL", "GC": "MGC"}
KINDS = ("shares", "coins", "futures")
# Reward to risk the break-even table is worked out for, beside a target's own.
BREAKEVEN_RS = (1.0, 1.5, 2.0, 3.0)


def breakeven_for(reward_r: float, cost_r: float) -> float:
    """The hit rate a fixed reward to risk needs, after costs: (1 + c) / (1 + R).

    A win pays R less the cost, a loss costs 1 plus it: p (R - c) = (1 - p)(1 + c).
    """
    return (1.0 + cost_r) / (1.0 + reward_r)


def _money(value: float) -> str:
    return "${:,.2f}".format(value)


def size(account: float, risk_pct: float, entry: float, stop: float,
         kind: str = "shares", contract: str = "", point_value: Optional[float] = None,
         cost_bps: float = 0.0, target: Optional[float] = None) -> Dict:
    """How much to trade so the stop loses at most risk_pct of the account.

    Shares are whole; coins are cut to six decimals; futures contracts are
    whole, at the contract's dollars per point (or point_value for one of
    your own). Rounded down, never up, so the risk is at most the setting.
    Raises ValueError in words for anything that cannot be sized.
    """
    for label, value in (("The account", account), ("The risk", risk_pct),
                         ("The entry", entry), ("The stop", stop)):
        if value is None or not math.isfinite(float(value)):
            raise ValueError("%s must be a number." % label)
    if account <= 0:
        raise ValueError("The account must be more than zero.")
    if not 0 < risk_pct <= 100:
        raise ValueError("The risk must be more than 0% and at most 100% of the account.")
    if entry <= 0 or stop <= 0:
        raise ValueError("The entry and the stop must be prices above zero.")
    if entry == stop:
        raise ValueError("The stop must be away from the entry.")
    if kind not in KINDS:
        raise ValueError("Size shares, coins or futures contracts.")
    if cost_bps is None or not math.isfinite(float(cost_bps)) or cost_bps < 0:
        raise ValueError("The cost must be zero or more.")

    name, label, micro = "", "", None
    multiplier = 1.0
    if kind == "futures":
        contract = (contract or "").upper()
        if contract in CONTRACTS:
            label, multiplier, _tick = CONTRACTS[contract]
            name = contract
            micro = MICRO.get(contract)
        else:
            if point_value is None or not math.isfinite(float(point_value)) or point_value <= 0:
                raise ValueError("Pick a contract, or give your contract's dollars per point.")
            multiplier = float(point_value)
            name, label = "contract", "your contract"

    direction = 1 if stop < entry else -1
    distance = abs(entry - stop)
    per_unit = distance * multiplier
    budget = account * risk_pct / 100.0
    # Real numbers that multiply or divide past what a computer holds: a page
    # anyone can open must say so rather than break.
    aim = float(target) if target is not None and math.isfinite(float(target)) else 0.0
    fits_in_a_number = per_unit > 0 and all(map(math.isfinite, (
        per_unit, budget, budget / per_unit * 1e6, budget / per_unit * entry * multiplier,
        entry * cost_bps / distance, aim / distance)))
    if not fits_in_a_number:
        raise ValueError("Those numbers are too large or too small to size.")
    raw = budget / per_unit
    if kind == "coins":
        qty = math.floor(raw * 1e6 + 1e-6) / 1e6
    else:
        qty = float(math.floor(raw + 1e-9))
    at_risk = qty * per_unit
    cost_r = (entry * cost_bps / 10000.0) / distance
    unit = {"shares": "share", "coins": "coin", "futures": "contract"}[kind]

    def at(multiple):
        """The price that makes this many R, None for a short when it would be zero or below."""
        price = entry + direction * multiple * distance
        return price if price > 0 else None

    out = {"kind": kind, "unit": unit, "direction": direction, "quantity": qty,
           "quantity_text": (("%.6f" % qty).rstrip("0").rstrip(".") if kind == "coins"
                             else "{:,.0f}".format(qty)),
           "per_unit": per_unit, "budget": budget, "at_risk": at_risk,
           "pct": at_risk / account * 100.0, "notional": qty * entry * multiplier,
           "cost_r": cost_r, "cost_bps": cost_bps, "contract": name, "label": label,
           "multiplier": multiplier, "distance": distance,
           "one_r": {"price": at(1), "cash": at_risk * (1 - cost_r)},
           "two_r": {"price": at(2), "cash": at_risk * (2 - cost_r)},
           "fits": qty > 0, "why_none": ""}
    rows = []
    for reward in BREAKEVEN_RS:
        rows.append({"reward": reward, "needed": breakeven_for(reward, cost_r), "target": False})
    if target is not None and math.isfinite(float(target)) and target > 0:
        reward = (float(target) - entry) * direction / distance
        if reward <= 0:
            raise ValueError("The target must be on the winning side of the entry.")
        out["target_r"] = reward
        rows = [r for r in rows if abs(r["reward"] - reward) > 1e-9]
        rows.append({"reward": reward, "needed": breakeven_for(reward, cost_r), "target": True})
    out["breakeven"] = sorted(rows, key=lambda r: r["reward"])

    if qty <= 0:
        what = {"shares": "share", "coins": "millionth of a coin"}.get(kind, name + " contract"
                                                                       if name != "contract"
                                                                       else "contract")
        said = ("Not even one %s fits: one risks %s at this stop, more than the %s your "
                "risk setting allows." % (what, _money(per_unit), _money(budget)))
        if micro:
            micro_risk = distance * CONTRACTS[micro][1]
            said += (" The micro contract %s is a tenth of the size: one risks %s."
                     % (micro, _money(micro_risk)))
        out["why_none"] = said
    return out


def fixed_contracts(account: float, contract: str = "MES", contracts: int = 3,
                    ticks: Sequence[int] = (16, 28, 35)) -> Dict:
    """What a fixed number of contracts risks at a few stop sizes, as the course
    sizes (5:39:53-5:44:07): a set number of contracts, and the stop in ticks."""
    contract = (contract or "MES").upper()
    if contract not in CONTRACTS:
        raise ValueError("Pick a contract from the list.")
    if account <= 0:
        raise ValueError("The account must be more than zero.")
    if contracts < 1:
        raise ValueError("Give at least one contract.")
    label, per_point, tick = CONTRACTS[contract]
    tick_value = per_point * tick
    if not math.isfinite(contracts * max(ticks) * tick_value / account * 100.0):
        raise ValueError("That account is too small to work this out for.")
    rows = [{"ticks": t, "points": t * tick, "cash": contracts * t * tick_value,
             "pct": contracts * t * tick_value / account * 100.0} for t in ticks]
    return {"contract": contract, "label": label, "contracts": contracts,
            "tick": tick, "tick_value": tick_value, "account": account, "rows": rows}


# --- survival -----------------------------------------------------------------------

RISKS = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0)


def survival(rs: Optional[Sequence[float]] = None, p: Optional[float] = None,
             win: Optional[float] = None, loss: Optional[float] = None,
             risks: Sequence[float] = RISKS, trades: int = 100, paths: int = 2000,
             seed: int = 7) -> Dict:
    """What different risks a trade do to an account over the same run of trades.

    The trades are drawn either from a record of results in R (rs, drawn with
    replacement) or from a hit rate p with a win of `win` R and a loss of
    `loss` R. Each path is `trades` trades long; every risk level is run on
    the very same paths, so they differ only by the risk. Each trade moves
    the balance by risk% of the balance times its R (a fixed share of what
    is there, as most people size), and a balance cannot go below zero.

    Per risk: the median ending balance (1.0 is where it started), the share
    of paths that at some point fell 20% and 50% below their highest, and
    the share that ended at zero. Also the median longest losing run. Draws
    are independent, so real losing runs, which bunch, are longer than these.
    """
    if rs is not None:
        draws_from = np.asarray([float(r) for r in rs if math.isfinite(float(r))])
        if draws_from.size == 0:
            raise ValueError("There are no results to draw from.")
        source = {"from": "journal", "trades": int(draws_from.size),
                  "hit_rate": float((draws_from > 0).mean()),
                  "avg_r": float(draws_from.mean())}
    else:
        if p is None or win is None or loss is None:
            raise ValueError("Give a hit rate, an average win and an average loss.")
        if not 0 < p < 1:
            raise ValueError("The hit rate must be more than 0% and less than 100%.")
        if not win > 0 or not loss > 0:
            raise ValueError("The average win and loss must be more than zero.")
        if win > 50 or loss > 50:
            raise ValueError("The average win and loss are in R, at most 50.")
        source = {"from": "typed", "trades": None, "hit_rate": float(p),
                  "avg_r": float(p * win - (1 - p) * loss)}
    rng = np.random.default_rng(seed)
    if rs is not None:
        draw = draws_from[rng.integers(0, draws_from.size, size=(paths, trades))]
    else:
        draw = np.where(rng.random((paths, trades)) < p, float(win), -float(loss))

    # The longest run of losses in each path, the same for every risk.
    run = np.zeros(paths, dtype=int)
    longest = np.zeros(paths, dtype=int)
    for k in range(trades):
        run = np.where(draw[:, k] < 0, run + 1, 0)
        longest = np.maximum(longest, run)

    rows = []
    for risk in risks:
        step = np.maximum(1.0 + float(risk) / 100.0 * draw, 0.0)
        balance = np.concatenate([np.ones((paths, 1)), np.cumprod(step, axis=1)], axis=1)
        peak = np.maximum.accumulate(balance, axis=1)
        fall = 1.0 - balance / peak
        worst = fall.max(axis=1)
        end = balance[:, -1]
        rows.append({"risk": float(risk), "median_end": float(np.median(end)),
                     "fall20": float((worst >= 0.2 - 1e-12).mean()),
                     "fall50": float((worst >= 0.5 - 1e-12).mean()),
                     "ruined": float((end <= 1e-9).mean())})
    return {"source": source, "rows": rows, "trades": trades, "paths": paths, "seed": seed,
            "longest_losing_run": float(np.median(longest))}


def survival_from_journal(conn, user: Optional[int],
                          costs: Optional[Dict[str, float]]) -> Optional[Dict]:
    """The survival check on a person's own closed trades, from 20 of them; else None."""
    items = closed_results(conn, user, costs)
    if len(items) < SURVIVAL_MIN:
        return None
    return survival(rs=[i["r"] for i in items])


# --- the trade picture ------------------------------------------------------------

def picture_intervals(interval: str) -> List[str]:
    """The bar sizes to try for a trade's picture: its own, then hourly, then daily."""
    own = interval or "1d"
    out = [own]
    for longer in ("1h", "1d"):
        if longer not in out and clock.interval_seconds(longer) > clock.interval_seconds(own):
            out.append(longer)
    return out


def bar_at(timestamps: Sequence[int], ts: int) -> int:
    """The bar a moment falls in: the last one starting at or before it (0 if none)."""
    import bisect
    k = bisect.bisect_right([int(t) for t in timestamps], int(ts)) - 1
    return max(0, k)


def picture_window(n: int, first: int, last: int, before: int, after: int,
                   most: int) -> Tuple[int, int]:
    """[start, end) of the bars to draw round a trade from bar first to bar last."""
    start = max(0, first - before)
    end = min(n, last + after + 1)
    if end - start > most:
        start = max(0, end - most)
    return start, end
