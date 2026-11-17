"""Tickers shared by a coin and a stock: each one reachable, each kept apart.

Typed bare, LINK, LTC, BTC, ETH and XRP are coins, because the public pages
name those coins so. Interlink Electronics (LINK), LTC Properties (LTC) and
the funds listed as BTC, ETH and XRP are reached by Yahoo's exact symbol,
marked exact=1, which a pick from the suggestions sends. These check that
each of the five opens the listing with the mark and the coin without it,
that the suggestions offer both, and that what is saved for a listing (a
watchlist entry, an alert, a journal entry, a thesis, a share) stays the
listing, while what was saved before the mark keeps the meaning it had.

No network: the search, the prices, the news and the calendar are stand-ins
that answer by symbol.
"""
import calendar
import html
import os as _os
import re
import sqlite3
import sys as _sys
import tempfile
import time
import zlib
from types import SimpleNamespace as NS

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

c, DB = harness.isolated_web("tickers")

import numpy as np  # noqa: E402

import web  # noqa: E402
from bot import alerts as alerts_mod  # noqa: E402
from bot import catalysts as catalysts_mod  # noqa: E402
from bot import database as db  # noqa: E402
from bot import engine  # noqa: E402
from bot import market  # noqa: E402
from bot import workflows  # noqa: E402
from bot.sentiment import Sentiment  # noqa: E402

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


# ---------------------------------------------------------------------------
# Stand-ins for the source
# ---------------------------------------------------------------------------

# Each shared ticker: the listing's name and where it trades, the coin's id at
# Yahoo and Yahoo's name for it.
SHARED = [
    ("LINK", "Interlink Electronics, Inc.", "NASDAQ", "LINK-USD", "Chainlink USD"),
    ("LTC", "LTC Properties, Inc.", "NYSE", "LTC-USD", "Litecoin USD"),
    ("BTC", "Grayscale Bitcoin Mini Trust ETF", "NYSEArca", "BTC-USD", "Bitcoin USD"),
    ("ETH", "Grayscale Ethereum Mini Trust ETF", "NYSEArca", "ETH-USD", "Ethereum USD"),
    ("XRP", "Bitwise XRP ETF", "NYSEArca", "XRP-USD", "XRP USD"),
]
# Far apart, so a price read from the wrong one of a pair shows.
PRICE = {"LINK": 6.0, "LINK-USD": 15.0, "LTC": 35.0, "LTC-USD": 90.0,
         "BTC": 45.0, "BTC-USD": 95000.0, "ETH": 30.0, "ETH-USD": 3200.0,
         "XRP": 20.0, "XRP-USD": 2.3}
NAMES = {}
for _t, _name, _where, _coin, _coin_name in SHARED:
    NAMES[_t] = (_name, "equity")
    NAMES[_coin] = (_coin_name, "crypto")


def quote(symbol, kind, name, where):
    return {"symbol": symbol, "quoteType": kind, "shortname": name, "exchDisp": where}


def answers(q):
    """Yahoo's search: a shared ticker, or the coin's id, finds both."""
    q = q.strip().upper()
    for ticker, name, where, coin, coin_name in SHARED:
        if q in (ticker, coin):
            return [quote(coin, "CRYPTOCURRENCY", coin_name, "CCC"),
                    quote(ticker, "ETF" if "ETF" in name else "EQUITY", name, where)]
    if q == "INTERLINK":
        return [quote("LINK", "EQUITY", "Interlink Electronics, Inc.", "NASDAQ")]
    return []


def search_only(url, params, tries=2):
    if url != market._SEARCH:
        raise AssertionError("only the search should be asked here: %s" % url)
    return {"quotes": answers(params["q"])}


DAY = 86400
TODAY = int(time.time()) // DAY * DAY
FETCHED = []


def made_up_bars(symbol, interval="1d", lookback=None):
    """A year of daily bars ending at PRICE, named as Yahoo names the symbol."""
    FETCHED.append(symbol)
    if symbol not in NAMES:
        raise market.SymbolNotFound("The data source has nothing under that symbol.")
    name, asset = NAMES[symbol]
    n = 300
    rng = np.random.RandomState(zlib.crc32(symbol.encode()))
    close = np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    close *= PRICE[symbol] / close[-1]
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    ts = np.array([TODAY - (n - 1 - k) * DAY for k in range(n)], dtype=np.int64)
    return market.Bars(symbol=symbol, name=name, asset_class=asset, currency="USD",
                       exchange="CCC" if asset == "crypto" else "NasdaqGM",
                       interval="1d", timestamp=ts, open=open_, high=high, low=low,
                       close=close, volume=rng.randint(800_000, 1_600_000, n).astype(float),
                       session_id=ts // DAY, prev_close=float(close[-2]),
                       market_open=False, last_bar_time="2026-11-13")


