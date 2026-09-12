"""Company fundamentals: what the business actually earns and owns.

Two sources, deliberately:

* **Yahoo** supplies current ratios, analyst targets and the market's view.
  Fast, broad, and already normalised.
* **SEC XBRL** supplies the numbers as filed. Slower, US-only, but it is the
  primary record rather than somebody's copy of it, and every figure can be
  traced to a filing.

Where they disagree the SEC figure is the one to trust, and the report says so.
Nothing here guesses: a field that is missing stays missing rather than being
filled with a plausible-looking zero, because a zero silently poisons every
ratio computed from it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import sec as sec_mod
from . import yahoo

_MODULES = ("quoteType,price,assetProfile,financialData,defaultKeyStatistics,summaryDetail,"
            "incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory,"
            "earnings,earningsTrend,recommendationTrend,calendarEvents,"
            "majorHoldersBreakdown")


@dataclass
class Metric:
    """One fundamental figure, with where it came from."""
    key: str
    label: str
    value: Optional[float]
    unit: str = ""            # "ratio" | "pct" | "usd" | "x" | ""
    source: str = ""
    note: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def display(self) -> str:
        if self.value is None:
            return "—"
        v = self.value
        if self.unit == "pct":
            return "%.1f%%" % (v * 100)
        if self.unit == "usd":
            for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
                if abs(v) >= cut:
                    return "%.2f%s" % (v / cut, suffix)
            return "%.2f" % v
        if self.unit == "x":
            return "%.1fx" % v
        return "%.2f" % v


@dataclass
class Fundamentals:
    """Everything known about a company's financial position."""
    symbol: str
    name: str = ""
    sector: str = ""
    industry: str = ""
    country: str = ""
    employees: Optional[int] = None
    summary: str = ""

    metrics: Dict[str, Metric] = field(default_factory=dict)
    income: List[Dict] = field(default_factory=list)      # newest first
    balance: List[Dict] = field(default_factory=list)
    cashflow: List[Dict] = field(default_factory=list)
    analysts: Dict = field(default_factory=dict)
    calendar: Dict = field(default_factory=dict)
    sec_facts: Optional[sec_mod.CompanyFacts] = None
    sources: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def get(self, key: str) -> Optional[float]:
        m = self.metrics.get(key)
        return m.value if m else None

    def show(self, key: str) -> str:
        m = self.metrics.get(key)
        return m.display() if m else "—"


def _put(store: Dict[str, Metric], key: str, label: str, value,
         unit: str = "", source: str = "Yahoo Finance", note: str = "") -> None:
    try:
        value = None if value is None else float(value)
    except (TypeError, ValueError):
        value = None
    store[key] = Metric(key, label, value, unit, source, note)


