"""Market data access.

One upstream source (Yahoo Finance's public chart endpoint) covers equities,
ETFs and crypto, which keeps the bot key-free. The same endpoint reports the
instrument type, so the rest of the pipeline can adapt its rules per asset
class without the user telling it what they pasted in.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote as _url_quote

import numpy as np
import requests

from . import upstream

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

# Each coin's id at Yahoo. Most are the ticker with -USD on the end, but where
# an older coin already had the ticker Yahoo files the newer one under a
# number: its ARB-USD is "ARbit" and XBT-USD "XBIT", not Arbitrum and Bitcoin,
# and SUI-USD, UNI-USD, APT-USD, PEPE-USD and MATIC-USD hold nothing at all.
# Polygon's coin is POL now, and Yahoo lists Toncoin as Gram.
_COINS = {
    "BTC": "BTC-USD", "XBT": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "XRP": "XRP-USD", "ADA": "ADA-USD", "DOGE": "DOGE-USD", "AVAX": "AVAX-USD",
    "DOT": "DOT-USD", "MATIC": "POL28321-USD", "LINK": "LINK-USD",
    "LTC": "LTC-USD", "BCH": "BCH-USD", "SHIB": "SHIB-USD", "TRX": "TRX-USD",
    "UNI": "UNI7083-USD", "ATOM": "ATOM-USD", "XLM": "XLM-USD",
    "NEAR": "NEAR-USD", "APT": "APT21794-USD", "ARB": "ARB11841-USD",
    "OP": "OP-USD", "SUI": "SUI20947-USD", "PEPE": "PEPE24478-USD",
    "TON": "GRAM-USD", "ICP": "ICP-USD", "FIL": "FIL-USD", "HBAR": "HBAR-USD",
    "VET": "VET-USD", "INJ": "INJ-USD",
}

# A bare ticker that is also a coin's is the hard case. "btc" typed into an
# alert means Bitcoin, not the Grayscale fund listed as BTC, but SUI is Sun
# Communities to anyone who owns it. The rule: a bare ticker is the coin,
# unless a US stock or fund trades under the same ticker and the coin is not
# one this site names by that ticker itself. These are the coin tickers where
# the listing wins (as Yahoo listed them in November 2026). BTC, ETH, SOL, XRP,
# DOGE, ADA, AVAX, LINK and LTC stay coins: they are the coins on the public
# pages (public.POPULAR), which show each by its bare ticker, so typing what
# the site shows has to open what it showed. The funds listed as BTC, ETH and
# XRP, Interlink Electronics (LINK) and LTC Properties (LTC) are reached by
# Yahoo's exact symbol instead: a pick from the suggestions is sent marked
# exact (search), and so is every link the site makes to one (needs_exact).
# Each coin below stays one step away, in the forms that can only mean a coin
# (SUI-USD, SUIUSD, SUI/USD, BINANCE:SUIUSDT).
_LISTED_FIRST = {"BCH", "TRX", "ATOM", "NEAR", "APT", "ARB", "SUI", "VET"}


class DataError(RuntimeError):
    """Raised when market data cannot be retrieved or is unusable."""


class SymbolNotFound(DataError):
    """The source has nothing under that symbol."""


class DataThrottled(DataError):
    """The source turned requests away for now, or has stopped answering and
    is being left alone; the request itself was fine.

    Still a DataError, so every caller that copes with missing prices copes
    with this, but its own kind, so a rate limit is never read as a ticker
    that does not exist. `seconds` is roughly how long to leave it.
    """

    def __init__(self, message: str, seconds: float = upstream.COOLDOWN):
        super().__init__(message)
        self.seconds = max(1, int(round(seconds)))


class DataUnavailable(DataError):
    """The source did not answer: it timed out, could not be reached, or
    answered with a server error.

    Like a refusal it is a wait, not a ticker that does not exist, so pages
    say so rather than suggest checking the ticker. Nothing says how long.
    """


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
    # Daily bars only: each bar's dividend factor (adjusted close over close),
    # 1.0 for the latest. Prices stay as traded, because stops, alerts and the
    # public record compare them with prices people saw; the back-test reads
    # this so an ex-dividend drop is not scored as a loss on a long.
    dividend_factor: Optional[np.ndarray] = None
    # Share splits inside the range fetched, oldest first, as (when, new
    # shares per old share): 10.0 is a ten-for-one, 0.1 a one-for-ten. The
    # prices above are already adjusted for them; this is for anything that
    # holds a price recorded before one.
    splits: List[Tuple[int, float]] = field(default_factory=list)
    # Start times of daily coin bars the source left blank and that were
    # rebuilt from their neighbours. A coin's day opens at the previous day's
    # close, so their open and close are exact; their range is only what
    # those two span, and their volume is not known and is left at nothing.
    rebuilt: List[int] = field(default_factory=list)

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


_HOST = "Yahoo Finance"

# Seconds to connect and to wait for each part of the answer, and how many
# tries. Together well inside the 30 seconds upstream.cached lets other callers
# wait for this one: a request that could outlast that wait sent every waiting
# caller to fetch for itself, which is the stampede the wait exists to stop.
# The source answers in well under a second when it answers at all.
_TIMEOUT = (5, 8)
_TRIES = 2


def _refused(status: int, seconds: Optional[float]) -> DataThrottled:
    """The error for a request the source turned away, said in words."""
    # Capped as the breaker caps it, so the wait said is the wait kept.
    wait = min(seconds if seconds and seconds > 0 else upstream.COOLDOWN, 900.0)
    when = upstream._plainly(int(round(wait)))
    if status == 429:
        text = ("The market data source limits how often this site may ask for "
                "prices, and turned this request away. Try again in about %s. "
                "The limit is shared by everyone using the site right now, so "
                "it is nothing to do with your account." % when)
    else:
        text = ("The market data source is busy or briefly down, and did not "
                "answer. Try again in about %s. It is nothing to do with your "
                "account." % when)
    return DataThrottled(text, wait)


def _fetch(url: str, params: dict, tries: int = _TRIES) -> dict:
    """One request, with a retry that stops when the source says stop.

    What went wrong is said in words. The address asked, query and all, used
    to be the message, and a person reading it cannot act on a URL.
    """
    last, answered = "it did not answer", False
    for attempt in range(tries):
        if attempt:
            time.sleep(0.6 * attempt)
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.Timeout:
            last = "it took too long to answer"
            continue
        except requests.RequestException:
            last = "it could not be reached"
            continue
        answered = True
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                last = "it sent something that was not price data"
                continue
            upstream.note_ok(_HOST)
            return data
        if upstream.is_throttle(r.status_code):
            # Retrying into a rate limit is how a shared address gets
            # blocked rather than throttled. Record it and give up now.
            wait = upstream.retry_after_seconds(r)
            upstream.note_throttled(_HOST, wait, r.status_code)
            raise _refused(r.status_code, wait)
        if r.status_code == 404:
            # Its own kind, so a caller can tell "no such ticker" from any
            # other failure without reading the message. It did answer, so
            # whatever was wrong with the source has passed.
            upstream.note_ok(_HOST)
            raise SymbolNotFound("The data source has nothing under that symbol.")
        if r.status_code == 422:
            # A range the source does not serve at that bar size: asking
            # again gets the same answer.
            upstream.note_ok(_HOST)
            raise DataError("The market data source could not be used: it does "
                            "not serve that range of bars (HTTP 422).")
        last = "it answered with an error (HTTP %d)" % r.status_code
    # With the source down every request used its full time on one of the
    # server's few threads. No answer on any try counts towards leaving it be.
    if answered:
        upstream.note_unclear(_HOST)
    else:
        upstream.note_failed(_HOST)
    raise DataUnavailable("The market data source could not be used: %s." % last)


def _get(url: str, params: dict, tries: int = _TRIES) -> dict:
    """A request that may be answered from a recent identical one.

    Keyed on the full query, so two callers asking for different ranges of the
    same instrument are correctly treated as different requests.
    """
    key = "chart|%s|%s" % (url, sorted(params.items()))
    try:
        return upstream.cached(key, lambda: _fetch(url, params, tries),
                               host=_HOST)
    except upstream.Throttled as exc:
        # The breaker is open, so nothing was sent; its message says so.
        raise DataThrottled(str(exc), exc.seconds)


def _chart_url(symbol: str) -> str:
    """The chart address for a symbol. Quoted, so that what follows a # or ?
    in one is not dropped and another instrument's prices sent back."""
    return _CHART.format(sym=_url_quote(symbol, safe="^=&"))


