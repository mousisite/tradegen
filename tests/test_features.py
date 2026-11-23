"""The signed-in front page, the paper-trade review, the journal's plan against
its result, the usage counts and the free allowance for routines.

No network: prices and analyses are stand-ins. The front page and the review
page must read what is stored, so anything that would start an analysis or a
scan while either is drawn fails the test.
"""
import hashlib
import hmac
import json
import os as _os
import re
import sys as _sys
import time
from types import SimpleNamespace as NS

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
          "STRIPE_WEBHOOK_SECRET": "whsec_test", "PRO_PRICE_LABEL": "$9 a month"}
MAIL = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "MAIL_FROM": "Orenth <hi@example.com>"}
for key in list(STRIPE) + list(MAIL) + ["STOCKBOT_ADMIN_EMAIL", "STRIPE_PRICE_ID_YEARLY",
                                        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                                        "TWILIO_VERIFY_SID", "ANTHROPIC_API_KEY"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import codes  # noqa: E402
from bot import database as db  # noqa: E402
from bot import events  # noqa: E402
from bot import ideas as ideas_mod  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot import public as public_mod  # noqa: E402
from bot import screener as screener_mod  # noqa: E402
from bot import workflows  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def text(response):
    return response.data.decode("utf-8", "replace")


def flat(html):
    """The page with its line breaks and indents folded into single spaces."""
    return " ".join(html.split())


c, DB = harness.isolated_web("features")
# The front door refreshes its example analysis in the background; marked
# busy, it never starts one, so nothing here reaches the network.
web._example_state["busy"] = True
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"
NOW = int(time.time())
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/131.0"}

conn = db.connect(DB)
alice = A.upsert_google_user(conn, {"email": "alice@example.com", "sub": "g-a",
                                    "name": "Alice", "email_verified": True})
bob = A.upsert_google_user(conn, {"email": "bob@example.com", "sub": "g-b",
                                  "name": "Bob", "email_verified": True})
conn.close()


def sign_in(client, user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


def count(name):
    """Today's count of one thing, as /usage reads it."""
    conn = db.connect(DB)
    try:
        return events.read(conn, 1)["total"][name]
    finally:
        conn.close()


def add_run(user, symbol, action, entry=100.0, stop=95.0, target=110.0, interval="1d",
            ts=None, price=None, asset="equity", market_open=1, source="web"):
    conn = db.connect(DB)
    try:
        run_id = conn.execute(
            "INSERT INTO runs (ts, symbol, name, asset_class, interval, price, market_open, "
            "action, entry, stop, target1, user_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(ts or time.time()), symbol, symbol + " Corp", asset, interval,
             price if price is not None else entry, market_open, action, entry, stop,
             target, user.id, source)).lastrowid
        conn.commit()
        return run_id
    finally:
        conn.close()


# Anything that analyses or scans. The front page and the review page must
# never reach one of these.
started = []


def must_not_run(what):
    def refuse(*_a, **_k):
        started.append(what)
        raise AssertionError("%s was started while drawing a page" % what)
    return refuse


QUOTED_AT = 1762956300                      # 12 Nov 2025 14:05 UTC
PRICES = {"ZZA": 90.0, "ZZB": 106.0, "ZZC": 101.0, "ZZSTALE": 100.0}


def fake_bars(symbol, interval="1d", lookback=None):
    if symbol not in PRICES:
        raise market_mod.DataError("no instrument found for %r" % symbol)
    return NS(symbol=symbol, interval=interval, last_price=PRICES[symbol],
              timestamp=[], low=[], high=[], close=[PRICES[symbol]],
              meta={"regularMarketTime": QUOTED_AT}, market_open=True,
              last_bar_time="")


real = {"bars": market_mod.fetch_bars, "analyse": web.engine.analyse,
        "cached": web.cached_analysis, "find": ideas_mod.find,
        "screen": screener_mod.run, "history": market_mod.split_history}
market_mod.fetch_bars = fake_bars
# No splits anywhere unless a check says so; and no request for them.
market_mod.split_history = lambda symbol: []
web.engine.analyse = must_not_run("an analysis")
web.cached_analysis = must_not_run("an analysis")
ideas_mod.find = must_not_run("a Find scan")
screener_mod.run = must_not_run("a screen")
try:
    print("=" * 72)
    print("THE FRONT PAGE, FROM WHAT IS SAVED")
    print("=" * 72)

    conn = db.connect(DB)
    # Alice's book: one position past its stop, one at its target, one running.
    za = db.open_trade(conn, "ZZA", "1d", 1, 100.0, 95.0, 120.0, quantity=10, user=alice.id)
    db.open_trade(conn, "ZZB", "1d", 1, 100.0, 90.0, 105.0, quantity=10, user=alice.id)
    db.open_trade(conn, "ZZC", "1d", 1, 100.0, 95.0, 110.0, quantity=10, user=alice.id)
    db.open_trade(conn, "ZZBOB", "1d", 1, 100.0, 95.0, 110.0, quantity=10, user=bob.id)
    # Her alert history: one this morning, one from last week, and the
    # background check's own note of the stop ZZA has crossed.
    db.event_add(conn, alice.id, "price_above", "ZZQ", "ZZQ rose above 10.00, at 10.40.",
                 10.4, "12 Nov 14:05 UTC")
    db.event_add(conn, alice.id, "stop_breach", "ZZA", "ZZA is at 90.00, past the stop "
                 "of 95.00 on your long position.", 90.0, "12 Nov 14:05 UTC", trade_id=za)
    old = db.event_add(conn, alice.id, "price_below", "ZZOLD", "ZZOLD fell below 5.", 4.9, "")
    conn.execute("UPDATE alert_events SET ts=? WHERE id=?", (NOW - 5 * 86400, old))
    db.event_add(conn, bob.id, "price_above", "ZZBOBALERT", "Bob's alert fired.", 1.0, "")
    # Her watchlist: a popular stock, one she analysed herself, one never looked at.
    db.watch_add(conn, "NVDA", "1d", user=alice.id)
    db.watch_add(conn, "ZZOWN", "1h", user=alice.id)
    db.watch_add(conn, "ZZNEW", "1d", user=alice.id)
    # Today's popular checks, and one from three days ago.
    for sym, action, age, samples, worked in (("NVDA", "BUY", 2 * 3600, 52, 34),
                                              ("TSLA", "BUY", 3 * 3600, 61, 40),
                                              ("AMD", "WAIT", 3600, 30, 12),
                                              ("META", "BUY", 3 * 86400, 70, 50)):
        public_mod.save_verdict(conn, {
            "symbol": sym, "name": public_mod.NAMES[sym], "action": action,
            "headline": "", "sure": "", "points": [], "samples": samples,
            "worked": worked, "reliable": True, "price": 182.4, "currency": "USD",
            "interval": "1d", "stop": None, "target": None, "horizon": 10,
            "price_day": (NOW - age) // 86400, "at": NOW - age})
    conn.close()
    add_run(alice, "ZZOWN", "WAIT", interval="1h", ts=NOW - 2 * 86400, price=12.5,
            market_open=0)
    add_run(bob, "ZZNEW", "BUY", ts=NOW - 60)

    sign_in(c, alice)
    t0 = time.time()
    r = c.get("/")
    took = time.time() - t0
    body = text(r)
    check("the front page draws", r.status_code == 200, r.status_code)
    check("without starting an analysis or a scan", not started, started)
    check("quickly, from what is stored", took < 3.0, "%.1fs" % took)
    top = body[body.index("Needs your attention"):body.index('class="searchrow"')]
    check("it starts with what needs her", "Needs your attention" in body
          and body.index("Needs your attention") < body.index('class="searchrow"'))
    check("a position past its stop is named, with how old its price is",
          "<b>ZZA</b> long: at 90, past its stop of 95. The journal does not close it "
          "for you. <span class=\"faint\">Price as of 12 Nov 14:05 UTC.</span>" in flat(top))
    check("and the card says it plainly", 'class="card breach"' in top)
    check("a position at its target is named too",
          "<b>ZZB</b> long: at 106, at or past its target of 105." in flat(top))
    check("a position still running is not", "<b>ZZC</b>" not in top)
    check("nor is anyone else's position", "ZZBOB" not in body)
    check("an alert from today is there, with when and how old its price is",
          "ZZQ rose above 10.00" in top and "price as of 12 Nov 14:05 UTC" in top
          and "Told you just now" in top)
    check("one from last week is not", "ZZOLD" not in top)
    check("the crossed stop is said once, not again as an alert",
          "past the stop of 95.00 on your long position" not in top)
    check("and nobody else's alert", "ZZBOBALERT" not in body)
    check("a popular stock on the watchlist shows the daily check and its age",
          '<a href="/stock/nvda"><b class="fig">NVDA</b></a> <span class="chip long">BUY'
          '</span> <span class="faint">daily check 2h ago at 182.4</span>' in flat(top))
    check("one she analysed herself shows her own call and its age",
          '<b class="fig">ZZOWN</b></a> <span class="chip hold">WAIT</span> <span '
          'class="faint">your 1h analysis 2d ago at 12.5, market closed</span>' in flat(top))
    check("one never analysed says so, whatever anyone else ran",
          '<b class="fig">ZZNEW</b></a> <span class="faint">not analysed yet</span>'
          in flat(top))
    check("today's BUY calls are a shortlist with their counts and age",
          "BUY calls in today's popular checks" in top
          and "Tested on past prices: it hit its target before its stop 34 of 52 "
              "times." in flat(top)
          and "Checked 2h ago at 182.4." in top and ">TSLA<" in top)
    check("labelled as a shortlist, not advice", "A shortlist to look into, not advice" in top)
    check("a WAIT and a three-day-old BUY are not on it",
          ">AMD<" not in top and ">META<" not in top)
    check("the search box does not jump the page past the list", "autofocus" not in body)
    check("and the rest of the page is still there",
          "Your record" in body and "Recent analyses" in body)

    # The popular page a shortlist row opens.
    check("each shortlist row opens the stock's public page",
          'href="/stock/nvda"' in top and 'href="/stock/tsla"' in top)

    print()
    print("--- when nothing needs her ---")
    conn = db.connect(DB)
    carol = A.upsert_google_user(conn, {"email": "carol@example.com", "sub": "g-c",
                                        "name": "Carol", "email_verified": True})
    saved = [dict(r) for r in conn.execute("SELECT * FROM public_verdicts")]
    conn.execute("DELETE FROM public_verdicts")
    conn.commit()
    conn.close()
    sign_in(c, carol)
    body = text(c.get("/"))
    check("an empty book says so in a sentence",
          "Nothing has happened that needs you. You have no open positions, and no "
          "alert has fired in the last three days." in flat(body))
    check("an empty watchlist says how to fill it", "Nothing on your watchlist yet" in body)
    check("with no checks today, it says they have not run",
          "The popular checks have not run in the last day" in body)
    check("and with nothing to read, the search box takes the cursor", "autofocus" in body)
    conn = db.connect(DB)
    public_mod.save_verdict(conn, {"symbol": "AMD", "name": "AMD", "action": "WAIT",
                                   "samples": 30, "worked": 12, "price": 150.0,
                                   "price_day": NOW // 86400, "at": NOW - 600})
    conn.close()
    body = text(c.get("/"))
    check("checks that ran with no BUY say that instead",
          "None of today's popular checks says BUY" in body)
    conn = db.connect(DB)
    conn.execute("DELETE FROM public_verdicts")
    for row in saved:
        conn.execute("INSERT INTO public_verdicts (symbol, data, updated) VALUES (?,?,?)",
                     (row["symbol"], row["data"], row["updated"]))
    conn.commit()
    conn.close()

    print()
    print("--- a weekend price is not passed off as fresh ---")
    check("a check on Sunday of Friday's close names the close",
          web._quote_day({"price_day": 20403, "at": 20405 * 86400 + 3600}) == "11 Nov")
    check("a check on the price's own day says nothing more",
          web._quote_day({"price_day": 20405, "at": 20405 * 86400 + 3600}) == "")

    print()
    print("--- a slow price source cannot hold the page ---")
    sign_in(c, alice)
    real_wait = web.DASHBOARD_WAIT

    def slow_bars(symbol, interval="1d", lookback=None):
        time.sleep(1.5)
        return fake_bars(symbol, interval)

    market_mod.fetch_bars = slow_bars
    web.DASHBOARD_WAIT = 0.3
    try:
        t0 = time.time()
        body = text(c.get("/"))
        took = time.time() - t0
    finally:
        web.DASHBOARD_WAIT = real_wait
        market_mod.fetch_bars = fake_bars
    check("the page answers without waiting for the prices", took < 1.2, "%.1fs" % took)
    check("and says the stops were not checked here",
          "did not come back in time, so their stops are not checked here" in flat(body)
          and "ZZQ rose above" in body)
    time.sleep(1.6)                    # let the abandoned fetches finish

    print()
    print("=" * 72)
    print("THE REVIEW BEFORE A PAPER TRADE")
    print("=" * 72)
    buy = add_run(alice, "ZZBUY", "BUY", 100.0, 95.0, 110.0)
    r = c.get("/trades/simulate?run_id=%d" % buy,
              headers={"Referer": "http://localhost/analyse?symbol=ZZBUY&interval=1d"})
    body = text(r)
    check("the review page opens", r.status_code == 200, r.status_code)
    check("without analysing anything", not started, started)
    check("it says plainly that nothing is bought or sold",
          "Nothing is bought or sold, and no order goes to any broker" in body)
    check("it shows the side", '<span class="word">Long</span>' in body
          and "it gains if the price rises" in body)
    check("the entry, stop and target",
          '<dt>Entry</dt><dd class="fig">100</dd>' in body
          and '<dt>Stop</dt><dd class="fig loss">95</dd>' in body
          and '<dd class="fig gain">110</dd>' in body)
    plan = web._paper_size({"entry": 100.0, "stop": 95.0, "target1": 110.0,
                            "action": "BUY", "asset_class": "equity"},
                           {"account_size": 10000.0, "risk_per_trade_pct": 1.0,
                            "max_position_pct": 25.0})
    check("the size from the journal's own rule", plan["quantity"] == 20.0
          and '<span class="fig">20</span> units' in body, plan)
    check("the money at risk and its share of the account",
          '<span class="fig">100.00</span>' in body and "1.00%</span>" in body
          and "of 10,000.00" in body)
    check("its reward to risk", "2.00&thinsp;:&thinsp;1" in body)
    check("Confirm posts the run to be logged",
          re.search(r'action="/trades/take">\s*<input type="hidden" name="run_id" value="%d">'
                    % buy, body) is not None)
    check("Cancel goes back to the analysis it came from",
          'href="/analyse?symbol=ZZBUY&amp;interval=1d">Cancel</a>' in body)
    body = text(c.get("/trades/simulate?run_id=%d" % buy))
    check("with no page to go back to, Cancel goes to the journal, not to a new analysis",
          'href="/trades">Cancel</a>' in body)
    check("a fresh plan carries no warning about its age", "This plan was made" not in body)

    conn = db.connect(DB)
    check("nothing was written by looking",
          not db.list_trades(conn, user=alice.id, symbol="ZZBUY"))
    conn.close()
    r = c.post("/trades/take", data={"run_id": buy})
    conn = db.connect(DB)
    logged = db.list_trades(conn, symbol="ZZBUY", user=alice.id)
    conn.close()
    check("Confirm logs exactly what was shown",
          r.status_code == 302 and len(logged) == 1
          and logged[0]["quantity"] == 20.0 and logged[0]["risk_amount"] == 100.0
          and logged[0]["entry"] == 100.0 and logged[0]["stop"] == 95.0,
          [dict(x) for x in logged])
    check("and the journal it lands on says so first",
          "Logged as a paper trade" in text(c.get(r.headers["Location"])))
    c.post("/trades/take", data={"run_id": buy})
    conn = db.connect(DB)
    check("pressing Confirm twice logs it once",
          len(db.list_trades(conn, symbol="ZZBUY", user=alice.id)) == 1)
    conn.close()
    body = text(c.get("/trades/simulate?run_id=%d" % buy))
    check("and the review then says it is already in the journal",
          "already in your journal" in body and 'action="/trades/take"' not in body)

    short = add_run(alice, "ZZSHORT", "SHORT", 100.0, 105.0, 90.0)
    body = text(c.get("/trades/simulate?run_id=%d" % short))
    check("a SHORT is shown as a short", '<span class="word">Short</span>' in body
          and "it gains if the price falls" in body)
    wait = add_run(alice, "ZZWAIT", "WAIT", 95.0, 90.0, 105.0)
    r = c.get("/trades/simulate?run_id=%d" % wait)
    check("a WAIT has nothing to review", r.status_code == 400
          and "Nothing has filled yet" in text(r))
    avoid = add_run(alice, "ZZAVOID", "AVOID")
    check("nor has an AVOID", c.get("/trades/simulate?run_id=%d" % avoid).status_code == 400)
    tight = add_run(alice, "ZZTIGHT", "BUY", 100.0, 99.0, 102.0)
    body = text(c.get("/trades/simulate?run_id=%d" % tight))
    check("a position the size cap trims says so",
          '<span class="fig">25</span> units' in body and "second limit applied" in body
          and "0.25%</span>" in body)
    coin = add_run(alice, "ZZC-USD", "BUY", 50000.0, 49000.0, 52000.0, asset="crypto")
    body = text(c.get("/trades/simulate?run_id=%d" % coin))
    check("a coin is sized in fractions", '<span class="fig">0.05</span> units' in body)
    stale = add_run(alice, "ZZSTALE", "BUY", ts=NOW - 3 * 3600)
    body = text(c.get("/trades/simulate?run_id=%d" % stale))
    check("a plan made hours ago warns that the price may have moved",
          "This plan was made 3h ago" in body and "not the price now" in body)
    # A plan made before a split and confirmed after it: its prices are in
    # the shares of its day, so they are divided by the split, shown so and
    # logged so. Logged as made, the journal read them as a 90% fall.
    def split_fake(symbol, interval="1d", lookback=None):
        if symbol != "ZZSPLIT":
            return fake_bars(symbol, interval, lookback)
        return NS(symbol=symbol, interval=interval, last_price=101.0,
                  timestamp=[NOW - (10 - k) * 86400 for k in range(10)],
                  low=[], high=[], close=[101.0], splits=[(NOW - 86400, 10.0)],
                  meta={"regularMarketTime": QUOTED_AT}, market_open=True,
                  last_bar_time="")

    market_mod.fetch_bars = split_fake
    before_split = add_run(alice, "ZZSPLIT", "BUY", entry=1000.0, stop=950.0, target=1100.0,
                           ts=NOW - 3 * 86400)
    body = text(c.get("/trades/simulate?run_id=%d" % before_split))
    check("a plan made before a split is shown in today's shares, and says why",
          "split 10-for-1 after this plan was made" in body, body[:400])
    c.post("/trades/take", data={"run_id": before_split})
    conn = db.connect(DB)
    got = db.list_trades(conn, symbol="ZZSPLIT", user=alice.id)
    conn.close()
    check("and is logged in today's shares",
          len(got) == 1 and abs(got[0]["entry"] - 100.0) < 1e-9
          and abs(got[0]["stop"] - 95.0) < 1e-9 and abs(got[0]["target1"] - 110.0) < 1e-9,
          [dict(x) for x in got])
    market_mod.fetch_bars = fake_bars
    # A listing too young for three months of daily bars, or one gone: the
    # whole history decides, and the plan can still be confirmed.
    young = add_run(alice, "ZZYOUNG", "BUY", ts=NOW - 2 * 3600)
    check("a plan on a young listing, over an hour old, can still be reviewed",
          c.get("/trades/simulate?run_id=%d" % young).status_code == 200)
    check("an unknown run is not found", c.get("/trades/simulate?run_id=999999").status_code == 404)
    check("nor is a run that is not a number", c.get("/trades/simulate?run_id=x").status_code == 404)
    sign_in(c, bob)
    check("Bob cannot review Alice's run", c.get("/trades/simulate?run_id=%d" % buy).status_code == 404)
    check("nor log it", c.post("/trades/take", data={"run_id": short}).status_code == 400)
    check("and drawing the review never started an analysis", not started, started)

    # A position that could not be checked is said to be so: the page does
    # not claim it is not past its stop.
    sign_in(c, alice)
    conn = db.connect(DB)
    gone = db.open_trade(conn, "ZZGONE", "1d", 1, 100.0, 95.0, 110.0, quantity=1,
                         user=alice.id)
    conn.close()
    body = " ".join(text(c.get("/")).split())
    check("a position that could not be checked is not called fine",
          "could not be checked just now" in body
          and "None of your open positions is past its stop" not in body, body[:600])
    conn = db.connect(DB)
    conn.execute("DELETE FROM trades WHERE id=?", (gone,))
    conn.commit()
    conn.close()

    # A saved thesis that cannot be priced just now (a split that cannot be
    # ruled out) is said, and the research page stands.
    real_research = web.engine.research
    long_ago = [NOW - 60 * 86400 + k * 3000 for k in range(3)]
    stand_in = NS(symbol="ZZRES", name="Res Corp", last_price=101.0, interval="15m",
                  close=[101.0] * 3, timestamp=long_ago, splits=[], meta={},
                  market_open=True, last_bar_time="", asset_class="equity",
                  currency="USD")

    def history_down(symbol):
        raise market_mod.DataUnavailable("the source did not answer")

    web.cached_analysis = lambda *a, **k: NS(bars=stand_in)
    web.engine.research = lambda symbol, price, cfg, **k: web.engine.Research(
        symbol=symbol, price=price)
    market_mod.split_history = history_down
    web._RESEARCH_CACHE.clear()
    conn = db.connect(DB)
    conn.execute("INSERT INTO theses (symbol, created_ts, price, balance, verdict, user_id) "
                 "VALUES (?,?,?,?,?,?)", ("ZZRES", NOW - 90 * 86400, 100.0, 0.4,
                                          "lean bull", alice.id))
    conn.commit()
    conn.close()
    try:
        r = c.get("/research?symbol=ZZRES&interval=15m")
        page = r.data.decode("utf-8", "replace")
    finally:
        web.cached_analysis = must_not_run("an analysis")
        web.engine.research = real_research
        market_mod.split_history = lambda symbol: []
        web._RESEARCH_CACHE.clear()
    check("a saved thesis that cannot be priced just now leaves the research page standing",
          r.status_code == 200 and "Could not price ZZRES today" in page, r.status_code)

    # A thesis that was closed keeps the outcome recorded for it: the price
    # recovering afterwards made a "wrong" one read "right so far".
    def thesis_of(balance, case):
        return NS(balance=balance, verdict="lean", evidence_count=1, unknowns=[], neutral=[],
                  bull=NS(summary="", points=case), bear=NS(summary="", points=[]),
                  falsifiers=["A price below 80"], all_sources=[])

    asked = []

    def research_stub(symbol, price, cfg, **k):
        asked.append(k)
        points = [NS(headline="Coverage is positive", detail="d", evidence=[])]
        return web.engine.Research(symbol=symbol, price=price, thesis=thesis_of(0.19, points))

    web.cached_analysis = lambda *a, **k: NS(bars=stand_in)
    web.engine.research = research_stub
    market_mod.split_history = lambda symbol: []
    web._RESEARCH_CACHE.clear()
    conn = db.connect(DB)
    conn.execute("DELETE FROM theses WHERE symbol='ZZRES'")
    conn.execute("INSERT INTO theses (symbol, created_ts, price, balance, verdict, user_id, "
                 "outcome, closed_price, closed_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("ZZRES", NOW - 90 * 86400, 100.0, 0.4, "lean bull", alice.id, "wrong", 60.0,
                  NOW - 30 * 86400))
    conn.commit()
    conn.close()
    daily = NS(symbol="ZZRES", interval="1d", close=[50.0 + k / 10 for k in range(400)],
               timestamp=[NOW - (399 - k) * 86400 for k in range(400)], gmt_offset=0)
    market_mod.fetch_bars = lambda symbol, interval="1d", lookback=None: daily
    try:
        sign_in(c, alice)
        page = flat(text(c.get("/research?symbol=ZZRES&interval=15m")))
        saved_part = page[page.index("Theses you saved"):]
        check("a closed thesis keeps the outcome recorded for it",
              ">wrong</span>" in saved_part and "so far" not in saved_part
              and "closed 30d ago" in saved_part, saved_part[:1200])
        check("at the price it was closed at", "-40.0%" in saved_part
              and 'data-label="Now">60 ' in saved_part and "101" not in saved_part,
              saved_part[:1200])

        # The chart named for a year is a year of daily prices, whatever bars
        # the analysis was opened on.
        check("opened from 15-minute bars, the year's chart is of daily prices",
              "<h2>The last year</h2>" in page and "daily closing prices" in page
              and "about a year ago" not in page and "15m bars" not in page)
        start = time.gmtime(NOW - 366 * 86400)
        check("and its left end is dated",
              "%d %s" % (start.tm_mday, time.strftime("%b %Y", start)) in page)
        young = NS(symbol="ZZRES", interval="1d", close=daily.close[-40:],
                   timestamp=daily.timestamp[-40:], gmt_offset=0)
        check("forty days of prices are not called a year",
              web._year_chart(young)["title"] == "The last 39 days",
              web._year_chart(young)["title"])
        market_mod.fetch_bars = must_not_run("a request for prices")
        check("daily bars already in hand are used as they are",
              web._year_chart(daily)["title"] == "The last year")
        check("and with none to be had the chart is left out",
              web._year_chart(stand_in) == {})
        started.clear()

        # What is saved is the case that was on the page.
        asked.clear()
        r = c.post("/thesis/save", data={"symbol": "ZZRES", "interval": "15m"})
        conn = db.connect(DB)
        row = db.thesis_list(conn, "ZZRES", user=alice.id)[0]
        conn.close()
        check("saving a thesis saves the one on the page, worked out once",
              r.status_code == 302 and not asked and row["balance"] == 0.19
              and row["price"] == 101.0 and "Coverage is positive" in row["bull_json"],
              (r.status_code, asked, dict(row)))
        web._RESEARCH_CACHE.clear()
        c.post("/thesis/save", data={"symbol": "ZZRES", "interval": "15m"})
        check("and one whose page is no longer kept is worked out as the page is",
              len(asked) == 1 and asked[0]["with_options"] is True
              and asked[0]["with_reasoning"] is False, asked)
        c.post("/thesis/save", data={"symbol": "ZZRES", "interval": "15m", "options": "0"})
        check("a page without options saves without them", asked[-1]["with_options"] is False)

        # Two people opening one page, or one person twice, share the work.
        import threading
        together, done = threading.Barrier(2), []

        def slow_research(symbol, price, cfg, **k):
            asked.append(k)
            time.sleep(0.4)
            return web.engine.Research(symbol=symbol, price=price)

        web.engine.research = slow_research
        web._RESEARCH_CACHE.clear()
        asked.clear()

        def open_page():
            client = web.app.test_client()
            sign_in(client, alice)
            together.wait(5)
            done.append(client.get("/research?symbol=ZZRES&interval=1d").status_code)

        market_mod.fetch_bars = fake_bars
        threads = [threading.Thread(target=open_page) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        check("two requests at once build the research once",
              done == [200, 200] and len(asked) == 1 and not web._RESEARCH_RUNNING,
              (done, asked))

        # The example is open to anyone, so its address cannot make the server
        # build it again, nor send a paid request.
        visitor = web.app.test_client()
        asked.clear()
        stand_in.interval, stand_in.symbol = "1d", "NVDA"
        real_clock, web.time = web.time, NS(**{k: getattr(time, k) for k in (
            "time", "gmtime", "strftime", "localtime", "mktime", "sleep", "monotonic")
            if hasattr(time, k)})
        try:
            statuses = []
            for later in (0, 600):
                web.time.time = lambda later=later: time.time() + later
                for extra in ("", "&news=0", "&options=0", "&news=0&options=0"):
                    statuses.append(visitor.get("/research?symbol=NVDA&interval=1d" + extra)
                                    .status_code)
        finally:
            web.time = real_clock
            stand_in.interval, stand_in.symbol = "15m", "ZZRES"
        check("the example's research is built once, whatever the address asks for",
              set(statuses) == {200} and len(asked) == 1, (statuses, asked))
        check("and without the paid written reasoning",
              asked and asked[0]["with_reasoning"] is False, asked)
        sign_in(c, alice)
        stand_in.interval, stand_in.symbol = "1d", "NVDA"
        c.get("/research?symbol=NVDA&interval=1d")
        stand_in.interval, stand_in.symbol = "15m", "ZZRES"
        check("an account's own page is never the example's copy",
              len(asked) == 2 and asked[1]["with_reasoning"] is True, asked)
    finally:
        web.cached_analysis = must_not_run("an analysis")
        web.engine.research = real_research
        market_mod.split_history = lambda symbol: []
        market_mod.fetch_bars = fake_bars
        web._RESEARCH_CACHE.clear()

    print()
    print("=" * 72)
    print("THE JOURNAL: WHAT WAS PLANNED, WHAT HAPPENED")
    print("=" * 72)
    sign_in(c, alice)
    body = text(c.get("/trades"))
    row = body[body.index("<b>ZZBUY</b>"):]
    row = row[:row.index("</tr>")]
    check("a trade from an analysis says which call it came from",
          "from your BUY call" in row, row[:300])
    check("and the reward to risk it was planned at", "plans 2.00&thinsp;:&thinsp;1" in row)
    hand = c.post("/trades/open", data={"symbol": "ZZHAND", "direction": "short",
                                        "entry": "50", "stop": "52", "target": "44"})
    body = text(c.get("/trades"))
    row = body[body.index("<b>ZZHAND</b>"):]
    row = row[:row.index("</tr>")]
    check("one entered by hand has a plan but no call", hand.status_code == 302
          and "plans 3.00&thinsp;:&thinsp;1" in row and "from your" not in row, row[:300])
    conn = db.connect(DB)
    tid = db.list_trades(conn, symbol="ZZBUY", user=alice.id)[0]["id"]
    conn.close()
    c.post("/trades/close", data={"trade_id": tid, "price": "95", "reason": "stop"})
    body = text(c.get("/trades"))
    closed = body[body.index("<h2>Closed</h2>"):]
    row = closed[closed.index("<b>ZZBUY</b>"):]
    row = row[:row.index("</tr>")]
    check("a closed trade sets what it made against what was planned",
          "<b>-1.00</b>" in row and "of +2.00 planned" in row and "from your BUY call" in row,
          row[:500])
    conn = db.connect(DB)
    conn.execute("UPDATE trades SET strategy_note='from run #1 (WAIT)' WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    body = text(c.get("/trades"))
    check("an old trade taken from a WAIT says so", "from your WAIT call" in body)
finally:
    market_mod.fetch_bars = real["bars"]
    market_mod.split_history = real["history"]
    web.engine.analyse = real["analyse"]
    web.cached_analysis = real["cached"]
    ideas_mod.find = real["find"]
    screener_mod.run = real["screen"]

print()
print("=" * 72)
print("COUNTS FOR THE OPERATOR, AND NOTHING ABOUT WHO")
print("=" * 72)

stranger = web.app.test_client()
before = count("landing_view")
stranger.get("/", headers=BROWSER)
check("a visitor's look at the front page is counted", count("landing_view") == before + 1)
stranger.get("/", headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
stranger.get("/", headers={"User-Agent": "WhatsApp/2.23.20.0"})
stranger.get("/", headers={"User-Agent": ""})
stranger.head("/", headers=BROWSER)
check("robots, link previews and HEAD requests are not",
      count("landing_view") == before + 1, count("landing_view"))
sign_in(stranger, alice)
stranger.get("/", headers=BROWSER)
check("nor is a signed-in person's dashboard", count("landing_view") == before + 1)

# A sign-up through the emailed code, as a person would do it.
_os.environ.update(MAIL)
outbox = {}
real_send = codes._smtp_send
codes._smtp_send = lambda to, code: outbox.__setitem__(to, code)
try:
    newcomer = web.app.test_client()
    before = count("sign_up")
    for attempt in range(2):
        newcomer.post("/signin/email", data={"to": "newcomer@example.com"})
        r = newcomer.post("/signin/email/code", data={"code": outbox["newcomer@example.com"]})
        if attempt == 0:
            check("a new account made by signing in is counted",
                  r.status_code == 302 and count("sign_up") == before + 1, r.status_code)
        newcomer.post("/signout")
    check("signing in to it again is not", count("sign_up") == before + 1, count("sign_up"))
finally:
    codes._smtp_send = real_send
    for key in MAIL:
        _os.environ.pop(key, None)
conn = db.connect(DB)
before = count("sign_up")
events.new_account(conn, A.upsert_google_user, {"email": "gina@example.com", "sub": "g-g",
                                                "email_verified": True})
events.new_account(conn, A.upsert_google_user, {"email": "gina@example.com", "sub": "g-g",
                                                "email_verified": True})
conn.close()
check("so is a new Google account, once", count("sign_up") == before + 1)

# First analyses: a stand-in analysis that records a run, as the real one does.
from bot import engine as engine_mod  # noqa: E402


def recording_analyse(symbol, cfg, interval=None, with_news=True, source="", db_path=None,
                      user=1, record=True, **_kw):
    bars = NS(symbol=symbol.upper(), name=symbol, asset_class="equity", interval=interval,
              last_price=100.0, market_open=True)
    plan = NS(conviction=0.5, action="BUY", headline="", entry=100.0, stop=95.0,
              target1=110.0, target2=None, reward_risk=2.0, breakeven_rate=0.4,
              probability=0.5, prob_samples=60, expectancy_r=0.1)
    regime = NS(trend="up", volatility="normal", direction=1, adx=20.0)
    calib, mood = NS(cost_r=0.01), NS(score=0.0, items=[])
    run_id = None
    if record:
        rconn = db.connect(DB)
        run_id = db.record_run(rconn, bars, plan, [], 0.3, regime, calib, mood,
                               source=source, user=user)
        rconn.close()
    return engine_mod.Analysis(bars=bars, context=None, signals=[], composite=0.3,
                               regime=regime, levels=[], calibration=calib,
                               sentiment=mood, plan=plan, run_id=run_id)


def analyse_as(user, symbol):
    conn = db.connect(DB)
    try:
        with web.app.test_request_context("/"):
            web.g.user, web.g.demo = A.get_user(conn, user.id), False
            return web.cached_analysis(symbol, "1d", web.config_mod.load(harness.CFG_PATH),
                                       False)
    finally:
        conn.close()


real_analyse = web.engine.analyse
web.engine.analyse = recording_analyse
web._CACHE.clear()
try:
    conn = db.connect(DB)
    hank = A.upsert_email_user(conn, "hank@example.com")
    ivy = A.upsert_email_user(conn, "ivy@example.com")
    jo = A.upsert_email_user(conn, "jo@example.com")
    conn.close()
    before = count("first_analysis")
    analyse_as(hank, "ZZF1")
    check("a person's first analysis is counted", count("first_analysis") == before + 1)
    analyse_as(hank, "ZZF2")
    analyse_as(hank, "ZZF1")
    check("their second, and a refresh of the first, are not",
          count("first_analysis") == before + 1)
    analyse_as(ivy, "ZZF1")
    check("someone else's first, from the shared analysis, is",
          count("first_analysis") == before + 2)
    add_run(jo, "ZZROUTINE", "BUY", source="workflow")
    analyse_as(jo, "ZZF3")
    check("a routine's run before it does not hide a first look on the site",
          count("first_analysis") == before + 3)
finally:
    web.engine.analyse = real_analyse
    web._CACHE.clear()

sign_in(c, bob)
before = {name: count(name) for name in ("watch_add", "alert_add", "paper_trade")}
c.post("/watch/add", data={"symbol": "ZZW", "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "ZZW", "kind": "price_above",
                                   "threshold": "5", "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "ZZW", "kind": "nonsense", "threshold": "5"})
c.post("/trades/open", data={"symbol": "ZZW", "direction": "long", "entry": "10",
                             "stop": "9"})
c.post("/trades/open", data={"symbol": "ZZW", "direction": "long", "entry": "10",
                             "stop": "11"})
bob_buy = add_run(bob, "ZZBB", "BUY")
c.post("/trades/take", data={"run_id": bob_buy})
c.post("/trades/take", data={"run_id": bob_buy})
check("a watchlist add is counted", count("watch_add") == before["watch_add"] + 1)
check("an alert set is counted, a refused one is not",
      count("alert_add") == before["alert_add"] + 1)
check("paper trades are counted, by hand or from a call, once each",
      count("paper_trade") == before["paper_trade"] + 2, count("paper_trade"))
body = text(c.get("/monitor"))
form = body[body.index('action="/monitor/alert/delete"') - 40:]
form = form[:form.index("</form>")]
check("deleting an alert asks first, like every other delete",
      "return confirm('Delete this alert? This cannot be undone.');" in form, form[:200])

# Turning Pro, told by Stripe and by the page a buyer comes back to.
_os.environ.update(STRIPE)
real_stripe = billing._stripe
conn = db.connect(DB)
kim = A.upsert_email_user(conn, "kim@example.com")
conn.close()


def fake_stripe(method, path, data=None):
    if path.startswith("checkout/sessions/"):
        return {"status": "complete", "client_reference_id": str(kim.id),
                "customer": "cus_kim", "payment_status": "paid",
                "created": int(time.time()) - 60,
                "subscription": {"id": "sub_kim", "status": "active",
                                 "current_period_end": int(time.time()) + 30 * 86400}}
    return {}


def signed(payload, secret="whsec_test"):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), ("%d." % ts).encode() + payload,
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, sig)


def webhook(kind, created, **obj):
    body = json.dumps({"type": kind, "created": created, "data": {"object": obj}}).encode()
    return c.post("/billing/webhook", data=body, headers={"Stripe-Signature": signed(body)})


billing._stripe = fake_stripe
try:
    before = count("turned_pro")
    sign_in(c, kim)
    r = c.get("/billing/done?session_id=cs_test_kim")
    check("a buyer coming back from paying is counted as turning Pro",
          "Pro" in text(r) and count("turned_pro") == before + 1, count("turned_pro"))
    webhook("checkout.session.completed", int(time.time()), client_reference_id=str(kim.id),
            customer="cus_kim", payment_status="paid")
    c.get("/billing/done?session_id=cs_test_kim")
    check("Stripe's own word of the same upgrade, and a reload, are not counted again",
          count("turned_pro") == before + 1, count("turned_pro"))
    webhook("customer.subscription.updated", int(time.time()) + 5, customer="cus_kim",
            status="active", current_period_end=int(time.time()) + 60 * 86400)
    check("nor is a renewal", count("turned_pro") == before + 1)
    conn = db.connect(DB)
    lee = A.upsert_email_user(conn, "lee@example.com")
    conn.close()
    webhook("checkout.session.completed", int(time.time()), client_reference_id=str(lee.id),
            customer="cus_lee", payment_status="unpaid")
    check("a payment still clearing is not yet a change of plan",
          count("turned_pro") == before + 1)
    webhook("customer.subscription.updated", int(time.time()) + 5, customer="cus_lee",
            status="active", metadata={"user_id": str(lee.id)})
    check("and is counted once it clears", count("turned_pro") == before + 2)
finally:
    billing._stripe = real_stripe
    for key in STRIPE:
        _os.environ.pop(key, None)

conn = db.connect(DB)
columns = [r[1] for r in conn.execute("PRAGMA table_info(usage_counts)")]
rows = [tuple(r) for r in conn.execute("SELECT day, name, n FROM usage_counts")]
conn.close()
check("the counts table holds a day, a name and a number, nothing else",
      columns == ["day", "name", "n"], columns)
check("and every name is one of the things counted",
      rows and all(name in dict(events.NAMES) for _day, name, _n in rows), rows)
check("with no account, address or ticker anywhere in it",
      not any(s in repr(rows) for s in ("alice", "@", "ZZ", "NVDA", "127.0.0.1")))

real_add = events._add


def broken(*_a, **_k):
    raise events.sqlite3.OperationalError("disk I/O error")


events._add = broken
web.app.logger.disabled = True
_logger = events.log.disabled
events.log.disabled = True
try:
    r = c.post("/watch/add", data={"symbol": "ZZBROKEN", "interval": "1d"})
    conn = db.connect(DB)
    kept = [w["symbol"] for w in db.watchlist(conn, user=kim.id)]
    conn.close()
    check("a count that cannot be written never costs the thing being counted",
          r.status_code == 302 and "ZZBROKEN" in kept, kept)
finally:
    events._add = real_add
    events.log.disabled = _logger
    web.app.logger.disabled = False

_os.environ["STOCKBOT_ADMIN_EMAIL"] = "alice@example.com"
try:
    sign_in(c, alice)
    body = text(c.get("/usage"))
    table = body[body.index("From visit to Pro"):]
    table = table[:table.index("</section>")]
    check("the operator sees the counts, day by day",
          "Front page views" in table and "Turned Pro" in table
          and time.strftime("%Y-%m-%d", time.gmtime()) in table)
    sign_in(c, bob)
    check("nobody else does", c.get("/usage").status_code == 403)
finally:
    _os.environ.pop("STOCKBOT_ADMIN_EMAIL", None)
body = text(c.get("/privacy"))
check("the privacy page says they are counts only",
      "These are counts only" in body and "cannot be traced back to you" in body)

print()
print("=" * 72)
print("ROUTINES SPEND A FREE ACCOUNT'S LOOKS LIKE ANY ANALYSIS")
print("=" * 72)

analysed = []
refuse = set()


def routine_analyse(symbol, cfg, interval=None, with_news=True, with_learning=True,
                    record=True, db_path=None, user=1, progress=None, source="engine"):
    if symbol in refuse:
        raise market_mod.DataError("no data for %s" % symbol)
    analysed.append(symbol)
    return NS(bars=NS(symbol=symbol.upper(), interval=interval), run_id=None,
              plan=NS(action="WAIT", headline="A stand-in call."))


def run_routine(name, user):
    del analysed[:]
    return workflows.run(name, web.config_mod.load(harness.CFG_PATH), DB, user=user.id)


def looks_used(user):
    conn = db.connect(DB)
    try:
        return billing.views_today(conn, user.id)
    finally:
        conn.close()


real_routine = (workflows.engine.analyse, workflows.screener_mod.run)
workflows.engine.analyse = routine_analyse
_os.environ.update(STRIPE)
try:
    conn = db.connect(DB)
    max_ = A.upsert_email_user(conn, "max@example.com")
    for sym in ("ZZR1", "ZZR2", "ZZR3", "ZZR4", "ZZR5"):
        db.watch_add(conn, sym, "1d", user=max_.id)
    conn.close()
    limit = billing.free_limits()["analyses_per_day"]
    report = run_routine("watchlist", max_)
    skipped = [s for s in report.steps if s.label == "Free allowance"]
    check("a free account's watchlist routine analyses only what its looks allow",
          len(analysed) == limit == 3 and looks_used(max_) == 3, (analysed, looks_used(max_)))
    check("and says which were left, and why",
          skipped and "ZZR4, ZZR5" in skipped[0].detail and skipped[0].status == "attention"
          and "midnight UTC" in skipped[0].detail, [s.detail for s in report.steps])
    check("the headline counts only what was analysed",
          report.headline.startswith("None of the 3 watched instruments changed.")
          and "2 more were not analysed" in report.headline, report.headline)
    report = run_routine("watchlist", max_)
    check("run again the same day, what it already opened costs nothing more",
          analysed == ["ZZR1", "ZZR2", "ZZR3"] and looks_used(max_) == 3, analysed)

    conn = db.connect(DB)
    nia = A.upsert_email_user(conn, "nia@example.com")
    for sym in ("ZZS1", "ZZS2", "ZZS3"):
        db.watch_add(conn, sym, "1d", user=nia.id)
    conn.close()
    refuse.add("ZZS2")
    run_routine("watchlist", nia)
    check("an analysis that fails hands its look back",
          analysed == ["ZZS1", "ZZS3"] and looks_used(nia) == 2, (analysed, looks_used(nia)))
    refuse.clear()

    conn = db.connect(DB)
    billing.set_plan(conn, max_.id, "pro", "active", "cus_max")
    conn.close()
    run_routine("watchlist", max_)
    check("on Pro, the whole watchlist is analysed", len(analysed) == 5, analysed)

    conn = db.connect(DB)
    oz = A.upsert_email_user(conn, "oz@example.com")
    for sym in ("ZZT1", "ZZT2", "ZZT3"):
        billing.reserve_view(conn, oz.id, "%s|1d" % sym)
    conn.close()
    workflows.screener_mod.run = lambda **_k: NS(
        passed=[NS(symbol=s) for s in ("ZZH1", "ZZH2", "ZZH3", "ZZH4")], scanned=12, notes=[])
    report = run_routine("hunt", oz)
    check("the hunt routine on a free account with no looks left analyses nothing",
          analysed == [] and "none was analysed" in report.headline, report.headline)
    _os.environ["STOCKBOT_ADMIN_EMAIL"] = "oz@example.com"
    report = run_routine("hunt", oz)
    check("while the operator's is never limited", len(analysed) == 4, analysed)
    _os.environ.pop("STOCKBOT_ADMIN_EMAIL", None)
finally:
    workflows.engine.analyse, workflows.screener_mod.run = real_routine
    for key in STRIPE:
        _os.environ.pop(key, None)

conn = db.connect(DB)
pat = A.upsert_email_user(conn, "pat@example.com")
for sym in ("ZZP1", "ZZP2", "ZZP3", "ZZP4"):
    db.watch_add(conn, sym, "1d", user=pat.id)
conn.close()
workflows.engine.analyse = routine_analyse
try:
    run_routine("watchlist", pat)
finally:
    workflows.engine.analyse = real_routine[0]
check("and with billing off nobody is",
      len(analysed) == 4 and looks_used(pat) == 0, (analysed, looks_used(pat)))

for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("FEATURES OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
