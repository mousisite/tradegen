"""Market data, from payloads made by hand: what the source sends, what it
refuses, and what the app makes of each.

No network: where a download would happen, requests.get is replaced by a
stand-in that answers as the source does, and records what it was asked.
Each section pins one way the data used to mislead, and says how.
"""
import inspect
import os as _os
import sys as _sys
import threading
import time
import types

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

DB_PATH, _ = harness.isolate("market")

import numpy as np  # noqa: E402
import requests  # noqa: E402

from bot import market, upstream  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


DAY = 86400
D0 = 1_700_000_000 - (1_700_000_000 % DAY)          # a midnight UTC
OPEN = 14 * 3600 + 1800                              # 09:30 New York, in UTC

# The clock fetch_bars reads, and the pauses between its tries, recorded
# instead of slept.
NOW = [D0 + 400 * DAY]
SLEPT = []
market.time = types.SimpleNamespace(time=lambda: NOW[0], strftime=time.strftime,
                                    gmtime=time.gmtime, sleep=SLEPT.append)


class Answer:
    """What requests.get hands back, as far as market.py reads it."""

    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class Wire:
    """Stands in for requests.get: gives its answers in turn, the last one
    again once they run out, and keeps every request it was sent."""

    def __init__(self, *answers, delay=0.0):
        self.answers = list(answers)
        self.asked = []
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, url, params=None, headers=None, timeout=None):
        with self.lock:
            self.asked.append({"url": url, "params": dict(params or {}),
                               "timeout": timeout})
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if self.delay:
            time.sleep(self.delay)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def on(wire):
    """Send market.py's requests to `wire`, with nothing cached or refused."""
    upstream.reset()
    del SLEPT[:]
    market.requests = types.SimpleNamespace(
        get=wire, Timeout=requests.Timeout, RequestException=requests.RequestException)
    return wire


def chart(ts, closes, opens=None, highs=None, lows=None, volumes=None,
          itype="EQUITY", gmt=-18000, regular=None, granularity=None, splits=None,
          symbol="TEST"):
    """A chart answer shaped like Yahoo's. A None close is a blank bar."""
    n = len(ts)
    known = [x if x is not None else 100.0 for x in closes]
    blank = [x is None for x in closes]

    def col(given, default):
        vals = list(given) if given is not None else default
        return [None if b else v for v, b in zip(vals, blank)]

    quote = {"open": col(opens, known), "high": col(highs, [x * 1.01 for x in known]),
             "low": col(lows, [x * 0.99 for x in known]), "close": list(closes),
             "volume": col(volumes, [1000.0] * n)}
    meta = {"symbol": symbol, "instrumentType": itype, "gmtoffset": gmt,
            "currency": "USD", "regularMarketPrice": known[-1],
            "currentTradingPeriod": {"regular": regular} if regular else {}}
    if granularity is not None:
        meta["dataGranularity"] = granularity
    result = {"meta": meta, "timestamp": list(ts), "indicators": {"quote": [quote]}}
    if splits is not None:
        result["events"] = {"splits": splits}
    return {"chart": {"result": [result], "error": None}}


def daily_stock(n=40, **kw):
    """n finished daily sessions, the last one closed hours ago."""
    days = [D0 + k * DAY + OPEN for k in range(n)]
    last = days[-1]
    regular = {"start": last, "end": last + 23400}
    NOW[0] = last + 23400 + 3 * 3600
    return days, chart(days, [100.0 + k for k in range(n)], regular=regular, **kw)


# ---------------------------------------------------------------------------
head("A REFUSAL IS SAID IN WORDS, AND NEVER AS A MISSING TICKER")
# ---------------------------------------------------------------------------
# A rate limit used to reach the page as "could not fetch NVDA: request to
# https://query1.finance.yahoo.com/v8/finance/chart/NVDA failed: HTTP 429",
# and on 4043.T, whose name holds the digits 404, as "no instrument found".

def refused(symbol, wire, interval="1d"):
    on(wire)
    try:
        market.fetch_bars(symbol, interval)
    except market.DataError as exc:
        return exc
    return None


URLISH = ("http", "query1", "yahoo.com", "finance/chart", "?")

