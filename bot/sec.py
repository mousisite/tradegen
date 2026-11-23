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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from . import upstream

# What the circuit breaker in upstream knows this source as.
_HOST = "SEC EDGAR"


def _headers() -> Dict[str, str]:
    """Who is asking, as EDGAR wants it said.

    The contact is the site's own, from STOCKBOT_CONTACT, read when a request
    is made. Nobody's address belongs in the code: the repository is public
    and every copy of it would send the same one. Without the setting the
    site's address stands in.
    """
    contact = (os.environ.get("STOCKBOT_CONTACT") or "").strip()
    # A header takes one line of plain characters; anything else in the
    # setting would break every request rather than only the contact.
    if not all(" " <= ch <= "~" for ch in contact):
        contact = ""
    who = "https://orenth.app" + ("; " + contact if contact else "")
    return {"User-Agent": "Orenth/1.0 (%s)" % who,
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
# When the list in memory was downloaded, when a newer one was last asked for,
# and who is asking: a server that runs for months still learns of listings.
_TICKER_AT = [0.0]
_TICKER_ASKED = [0.0]
_TICKER_RETRY = 3600.0
_TICKER_LOCK = threading.Lock()
# Companies' parsed filings, most recently used last. Bounded, because each is
# close to a megabyte and the server has half a gigabyte; and refreshed daily,
# because a company files new figures without the server restarting.
_FACTS_CACHE: "OrderedDict[int, Tuple[float, CompanyFacts]]" = OrderedDict()
_FACTS_MAX = 64
_FACTS_TTL = 24 * 3600.0
_CACHE_LOCK = threading.Lock()
# The companies whose figures are being downloaded right now, so that the two
# halves of one research page, and two people opening it together, share the
# one download. Several megabytes each, on half a processor.
_FACTS_FLIGHTS: Dict[int, Dict] = {}
# Longer than a download takes at its slowest, so nobody waiting starts another.
_FACTS_WAIT = 45.0

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
    "40-F": "Annual report from a Canadian company.",
    "6-K": "Interim report from a foreign private issuer.",
}

MATERIAL_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A", "S-1", "424B5",
                  "SC 13D", "SC 13G", "20-F", "40-F", "6-K")

# The forms a year's accounts are filed on: a US company's, a foreign one's
# and a Canadian one's.
ANNUAL_FORMS = ("10-K", "20-F", "40-F")


class SECError(RuntimeError):
    """Raised when EDGAR cannot be reached or has nothing for this company."""


def _year_of(date_str: str) -> Optional[int]:
    """Calendar year a reporting period ended in."""
    try:
        return int(str(date_str)[:4])
    except (TypeError, ValueError):
        return None


def _fiscal_year(end: str) -> Optional[int]:
    """The year a period's figures are labelled with.

    The calendar year it ended in, except that a period ending in the first
    week of January belongs to the year before. Companies on 52 or 53 week
    years end on the Saturday nearest 31 December, which is sometimes 1 or 2
    January, and the plain calendar year then gave two fiscal years the same
    label: one replaced the other, and the years after it were labelled wrong.
    """
    year = _year_of(end)
    if year is None:
        return None
    try:
        month, day = int(str(end)[5:7]), int(str(end)[8:10])
    except (TypeError, ValueError):
        return year
    return year - 1 if month == 1 and day <= 7 else year


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


def _get(url: str, params: Optional[Dict] = None, tries: int = 2):
    """One EDGAR answer, inside about half a minute, or an SECError.

    A page waits for this, and Cloudflare gives up on a page at 100 seconds:
    three tries of thirty seconds each outlasted it. A refusal is not asked
    again at once either. EDGAR answers a client that is over its limit with
    403 or 429, and asking on keeps the door shut for everybody here.
    """
    last = None
    for attempt in range(tries):
        if attempt:
            time.sleep(0.6)
        try:
            upstream.check(_HOST)
        except upstream.Throttled as exc:
            raise SECError("EDGAR has asked for a pause of %d seconds" % exc.seconds)
        _throttle()
        try:
            r = requests.get(url, params=params, headers=_headers(), timeout=(5, 10))
        except requests.RequestException as exc:
            last = str(exc)
            continue
        if r.status_code == 200:
            upstream.note_ok(_HOST)
            try:
                return r.json()
            except ValueError:
                return r.text
        if r.status_code == 404:
            return None
        last = "HTTP %s" % r.status_code
        if r.status_code in (403, 429, 503):
            upstream.note_throttled(_HOST, upstream.retry_after_seconds(r))
            break
    raise SECError("EDGAR request failed: %s" % last)


