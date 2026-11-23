"""Stock screening: find candidates, then prove they survive scrutiny.

Two stages, because they cost very different amounts.

The **scan** stage works on bulk quotes, which arrive fifty at a time and cover
price, volume, market cap and basic ratios. Cheap enough to run over hundreds
of names.

The **deep** stage pulls full fundamentals for the survivors only. That is one
request per company, so it runs on a shortlist rather than a universe.

Screens are stated as explicit filters with units, and every result carries the
value that passed each filter. A screener that shows a list of tickers without
showing why they qualified is asking to be trusted rather than checked.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import fundamentals as fund
from . import yahoo

# How long one screen may take, start to finish. The page answers inside the
# request, and Cloudflare drops a request after 100 seconds; one slow answer
# from the data source used to hold a page for minutes. Whatever has not
# answered by then is named as not read rather than waited for.
BUDGET = 40.0

# How many instruments one typed list may hold. Every one is a share of a
# request to a data source the whole site shares, outside the budget above.
MAX_LISTED = 60

# Yahoo's own saved screens, usable as starting universes.
UNIVERSES = {
    "most_actives": "Most actively traded today",
    "day_gainers": "Biggest gainers today",
    "day_losers": "Biggest losers today",
    "growth_technology_stocks": "Technology names with growth",
    "undervalued_growth_stocks": "Growth at a lower multiple",
    "undervalued_large_caps": "Large companies on low multiples",
    "aggressive_small_caps": "Small companies with momentum",
    "small_cap_gainers": "Small companies rising",
    "most_shorted_stocks": "Heavily shorted",
    "portfolio_anchors": "Large, stable holdings",
}

# Every filter the screener understands, with the field it reads and how to
# describe it. Keeping this in one table means the UI and the engine can never
# disagree about what a filter means. A unit of "pct" is a fraction here and a
# whole percent on the page; "points" is already a whole percent, as the data
# source gives it. A hint is shown under the filter's box.
FILTERS: Dict[str, Dict] = {
    "price_min": {"field": "price", "op": ">=", "label": "Price at least", "unit": ""},
    "price_max": {"field": "price", "op": "<=", "label": "Price at most", "unit": ""},
    "market_cap_min": {"field": "market_cap", "op": ">=",
                       "label": "Market cap at least", "unit": "usd"},
    "market_cap_max": {"field": "market_cap", "op": "<=",
                       "label": "Market cap at most", "unit": "usd"},
    "volume_min": {"field": "volume", "op": ">=", "label": "Volume at least", "unit": ""},
    "change_min": {"field": "change_pct", "op": ">=",
                   "label": "Up at least", "unit": "pct"},
    "change_max": {"field": "change_pct", "op": "<=",
                   "label": "Up at most", "unit": "pct"},
    "pe_max": {"field": "pe", "op": "<=", "label": "P/E at most", "unit": "x"},
    "pe_min": {"field": "pe", "op": ">=", "label": "P/E at least", "unit": "x"},
    "dividend_min": {"field": "dividend_yield", "op": ">=",
                     "label": "Dividend yield at least", "unit": "pct"},
    "off_high_min": {"field": "off_high", "op": ">=",
                     "label": "Down from 52-week high at least", "unit": "pct"},
    "off_high_max": {"field": "off_high", "op": "<=",
                     "label": "Down from 52-week high at most", "unit": "pct"},
    # These need the deep stage.
    "roe_min": {"field": "roe", "op": ">=", "label": "Return on equity at least",
                "unit": "pct", "deep": True},
    "margin_min": {"field": "net_margin", "op": ">=", "label": "Net margin at least",
                   "unit": "pct", "deep": True},
    "growth_min": {"field": "revenue_growth", "op": ">=",
                   "label": "Revenue growth at least", "unit": "pct", "deep": True},
    "debt_max": {"field": "debt_to_equity", "op": "<=",
                 "label": "Debt to equity at most", "unit": "points", "deep": True,
                 "hint": "As a percent: 150 means debt of one and a half times equity."},
    "fcf_positive": {"field": "free_cashflow", "op": ">", "label": "Free cash flow above",
                     "unit": "usd", "deep": True,
                     "hint": "Type 0 for any company that takes in more cash than it spends."},
}


@dataclass
class Candidate:
    """One instrument that passed, with the evidence."""
    symbol: str
    name: str = ""
    price: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    market_cap: Optional[float] = None
    pe: Optional[float] = None
    dividend_yield: Optional[float] = None
    off_high: Optional[float] = None
    sector: str = ""
    # Filled by the deep stage
    roe: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_growth: Optional[float] = None
    debt_to_equity: Optional[float] = None
    free_cashflow: Optional[float] = None
    quality: Optional[float] = None
    passed: List[str] = field(default_factory=list)
    deep_loaded: bool = False
    # What its price and its accounts are in; empty when not said.
    currency: str = ""
    accounts_currency: str = ""

    def value(self, field_name: str):
        return getattr(self, field_name, None)

    @property
    def cap_currency(self) -> str:
        """What the market cap is in when that is not dollars. A price in
        pence has its market cap in pounds."""
        code = fund._MAJOR.get(self.currency, self.currency)
        return "" if code == "USD" else code

    @property
    def size(self) -> str:
        """The market cap at the scale a person would say it: 52M, 3.40T."""
        cap = self.market_cap
        if not cap:
            return ""
        # Where each takes over from the one below is where that would round
        # up to it: 999.96 billion is 1.00T, not 1000.0B.
        for least, scale, shown in ((999.95e9, 1e12, "%.2fT"), (999.5e6, 1e9, "%.1fB"),
                                    (950e3, 1e6, "%.0fM")):
            if cap >= least:
                return (shown % (cap / scale) + " " + self.cap_currency).strip()
        return ("under 1M " + self.cap_currency).strip()


@dataclass
class ScreenResult:
    """What a screen found, with every name scanned accounted for.

    scanned = passed + rejected + not_checked + unread + not_shown, so a count
    on the page never quietly loses names.
    """
    universe: str
    scanned: int
    passed: List[Candidate]
    rejected: int
    filters: List[Dict]
    deep: bool
    notes: List[str] = field(default_factory=list)
    # Passed the quick filters but never checked against the slower ones.
    not_checked: int = 0
    # Fundamentals could not be read, or not in time: neither passed nor
    # rejected, because nothing was judged.
    unread: List[str] = field(default_factory=list)
    # Passed everything but beyond the number listed.
    not_shown: int = 0
    # The list or its quotes could not be fetched at all.
    failed: bool = False
    # When the quotes behind it were read.
    at: float = field(default_factory=time.time)

    @property
    def complete(self) -> bool:
        """Read everything it set out to, so it is safe to share."""
        return not self.failed and self.scanned > 0 and not self.unread


def _from_quote(q: Dict) -> Candidate:
    price = q.get("regularMarketPrice")
    high52 = q.get("fiftyTwoWeekHigh")
    off_high = None
    if price and high52 and high52 > 0:
        off_high = (high52 - price) / high52
    return Candidate(
        symbol=q.get("symbol", ""),
        name=q.get("shortName") or q.get("longName") or "",
        price=price,
        change_pct=(q.get("regularMarketChangePercent") or 0) / 100.0
        if q.get("regularMarketChangePercent") is not None else None,
        volume=q.get("regularMarketVolume"),
        market_cap=q.get("marketCap"),
        pe=q.get("trailingPE"),
        dividend_yield=(q.get("dividendYield") or 0) / 100.0
        if q.get("dividendYield") else None,
        off_high=off_high,
        sector=q.get("sector") or "",
        currency=q.get("currency") or "",
    )


def universe_symbols(name: str, count: int = 100) -> List[str]:
    """Symbols from one of Yahoo's saved screens."""
    rows = yahoo.predefined_screen(name, count)
    return [r.get("symbol") for r in rows if r.get("symbol")]


