"""Financial modelling: what a business might be worth, and on what assumptions.

Three approaches, because no single one is trustworthy alone:

* **Discounted cash flow** builds a value from the cash the business produces.
  Honest but sensitive: small changes in the growth and discount assumptions
  move the answer enormously, which is why a sensitivity grid is returned
  alongside the point estimate rather than instead of it.
* **Reverse DCF** inverts the question. Instead of asking what the business is
  worth, it asks what growth the current price already assumes. That is often
  the more useful question, because it can be judged against history.
* **Multiples** compare the price to earnings, sales and cash flow, and to the
  company's own history.

Every model here refuses to produce a number when the inputs are missing.
A valuation built on invented inputs looks exactly like one built on real
inputs, which is what makes it dangerous.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .fundamentals import Fundamentals


def normalised_fcf(f: Fundamentals) -> Dict:
    """A starting free cash flow that is not hostage to one unusual year.

    Trailing free cash flow can swing hard on working capital, a legal
    settlement or a single large purchase. Building a ten-year model on one
    such year produces a confident and wrong answer, so where the SEC filings
    provide a history, the average of the last three years is used and the
    difference is reported.
    """
    trailing = f.get("free_cashflow")
    out = {"value": trailing, "source": "trailing twelve months (Yahoo)",
           "history": [], "note": ""}

    facts = f.sec_facts
    if facts is None:
        return out

    ocf = {r["fy"]: r["value"] for r in facts.series("operating_cashflow", 6)}
    capex = {r["fy"]: r["value"] for r in facts.series("capex", 6)}
    years = sorted(set(ocf) & set(capex))
    if len(years) < 3:
        return out

    history = [{"fy": y, "value": ocf[y] - abs(capex[y])} for y in years]
    out["history"] = history

    recent = [h["value"] for h in history[-3:]]
    average = sum(recent) / len(recent)
    if average <= 0:
        return out

    out["value"] = average
    out["source"] = "average of filed free cash flow, %s to %s" % (
        history[-3]["fy"], history[-1]["fy"])

    if trailing and trailing > 0:
        gap = (trailing - average) / average
        if abs(gap) > 0.25:
            out["note"] = (
                "The trailing figure of %.1fB is %.0f%% %s the three-year filed "
                "average of %.1fB. The average is used here, because one unusual "
                "year should not set a ten-year forecast."
                % (trailing / 1e9, abs(gap) * 100,
                   "above" if gap > 0 else "below", average / 1e9))
    return out


@dataclass
class Assumption:
    """One input to a model, and where it came from."""
    key: str
    label: str
    value: float
    source: str
    editable: bool = True
    note: str = ""


@dataclass
class Valuation:
    """A model's output, its assumptions, and how much to trust it."""
    method: str
    fair_value: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    upside: Optional[float] = None
    implied_growth: Optional[float] = None
    assumptions: List[Assumption] = field(default_factory=list)
    sensitivity: List[Dict] = field(default_factory=list)
    workings: List[Dict] = field(default_factory=list)
    usable: bool = False
    reason: str = ""
    notes: List[str] = field(default_factory=list)