def _cache_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "data", "cache")
    os.makedirs(path, exist_ok=True)
    return path


def ticker_map(max_age_days: int = 14) -> Dict[str, int]:
    """Ticker to CIK for every SEC registrant, cached on disk.

    The file is about 800 KB and changes rarely, so re-downloading it on every
    lookup would be wasteful and rude to a free public service. Once the list
    is older than `max_age_days` one caller asks for a newer one while the
    others go on with the old, and when none comes the old one stays in use.
    """
    path = os.path.join(_cache_dir(), "sec_tickers.json")
    if _TICKER_CACHE is None:
        try:
            made = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8") as fh:
                _keep_tickers(json.load(fh), made)
        except (OSError, ValueError):
            pass
    if _TICKER_CACHE is None:
        return _download_tickers(path)

    now = time.time()
    if (now - _TICKER_AT[0] >= max_age_days * 86400
            and now - _TICKER_ASKED[0] >= _TICKER_RETRY
            and _TICKER_LOCK.acquire(blocking=False)):
        try:
            _TICKER_ASKED[0] = now
            return _download_tickers(path)
        except Exception:
            pass                        # whatever came, the old list still serves
        finally:
            _TICKER_LOCK.release()
    return _TICKER_CACHE


def _keep_tickers(mapping: Dict[str, int], made: float) -> None:
    global _TICKER_CACHE
    _TICKER_CACHE, _TICKER_AT[0] = mapping, made


def _download_tickers(path: str) -> Dict[str, int]:
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
    _keep_tickers(mapping, time.time())
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
    # Whether it files figures under international rules, which are not read.
    international: bool = False

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
        # Per year of time that passed, not per row: a year missing from the
        # filings would otherwise squeeze the growth of two into one.
        days = _span_days(rows[0]["end"], rows[-1]["end"])
        span = days / 365.25 if days else len(rows) - 1
        # A sign change makes a growth rate meaningless rather than merely large.
        if first is None or last is None or first <= 0 or last <= 0 or span <= 0:
            return None
        return (last / first) ** (1.0 / span) - 1.0


def _joined(by_tag: List[Dict]) -> List["Fact"]:
    """One history for a figure the company has filed under several tags.

    by_tag holds each tag's rows by period, in order of preference. The tag
    still in use, the one with the newest period, is taken whole, so two
    neighbouring years are compared on the same definition; the other tags
    only supply periods it lacks. Taking the first tag with any data instead
    kept a company's 2019 revenue as its latest after it moved to another tag.
    Only rows in one currency are joined.
    """
    used = max(by_tag, key=lambda rows: max(rows))      # the first of equals
    unit = used[max(used)].unit
    out = dict(used)
    for rows in by_tag:
        for period, fact in rows.items():
            if fact.unit == unit:
                out.setdefault(period, fact)
    return [out[k] for k in sorted(out)]


def company_facts(symbol: str) -> Optional[CompanyFacts]:
    """Every XBRL concept this company has filed, normalised and de-duplicated."""
    cik = cik_for(symbol)
    if cik is None:
        return None

    with _CACHE_LOCK:
        hit = _FACTS_CACHE.get(cik)
        if hit is not None and time.time() - hit[0] < _FACTS_TTL:
            _FACTS_CACHE.move_to_end(cik)
            return hit[1]
        flight = _FACTS_FLIGHTS.get(cik)
        mine = flight is None
        if mine:
            flight = {"done": threading.Event(), "facts": None, "error": None}
            _FACTS_FLIGHTS[cik] = flight

    if not mine:
        if not flight["done"].wait(_FACTS_WAIT):
            raise SECError("EDGAR request failed: no answer in time")
        if flight["error"] is not None:
            raise flight["error"]
        return flight["facts"]

    try:
        flight["facts"] = _load_facts(cik, symbol)
        return flight["facts"]
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        # Let go of in the same moment as it is over: whoever looks next finds
        # the answer in the cache, or asks afresh.
        with _CACHE_LOCK:
            _FACTS_FLIGHTS.pop(cik, None)
        flight["done"].set()


