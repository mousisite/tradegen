"""Popular pages, sharing, invites and the weekly email."""
import calendar
import html as _html
import json
import os as _os
import re
import sys as _sys
import time
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


MAIL = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "MAIL_FROM": "Orenth <hi@example.com>"}
STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
          "STRIPE_WEBHOOK_SECRET": "whsec_x"}
for key in list(MAIL) + list(STRIPE) + ["MAIL_POSTAL_ADDRESS", "STOCKBOT_ADMIN_EMAIL"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from flask import session  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402
from bot import digest, invites, public  # noqa: E402

c, DB = harness.isolated_web("growth")
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"


def sign_in(user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with c.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


def sign_out():
    with c.session_transaction() as sess:
        sess.clear()


def fake_result(symbol="NVDA", action="AVOID", samples=83, worked=22):
    plan = SimpleNamespace(action=action, probability=worked / samples,
                           prob_samples=samples, prob_reliable=True, entry=None,
                           stop=None, target1=None, avg_win_r=None,
                           avg_loss_r=None, breakeven_rate=None, position={},
                           headline="technical headline")
    bars = SimpleNamespace(symbol=symbol, name="Some Corp", last_price=228.38,
                           interval="1d", currency="USD")
    return SimpleNamespace(plan=plan, bars=bars)


def snap_for(symbol, action="WAIT", samples=100, worked=40):
    return public.snapshot(fake_result(symbol, action, samples, worked),
                           {"headline": "Plain words for %s." % symbol})


def user(email):
    conn = db.connect(DB)
    try:
        return A.upsert_email_user(conn, email)
    finally:
        conn.close()


def fresh(user_id):
    conn = db.connect(DB)
    try:
        return A.get_user(conn, user_id)
    finally:
        conn.close()


try:
    print("=" * 72)
    print("POPULAR PAGES, CHECKED IN THE BACKGROUND")
    print("=" * 72)
    r = c.get("/stock/aapl")
    page = r.data.decode("utf-8", "replace")
    check("a popular page exists before its first check",
          r.status_code == 200 and "Being checked" in page, r.status_code)
    check("but asks search engines to wait for it", 'name="robots" content="noindex"' in page)
    check("an unknown symbol has no page", c.get("/stock/zzzz").status_code == 404)

    snap = snap_for("NVDA", "AVOID", 83, 22)
    check("a snapshot keeps the call and the counts",
          (snap["action"], snap["samples"], snap["worked"]) == ("AVOID", 83, 22))
    check("and uses the familiar name", snap["name"] == "NVIDIA")
    said = public.snapshot(fake_result("NVDA", "AVOID", 83, 22), {
        "headline": "h",
        "sure": "In tests on past prices, this same setup came up 83 times and "
                "worked 22 of them, reaching its target before its stop. When "
                "it worked it made about 2.0 times what you put at risk."})
    check("the counts are not said twice on the page",
          said["sure"].startswith("When it worked"), said["sure"])

    falling = fake_result("NVDA", "AVOID", 83, 61)
    falling.plan.odds_direction = -1
    from bot import plain as plain_words
    down = public.snapshot(falling, plain_words.explain_plan(falling.plan, falling.bars))
    check("a strong record of falls never reads as a record of buying",
          "a bet on the price falling worked 61 of them" in public.counts_line(down)
          and "a bet on a fall worked 61 of 83 times"
          in public.counts_line(down, short=True), public.counts_line(down))
    check("and the page does not say it twice", "Here, worked means" not in down["sure"],
          down["sure"])
    falling.plan.action, falling.plan.direction = "WAIT", -1
    short = public.snapshot(falling, plain_words.explain_plan(falling.plan, falling.bars))
    check("a plan to short says its counts as they are", not short["fall"]
          and "a bet on" not in public.counts_line(short))

    conn = db.connect(DB)
    asked, slept = [], []

    def analyse(symbol):
        asked.append(symbol)
        if symbol == "GME":
            raise RuntimeError("no data")
        return snap_for(symbol)

    out = public.refresh(conn, analyse, sleep=slept.append)
    check("the first run checks every popular symbol",
          len(asked) == len(public.POPULAR), len(asked))
    check("pausing between them, not before the first",
          len(slept) == len(public.POPULAR) - 1)
    check("one that fails is skipped, the rest are stored",
          out["checked"] == len(public.POPULAR) - 1 and len(out["errors"]) == 1, out)
    asked.clear()
    public.refresh(conn, analyse, sleep=slept.append)
    check("a run soon after only retries what failed", asked == ["GME"], asked)
    asked.clear()
    public.refresh(conn, analyse, now=time.time() + public.MAX_AGE + 1,
                   sleep=slept.append)
    check("a day later everything is checked again",
          len(asked) == len(public.POPULAR), len(asked))
    public.save_verdict(conn, down)
    page = c.get("/stock/nvda").data.decode("utf-8", "replace")
    said_by = [m for m in page.split("\n") if "61 of them" in m]
    check("not in the page, its link preview, its FAQ answer or its share text",
          len(said_by) >= 4 and all("a bet on the price falling worked 61" in m
                                    for m in said_by), said_by)
    public.save_verdict(conn, snap)
    conn.close()

    r = c.get("/stock/nvda")
    page = r.data.decode("utf-8", "replace")
    check("the page shows the call", r.status_code == 200 and "Avoid" in page
          and "In tests on past prices, this same setup came up 83 times and "
              "worked 22 of them" in page, r.status_code)
    check("titled the way people search", "NVIDIA (NVDA): buy, wait or avoid?" in page)
    check("open to search engines", "noindex" not in page)
    check("its link preview is its own card", "/stock/nvda/card.png" in page)
    r = c.get("/stock/nvda/card.png")
    check("the card is a real picture",
          r.status_code == 200 and r.mimetype == "image/png"
          and r.data[:8] == b"\x89PNG\r\n\x1a\n", r.status_code)
    check("that a cache may keep for an hour",
          "max-age=3600" in r.headers.get("Cache-Control", ""))
    block = re.search(r'(?s)<script type="application/ld\+json">\s*(\{[^<]*"@graph".*?)</script>',
                      page)
    nodes = json.loads(block.group(1))["@graph"] if block else []
    check("what it tells a search engine Orenth is does not become one stock's call",
          len(nodes) == 2 and all("NVIDIA" not in n["description"]
                                  and "/stock/" not in n.get("image", "") for n in nodes),
          nodes)
    page = c.get("/stocks").data.decode("utf-8", "replace")
    check("the list shows every popular symbol",
          all(">%s<" % public.short(s) in page for s, _ in public.POPULAR))
    check("crypto reads without its -USD", ">BTC<" in page and "BTC-USD<" not in page)
    sitemap = c.get("/sitemap.xml").data.decode()
    check("the sitemap lists the list and every page",
          "/stocks</loc>" in sitemap and "/stock/nvda</loc>" in sitemap
          and "/stock/btc-usd</loc>" in sitemap)

    # Checks that fail for days leave the last one standing. It was still
    # listed as a call under "checked daily" and titled "Today's check".
    conn = db.connect(DB)
    public.save_verdict(conn, dict(snap_for("GME", "BUY", 40, 30),
                                   at=int(time.time()) - 9 * 86400))
    conn.close()
    listed = [m for m in c.get("/stocks").data.decode().split("<li>") if "/stock/gme" in m][0]
    check("a check days old is not listed as a call", "BUY" not in listed
          and ">checking<" in listed, listed)
    page = " ".join(c.get("/stock/gme").data.decode("utf-8", "replace").split())
    when = time.strftime("%d %b %Y", time.gmtime(time.time() - 9 * 86400)).lstrip("0")
    check("and its page says when it was made, not that it is today's",
          "Last check " + when in page and "Last checked on " + when in page
          and "Today's check" not in page and "Today&#39;s check" not in page)
    check("and asks search engines to wait for the next one",
          'name="robots" content="noindex"' in page and '"FAQPage"' not in page)
    check("and its link preview and share text date it",
          "Orenth&#39;s check on GameStop (GME) on %s: BUY." % when in page
          and "GME: BUY on %s · Orenth" % when in page
          and "Orenth said BUY on GME on %s." % when in page
          and "Orenth says" not in page, page[:2500])
    check("and the sitemap leaves it out until then",
          "/stock/gme</loc>" not in c.get("/sitemap.xml").data.decode())
    check("a check two days old at most is today's",
          not public.stale({"at": 100}, now=100 + 2 * 86400)
          and public.stale({"at": 100}, now=101 + 2 * 86400) and not public.stale({}))

    conn = db.connect(DB)
    ran = []
    conn.set_trace_callback(ran.append)
    for read in (lambda: public.verdict(conn, "NVDA"), lambda: public.verdicts(conn),
                 lambda: public.symbol_record(conn, "NVDA"), lambda: public.record(conn),
                 lambda: public.record_mark(conn), lambda: public.calls_csv(conn),
                 lambda: public.fingerprints(conn), lambda: public.get_share(conn, "x")):
        read()
    conn.set_trace_callback(None)
    conn.close()
    check("reading a public page's data only reads", len(ran) == 8
          and all(s.startswith("SELECT") for s in ran), ran)

    for other, own in (("/stock/NVDA", "/stock/nvda"), ("/stock/Nvda?utm=x", "/stock/nvda?utm=x"),
                       ("/stock/NVDA/card.png", "/stock/nvda/card.png"),
                       ("/backtest/rsi_reversion", "/backtest/rsi-reversion"),
                       ("/backtest/RSI-Reversion", "/backtest/rsi-reversion"),
                       ("/stock/btc", "/stock/btc-usd"), ("/stock/BTC?utm=x", "/stock/btc-usd?utm=x"),
                       ("/stock/link", "/stock/link-usd"),
                       ("/stocks/", "/stocks"), ("/learn/?a=b", "/learn?a=b"),
                       ("/stock/nvda/", "/stock/nvda")):
        r = c.get(other)
        check("%s is sent to the page's one address" % other,
              r.status_code == 301 and r.headers["Location"].endswith(own),
              (r.status_code, r.headers.get("Location")))
    # The path as the server is handed it, its %2F and %5C already read.
    for wrong in ("/no-such-page/", "//elsewhere.example/", "/\\elsewhere.example/",
                  "/stock/zzzz", "/stock/usd"):
        r = c.get("/x", environ_overrides={"PATH_INFO": wrong})
        check("%s is still no page" % wrong, r.status_code == 404
              and "Location" not in r.headers, (r.status_code, r.headers.get("Location")))

    robots = c.get("/robots.txt").data.decode().splitlines()
    check("robots are kept off every page of the example account",
          all("Disallow: " + p in robots for p in (
              "/analyse", "/research", "/ideas", "/screener", "/strategies", "/settings",
              "/trades", "/portfolio", "/monitor")), robots)
    check("with no plans for sale the sitemap lists no pricing page", "/pricing<" not in sitemap)
    _os.environ.update(STRIPE)
    try:
        check("and lists it once there are", "/pricing</loc>" in c.get("/sitemap.xml").data.decode())
    finally:
        for key in STRIPE:
            _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("STRATEGY TESTS")
    print("=" * 72)
    r = c.get("/backtest/rsi-reversion")
    page = r.data.decode("utf-8", "replace")
    check("a strategy page exists before any test",
          r.status_code == 200 and "Being tested" in page and "noindex" in page,
          r.status_code)
    check("an unknown strategy has no page", c.get("/backtest/golden-cross").status_code == 404)
    report = SimpleNamespace(overall={
        "rsi_reversion": SimpleNamespace(name="rsi_reversion", n=40, wins=18, sum_r=4.0),
        "trend_stack": SimpleNamespace(name="trend_stack", n=10, wins=9, sum_r=9.0)})
    check("a test keeps times, worked and R kept",
          public.tests(report)["rsi_reversion"] == [40, 18, 4.0])
    conn = db.connect(DB)
    for sym, kept in (("NVDA", 4.0), ("AMD", -2.0)):
        s = snap_for(sym)
        report.overall["rsi_reversion"].sum_r = kept
        s["tests"] = public.tests(report)
        public.save_verdict(conn, s)
    for name in ("macd_momentum", "pin_bar", "cci_reversion", "aroon_trend",
                 "obv_divergence"):
        s = public.verdict(conn, "NVDA")
        s["tests"][name] = [50, 10, -1.5]
        public.save_verdict(conn, s)
    conn.close()
    r = c.get("/backtest/rsi-reversion")
    page = r.data.decode("utf-8", "replace")
    check("then shows each stock it was tested on",
          r.status_code == 200 and "Does trading against an extreme RSI work?" in page
          and "/stock/nvda" in page and "/stock/amd" in page, r.status_code)
    check("and how many it kept money on", "kept money on 1 of the" in page)
    check("open to search engines once tested", "noindex" not in page)
    page = c.get("/backtests").data.decode("utf-8", "replace")
    check("the list ranks tested strategies, leaving out too few trades",
          "RSI reversion" in page and "Trend stack" not in page)
    from bot import strategies as _strats
    _all, _daily = len(_strats.voting_names()), len(_strats.daily_voting_names())
    _inside = len(_strats.INTRADAY_ONLY)
    _flat = " ".join(page.split())
    check("every count of the strategies comes from the registry",
          _daily == _all - _inside
          and ("%d strategies decide Orenth's calls." % _all) in _flat
          and ("running %d strategies" % _all) in page, (_all, _daily))
    # Six strategies are in the table; the page accounts for every other one.
    _rest = re.search(r"(\d+) of them (?:has|have) fewer than 40 separate trades", _flat)
    _in_day = re.search(r"(\d+) of them reads? the inside of a trading day", _flat)
    _shown = re.search(r"<p>(\d+) strategies, back-tested every day", _flat)
    check("and the ones not listed are counted, so the numbers add up",
          _rest and _in_day and _shown and int(_shown.group(1)) == 6
          and int(_shown.group(1)) + int(_rest.group(1)) + int(_in_day.group(1)) == _all,
          [m and m.group(0) for m in (_shown, _rest, _in_day)])
    check("no page speaks of a trial while nothing is on trial", not _strats.TRIAL
          and not [p for p in ("/backtests", "/strategies", "/backtest/liquidity-model",
                               "/backtest/session-sweep", "/backtest/rsi-reversion")
                   if "on trial" in c.get(p).data.decode("utf-8", "replace")])
    # A strategy tested everywhere with too few trades was "being tested" for good.
    page = " ".join(c.get("/backtest/trend-stack").data.decode("utf-8", "replace").split())
    check("a strategy tested but too rare to rank says so, with its figures",
          "Too rare to rank" in page and "Being tested" not in page
          and "2 stocks and coins tested so far, it took a pretend trade on 20 days in all, "
              "fewer than 40 separate trades on any one of them" in page)
    page = " ".join(c.get("/backtest/structure-flip").data.decode("utf-8", "replace").split())
    check("and one that never fired says that", "it has not come up" in page
          and "Being tested" not in page)
    check("its heading reads as English", "Does a break of structure work?" in page)
    # Each of the 44 has words of its own: an article or a doing word first, and
    # never the bare label ("Does Pivot levels work?").
    _said = {s.name: public.strategy_asked(s.name) for s in _strats.REGISTRY}
    check("so does every other strategy's",
          not [n for n, said in _said.items()
               if said.split()[0] not in ("a", "an", "the", "following", "trading")]
          and len(set(_said.values())) == len(_said),
          [n for n, said in _said.items() if said == public.strategy_label(n)])
    check("each short enough for a preview to show whole",
          all(len("Does %s work? · Orenth" % said) < 60 for said in _said.values()),
          [said for said in _said.values() if len("Does %s work? · Orenth" % said) >= 60])
    # Higher highs and lower lows at once is a widening range, not what it follows.
    check("and true: it follows higher highs up or lower lows down, not both at once",
          _said["market_structure"] == "following higher highs or lower lows")
    page = _html.unescape(c.get("/backtest/pivot-levels").data.decode("utf-8", "replace"))
    check("in its title, its heading, its preview and its links to the others",
          "<title>Does trading from pivot points work?" in page
          and "<h1>Does trading from pivot points work?</h1>" in page
          and 'content="Does trading from pivot points work? · Orenth"' in page
          and "Does trading an opening gap work?" in page and "Does Gap" not in page)
    check("on a phone each row of the tests and of Strategies is a card of named figures",
          all('<table class="stack">' in c.get(p).data.decode("utf-8", "replace")
              for p in ("/backtests", "/backtest/rsi-reversion"))
          and 'data-label="Kept a trade"' in c.get("/backtests").data.decode("utf-8", "replace"))
    # With one on trial (votes=False still works), the pages say so again.
    _real = _strats.TRIAL
    _strats.TRIAL = ("macd_momentum",)
    try:
        page = " ".join(c.get("/backtests").data.decode("utf-8", "replace").split())
        check("with one on trial, the list says so and marks it",
              "1 more strategy is on trial" in page
              and ">on trial</span>" in page[page.index("/backtest/macd-momentum"):]
              [:260])
        page = " ".join(c.get("/backtest/macd-momentum").data.decode("utf-8", "replace").split())
        check("and so does its own page", "Measured here, not used in Orenth's calls" in page)
        check("and Strategies", "1 more is on trial" in " ".join(
            c.get("/strategies").data.decode("utf-8", "replace").split()))
    finally:
        _strats.TRIAL = _real
    # The four that did worst were left off a page that says "every strategy".
    conn = db.connect(DB)
    for k, s in enumerate(_strats.REGISTRY):
        conn.execute("INSERT INTO strategy_stats (symbol, interval, strategy, family, regime, "
                     "n, wins, sum_r, sum_r_gross, sum_bars, updated_ts) VALUES "
                     "('ALLFORTY', '1d', ?, ?, 'all', 200, 80, ?, ?, 600, 0)",
                     (s.name, s.family, 5.0 - k, 5.0 - k))
    conn.commit()
    page = c.get("/strategies?symbol=ALLFORTY").data.decode("utf-8", "replace")
    check("with a name on every column", not re.findall(r"<th[^>]*>\s*</th>", page))
    check("Strategies lists every strategy with enough setups",
          page.count('<td class="lead">') == len(_strats.REGISTRY),
          page.count('<td class="lead">'))
    conn.execute("DELETE FROM strategy_stats WHERE symbol='ALLFORTY'")
    conn.commit()
    conn.close()
    page = c.get("/stock/nvda").data.decode("utf-8", "replace")
    # Its colours say what the tests can: a gain or a loss beyond the margin.
    # Without a margin, stored before one was measured, the sign is all there is.
    conn = db.connect(DB)
    s = public.verdict(conn, "NVDA")
    s["tests"] = {"rsi_reversion": [50, 30, 25.0], "macd_momentum": [50, 20, 2.0],
                  "pin_bar": [50, 20, -2.0], "cci_reversion": [50, 10, -25.0],
                  "aroon_trend": [50, 20, 0.1], "obv_divergence": [50, 20, -5.0]}
    s["spread"] = {name: [100.0, 50.0] for name in s["tests"]
                   if name not in ("aroon_trend", "obv_divergence")}
    kept_before = public.verdict(conn, "NVDA")
    public.save_verdict(conn, s)
    coloured = c.get("/stock/nvda").data.decode("utf-8", "replace")
    public.save_verdict(conn, kept_before)
    conn.close()
    shown = dict(re.findall(r'/backtest/([a-z-]+)">[^<]*</a>:.*?<span class="(fig[^"]*)">',
                            coloured, re.S))
    check("a result inside its margin, or of nothing, is neither green nor red",
          shown == {"rsi-reversion": "fig gain", "macd-momentum": "fig", "pin-bar": "fig",
                    "cci-reversion": "fig loss", "aroon-trend": "fig",
                    "obv-divergence": "fig loss"}, shown)
    check("a stock page says which strategies worked there",
          "Which strategies worked on NVDA" in page and "/backtest/rsi-reversion" in page)
    check("and answers its question for AI search", '"FAQPage"' in page
          and "Is NVIDIA (NVDA) a buy right now?" in page)
    check("its other checks start with look-alikes",
          page.index("/stock/amd") < page.index("/stock/aapl"))
    sitemap = c.get("/sitemap.xml").data.decode()
    check("the sitemap lists the strategy pages",
          "/backtests</loc>" in sitemap and "/backtest/rsi-reversion</loc>" in sitemap
          and "/backtest/opening-range-break<" not in sitemap)
    check("a result that rounds to zero has no sign",
          web._signed(-0.004) == "0.00" and web._signed(0.3) == "+0.30")

    page = c.get("/stocks").data.decode()
    check("a copy with no public address counts no visits",
          "tracker.metricool.com" not in page)
    _os.environ["STOCKBOT_PUBLIC_URL"] = "https://www.Orenth.example/"
    try:
        page = c.get("/stocks").data.decode()
    finally:
        del _os.environ["STOCKBOT_PUBLIC_URL"]
    check("visitors are counted, by page path only",
          "tracker.metricool.com" in page and "location.pathname" in page)
    check("and only where the browser is at the public address",
          'if (location.hostname.replace(/^www\\./, "") === "orenth.example")' in page)

    print()
    print("=" * 72)
    print("THE TRACK RECORD")
    print("=" * 72)
    T = calendar.timegm((2026, 1, 5, 15, 0, 0, 0, 0, 0))          # a Monday, mid-session
    DAY = 86400

    def call(symbol, action, price, atr):
        s = snap_for(symbol, action)
        s.update(price=price, stop=price - 1.5 * atr, target=price + 2.25 * atr,
                 horizon=10, at=T)
        return s

    def path(closes, swing=1.0):
        """Daily bars after the call, one per day, each a little either side of its close."""
        return SimpleNamespace(timestamp=[T + DAY * (k + 1) for k in range(len(closes))],
                               high=[c + swing for c in closes], low=[c - swing for c in closes],
                               close=list(closes))

    page = c.get("/record").data.decode("utf-8", "replace")
    check("the record says when the method changed, and from what to what",
          "Calls up to 19 November 2026 came from 34 strategies. Early on 20 November "
          "(UTC)" in " ".join(page.split())
          and "so calls since come from 44" in " ".join(page.split()))
    check("before any call, the record page says it is starting",
          "first calls are written down" in page and "noindex" in page)
    conn = db.connect(DB)
    check("a snapshot with no range has no stop or target, so it is not written down",
          not public.record_call(conn, snap_for("GME")))
    check("a call is written down", public.record_call(conn, call("NVDA", "BUY", 100.0, 2.0)))
    check("but only once a day", not public.record_call(conn, call("NVDA", "BUY", 101.0, 2.0)))
    public.record_call(conn, call("AMD", "AVOID", 50.0, 1.0))
    public.record_call(conn, call("TSLA", "WAIT", 200.0, 4.0))
    public.record_call(conn, call("AAPL", "BUY", 80.0, 1.0))
    public.record_call(conn, call("MSFT", "BUY", 300.0, 3.0))
    page = c.get("/record").data.decode("utf-8", "replace")
    check("while waiting, it says how many calls it is holding",
          "5 calls written down since 5 Jan 2026" in page)

    # NVDA, BUY at 100 with target 104.5: reaches it on the third day.
    check("a buy that reached its target was right",
          public.settle_calls(conn, "NVDA", path([101, 102, 105, 103])) == 1
          and conn.execute("SELECT outcome, how FROM calls WHERE symbol='NVDA'").fetchone()[:]
          == ("right", "target"))
    # AMD, AVOID at 50 with target 52.25: the price went there anyway.
    public.settle_calls(conn, "AMD", path([50.5, 51, 53, 53]))
    check("an avoid when the price rose to the target was wrong",
          conn.execute("SELECT outcome FROM calls WHERE symbol='AMD'").fetchone()[0] == "wrong")
    # TSLA, WAIT at 200 with stop 194: the price fell through it.
    public.settle_calls(conn, "TSLA", path([198, 195, 192, 190], swing=2.0))
    check("a wait when the price fell to the stop was right",
          conn.execute("SELECT outcome, how FROM calls WHERE symbol='TSLA'").fetchone()[:]
          == ("right", "stop"))
    # AAPL, BUY at 80: ten quiet days, ending at 80.2, inside half a day's move.
    public.settle_calls(conn, "AAPL", path([80.1] * 9 + [80.2], swing=0.3))
    check("a price that went nowhere is neither right nor wrong",
          conn.execute("SELECT outcome, how FROM calls WHERE symbol='AAPL'").fetchone()[:]
          == ("flat", "flat"))
    # MSFT: only three quiet days so far.
    check("a call whose ten days are not up stays open",
          public.settle_calls(conn, "MSFT", path([301, 302, 301], swing=0.5)) == 0
          and conn.execute("SELECT outcome FROM calls WHERE symbol='MSFT'").fetchone()[0] is None)
    check("the stop is checked before the target inside one day",
          public._walk(SimpleNamespace(high=[110.0], low=[90.0], close=[100.0]),
                       0, 100.0, 97.0, 104.5, 10)[0] == "stop")

    rec = public.record(conn)
    check("the record adds up", (rec["all"]["right"], rec["all"]["wrong"], rec["all"]["flat"],
                                 rec["all"]["open"]) == (2, 1, 1, 1), rec["all"])
    kinds = {c["action"]: c for c in rec["classes"]}
    check("and scores each kind of call on its own",
          (kinds["BUY"]["right"], kinds["BUY"]["flat"], kinds["AVOID"]["wrong"],
           kinds["WAIT"]["right"]) == (1, 1, 1, 1), kinds.keys())
    # Buys after these calls: NVDA target (worked), AMD target (worked),
    # TSLA stop (lost), AAPL flat. Two of three decided buys worked.
    check("against chance: what calling it every time would have scored",
          abs(kinds["BUY"]["chance"] - 2 / 3) < 1e-9
          and abs(kinds["AVOID"]["chance"] - 1 / 3) < 1e-9, kinds["BUY"]["chance"])
    check("with a 95% range on the right rate",
          kinds["BUY"]["lo"] is not None and kinds["BUY"]["lo"] < 1.0 <= kinds["BUY"]["hi"] + 1e-9)
    check("and what a buy after the calls did, after costs",
          kinds["BUY"]["mean_move"] is not None and kinds["BUY"]["mean_move"] > 0)
    mine = public.symbol_record(conn, "NVDA")
    check("one stock's own record is there too",
          len(mine["latest"]) == 1 and mine["latest"][0]["how"] == "target")
    conn.close()

    page = c.get("/record").data.decode("utf-8", "replace")
    check("the record page has no pooled headline score",
          "4 calls scored" in page and "% right" not in page.split("When it said")[0])
    check("each kind is set against chance",
          "When it said BUY" in page and "Chance level" in page
          and "Not yet distinguishable from chance" in page)
    check("lists every settled call with what the price did",
          "rose to the target" in page and "fell to the stop" in page
          and "went nowhere" in page and "/stock/amd" in page)
    check("and is open to search engines once there is a score", "noindex" not in page)
    page = c.get("/stock/nvda").data.decode("utf-8", "replace")
    check("a stock page shows its own past calls",
          "Past calls on NVDA" in page and "rose to the target" in page)
    sitemap = c.get("/sitemap.xml").data.decode()
    check("the sitemap lists the record", "/record</loc>" in sitemap)

    conn = db.connect(DB)
    fri = call("VOO", "AVOID", 500.0, 5.0)
    fri.update(price_day=20000)
    check("a quote is written down once", public.record_call(conn, fri))
    sat = dict(fri, at=fri["at"] + DAY)
    check("the same Friday quote seen on Saturday is not a new call",
          not public.record_call(conn, sat))
    mon = dict(fri, at=fri["at"] + 3 * DAY, price=505.0, price_day=20003)
    check("Monday's new quote is", public.record_call(conn, mon))
    # Repeats written by the old rule, before price_day existed: the first
    # check of each UTC day, before the open, so Saturday, Sunday and Monday
    # all carried Friday's close, stored once as the bar's float and once as
    # the two-decimal quote. Daily bars only on weekdays, stamped at the open.
    fri9 = calendar.timegm((2026, 1, 9, 3, 0, 0, 0, 0, 0))
    for k, price in enumerate((399.5, 400.0899963378906, 400.09, 400.09)):
        conn.execute("INSERT INTO calls (symbol, day, action, price, stop, target, "
                     "horizon, at) VALUES ('QQQ', ?, 'AVOID', ?, 394.0, 409.0, 10, ?)",
                     ("2026-01-%02d" % (9 + k), price, fri9 + k * DAY))
    conn.commit()
    opens = [calendar.timegm((2026, 1, d, 14, 30, 0, 0, 0, 0)) for d in (8, 9, 12, 13)]
    weekdays = SimpleNamespace(timestamp=opens, high=[401.0] * 4, low=[399.0] * 4,
                               close=[400.0] * 4, gmt_offset=-18000)
    check("old repeats are marked by the bars they are scored on, not by price",
          public.mark_calls(conn, "QQQ", weekdays) == 2)
    check("and marking twice changes nothing", public.mark_calls(conn, "QQQ", weekdays) == 0)
    kept = conn.execute("SELECT day, price_day FROM calls WHERE symbol='QQQ' "
                        "AND excluded IS NULL ORDER BY at").fetchall()
    check("the earliest call of a repeat run is the one kept",
          [r[0] for r in kept] == ["2026-01-09", "2026-01-10"], [r[0] for r in kept])
    check("and each kept call is dated by the session its price came from",
          [time.strftime("%a", time.gmtime(r[1] * DAY)) for r in kept] == ["Thu", "Fri"],
          [r[1] for r in kept])
    check("nothing was deleted",
          conn.execute("SELECT COUNT(*) FROM calls WHERE symbol='QQQ'").fetchone()[0] == 4)
    rec = public.record(conn)
    check("left-out calls are counted apart, with their reason",
          len(rec["excluded"]) == 2
          and "no market session between them" in rec["excluded"][0]["excluded"])
    conn.close()
    page = c.get("/record").data.decode("utf-8", "replace")
    check("and listed on the page", "left out of the scores" in page
          and "Scored on the same bars as the call written down on 10 Jan 2026" in page)
    check("the scoring rule on the page matches the code: half the stop distance",
          "half the distance to the stop" in page and "half a day" not in page)

    # A symbol taken off the popular list: its calls still waiting are scored,
    # and its page, gone, is not linked to.
    conn = db.connect(DB)
    gone_at = int(time.time()) - 30 * 86400
    for sym, when in (("SQZZ", gone_at), ("OLDZZ", gone_at - 60 * 86400)):
        conn.execute("INSERT INTO calls (symbol, day, action, price, stop, target, horizon, at) "
                     "VALUES (?, '2026-01-05', 'BUY', 100, 97, 104.5, 10, ?)", (sym, when))
    conn.commit()
    asked = []

    def gone_bars(symbol):
        asked.append(symbol)
        return SimpleNamespace(timestamp=[gone_at + 86400 * k for k in range(1, 4)],
                               high=[106.0] * 3, low=[99.0] * 3, close=[105.0] * 3)

    real_due = public.due
    public.due = lambda conn, now=None: ["KO"]
    try:
        public.refresh(conn, lambda s: snap_for(s), sleep=slept.append, bars=gone_bars)
        public.refresh(conn, lambda s: snap_for(s), sleep=slept.append, bars=gone_bars)
    finally:
        public.due = real_due
    check("a call on a symbol no longer popular is still scored, and only once",
          asked == ["KO", "SQZZ", "KO"] and conn.execute(
              "SELECT outcome FROM calls WHERE symbol='SQZZ'").fetchone()[0] == "right", asked)
    conn.close()
    page = c.get("/record").data.decode("utf-8", "replace")
    check("the record lists it, without a link to a page that is gone",
          "<b>SQZZ</b>" in page and "/stock/sqzz" not in page and "/stock/amd" in page)

    conn = db.connect(DB)
    fetched = []

    def rising(symbol):
        fetched.append(symbol)
        return path([310, 320, 330, 340])

    def analyse_with_levels(symbol):
        # The next day's session, closing at the price of the day before on
        # MSFT: a new session, so a new call, however alike the two prices.
        s = call(symbol, "BUY", 300.0, 3.0)
        s["at"] = T + DAY
        return s

    public.refresh(conn, analyse_with_levels, now=time.time() + public.MAX_AGE + 1,
                   sleep=slept.append, bars=rising)
    check("the daily check writes each call down and scores the earlier ones",
          fetched and conn.execute("SELECT COUNT(*) FROM calls WHERE day='2026-01-06'").fetchone()[0]
          == len(public.POPULAR)
          and conn.execute("SELECT outcome FROM calls WHERE symbol='MSFT' AND day='2026-01-05'")
          .fetchone()[0] == "right")
    conn.close()

    print()
    print("=" * 72)
    print("WRONG ADDRESSES, AND THE HEADERS EVERY PAGE CARRIES")
    print("=" * 72)
    r = c.get("/no-such-page")
    check("a visitor at a wrong address gets a page saying so, not the sign-in door",
          r.status_code == 404 and "There is no page here" in r.data.decode(), r.status_code)
    check("and a link back to the front page", 'href="/"' in r.data.decode())
    r = c.get("/api/no-such-call")
    check("a wrong API address says so in JSON",
          r.status_code == 404 and r.get_json() == {"error": "No such address."})
    # An unexpected failure is JSON too, for whatever reads the API, and so is
    # an answer whose readings could not all be measured.
    looker = user("looker@example.com")
    sign_in(looker)
    was_analysis = web.cached_analysis
    was_propagate = web.app.config.get("PROPAGATE_EXCEPTIONS")

    def broken_analysis(*a, **k):
        raise RuntimeError("secret")

    web.cached_analysis = broken_analysis
    web.app.config["PROPAGATE_EXCEPTIONS"] = False
    web.app.logger.disabled = True
    _os.environ.update(STRIPE)
    try:
        r = c.get("/api/analyse?symbol=NVDA")
        check("an API call that breaks says so in JSON", r.status_code == 500
              and "broke on our side" in (r.get_json(silent=True) or {}).get("error", "")
              and b"secret" not in r.data, (r.status_code, r.mimetype))
        # Signed in a while ago: a link on another site costs no look.
        conn = db.connect(DB)
        conn.execute("UPDATE sessions SET created_ts=created_ts-3600 WHERE user_id=?",
                     (looker.id,))
        conn.commit()
        conn.close()
        r = c.get("/api/analyse?symbol=NVDA", headers={"Sec-Fetch-Site": "cross-site"})
        check("one asked for from another site is told that, not that its looks are used up",
              r.status_code == 402 and "another site" in r.get_json()["error"]
              and "used up" not in r.get_json()["error"], r.data[:200])
    finally:
        web.cached_analysis = was_analysis
        web.app.config["PROPAGATE_EXCEPTIONS"] = was_propagate
        web.app.logger.disabled = False
        for key in STRIPE:
            _os.environ.pop(key, None)
        sign_out()
    import json as json_mod
    from bot import report as report_mod

    def refuse(word):
        raise ValueError(word)

    class Anything(SimpleNamespace):
        def __getattr__(self, name):
            return float("nan") if name in ("adx", "reward_risk") else None

    blank = Anything(samples=0, curve=[(1, float("inf"))], items=[], label=lambda: "neutral")
    strict = json_mod.loads(report_mod.to_json(blank, blank, [], float("nan"), blank, blank,
                                               blank), parse_constant=refuse)
    check("a reading that could not be measured is null, never the bare word NaN",
          strict["regime"]["adx"] is None and strict["call"]["reward_risk"] is None
          and strict["odds"]["curve"] == [[1, None]], strict["regime"])
    r = c.get("/account")
    loc = r.headers.get("Location", "")
    check("a real page still asks them to sign in, with a clean return address",
          r.status_code == 302 and loc.endswith(("next=/account", "next=%2Faccount")), loc)
    lost = user("lost@example.com")
    sign_in(lost)
    check("signed in, a wrong address is the same page",
          c.get("/no-such-page").status_code == 404)
    sign_out()
    h = c.get("/stocks").headers
    check("every page says do not sniff, do not frame, and keep the referrer short",
          h.get("X-Content-Type-Options") == "nosniff"
          and h.get("X-Frame-Options") == "DENY"
          and h.get("Content-Security-Policy").split("; ")
          == ["frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'"]
          and h.get("Referrer-Policy") == "strict-origin-when-cross-origin", dict(h))
    check("but not to use https only, when it is not served that way",
          "Strict-Transport-Security" not in h)
    web.app.config["SESSION_COOKIE_SECURE"] = True
    try:
        check("on https it does, for a year",
              c.get("/stocks").headers.get("Strict-Transport-Security") == "max-age=31536000")
    finally:
        web.app.config["SESSION_COOKIE_SECURE"] = False
    check("a popular page is checked a few times a day", public.MAX_AGE == 6 * 3600)

    print()
    print("=" * 72)
    print("THE FRONT DOOR SAYS ONLY WHAT IS TRUE")
    print("=" * 72)
    sign_out()
    # A recent example, so the page shows one without starting a real analysis.
    _was = dict(web._example_state)
    with web._example_lock:
        web._example_state.update(at=time.time(), data={
            "symbol": "NVDA", "samples": 80, "worked": 30, "action": "WAIT",
            "headline": "Wait for a better price.", "sure": "", "price": 166.19,
            "at": time.time() - 20 * 60})
    try:
        page = c.get("/").data.decode("utf-8", "replace")
    finally:
        with web._example_lock:
            web._example_state.update(_was)
    key = ("NVDA", False, "1d", True, web._settings_key(dict(web.config_mod.DEFAULTS)))
    kept_before, asked_of, scans = dict(web._CACHE), [], []
    real_analyse, real_find = web.engine.analyse, web.ideas_mod.find

    def worked_out(symbol, cfg, **how):
        asked_of.append((symbol, cfg.get("use_llm_sentiment", True), how))
        return fake_result("NVDA", "WAIT", 60, 33)

    web.engine.analyse = worked_out
    web.ideas_mod.find = lambda *a, **k: scans.append((a, k))
    try:
        web._CACHE.clear()
        web._CACHE[key] = (time.time() - 60, fake_result("NVDA", "BUY", 70, 41), {})
        page_now = c.get("/").data.decode("utf-8", "replace")
        web._example_state.update(at=0.0, data=None, busy=False, failed=0.0)
        web._refresh_example()
        check("the front page's check is the one the page behind it was given",
              asked_of == [] and web._example_state["data"]["action"] == "BUY"
              and web._example_state["data"]["samples"] == 70, web._example_state)
        web._CACHE[key] = (time.time(), fake_result("NVDA", "AVOID", 50, 20), {})
        check("and follows it when a visitor has it worked out again",
              web._example()["action"] == "AVOID" and web._example()["samples"] == 50)
        web._CACHE.clear()
        web._refresh_example()
        check("with none kept it is worked out the same way, news and all, and kept for that page",
              len(asked_of) == 1 and asked_of[0][2].get("with_news", True)
              and not asked_of[0][2]["record"] and key in web._CACHE
              and web._CACHE[key][1].plan.prob_samples == 60, (asked_of, list(web._CACHE)))
        check("and the list behind the Find tile is started, without waiting for it",
              scans and scans[-1][1]["wait"] == 0.0
              and scans[-1][1]["ttl"] == web.ideas_mod.JOB_KEEP, scans)
        _os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
        web._CACHE.clear()
        web._refresh_example()
        paid = ("NVDA", False, "1d", True, key[4])
        check("no model is paid to read headlines for it, and a member's own is kept apart",
              asked_of[-1][1] is False and paid not in web._CACHE and len(web._CACHE) == 1,
              (asked_of[-1], list(web._CACHE)))
    finally:
        _os.environ.pop("ANTHROPIC_API_KEY", None)
        web.engine.analyse, web.ideas_mod.find = real_analyse, real_find
        web._CACHE.clear()
        web._CACHE.update(kept_before)
        with web._example_lock:
            web._example_state.update(_was)
    waits = []
    web.ideas_mod.find = lambda *a, **k: waits.append(k["wait"]) or {}
    try:
        web._demo_shortlist("stocks", "medium", "balanced")
    finally:
        web.ideas_mod.find = real_find
    check("a visitor who starts a scan is shown how far it has got, not kept waiting",
          waits == [web.DEMO_SCAN_WAIT] and web.DEMO_SCAN_WAIT <= 2, waits)
    check("an example from twenty minutes ago is not called live",
          "Live right now" not in page and "Latest check" in page and "20 min ago" in page)
    check("someone with an account is offered a way back in",
          ">Sign in</a>" in page and ">Sign up</a>" not in page)
    check("the screener is not said to read filings",
          "numbers in their filings" not in page
          and "price, size and reported fundamentals" in page)
    check("nor is every company figure said to cite one",
          "Every company figure cites" not in page and "on the research page cite" in page)
    check("the Monitor page has one name", "<b>Monitor</b>" in page and "<b>Alerts</b>" not in page)
    check("and nothing promises to tell anyone anything", "get told" not in page)
    check("how it is tested, and how its calls turned out, are one tap away",
          'href="/backtests#how-we-tested">how the strategies are tested' in page
          and 'href="/record">how past calls turned out' in page)
    check("every page has a skip link and one main landmark",
          page.count('<main id="main">') == 1 and 'href="#main">Skip to content' in page)
    page = c.get("/signin").data.decode("utf-8", "replace")
    check("the sign-in page says it signs in too",
          "<title>Sign in or create your free account" in page
          and "<h1>Sign in or create your free account</h1>" in page)
    # Seven pages had one search description and one preview title ("Orenth").
    _os.environ.update(STRIPE)
    real_offers, billing.offers = billing.offers, lambda: {}
    try:
        heads = {}
        for path in ("/", "/pricing", "/terms", "/privacy", "/signin", "/learn/practice",
                     "/tools/size", "/tools/survival"):
            page = c.get(path).data.decode("utf-8", "replace")
            heads[path] = [_html.unescape(re.search(p, page).group(1)) for p in (
                r"<title>(.*?)</title>", r'name="description" content="(.*?)"',
                r'property="og:title" content="(.*?)"',
                r'name="twitter:title" content="(.*?)"')]
    finally:
        billing.offers = real_offers
        for key in STRIPE:
            _os.environ.pop(key, None)
    check("each page a search finds has a title and a description of its own",
          len({h[0] for h in heads.values()}) == len({h[1] for h in heads.values()})
          == len(heads), heads)
    check("and its preview carries that title", all(h[0] == h[2] == h[3] and h[2] != "Orenth"
                                                    for h in heads.values()), heads)
    check("short enough to be shown whole", all(
        len(h[0]) < 60 and len(h[1]) < 155 for p, h in heads.items() if p != "/"),
        {p: (len(h[0]), len(h[1])) for p, h in heads.items()})
    page = c.get("/stocks").data.decode("utf-8", "replace")
    check("asking someone to analyse a stock says analyse, like the button",
          "Analyse any stock or coin" in page and "Check any stock" not in page)

    print()
    print("=" * 72)
    print("SHARING A CALL")
    print("=" * 72)
    sharer = user("sharer@example.com")
    real_analysis = web.cached_analysis
    web.cached_analysis = lambda symbol, *a, **k: fake_result(symbol.upper())
    try:
        r = c.post("/share", data={"symbol": "TSLA", "interval": "1d"})
        check("a visitor cannot make a share",
              r.status_code in (302, 303) and "/signin" in r.headers["Location"])
        sign_in(sharer)
        _os.environ["STOCKBOT_PUBLIC_URL"] = "https://orenth.example"
        try:
            check("signed-in pages are never counted",
                  "tracker.metricool.com" not in c.get("/stocks").data.decode())
        finally:
            del _os.environ["STOCKBOT_PUBLIC_URL"]
        r = c.post("/share", data={"symbol": "TSLA", "interval": "1d"})
        where = r.headers.get("Location", "")
        check("sharing makes a page and goes to it",
              r.status_code == 303 and "/s/" in where, (r.status_code, where))
        sid = where.rsplit("/", 1)[-1]
        again = c.post("/share", data={"symbol": "TSLA", "interval": "1d"})
        check("sharing the same call twice gives the same link",
              again.headers.get("Location", "").endswith("/s/" + sid))
        sign_out()
        page = c.get("/s/" + sid).data.decode("utf-8", "replace")
        check("anyone can open it", "Avoid" in page and "Shared from Orenth" in page)
        check("it never says who shared it", "sharer" not in page)
        check("search engines are kept off copies", "noindex" in page)
        check("it points to today's check for a popular symbol", "/stock/tsla" in page)
        r = c.get("/s/%s.png" % sid)
        check("and has its own card", r.status_code == 200 and r.data[:4] == b"\x89PNG")
        check("an unknown share is not found", c.get("/s/nope").status_code == 404)

        # Priced in rupees: a bare 912.4 reads as dollars.
        rupees = fake_result("TATAMOTORS.NS", "BUY")
        rupees.bars.currency = "INR"
        rupees.plan.entry = 912.4
        told = public.snapshot(rupees, plain_words.explain_plan(rupees.plan, rupees.bars))
        conn = db.connect(DB)
        there = public.create_share(conn, sharer.id, told)
        conn.close()
        page = " ".join(c.get("/s/" + there).data.decode("utf-8", "replace").split())
        check("a share of a stock not priced in dollars names its currency",
              "Price INR 228.38 ·" in page and "buy price is INR 912.40." in page,
              told["points"])

        from PIL import Image
        import io as io_mod
        for sym, name in (("TATAMOTORS.NS", "Tata Motors Limited"),
                          ("AAPL261218C00200000", "AAPL Dec 2026 200.000 call"),
                          ("DRIP", "Direxion Daily S&P Oil & Gas Exp. & Prod. Bear 2X Shares "
                                   "And More Words")):
            card = Image.open(io_mod.BytesIO(public.render_card(
                dict(told, symbol=sym, name=name, action="AVOID"))))
            edge = card.crop((public.WIDTH - public.MARGIN + 1, 130, public.WIDTH, 330))
            check("the card of %s keeps its call and its price inside the margin" % sym,
                  edge.getcolors() == [(edge.width * edge.height, public.PAPER)])

        class Emptied(type(public._CARDS)):
            """A cache whose entry goes while it is being looked up, unless held."""
            def __contains__(self, key):
                held = super().__contains__(key)
                if not public._CARDS_LOCK.locked():
                    self.pop(key, None)
                return held

            def move_to_end(self, key, last=True):
                assert public._CARDS_LOCK.locked()
                super().move_to_end(key, last)

        was_cards, public._CARDS = public._CARDS, Emptied()
        try:
            first = public.render_card(told)
            check("a card is drawn once and kept, whoever else is asking",
                  public.render_card(told) is first and len(public._CARDS) == 1)
        finally:
            public._CARDS = was_cards

        sign_in(sharer)
        old_limit = public.SHARES_PER_DAY
        public.SHARES_PER_DAY = 2
        c.post("/share", data={"symbol": "AAPL", "interval": "1d"})
        r = c.post("/share", data={"symbol": "MSFT", "interval": "1d"})
        check("there is a daily limit on new shares", r.status_code == 429, r.status_code)
        public.SHARES_PER_DAY = old_limit
        check("only real intervals are accepted",
              c.post("/share", data={"symbol": "AAPL", "interval": "7y"}).status_code == 400)
    finally:
        web.cached_analysis = real_analysis
    conn = db.connect(DB)
    A.delete_user(conn, sharer.id)
    conn.close()
    check("deleting the account deletes its shares",
          c.get("/s/" + sid).status_code == 404)

    print()
    print("=" * 72)
    print("INVITE A FRIEND")
    print("=" * 72)
    _os.environ.update(STRIPE)
    host = user("host@example.com")
    conn = db.connect(DB)
    code = invites.code_for(conn, host.id)
    check("each account gets one code", code == invites.code_for(conn, host.id)
          and len(code) == 8)
    check("which leads back to it", invites.inviter(conn, code) == host.id)
    check("and nothing else does", invites.inviter(conn, "zzzz") is None
          and invites.inviter(conn, "' or 1=1") is None)
    conn.close()

    sign_out()
    r = c.get("/join/" + code)
    with c.session_transaction() as sess:
        remembered = sess.get("ref")
    check("an invite link remembers the code and opens the front page",
          remembered == code and r.headers["Location"].endswith("/"))
    page = c.get("/").data.decode("utf-8", "replace")
    check("which says a friend invited them", "A friend invited you" in page)
    check("and what they get, without speaking for the friend, who may have had theirs",
          "you both get" not in page
          and "you both get" not in c.get("/signin").data.decode("utf-8", "replace"))
    c.get("/join/notacode")
    with c.session_transaction() as sess:
        check("a made-up code is not remembered", sess.get("ref") == code)

    friend = user("friend@example.com")
    with web.app.test_request_context("/"):
        session["ref"] = code
        web._finish_sign_in(friend, "")
        check("signing up uses the code up", "ref" not in session)
    week = invites.BONUS_DAYS * 86400
    f, h = fresh(friend.id), fresh(host.id)
    check("the friend gets a free week", f.bonus_until > time.time() + week - 60)
    check("and so does the person who invited them",
          h.bonus_until > time.time() + week - 60)
    check("a free week is Pro", billing.is_pro(f) and billing.is_pro(h))
    conn = db.connect(DB)
    check("it counts once", not invites.accept(conn, friend.id, code))
    check("nobody can invite themselves", not invites.accept(conn, host.id, code))
    old = A.upsert_email_user(conn, "old@example.com")
    conn.execute("UPDATE users SET created_ts=? WHERE id=?",
                 (int(time.time()) - invites.FRESH - 60, old.id))
    conn.commit()
    check("an existing account does not count as a new friend",
          not invites.accept(conn, old.id, code))
    second = A.upsert_email_user(conn, "second@example.com")
    before = fresh(host.id).bonus_until
    invites.accept(conn, second.id, code)
    check("weeks stack", fresh(host.id).bonus_until >= before + week - 5)
    invites.MAX_REWARDS, cap = 2, invites.MAX_REWARDS
    third = A.upsert_email_user(conn, "third@example.com")
    before = fresh(host.id).bonus_until
    invites.accept(conn, third.id, code)
    check("past the cap, the friend still gets a week but the host does not",
          fresh(third.id).bonus_until > time.time() and
          fresh(host.id).bonus_until == before)
    # One friend, joining, leaving and joining again, used to earn the host a
    # week every time, because the count was of friends still here.
    looper = user("looper@example.com")
    again_code = invites.code_for(conn, looper.id)
    invites.MAX_REWARDS = 3
    for _ in range(6):
        leaver = A.upsert_email_user(conn, "leaver@example.com")
        check("a friend who comes back can still use the link",
              invites.accept(conn, leaver.id, again_code))
        A.delete_user(conn, leaver.id)
    check("but the host's weeks stop at the cap, however often they do",
          abs(fresh(looper.id).bonus_until - time.time() - 3 * week) < 120
          and invites.joined(conn, looper.id) == 0,
          (fresh(looper.id).bonus_until - time.time()) / week)
    # An account whose friends joined before rewards were counted.
    veteran = user("veteran@example.com")
    for n in range(3):
        past = A.upsert_email_user(conn, "past%d@example.com" % n)
        conn.execute("UPDATE users SET invited_by=? WHERE id=?", (veteran.id, past.id))
    conn.commit()
    newest = A.upsert_email_user(conn, "newest@example.com")
    invites.accept(conn, newest.id, invites.code_for(conn, veteran.id))
    check("friends from before the count are counted too",
          fresh(veteran.id).bonus_until == 0 and fresh(newest.id).bonus_until > time.time())
    invites.MAX_REWARDS = cap
    conn.close()
    payer = user("payer@example.com")
    conn = db.connect(DB)
    billing.set_plan(conn, payer.id, "pro", "active", "cus_payer",
                     int(time.time()) + 20 * 86400)
    conn.close()
    sign_in(payer)
    page = c.get("/account").data.decode("utf-8", "replace")
    check("someone paying for Pro is not promised a week that adds nothing",
          "add nothing while you are paying for Pro" in page
          and "<b>both</b> get" not in page and "we both get" not in page)
    # They cancel later, and then a friend joins.
    conn = db.connect(DB)
    billing.set_plan(conn, payer.id, "free", "canceled", "cus_payer", 0)
    invites.accept(conn, A.upsert_email_user(conn, "payers-friend@example.com").id,
                   invites.code_for(conn, payer.id))
    conn.close()
    page = c.get("/account").data.decode("utf-8", "replace")
    check("someone who used to pay is told when their free days end",
          "Free from invites until" in page)
    sign_in(host)
    page = c.get("/account").data.decode("utf-8", "replace")
    check("everyone else still reads that both get it",
          "<b>both</b> get" in page and "we both get a free week" in page)
    check("and for how many friends", "for your first %d" % cap in page)
    invites.MAX_REWARDS = 2
    page = c.get("/account").data.decode("utf-8", "replace")
    invites.MAX_REWARDS = cap
    check("someone who has had them all is promised no more",
          "as many as" in page and "<b>both</b> get" not in page
          and "we both get" not in page)
    check("the account page has the invite link", "/join/" + code in page)
    check("and says how many joined", "3 joined so far" in page)
    check("and until when Pro is free", "Free from invites until" in page)

    # A giveaway prize: free Pro with no card and nothing that renews.
    _sys.path.insert(0, _os.path.join(harness.ROOT, "tools"))
    import gift_pro
    winner = user("winner@example.com")
    conn = db.connect(DB)
    until = gift_pro.gift(conn, "  Winner@Example.com ", 365)
    check("a gifted year is free Pro for a year",
          abs(until - (time.time() + 365 * 86400)) < 120
          and billing.is_pro(fresh(winner.id)))
    check("and stacks on top of free time already there",
          gift_pro.gift(conn, "winner@example.com", 7) == until + 7 * 86400)
    texter = A.upsert_phone_user(conn, "+15555550123")
    gift_pro.gift(conn, "+15555550123", 30)
    check("a phone account can be gifted by its number",
          billing.is_pro(fresh(texter.id)))
    try:
        gift_pro.gift(conn, "nobody@example.com", 365)
        check("an unknown address is refused, not created", False)
    except ValueError:
        check("an unknown address is refused, not created", True)
    conn.close()
    for key in STRIPE:
        _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("THE WEEKLY EMAIL")
    print("=" * 72)
    _os.environ.update(MAIL)
    check("not sent without a postal address", not digest.ready())
    _os.environ["MAIL_POSTAL_ADDRESS"] = "PO Box 1, Somewhere, CA 90000"
    check("ready with one", digest.ready())

    monday = calendar.timegm((2026, 10, 5, 14, 0, 0))
    check("the test's Monday is a Monday", time.gmtime(monday).tm_wday == 0)
    check("it sends on Monday afternoon UTC", digest.sending_window(monday))
    check("not Monday before 13:00", not digest.sending_window(monday - 2 * 3600))
    check("Tuesday, in case Monday was missed", digest.sending_window(monday + 86400))
    check("not Wednesday", not digest.sending_window(monday + 2 * 86400))

    reader = user("reader@example.com")
    quiet = user("quiet@example.com")
    conn = db.connect(DB)
    phone = A.upsert_phone_user(conn, "+15555550100")
    conn.close()
    sign_in(reader)
    page = c.get("/account").data.decode("utf-8", "replace")
    check("the account page offers it", "Send it to me" in page)
    c.post("/account/weekly-email", data={"on": "1"})
    check("one tap turns it on", fresh(reader.id).weekly_email)
    conn = db.connect(DB)
    digest.set_wanted(conn, phone.id, True)
    conn.execute("DELETE FROM watchlist WHERE user_id=?", (reader.id,))
    for sym in ("NVDA", "ZZZZ"):
        conn.execute("INSERT INTO watchlist (symbol, user_id, added_ts) VALUES (?,?,?)",
                     (sym, reader.id, int(time.time())))
    conn.commit()

    outbox = []

    def send(to, subject, body, headers=None):
        outbox.append((to, subject, body, headers or {}))

    def look_up(symbol):
        if symbol == "ZZZZ":
            raise RuntimeError("no data")
        return snap_for(symbol, "AVOID", 83, 22)

    out = digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
                     send=send, now=monday)
    check("only people who turned it on, with a real inbox, get it",
          [m[0] for m in outbox] == ["reader@example.com"], [m[0] for m in outbox])
    to, subject, body, headers = outbox[0]
    check("it covers their watchlist", "NVDA" in body and "AVOID" in body
          and "came up 83 times and worked 22 of them" in body)
    check("and says plainly what could not be checked", "ZZZZ   could not be checked" in body)
    check("the subject names the calls", "NVDA avoid" in subject, subject)
    stop = "https://orenth.app/email/stop?u=%d&t=%s" % (
        reader.id, digest.token(web.app.secret_key, reader.id))
    check("it has a working stop link", stop in body)
    check("and the inbox's own unsubscribe button",
          headers.get("List-Unsubscribe") == "<%s>" % stop
          and headers.get("List-Unsubscribe-Post") == "List-Unsubscribe=One-Click")
    check("and the postal address", "PO Box 1" in body)
    digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
               send=send, now=monday + 3600)
    check("once a week, however often the job runs", len(outbox) == 1)
    digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
               send=send, now=monday + 7 * 86400)
    check("and again the next week", len(outbox) == 2)
    day_old = dict(snap_for("GME", "BUY", 40, 30), at=monday - 9 * 86400)
    today = dict(snap_for("KO", "BUY", 40, 20), at=monday - 3600)
    check("a popular check nine days old is not one of this week's strongest",
          [v["symbol"] for v in digest.strongest([day_old, today], now=monday)] == ["KO"])
    check("one that says wait leaves none, which is a normal answer",
          digest.strongest([dict(today, action="WAIT")], now=monday) == [])
    stale = digest.strongest([day_old], now=monday)
    _, said_stale = digest.compose(reader, [], stale, "https://orenth.app", stop)
    check("but with nothing checked in time the email says that, not that nothing passed",
          stale is None and "could not be checked today" in said_stale
          and "clears the" not in said_stale, said_stale)

    def broken(*a, **k):
        raise RuntimeError("smtp down")

    out = digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
                     send=broken, now=monday + 14 * 86400)
    check("a failed send is reported", len(out["errors"]) == 1, out)
    digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
               send=send, now=monday + 14 * 86400 + 1800)
    check("and tried again later that week", len(outbox) == 3)
    conn.close()

    ran_with = []
    real_run = digest.run
    digest.run = lambda conn, secret, base, *a, **k: (ran_with.append(base), {"checked": 1})[1]
    try:
        _os.environ.pop("STOCKBOT_PUBLIC_URL", None)
        web.app.logger.disabled = True
        none = web._weekly_email_job()
        _os.environ["STOCKBOT_PUBLIC_URL"] = "https://orenth.app/"
        one = web._weekly_email_job()
        _os.environ["STOCKBOT_PUBLIC_URL"] = "HTTPS://orenth.app"
        typed = web._weekly_email_job()
    finally:
        digest.run = real_run
        web.app.logger.disabled = False
        _os.environ.pop("STOCKBOT_PUBLIC_URL", None)
    check("with no public address for its stop link, no email is sent",
          none == {"checked": 0} and one == {"checked": 1}
          and ran_with[:1] == ["https://orenth.app"], (none, ran_with))
    check("an address typed in capitals is one, as everywhere else",
          typed == {"checked": 1} and len(ran_with) == 2, (typed, ran_with))

    sign_out()
    url = "/email/stop?u=%d&t=%s" % (reader.id, digest.token(web.app.secret_key, reader.id))
    page = c.get(url).data.decode("utf-8", "replace")
    check("opening the stop link only asks",
          "Stop the weekly email?" in page and fresh(reader.id).weekly_email)
    check("a forged stop link is refused",
          c.post("/email/stop?u=%d&t=%s" % (reader.id, "0" * 32)).status_code == 404)
    check("nor one with a letter outside ASCII, which is no page here either",
          c.get("/email/stop?u=%d&t=%%C3%%A9" % reader.id).status_code == 404)
    check("and a mangled one, with a letter no stop link has, is not a fault",
          c.get("/email/stop?u=%d&t=caf%%C3%%A9" % reader.id).status_code == 404)
    r = c.post(url, data={"List-Unsubscribe": "One-Click"})
    check("pressing it, or the inbox button, stops it",
          r.status_code == 200 and not fresh(reader.id).weekly_email)
    for key in list(MAIL) + ["MAIL_POSTAL_ADDRESS"]:
        _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("WHAT A STRANGER, OR ONE ACCOUNT, CAN SEND")
    print("=" * 72)
    import json
    import threading
    from bot import export as export_mod
    from bot import ideas as ideas_mod

    def page_of(response):
        return response.data.decode("utf-8", "replace")

    web.app.config["PROPAGATE_EXCEPTIONS"] = False
    web.app.config["TESTING"] = False
    try:
        sign_out()
        long = "9" * 5000
        check("a number thousands of digits long in an address is not a page",
              c.get("/trades/%s/picture" % long).status_code == 404)
        guest = user("numbers@example.com")
        sign_in(guest)
        past = {act: c.post("/monitor/alert/" + act, data={
            "alert_id": "9" * 25, "threshold": "1"}).status_code
            for act in ("delete", "pause", "resume", "reset", "edit")}
        check("an alert number the database cannot hold is refused, not a fault",
              set(past.values()) == {400}, past)
        check("nor is a device's", c.post("/settings/notifications/remove", data={
            "device_id": "9" * 25}).status_code == 302)
    finally:
        web.app.config["TESTING"] = True
        web.app.config["PROPAGATE_EXCEPTIONS"] = None

    # A page on another site, against a copy that asks for no sign-in.
    sign_out()
    web.app.config["AUTH_MODE"] = "off"
    try:
        c.post("/watch/add", data={"symbol": "KEPT"})

        def kept():
            return "KEPT" in page_of(c.get("/monitor"))

        answers = [c.post("/watch/remove", data={"symbol": "KEPT"}, headers=h).status_code
                   for h in ({"Sec-Fetch-Site": "cross-site",
                              "Origin": "https://evil.example"},
                             {"Sec-Fetch-Site": "same-site"},
                             {"Origin": "https://evil.example"},
                             {"Referer": "https://evil.example/x"})]
        check("a change sent by a page on another site is refused",
              answers == [403] * 4 and kept(), answers)
        check("a program's too, in JSON", c.post(
            "/api/alerts/check-now", headers={"Sec-Fetch-Site": "cross-site"}).json
            == {"error": "That request came from another site."})
        c.post("/watch/remove", data={"symbol": "KEPT"},
               headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"})
        check("one from the site itself goes through", not kept())
    finally:
        web.app.config["AUTH_MODE"] = None

    with web.app.test_request_context("/", headers={"CF-Connecting-IP": "2001:db8:1:2::5"}):
        one = web._client_ip()
    with web.app.test_request_context("/", headers={
            "CF-Connecting-IP": "2001:db8:1:2:ffff::9"}):
        two = web._client_ip()
    check("every IPv6 address of one connection is one network to the limits",
          one == two == "2001:db8:1:2::/64", (one, two))
    with web.app.test_request_context("/", headers={"CF-Connecting-IP": "made up"},
                                      environ_base={"REMOTE_ADDR": "192.0.2.7"}):
        made_up = web._client_ip()
    check("and words that are no address are not a network of their own",
          made_up == "192.0.2.7", made_up)

    _os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_x"
    try:
        check("a message to the payments address is never read past a size Stripe sends",
              c.post("/billing/webhook", data=b"x" * (2 * 1024 * 1024),
                     content_type="application/json").status_code == 413)
    finally:
        _os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

    with web.app.test_request_context("/analyse?symbol=" + "A" * 5000):
        check("a ticker is cut to the length of a long name",
              len(web._symbol_from(web.request.args)[0]) == 100)
    sign_in(guest)
    check("one with a line break in it is refused", c.post("/trades/open", data={
        "symbol": "A\nB", "entry": "10", "stop": "9"}).status_code == 400)
    c.post("/trades/open", data={"symbol": "NOTED", "entry": "10", "stop": "9",
                                 "quantity": "100", "notes": "n" * 15000})
    conn = db.connect(DB)
    try:
        row = conn.execute("SELECT id, notes FROM trades WHERE symbol='NOTED'").fetchone()
        check("a trade's note is a note", len(row["notes"]) == web.NOTE_LIMIT,
              len(row["notes"]))
        for _ in range(12):
            db.exit_trade(conn, row["id"], 11.0, 1.0, notes="m" * 1000, user=guest.id)
        check("and closing it in parts does not grow it for ever",
              conn.execute("SELECT LENGTH(notes) FROM trades WHERE id=?",
                           (row["id"],)).fetchone()[0] == db.MAX_TRADE_NOTES)
    finally:
        conn.close()

    # Everything kept, as a file: written a row at a time, never built whole.
    real_build = export_mod.build
    export_mod.build = None
    try:
        r = c.get("/export/trades.json")
        body = json.loads(r.data)
        check("an export is written to a file a row at a time",
              r.status_code == 200 and body["dataset"] == "trades"
              and body["count"] == len(body["rows"]) == 1
              and body["rows"][0]["symbol"] == "NOTED"
              and int(r.headers["Content-Length"]) == len(r.data), body)
        empty = json.loads(c.get("/export/theses.json").data)
        check("an empty one is still whole JSON", empty["rows"] == [] and empty["count"] == 0)
        sheet = c.get("/export/trades.csv")
        check("a spreadsheet says it is UTF-8, and has its name",
              sheet.data.startswith(b"\xef\xbb\xbfid,symbol,")
              and sheet.headers["Content-Type"] == "text/csv; charset=utf-8"
              and 'filename="orenth-trades-' in sheet.headers["Content-Disposition"],
              dict(sheet.headers))
        check("marked as nothing to keep a copy of",
              r.headers.get("Cache-Control") == "private, no-store", dict(r.headers))
    finally:
        export_mod.build = real_build
    web._RATE.clear()
    real_most, web.EXPORTS_PER_HOUR = web.EXPORTS_PER_HOUR, 1
    try:
        answers = [c.get("/export/trades.csv").status_code for _ in range(2)]
        check("and only so many an hour", answers == [200, 429], answers)
    finally:
        web.EXPORTS_PER_HOUR = real_most
    real_most, web.CHECKS_PER_HOUR = web.CHECKS_PER_HOUR, 1
    try:
        answers = [c.post("/monitor/alerts/check").status_code for _ in range(2)]
        check("as are checks of every alert at once", answers == [302, 429], answers)
    finally:
        web.CHECKS_PER_HOUR = real_most

    # One account and the server's few threads.
    going, let_go = threading.Semaphore(0), threading.Event()

    def slow(symbol, *a, **k):
        going.release()
        let_go.wait(30)
        raise web.DataError("enough")

    def another():
        mine = web.app.test_client()
        conn = db.connect(DB)
        try:
            token = A.start_session(conn, guest.id, "test")
        finally:
            conn.close()
        with mine.session_transaction() as sess:
            sess[web.SESSION_KEY] = token
        return mine

    def ask(n, out):
        out.append(another().get("/analyse?symbol=AAPL&interval=1d&news=0&account=%d"
                                 % (1000 + n)).status_code)

    real_analyse, web.engine.analyse = web.engine.analyse, slow
    out = []
    threads = [threading.Thread(target=ask, args=(n, out)) for n in range(web.SLOW_AT_ONCE)]
    try:
        for t in threads:
            t.start()
        for _ in threads:
            going.acquire(timeout=20)
        refused = []
        ask(99, refused)
        check("one account cannot have every thread at work for it", refused == [503], refused)
        r = another().get("/analyse?symbol=AAPL&interval=1d&news=0&account=77")
        said = page_of(r)
        check("what it asked for waits its turn and opens itself, as no thread of the server does",
              r.status_code == 503 and re.search(
                  r'<meta http-equiv="refresh" content="[2-5]">', said)
              and r.headers.get("Retry-After") and "opens by itself" in said
              and "didn't work" not in said, said[:300])
        r = another().post("/monitor/alerts/check")
        check("a button is not pressed again by itself",
              r.status_code == 503 and "refresh" not in page_of(r)
              and "press the button again" in page_of(r), page_of(r)[:300])
        pages = {p: another().get(p).status_code for p in (
            "/trades", "/portfolio", "/screener?go=1", "/export/trades.csv",
            "/monitor", "/screener", "/tools/size", "/account", "/")}
        check("nor has it more of the pages that wait for prices, while every other opens",
              pages == {"/trades": 503, "/portfolio": 503,
                        "/screener?go=1": 503, "/export/trades.csv": 503,
                        "/monitor": 200, "/screener": 200,
                        "/tools/size": 200, "/account": 200, "/": 200}, pages)
        read = []
        real_summary = web.db_mod.journal_summary
        web.db_mod.journal_summary = lambda *a, **k: read.append(1) or real_summary(*a, **k)
        try:
            for path in ("/trades", "/portfolio"):
                another().get(path)
        finally:
            web.db_mod.journal_summary = real_summary
        check("and one sent away to wait has not read the journal for nothing", not read, read)
        other = web.app.test_client()
        conn = db.connect(DB)
        try:
            token = A.start_session(conn, user("patient@example.com").id, "test")
        finally:
            conn.close()
        with other.session_transaction() as sess:
            sess[web.SESSION_KEY] = token
        late = threading.Thread(target=lambda: other.get(
            "/analyse?symbol=MSFT&interval=1d&news=0"))
        late.start()
        check("while somebody else's is still worked out", going.acquire(timeout=20))
        # Five of the eight threads are at it. One place more, and the other
        # two are left for pages that need nothing worked out.
        third, sixth = user("third@example.com"), []

        def as_third(path, out):
            mine = web.app.test_client()
            k = db.connect(DB)
            try:
                token = A.start_session(k, third.id, "test")
            finally:
                k.close()
            with mine.session_transaction() as sess:
                sess[web.SESSION_KEY] = token
            out.append(mine.get(path).status_code)

        last = threading.Thread(target=as_third, args=(
            "/analyse?symbol=TSLA&interval=1d&news=0", sixth))
        last.start()
        going.acquire(timeout=20)
        late_ones = []
        as_third("/analyse?symbol=AMD&interval=1d&news=0", late_ones)
        as_third("/export/trades.csv", late_ones)
        as_third("/trades", late_ones)
        as_third("/learn", late_ones)
        check("two threads are kept for other pages, whoever asks for the slow ones",
              sum(web._SLOW.values()) == 6 and late_ones == [503, 503, 200, 200],
              (web._SLOW, late_ones))
        let_go.set()
        late.join(20)
        last.join(20)
    finally:
        let_go.set()
        for t in threads:
            t.join(20)
        web.engine.analyse = real_analyse
    check("and each is let go of when it is done", out == [404] * web.SLOW_AT_ONCE
          and not web._SLOW, (out, web._SLOW))

    # Visitors on the example, with nothing kept yet. Seven ask for the same
    # instrument: they wait for the one answer, six of them in threads.
    let_go.clear()
    asked_for = []

    def slow_example(symbol, *a, **k):
        asked_for.append(symbol)
        let_go.wait(30)
        raise web.DataError("enough")

    web.engine.analyse = slow_example
    web._IN_LINE.clear()           # the pages refused above do not come back
    web._CACHE.clear()
    out = []
    looking = [threading.Thread(target=lambda: out.append(web.app.test_client().get(
        "/analyse?symbol=NVDA").status_code)) for _ in range(6)]
    try:
        for t in looking:
            t.start()
            time.sleep(0.05)
        until = time.time() + 20
        while sum(web._SLOW.values()) < 6 and time.time() < until:
            time.sleep(0.05)
        places = sum(web._SLOW.values())
        r = web.app.test_client().get("/analyse?symbol=NVDA")
        check("a crowd on a cold page waits for the one answer, and nobody gets an error page",
              places == 6 and asked_for == ["NVDA"] and r.status_code == 503
              and "opens by itself" in page_of(r), (places, asked_for, r.status_code))
        check("while the front page and the public pages open",
              [web.app.test_client().get(p).status_code
               for p in ("/", "/stocks", "/healthz")] == [200, 200, 200])
    finally:
        let_go.set()
        for t in looking:
            t.join(20)
        web.engine.analyse = real_analyse
    check("and every place is given back", not web._SLOW, web._SLOW)

    # The quarter hour up, with people looking: one has it worked out again,
    # and the rest see the last one meanwhile.
    def as_visitor(out):
        with web.app.test_request_context("/analyse?symbol=NVDA"):
            web.g.user, web.g.demo = SimpleNamespace(id=1, is_local=False), True
            try:
                out.append(web.cached_analysis("NVDA", "1d", dict(web.config_mod.DEFAULTS),
                                               True, record=False, ttl=web.DEMO_TTL))
            except web.DataError:
                out.append("failed")

    let_go.clear()
    del asked_for[:]
    web.engine.analyse = slow_example
    last_one = SimpleNamespace(run_id=None)
    key = ("NVDA", False, "1d", True, web._settings_key(dict(web.config_mod.DEFAULTS)))
    web._CACHE.clear()
    web._CACHE[key] = (time.time() - web.DEMO_TTL - 5, last_one, {})
    refreshing, meanwhile = [], []
    first = threading.Thread(target=as_visitor, args=(refreshing,))
    try:
        first.start()
        until = time.time() + 20
        while not asked_for and time.time() < until:
            time.sleep(0.02)
        as_visitor(meanwhile)
        check("a visitor is shown the last one while it is worked out again for another",
              meanwhile == [last_one] and asked_for == ["NVDA"], (meanwhile, asked_for))
        web._CACHE[key] = (time.time() - web.DEMO_STALE - 5, last_one, {})
        waits = threading.Thread(target=as_visitor, args=(meanwhile,))
        waits.start()
        waits.join(0.5)
        check("but not one half an hour old", len(meanwhile) == 1 and waits.is_alive())
    finally:
        let_go.set()
        first.join(20)
        waits.join(20)
        web.engine.analyse = real_analyse
        web._CACHE.clear()

    # Pages that found no place come back for one: the one that has waited
    # longest has it first, whoever asks in the instant it comes free.
    class Person(object):
        is_local = False

        def __init__(self, n):
            self.id = 90000 + n

    def asks(n, path="/analyse?symbol=X", method="GET", demo=False, **k):
        with web.app.test_request_context(path, method=method, headers={
                "CF-Connecting-IP": "203.0.113.%d" % n}):
            web.g.user, web.g.demo = Person(0 if demo else n), demo
            slot = web._SlowSlot()
            try:
                slot.take(**k)
            except web.Busy:
                return None
            return slot

    def drawn(n, path):
        with web.app.test_request_context(path):
            web.g.user, web.g.demo = Person(n), False
            with web._patience(4.0) as wait:
                return wait

    web._IN_LINE.clear()
    places = [asks(n) for n in range(1, 7)]
    check("six accounts have the six places", all(places) and asks(10) is None)
    time.sleep(0.02)
    check("the next is after it in the line", asks(11) is None)
    check("a page drawn without the wait, and a button, are in no line",
          drawn(12, "/") == 0.0 and asks(13, method="POST") is None
          and {k[0] for k in web._IN_LINE} == {90010, 90011}, web._IN_LINE)
    places.pop().free()
    check("a place that comes free is kept for whoever has waited longest",
          asks(14) is None and asks(11) is None)
    pressed = asks(13, method="POST")
    check("save from a button, which has no way to wait for one", pressed is not None)
    pressed.free()
    first = asks(10)
    check("who has it on coming back", first is not None and (90010, "/analyse?symbol=X")
          not in web._IN_LINE, web._IN_LINE)
    first.free()
    second = asks(11)
    check("and then the next", second is not None and asks(10) is None)
    second.free()
    # Somebody who went away holds it up for seconds, not for good.
    for entry in web._IN_LINE.values():
        entry[2] -= web.LINE_FOR + 1
    late = asks(15)
    check("one that does not come back loses its place in the line",
          late is not None and not web._IN_LINE, web._IN_LINE)
    for slot in places + [late]:
        slot.free()
    # Visitors in line past what the example may have keep nothing from members.
    example = [asks(n, demo=True) for n in range(1, 5)]
    check("visitors past the example's own places wait, and a member does not wait behind them",
          all(example) and all(asks(n, path="/portfolio?%d" % n, demo=True) is None
                               for n in range(20, 30)) and asks(16) is not None, web._IN_LINE)
    check("but one asking for what is already being worked out joins it",
          asks(30, demo=True, joining=True) is not None)
    web._SLOW.clear()
    web._IN_LINE.clear()

    # How long each kept analysis stays, and how much they may come to.
    def kept(symbol, interval, age, result=None):
        web._CACHE[(symbol, False, interval, True, "house")] = (
            time.time() - age, result or SimpleNamespace(run_id=None), {})

    def another_kept():
        web.engine.analyse = lambda *a, **k: SimpleNamespace(run_id=None)
        try:
            with web.app.test_request_context("/analyse"):
                web.g.user, web.g.demo = guest, False
                web.cached_analysis("FRESH", "1d", dict(web.config_mod.DEFAULTS), False)
        finally:
            web.engine.analyse = real_analyse
        return {(k[0], k[2]) for k in web._CACHE}

    web._CACHE.clear()
    kept("NVDA", "1d", web.DEMO_STALE + 1)
    kept("AAPL", "1d", web.DEMO_TTL - 60)
    kept("AAPL", "5m", web.DEMO_TTL - 60)
    kept("OWNS", "1d", web._CACHE_TTL + 1)
    kept("SOON", "1d", web._CACHE_TTL - 30)
    left = another_kept()
    check("a kept analysis goes once its minute and a half is up, the example's once its "
          "half hour is, and not before",
          left == {("AAPL", "1d"), ("SOON", "1d"), ("FRESH", "1d")}, left)

    # Sixty days of a coin's five-minute bars is sixteen megabytes of series,
    # and a person can open one every few seconds.
    web._CACHE.clear()
    for n in range(20):
        kept("C%d-USD" % n, "5m", 60 - n, SimpleNamespace(
            run_id=None, bars=SimpleNamespace(close=range(17000)),
            context=SimpleNamespace(s=dict.fromkeys(range(110)))))
    left = another_kept()
    held = sum(web._size_of(v[1]) for v in web._CACHE.values())
    check("and the kept ones never come to more than the server has room for",
          held <= web.CACHE_MOST and ("C19-USD", "5m") in left
          and ("C0-USD", "5m") not in left and ("FRESH", "1d") in left, (held, left))
    web._CACHE.clear()

    def stopped(job, *a):
        job.update(running=False, error="no source", retry_at=time.time() + 60)
        job["finished"].set()

    real_run, ideas_mod._run = ideas_mod._run, stopped
    real_most, web.SCANS_PER_HOUR = web.SCANS_PER_HOUR, 1
    try:
        web._RATE.clear()
        ideas_mod._JOBS.clear()
        c.get("/ideas?market=crypto&horizon=medium")
        first = len(ideas_mod._JOBS)
        c.get("/ideas?market=crypto&horizon=medium")
        c.post("/settings", data={"cost_bps_crypto": "33"})
        r = c.get("/ideas?market=crypto&horizon=medium")
        check("new scans under settings changed each time are limited too",
              first == 1 and len(ideas_mod._JOBS) == 1
              and "started a lot of new scans" in page_of(r), page_of(r)[:200])
    finally:
        ideas_mod._run, web.SCANS_PER_HOUR = real_run, real_most
        ideas_mod._JOBS.clear()

    # A paid request for each piece of written reasoning.
    from bot import reasoning as reasoning_mod
    asked_for = []
    real = (web.engine.research, reasoning_mod.available, web.REASONINGS_PER_HOUR)

    def researched(symbol, price, cfg, with_reasoning=True, **k):
        asked_for.append(with_reasoning)
        return SimpleNamespace(reasoning="written" if with_reasoning else None)

    web.engine.research, reasoning_mod.available = researched, lambda: True
    web.REASONINGS_PER_HOUR = 2
    try:
        web._RATE.clear()
        web._RESEARCH_CACHE.clear()
        got = []
        with web.app.test_request_context("/research"):
            web.g.user, web.g.demo = guest, False
            found = fake_result("NVDA")
            for n in range(3):
                got.append(web._cached_research(found, "1d", dict(
                    web.config_mod.DEFAULTS, account_size=1000 + n), True, True))
        check("one account has the same company reasoned over only so often an hour",
              asked_for == [True, True, False]
              and got[2].reasoning.reason_unavailable == web.REASONED_ENOUGH, asked_for)
        check("and the page without it is nobody else's answer",
              len(web._RESEARCH_CACHE) == 2)
    finally:
        web.engine.research, reasoning_mod.available, web.REASONINGS_PER_HOUR = real
        web._RESEARCH_CACHE.clear()
        web._RATE.clear()

    print()
    print("=" * 72)
    print("BACKGROUND JOBS")
    print("=" * 72)
    jobs = web.start_background_jobs()
    check("the server starts the daily checks, the weekly email, the plan rechecks and the lab",
          set(jobs) == {"popular", "weekly", "plans", "lab"}
          and all(j.running for j in jobs.values()))
    check("and only once", web.start_background_jobs() is jobs)
    for job in jobs.values():
        job.stop()
finally:
    for key in list(MAIL) + list(STRIPE) + ["MAIL_POSTAL_ADDRESS", "GOOGLE_CLIENT_ID",
                                           "GOOGLE_CLIENT_SECRET"]:
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("GROWTH OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