def normalise_symbol(raw: str, exact: bool = False) -> str:
    """Turn loose user or screenshot text into a Yahoo-style ticker.

    Handles the shapes a chart screenshot actually shows: BTCUSD, BTC/USD,
    BINANCE:ETHUSDT, BTC-USDT, NASDAQ:AAPL, a leading dollar-sign cashtag,
    and a share class after a dot (BRK.B, which Yahoo writes BRK-B). A coin
    in any of the shapes that can only mean a coin becomes Yahoo's id for it;
    a bare ticker follows the rule above _LISTED_FIRST.

    exact is for Yahoo's own symbol, as a pick from the suggestions sends it.
    It is only tidied, never read as a coin, so LINK stays Interlink
    Electronics and BTC the fund listed under that ticker.
    """
    s = (raw or "").strip().upper()
    if not s:
        raise DataError("empty symbol")
    if exact:
        return s.replace(" ", "")
    if ":" in s:                       # strip exchange prefix, e.g. NASDAQ:AAPL
        s = s.split(":", 1)[1]
    s = s.lstrip("$").replace(" ", "")
    if not s:                          # "$" or "NASDAQ:" alone
        raise DataError("empty symbol")
    if "/" in s:                       # BTC/USD -> BTC-USD
        base, _, quote = s.partition("/")
        if quote in ("USDT", "USDC", "USD", ""):
            return _COINS.get(base, base + "-USD")
        return base + "-" + quote
    if s.endswith("-USD"):
        # Yahoo's own id is used as given, except a coin's ticker with -USD
        # added, which Yahoo files elsewhere or under another coin.
        return _COINS.get(s[:-4], s)
    coin = re.match(r"([A-Z0-9]+)[-_](?:USDT|USDC|USD)$", s)
    if coin:                           # BTC-USDT, ETH_USDT -> the dollar price
        return _COINS.get(coin.group(1), coin.group(1) + "-USD")
    if re.match(r"[A-Z]{1,5}\.[AB]$", s):
        # A share class. No exchange Yahoo lists ends its symbols .A or .B.
        return s[:-2] + "-" + s[-1]
    for suffix in ("USDT", "USDC", "USD"):
        if s.endswith(suffix) and s[:-len(suffix)] in _COINS:
            return _COINS[s[:-len(suffix)]]
    if s in _COINS and s not in _LISTED_FIRST:
        return _COINS[s]
    return s