def load(symbol: str, with_sec: bool = True) -> Fundamentals:
    """Fetch and normalise everything known about one company."""
    out = Fundamentals(symbol=symbol.upper())

    try:
        mods = yahoo.quote_summary(symbol, _MODULES)
    except yahoo.YahooError as exc:
        out.notes.append("Yahoo fundamentals unavailable: %s" % exc)
        mods = {}

    profile = mods.get("assetProfile") or {}
    fin = mods.get("financialData") or {}
    stats = mods.get("defaultKeyStatistics") or {}
    detail = mods.get("summaryDetail") or {}
    qtype = mods.get("quoteType") or {}
    pricing = mods.get("price") or {}

    # assetProfile carries no name, so take it from whichever module has one and
    # fall back to the ticker rather than showing an empty heading.
    out.name = (qtype.get("longName") or pricing.get("longName")
                or qtype.get("shortName") or pricing.get("shortName")
                or out.symbol)
    out.sector = profile.get("sector") or ""
    out.industry = profile.get("industry") or ""
    out.country = profile.get("country") or ""
    out.employees = profile.get("fullTimeEmployees")
    out.summary = (profile.get("longBusinessSummary") or "").strip()

    g = yahoo.fmt
    m = out.metrics

    # Size and price
    _put(m, "market_cap", "Market capitalisation", g(detail, "marketCap"), "usd")
    _put(m, "enterprise_value", "Enterprise value", g(stats, "enterpriseValue"), "usd")
    _put(m, "shares_out", "Shares outstanding", g(stats, "sharesOutstanding"), "usd")
    _put(m, "price", "Current price", g(fin, "currentPrice"))

    # Valuation
    _put(m, "pe", "Price to earnings", g(detail, "trailingPE"), "x")
    _put(m, "forward_pe", "Forward P/E", g(stats, "forwardPE"), "x")
    _put(m, "peg", "PEG ratio", g(stats, "pegRatio"), "x")
    _put(m, "pb", "Price to book", g(stats, "priceToBook"), "x")
    _put(m, "ps", "Price to sales", g(stats, "priceToSalesTrailing12Months")
         or g(detail, "priceToSalesTrailing12Months"), "x")
    _put(m, "ev_ebitda", "EV to EBITDA", g(stats, "enterpriseToEbitda"), "x")
    _put(m, "ev_revenue", "EV to revenue", g(stats, "enterpriseToRevenue"), "x")

    # Profitability
    _put(m, "gross_margin", "Gross margin", g(fin, "grossMargins"), "pct")
    _put(m, "operating_margin", "Operating margin", g(fin, "operatingMargins"), "pct")
    _put(m, "net_margin", "Net margin", g(fin, "profitMargins"), "pct")
    _put(m, "roe", "Return on equity", g(fin, "returnOnEquity"), "pct")
    _put(m, "roa", "Return on assets", g(fin, "returnOnAssets"), "pct")

    # Growth
    _put(m, "revenue_growth", "Revenue growth", g(fin, "revenueGrowth"), "pct")
    _put(m, "earnings_growth", "Earnings growth", g(fin, "earningsGrowth"), "pct")

    # Balance sheet and cash
    _put(m, "revenue", "Revenue (trailing)", g(fin, "totalRevenue"), "usd")
    _put(m, "ebitda", "EBITDA", g(fin, "ebitda"), "usd")
    _put(m, "free_cashflow", "Free cash flow", g(fin, "freeCashflow"), "usd")
    _put(m, "operating_cashflow", "Operating cash flow", g(fin, "operatingCashflow"), "usd")
    _put(m, "total_cash", "Cash", g(fin, "totalCash"), "usd")
    _put(m, "total_debt", "Total debt", g(fin, "totalDebt"), "usd")
    _put(m, "debt_to_equity", "Debt to equity", g(fin, "debtToEquity"))
    _put(m, "current_ratio", "Current ratio", g(fin, "currentRatio"))
    _put(m, "quick_ratio", "Quick ratio", g(fin, "quickRatio"))

    # Risk and income
    _put(m, "beta", "Beta", g(stats, "beta") or g(detail, "beta"))
    _put(m, "dividend_yield", "Dividend yield", g(detail, "dividendYield"), "pct")
    _put(m, "payout_ratio", "Payout ratio", g(detail, "payoutRatio"), "pct")
    _put(m, "short_pct_float", "Short interest of float",
         g(stats, "shortPercentOfFloat"), "pct")

    # Derived: free cash flow yield is one of the more honest valuation reads,
    # because it is harder to flatter with accounting choices than earnings.
    fcf, cap = out.get("free_cashflow"), out.get("market_cap")
    if fcf and cap and cap > 0:
        _put(m, "fcf_yield", "Free cash flow yield", fcf / cap, "pct",
             "derived from Yahoo", "free cash flow divided by market cap")

    out.income = _statements(mods, "incomeStatementHistory", "incomeStatementHistory")
    out.balance = _statements(mods, "balanceSheetHistory", "balanceSheetStatements")
    out.cashflow = _statements(mods, "cashflowStatementHistory", "cashflowStatements")

    trend = mods.get("recommendationTrend") or {}
    rec_rows = trend.get("trend") or []
    out.analysts = {
        "recommendation": fin.get("recommendationKey"),
        "target_mean": g(fin, "targetMeanPrice"),
        "target_high": g(fin, "targetHighPrice"),
        "target_low": g(fin, "targetLowPrice"),
        "analyst_count": g(fin, "numberOfAnalystOpinions"),
        "trend": rec_rows[0] if rec_rows else {},
    }

    cal = mods.get("calendarEvents") or {}
    earnings = cal.get("earnings") or {}
    dates = earnings.get("earningsDate") or []
    out.calendar = {
        "next_earnings": g({"d": dates[0]}, "d") if dates else None,
        "ex_dividend": g(cal, "exDividendDate"),
        "dividend_date": g(cal, "dividendDate"),
    }

    if mods:
        out.sources.append({
            "name": "Yahoo Finance fundamentals",
            "url": "https://finance.yahoo.com/quote/%s/key-statistics" % out.symbol,
            "detail": "ratios, analyst targets, statement history",
        })

    if with_sec:
        try:
            facts = sec_mod.company_facts(out.symbol)
            out.sec_facts = facts
            if facts:
                _reconcile(out, facts)
                out.sources.append({
                    "name": "SEC EDGAR XBRL company facts",
                    "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % facts.cik,
                    "detail": "figures as filed, %s" % facts.entity,
                })
        except Exception as exc:
            out.notes.append("SEC data unavailable: %s" % str(exc)[:120])

    missing = [k for k in ("revenue", "net_margin", "roe", "pe") if out.get(k) is None]
    if missing:
        out.notes.append(
            "Not reported for this instrument: %s. Funds, ETFs and some foreign "
            "listings do not publish company fundamentals." % ", ".join(missing))
    return out