def discounted_cash_flow(f: Fundamentals, price: float,
                         growth: Optional[float] = None,
                         terminal_growth: float = 0.025,
                         discount: Optional[float] = None,
                         years: int = 10) -> Valuation:
    """Value the business on the cash it is expected to produce.

    Free cash flow is grown for `years`, then capitalised into a terminal value
    using a perpetuity. The terminal value usually dominates the result, so it
    is reported separately rather than buried in the total.
    """
    v = Valuation(method="Discounted cash flow")

    norm = normalised_fcf(f)
    fcf = norm["value"]
    shares = f.get("shares_out")
    cash = f.get("total_cash") or 0.0
    debt = f.get("total_debt") or 0.0

    if not fcf or fcf <= 0:
        v.reason = ("This model needs positive free cash flow, and none was "
                    "reported. Companies that burn cash cannot be valued this "
                    "way; their worth depends on what happens when they stop.")
        return v
    if norm.get("note"):
        v.notes.append(norm["note"])
    if not shares or shares <= 0:
        v.reason = "Shares outstanding were not reported, so a per-share value "\
                   "cannot be produced."
        return v

    if growth is None:
        reported = f.get("revenue_growth")
        # Growth is capped hard. Extrapolating a 40% year for a decade is how
        # a spreadsheet produces a valuation ten times the truth.
        growth = max(0.0, min(reported if reported is not None else 0.06, 0.20))
        growth_src = ("reported revenue growth, capped at 20%"
                      if reported is not None else "6% default, nothing reported")
    else:
        growth_src = "your input"

    if discount is None:
        beta = f.get("beta")
        # Cost of equity via CAPM with a 4.2% risk-free rate and a 5% equity
        # risk premium, floored so a low-beta name cannot look risk-free.
        discount = 0.042 + (beta if beta else 1.0) * 0.05
        discount = max(0.07, min(discount, 0.16))
        discount_src = "CAPM using beta %.2f" % (beta if beta else 1.0)
    else:
        discount_src = "your input"

    if terminal_growth >= discount:
        v.reason = ("Terminal growth of %.1f%% is at or above the discount rate "
                    "of %.1f%%, which implies infinite value. Lower one of them."
                    % (terminal_growth * 100, discount * 100))
        return v

    v.assumptions = [
        Assumption("fcf", "Starting free cash flow", fcf, norm["source"], False),
        Assumption("growth", "Growth for %d years" % years, growth, growth_src),
        Assumption("terminal_growth", "Growth thereafter", terminal_growth, "your input"),
        Assumption("discount", "Discount rate", discount, discount_src),
        Assumption("shares", "Shares outstanding", shares, "Yahoo Finance", False),
    ]

    total_pv = 0.0
    flow = fcf
    for year in range(1, years + 1):
        # Growth fades linearly toward the terminal rate. A business does not
        # grow at its current rate forever, and pretending otherwise is the
        # single biggest source of inflated DCF values.
        faded = growth + (terminal_growth - growth) * (year - 1) / max(years - 1, 1)
        flow *= (1.0 + faded)
        pv = flow / ((1.0 + discount) ** year)
        total_pv += pv
        v.workings.append({"year": year, "growth": faded, "cash_flow": flow,
                           "present_value": pv})

    terminal = flow * (1.0 + terminal_growth) / (discount - terminal_growth)
    terminal_pv = terminal / ((1.0 + discount) ** years)
    enterprise = total_pv + terminal_pv
    equity = enterprise + cash - debt
    per_share = equity / shares

    v.fair_value = per_share
    v.upside = (per_share - price) / price if price else None
    v.usable = True
    v.workings.append({"year": "terminal", "growth": terminal_growth,
                       "cash_flow": terminal, "present_value": terminal_pv})

    share_of_terminal = terminal_pv / enterprise if enterprise else 0
    if share_of_terminal > 0.75:
        v.notes.append(
            "%.0f%% of this valuation is the terminal value, meaning almost all "
            "of it rests on what happens after year %d. Treat the precision as "
            "illusory." % (share_of_terminal * 100, years))

    # Sensitivity: the honest presentation of a DCF is a grid, not a number.
    grid = []
    for g_adj in (-0.04, -0.02, 0.0, 0.02, 0.04):
        row = {"growth": growth + g_adj, "values": []}
        for d_adj in (-0.02, -0.01, 0.0, 0.01, 0.02):
            d = discount + d_adj
            g = max(0.0, growth + g_adj)
            if terminal_growth >= d:
                row["values"].append(None)
                continue
            sub_total, sub_flow = 0.0, fcf
            for year in range(1, years + 1):
                faded = g + (terminal_growth - g) * (year - 1) / max(years - 1, 1)
                sub_flow *= (1.0 + faded)
                sub_total += sub_flow / ((1.0 + d) ** year)
            term = sub_flow * (1.0 + terminal_growth) / (d - terminal_growth)
            sub_total += term / ((1.0 + d) ** years)
            row["values"].append((sub_total + cash - debt) / shares)
        grid.append(row)
    v.sensitivity = grid

    spread = [val for row in grid for val in row["values"] if val]
    if spread:
        v.low, v.high = min(spread), max(spread)
        if v.low > 0 and v.high / v.low > 4:
            v.notes.append(
                "Across reasonable assumptions the answer ranges from %.2f to "
                "%.2f. That spread is the real output of this model; the single "
                "number is not." % (v.low, v.high))
    return v