def coin_ticker(symbol: str) -> Optional[str]:
    """The ticker people call a coin by, from Yahoo's id for it: SUI for
    SUI20947-USD, TON for GRAM-USD. None for anything that is not a coin.
    """
    if not symbol.upper().endswith("-USD"):
        return None
    base = symbol.upper()[:-4]
    numbered = re.match(r"([A-Z]+)\d{4,}$", base)
    if numbered:
        return numbered.group(1)
    if base not in _COINS:
        for ticker, its_id in _COINS.items():
            if its_id == base + "-USD":
                return ticker
    return base


def needs_exact(symbol: str) -> bool:
    """Whether Yahoo's symbol, typed bare, would be read as something else.

    LINK is Interlink Electronics at Yahoo, but a bare LINK is the Chainlink
    coin here. A link to the listing, or an alert or watchlist entry saved for
    it, has to say that it means the symbol exactly. AAPL or BTC-USD reads the
    same either way, and needs nothing.
    """
    try:
        own = normalise_symbol(symbol, exact=True)
        return normalise_symbol(own) != own
    except DataError:
        return False


def resolve(query: str, exact: bool = False) -> dict:
    """Look up a ticker or company name and return the best matching instrument.

    Falls back to the normalised string itself when search returns nothing, so
    a valid-but-unindexed symbol still gets a chance at a data fetch.

    With exact, the query is Yahoo's own symbol and the answer is that symbol
    and no other. The search only supplies its name, which the news search
    reads: "Interlink Electronics" finds the company's news, "LINK" the coin's.
    """
    candidate = normalise_symbol(query, exact=exact)
    # Search the *normalised* candidate, not the raw text. Querying "$TSLA"
    # verbatim ranks foreign depositary receipts above the real listing.
    # A refusal is passed on rather than guessed past. The guess sends one
    # more request into the limit, and a company name guessed as a ticker
    # ("TESLA") comes back as a ticker that does not exist.
    # A search that did not answer is kept: if the guess it leaves then turns
    # out not to exist, the caller says the source did not answer, not that
    # the ticker is wrong (engine.analyse).
    search_down = None
    try:
        data = _get(_SEARCH, {"q": candidate, "quotesCount": 8, "newsCount": 0})
        quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
    except DataThrottled:
        raise
    except DataUnavailable as exc:
        search_down, quotes = exc, []
    except DataError:
        quotes = []
    if not quotes and not exact and candidate != query.strip().upper():
        try:
            data = _get(_SEARCH, {"q": query, "quotesCount": 8, "newsCount": 0})
            quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
        except DataThrottled:
            raise
        except DataUnavailable as exc:
            search_down, quotes = exc, []
        except DataError:
            quotes = []

    def pack(q):
        return {"symbol": q["symbol"],
                "name": q.get("shortname") or q.get("longname") or q["symbol"],
                "type": (q.get("quoteType") or "").lower()}

    for q in quotes:                                   # exact match wins
        if q["symbol"].upper() == candidate:
            return pack(q)
    if exact:
        # Never another instrument. Nor is the symbol a guess, so one the
        # source turns out not to have is said as such, search or no search.
        return {"symbol": candidate, "name": candidate, "type": ""}

    # A company and a coin keep the source's own order between them. The coin
    # used to come first, so a company's name whose results also listed a
    # token named after it ("gamestop") could open the token. A coin typed as
    # a coin has been read as one already (normalise_symbol).
    preferred = {"cryptocurrency": 0, "equity": 0, "etf": 1, "index": 2, "future": 3}

    def rank(q):
        return preferred.get((q.get("quoteType") or "").lower(), 99)

    ranked = sorted(quotes, key=rank)
    if ranked:
        return pack(ranked[0])
    guess = {"symbol": candidate, "name": candidate, "type": ""}
    if search_down is not None:
        guess["search_down"] = search_down
    return guess