NEWS = []


def no_news(symbol, name="", use_llm=True):
    NEWS.append((symbol, name))
    return Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped", notes=["Not read in a test."])


def offline(*_a, **_k):
    raise RuntimeError("offline")


market._get = search_only
market.split_history = lambda symbol: []
market.fetch_bars = engine.fetch_bars = alerts_mod.fetch_bars = made_up_bars
workflows.fetch_bars = made_up_bars
engine.analyse_sentiment = no_news
catalysts_mod.fetch = offline
web.portfolio_mod.overlap = lambda *a, **k: {"warnings": [], "pairs": []}


def text(response):
    return response.data.decode("utf-8", "replace")


def heading(body):
    """The instrument a page is about: its symbol and its name."""
    sym = re.search(r'<h1 class="sym">([^<]*)</h1>', body)
    name = re.search(r'<span class="name">([^<]*)</span>', body)
    return (sym.group(1) if sym else None, name.group(1) if name else None)


def rows(sql, *args):
    conn = db.connect(DB)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


ME = rows("SELECT id FROM users WHERE is_local=1")[0]["id"]
MARK = '<input type="hidden" name="exact" value="1">'

# ---------------------------------------------------------------------------
head("THE MARKET LAYER: A BARE TICKER IS THE COIN, AN EXACT ONE THE LISTING")
# ---------------------------------------------------------------------------
for ticker, name, where, coin, coin_name in SHARED:
    check("%s typed is the coin, %s" % (ticker, coin),
          market.normalise_symbol(ticker.lower()) == coin)
    check("%s exactly is the listing, as Yahoo files it" % ticker,
          market.normalise_symbol(" %s " % ticker.lower(), exact=True) == ticker)
    check("so a link to %s has to say exactly, and one to %s need not" % (ticker, coin),
          market.needs_exact(ticker) and not market.needs_exact(coin))
    found = market.resolve(ticker, exact=True)
    check("looked up exactly, %s is %s" % (ticker, name),
          (found["symbol"], found["name"]) == (ticker, name), found)
    found = market.resolve(ticker)
    check("looked up bare, %s is %s" % (ticker, coin_name),
          (found["symbol"], found["name"]) == (coin, coin_name), found)
    picks = {(s["symbol"], s["exact"], s["name"], s["where"]) for s in market.search(ticker)}
    check("the suggestions for %s offer both, each said for what it is" % ticker,
          picks == {(coin, False, coin_name[:-4], "coin"), (ticker, True, name, where)},
          picks)
check("a ticker that reads the same either way needs no mark",
      not market.needs_exact("AAPL") and not market.needs_exact("SUI")
      and not market.needs_exact(""))
found = market.resolve("interlink", exact=True)
check("an exact symbol is never swapped for the closest match",
      found["symbol"] == "INTERLINK", found)
check("as a name typed bare still is", market.resolve("interlink")["symbol"] == "LINK")

# ---------------------------------------------------------------------------
head("THE SUGGESTION BOX")
# ---------------------------------------------------------------------------
found = c.get("/api/search?q=link").get_json()
check("asking for LINK suggests the coin and the company",
      [(f["symbol"], f["exact"]) for f in found] == [("LINK-USD", False), ("LINK", True)],
      found)
check("labelled the way a person tells them apart",
      [(f["name"], f["where"]) for f in found]
      == [("Chainlink", "coin"), ("Interlink Electronics, Inc.", "NASDAQ")], found)
page = text(c.get("/"))
check("a pick marked exact is sent with exact=1, and typing again drops it",
      "markExact(items[i].exact)" in page and "markExact(false)" in page
      and "mark.name = 'exact'" in page)

