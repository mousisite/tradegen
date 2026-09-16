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


def explain_plan(plan, bars, hold_text: str = "") -> Dict:
    """The trade plan, said plainly.

    Returns a headline, a few short points, and one honest line about how sure
    the app actually is.
    """
    action = (plan.action or "").upper()
    currency = getattr(bars, "currency", "USD")
    points: List[str] = []

    headline = _WHAT_IT_MEANS.get(action, "The app has no clear view here.")

    # What to do, concretely.
    if action == "WAIT" and plan.entry:
        points.append(
            "Watch for the price to reach %s. That is the app's buy price."
            % _money(plan.entry, currency))
    elif action == "BUY" and plan.entry:
        points.append("The app's buy price is %s." % _money(plan.entry, currency))
    elif action == "SHORT" and plan.entry:
        points.append(
            "Shorting means betting the price falls. It is riskier than buying "
            "because losses have no limit. Most beginners should skip it.")
    elif action == "AVOID":
        points.append(
            "There is no setup here worth the trading fees right now. That is "
            "a normal answer, not a broken one.")

    if plan.stop and plan.entry:
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
        sure = ("The app looked at %d times this same setup happened before. "
                "It worked %d of those times."
                % (plan.prob_samples, worked))
        if plan.breakeven_rate:
            needed = int(round(plan.breakeven_rate * plan.prob_samples))
            if worked <= needed:
                sure += (" It needed %d wins just to cover trading fees, so "
                         "this has not been shown to make money." % needed)
            else:
                sure += (" It needed %d wins to cover trading fees, so it "
                         "cleared that bar, but not by a lot." % needed)
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


# --- the company ------------------------------------------------------------

def explain_company(research) -> Dict:
    """What the filings say, without the accounting words."""
    points: List[str] = []
    history = getattr(research, "sec_history", None) or {}
    facts = getattr(research, "fundamentals", None)
    scores = getattr(research, "scores", None) or {}

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