def _load_facts(cik: int, symbol: str) -> Optional[CompanyFacts]:
    """Download, read and keep one company's filed figures."""
    data = _get(_FACTS_URL % cik)
    if not isinstance(data, dict):
        return None

    facts = CompanyFacts(cik=cik, entity=data.get("entityName") or symbol.upper())
    us_gaap = (data.get("facts") or {}).get("us-gaap") or {}
    facts.international = bool((data.get("facts") or {}).get("ifrs-full"))

    for concept, tags in _CONCEPTS.items():
        by_year, by_quarter = [], []       # one entry per tag that has data
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
                period_year = _fiscal_year(end)
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

                if form in ANNUAL_FORMS and is_annual_period:
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
                by_year.append(annual)
            if quarterly:
                by_quarter.append(quarterly)

        if by_year:
            facts.annual[concept] = _joined(by_year)
        if by_quarter:
            facts.quarterly[concept] = _joined(by_quarter)

    with _CACHE_LOCK:
        _FACTS_CACHE[cik] = (time.time(), facts)
        _FACTS_CACHE.move_to_end(cik)
        while len(_FACTS_CACHE) > _FACTS_MAX:
            _FACTS_CACHE.popitem(last=False)
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
    # Keyed with the unit, so a ratio is only formed between figures reported in
    # the same currency. Toyota files revenue in yen and net income in dollars;
    # dividing one by the other gives a margin wrong by the exchange rate.
    def _by_year(concept):
        return {r["fy"]: (r["value"], r.get("unit")) for r in table.get(concept, [])}

    def _same_unit(*pairs):
        units = {p[1] for p in pairs if p is not None}
        return len(units) <= 1

    rev_u = _by_year("revenue")
    ni_u = _by_year("net_income")
    ocf_u = _by_year("operating_cashflow")
    capex_u = _by_year("capex")
    rev = {k: v[0] for k, v in rev_u.items()}
    ni = {k: v[0] for k, v in ni_u.items()}
    ocf = {k: v[0] for k, v in ocf_u.items()}
    capex = {k: v[0] for k, v in capex_u.items()}

    for fy in years_seen:
        row = {}
        if (rev.get(fy) and ni.get(fy) is not None and rev[fy] != 0
                and _same_unit(rev_u.get(fy), ni_u.get(fy))):
            row["net_margin"] = ni[fy] / rev[fy]
        if (ocf.get(fy) is not None and capex.get(fy) is not None
                and _same_unit(ocf_u.get(fy), capex_u.get(fy))):
            # Capex is filed as a positive outflow, so free cash flow subtracts it.
            row["free_cashflow"] = ocf[fy] - abs(capex[fy])
            if rev.get(fy) and _same_unit(rev_u.get(fy), ocf_u.get(fy)):
                row["fcf_margin"] = row["free_cashflow"] / rev[fy]
        if row:
            derived[fy] = row

    # A company that re-registers gets a new CIK, and its whole filing history
    # stays under the old one. The new entity then has a couple of quarterly
    # filings and nothing else, which would otherwise render as a blank table
    # with no explanation. Exxon Mobil did exactly this. A company that keeps
    # its accounts under international rules has years of them, only not in
    # the part that is read here, and is told that instead.
    if len(years_seen) < 2:
        if facts.international:
            reason = ("%s keeps its accounts under international rules (IFRS), "
                      "which Orenth does not read yet. The filings themselves "
                      "are on EDGAR, the SEC's public site." % facts.entity)
        else:
            reason = ("The SEC's records of %s hold %s of annual figures, too few "
                      "to compare. That usually means it listed recently, or that "
                      "it re-registered and its older filings are under another "
                      "company number. The filings themselves are on EDGAR, the "
                      "SEC's public site."
                      % (facts.entity, {0: "no years", 1: "only one year"}[len(years_seen)]))
        return {
            "available": False,
            "entity": facts.entity,
            "cik": facts.cik,
            "reason": reason,
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