# ---------------------------------------------------------------------------
head("EACH OF THE FIVE: THE LISTING WITH exact=1, THE COIN WITHOUT")
# ---------------------------------------------------------------------------
for ticker, name, where, coin, coin_name in SHARED:
    bare = text(c.get("/analyse?symbol=%s&interval=1d&news=0" % ticker.lower()))
    listing = text(c.get("/analyse?symbol=%s&interval=1d&news=0&exact=1" % ticker))
    check("%s with exact=1 is %s" % (ticker, name), heading(listing) == (ticker, name),
          heading(listing))
    check("%s without it is still %s" % (ticker, coin_name), heading(bare) == (coin, coin_name),
          heading(bare))
    again = text(c.get("/analyse?symbol=%s&interval=1d&news=0" % ticker))
    check("and kept apart in the cache: the coin again is the coin",
          heading(again) == (coin, coin_name), heading(again))

listing = text(c.get("/analyse?symbol=LINK&interval=1d&news=0&exact=1"))
coin_page = text(c.get("/analyse?symbol=LINK&interval=1d&news=0"))
check("the listing's research link says exactly",
      'href="/research?symbol=LINK&amp;interval=1d&amp;exact=1"' in listing)
check("and so does its JSON link",
      'href="/api/analyse?symbol=LINK&amp;interval=1d&amp;exact=1"' in listing)
check("its share, watch and alert forms send the mark",
      listing.count(MARK) >= 3, listing.count(MARK))
check("its own address names it exactly",
      '<link rel="canonical" href="http://localhost/analyse?symbol=LINK&amp;interval=1d'
      '&amp;exact=1">' in listing)
check("while the coin's names the coin, with no mark anywhere",
      '<link rel="canonical" href="http://localhost/analyse?symbol=LINK-USD&amp;interval=1d">'
      in coin_page and MARK not in coin_page and "&amp;exact=1" not in coin_page)
check("an exact pick of a ticker that needs none is the plain one",
      heading(text(c.get("/analyse?symbol=LINK-USD&interval=1d&news=0&exact=1")))
      == ("LINK-USD", "Chainlink USD"))
check("each kind counts its own free look",
      web._look_item("LINK", "1d", True) != web._look_item("LINK", "1d")
      and web._look_item("LINK", "1d", True) == workflows._Looks._item("LINK", "1d", True))

del NEWS[:]
c.get("/analyse?symbol=LTC&interval=1d&exact=1")
check("the news is searched by the listing's name, not by a ticker the coin shares",
      ("LTC", "LTC Properties, Inc.") in NEWS, NEWS)

# ---------------------------------------------------------------------------
head("THE RESEARCH PAGE AND A SAVED THESIS")
# ---------------------------------------------------------------------------
real_research = engine.research
engine.research = lambda symbol, price, cfg, **k: engine.Research(symbol=symbol.upper(),
                                                                 price=price)
try:
    page = text(c.get("/research?symbol=LINK&interval=1d&exact=1"))
    check("research with exact=1 is the listing's", heading(page)[0] == "LINK"
          and "Interlink Electronics" in page, heading(page))
    check("its way back to the trade view says exactly",
          'href="/analyse?symbol=LINK&amp;interval=1d&amp;exact=1"' in page)
    check("and its thesis and watch forms send the mark", page.count(MARK) == 2,
          page.count(MARK))
    page = text(c.get("/research?symbol=LINK&interval=1d"))
    check("without the mark it is the coin's", heading(page)[0] == "LINK-USD")

    engine.research = lambda *a, **k: NS(thesis=NS(
        balance=0.3, verdict="Leans bullish", bull=NS(points=[]), bear=NS(points=[]),
        falsifiers=["A close below 5"], all_sources=[]))
    r = c.post("/thesis/save", data={"symbol": "LINK", "exact": "1", "interval": "1d"})
    check("a thesis saved from the listing's page goes back to the listing's page",
          r.status_code == 302
          and r.headers["Location"].endswith("/research?symbol=LINK&interval=1d&exact=1"),
          (r.status_code, r.headers.get("Location")))
    saved = rows("SELECT symbol, price FROM theses WHERE user_id=?", ME)
    check("and is kept under the listing, at the listing's price",
          saved == [{"symbol": "LINK", "price": PRICE["LINK"]}], saved)
finally:
    engine.research = real_research
page = text(c.get("/monitor"))
check("the Monitor page reviews it on the listing's page",
      'href="/research?symbol=LINK&amp;exact=1">Review</a>' in page)