exc = refused("NVDA", Wire(Answer(429)))
check("a rate limit is its own kind of error", isinstance(exc, market.DataThrottled), repr(exc))
check("and still a DataError, so every caller that copes with missing data copes",
      isinstance(exc, market.DataError))
check("it says what happened in words, with no address in it",
      exc is not None and not any(u in str(exc) for u in URLISH), str(exc))
check("how long to wait, and that it is nothing the reader did",
      "Try again in about" in str(exc) and "nothing to do with your account" in str(exc),
      str(exc))
check("and carries the wait for anything that wants to send it on",
      getattr(exc, "seconds", None) == int(upstream.COOLDOWN), getattr(exc, "seconds", None))
wire = Wire(Answer(429))
refused("NVDA", wire)
check("a refusal is not retried into", len(wire.asked) == 1, len(wire.asked))

exc = refused("4043.T", Wire(Answer(429)))
check("a rate limit on 4043.T is not reported as a ticker that does not exist",
      isinstance(exc, market.DataThrottled) and "no instrument" not in str(exc), str(exc))

exc = refused("NVDA", Wire(Answer(503, headers={"Retry-After": "120"})))
check("a source that is down says so, with the wait it asked for",
      isinstance(exc, market.DataThrottled) and exc.seconds == 120
      and "2 minutes" in str(exc) and "busy or briefly down" in str(exc), str(exc))

exc = refused("ZZZZ", Wire(Answer(404, body={"chart": {"result": None, "error": {
    "code": "Not Found", "description": "No data found, symbol may be delisted"}}})))
check("a ticker the source does not have is its own kind too",
      isinstance(exc, market.SymbolNotFound) and "no instrument found for 'ZZZZ'" in str(exc),
      repr(exc))

days, good = daily_stock()
wire = on(Wire(requests.Timeout("read timed out"), Answer(200, good)))
bars = market.fetch_bars("TEST", "1d")
check("a slow answer is tried once more, and the second answer used",
      len(bars) == 40 and len(wire.asked) == 2, len(wire.asked))
check("with one short pause between, and none after the last try", SLEPT == [0.6], SLEPT)

exc = refused("TEST", Wire(requests.Timeout("read timed out")))
check("two slow answers are said plainly",
      exc is not None and "took too long to answer" in str(exc)
      and not any(u in str(exc) for u in URLISH), str(exc))
check("after two tries, not three", len(SLEPT) == 1, SLEPT)

exc = refused("TEST", Wire(Answer(200, None)))
check("a page that is not price data is an error in words, not a crash",
      isinstance(exc, market.DataError) and "not price data" in str(exc), repr(exc))

exc = refused("TEST", Wire(Answer(500)))
check("any other failure keeps its status, but not the address",
      "HTTP 500" in str(exc) and not any(u in str(exc) for u in URLISH), str(exc))

wire = Wire(Answer(422))
exc = refused("TEST", wire)
check("a range the source does not serve is not a missing ticker, and is not asked twice",
      not isinstance(exc, market.SymbolNotFound) and "no instrument" not in str(exc)
      and "HTTP 422" in str(exc) and len(wire.asked) == 1, (repr(exc), len(wire.asked)))

on(Wire(Answer(429)))
for _ in range(upstream.THRESHOLD):
    try:
        market.fetch_bars("NVDA", "5m")
    except market.DataError:
        pass
    upstream._cache.clear()
wire = Wire(Answer(200, good))
market.requests.get = wire
try:
    market.fetch_bars("NVDA", "1d")
    exc = None
except market.DataError as caught:
    exc = caught
check("once the source has refused three times, nothing more is sent",
      isinstance(exc, market.DataThrottled) and not wire.asked, (repr(exc), len(wire.asked)))
check("and the reader is told it was not sent", "not sent" in str(exc or ""), str(exc))

waits = inspect.signature(upstream.cached).parameters["wait"].default
worst = sum(market._TIMEOUT) * market._TRIES + sum(0.6 * k for k in range(1, market._TRIES))
check("a request gives up before the callers waiting on it would",
      worst < waits, "%.1fs against %.1fs" % (worst, waits))


# ---------------------------------------------------------------------------
head("EIGHT PEOPLE ASKING DURING A RATE LIMIT ARE ONE REQUEST")
# ---------------------------------------------------------------------------
# Callers waiting on a refused request each sent their own, outside the
# breaker, so eight people asking at once cost eight refusals.

