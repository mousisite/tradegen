"""Market data access.

One upstream source (Yahoo Finance's public chart endpoint) covers equities,
ETFs and crypto, which keeps the bot key-free. The same endpoint reports the
instrument type, so the rest of the pipeline can adapt its rules per asset
class without the user telling it what they pasted in.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"

# Intraday history caps enforced by the upstream endpoint.
_MAX_RANGE = {"1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d",
              "30m": "60d", "60m": "730d", "1h": "730d", "1d": "5y"}

_INTERVAL_SEC = {"1m": 60, "2m": 120, "5m": 300, "15m": 900,
                 "30m": 1800, "60m": 3600, "1h": 3600, "1d": 86400}

_COMMON_CRYPTO = {
    "BTC", "XBT", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "LTC", "BCH", "SHIB", "TRX", "UNI", "ATOM", "XLM", "NEAR", "APT",
    "ARB", "OP", "SUI", "PEPE", "TON", "ICP", "FIL", "HBAR", "VET", "INJ",
}


class DataError(RuntimeError):
    """Raised when market data cannot be retrieved or is unusable."""


def format_price(value, currency: str = "") -> str:
    """Render a price readably across the full range of instrument prices.

    A generic significant-figure format turns 78230.4 into '7.823e+04', which
    is useless on a trading screen. Precision has to follow magnitude: index
    levels want thousands separators, small-cap tokens want six decimals.
    """
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)

    av = abs(v)
    if av >= 1000:
        out = "{:,.2f}".format(v)
    elif av >= 100:
        out = "{:,.2f}".format(v)
    elif av >= 1:
        out = "{:.3f}".format(v)
    elif av >= 0.01:
        out = "{:.5f}".format(v)
    else:
        out = "{:.8f}".format(v)

    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return ("%s %s" % (currency, out)).strip() if currency else out


@dataclass
class Bars:
    """An OHLCV series plus the metadata needed to interpret it."""
    symbol: str
    name: str
    asset_class: str          # "crypto" | "equity" | "other"
    currency: str
    exchange: str
    interval: str
    timestamp: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    session_id: np.ndarray    # exchange-local trading day per bar
    gmt_offset: int = 0
    prev_close: Optional[float] = None
    live_price: Optional[float] = None   # price of the still-forming bar, if any
    market_open: bool = True
    last_bar_time: str = ""
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last_price(self) -> float:
        """Best available current price: the forming bar if we have it."""
        if self.live_price is not None:
            return float(self.live_price)
        return float(self.close[-1])

    @property
    def last_closed_price(self) -> float:
        """Close of the last completed bar. Signals are computed against this."""
        return float(self.close[-1])

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"

    def today_mask(self) -> np.ndarray:
        """Boolean mask selecting bars from the most recent session."""
        return self.session_id == self.session_id[-1]


def _get(url: str, params: dict, tries: int = 3) -> dict:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s" % r.status_code
            if r.status_code in (404, 422):
                break
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(0.6 * (attempt + 1))
    raise DataError("request to %s failed: %s" % (url, last))


def normalise_symbol(raw: str) -> str:
    """Turn loose user or screenshot text into a Yahoo-style ticker.

    Handles the shapes a chart screenshot actually shows: BTCUSD, BTC/USD,
    BINANCE:ETHUSDT, NASDAQ:AAPL, and a leading dollar-sign cashtag.
    """
    s = (raw or "").strip().upper()
    if not s:
        raise DataError("empty symbol")
    if ":" in s:                       # strip exchange prefix, e.g. NASDAQ:AAPL
        s = s.split(":", 1)[1]
    s = s.lstrip("$").replace(" ", "")
    if "/" in s:                       # BTC/USD -> BTC-USD
        base, _, quote = s.partition("/")
        if quote in ("USDT", "USDC", "USD", ""):
            quote = "USD"
        return base + "-" + quote
    if s.endswith("-USD"):
        return s
    for suffix in ("USDT", "USDC", "USD"):
        if s.endswith(suffix) and s[:-len(suffix)] in _COMMON_CRYPTO:
            return s[:-len(suffix)] + "-USD"
    if s in _COMMON_CRYPTO:
        base = "BTC" if s == "XBT" else s
        return base + "-USD"
    return s


def resolve(query: str) -> dict:
    """Look up a ticker or company name and return the best matching instrument.

    Falls back to the normalised string itself when search returns nothing, so
    a valid-but-unindexed symbol still gets a chance at a data fetch.
    """
    candidate = normalise_symbol(query)
    # Search the *normalised* candidate, not the raw text. Querying "$TSLA"
    # verbatim ranks foreign depositary receipts above the real listing.
    try:
        data = _get(_SEARCH, {"q": candidate, "quotesCount": 8, "newsCount": 0})
        quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
    except DataError:
        quotes = []
    if not quotes and candidate != query.strip().upper():
        try:
            data = _get(_SEARCH, {"q": query, "quotesCount": 8, "newsCount": 0})
            quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
        except DataError:
            quotes = []

    def pack(q):
        return {"symbol": q["symbol"],
                "name": q.get("shortname") or q.get("longname") or q["symbol"],
                "type": (q.get("quoteType") or "").lower()}

    for q in quotes:                                   # exact match wins
        if q["symbol"].upper() == candidate:
            return pack(q)

    preferred = ("cryptocurrency", "equity", "etf", "index", "future")

    def rank(q):
        t = (q.get("quoteType") or "").lower()
        return preferred.index(t) if t in preferred else 99

    ranked = sorted(quotes, key=rank)
    if ranked:
        return pack(ranked[0])
    return {"symbol": candidate, "name": candidate, "type": ""}


def _classify(instrument_type: str) -> str:
    t = (instrument_type or "").upper()
    if "CRYPTO" in t:
        return "crypto"
    if t in ("EQUITY", "ETF", "MUTUALFUND"):
        return "equity"
    return "other"


def fetch_bars(symbol: str, interval: str = "5m", lookback: Optional[str] = None) -> Bars:
    """Download an OHLCV series and clean it into aligned NumPy arrays."""
    interval = interval.lower()
    if interval not in _MAX_RANGE:
        raise DataError("unsupported interval %r. Use one of: %s"
                        % (interval, ", ".join(sorted(_MAX_RANGE))))
    rng = lookback or _MAX_RANGE.get(interval, "60d")
    try:
        payload = _get(_CHART.format(sym=symbol),
                       {"range": rng, "interval": interval, "includePrePost": "false"})
    except DataError as exc:
        # A raw URL and status code helps nobody. Say what actually went wrong.
        if "404" in str(exc):
            raise DataError("no instrument found for %r. Check the ticker, and "
                            "note that crypto needs the -USD suffix, as in "
                            "BTC-USD." % symbol)
        raise DataError("could not fetch %s: %s" % (symbol, exc))

    chart = payload.get("chart") or {}
    if chart.get("error"):
        desc = chart["error"].get("description", "unknown upstream error")
        raise DataError("%s: %s" % (symbol, desc))
    results = chart.get("result") or []
    if not results:
        raise DataError("%s: no data returned" % symbol)
    res = results[0]
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not ts or not quote.get("close"):
        raise DataError("%s: empty price series at interval %s" % (symbol, interval))

    def col(key) -> np.ndarray:
        vals = list(quote.get(key) or [])
        vals += [None] * (len(ts) - len(vals))
        return np.array([np.nan if v is None else float(v) for v in vals], dtype=float)

    o, h, l, c, v = col("open"), col("high"), col("low"), col("close"), col("volume")
    t = np.array(ts, dtype=np.int64)

    # Yahoo interleaves null bars for halts and illiquid periods. Drop any bar
    # without a close, then fill the remaining OHLC gaps from that close so the
    # indicator maths never sees a NaN mid-series.
    keep = ~np.isnan(c)
    if int(keep.sum()) < 30:
        raise DataError("%s: only %d usable bars at %s" % (symbol, int(keep.sum()), interval))
    t, o, h, l, c, v = t[keep], o[keep], h[keep], l[keep], c[keep], v[keep]
    o = np.where(np.isnan(o), c, o)
    h = np.where(np.isnan(h), np.maximum(o, c), h)
    l = np.where(np.isnan(l), np.minimum(o, c), l)
    v = np.nan_to_num(v, nan=0.0)

    # Yahoo appends a synthetic session-close bar with zero volume and zero
    # range (O==H==L==C). Left in place it deflates ATR and zeroes RVOL, which
    # would size every stop far too tightly. Trim any such stubs off the tail.
    live = None
    while len(t) > 2 and v[-1] == 0 and h[-1] == l[-1]:
        live = float(c[-1])
        t, o, h, l, c, v = t[:-1], o[:-1], h[:-1], l[:-1], c[:-1], v[:-1]

    # The newest remaining bar may still be forming: partial volume and a close
    # that keeps moving. Signals computed on it flicker between runs. Split it
    # off as the live price and evaluate everything else on completed bars.
    step = _INTERVAL_SEC.get(interval, 300)
    if len(t) > 2 and (int(t[-1]) + step) > int(time.time()):
        live = float(c[-1])
        t, o, h, l, c, v = t[:-1], o[:-1], h[:-1], l[:-1], c[:-1], v[:-1]

    gmt = int(meta.get("gmtoffset") or 0)
    session = ((t + gmt) // 86400).astype(np.int64)

    # Crypto never closes; equities do. Knowing which lets the report say
    # plainly that a reading is from the previous session rather than live.
    asset = _classify(meta.get("instrumentType"))
    if asset == "crypto":
        is_open = True
    else:
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        now = int(time.time())
        is_open = bool(regular.get("start", 0) <= now <= regular.get("end", 0))

    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(t[-1]) + gmt))

    # Derive the reference close from our own bars. Yahoo's chartPreviousClose
    # is the close before the *whole requested range*, so on a 60-day pull it
    # reports a two-month change and labels it as the daily move.
    prev_sessions = np.unique(session)
    if len(prev_sessions) >= 2:
        prior_mask = session == prev_sessions[-2]
        prior_close = float(c[prior_mask][-1])
    else:
        prior_close = meta.get("chartPreviousClose") or meta.get("previousClose")

    return Bars(
        symbol=meta.get("symbol", symbol),
        name=meta.get("shortName") or meta.get("longName") or meta.get("symbol", symbol),
        asset_class=asset,
        currency=meta.get("currency", "USD"),
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        interval=interval,
        timestamp=t, open=o, high=h, low=l, close=c, volume=v,
        session_id=session,
        gmt_offset=gmt,
        prev_close=prior_close,
        live_price=live if live is not None else meta.get("regularMarketPrice"),
        market_open=is_open,
        last_bar_time=stamp,
        meta=meta,
    )