def reverse_dcf(f: Fundamentals, price: float, years: int = 10,
                terminal_growth: float = 0.025,
                discount: Optional[float] = None) -> Valuation:
    """What growth does today's price already assume?

    Often the more answerable question. Rather than arguing about what a
    company is worth, it asks what the market has already priced in, which can
    be compared against what the business has actually delivered.
    """
    v = Valuation(method="Reverse DCF: growth already priced in")

    fcf = normalised_fcf(f)["value"]
    shares = f.get("shares_out")
    cash = f.get("total_cash") or 0.0
    debt = f.get("total_debt") or 0.0

    if not fcf or fcf <= 0 or not shares or shares <= 0 or not price:
        v.reason = ("Needs positive free cash flow, shares outstanding and a "
                    "price. At least one is missing.")
        return v

    if discount is None:
        beta = f.get("beta")
        discount = max(0.07, min(0.042 + (beta if beta else 1.0) * 0.05, 0.16))

    target_equity = price * shares
    target_enterprise = target_equity - cash + debt

    def value_at(growth: float) -> float:
        total, flow = 0.0, fcf
        for year in range(1, years + 1):
            faded = growth + (terminal_growth - growth) * (year - 1) / max(years - 1, 1)
            flow *= (1.0 + faded)
            total += flow / ((1.0 + discount) ** year)
        term = flow * (1.0 + terminal_growth) / (discount - terminal_growth)
        return total + term / ((1.0 + discount) ** years)

    low, high = -0.30, 1.00
    if value_at(high) < target_enterprise:
        v.reason = ("Even 100% annual growth for %d years does not justify this "
                    "price on cash flow alone. The market must be valuing "
                    "something this model cannot see." % years)
        return v

    implied = None
    for _ in range(90):
        mid = 0.5 * (low + high)
        if value_at(mid) > target_enterprise:
            high = mid
        else:
            low = mid
        implied = 0.5 * (low + high)

    # Deliberately no fair_value. This model answers "what is priced in?", not
    # "what is it worth?". Reporting the current price as a fair value would
    # drag any blended estimate toward the market's own opinion, which is the
    # one thing an independent valuation must not do.
    v.usable = True
    v.implied_growth = implied
    v.assumptions = [
        Assumption("price", "Current price", price, "live market", False),
        Assumption("implied_growth", "Growth the price implies", implied, "solved"),
        Assumption("discount", "Discount rate", discount, "CAPM"),
        Assumption("terminal_growth", "Growth thereafter", terminal_growth, "assumed"),
    ]

    actual = f.get("revenue_growth")
    verdict = ("At %.2f the market is assuming free cash flow grows about %.1f%% "
               "a year for %d years." % (price, implied * 100, years))
    if actual is not None:
        verdict += (" The business most recently grew revenue %.1f%%." % (actual * 100))
        if implied > actual + 0.08:
            verdict += (" The price needs materially faster growth than the "
                        "company is currently delivering.")
        elif implied < actual - 0.05:
            verdict += (" The price assumes less growth than the company is "
                        "currently delivering, which is where value can hide.")
        else:
            verdict += " That is roughly in line with recent delivery."
    v.notes.append(verdict)
    return v