wire = on(Wire(Answer(429), delay=0.4))
gate = threading.Barrier(9)
told = []


def ask():
    gate.wait(timeout=5)
    try:
        market.fetch_bars("NVDA", "1d")
        told.append(None)
    except market.DataError as caught:
        told.append(caught)


threads = [threading.Thread(target=ask) for _ in range(8)]
for t in threads:
    t.start()
gate.wait(timeout=5)
for t in threads:
    t.join(timeout=15)
check("eight at once cost the source one request", len(wire.asked) == 1, len(wire.asked))
check("and all eight are told, in words, that it was refused",
      len(told) == 8 and all(isinstance(e, market.DataThrottled) for e in told),
      [type(e).__name__ for e in told])


# ---------------------------------------------------------------------------
head("A FUND PRICED ONCE A DAY")
# ---------------------------------------------------------------------------
# Every bar of a mutual fund is flat with no volume. Read as a run of
# session-close stubs, all but two were trimmed, and VFIAX was analysed on
# two bars from 2021. Asked for five-minute bars, Yahoo quietly sends daily
# ones, which were then read as five-minute bars.

n = 300
days = [D0 + k * DAY + OPEN for k in range(n)]
nav = [400.0 + 0.5 * k for k in range(n)]
NOW[0] = days[-1] + 23400 + 3 * 3600
fund = chart(days, nav, opens=nav, highs=nav, lows=nav, volumes=[0.0] * n,
             itype="MUTUALFUND", regular={"start": days[-1], "end": days[-1] + 23400})
on(Wire(Answer(200, fund)))
try:
    bars = market.fetch_bars("VFIAX", "1d")
except market.DataError as exc:
    bars = exc
check("a fund's daily prices are all kept",
      not isinstance(bars, Exception) and len(bars) == n,
      bars if isinstance(bars, Exception) else len(bars))
check("up to the latest", not isinstance(bars, Exception)
      and bars.close[-1] == nav[-1] and bars.last_price == nav[-1])

daily_only = chart(days[-60:], nav[-60:], opens=nav[-60:], highs=nav[-60:], lows=nav[-60:],
                   volumes=[0.0] * 60, itype="MUTUALFUND", granularity="1d")
exc = refused("VFIAX", Wire(Answer(200, daily_only)), interval="5m")
check("asked for five-minute bars, a fund says it has only daily ones",
      exc is not None and "mutual fund" in str(exc) and "daily" in str(exc)
      and "usable bars" not in str(exc), str(exc))

hours = [D0 + 3 * DAY + k * 3600 for k in range(40)]
NOW[0] = hours[-1] + DAY
on(Wire(Answer(200, chart(hours, [50.0 + k for k in range(40)], granularity="1h"))))
check("sixty-minute bars served as one-hour bars are the same thing",
      len(market.fetch_bars("TEST", "60m")) == 40)


# ---------------------------------------------------------------------------
head("ONE SESSION-CLOSE STUB IS TRIMMED, AND ONLY ONE")
# ---------------------------------------------------------------------------
# Yahoo ends an intraday series with a flat bar of no volume at the close.
# It has to go, or it flattens the ATR, but only that one bar.

def intraday(tail, volumes_tail):
    start = D0 + 5 * DAY + OPEN
    ts = [start + j * 300 for j in range(78)]
    closes = [200.0 + 0.01 * j for j in range(78)]
    vols = [1000.0] * 78
    end = start + 78 * 300
    for k, (price, vol) in enumerate(zip(tail, volumes_tail)):
        ts.append(end + 60 * k)
        closes.append(price)
        vols.append(vol)
    NOW[0] = end + 3 * 3600
    return chart(ts, closes, opens=closes, highs=closes if tail else None,
                 lows=closes if tail else None, volumes=vols,
                 regular={"start": start, "end": end})


on(Wire(Answer(200, intraday([200.9], [0.0]))))
bars = market.fetch_bars("TEST", "5m")
check("the stub after the last traded bar is trimmed", len(bars) == 78, len(bars))
check("and its price kept as the latest", bars.last_price == 200.9, bars.last_price)