# What an analysis can run on. Options, currencies and the like are left out
# of suggestions rather than offered and then refused.
_SUGGEST_KINDS = {"equity", "etf", "cryptocurrency", "index"}


def search(query: str, limit: int = 6) -> list:
    """Instruments matching a half-typed ticker or name, best first.

    For the dropdown under a search box: an empty list rather than an error
    when the lookup fails, because a missing suggestion must never stop
    someone typing the ticker themselves. Answers are cached by _get, so the
    same few letters typed twice ask Yahoo once.

    What is picked here is analysed exactly as picked. An entry whose ticker
    this app would read as something else, such as Interlink Electronics
    under LINK beside the Chainlink coin, carries exact, and the box sends it
    marked so. A coin says it is one: Yahoo calls it by its dollar price
    ("Chainlink USD") and lists it on an exchange code of its own (CCC),
    which tells a person choosing between a coin and a company nothing.
    """
    text = (query or "").strip()
    if not text:
        return []
    try:
        data = _get(_SEARCH, {"q": text, "quotesCount": 10, "newsCount": 0})
    except DataError:
        return []
    out = []
    for q in data.get("quotes", []):
        kind = (q.get("quoteType") or "").lower()
        if not q.get("symbol") or kind not in _SUGGEST_KINDS:
            continue
        name = q.get("shortname") or q.get("longname") or q["symbol"]
        where = q.get("exchDisp") or ""
        if kind == "cryptocurrency":
            if name.upper().endswith(" USD"):
                name = name[:-4].rstrip() or name
            where = "coin"
        out.append({"symbol": q["symbol"], "name": name, "where": where,
                    "exact": needs_exact(q["symbol"])})
    return out[:limit]


def _classify(instrument_type: str) -> str:
    t = (instrument_type or "").upper()
    if "CRYPTO" in t:
        return "crypto"
    if t in ("EQUITY", "ETF", "MUTUALFUND"):
        return "equity"
    return "other"


