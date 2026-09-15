"""SEC EDGAR: filings, filed financials, and full-text search.

Free, keyless, and authoritative. Unlike a data vendor's copy, everything here
traces to a document a company signed and filed, so every figure can carry a
link to its source.

Three endpoints do the work:

* ``company_tickers.json`` maps a ticker to a CIK, which everything else needs
* ``companyfacts`` returns every XBRL concept a company has ever reported
* ``submissions`` lists their filings with dates and document names

EDGAR asks that automated clients identify themselves with a real contact in
the User-Agent and stay under roughly ten requests a second. Both are honoured
here; the ticker map is cached on disk because it is 800 KB and rarely changes.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

_UA = {"User-Agent": "StockBot/1.0 (personal research tool; mousihleb@gmail.com)",
       "Accept-Encoding": "gzip, deflate",
       "Accept": "application/json,text/html,*/*"}

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json"
_SUBS_URL = "https://data.sec.gov/submissions/CIK%010d.json"
_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"
_FILING_PAGE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%010d&type=%s"

_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_MIN_GAP = 0.12          # EDGAR asks for under ~10 requests a second

_TICKER_CACHE: Optional[Dict[str, int]] = None
_FACTS_CACHE: Dict[int, "CompanyFacts"] = {}
_CACHE_LOCK = threading.Lock()

# The XBRL tag for a given idea changes between companies and over time, so
# each concept lists the tags to try in order of preference.
_CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss",
                   "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "operating_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "eps_basic": ["EarningsPerShareBasic"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities",
                           "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "rd_expense": ["ResearchAndDevelopmentExpense"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],

    # Added for the measured quality, distress and manipulation scores. Each is
    # a documented input to a published, tested model rather than something
    # invented here, and every one of them is read from a filing.
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "receivables": ["AccountsReceivableNetCurrent",
                    "ReceivablesNetCurrent",
                    "AccountsReceivableNet"],
    "inventory": ["InventoryNet"],
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    "depreciation": ["DepreciationDepletionAndAmortization",
                     "DepreciationAmortizationAndAccretionNet",
                     "Depreciation"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense",
                    "GeneralAndAdministrativeExpense"],
    "total_debt": ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold",
                        "CostOfGoodsSold", "CostOfServices"],
    "assets_and_equity": ["LiabilitiesAndStockholdersEquity"],
}

# Filings worth surfacing, with what each one actually tells you.
FORM_MEANING = {
    "10-K": "Annual report. The full picture: business, risks, audited accounts.",
    "10-Q": "Quarterly report. Unaudited, but the most recent numbers.",
    "8-K": "Something happened that investors should know about now.",
    "DEF 14A": "Proxy statement. Pay, board, and what shareholders vote on.",
    "S-1": "Registration for new shares. Often an IPO or a raise.",
    "424B5": "Pricing of a share offering. Usually dilution.",
    "SC 13D": "Someone took an activist stake above 5%.",
    "SC 13G": "Someone took a passive stake above 5%.",
    "4": "An insider bought or sold.",
    "3": "Someone became an insider.",
    "144": "An insider gave notice of intent to sell.",
    "20-F": "Annual report from a foreign private issuer.",
    "6-K": "Interim report from a foreign private issuer.",
}

MATERIAL_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A", "S-1", "424B5",
                  "SC 13D", "SC 13G", "20-F", "6-K")


class SECError(RuntimeError):
    """Raised when EDGAR cannot be reached or has nothing for this company."""


def _year_of(date_str: str) -> Optional[int]:
    """Calendar year a reporting period ended in."""
    try:
        return int(str(date_str)[:4])
    except (TypeError, ValueError):
        return None


def _span_days(start: Optional[str], end: str) -> Optional[int]:
    """Length of a reporting period in days, or None for point-in-time facts."""
    if not start or not end:
        return None
    try:
        from datetime import date
        a = date(int(start[:4]), int(start[5:7]), int(start[8:10]))
        b = date(int(end[:4]), int(end[5:7]), int(end[8:10]))
        return (b - a).days
    except (TypeError, ValueError, IndexError):
        return None


def _throttle() -> None:
    with _RATE_LOCK:
        gap = time.time() - _LAST_CALL[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        _LAST_CALL[0] = time.time()


def _get(url: str, params: Optional[Dict] = None, tries: int = 3):
    last = None
    for attempt in range(tries):
        _throttle()
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=30)
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(0.6 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return r.text
        if r.status_code == 404:
            return None
        last = "HTTP %s" % r.status_code
        time.sleep(0.8 * (attempt + 1))
    raise SECError("EDGAR request failed: %s" % last)


def _cache_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "data", "cache")
    os.makedirs(path, exist_ok=True)
    return path


def ticker_map(max_age_days: int = 14) -> Dict[str, int]:
    """Ticker to CIK for every SEC registrant, cached on disk.

    The file is about 800 KB and changes rarely, so re-downloading it on every
    lookup would be wasteful and rude to a free public service.
    """
    global _TICKER_CACHE
    if _TICKER_CACHE is not None:
        return _TICKER_CACHE

    path = os.path.join(_cache_dir(), "sec_tickers.json")
    fresh = (os.path.exists(path) and
             (time.time() - os.path.getmtime(path)) < max_age_days * 86400)
    if fresh:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _TICKER_CACHE = json.load(fh)
                return _TICKER_CACHE
        except (OSError, json.JSONDecodeError):
            pass

    data = _get(_TICKERS_URL)
    if not isinstance(data, dict):
        raise SECError("Could not download the SEC ticker list.")
    mapping = {}
    for row in data.values():
        ticker = (row.get("ticker") or "").upper()
        if ticker:
            mapping[ticker] = int(row.get("cik_str"))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh)
    except OSError:
        pass
    _TICKER_CACHE = mapping
    return mapping


# Suffixes that mark an instrument as something other than a US-listed share.
# Matching on a stripped base would be actively harmful: "BTC-USD" would strip
# to "BTC", which is a real registrant, and Bitcoin would be shown another
# company's financial statements.
_NON_EQUITY_SUFFIXES = ("-USD", "-EUR", "-GBP", "-USDT", "-BTC", "-ETH")


def cik_for(symbol: str) -> Optional[int]:
    """CIK for a ticker, or None when it is not a US SEC registrant.

    Matches the ticker as written. The only normalisation is the dot-to-dash
    convention for share classes, where Yahoo writes BRK.B and the SEC writes
    BRK-B for the same security.
    """
    raw = (symbol or "").upper().strip()
    if not raw:
        return None
    # Crypto, forex and index symbols never file with the SEC.
    if any(raw.endswith(s) for s in _NON_EQUITY_SUFFIXES) or raw.startswith("^") \
            or "=" in raw:
        return None
    try:
        table = ticker_map()
    except SECError:
        return None
    return table.get(raw) or table.get(raw.replace(".", "-"))


@dataclass
class Fact:
    """One reported figure, with the filing it came from."""
    concept: str
    tag: str
    value: float
    unit: str
    fy: Optional[int]
    fp: str
    form: str
    filed: str
    end: str
    accession: str = ""

    def url(self, cik: int) -> str:
        if not self.accession:
            return "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%010d" % cik
        plain = self.accession.replace("-", "")
        return "https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm" % (
            cik, plain, self.accession)


@dataclass
class CompanyFacts:
    """Everything a company has reported in XBRL, indexed by concept."""
    cik: int
    entity: str
    annual: Dict[str, List[Fact]] = field(default_factory=dict)
    quarterly: Dict[str, List[Fact]] = field(default_factory=dict)

    def latest(self, concept: str, annual_only: bool = True) -> Optional[Dict]:
        """Most recent reported value for a concept."""
        rows = self.annual.get(concept) or ([] if annual_only
                                            else self.quarterly.get(concept) or [])
        if not rows and not annual_only:
            rows = self.quarterly.get(concept) or []
        if not rows:
            return None
        f = rows[-1]
        return {"value": f.value, "unit": f.unit, "fy": f.fy, "form": f.form,
                "filed": f.filed, "end": f.end, "tag": f.tag,
                "url": f.url(self.cik)}

    def series(self, concept: str, years: int = 10) -> List[Dict]:
        """Annual history for a concept, oldest first.

        The unit travels with the value. A foreign private issuer files in its
        home currency, so dropping the unit here is how a figure in yen ends up
        being divided by a market value in dollars three modules away, without
        anything noticing.
        """
        rows = self.annual.get(concept) or []
        return [{"fy": f.fy, "value": f.value, "end": f.end, "form": f.form,
                 "filed": f.filed, "unit": f.unit, "url": f.url(self.cik)}
                for f in rows[-years:]]

    def currencies(self, concepts) -> set:
        """Every currency the given concepts were reported in."""
        found = set()
        for concept in concepts:
            for fact in self.annual.get(concept) or []:
                if fact.unit and fact.unit not in ("shares", "pure"):
                    found.add(fact.unit.split("/")[0])
        return found

    def cagr(self, concept: str, years: int = 5) -> Optional[float]:
        """Compound annual growth for a concept, when the history supports it."""
        rows = self.series(concept, years + 1)
        if len(rows) < 3:
            return None
        first, last = rows[0]["value"], rows[-1]["value"]
        span = len(rows) - 1
        # A sign change makes a growth rate meaningless rather than merely large.
        if first is None or last is None or first <= 0 or last <= 0:
            return None
        return (last / first) ** (1.0 / span) - 1.0


def company_facts(symbol: str) -> Optional[CompanyFacts]:
    """Every XBRL concept this company has filed, normalised and de-duplicated."""
    cik = cik_for(symbol)
    if cik is None:
        return None

    with _CACHE_LOCK:
        if cik in _FACTS_CACHE:
            return _FACTS_CACHE[cik]

    data = _get(_FACTS_URL % cik)
    if not isinstance(data, dict):
        return None

    facts = CompanyFacts(cik=cik, entity=data.get("entityName") or symbol.upper())
    us_gaap = (data.get("facts") or {}).get("us-gaap") or {}

    for concept, tags in _CONCEPTS.items():
        for tag in tags:
            node = us_gaap.get(tag)
            if not node:
                continue
            units = node.get("units") or {}
            unit_key = next((u for u in units if u in ("USD", "USD/shares", "shares")),
                            next(iter(units), None))
            if not unit_key:
                continue

            annual, quarterly = {}, {}
            for row in units[unit_key]:
                form = row.get("form", "")
                end = row.get("end", "")
                if not end:
                    continue

                # The "fy" field is the fiscal year of the *filing*, not of the
                # figure. A 2025 annual report restates 2023 and 2024 as
                # comparatives and tags all three fy=2025, which produced three
                # different "2025" revenues and a nonsense growth rate. The
                # period end is the only reliable marker of which year a number
                # belongs to.
                period_year = _year_of(end)
                span = _span_days(row.get("start"), end)

                # Duration concepts (revenue, income) must cover a full year to
                # be annual; a quarter filed inside a 10-K is not an annual
                # figure. Instant concepts (assets, equity) have no start date
                # and are point-in-time, so they are accepted as they come.
                is_duration = row.get("start") is not None
                if is_duration and span is not None and not (300 <= span <= 400):
                    is_annual_period = False
                    is_quarter_period = 60 <= span <= 120
                else:
                    is_annual_period = True
                    is_quarter_period = False

                entry = Fact(concept=concept, tag=tag, value=float(row.get("val", 0)),
                             unit=unit_key, fy=period_year, fp=row.get("fp", ""),
                             form=form, filed=row.get("filed", ""),
                             end=end, accession=row.get("accn", ""))

                if form in ("10-K", "20-F") and is_annual_period:
                    # Key by the period, so a later restatement of the same year
                    # replaces the original instead of appearing beside it.
                    prior = annual.get(period_year)
                    if prior is None or entry.filed >= prior.filed:
                        annual[period_year] = entry
                elif form in ("10-Q", "6-K") and (is_quarter_period or not is_duration):
                    prior = quarterly.get(end)
                    if prior is None or entry.filed >= prior.filed:
                        quarterly[end] = entry

            if annual:
                facts.annual[concept] = [annual[k] for k in sorted(annual)]
            if quarterly:
                facts.quarterly[concept] = [quarterly[k] for k in sorted(quarterly)]
            if annual or quarterly:
                break        # first tag that produced data wins

    with _CACHE_LOCK:
        _FACTS_CACHE[cik] = facts
    return facts


@dataclass
class Filing:
    """One filed document."""
    form: str
    filed: str
    period: str
    accession: str
    document: str
    description: str
    cik: int
    size: int = 0

    @property
    def url(self) -> str:
        plain = self.accession.replace("-", "")
        return _ARCHIVE % (self.cik, plain, self.document)

    @property
    def index_url(self) -> str:
        plain = self.accession.replace("-", "")
        return "https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm" % (
            self.cik, plain, self.accession)

    @property
    def meaning(self) -> str:
        return FORM_MEANING.get(self.form, "")

    @property
    def material(self) -> bool:
        return self.form in MATERIAL_FORMS


def filings(symbol: str, forms: Optional[tuple] = None, limit: int = 40) -> List[Filing]:
    """Recent filings, newest first, optionally restricted to certain forms."""
    cik = cik_for(symbol)
    if cik is None:
        return []
    data = _get(_SUBS_URL % cik)
    if not isinstance(data, dict):
        return []

    recent = (data.get("filings") or {}).get("recent") or {}
    out: List[Filing] = []
    count = len(recent.get("form", []))
    for i in range(count):
        form = recent["form"][i]
        if forms and form not in forms:
            continue
        out.append(Filing(
            form=form,
            filed=recent["filingDate"][i],
            period=recent.get("reportDate", [""] * count)[i],
            accession=recent["accessionNumber"][i],
            document=recent.get("primaryDocument", [""] * count)[i],
            description=recent.get("primaryDocDescription", [""] * count)[i] or "",
            size=recent.get("size", [0] * count)[i] or 0,
            cik=cik))
        if len(out) >= limit:
            break
    return out


def recent_material(symbol: str, limit: int = 12) -> List[Filing]:
    """The filings that actually move a thesis, skipping routine insider forms."""
    return filings(symbol, forms=MATERIAL_FORMS, limit=limit)


def full_text_search(query: str, forms: Optional[str] = None,
                     limit: int = 10) -> List[Dict]:
    """Search the text of filings across all companies.

    Useful for questions no ratio answers, such as which filers discuss a
    supplier, a technology, or a legal exposure by name.
    """
    params = {"q": '"%s"' % query.strip('"'), "from": 0, "size": limit}
    if forms:
        params["forms"] = forms
    data = _get(_SEARCH_URL, params)
    if not isinstance(data, dict):
        return []

    hits = ((data.get("hits") or {}).get("hits") or [])
    out = []
    for hit in hits[:limit]:
        src = hit.get("_source") or {}
        ident = hit.get("_id", "")
        accession = ident.split(":")[0] if ":" in ident else ident
        ciks = src.get("ciks") or []
        cik = int(ciks[0]) if ciks else 0
        out.append({
            "company": (src.get("display_names") or [""])[0],
            "form": src.get("root_form") or src.get("file_type") or "",
            "filed": src.get("file_date") or "",
            "cik": cik,
            "accession": accession,
            "url": ("https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm"
                    % (cik, accession.replace("-", ""), accession)) if cik else "",
        })
    return out


def total_filings_hits(query: str, forms: Optional[str] = None) -> Optional[int]:
    """How many filings mention a phrase. A crude but real popularity measure."""
    params = {"q": '"%s"' % query.strip('"'), "from": 0, "size": 1}
    if forms:
        params["forms"] = forms
    data = _get(_SEARCH_URL, params)
    if not isinstance(data, dict):
        return None
    total = ((data.get("hits") or {}).get("total") or {})
    return total.get("value") if isinstance(total, dict) else total


def financial_history(symbol: str, years: int = 6) -> Dict:
    """A tidy multi-year table of filed figures, with growth and margins.

    Everything here is as reported, and every row links to the filing it came
    from, so a claim about the business can always be checked.
    """
    facts = company_facts(symbol)
    if facts is None:
        return {"available": False,
                "reason": "This instrument does not file with the SEC. That is "
                          "expected for ETFs, funds, crypto and foreign listings."}

    wanted = ("revenue", "gross_profit", "operating_income", "net_income",
              "operating_cashflow", "capex", "assets", "liabilities", "equity",
              "eps_diluted", "rd_expense", "long_term_debt",
              # Inputs to the scored models. Requested here so one fetch of the
              # company's facts serves the whole analysis.
              "current_assets", "current_liabilities", "retained_earnings",
              "receivables", "inventory", "ppe_net", "depreciation",
              "sga_expense", "shares_diluted", "cost_of_revenue")
    table: Dict[str, List[Dict]] = {}
    for concept in wanted:
        rows = facts.series(concept, years)
        if rows:
            table[concept] = rows

    years_seen = sorted({r["fy"] for rows in table.values() for r in rows if r["fy"]})

    derived = {}
    rev = {r["fy"]: r["value"] for r in table.get("revenue", [])}
    ni = {r["fy"]: r["value"] for r in table.get("net_income", [])}
    ocf = {r["fy"]: r["value"] for r in table.get("operating_cashflow", [])}
    capex = {r["fy"]: r["value"] for r in table.get("capex", [])}
    for fy in years_seen:
        row = {}
        if rev.get(fy) and ni.get(fy) is not None and rev[fy] != 0:
            row["net_margin"] = ni[fy] / rev[fy]
        if ocf.get(fy) is not None and capex.get(fy) is not None:
            # Capex is filed as a positive outflow, so free cash flow subtracts it.
            row["free_cashflow"] = ocf[fy] - abs(capex[fy])
            if rev.get(fy):
                row["fcf_margin"] = row["free_cashflow"] / rev[fy]
        if row:
            derived[fy] = row

    # A company that re-registers gets a new CIK, and its whole filing history
    # stays under the old one. The new entity then has a couple of quarterly
    # filings and nothing else, which would otherwise render as a blank table
    # with no explanation. Exxon Mobil did exactly this.
    if len(years_seen) < 2:
        return {
            "available": False,
            "entity": facts.entity,
            "cik": facts.cik,
            "reason": (
                "%s files under CIK %d, which holds only %d year%s of annual "
                "figures. That usually means the company re-registered and its "
                "history sits under a predecessor CIK. The filings are on "
                "EDGAR; this table cannot assemble them automatically."
                % (facts.entity, facts.cik, len(years_seen),
                   "" if len(years_seen) == 1 else "s")),
            "source_url": ("https://www.sec.gov/cgi-bin/browse-edgar?action="
                           "getcompany&CIK=%010d" % facts.cik),
        }

    # Which currency the accounts are in, and whether they are consistent.
    # A mixed set means some concepts came back in a different unit, and any
    # ratio crossing them would be meaningless.
    money_concepts = [c for c in wanted
                      if c not in ("eps_diluted", "shares_diluted")]
    currencies = facts.currencies(money_concepts)
    currency = sorted(currencies)[0] if len(currencies) == 1 else ""

    return {
        "available": True,
        "entity": facts.entity,
        "cik": facts.cik,
        "currency": currency,
        "currencies": sorted(currencies),
        "mixed_currency": len(currencies) > 1,
        "years": years_seen,
        "table": table,
        "derived": derived,
        "cagr": {c: facts.cagr(c, 5) for c in ("revenue", "net_income",
                                               "operating_income", "equity")},
        "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%010d" % facts.cik,
    }