on(Wire(Answer(200, intraday([200.9, 200.9], [0.0, 0.0]))))
check("flat bars with no volume in a row are quiet trading, and none is trimmed",
      len(market.fetch_bars("TEST", "5m")) == 80)

quiet = intraday([200.9], [0.0])
quiet["chart"]["result"][0]["indicators"]["quote"][0]["volume"][77] = 0.0
on(Wire(Answer(200, quiet)))
check("a flat bar after one that did not trade is a quiet bar, not a stub",
      len(market.fetch_bars("TEST", "5m")) == 79)

days, short = daily_stock(30)
q = short["chart"]["result"][0]["indicators"]["quote"][0]
q["volume"][-1], q["high"][-1], q["low"][-1] = 0.0, q["close"][-1], q["close"][-1]
exc = refused("TEST", Wire(Answer(200, short)))
check("thirty bars with a stub are twenty-nine, too few, and said so",
      exc is not None and "only 29 usable bars" in str(exc), str(exc))


# ---------------------------------------------------------------------------
head("A COIN'S BLANK DAY IS REBUILT FROM THE DAYS EITHER SIDE")
# ---------------------------------------------------------------------------
# Just after midnight UTC Yahoo has sent BTC-USD, ETH-USD and SOL-USD with
# the day that just ended blank and the new day already trading. Dropped, it
# left the analysis on the day before, and the day's change measured across
# two days. A coin's day opens at the previous day's close, so the blank
# day's open and close are known exactly.

n = 40
cdays = [D0 + k * DAY for k in range(n)]
true_close = [100.0 + k for k in range(n)]
true_close[38] = 140.5                         # the day that came through blank
opens = [100.0] + true_close[:-1]
highs = [max(o, c) + 1.0 for o, c in zip(opens, true_close)]
lows = [min(o, c) - 1.0 for o, c in zip(opens, true_close)]
closes = list(true_close)
closes[38] = None
true_close[39] = 141.0                          # the live price of the forming day
closes[39] = 141.0
coin = chart(cdays, closes, opens=opens, highs=highs, lows=lows, volumes=[5e9] * n,
             itype="CRYPTOCURRENCY", gmt=0,
             regular={"start": cdays[-1], "end": cdays[-1] + DAY - 1})
NOW[0] = cdays[-1] + 2 * 3600
on(Wire(Answer(200, coin)))
bars = market.fetch_bars("BTC-USD", "1d")
check("the blank day is kept, and the forming day is the live price",
      len(bars) == 39 and int(bars.timestamp[-1]) == cdays[38] and bars.live_price == 141.0,
      (len(bars), bars.live_price))
check("its close is the next day's open, its open the day before's close",
      bars.close[-1] == 140.5 and bars.open[-1] == true_close[37],
      (bars.open[-1], bars.close[-1]))
check("its range is only what those two span, and its volume is not made up",
      bars.high[-1] == 140.5 and bars.low[-1] == true_close[37] and bars.volume[-1] == 0.0,
      (bars.high[-1], bars.low[-1], bars.volume[-1]))
check("and it is listed as rebuilt", bars.rebuilt == [cdays[38]], bars.rebuilt)
check("the day's change runs from the day that just ended, not two days back",
      bars.prev_close == 140.5, bars.prev_close)
check("and the last bar is dated that day", bars.last_bar_time.startswith(
    time.strftime("%Y-%m-%d", time.gmtime(cdays[38]))), bars.last_bar_time)

two = chart(cdays, closes[:37] + [None, None, 141.0], opens=opens, highs=highs, lows=lows,
            volumes=[5e9] * n, itype="CRYPTOCURRENCY", gmt=0,
            regular={"start": cdays[-1], "end": cdays[-1] + DAY - 1})
on(Wire(Answer(200, two)))
bars = market.fetch_bars("BTC-USD", "1d")
check("two blank days in a row are not invented", len(bars) == 37 and not bars.rebuilt,
      (len(bars), bars.rebuilt))

sdays = [D0 + k * DAY + OPEN for k in range(n)]
NOW[0] = sdays[-1] + 2 * 3600
stock = chart(sdays, closes, opens=opens, highs=highs, lows=lows,
              regular={"start": sdays[-1], "end": sdays[-1] + 23400})