def _still_forming(start: int, step: int, interval: str, asset: str,
                   regular: dict, gmt: int, now: int) -> bool:
    """Whether the bar that opened at `start` is still being traded.

    A bar is stamped with the time it opened. An intraday bar, and a coin's
    round-the-clock day, lasts exactly its interval, so it is over once that
    much time has passed. A stock's daily bar is stamped at the session's open
    but lasts only the session, six and a half hours, not 24. Read the same
    way, the session that closed at 4pm counted as unfinished until the next
    morning's open, so every evening and overnight analysis ran on the session
    before, with today's close as its price.

    So for daily bars off crypto the exchange's own trading hours decide: the
    bar of the session Yahoo describes is forming only until that session
    closes, and a bar from an earlier day is finished. A bar dated after the
    session described cannot be placed, and keeps the length-of-a-day rule.
    """
    s_start, s_end = regular.get("start"), regular.get("end")
    if interval == "1d" and asset != "crypto" and s_start and s_end:
        bar_day = (start + gmt) // 86400
        session_day = (int(s_start) + gmt) // 86400
        if bar_day < session_day:
            return False
        if bar_day == session_day:
            return now < int(s_end)
    return start + step > now


def _size_word(interval: str) -> str:
    """A bar size as people say it: daily, hourly, 5-minute."""
    return {"1d": "daily", "1h": "hourly", "60m": "hourly"}.get(
        interval, interval.replace("m", "-minute"))


def _too_few(symbol: str, count: int, interval: str) -> DataError:
    """The error for an instrument with too little history, in words."""
    return DataError("%s has only %d %s bar%s of history, and an analysis needs "
                     "at least 30." % (symbol, count, _size_word(interval), "" if count == 1 else "s"))