def _statements(mods: Dict, module: str, key: str) -> List[Dict]:
    """Flatten a Yahoo statement history into plain dictionaries, newest first."""
    rows = (mods.get(module) or {}).get(key) or []
    out = []
    for row in rows:
        flat = {}
        for name, value in row.items():
            if name == "maxAge":
                continue
            flat[name] = yahoo.fmt(row, name)
        if flat:
            out.append(flat)
    return out


def _reconcile(out: Fundamentals, facts: "sec_mod.CompanyFacts") -> None:
    """Cross-check the market's numbers against the filed ones.

    A gap here is worth seeing. Yahoo's trailing figures roll four quarters and
    can drift from the last annual filing, and occasionally a data provider
    simply has it wrong. Naming the discrepancy is more useful than silently
    picking a winner.
    """
    filed_revenue = facts.latest("revenue")
    filed_income = facts.latest("net_income")

    if filed_revenue:
        _put(out.metrics, "sec_revenue", "Revenue (last annual report)",
             filed_revenue["value"], "usd", "SEC filing",
             "fiscal %s, filed %s" % (filed_revenue.get("fy"), filed_revenue.get("filed")))
        market = out.get("revenue")
        if market and filed_revenue["value"]:
            gap = abs(market - filed_revenue["value"]) / filed_revenue["value"]
            if gap > 0.25:
                out.notes.append(
                    "Trailing revenue differs from the last annual filing by "
                    "%.0f%%. That is normal when the business is growing fast, "
                    "but check the filing before relying on the ratio." % (gap * 100))

    if filed_income:
        _put(out.metrics, "sec_net_income", "Net income (last annual report)",
             filed_income["value"], "usd", "SEC filing",
             "fiscal %s" % filed_income.get("fy"))

    for concept, key, label in (("assets", "sec_assets", "Total assets"),
                                ("liabilities", "sec_liabilities", "Total liabilities"),
                                ("equity", "sec_equity", "Shareholder equity"),
                                ("eps_diluted", "sec_eps", "Diluted EPS (filed)")):
        row = facts.latest(concept)
        if row:
            _put(out.metrics, key, label, row["value"],
                 "" if concept == "eps_diluted" else "usd", "SEC filing",
                 "fiscal %s" % row.get("fy"))


def quality_score(f: Fundamentals) -> Dict:
    """Score business quality on evidence, and say what is missing.

    Deliberately refuses to produce a number when too little is known. A score
    computed from two of six inputs looks just as confident as one computed
    from all six, which is how these summaries mislead.
    """
    checks = [
        ("Profitable", f.get("net_margin"), lambda v: v > 0, "net margin above zero"),
        ("Strong margins", f.get("operating_margin"), lambda v: v > 0.15,
         "operating margin above 15%"),
        ("Good returns", f.get("roe"), lambda v: v > 0.15, "return on equity above 15%"),
        ("Growing", f.get("revenue_growth"), lambda v: v > 0.05,
         "revenue growing more than 5%"),
        ("Cash generative", f.get("free_cashflow"), lambda v: v > 0,
         "positive free cash flow"),
        ("Manageable debt", f.get("debt_to_equity"), lambda v: v < 150,
         "debt to equity below 150%"),
        ("Liquid", f.get("current_ratio"), lambda v: v > 1.2,
         "current ratio above 1.2"),
    ]
    passed, failed, unknown = [], [], []
    for label, value, test, why in checks:
        if value is None:
            unknown.append({"label": label, "why": why})
        elif test(value):
            passed.append({"label": label, "why": why})
        else:
            failed.append({"label": label, "why": why})

    known = len(passed) + len(failed)
    score = (len(passed) / known) if known else None
    return {
        "score": score,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "known": known,
        "total": len(checks),
        "reliable": known >= 5,
        "verdict": _quality_words(score, known),
    }


def _quality_words(score: Optional[float], known: int) -> str:
    if score is None or known < 5:
        return ("Too little was reported to judge business quality. This is "
                "normal for funds, ETFs and many foreign listings.")
    if score >= 0.85:
        return "Strong on almost every measure that could be checked."
    if score >= 0.6:
        return "Solid, with some weak spots worth reading before buying."
    if score >= 0.35:
        return "Mixed. Several quality measures fail."
    return "Weak on most measures. Understand why before going near it."