on(Wire(Answer(200, stock)))
bars = market.fetch_bars("TEST", "1d")
check("a stock's blank day is still left out: its sessions do not run into each other",
      len(bars) == 38 and not bars.rebuilt, (len(bars), bars.rebuilt))

hours = [D0 + k * 3600 for k in range(n)]
NOW[0] = hours[-1] + 1800
hourly = chart(hours, closes, opens=opens, highs=highs, lows=lows, volumes=[5e9] * n,
               itype="CRYPTOCURRENCY", gmt=0)
on(Wire(Answer(200, hourly)))
bars = market.fetch_bars("BTC-USD", "1h")
check("and a coin's blank hour too: an hour does not open at the last one's close",
      len(bars) == 38 and not bars.rebuilt, (len(bars), bars.rebuilt))


# ---------------------------------------------------------------------------
head("SPLITS COME WITH THE PRICES")
# ---------------------------------------------------------------------------
# Yahoo's prices are adjusted for splits, so a price recorded before one no
# longer matches them. The split events say by how much.

days, plain = daily_stock()
wire = on(Wire(Answer(200, plain)))
bars = market.fetch_bars("TEST", "1d")
check("every price request asks for split events",
      wire.asked[0]["params"].get("events") == "split", wire.asked[0]["params"])
check("none listed is an empty list", bars.splits == [], bars.splits)

days, split = daily_stock(splits={
    str(days[30]): {"date": days[30], "numerator": 10.0, "denominator": 1.0,
                    "splitRatio": "10:1"},
    str(days[10]): {"date": days[10], "numerator": 1.0, "denominator": 10.0,
                    "splitRatio": "1:10"},
    "bad": {"date": days[5], "numerator": 2.0, "denominator": 0.0}})
on(Wire(Answer(200, split)))
bars = market.fetch_bars("TEST", "1d")
check("each split is (when, new shares per old), oldest first, the unreadable left out",
      bars.splits == [(days[10], 0.1), (days[30], 10.0)], bars.splits)


# ---------------------------------------------------------------------------
head("A TICKER MEANS WHAT IT MEANS TO THE PERSON TYPING IT")
# ---------------------------------------------------------------------------
# Every bare ticker on the coin list was taken as the coin: SUI, Sun
# Communities on the NYSE, was analysed as the Sui coin, even when picked
# from the suggestions. Some coins also went to the wrong id: ARB to "ARbit",
# and SUI, UNI and MATIC to ids that hold nothing.

for typed, meant in (
        ("btc", "BTC-USD"), ("XBT", "BTC-USD"), ("XBTUSD", "BTC-USD"),
        ("eth", "ETH-USD"), ("SOL", "SOL-USD"), ("xrp", "XRP-USD"),
        ("DOGE", "DOGE-USD"), ("LINK", "LINK-USD"), ("ltc", "LTC-USD"),
        ("SUI", "SUI"), ("ATOM", "ATOM"), ("VET", "VET"),
        ("BCH", "BCH"), ("NEAR", "NEAR"), ("ARB", "ARB"), ("APT", "APT"), ("TRX", "TRX"),
        ("SUI-USD", "SUI20947-USD"), ("SUIUSDT", "SUI20947-USD"),
        ("sui/usd", "SUI20947-USD"), ("BINANCE:ARBUSDT", "ARB11841-USD"),
        ("ARB-USD", "ARB11841-USD"), ("LTC-USD", "LTC-USD"), ("LTCUSD", "LTC-USD"),
        ("UNI", "UNI7083-USD"), ("MATIC", "POL28321-USD"), ("APTUSDT", "APT21794-USD"),
        ("PEPE", "PEPE24478-USD"), ("hbar", "HBAR-USD"),
        ("SUI20947-USD", "SUI20947-USD"), ("$aapl", "AAPL"), ("NASDAQ:msft", "MSFT"),
        ("BRK-B", "BRK-B"), ("eth/eur", "ETH-EUR"), ("^GSPC", "^GSPC"),
        ("4043.T", "4043.T")):
    got = market.normalise_symbol(typed)
    check("%r is %s" % (typed, meant), got == meant, got)
for empty in ("", "   ", "$", "NYSE:"):
    try:
        market.normalise_symbol(empty)
        check("%r is refused as empty" % empty, False)
    except market.DataError:
        check("%r is refused as empty" % empty, True)