# ---------------------------------------------------------------------------
head("A WATCHLIST ENTRY SAVED FOR THE LISTING STAYS THE LISTING")
# ---------------------------------------------------------------------------
c.post("/watch/add", data={"symbol": "LINK", "exact": "1", "interval": "1d"})
c.post("/watch/add", data={"symbol": "ltc", "interval": "1d"})
watched = {(w["symbol"], w["exact"]) for w in
           rows("SELECT symbol, exact FROM watchlist WHERE user_id=?", ME)}
check("Interlink is watched as LINK, exactly, and a typed ltc as the coin",
      watched == {("LINK", 1), ("LTC-USD", 0)}, watched)
page = text(c.get("/monitor"))
check("its Analyse link opens the listing",
      'href="/analyse?symbol=LINK&amp;interval=1d&amp;exact=1">Analyse</a>' in page)
check("and the coin's opens the coin",
      'href="/analyse?symbol=LTC-USD&amp;interval=1d">Analyse</a>' in page)
check("the front page links to it the same way",
      'href="/analyse?symbol=LINK&amp;interval=1d&amp;exact=1"' in text(c.get("/")))
page = text(c.get("/research?symbol=LINK&interval=1d&exact=1"))
check("the listing's research page says it is watched", "Stop watching" in page)

c.post("/workflow/watchlist")
runs = {(r["symbol"], r["name"]) for r in
        rows("SELECT symbol, name FROM runs WHERE source='workflow'")}
check("the watchlist routine analyses the listing, and the coin as the coin",
      runs == {("LINK", "Interlink Electronics, Inc."), ("LTC-USD", "Litecoin USD")}, runs)
page = text(c.get("/monitor"))
check("and its report opens the listing",
      'href="/analyse?symbol=LINK&amp;interval=1d&amp;exact=1">Open</a>' in page)

# What the deployed version saved: whatever was typed, upper-cased. A typed
# "eth" was the coin then and stays the coin.
conn = db.connect(DB)
for symbol in ("ETH", "XRP", "XRP-USD"):
    conn.execute("INSERT INTO watchlist (symbol, interval, added_ts, note, user_id) "
                 "VALUES (?, '1d', ?, 'typed long ago', ?)", (symbol, int(time.time()), ME))
conn.commit()
conn.close()
page = text(c.get("/monitor"))
check("an entry saved before the mark keeps opening the coin",
      'href="/analyse?symbol=ETH&amp;interval=1d">Analyse</a>' in page)
c.post("/watch/add", data={"symbol": "ETH", "exact": "1", "interval": "1d"})
watched = {(w["symbol"], w["exact"], w["note"]) for w in
           rows("SELECT symbol, exact, note FROM watchlist WHERE user_id=? AND "
                "symbol LIKE 'ETH%'", ME)}
check("watching the fund then moves it to the coin's id rather than replace it",
      watched == {("ETH", 1, ""), ("ETH-USD", 0, "typed long ago")}, watched)
c.post("/watch/add", data={"symbol": "XRP", "exact": "1", "interval": "1d"})
watched = {(w["symbol"], w["exact"]) for w in
           rows("SELECT symbol, exact FROM watchlist WHERE user_id=? AND "
                "symbol LIKE 'XRP%'", ME)}
check("and where the coin was watched under its id already, keeps that one",
      watched == {("XRP", 1), ("XRP-USD", 0)}, watched)

# ---------------------------------------------------------------------------
head("AN ALERT SAVED FOR THE LISTING STAYS THE LISTING")
# ---------------------------------------------------------------------------
c.post("/monitor/alert/add", data={"symbol": "LTC", "exact": "1", "kind": "price_above",
                                   "threshold": "60", "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "ltc", "kind": "price_above",
                                   "threshold": "60", "interval": "1d"})
saved = [(a["symbol"], a["exact"]) for a in
         rows("SELECT symbol, exact FROM alerts WHERE user_id=? ORDER BY id", ME)]
check("one is saved for LTC Properties, exactly, and one for Litecoin",
      saved == [("LTC", 1), ("LTC-USD", 0)], saved)
conn = db.connect(DB)
try:
    fired = {f.symbol for f in alerts_mod.check(conn, user=ME)["fired"]}
finally:
    conn.close()
check("each is checked against its own price: Litecoin passed 60, LTC Properties did not",
      fired == {"LTC-USD"}, fired)

form = re.search(r'<form method="post" action="/monitor/alert/add">(.*?)</form>',
                 text(c.get("/analyse?symbol=BTC&interval=1d&news=0&exact=1")), re.S)
