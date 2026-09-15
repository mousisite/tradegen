"""Published, tested models of financial strength, distress and manipulation.

Nothing in this file was invented here. Each score is a model that was
published, tested against decades of filings, and is still used; the point of
implementing them rather than inventing a house scoring system is that somebody
has already checked whether these work, and the answer is on record.

  Piotroski F-Score   Nine binary tests of whether a company's fundamentals
                      improved. Piotroski (2000) found that, among cheap
                      stocks, the high-scoring ones went on to beat the
                      low-scoring ones by a wide margin.

  Altman Z-Score      A bankruptcy predictor from 1968, still accurate enough
                      to be worth knowing. Says how far a company is from
                      financial distress.

  Beneish M-Score     Eight ratios that tend to move together when earnings are
                      being manipulated. It flagged Enron before the collapse.
                      It is a smoke detector, not a verdict.

  Accruals ratio      Sloan (1996): earnings that are not backed by cash tend
                      to reverse. The larger the gap between profit and cash,
                      the less durable the profit.

Every input is read from an SEC filing, so every output can be traced back to a
document. Where an input is missing the score says so rather than guessing, and
a partial score is reported as partial rather than quietly rescaled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Test:
    """One yes-or-no question, and what the answer was based on."""
    name: str
    passed: Optional[bool]
    detail: str
    basis: str = ""

    @property
    def known(self) -> bool:
        return self.passed is not None


@dataclass
class Score:
    """A model's output, with the reasoning kept attached to it."""
    name: str
    value: Optional[float] = None
    scale: str = ""
    verdict: str = ""
    meaning: str = ""
    tests: List[Test] = field(default_factory=list)
    inputs: Dict[str, float] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    reliable: bool = True
    source: str = "Filed with the SEC"

    @property
    def usable(self) -> bool:
        return self.value is not None


