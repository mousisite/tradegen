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

from . import upstream

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

# After a handshake fails, how long to answer without trying again. Every
# caller used to repeat the whole handshake in turn behind the lock, so a page
# that needed fundamentals, options and the calendar waited for each of them.
_FAILED_AT: float = 0.0
_RETRY_AFTER_FAILURE = 60.0
# Per request in the handshake. These only fetch a cookie and a short token;
# a source that has not answered in this long is not going to.
_HANDSHAKE_TIMEOUT = 5

_NO_SESSION = ("Could not obtain a Yahoo session token. This usually means no "
               "internet connection, or Yahoo is blocking this network.")


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
            session.get(url, timeout=_HANDSHAKE_TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            continue      # fc.yahoo.com answers 404 but still sets the cookie

    crumb = None
    for url in _CRUMB_URLS:
        try:
            r = session.get(url, timeout=_HANDSHAKE_TIMEOUT)
        except requests.RequestException:
            continue
        if upstream.is_throttle(r.status_code):
            # The same refusal the data requests would get, so it counts
            # towards closing the door on them too, and the other address is
            # not tried: asking again is what turns a limit into a block.
            upstream.note_throttled(_HOST, upstream.retry_after_seconds(r))
            break
        text = (r.text or "").strip()
        # A crumb is a short opaque token. An HTML body means we were bounced
        # to a consent page instead, so it is not usable.
        if r.status_code == 200 and text and "<" not in text and len(text) < 40:
            crumb = text
            break

    if not crumb:
        raise YahooError(_NO_SESSION)

    _SESSION, _CRUMB, _OBTAINED_AT = session, crumb, time.time()


def _ensure(force: bool = False, seen: Optional[str] = None) -> None:
    """Make sure there is a session and crumb, doing the handshake if needed.

    One caller does the handshake while the rest wait on the lock, and then
    use its result: a new crumb, or, if it failed, an immediate answer rather
    than a handshake of their own. `seen` is the crumb a caller was refused
    with; if it has been replaced meanwhile, there is nothing to redo.
    """
    global _FAILED_AT
    with _LOCK:
        have = _SESSION is not None and _CRUMB is not None
        if force and have and seen is not None and _CRUMB != seen:
            return
        if have and not force and (time.time() - _OBTAINED_AT) <= _TTL:
            return
        if time.time() - _FAILED_AT < _RETRY_AFTER_FAILURE:
            # It failed moments ago. A crumb past its half hour usually still
            # works, so keep using it; with none, say so straight away.
            if have and not force:
                return
            raise YahooError(_NO_SESSION)
        try:
            _handshake()
        except YahooError:
            _FAILED_AT = time.time()
            if have and not force:
                return
            raise


_HOST = "Yahoo Finance"


def get(url: str, params: Optional[Dict] = None, tries: int = 3) -> Dict:
    """GET an authenticated Yahoo endpoint, cached and de-duplicated.

    A 401 means the crumb went stale, so the handshake is redone once and the
    request retried rather than surfacing a confusing authorisation error.
    """
    key = "yahoo|%s|%s" % (url, sorted((params or {}).items()))
    try:
        return upstream.cached(key, lambda: _get_once(url, params, tries),
                               host=_HOST)
    except upstream.Throttled as exc:
        raise YahooError(str(exc))


def _get_once(url: str, params: Optional[Dict] = None, tries: int = 3) -> Dict:
    last, used = None, None
    for attempt in range(tries):
        _ensure(force=(attempt > 0 and last == 401), seen=used)
        session, used = _SESSION, _CRUMB
        query = dict(params or {})
        query["crumb"] = used
        try:
            r = session.get(url, params=query, timeout=25)
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(0.5 * (attempt + 1))
            continue

        if r.status_code == 200:
            try:
                parsed = r.json()
            except ValueError:
                raise YahooError("Yahoo returned something that was not JSON.")
            upstream.note_ok(_HOST)
            return parsed
        last = r.status_code
        if r.status_code == 404:
            raise YahooError("Yahoo has no data at %s" % url.rsplit("/", 1)[-1])
        if upstream.is_throttle(r.status_code):
            # Retrying into a rate limit makes it worse for everyone sharing
            # this address, so stop here and let the breaker hold the door.
            upstream.note_throttled(_HOST, upstream.retry_after_seconds(r))
            raise YahooError(
                "Yahoo is rate limiting this server (HTTP %s). Wait a minute "
                "and try again; the limit is shared by everyone using this "
                "installation." % r.status_code)
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