def multiples(f: Fundamentals, price: float) -> Valuation:
    """Where the price sits on the usual ratios, and what each one implies."""
    v = Valuation(method="Multiples")
    rows = []

    checks = [
        ("pe", "Price to earnings", 25.0, "x"),
        ("forward_pe", "Forward P/E", 22.0, "x"),
        ("ps", "Price to sales", 3.0, "x"),
        ("pb", "Price to book", 4.0, "x"),
        ("ev_ebitda", "EV to EBITDA", 15.0, "x"),
        ("fcf_yield", "Free cash flow yield", 0.04, "pct"),
    ]
    for key, label, benchmark, unit in checks:
        value = f.get(key)
        if value is None:
            continue
        if key == "fcf_yield":
            cheap = value > benchmark
            comment = ("above" if cheap else "below") + " the 4% that usually marks fair value"
        else:
            cheap = value < benchmark
            comment = ("below" if cheap else "above") + " a typical %.0f%s" % (benchmark, unit)
        rows.append({"key": key, "label": label, "value": value, "unit": unit,
                     "benchmark": benchmark, "cheap": cheap, "comment": comment})

    if not rows:
        v.reason = ("No valuation ratios were reported. This is normal for "
                    "funds, ETFs and companies with no earnings.")
        return v

    v.usable = True
    v.workings = rows
    cheap_count = sum(1 for r in rows if r["cheap"])
    v.notes.append("%d of %d ratios look cheap against a typical benchmark."
                   % (cheap_count, len(rows)))
    v.notes.append(
        "Benchmarks here are broad market rules of thumb, not sector-specific. "
        "A software business at 30 times earnings and a utility at 30 times "
        "earnings are not the same statement.")

    # A crude fair value from earnings, clearly labelled as crude.
    pe, eps = f.get("pe"), None
    if pe and pe > 0 and price:
        eps = price / pe
        v.fair_value = eps * 20.0
        v.upside = (v.fair_value - price) / price
        v.notes.append(
            "Applying a flat 20 times earnings to trailing EPS of %.2f gives "
            "%.2f. That is a sanity check, not a valuation." % (eps, v.fair_value))
    return v


def combine(models: List[Valuation], price: float) -> Dict:
    """Pull the models together without pretending they agree.

    A blended average of disagreeing models hides the disagreement, which is
    usually the most informative thing in the whole exercise.
    """
    usable = [m for m in models if m.usable and m.fair_value]
    if not usable:
        return {"usable": False,
                "reason": "No model produced a value from the available data.",
                "models": models}

    values = [m.fair_value for m in usable]
    lo, hi = min(values), max(values)
    spread = (hi / lo) if lo > 0 else None

    agreement = "tight"
    if spread and spread > 2.5:
        agreement = "wide"
    elif spread and spread > 1.5:
        agreement = "loose"

    lines = []
    if agreement == "wide":
        lines.append(
            "The models disagree by more than a factor of two (%.2f to %.2f). "
            "That disagreement is the finding: this business is hard to value, "
            "and any single number would be false precision." % (lo, hi))
    elif agreement == "loose":
        lines.append("The models give a range of %.2f to %.2f. Treat the midpoint "
                     "as a rough centre, not a target." % (lo, hi))
    else:
        lines.append("The models broadly agree, between %.2f and %.2f." % (lo, hi))

    midpoint = sum(values) / len(values)
    upside = (midpoint - price) / price if price else None
    if upside is not None:
        if upside > 0.25:
            lines.append("That is roughly %.0f%% above the current %.2f, though "
                         "a gap that large usually means the market disagrees "
                         "with an assumption rather than that it is wrong."
                         % (upside * 100, price))
        elif upside < -0.20:
            lines.append("That is roughly %.0f%% below the current %.2f."
                         % (abs(upside) * 100, price))
        else:
            lines.append("That puts the current price of %.2f close to fair."
                         % price)

    return {"usable": True, "low": lo, "high": hi, "midpoint": midpoint,
            "upside": upside, "agreement": agreement, "models": models,
            "commentary": lines}