def _not_dollars(candidate: Candidate, key: str, threshold: float) -> bool:
    """Whether this filter is an amount of dollars and the figure is not one.

    No exchange rate is at hand, so the two cannot be compared. More or less
    than nothing is the same question in any currency.
    """
    if FILTERS[key]["unit"] != "usd" or not threshold:
        return False
    if FILTERS[key]["field"] == "free_cashflow":
        return candidate.accounts_currency not in ("", "USD")
    return bool(candidate.cap_currency)


def _passes(candidate: Candidate, key: str, threshold: float) -> Optional[bool]:
    """Test one filter. None means the data was not available to judge."""
    spec = FILTERS.get(key)
    if not spec:
        return None
    value = candidate.value(spec["field"])
    if value is None or _not_dollars(candidate, key, threshold):
        return None
    op = spec["op"]
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    return None


def run(symbols: Optional[List[str]] = None, universe: str = "most_actives",
        filters: Optional[Dict[str, float]] = None, deep: bool = False,
        limit: int = 40, require_known: bool = True,
        budget: Optional[float] = None) -> ScreenResult:
    """Screen a universe or an explicit list.

    `require_known` controls what happens when a filter cannot be evaluated
    because the data is missing. True excludes the candidate, which is the
    honest default: an unknown is not a pass. A figure the data source would
    not send at all is different: that candidate is neither passed nor
    rejected, and is named as not read.

    `budget` is how many seconds the whole screen may take (BUDGET when not
    given); whatever the deep stage has not read by then is named, not waited
    for.
    """
    started = time.time()
    deadline = started + (BUDGET if budget is None else budget)
    filters = {k: v for k, v in (filters or {}).items()
               if v is not None and k in FILTERS}

    notes: List[str] = []
    if symbols:
        universe_name = "your list"
        pool = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
        if len(pool) > MAX_LISTED:
            notes.append("%d of the %d instruments on your list were not looked at, "
                         "because one screen takes up to %d. Screen the rest as a "
                         "second list." % (len(pool) - MAX_LISTED, len(pool), MAX_LISTED))
            pool = pool[:MAX_LISTED]
    else:
        universe_name = UNIVERSES.get(universe, universe)
        try:
            pool = universe_symbols(universe, max(limit * 3, 60))
        except yahoo.YahooError as exc:
            return ScreenResult(universe_name, 0, [], 0, _describe(filters), deep,
                                ["Could not fetch that list just now: %s" % exc],
                                failed=True, at=started)

    if not pool:
        return ScreenResult(universe_name, 0, [], 0,
                            _describe(filters), deep,
                            ["That universe returned nothing."], at=started)

    try:
        rows = yahoo.quotes(pool)
    except yahoo.YahooError as exc:
        return ScreenResult(universe_name, 0, [], 0, _describe(filters), deep,
                            ["Could not fetch quotes: %s" % exc],
                            failed=True, at=started)
    read_at = time.time()

    candidates = [_from_quote(q) for q in rows if q.get("symbol")]
    scanned = len(candidates)

    shallow = {k: v for k, v in filters.items() if not FILTERS[k].get("deep")}
    deep_filters = {k: v for k, v in filters.items() if FILTERS[k].get("deep")}
    if deep_filters and not deep:
        deep = True
        notes.append("Some of these filters need each company's fundamentals, "
                     "so they were checked only on names that passed the others.")

    survivors, rejected = [], 0
    elsewhere: List[str] = []           # left out: its amounts are not in dollars
    for c in candidates:
        ok = True
        for key, threshold in shallow.items():
            result = _passes(c, key, threshold)
            if result is None:
                if require_known:
                    if _not_dollars(c, key, threshold):
                        elsewhere.append(c.symbol)
                    ok = False
                    break
                continue
            if not result:
                ok = False
                break
            c.passed.append(key)
        if ok:
            survivors.append(c)
        else:
            rejected += 1

    # Largest first, before anything is cut. The list is shown largest first,
    # so the deep checks have to go to the largest too: cutting in the data
    # source's own order meant a large company that passed everything else was
    # never checked while a smaller one was shown. Dollars first: a size in
    # yen is not a larger company for being a larger number.
    survivors.sort(key=lambda c: (bool(c.cap_currency), -(c.market_cap or 0)))

    not_checked = 0
    unread: List[str] = []
    if deep and survivors:
        # One request per company, so only the shortlist and only in parallel.
        shortlist = survivors[:max(limit, 25)]
        not_checked = len(survivors) - len(shortlist)
        _load_deep(shortlist, deadline)
        refined = []
        for c in shortlist:
            if deep_filters and not c.deep_loaded:
                # Nothing was judged, so it is neither a pass nor a rejection.
                unread.append(c.symbol)
                continue
            ok = True
            for key, threshold in deep_filters.items():
                result = _passes(c, key, threshold)
                if result is None:
                    if require_known:
                        if _not_dollars(c, key, threshold):
                            elsewhere.append(c.symbol)
                        ok = False
                        break
                    continue
                if not result:
                    ok = False
                    break
                c.passed.append(key)
            if ok:
                refined.append(c)
            else:
                rejected += 1
        survivors = refined

    not_shown = max(0, len(survivors) - limit)
    survivors = survivors[:limit]

    if not_checked:
        notes.append("%d more passed the other filters but were not checked "
                     "against the slower ones, because only the %d largest are. "
                     "Narrow the screen to reach them." % (not_checked, len(shortlist)))
    if elsewhere:
        notes.append("%s left out: an amount asked for is in dollars, and theirs are "
                     "in another currency, so the two cannot be compared."
                     % _names(elsewhere))
    if unread:
        notes.append("The fundamentals for %d could not be read just now (%s), so "
                     "they are neither passed nor rejected. Try again in a minute."
                     % (len(unread), _names(unread)))
    if not_shown:
        notes.append("%d more passed but are not listed: only the %d largest are "
                     "shown. Narrow the screen to see the rest." % (not_shown, limit))
    if not survivors and filters:
        if not unread:
            notes.append("Nothing passed. Either the filters are too tight, or the "
                         "data needed to judge them was not reported for this "
                         "universe.")
        elif rejected:
            notes.append("Nothing passed among the ones that could be read.")
    return ScreenResult(universe_name, scanned, survivors, rejected,
                        _describe(filters), deep, notes, not_checked=not_checked,
                        unread=unread, not_shown=not_shown, at=read_at)