check("every coin id is a dollar price", all(v.endswith("-USD") for v in market._COINS.values()))
check("the listing-first tickers are all coin tickers",
      market._LISTED_FIRST <= set(market._COINS), market._LISTED_FIRST - set(market._COINS))

from bot import public  # noqa: E402

shown = [(public.short(s), s) for s, _ in public.POPULAR if s.endswith("-USD")]
check("every coin on the public pages opens by the ticker those pages show it as",
      shown and all(market.normalise_symbol(label) == s for label, s in shown),
      [(label, market.normalise_symbol(label)) for label, s in shown
       if market.normalise_symbol(label) != s])


def quote(symbol, kind, name, where=""):
    return {"symbol": symbol, "quoteType": kind, "shortname": name, "exchDisp": where}


SEARCH = {
    "SUI": [quote("SUI", "EQUITY", "Sun Communities, Inc.", "NYSE"),
            quote("SUI20947-USD", "CRYPTOCURRENCY", "Sui USD", "CCC"),
            quote("SUI=F", "FUTURE", "SUI Futures")],
    "LTC": [quote("LTC", "EQUITY", "LTC Properties, Inc.", "NYSE"),
            quote("LTC-USD", "CRYPTOCURRENCY", "Litecoin USD", "CCC")],
    "LTC-USD": [quote("LTC-USD", "CRYPTOCURRENCY", "Litecoin USD", "CCC")],
    "ATOM": [quote("ATOM", "EQUITY", "Atomera Incorporated", "NASDAQ"),
             quote("ATOM-USD", "CRYPTOCURRENCY", "Cosmos USD", "CCC")],
    "BTC-USD": [quote("BTC-USD", "CRYPTOCURRENCY", "Bitcoin USD", "CCC")],
    "UNI7083-USD": [quote("UNI7083-USD", "CRYPTOCURRENCY", "Uniswap USD", "CCC")],
    "XRP": [quote("XRP", "ETF", "Bitwise XRP ETF", "NYSEArca"),
            quote("XRP-USD", "CRYPTOCURRENCY", "XRP USD", "CCC"),
            quote("XRP=F", "FUTURE", "XRP Futures")],
    "ARB-USD": [quote("ARB-USD", "CRYPTOCURRENCY", "ARbit USD", "CCC"),
                quote("ARB11841-USD", "CRYPTOCURRENCY", "Arbitrum USD", "CCC")],
}
ASKED = []
REFUSE = [False]


def search_only(url, params, tries=2):
    ASKED.append(url)
    if REFUSE[0]:
        raise market.DataThrottled("refused", 60)
    if url != market._SEARCH:
        raise AssertionError("only the search should be asked here: %s" % url)
    return {"quotes": SEARCH.get(params["q"], [])}


real_get = market._get
market._get = search_only
try:
    for typed, meant, name in (("SUI", "SUI", "Sun Communities, Inc."),
                               ("ATOM", "ATOM", "Atomera Incorporated"),
                               ("ltc", "LTC-USD", "Litecoin USD"),
                               ("btc", "BTC-USD", "Bitcoin USD"),
                               ("uni", "UNI7083-USD", "Uniswap USD")):
        found = market.resolve(typed)
        check("an analysis of %r is of %s, %s" % (typed, meant, name),
              found["symbol"] == meant and found["name"] == name, found)

    picks = [s["symbol"] for s in market.search("XRP")]
    check("the fund listed as XRP is not offered, since picking it would open the coin",
          picks == ["XRP-USD"], picks)
    picks = [s["symbol"] for s in market.search("LTC")]
    check("nor LTC Properties, for the same reason", picks == ["LTC-USD"], picks)
    picks = [s["symbol"] for s in market.search("ARB-USD")]
    check("nor a coin whose id this app reads as another coin", picks == ["ARB11841-USD"], picks)
    picks = [s["symbol"] for s in market.search("SUI")]
    check("Sun Communities is offered as SUI, and the Sui coin by its own id",
          picks == ["SUI", "SUI20947-USD"], picks)
    every = [s["symbol"] for typed in SEARCH for s in market.search(typed)]
    check("every suggestion is analysed exactly as picked",
          every and all(market.normalise_symbol(s) == s for s in every),
          [s for s in every if market.normalise_symbol(s) != s])
    check("and the analysis of a pick finds that same instrument",
          all(market.resolve(s)["symbol"] == s for s in every),
          [(s, market.resolve(s)["symbol"]) for s in every])

    REFUSE[0] = True
    del ASKED[:]
    try:
        market.resolve("SUI")
        check("a refused look-up is passed on, not guessed past", False, "it guessed")
    except market.DataThrottled:
        check("a refused look-up is passed on, not guessed past", len(ASKED) == 1, ASKED)
    check("while the suggestions just go quiet", market.search("SUI") == [])