def fetch_bars(symbol: str, interval: str = "5m", lookback: Optional[str] = None) -> Bars:
    """Download an OHLCV series and clean it into aligned NumPy arrays."""
    interval = interval.lower()
    if interval not in _MAX_RANGE:
        raise DataError("unsupported interval %r. Use one of: %s"
                        % (interval, ", ".join(sorted(_MAX_RANGE))))
    rng = lookback or _MAX_RANGE.get(interval, "60d")
    step = _INTERVAL_SEC.get(interval, 300)
    try:
        # Splits come with the prices, for anything that holds a price from
        # before one (Bars.splits).
        payload = _get(_chart_url(symbol),
                       {"range": rng, "interval": interval, "includePrePost": "false",
                        "events": "split"})
    except SymbolNotFound:
        raise SymbolNotFound("No instrument was found for %r. Check the ticker, and "
                             "note that crypto needs the -USD suffix, as in "
                             "BTC-USD." % symbol)
    except DataThrottled:
        # Told apart by its kind, not by reading the message: looking for
        # "404" in the text once reported a rate limit on 4043.T as a ticker
        # that does not exist. Its own words already say how long to wait.
        raise
    except DataUnavailable as exc:
        raise DataUnavailable("Prices for %s could not be fetched. %s" % (symbol, exc))
    except DataError as exc:
        raise DataError("Prices for %s could not be fetched. %s" % (symbol, exc))

    chart = payload.get("chart") or {}
    if chart.get("error"):
        desc = chart["error"].get("description", "unknown upstream error")
        raise DataError("%s: %s" % (symbol, desc))
    results = chart.get("result") or []
    if not results:
        raise DataError("The market data source sent no prices for %s." % symbol)
    res = results[0]
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not ts or not quote.get("close"):
        raise DataError("The market data source sent no %s prices for %s."
                        % (_size_word(interval), symbol))
    # Asked for five-minute bars of a fund priced once a day, Yahoo quietly
    # sends daily ones, and they were analysed as if each were five minutes.
    granted = str(meta.get("dataGranularity") or interval)
    if _INTERVAL_SEC.get(granted) != step:
        given = "daily" if granted == "1d" else granted
        fund = (meta.get("instrumentType") or "").upper() == "MUTUALFUND"
        raise DataError("%s has no %s bars at the data source, only %s ones%s. "
                        "Choose %s bars instead."
                        % (symbol, interval, given,
                           ": a mutual fund is priced once a day, after the close"
                           if fund else "", given))

    def col(key) -> np.ndarray:
        vals = list(quote.get(key) or [])
        vals += [None] * (len(ts) - len(vals))
        return np.array([np.nan if v is None else float(v) for v in vals], dtype=float)

    o, h, l, c, v = col("open"), col("high"), col("low"), col("close"), col("volume")
    t = np.array(ts, dtype=np.int64)

    gmt = int(meta.get("gmtoffset") or 0)
    asset = _classify(meta.get("instrumentType"))
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = int(time.time())

    # After the close Yahoo sometimes sends the finished session's daily bar
    # with everything but its close, and that close is the quote it reports
    # for the same session. Dropped for want of a close, the bar took the
    # session with it, and the evening's analysis ran on the day before. Only
    # a session that is over is filled, and only from a quote stamped on its
    # own day.
    quoted, quoted_at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    from_quote = False
    if (interval == "1d" and len(c) and np.isnan(c[-1]) and quoted and quoted_at
            and (int(quoted_at) + gmt) // 86400 == (int(t[-1]) + gmt) // 86400
            and not _still_forming(int(t[-1]), step, interval, asset, regular,
                                   gmt, now)):
        c[-1] = float(quoted)
        from_quote = True
        if np.isfinite(h[-1]):
            h[-1] = max(h[-1], c[-1])
        if np.isfinite(l[-1]):
            l[-1] = min(l[-1], c[-1])

    # A coin's finished day is sometimes left blank for an hour or more after
    # midnight UTC, with the new day already trading. Dropped, it took the day
    # out of every reading: the analysis ran on the day before it, and the
    # day's change was measured across two days. A coin's day opens at the
    # previous day's close, so a blank day between two known ones has its open
    # and close exactly. Its range and volume are not known yet: the range is
    # left at what those two span, and the volume at nothing.
    rebuilt: List[int] = []
    if interval == "1d" and asset == "crypto":
        for k in range(1, len(t) - 1):
            if (np.isnan(c[k]) and np.isfinite(c[k - 1]) and np.isfinite(o[k + 1])
                    and t[k] - t[k - 1] == step
                    # The day now trading is stamped with its last trade's
                    # time, not its midnight, so the dates are compared.
                    and t[k + 1] // step == t[k] // step + 1):
                if not np.isfinite(o[k]):
                    o[k] = c[k - 1]
                c[k] = o[k + 1]
                h[k] = np.nanmax([h[k], o[k], c[k]])
                l[k] = np.nanmin([l[k], o[k], c[k]])
                rebuilt.append(int(t[k]))

    # Each daily bar's dividend factor. Yahoo's close is already adjusted for
    # splits but not for dividends, so a stock paying 3% a year shows a small
    # drop on every ex-date that no trade could have avoided, and back-tested
    # longs were charged it as a loss (shorts were paid it). The factor lets
    # the back-test count the dividend without rewriting the traded prices.
    factor = None
    if interval == "1d":
        adj = (((res.get("indicators") or {}).get("adjclose") or [{}])[0]
               .get("adjclose"))
        if adj:
            vals = list(adj) + [None] * (len(ts) - len(adj))
            a = np.array([np.nan if x is None else float(x) for x in vals[:len(ts)]],
                         dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                factor = np.where((a > 0) & (c > 0), a / c, np.nan)
            # The newest bar is never adjusted: a dividend only rescales the
            # bars before its ex-date.
            if not np.isfinite(factor[-1]):
                factor[-1] = 1.0

    # Yahoo interleaves null bars for halts and illiquid periods. Drop any bar
    # without a close, then fill the remaining OHLC gaps from that close so the
    # indicator maths never sees a NaN mid-series.
    keep = ~np.isnan(c)
    if int(keep.sum()) < 30:
        raise _too_few(symbol, int(keep.sum()), interval)
    t, o, h, l, c, v = t[keep], o[keep], h[keep], l[keep], c[keep], v[keep]
    o = np.where(np.isnan(o), c, o)
    h = np.where(np.isnan(h), np.maximum(o, c), h)
    l = np.where(np.isnan(l), np.minimum(o, c), l)
    v = np.nan_to_num(v, nan=0.0)
    if factor is not None:
        factor = factor[keep]
        # A missing factor borrows the next known one: no dividend is assumed
        # to have gone ex on a day the source left blank.
        for k in range(len(factor) - 2, -1, -1):
            if not np.isfinite(factor[k]):
                factor[k] = factor[k + 1]
        if not np.isfinite(factor[-1]):
            factor = None

    # Yahoo appends a synthetic session-close bar with zero volume and zero
    # range (O==H==L==C). Left in place it deflates ATR and zeroes RVOL, which
    # would size every stop far too tightly. Trim it off the tail: one bar,
    # and only after a bar that traded, in a series that reports volume at
    # all. A fund priced once a day reports every bar flat and without
    # volume, and trimming "stubs" until one traded cut such a series to two
    # bars from 2021. A session closed from its own quote is a session, too.
    live = None
    if (len(t) > 2 and v[-1] == 0 and h[-1] == l[-1] and v[-2] > 0
            and float(np.median(v)) > 0 and not from_quote):
        live = float(c[-1])
        t, o, h, l, c, v = t[:-1], o[:-1], h[:-1], l[:-1], c[:-1], v[:-1]

    # The newest remaining bar may still be forming: partial volume and a close
    # that keeps moving. Signals computed on it flicker between runs. Split it
    # off as the live price and evaluate everything else on completed bars.
    live_start = None
    if len(t) > 2 and _still_forming(int(t[-1]), step, interval, asset, regular,
                                     gmt, now):
        live = float(c[-1])
        live_start = int(t[-1])
        t, o, h, l, c, v = t[:-1], o[:-1], h[:-1], l[:-1], c[:-1], v[:-1]
    # Counted again now the tail is off: thirty bars before it could be
    # twenty-eight after.
    if len(t) < 30:
        raise _too_few(symbol, len(t), interval)
    if factor is not None:
        factor = factor[:len(t)]
        factor = factor / factor[-1]

    session = ((t + gmt) // 86400).astype(np.int64)

    # Crypto never closes; equities do. Knowing which lets the report say
    # plainly that a reading is from the previous session rather than live.
    if asset == "crypto":
        is_open = True
    else:
        is_open = bool(regular.get("start", 0) <= now <= regular.get("end", 0))

    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(t[-1]) + gmt))

    # Derive the reference close from our own bars. Yahoo's chartPreviousClose
    # is the close before the *whole requested range*, so on a 60-day pull it
    # reports a two-month change and labels it as the daily move.
    #
    # The previous close belongs to the session before the live price's, not
    # before the last completed bar's. When the live price is from a newer day
    # than every completed bar (a daily bar split off while the market trades,
    # the first bar of a session, or a quote whose bar never came), the newest
    # completed bar closed the session before it. Counting back from the
    # completed bars instead reached a second session back: "down 1.91%" on a
    # day that was up 0.36%.
    if live_start is not None:
        live_day = (live_start + gmt) // 86400
    elif live is None and quoted_at:
        live_day = (int(quoted_at) + gmt) // 86400
    else:
        live_day = None
    prev_sessions = np.unique(session)
    if live_day is not None and live_day > session[-1]:
        prior_close = float(c[-1])
    elif len(prev_sessions) >= 2:
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
        dividend_factor=factor,
        splits=_splits(res),
        rebuilt=[x for x in rebuilt if x <= int(t[-1])],
    )