def _series(table: Dict, key: str, years: List[int]) -> Dict[int, float]:
    """A concept as {year: value}, skipping years it was not reported for.

    Each row carries its own fiscal year, so the value is keyed on that rather
    than on its position in a list. Different concepts are reported for
    different spans, and lining them up by position would silently attribute a
    figure to the wrong year.
    """
    out = {}
    for row in table.get(key) or []:
        if not isinstance(row, dict):
            continue
        year, value = row.get("fy"), row.get("value")
        if year is None or value is None:
            continue
        try:
            out[int(year)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _derived(table: Dict, years: List[int], key: str) -> Dict[int, float]:
    """A concept, or the arithmetic that reconstructs it.

    Not every company tags every line. Total liabilities are frequently absent
    because the balance sheet reports assets and equity instead, and gross
    profit is often left implicit rather than tagged. Both follow from figures
    that are reported, so deriving them covers many more companies than
    insisting on the tag. Anything derived this way is still made only of
    filed numbers.
    """
    direct = _series(table, key, years)
    if key == "liabilities":
        assets = _series(table, "assets", years)
        equity = _series(table, "equity", years)
        for year in set(assets) & set(equity):
            if year not in direct:
                value = assets[year] - equity[year]
                if value > 0:
                    direct[year] = value
    elif key == "gross_profit":
        revenue = _series(table, "revenue", years)
        cost = _series(table, "cost_of_revenue", years)
        for year in set(revenue) & set(cost):
            if year not in direct:
                direct[year] = revenue[year] - cost[year]
    return direct


def _pair(series: Dict[int, float], year: int, prior: int):
    """This year and last year, or (None, None) if either is absent."""
    return series.get(year), series.get(prior)


def _two_years(table: Dict, years: List[int], needed: List[str]):
    """The two most recent years for which the needed concepts were reported.

    Taking the last two entries of the years list is wrong: that list is the
    union across every concept, so its final years may be ones where only a
    couple of items were filed. Choosing the latest pair that actually has the
    data is the difference between a score and a shrug.
    """
    have = [set(_derived(table, years, key)) for key in needed]
    if not have:
        return None, None
    complete = sorted(set.intersection(*have)) if len(have) > 1 else sorted(have[0])
    if len(complete) < 2:
        return None, None
    return complete[-1], complete[-2]


# Altman's coefficients were fitted on manufacturers. A bank's balance sheet is
# mostly other people's money by design, so the ratios that signal distress in
# a manufacturer are simply what a healthy bank looks like. Reporting a number
# anyway would be worse than reporting none.
_NOT_FOR_Z = ("financial services", "financial", "banks", "insurance",
              "capital markets", "credit services", "asset management")


def _safe(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


# ---------------------------------------------------------------------------
# Piotroski F-Score
# ---------------------------------------------------------------------------

def piotroski(history: Dict) -> Score:
    """Nine tests of whether the business got stronger or weaker last year.

    Deliberately binary. The model's value is that it refuses to weigh a small
    margin improvement against a large one: each test is a yes or a no, and the
    score is how many were yes.
    """
    out = Score(
        name="Piotroski F-Score", scale="0 to 9",
        meaning="How many of nine measures of financial strength improved. "
                "Published in 2000 and tested on twenty years of filings.")

    table = history.get("table") or {}
    years = history.get("years") or []
    if len(years) < 2:
        out.reliable = False
        out.missing.append("at least two years of filings")
        return out

    problem = _currency_problem(history)
    if problem:
        out.reliable = False
        out.verdict = problem
        return out

    year, prior = _two_years(table, years,
                             ["net_income", "assets", "operating_cashflow",
                              "revenue"])
    if year is None:
        out.reliable = False
        out.missing.append("two comparable years of core figures")
        out.verdict = "Not enough filed history to compare one year to the next."
        return out
    net_income = _series(table, "net_income", years)
    assets = _series(table, "assets", years)
    cfo = _series(table, "operating_cashflow", years)
    ltd = _series(table, "long_term_debt", years)
    curr_assets = _series(table, "current_assets", years)
    curr_liabs = _series(table, "current_liabilities", years)
    shares = _series(table, "shares_diluted", years)
    gross = _derived(table, years, "gross_profit")
    revenue = _series(table, "revenue", years)

    tests: List[Test] = []

    # --- profitability ----------------------------------------------------
    roa_now = _safe(net_income.get(year), assets.get(year))
    roa_before = _safe(net_income.get(prior), assets.get(prior))
    tests.append(Test(
        "Profitable", None if roa_now is None else roa_now > 0,
        "Return on assets %s" % ("%.1f%%" % (roa_now * 100) if roa_now is not None
                                 else "not reported"),
        "net income and total assets, %d" % year))

    cfo_ratio = _safe(cfo.get(year), assets.get(year))
    tests.append(Test(
        "Cash generative", None if cfo_ratio is None else cfo_ratio > 0,
        "Operating cash flow %s of assets"
        % ("%.1f%%" % (cfo_ratio * 100) if cfo_ratio is not None else "not reported"),
        "operating cash flow, %d" % year))

    tests.append(Test(
        "Returns improving",
        None if (roa_now is None or roa_before is None) else roa_now > roa_before,
        "Return on assets %s"
        % ("%.1f%% against %.1f%% last year" % (roa_now * 100, roa_before * 100)
           if (roa_now is not None and roa_before is not None) else "not comparable"),
        "%d against %d" % (year, prior)))

    # Cash exceeding profit is the single most informative of the nine: profit
    # that is not arriving as cash tends not to last.
    tests.append(Test(
        "Profit backed by cash",
        None if (cfo_ratio is None or roa_now is None) else cfo_ratio > roa_now,
        "Cash flow %s earnings"
        % ("exceeds" if (cfo_ratio is not None and roa_now is not None
                         and cfo_ratio > roa_now) else "does not exceed"
           if cfo_ratio is not None and roa_now is not None else "not comparable"),
        "operating cash flow against net income, %d" % year))

    # --- leverage and liquidity -------------------------------------------
    lev_now = _safe(ltd.get(year), assets.get(year))
    lev_before = _safe(ltd.get(prior), assets.get(prior))
    tests.append(Test(
        "Debt not rising",
        None if (lev_now is None or lev_before is None) else lev_now <= lev_before,
        "Long-term debt %s of assets"
        % ("%.1f%% against %.1f%%" % (lev_now * 100, lev_before * 100)
           if (lev_now is not None and lev_before is not None) else "not reported"),
        "long-term debt, %d against %d" % (year, prior)))

    cr_now = _safe(curr_assets.get(year), curr_liabs.get(year))
    cr_before = _safe(curr_assets.get(prior), curr_liabs.get(prior))
    tests.append(Test(
        "Liquidity improving",
        None if (cr_now is None or cr_before is None) else cr_now > cr_before,
        "Current ratio %s"
        % ("%.2f against %.2f" % (cr_now, cr_before)
           if (cr_now is not None and cr_before is not None) else "not reported"),
        "current assets and liabilities, %d against %d" % (year, prior)))

    sh_now, sh_before = _pair(shares, year, prior)
    tests.append(Test(
        "No dilution",
        None if (sh_now is None or sh_before is None) else sh_now <= sh_before * 1.02,
        "Share count %s"
        % ("%.1f%% %s" % (abs(sh_now / sh_before - 1) * 100,
                          "higher" if sh_now > sh_before else "lower")
           if (sh_now and sh_before) else "not reported"),
        "diluted shares, %d against %d" % (year, prior)))

    # --- operating efficiency ---------------------------------------------
    gm_now = _safe(gross.get(year), revenue.get(year))
    gm_before = _safe(gross.get(prior), revenue.get(prior))
    tests.append(Test(
        "Margins improving",
        None if (gm_now is None or gm_before is None) else gm_now > gm_before,
        "Gross margin %s"
        % ("%.1f%% against %.1f%%" % (gm_now * 100, gm_before * 100)
           if (gm_now is not None and gm_before is not None) else "not reported"),
        "gross profit over revenue, %d against %d" % (year, prior)))

    turn_now = _safe(revenue.get(year), assets.get(year))
    turn_before = _safe(revenue.get(prior), assets.get(prior))
    tests.append(Test(
        "Assets working harder",
        None if (turn_now is None or turn_before is None) else turn_now > turn_before,
        "Revenue per dollar of assets %s"
        % ("%.2f against %.2f" % (turn_now, turn_before)
           if (turn_now is not None and turn_before is not None) else "not reported"),
        "revenue over assets, %d against %d" % (year, prior)))

    out.tests = tests
    known = [t for t in tests if t.known]
    out.missing = [t.name for t in tests if not t.known]
    if len(known) < 6:
        out.reliable = False
        out.verdict = ("Too many of the nine could not be measured from what "
                       "was filed.")
        return out

    passed = sum(1 for t in known if t.passed)
    out.value = float(passed)
    out.reliable = len(known) == 9
    scored = "%d of %d" % (passed, len(known))

    if passed >= 8:
        out.verdict = "Strong and improving on almost every measure (%s)." % scored
    elif passed >= 6:
        out.verdict = "Solid. More improved than deteriorated (%s)." % scored
    elif passed >= 4:
        out.verdict = "Mixed. As much got worse as got better (%s)." % scored
    else:
        out.verdict = ("Weak. Most measures of financial strength went the "
                       "wrong way (%s)." % scored)
    if not out.reliable:
        out.verdict += (" Scored out of %d rather than 9, because %s was not "
                        "reported." % (len(known), " and ".join(out.missing)))
    return out


# ---------------------------------------------------------------------------
# Altman Z-Score
# ---------------------------------------------------------------------------

def _currency_problem(history: Dict, needs_market_value: bool = False) -> str:
    """Why these filings cannot be used, if they cannot.

    A foreign private issuer files in its home currency. Mixing those figures
    with each other, or with a market value quoted in dollars, produces a
    number that looks entirely reasonable and is wrong by whatever the exchange
    rate happens to be. Refusing is the only honest option, because there is no
    exchange rate in the filing to correct it with.
    """
    if history.get("mixed_currency"):
        return ("These filings report in more than one currency (%s), so the "
                "figures cannot be compared with each other. Any ratio across "
                "them would be wrong by an exchange rate that the filing does "
                "not state."
                % ", ".join(history.get("currencies") or []))
    currency = history.get("currency") or ""
    if needs_market_value and currency and currency != "USD":
        return ("The accounts are filed in %s while the market value is quoted "
                "in US dollars. Dividing one by the other would be wrong by the "
                "exchange rate, so this is not calculated." % currency)
    return ""


def altman_z(history: Dict, market_cap: Optional[float] = None,
             sector: str = "") -> Score:
    """How far this company is from financial distress.

    Altman (1968), the public-company formula. Still the standard first check
    for whether a balance sheet can survive a bad year.
    """
    out = Score(
        name="Altman Z-Score", scale="below 1.8 distressed, above 3.0 safe",
        meaning="A bankruptcy predictor built in 1968 and still in use. "
                "Weighs working capital, accumulated profit, operating "
                "earnings, market value and sales against total assets.")

    table = history.get("table") or {}
    years = history.get("years") or []
    if not years:
        out.reliable = False
        out.missing.append("filed financials")
        return out

    problem = _currency_problem(history, needs_market_value=True)
    if problem:
        out.reliable = False
        out.verdict = problem
        return out

    if sector and any(word in sector.lower() for word in _NOT_FOR_Z):
        out.reliable = False
        out.verdict = (
            "Not applicable to a %s company. Altman's coefficients were fitted "
            "on manufacturers, and a balance sheet that is mostly other "
            "people's money by design scores as distressed however healthy it "
            "is. A number here would mislead rather than inform." % sector.lower())
        return out

    year, _prior = _two_years(table, years,
                              ["assets", "current_assets", "current_liabilities",
                               "retained_earnings", "operating_income", "revenue"])
    if year is None:
        year = int(years[-1])
    assets = _series(table, "assets", years).get(year)
    curr_assets = _series(table, "current_assets", years).get(year)
    curr_liabs = _series(table, "current_liabilities", years).get(year)
    retained = _series(table, "retained_earnings", years).get(year)
    ebit = _series(table, "operating_income", years).get(year)
    liabilities = _derived(table, years, "liabilities").get(year)
    revenue = _series(table, "revenue", years).get(year)

    needed = {
        "total assets": assets, "current assets": curr_assets,
        "current liabilities": curr_liabs, "retained earnings": retained,
        "operating income": ebit, "total liabilities": liabilities,
        "revenue": revenue, "market capitalisation": market_cap,
    }
    out.missing = [label for label, value in needed.items() if not value]
    if out.missing or not assets:
        out.reliable = False
        out.verdict = ("Cannot be calculated: %s %s not reported."
                       % (", ".join(out.missing),
                          "was" if len(out.missing) == 1 else "were"))
        return out

    working_capital = curr_assets - curr_liabs
    a = working_capital / assets
    b = retained / assets
    c = ebit / assets
    d = market_cap / liabilities
    e = revenue / assets

    out.inputs = {
        "working capital / assets": a,
        "retained earnings / assets": b,
        "operating income / assets": c,
        "market value / liabilities": d,
        "revenue / assets": e,
    }
    z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
    out.value = z

    if z > 8:
        out.verdict = (
            "%.0f, far above the 3.0 safety line. The model was built to spot "
            "distress rather than to rank healthy companies, so treat this as "
            "\"nowhere near trouble\" rather than as a score worth comparing "
            "against another company's." % z)
    elif z > 2.99:
        out.verdict = ("%.2f. Comfortably out of distress territory." % z)
    elif z >= 1.81:
        out.verdict = ("%.2f. The grey zone: not distressed, not obviously "
                       "safe either." % z)
    else:
        out.verdict = ("%.2f. In the range the model associates with financial "
                       "distress. Worth understanding why before anything "
                       "else." % z)
    return out


# ---------------------------------------------------------------------------
# Beneish M-Score
# ---------------------------------------------------------------------------

def beneish_m(history: Dict) -> Score:
    """Eight ratios that tend to move together when earnings are massaged.

    A smoke detector rather than a verdict. Plenty of honest companies trip it,
    usually because they grew quickly, and the right response is to read the
    filing rather than to conclude anything.
    """
    out = Score(
        name="Beneish M-Score", scale="above -1.78 is a flag",
        meaning="Eight ratios that moved together in companies later found to "
                "have manipulated earnings. A reason to read more closely, "
                "not a conclusion.")

    table = history.get("table") or {}
    years = history.get("years") or []
    if len(years) < 2:
        out.reliable = False
        out.missing.append("two consecutive years")
        return out

    problem = _currency_problem(history)
    if problem:
        out.reliable = False
        out.verdict = problem
        return out

    year, prior = _two_years(table, years,
                             ["revenue", "assets", "receivables",
                              "current_assets", "current_liabilities"])
    if year is None:
        out.reliable = False
        out.missing.append("two comparable years")
        out.verdict = "Not enough filed history to compare one year to the next."
        return out

    def two(key):
        series = _series(table, key, years)
        return series.get(year), series.get(prior)

    revenue, revenue0 = two("revenue")
    receivables, receivables0 = two("receivables")
    _gross_series = _derived(table, years, "gross_profit")
    gross, gross0 = _gross_series.get(year), _gross_series.get(prior)
    curr_assets, curr_assets0 = two("current_assets")
    ppe, ppe0 = two("ppe_net")
    assets, assets0 = two("assets")
    depreciation, depreciation0 = two("depreciation")
    sga, sga0 = two("sga_expense")
    net_income, _ = two("net_income")
    cfo, _ = two("operating_cashflow")
    curr_liabs, curr_liabs0 = two("current_liabilities")
    ltd, ltd0 = two("long_term_debt")

    required = {
        "revenue": (revenue, revenue0), "receivables": (receivables, receivables0),
        "gross profit": (gross, gross0), "current assets": (curr_assets, curr_assets0),
        "property and equipment": (ppe, ppe0), "total assets": (assets, assets0),
        "depreciation": (depreciation, depreciation0),
        "selling and admin expense": (sga, sga0),
        "current liabilities": (curr_liabs, curr_liabs0),
    }
    out.missing = [label for label, pair in required.items()
                   if pair[0] is None or pair[1] is None]
    if out.missing or net_income is None or cfo is None:
        out.reliable = False
        out.verdict = ("Cannot be calculated: %s not reported for both years."
                       % ", ".join(out.missing or ["net income or cash flow"]))
        return out

    def ratio(a, b, default=1.0):
        value = _safe(a, b)
        return default if value is None else value

    dsri = ratio(ratio(receivables, revenue), ratio(receivables0, revenue0))
    gm_now, gm_before = ratio(gross, revenue), ratio(gross0, revenue0)
    gmi = ratio(gm_before, gm_now)
    soft_now = 1 - ratio((curr_assets + ppe), assets)
    soft_before = 1 - ratio((curr_assets0 + ppe0), assets0)
    aqi = ratio(soft_now, soft_before)
    sgi = ratio(revenue, revenue0)
    dep_now = ratio(depreciation, (depreciation + ppe))
    dep_before = ratio(depreciation0, (depreciation0 + ppe0))
    depi = ratio(dep_before, dep_now)
    sgai = ratio(ratio(sga, revenue), ratio(sga0, revenue0))
    tata = ratio((net_income - cfo), assets, default=0.0)
    lev_now = ratio((curr_liabs + (ltd or 0)), assets)
    lev_before = ratio((curr_liabs0 + (ltd0 or 0)), assets0)
    lvgi = ratio(lev_now, lev_before)

    out.inputs = {
        "receivables against sales": dsri, "gross margin": gmi,
        "asset quality": aqi, "sales growth": sgi, "depreciation rate": depi,
        "overheads against sales": sgai, "accruals": tata, "leverage": lvgi,
    }

    m = (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
         + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    out.value = m

    if m > -1.78:
        worst = sorted(out.inputs.items(), key=lambda kv: -abs(kv[1] - 1))[:2]
        out.verdict = (
            "%.2f, above the -1.78 line. The ratios pulling it up are %s. This "
            "happens to fast-growing companies as readily as to dishonest "
            "ones, so read the filing rather than drawing a conclusion."
            % (m, " and ".join(name for name, _ in worst)))
    else:
        out.verdict = ("%.2f, below the -1.78 line. Nothing in the pattern of "
                       "these eight ratios looks unusual." % m)
    return out


# ---------------------------------------------------------------------------
# Accruals
# ---------------------------------------------------------------------------

def accruals(history: Dict) -> Score:
    """How much of the profit arrived as cash.

    Sloan (1996) showed that the part of earnings not backed by cash tends to
    reverse, and that the market is slow to notice. It is the simplest useful
    check on whether a profit is real.
    """
    out = Score(
        name="Earnings quality", scale="lower is better",
        meaning="The gap between reported profit and cash actually generated. "
                "Profit that never becomes cash tends not to repeat.")

    table = history.get("table") or {}
    years = history.get("years") or []
    if len(years) < 2:
        out.reliable = False
        out.missing.append("two years of filings")
        return out

    problem = _currency_problem(history)
    if problem:
        out.reliable = False
        out.verdict = problem
        return out

    year, prior = _two_years(table, years,
                             ["net_income", "operating_cashflow", "assets"])
    if year is None:
        out.reliable = False
        out.missing.append("two comparable years")
        out.verdict = "Not enough filed history to compare one year to the next."
        return out
    net_income = _series(table, "net_income", years).get(year)
    cfo = _series(table, "operating_cashflow", years).get(year)
    assets = _series(table, "assets", years)
    average_assets = None
    if assets.get(year) and assets.get(prior):
        average_assets = (assets[year] + assets[prior]) / 2.0

    if net_income is None or cfo is None or not average_assets:
        out.reliable = False
        out.missing.append("net income, cash flow or total assets")
        out.verdict = "Cannot be calculated from what was filed."
        return out

    ratio = (net_income - cfo) / average_assets
    out.value = ratio
    out.inputs = {"net income": net_income, "operating cash flow": cfo,
                  "average assets": average_assets}

    covered = _safe(cfo, net_income)
    cash_note = ("Cash flow covered %.0f%% of reported profit."
                 % (covered * 100)) if covered and net_income > 0 else ""

    if ratio <= 0:
        out.verdict = ("Cash exceeded reported profit. That is the good "
                       "direction. %s" % cash_note).strip()
    elif ratio < 0.05:
        out.verdict = ("Profit and cash are closely aligned. %s" % cash_note).strip()
    elif ratio < 0.10:
        out.verdict = ("Profit ran somewhat ahead of cash. Worth watching "
                       "whether it persists. %s" % cash_note).strip()
    else:
        out.verdict = ("Profit ran well ahead of cash, by %.0f%% of assets. "
                       "Earnings like this have historically tended to "
                       "reverse. %s" % (ratio * 100, cash_note)).strip()
    return out


# ---------------------------------------------------------------------------
# Everything, in one call
# ---------------------------------------------------------------------------

def assess(history: Dict, market_cap: Optional[float] = None,
           sector: str = "") -> Dict:
    """Run every model and summarise what they agree on."""
    if not history or not history.get("available"):
        return {"available": False,
                "reason": (history or {}).get("reason")
                or "No SEC filings, so none of these can be measured."}

    scores = {
        "piotroski": piotroski(history),
        "altman": altman_z(history, market_cap, sector),
        "beneish": beneish_m(history),
        "accruals": accruals(history),
    }

    concerns: List[str] = []
    strengths: List[str] = []

    f = scores["piotroski"]
    if f.usable:
        if f.value >= 7:
            strengths.append("fundamentals improved on %d of the nine measures"
                             % int(f.value))
        elif f.value <= 3:
            concerns.append("fundamentals improved on only %d of nine measures"
                            % int(f.value))

    z = scores["altman"]
    if z.usable:
        if z.value < 1.81:
            concerns.append("the balance sheet scores in the distress range")
        elif z.value > 2.99:
            strengths.append("the balance sheet is comfortably out of distress")

    m = scores["beneish"]
    if m.usable and m.value > -1.78:
        concerns.append("the earnings-manipulation screen is above its "
                        "threshold, which warrants reading the filing")

    a = scores["accruals"]
    if a.usable:
        if a.value > 0.10:
            concerns.append("reported profit is running well ahead of cash")
        elif a.value <= 0:
            strengths.append("cash generation exceeds reported profit")

    if concerns and strengths:
        headline = ("Mixed: %s, but %s." % (strengths[0], concerns[0]))
    elif concerns:
        headline = ("Worth care: %s." % "; ".join(concerns))
    elif strengths:
        headline = ("Nothing in the filed numbers looks wrong: %s."
                    % "; ".join(strengths))
    else:
        headline = ("The models ran but none of them had anything strong to "
                    "say either way.")

    return {
        "available": True,
        "scores": scores,
        "concerns": concerns,
        "strengths": strengths,
        "headline": headline,
        "note": ("These are published models applied to filed figures, not "
                 "opinions. They describe the accounts, not the price, and a "
                 "good score is not a reason to buy."),
    }