def _names(symbols: List[str], most: int = 8) -> str:
    shown = ", ".join(symbols[:most])
    if len(symbols) > most:
        shown += " and %d more" % (len(symbols) - most)
    return shown


def _load_deep(candidates: List[Candidate], deadline: Optional[float] = None) -> None:
    """Attach fundamentals to a shortlist, concurrently, by a deadline.

    A candidate whose figures were not read, or not by the deadline, keeps
    deep_loaded False. fundamentals.load does not raise when the data source
    refuses: it returns an empty record with a note, and treating that as
    loaded is how a refused request came to read as "rejected by your filters".
    """
    def grab(symbol: str) -> Optional[Dict]:
        try:
            f = fund.load(symbol, with_sec=False)
            notes = getattr(f, "notes", None) or []
            metrics = getattr(f, "metrics", None)
            if any(str(n).startswith("Yahoo fundamentals unavailable") for n in notes):
                return None
            if metrics is not None and not any(getattr(m, "known", False)
                                               for m in metrics.values()):
                return None
            q = fund.quality_score(f)
            return {"roe": f.get("roe"), "net_margin": f.get("net_margin"),
                    "revenue_growth": f.get("revenue_growth"),
                    "debt_to_equity": f.get("debt_to_equity"),
                    "free_cashflow": f.get("free_cashflow"), "sector": f.sector,
                    "accounts_currency": getattr(f, "accounts_currency", ""),
                    "quality": q["score"] if q["reliable"] else None}
        except Exception:
            return None

    if not candidates:
        return
    pool = ThreadPoolExecutor(max_workers=min(6, len(candidates)))
    futures = {pool.submit(grab, c.symbol): c for c in candidates}
    try:
        timeout = None if deadline is None else max(0.0, deadline - time.time())
        # Applied here, in this thread, so a figure that arrives after the
        # deadline cannot change a candidate already counted as not read.
        for future in as_completed(futures, timeout=timeout):
            got = future.result()
            c = futures[future]
            if got is None:
                c.deep_loaded = False
                continue
            c.roe, c.net_margin = got["roe"], got["net_margin"]
            c.revenue_growth = got["revenue_growth"]
            c.debt_to_equity = got["debt_to_equity"]
            c.free_cashflow = got["free_cashflow"]
            c.accounts_currency = got["accounts_currency"]
            c.sector = c.sector or got["sector"]
            c.quality = got["quality"]
            c.deep_loaded = True
    except FuturesTimeout:
        pass
    finally:
        # Not waited for: requests still out finish in the background and are
        # discarded.
        pool.shutdown(wait=False, cancel_futures=True)