def split_factor(bars, since: int) -> float:
    """How many of today's shares one share held since `since` has become.

    Yahoo rescales prices from before a split by its ratio, so a price written
    down before one (a journal entry, an alert's level) is that many times
    today's quote. 1.0 when no split in the fetched range came after `since`:
    one older than the range cannot be seen here.
    """
    factor = 1.0
    for when, ratio in getattr(bars, "splits", None) or ():
        if int(when) > int(since) and ratio and float(ratio) > 0:
            factor *= float(ratio)
    return factor


def split_words(factor: float) -> str:
    """A split's ratio as people say it: 10-for-1, 3-for-2, 1-for-10."""
    from fractions import Fraction
    # A wide limit: two reverse splits can combine to 1-for-2000.
    ratio = Fraction(factor).limit_denominator(10 ** 6)
    return "%d-for-%d" % (ratio.numerator, ratio.denominator)


# How long a symbol's whole split history is kept, and for how many symbols.
SPLIT_HISTORY_TTL = 12 * 3600
SPLIT_HISTORY_KEPT = 2000
_SPLIT_HISTORY: Dict[str, Tuple[float, List[Tuple[int, float]]]] = {}
_SPLIT_HISTORY_LOCK = threading.Lock()


def split_history(symbol: str) -> List[Tuple[int, float]]:
    """Every split the source lists for a symbol, oldest first, as (when, ratio).

    Bars.splits holds only the splits inside the range fetched, so a price
    stored before an older one (a year-old position read with a year of
    bars, a 15-minute trade read with sixty days) could not be set beside
    today's quote. This asks for the whole history at a coarse bar size, a
    small answer, and keeps it for twelve hours in a store of its own: kept
    in the shared one, a twelve-hour entry held every forty-five-second chart
    there for as long, and was the first pushed out when it filled.

    Only splits older than the bars a caller holds are read from here
    (splits_since), so a copy kept from before today's split never hides it.
    """
    now = time.time()
    with _SPLIT_HISTORY_LOCK:
        kept = _SPLIT_HISTORY.get(symbol)
        if kept is not None and now - kept[0] < SPLIT_HISTORY_TTL:
            return list(kept[1])
    try:
        upstream.check(_HOST)
    except upstream.Throttled as exc:
        raise DataThrottled(str(exc), exc.seconds)
    payload = _fetch(_chart_url(symbol),
                     {"range": "max", "interval": "3mo", "events": "split"})
    results = (payload.get("chart") or {}).get("result") or []
    found = _splits(results[0]) if results else []
    with _SPLIT_HISTORY_LOCK:
        if symbol not in _SPLIT_HISTORY and len(_SPLIT_HISTORY) >= SPLIT_HISTORY_KEPT:
            del _SPLIT_HISTORY[min(_SPLIT_HISTORY, key=lambda k: _SPLIT_HISTORY[k][0])]
        _SPLIT_HISTORY[symbol] = (now, found)
    return list(found)


