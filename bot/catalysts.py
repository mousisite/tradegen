"""Scheduled events that sit inside a trade's holding window.

Technical analysis describes a market that is digesting known information. An
earnings date is the moment a large amount of unknown information arrives at
once, and price gaps to wherever the new information puts it. A stop does not
help: the gap opens past it.

So the question this module answers is not "when does this company report" but
"does it report before this plan expects to be finished". A plan with a
two-week horizon and earnings in three days is a different, worse trade than
the same setup with earnings in six weeks, and nothing else in the pipeline
notices the difference.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import yahoo

# Yahoo exposes the calendar on its own module, so this is one small request
# rather than the full fundamentals payload.
_MODULES = "calendarEvents,quoteType"

# Roughly how much of a session a bar covers, used to turn a horizon measured
# in bars into one measured in days. Intraday horizons are a fraction of a day.
_BARS_PER_DAY = {"1m": 390, "2m": 195, "5m": 78, "15m": 26, "30m": 13,
                 "60m": 6.5, "1h": 6.5, "1d": 1, "5d": 0.2, "1wk": 0.2,
                 "1mo": 0.05}


@dataclass
class Calendar:
    """Dated events for one instrument, and how far away each one is."""
    symbol: str
    earnings: Optional[int] = None          # unix seconds
    earnings_estimated: bool = False
    ex_dividend: Optional[int] = None
    dividend_paid: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    available: bool = True

    def days_to(self, stamp: Optional[int]) -> Optional[float]:
        if not stamp:
            return None
        return (stamp - time.time()) / 86400.0

    @property
    def days_to_earnings(self) -> Optional[float]:
        return self.days_to(self.earnings)

    @property
    def days_to_ex_dividend(self) -> Optional[float]:
        return self.days_to(self.ex_dividend)


def horizon_days(interval: str, horizon_bars: int) -> float:
    """Turn a horizon in bars into one in calendar days.

    Trading days, not calendar days, are what the horizon is counted in, so a
    ten-bar daily horizon is about two calendar weeks. The 7/5 factor converts.
    """
    per_day = _BARS_PER_DAY.get(interval, 1)
    trading_days = horizon_bars / per_day if per_day else horizon_bars
    return trading_days * 7.0 / 5.0


def _stamp(value) -> Optional[int]:
    """A unix timestamp from whatever shape Yahoo returned it in."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    # Reject anything implausible rather than reporting a date in 1970.
    if stamp < 946684800:               # 2000-01-01
        return None
    return stamp


def fetch(symbol: str) -> Calendar:
    """The dated events Yahoo knows about for one instrument.

    Never raises. An instrument with no calendar, such as a cryptocurrency or
    an index, returns an empty one rather than an error, because having no
    earnings date is a fact about the instrument, not a failure.
    """
    out = Calendar(symbol=symbol.upper())
    try:
        mods = yahoo.quote_summary(symbol, _MODULES)
    except Exception as exc:
        out.available = False
        out.notes.append("Could not read the calendar for %s: %s" % (symbol, exc))
        return out

    kind = ((mods.get("quoteType") or {}).get("quoteType") or "").upper()
    if kind in ("CRYPTOCURRENCY", "CURRENCY", "INDEX"):
        out.notes.append("%s has no scheduled company events." % out.symbol)
        return out

    cal = mods.get("calendarEvents") or {}
    earnings = cal.get("earnings") or {}
    dates = earnings.get("earningsDate") or []
    if dates:
        out.earnings = _stamp(dates[0])
        # Yahoo gives a range when the date is not yet confirmed by the company.
        out.earnings_estimated = len(dates) > 1 and _stamp(dates[-1]) != out.earnings

    out.ex_dividend = _stamp(cal.get("exDividendDate"))
    out.dividend_paid = _stamp(cal.get("dividendDate"))

    if out.earnings is None and kind in ("EQUITY", ""):
        out.notes.append("No earnings date published for %s yet." % out.symbol)
    return out


def _when(days: float) -> str:
    if days < 1:
        return "today or tomorrow"
    if days < 2:
        return "tomorrow"
    return "in %d days" % round(days)


def warnings_for(cal: Calendar, interval: str, horizon_bars: int,
                 direction: int = 1) -> List[str]:
    """What to say about a plan whose window contains a scheduled event.

    Only events inside the window are mentioned. An earnings date six weeks
    after a two-week trade is not this trade's problem, and saying so anyway
    trains people to ignore the warning that matters.
    """
    out: List[str] = []
    window = horizon_days(interval, horizon_bars)
    days = cal.days_to_earnings

    if days is not None and days >= 0:
        confirmed = "" if not cal.earnings_estimated else ", though the date is " \
                                                          "an estimate rather than confirmed"
        if days <= window:
            out.append(
                "Earnings are due %s%s, inside the %d-day window this plan "
                "expects to need. Earnings gap price to wherever the new "
                "information puts it, and a stop does not survive a gap. Either "
                "size this as a position you can afford to have gap against you, "
                "or wait until after the report."
                % (_when(days), confirmed, round(window)))
        elif days <= window * 2:
            out.append(
                "Earnings are due %s%s. That is outside this plan's %d-day "
                "window, but not by much, so a trade that runs long will meet "
                "them." % (_when(days), confirmed, round(window)))

    ex_div = cal.days_to_ex_dividend
    if ex_div is not None and 0 <= ex_div <= window:
        if direction < 0:
            out.append(
                "This goes ex-dividend %s. A short position pays the dividend "
                "rather than receiving it." % _when(ex_div))
        else:
            out.append(
                "This goes ex-dividend %s, so the price drops by roughly the "
                "dividend that morning. That is not the thesis failing."
                % _when(ex_div))
    return out


def describe(cal: Calendar) -> List[Dict]:
    """The calendar as rows for display, nearest first, past events dropped."""
    rows = []
    for label, stamp, meaning in (
        ("Next earnings", cal.earnings,
         "The largest scheduled source of a gap."),
        ("Ex-dividend", cal.ex_dividend,
         "Price drops by about the dividend on this date."),
        ("Dividend paid", cal.dividend_paid,
         "Cash reaches the account."),
    ):
        if not stamp:
            continue
        days = cal.days_to(stamp)
        if days is None or days < -1:
            continue
        rows.append({
            "label": label,
            "when": datetime.fromtimestamp(stamp, timezone.utc).strftime("%d %b %Y"),
            "days": days,
            "estimated": label == "Next earnings" and cal.earnings_estimated,
            "meaning": meaning,
        })
    rows.sort(key=lambda r: r["days"])
    return rows