sent = ({k: html.unescape(v) for k, v in
         re.findall(r'name="(\w+)" value="([^"]*)"', form.group(1))} if form else {})
c.post("/monitor/alert/add", data=sent)
latest = rows("SELECT symbol, exact FROM alerts WHERE user_id=? ORDER BY id DESC LIMIT 1", ME)
check("an alert set from the fund's analysis is the fund's",
      sent.get("exact") == "1" and latest == [{"symbol": "BTC", "exact": 1}], (sent, latest))

c.post("/monitor/alert/add", data={"symbol": "XRP", "exact": "1", "kind": "call_changes",
                                   "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "xrp", "kind": "call_changes", "interval": "1d"})
asked = []


def call_of(symbol, interval="1d", exact=False):
    asked.append((symbol, exact))
    return "WAIT"


conn = db.connect(DB)
try:
    alerts_mod.check(conn, user=ME, call_of=call_of)
finally:
    conn.close()
check("a call alert on the fund asks for the fund's call, exactly",
      ("XRP", True) in asked and ("XRP-USD", False) in asked, asked)

seen = []
real_analyse = engine.analyse


def spy(symbol, cfg, **kw):
    seen.append((symbol, kw.get("exact", False)))
    return real_analyse(symbol, cfg, **kw)


engine.analyse = spy
try:
    web._CALLS.clear()
    web._call_now("XRP", "1d", exact=True)
    web._call_now("XRP", "1d")
finally:
    engine.analyse = real_analyse
check("and the site works that call out on the fund, apart from the coin's",
      seen == [("XRP", True), ("XRP", False)], seen)

# ---------------------------------------------------------------------------
head("THE JOURNAL AND A SHARED CALL")
# ---------------------------------------------------------------------------
c.post("/trades/open", data={"symbol": "BTC", "exact": "1", "direction": "long",
                             "entry": "44", "stop": "40"})
c.post("/trades/open", data={"symbol": "btc", "direction": "long",
                             "entry": "94000", "stop": "90000"})
kept = [t["symbol"] for t in rows("SELECT symbol FROM trades WHERE user_id=? ORDER BY id", ME)]
check("a trade logged for the fund is the fund's, and a typed btc is Bitcoin",
      kept == ["BTC", "BTC-USD"], kept)
marks = {t["symbol"]: web._mark_one(t)["price"]
         for t in rows("SELECT * FROM trades WHERE user_id=?", ME)}
check("each priced as itself", marks == {"BTC": PRICE["BTC"], "BTC-USD": PRICE["BTC-USD"]},
      marks)

r = c.post("/share", data={"symbol": "LINK", "exact": "1", "interval": "1d"})
page = text(c.get(r.headers.get("Location", "/")))
check("a shared call from the listing is the listing's",
      r.status_code == 303 and "Interlink Electronics" in page, r.status_code)
check("and its full workings open the listing",
      'href="/analyse?symbol=LINK&amp;interval=1d&amp;exact=1"' in page)

check("a scan's row or a stored run links exactly where it needs to",
      web.exact_arg("LINK") == 1 and web.exact_arg("LINK-USD") is None
      and web.exact_arg("AAPL") is None)
check("and a saved entry by its own mark",
      web.exact_arg("LINK", 1) == 1 and web.exact_arg("LINK", 0) is None)

# ---------------------------------------------------------------------------
head("THE WEEKLY EMAIL")
# ---------------------------------------------------------------------------
check("the email's look-up of a watched listing is the listing's",
      web._digest_look_up("LINK", exact=True)["symbol"] == "LINK"
      and web._digest_look_up("LINK")["symbol"] == "LINK-USD")

from bot import accounts as accounts_mod  # noqa: E402
from bot import digest  # noqa: E402

MAIL = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "MAIL_FROM": "Orenth <hi@example.com>", "MAIL_POSTAL_ADDRESS": "PO Box 1"}
# Set only here: with email set up, the site would ask every request to sign in.
_os.environ.update(MAIL)
conn = db.connect(DB)
try:
    reader = accounts_mod.upsert_email_user(conn, "reader@example.com")
    digest.set_wanted(conn, reader.id, True)
    db.watch_add(conn, "LINK", "1d", user=reader.id, exact=True)
    db.watch_add(conn, "LINK-USD", "1d", user=reader.id)
    conn.execute("INSERT INTO watchlist (symbol, interval, added_ts, user_id) "
                 "VALUES ('ETH', '1d', 1, ?)", (reader.id,))
    conn.commit()
    asked = []
    # What the look-up gives: Yahoo's own symbol, LINK for Interlink and
    # LINK-USD for Chainlink.
    CALLS = {"LINK": ("AVOID", "Leave this one alone."),
             "LINK-USD": ("BUY", "The app thinks this is worth buying now.")}

    def look_up(symbol, exact=False):
        asked.append((symbol, exact))
        if symbol in CALLS:
            return {"symbol": symbol, "action": CALLS[symbol][0],
                    "headline": CALLS[symbol][1]}
        return {"symbol": symbol}

    monday = calendar.timegm((2026, 10, 5, 14, 0, 0))
    for sym, before, after in (("LINK", "AVOID", "AVOID"), ("LINK-USD", "WAIT", "BUY")):
        db.watch_call_save(conn, sym, monday // DAY - 2, before, 100.0, 99.0, monday - 2 * DAY)
        db.watch_call_save(conn, sym, monday // DAY - 1, after, 103.0, 100.0, monday - 3600)
    outbox = []
    sent = digest.run(conn, "secret", "https://orenth.app", look_up,
                      send=lambda to, subject, body, headers=None:
                      outbox.append((subject, body)),
                      now=monday, full=lambda person: True)
    check("the weekly email looks a watched listing up exactly, the rest as saved",
          sent.get("checked") == 1
          and sorted(asked) == [("ETH", False), ("LINK", True), ("LINK-USD", False)],
          (sent, asked))
    subject, body = outbox[0] if outbox else ("", "")
    check("it names the listing and the coin apart, each by Yahoo's own symbol",
          "LINK avoid" in subject and "LINK-USD buy" in subject
          and "  LINK   AVOID  Leave this one alone." in body
          and "  LINK-USD BUY    The app thinks this is worth buying now." in body,
          (subject, body))
    check("in what changed since yesterday too",
          "  LINK   Still AVOID; up 3.00% since the previous close." in body
          and "  LINK-USD The daily call went from WAIT to BUY; up 3.00%" in body, body)
    check("and a ticker that no other line shares keeps its short name",
          "  ETH    could not be checked this week." in body, body)
finally:
    conn.close()
    for key in MAIL:
        _os.environ.pop(key, None)

# ---------------------------------------------------------------------------
head("THE DATABASE: ONE COLUMN ADDED, NOTHING ELSE CHANGED")
# ---------------------------------------------------------------------------
conn = db.connect(DB)
try:
    for table in ("watchlist", "alerts"):
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(%s)" % table)}
        check("%s has an exact column, 0 unless set" % table,
              "exact" in cols and cols["exact"][4] == "0", cols.get("exact"))
    # The previous version writes rows without it, and they read as before.
    conn.execute("INSERT INTO alerts (symbol, kind, threshold, interval, note, active, "
                 "created_ts, repeat, user_id) VALUES ('ETH', 'price_above', 5000, "
                 "'1d', '', 1, 1, 0, ?)", (ME,))
    conn.commit()
    old = conn.execute("SELECT exact FROM alerts WHERE symbol='ETH'").fetchone()
    check("a row written by the previous version reads as unmarked", old[0] == 0)
finally:
    conn.close()

# A watchlist from before there were accounts is rebuilt after the columns
# are added; the rebuilt table has to keep the new one.
ancient = _os.path.join(tempfile.mkdtemp(prefix="stockbot-test-tickers-"), "old.db")
raw = sqlite3.connect(ancient)
raw.execute("CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, interval TEXT NOT NULL "
            "DEFAULT '1d', added_ts INTEGER NOT NULL, note TEXT, last_run INTEGER)")
raw.execute("INSERT INTO watchlist (symbol, added_ts) VALUES ('LINK', 1)")
raw.commit()
raw.close()
conn = db.connect(ancient)
try:
    db.watch_add(conn, "LTC", "1d", exact=True)
    kept = sorted((r["symbol"], r["exact"]) for r in conn.execute("SELECT * FROM watchlist"))
    check("a rebuilt old watchlist keeps the column, and its old entry its meaning",
          kept == [("LINK", 0), ("LTC", 1)], kept)
finally:
    conn.close()

print()
print("%d failure(s)" % len(fails))
print("TICKERS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