def splits_since(symbol: str, since: int, bars=None) -> List[Tuple[int, float]]:
    """The splits after `since`, oldest first, as (when, ratio).

    The splits inside the bars' own range come from the bars, which are
    fresh. Those before it, when the bars do not reach back to `since`, come
    from the whole history; a coin has none to look up. When that history is
    needed and the source does not answer, this raises DataThrottled or
    DataUnavailable: a split cannot be ruled out, and a caller that acts on
    the answer (a stop notice, an alert, a logged trade) waits rather than
    act on prices that may be ten times off. A history the source has no
    answer for at all leaves the bars' own.
    """
    since = int(since)
    have = [(int(when), float(ratio)) for when, ratio in getattr(bars, "splits", None) or ()]
    stamps = getattr(bars, "timestamp", None)
    # Bars that carry no times (stand-ins) cannot say how far back they
    # reach, so theirs are all there is; with no bars, the history is asked.
    timed = stamps is not None and len(stamps) > 0
    first = int(stamps[0]) if timed else None
    reaches = (bars is not None and not timed) or (timed and first <= since)
    if not reaches and not (symbol or "").upper().endswith("-USD"):
        try:
            history = split_history(symbol)
        except (DataThrottled, DataUnavailable):
            raise
        except DataError:
            history = []

        def listed(when: int, ratio: float) -> bool:
            # The same split already in the bars' own list: the bars can list
            # one dated before their first kept bar (a blank opening bar is
            # dropped, its split event is not), and the two answers may date
            # it a little apart. Counted twice, a 1-for-10 became 1-for-100.
            return any(abs(when - w) < 86400 and abs(ratio - r) <= 1e-9 * max(1.0, r)
                       for w, r in have)

        have = [(int(w), float(r)) for w, r in history
                if (first is None or int(w) < first)
                and not listed(int(w), float(r))] + have
    return sorted((when, ratio) for when, ratio in have if when > since and ratio > 0)


def split_factor_since(symbol: str, since: int, bars=None) -> float:
    """split_factor over every split since `since`, not only those in the bars."""
    factor = 1.0
    for _when, ratio in splits_since(symbol, since, bars):
        factor *= ratio
    return factor


def _splits(res: dict) -> List[Tuple[int, float]]:
    """The splits Yahoo lists with a chart, as (when, new over old), oldest first."""
    out = []
    listed = (res.get("events") or {}).get("splits") or {}
    for event in (listed.values() if isinstance(listed, dict) else listed):
        try:
            when = int(event["date"])
            ratio = float(event["numerator"]) / float(event["denominator"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio > 0 and np.isfinite(ratio):
            out.append((when, ratio))
    return sorted(out)