def _figure(value: float) -> str:
    """A number as typed: 1,000,000, never 1e+06."""
    if float(value).is_integer():
        return "{:,.0f}".format(value)
    return "{:,.10g}".format(value)


def _describe(filters: Dict[str, float]) -> List[Dict]:
    """Human-readable statement of what was asked for."""
    out = []
    for key, value in filters.items():
        spec = FILTERS[key]
        unit = spec["unit"]
        if unit == "pct":
            shown = "%.1f%%" % (value * 100)
        elif unit == "usd":
            shown = "{:,.0f}".format(value)
        elif unit == "points":
            shown = "%s%%" % _figure(value)
        elif unit == "x":
            shown = "%.1fx" % value
        else:
            shown = _figure(value)
        out.append({"key": key, "label": spec["label"], "value": shown,
                    "deep": bool(spec.get("deep"))})
    return out


# Ready-made screens, each with a stated rationale rather than a clever name.
PRESETS = {
    "quality_value": {
        "label": "Profitable and not expensive",
        "why": "Companies earning a real return on equity, generating cash, and "
               "not priced for perfection.",
        "universe": "undervalued_large_caps",
        "filters": {"market_cap_min": 2e9, "pe_max": 22, "roe_min": 0.12,
                    "margin_min": 0.08, "fcf_positive": 0},
    },
    "growth": {
        "label": "Growing fast and profitably",
        "why": "Revenue growing above 15% while still making money, which "
               "filters out growth bought with losses.",
        "universe": "growth_technology_stocks",
        "filters": {"market_cap_min": 1e9, "growth_min": 0.15, "margin_min": 0.05},
    },
    "dividend": {
        "label": "Income that looks affordable",
        "why": "A yield above 3% from a company with positive cash flow and "
               "moderate debt, so the dividend is paid from earnings.",
        "universe": "portfolio_anchors",
        "filters": {"dividend_min": 0.03, "market_cap_min": 5e9,
                    "debt_max": 200, "fcf_positive": 0},
    },
    "oversold_quality": {
        "label": "Good companies that have fallen",
        "why": "Down at least 25% from the 52-week high but still profitable "
               "with healthy returns. Where value sometimes appears.",
        "universe": "most_actives",
        "filters": {"off_high_min": 0.25, "roe_min": 0.10, "margin_min": 0.05,
                    "market_cap_min": 1e9},
    },
    "momentum": {
        "label": "Moving with real volume",
        "why": "Rising today on genuine liquidity, not a thin-volume spike.",
        "universe": "day_gainers",
        "filters": {"change_min": 0.03, "volume_min": 1_000_000,
                    "market_cap_min": 5e8},
    },
}
