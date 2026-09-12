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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import fundamentals as fund
from . import yahoo

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
# disagree about what a filter means.
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
                 "label": "Debt to equity at most", "unit": "", "deep": True},
    "fcf_positive": {"field": "free_cashflow", "op": ">", "label": "Free cash flow positive",
                     "unit": "usd", "deep": True},
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

    def value(self, field_name: str):
        return getattr(self, field_name, None)


@dataclass
class ScreenResult:
    universe: str
    scanned: int
    passed: List[Candidate]
    rejected: int
    filters: List[Dict]
    deep: bool
    notes: List[str] = field(default_factory=list)


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
    )


def universe_symbols(name: str, count: int = 100) -> List[str]:
    """Symbols from one of Yahoo's saved screens."""
    rows = yahoo.predefined_screen(name, count)
    return [r.get("symbol") for r in rows if r.get("symbol")]


def _passes(candidate: Candidate, key: str, threshold: float) -> Optional[bool]:
    """Test one filter. None means the data was not available to judge."""
    spec = FILTERS.get(key)
    if not spec:
        return None
    value = candidate.value(spec["field"])
    if value is None:
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
        limit: int = 40, require_known: bool = True) -> ScreenResult:
    """Screen a universe or an explicit list.

    `require_known` controls what happens when a filter cannot be evaluated
    because the data is missing. True excludes the candidate, which is the
    honest default: an unknown is not a pass.
    """
    filters = {k: v for k, v in (filters or {}).items()
               if v is not None and k in FILTERS}

    if symbols:
        universe_name = "your list"
        pool = [s.strip().upper() for s in symbols if s and s.strip()]
    else:
        universe_name = UNIVERSES.get(universe, universe)
        pool = universe_symbols(universe, max(limit * 3, 60))

    notes: List[str] = []
    if not pool:
        return ScreenResult(universe_name, 0, [], 0,
                            _describe(filters), deep,
                            ["That universe returned nothing."])

    try:
        rows = yahoo.quotes(pool)
    except yahoo.YahooError as exc:
        return ScreenResult(universe_name, 0, [], 0, _describe(filters), deep,
                            ["Could not fetch quotes: %s" % exc])

    candidates = [_from_quote(q) for q in rows if q.get("symbol")]
    scanned = len(candidates)

    shallow = {k: v for k, v in filters.items() if not FILTERS[k].get("deep")}
    deep_filters = {k: v for k, v in filters.items() if FILTERS[k].get("deep")}
    if deep_filters and not deep:
        deep = True
        notes.append("Deep checks were requested by the filters, so fundamentals "
                     "were loaded for the survivors.")

    survivors, rejected = [], 0
    for c in candidates:
        ok = True
        for key, threshold in shallow.items():
            result = _passes(c, key, threshold)
            if result is None:
                if require_known:
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

    if deep and survivors:
        # One request per company, so only the shortlist and only in parallel.
        shortlist = survivors[:max(limit, 25)]
        _load_deep(shortlist)
        refined = []
        for c in shortlist:
            ok = True
            for key, threshold in deep_filters.items():
                result = _passes(c, key, threshold)
                if result is None:
                    if require_known:
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

    survivors.sort(key=lambda c: -(c.market_cap or 0))
    if len(survivors) > limit:
        survivors = survivors[:limit]

    if not survivors and filters:
        notes.append("Nothing passed. Either the filters are too tight, or the "
                     "data needed to judge them was not reported for this universe.")
    return ScreenResult(universe_name, scanned, survivors, rejected,
                        _describe(filters), deep, notes)


def _load_deep(candidates: List[Candidate]) -> None:
    """Attach fundamentals to a shortlist, concurrently."""
    def grab(c: Candidate):
        try:
            f = fund.load(c.symbol, with_sec=False)
            c.roe = f.get("roe")
            c.net_margin = f.get("net_margin")
            c.revenue_growth = f.get("revenue_growth")
            c.debt_to_equity = f.get("debt_to_equity")
            c.free_cashflow = f.get("free_cashflow")
            c.sector = c.sector or f.sector
            q = fund.quality_score(f)
            c.quality = q["score"] if q["reliable"] else None
            c.deep_loaded = True
        except Exception:
            c.deep_loaded = False

    if not candidates:
        return
    with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
        list(pool.map(grab, candidates))


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
        elif unit == "x":
            shown = "%.1fx" % value
        else:
            shown = "{:,.4g}".format(value)
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
