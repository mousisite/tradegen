"""Shared authenticated session for Yahoo Finance endpoints.

Yahoo's fundamentals, options, bulk-quote and screener endpoints all reject
anonymous requests with 401 "Invalid Crumb". Access needs two things: a session
cookie picked up from the site, and a short "crumb" token fetched with that
cookie. Both then travel with every request.

That handshake is done once, lazily, and shared by every caller. It is also
repeated automatically if a request comes back 401, because the crumb expires.

The plain chart endpoint in `market.py` does not need any of this, which is why
price history kept working while everything else returned 401.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_SEED_URLS = ("https://fc.yahoo.com", "https://finance.yahoo.com")
_CRUMB_URLS = ("https://query1.finance.yahoo.com/v1/test/getcrumb",
               "https://query2.finance.yahoo.com/v1/test/getcrumb")

_LOCK = threading.Lock()
_SESSION: Optional[requests.Session] = None
_CRUMB: Optional[str] = None
_OBTAINED_AT: float = 0.0
_TTL = 30 * 60          # refresh the handshake every half hour


class YahooError(RuntimeError):
    """Raised when Yahoo cannot be reached or refuses the request."""


def _handshake() -> None:
    """Build a session, collect cookies, then fetch a crumb. Caller holds lock."""
    global _SESSION, _CRUMB, _OBTAINED_AT

    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA,
        "Accept": "application/json,text/plain,text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    for url in _SEED_URLS:
        try:
            session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException:
            continue      # fc.yahoo.com answers 404 but still sets the cookie

    crumb = None
    for url in _CRUMB_URLS:
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException:
            continue
        text = (r.text or "").strip()
        # A crumb is a short opaque token. An HTML body means we were bounced
        # to a consent page instead, so it is not usable.
        if r.status_code == 200 and text and "<" not in text and len(text) < 40:
            crumb = text
            break

    if not crumb:
        raise YahooError(
            "Could not obtain a Yahoo session token. This usually means no "
            "internet connection, or Yahoo is blocking this network.")

    _SESSION, _CRUMB, _OBTAINED_AT = session, crumb, time.time()


def _ensure(force: bool = False) -> None:
    global _OBTAINED_AT
    with _LOCK:
        stale = (time.time() - _OBTAINED_AT) > _TTL
        if force or _SESSION is None or _CRUMB is None or stale:
            _handshake()


def get(url: str, params: Optional[Dict] = None, tries: int = 3) -> Dict:
    """GET an authenticated Yahoo endpoint and return parsed JSON.

    A 401 means the crumb went stale, so the handshake is redone once and the
    request retried rather than surfacing a confusing authorisation error.
    """
    last = None
    for attempt in range(tries):
        _ensure(force=(attempt > 0 and last == 401))
        query = dict(params or {})
        query["crumb"] = _CRUMB
        try:
            r = _SESSION.get(url, params=query, timeout=25)
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(0.5 * (attempt + 1))
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise YahooError("Yahoo returned something that was not JSON.")
        last = r.status_code
        if r.status_code == 404:
            raise YahooError("Yahoo has no data at %s" % url.rsplit("/", 1)[-1])
        time.sleep(0.4 * (attempt + 1))

    raise YahooError("Yahoo request failed after %d tries (last: %s)" % (tries, last))


def quote_summary(symbol: str, modules: str) -> Dict:
    """One instrument's fundamental modules, already unwrapped."""
    data = get("https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s" % symbol,
               {"modules": modules})
    result = ((data.get("quoteSummary") or {}).get("result") or [])
    if not result:
        err = ((data.get("quoteSummary") or {}).get("error") or {})
        raise YahooError(err.get("description") or "No fundamental data for %s" % symbol)
    return result[0]


def quotes(symbols) -> list:
    """Live quote rows for many symbols at once. Chunked to stay under URL limits."""
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [s for s in symbols if s]
    out = []
    for start in range(0, len(symbols), 50):
        batch = symbols[start:start + 50]
        data = get("https://query1.finance.yahoo.com/v7/finance/quote",
                   {"symbols": ",".join(batch)})
        out.extend(((data.get("quoteResponse") or {}).get("result") or []))
    return out


def options(symbol: str, expiry: Optional[int] = None) -> Dict:
    """Option chain for one expiry, or the nearest one when none is given."""
    params = {"date": expiry} if expiry else {}
    data = get("https://query1.finance.yahoo.com/v7/finance/options/%s" % symbol, params)
    result = ((data.get("optionChain") or {}).get("result") or [])
    if not result:
        raise YahooError("No options listed for %s" % symbol)
    return result[0]


def predefined_screen(screen_id: str, count: int = 50) -> list:
    """Rows from one of Yahoo's own saved screens, such as day_gainers."""
    data = get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
               {"scrIds": screen_id, "count": count})
    result = ((data.get("finance") or {}).get("result") or [])
    return result[0].get("quotes", []) if result else []


def fmt(node, key: str, default=None):
    """Pull a value out of Yahoo's {raw, fmt, longFmt} wrappers.

    Yahoo returns numbers as objects in some modules and bare values in others,
    and missing fields as empty dicts rather than nulls. This normalises all
    three so callers never have to guess.
    """
    if not isinstance(node, dict):
        return default
    value = node.get(key)
    if isinstance(value, dict):
        raw = value.get("raw")
        return default if raw is None else raw
    if value is None or value == {}:
        return default
    return value