finally:
    market._get = real_get
    REFUSE[0] = False


# ---------------------------------------------------------------------------
head("PAPER CHECK READS A POSITION THE WAY THE JOURNAL DOES")
# ---------------------------------------------------------------------------
# `paper.py check` chose the bars to scan by a rule of its own, one step from
# the journal pages' (portfolio.stop_touched): a bar starting the moment a
# trade opened was read by one and not the other. The pages' rule leaves out
# the bar the trade opened in, since part of its range may be from before the
# entry, and a low from before you bought is not your stop being hit.

import io  # noqa: E402

import paper  # noqa: E402
from rich.console import Console  # noqa: E402

from bot import database as db  # noqa: E402
from bot.portfolio import stop_touched  # noqa: E402

E = D0 + 500 * DAY + OPEN                       # the entry day's bar
OPENED = E + 3600                               # bought an hour into the session
stamps = np.array([E + k * DAY for k in range(-3, 4)], dtype=np.int64)


def position(lows, highs, live):
    return types.SimpleNamespace(timestamp=stamps, low=np.asarray(lows, dtype=float),
                                 high=np.asarray(highs, dtype=float), last_price=live)


#                 E-3  E-2  E-1   E   E+1  E+2  E+3
BOOK = {
    "DIP": position([98, 98, 98, 93, 96, 96, 96], [101] * 7, 97.0),    # dip before entry
    "HIT": position([98, 98, 98, 99, 96, 94, 96], [101] * 7, 97.0),    # stop hit at E+2
    "WON": position([98, 98, 98, 99, 99, 99, 99], [101, 101, 101, 101, 105, 101, 101],
                    102.0),                                           # target at E+1
    "EDGE": position([98, 98, 98, 93, 96, 96, 96], [101] * 7, 97.0),   # opened at E's start
}
paper.fetch_bars = lambda symbol, interval="1d": BOOK[symbol]
conn = db.connect(DB_PATH)
for symbol, target, opened in (("DIP", 110.0, OPENED), ("HIT", 110.0, OPENED),
                               ("WON", 104.0, OPENED), ("EDGE", 110.0, E)):
    tid = db.open_trade(conn, symbol, "1d", 1, entry=100.0, stop=95.0, target1=target,
                        quantity=1)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (opened, tid))
conn.commit()
out = io.StringIO()
paper.console = Console(file=out, width=200, color_system=None)
paper.cmd_check(types.SimpleNamespace(account="paper"), conn)
conn.close()
rows = {}
for line in out.getvalue().splitlines():           # the table row, before any notes
    for symbol in BOOK:
        if symbol in line.split():
            rows.setdefault(symbol, line)


def said(symbol):
    return rows.get(symbol, "")


check("a dip in the entry bar from before the entry is not a stop hit",
      "STOP" not in said("DIP") and "open" in said("DIP"), said("DIP"))
check("which is what the journal pages say too",
      stop_touched(BOOK["DIP"], OPENED, 95.0, 1) == (None, None))
check("a stop hit on a later bar is", "STOP HIT" in said("HIT"), said("HIT"))
check("on the bar the journal pages name", stop_touched(BOOK["HIT"], OPENED, 95.0, 1)[0]
      == int(stamps[5]))
check("and a target reached after the entry is", "TARGET HIT" in said("WON"), said("WON"))
check("a bar that starts the moment the trade opened is read by the same rule",
      ("STOP" in said("EDGE")) == (stop_touched(BOOK["EDGE"], E, 95.0, 1)[0] is not None),
      (said("EDGE"), stop_touched(BOOK["EDGE"], E, 95.0, 1)))


print()
print("%d failure(s)" % len(fails))
print("MARKET OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
