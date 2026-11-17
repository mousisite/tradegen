"""What a visitor and a free account read on a public deployment.

The site is built the way orenth.app runs it: an https public address behind
a proxy, Google sign-in, Stripe on, and no Anthropic key. Every page a
visitor or a signed-in free account can reach is fetched, and none may show
builder text: a setting's name, ".env", "restart", a traceback, or a value
printed as None, nan or undefined. Setup advice is for the owner of a copy
on their own machine, and the end checks that it still reaches them.

No network: prices are made up, the news is two headlines, and every other
source is a stand-in that fails the way a busy source does, so the plain
words for a failure are tested too. Anything that still tries the network
is recorded, and fails the suite.
"""
import html as _html
import os as _os
import re
import sys as _sys
import time
import zlib

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

# Public mode, set before web is imported: it reads the address at import to
# decide on secure cookies and the proxy headers, as the real server does.
PUBLIC = {"STOCKBOT_PUBLIC_URL": "https://orenth.app", "STOCKBOT_BEHIND_PROXY": "1",
          "GOOGLE_CLIENT_ID": "1234.apps.googleusercontent.com",
          "GOOGLE_CLIENT_SECRET": "stub-secret",
          "STRIPE_SECRET_KEY": "sk_test_stub", "STRIPE_PRICE_ID": "price_month",
          "STRIPE_PRICE_ID_YEARLY": "price_year", "STRIPE_WEBHOOK_SECRET": "whsec_stub"}
UNSET = ("ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM",
         "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID", "SENTRY_DSN",
         "STOCKBOT_ADMIN_EMAIL", "STOCKBOT_CONTACT", "MAIL_POSTAL_ADDRESS")
_saved_env = {k: _os.environ.get(k) for k in list(PUBLIC) + list(UNSET)}
_os.environ.update(PUBLIC)
for _k in UNSET:
    _os.environ.pop(_k, None)

import harness  # noqa: E402

import numpy as np  # noqa: E402
import requests  # noqa: E402

c, DB = harness.isolated_web("wording")

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import alerts as alerts_mod  # noqa: E402
from bot import billing  # noqa: E402
from bot import catalysts  # noqa: E402
from bot import database as db  # noqa: E402
from bot import digest  # noqa: E402
from bot import engine  # noqa: E402
from bot import fundamentals  # noqa: E402
from bot import ideas as ideas_mod  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot import options as options_mod  # noqa: E402
from bot import portfolio as portfolio_mod  # noqa: E402
from bot import public  # noqa: E402
from bot import risk as risk_mod  # noqa: E402
from bot import screener as screener_mod  # noqa: E402
from bot import sec  # noqa: E402
from bot import sentiment as sentiment_mod  # noqa: E402
from bot import workflows as workflows_mod  # noqa: E402
from bot.market import Bars  # noqa: E402

# web.py reads .env when it is imported, and a developer's own copy may hold
# a real key. This suite is about a site with none.
for _k in UNSET:
    _os.environ.pop(_k, None)
web.app.config["AUTH_MODE"] = None
web.app.config["TESTING"] = False         # errors render as visitors see them

fails = []
BASE = "https://orenth.app"

# Each must never reach a visitor or a free account. The first group is the
# brief; the second is how raw errors look when they leak.
BANNED = (".env", "ANTHROPIC_API_KEY", "_API_KEY", "STOCKBOT_", "GOOGLE_CLIENT",
          "SMTP_", "TWILIO", "SENTRY_DSN", "restart", "Traceback", ">None<",
          "nan%", "undefined",
          "HTTPSConnectionPool", "Errno", "Max retries", "object at 0x")


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


def text(response):
    return response.data.decode("utf-8", "replace")


def flat(page):
    return " ".join(page.split())


# --- stand-ins for everything outside the server -----------------------------

DAY = 86400
NOW = int(time.time())
BOOM = ("HTTPSConnectionPool(host='query2.finance.yahoo.com', port=443): Max "
        "retries exceeded with url: /v10/finance/quoteSummary/NVDA (Caused by "
        "NewConnectionError('<urllib3.connection.HTTPSConnection object at "
        "0x0000>: Failed to establish a new connection: [Errno 11001]'))")


def bars_for(symbol, interval="1d", lookback=None):
    """A random walk of its own for each symbol, ending at yesterday's close."""
    if symbol == "ZZZZ":
        raise market_mod.SymbolNotFound("no instrument found for 'ZZZZ'. Check the "
                                        "ticker.")
    if symbol == "BOOM":
        raise RuntimeError(BOOM)
    seed = zlib.crc32(symbol.encode()) % 10000
    rng = np.random.RandomState(seed)
    n = 1250
    coin = symbol.endswith("-USD")
    vol = 0.03 if coin else 0.018
    close = (30000.0 if coin else 50.0 + seed % 400) * np.exp(
        np.cumsum(rng.normal(0.0004, vol, n)))
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, vol * 0.25, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.4, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.4, n)))
    last = (NOW // DAY - 1) * DAY + 14 * 3600 + 1800
    ts = np.array([last - (n - 1 - k) * DAY for k in range(n)], dtype=np.int64)
    gmt = 0 if coin else -18000
    return Bars(symbol=symbol, name=symbol + " Inc", asset_class="crypto" if coin
                else "equity", currency="USD", exchange="NasdaqGS", interval="1d",
                timestamp=ts, open=open_, high=high, low=low, close=close,
                volume=np.exp(rng.normal(15.0, 0.4, n)),
                session_id=((ts + gmt) // DAY).astype(np.int64), gmt_offset=gmt,
                prev_close=float(close[-2]), live_price=None, market_open=False,
                last_bar_time="2026-11-13 21:00", meta={"regularMarketTime": int(ts[-1])},
                dividend_factor=None)


def news(symbol, name="", limit=25):
    return [sentiment_mod.Item("%s beats estimates and raises guidance" % symbol,
                               "Example Wire", "news", 2.0),
            sentiment_mod.Item("Analysts cut targets on %s after a weak quarter"
                               % symbol, "Example Wire", "news", 30.0)]


def chatter(symbol, limit=30):
    return [sentiment_mod.Item("$%s looks strong into the close" % symbol,
                               "StockTwits", "social", 1.0, tagged="Bullish")]


def refused(*_a, **_k):
    raise requests.ConnectionError(BOOM)


def screen_fails(*_a, **_k):
    raise RuntimeError(BOOM)


def scans_busy(*_a, **_k):
    raise ideas_mod.Busy()


PRICES = {"price_month": {"unit_amount": 999, "currency": "usd",
                          "recurring": {"interval": "month", "interval_count": 1}},
          "price_year": {"unit_amount": 7999, "currency": "usd",
                         "recurring": {"interval": "year", "interval_count": 1}}}


def stripe(method, path, data=None):
    if path.startswith("prices/"):
        return PRICES[path.split("/", 1)[1]]
    raise billing.BillingError("Payments could not be reached just now.")


# Anything that still reaches for the network is noted here, and fails.
reached = []


def no_network(self, method, url, *a, **k):
    reached.append("%s %s" % (method, url))
    raise requests.ConnectionError("this suite is offline: %s" % url)


STUBS = [(m, "fetch_bars", bars_for) for m in (market_mod, portfolio_mod, risk_mod,
                                                options_mod, workflows_mod, alerts_mod)]
STUBS += [(engine, "fetch_bars", lambda s, iv: bars_for(s, iv)),
          (engine, "resolve", lambda s: {"symbol": s.strip().upper(),
                                         "name": s.strip().upper() + " Inc"}),
          (market_mod, "split_history", lambda symbol: []),
          (catalysts, "fetch", lambda symbol: catalysts.Calendar(symbol=symbol)),
          (sentiment_mod, "gather_news", news),
          (sentiment_mod, "gather_social", chatter),
          (fundamentals, "load", refused), (sec, "financial_history", refused),
          (sec, "recent_material", refused), (risk_mod, "profile", refused),
          (options_mod, "load_chain", refused),
          (screener_mod, "run", screen_fails), (ideas_mod, "find", scans_busy),
          (billing, "_stripe", stripe),
          (requests.sessions.Session, "request", no_network)]
REAL = [(m, name, getattr(m, name)) for m, name, _ in STUBS]
for m, name, fake in STUBS:
    setattr(m, name, fake)


def sign_in(client, user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction(base_url=BASE) as sess:
        sess[web.SESSION_KEY] = token


SEEN = {}


def get(client, who, path, **kw):
    """Fetch as a browser on the public site would, and keep the page."""
    r = client.get(path, base_url=BASE, **kw)
    if not r.mimetype.startswith("image/"):
        SEEN[(who, path)] = (r.status_code, text(r))
    return r


def leaks(page):
    return [word for word in BANNED if word in page]


def links(page):
    """Every address on this site that a page links to, as a path."""
    out = set()
    for href in re.findall(r'href="([^"]+)"', page):
        href = _html.unescape(href).split("#")[0]
        if href.startswith(BASE):
            href = href[len(BASE):] or "/"
        if href.startswith("/") and not href.startswith("//"):
            out.add(href)
    return out


try:
    # -----------------------------------------------------------------------
    head("THE PUBLIC PAGES, WITH SOMETHING ON THEM")
    # -----------------------------------------------------------------------
    db.migrate(DB)
    conn = db.connect(DB)
    # The background check, run for three popular names on made-up prices.
    real_due = public.due
    public.due = lambda conn_, now=None: ["NVDA", "AAPL", "BTC-USD"]
    try:
        done = public.refresh(conn, web._public_snapshot, pause=0,
                              sleep=lambda _s: None, bars=lambda s: bars_for(s))
    finally:
        public.due = real_due
    conn.close()
    check("three popular checks were made, offline", done["checked"] == 3, done)

    # The front page's example, as the background refresh leaves it: a call
    # whose odds are for a fall.
    web._example_state.update({"busy": True, "at": time.time(), "data": {
        "symbol": "NVDA", "samples": 59, "worked": 25, "fall": True,
        "action": "AVOID", "headline": "Leave this one alone.", "sure": "",
        "price": 156.14, "at": time.time()}})

    visitor = web.app.test_client()
    front = flat(text(get(visitor, "visitor", "/")))
    check("the front page says the example's counts are a test on past prices",
          "In tests on past prices, this same setup came up 59 times and worked "
          "25 of them, reaching its target before its stop." in front, front[:300])
    check("and that here a worked bet was one on the price falling",
          "worked means a bet on the price falling paid off" in front)
    check("and that it is a test, not a promise", "A test, not a promise." in front)
    check("the strategies card says they worked in tests on past prices",
          "trading strategies worked in tests on past prices." in front)
    check("nothing on it says it 'actually worked' or was 'found ... before'",
          "actually worked" not in front and "Found this setup" not in front)

    stock = _html.unescape(flat(text(get(visitor, "visitor", "/stock/nvda"))))
    check("a stock page leads with its counts as a test on past prices",
          '<p class="lead">In tests on past prices, this same setup came up' in stock)
    check("its strategy list is about past prices",
          "Which strategies worked on NVDA's past prices" in stock)
    snap = None
    conn = db.connect(DB)
    try:
        snap = public.verdict(conn, "NVDA")
    finally:
        conn.close()
    line = public.counts_line(snap, short=True)
    check("the picture card's line says it was tested on past prices",
          line.startswith("Tested on past prices: it hit its target before its stop"),
          line)
    font = public._font("inter-400.ttf", 30)
    widest = public.counts_line({"samples": 2500, "worked": 2500}, short=True)
    check("and fits the card's one line, even at the largest counts",
          font.getlength(widest) <= public.WIDTH - 2 * public.MARGIN,
          int(font.getlength(widest)))
    card = get(visitor, "visitor", "/stock/nvda/card.png")
    check("the card itself draws", card.status_code == 200
          and card.mimetype == "image/png", card.status_code)

    pricing = flat(text(get(visitor, "visitor", "/pricing")))
    check("Pro's card says its fair-use limits, from the server's own numbers",
          "Fair use: up to %d saved theses an hour, and Find scans wait their turn "
          "when the server is busy." % web.THESES_PER_HOUR in pricing, pricing[-900:])
    check("with no key set, it puts no limit on screenshots it does not read",
          "screenshot" not in pricing)
    check("and it links to how the results are measured",
          'href="/backtests#how-we-tested"' in pricing and 'href="/record"' in pricing)
    _os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    try:
        keyed = flat(text(visitor.get("/pricing", base_url=BASE)))
    finally:
        _os.environ.pop("ANTHROPIC_API_KEY", None)
    check("with a key set, the screenshot limit is said too",
          "up to %d screenshot reads and %d saved theses an hour"
          % (web.IMAGE_READS_PER_HOUR, web.THESES_PER_HOUR) in keyed)

    # -----------------------------------------------------------------------
    head("A VISITOR, LOOKING AROUND")
    # -----------------------------------------------------------------------
    for path in ("/stocks", "/stock/aapl", "/stock/tsla", "/backtests",
                 "/backtest/pin-bar", "/backtest/opening-range-break", "/record",
                 "/record.csv", "/signin", "/signin?next=/pricing", "/terms",
                 "/privacy", "/no-such-page", "/robots.txt", "/sitemap.xml",
                 "/healthz", "/analyse?symbol=NVDA&interval=1d",
                 "/analyse?symbol=GME&interval=1d", "/analyse?symbol=NVDA&interval=1h",
                 "/research?symbol=NVDA&interval=1d", "/ideas",
                 "/ideas?market=stocks&horizon=medium&risk=balanced", "/screener",
                 "/screener?preset=quality_value", "/portfolio", "/trades",
                 "/monitor", "/strategies", "/settings", "/account", "/usage",
                 "/api/alerts/status", "/api/analyse?symbol=NVDA"):
        get(visitor, "visitor", path)

    page = SEEN[("visitor", "/analyse?symbol=NVDA&interval=1d")][1]
    check("the example analysis opens", SEEN[("visitor",
          "/analyse?symbol=NVDA&interval=1d")][0] == 200)
    check("and says nothing about an API key", "API key" not in page
          and "ANTHROPIC" not in page and "LLM scoring" not in page)
    check("its news is said to be scored in plain words",
          "Scored with a built-in list of finance words." in flat(page))
    check("a fresh headline is warned about once, not twice",
          flat(page).count("landed within three hours") == 1
          and "high-impact headline landed" not in page)
    check("a visitor is not offered JSON the server will not give them",
          "View as JSON" not in page)

    page = SEEN[("visitor", "/research?symbol=NVDA&interval=1d")][1]
    check("research names what it could not get, not how it failed",
          "Company figures" in page and "Filed accounts" in page
          and "Not available here" in page)
    check("and leaves out a reasoning step this site has not switched on",
          "What the evidence supports" not in page)
    check("its case summary is a sentence", "The clearest: " in page
          and "that it coverage" not in page)
    check("and a company whose filings did not answer is not called a non-filer",
          "not a company that files" not in page
          and "could not be read just now" in page)

    page = SEEN[("visitor", "/backtest/opening-range-break")][1]
    check("a strategy that never fires on daily prices is not 'being tested'",
          "Not in the daily test" in page and "Being tested" not in page
          and "Tested on every popular stock" not in page)

    status, page = SEEN[("visitor", "/screener?preset=quality_value")]
    check("a screen that failed says so plainly", status == 500
          and "The screen could not be finished." in page, status)
    status, page = SEEN[("visitor", "/ideas?market=stocks&horizon=medium&risk=balanced")]
    check("a Find scan that cannot start says the server is busy", status == 503
          and "running as many scans as it can" in page, status)

    # Words in the address are not the site's to say.
    said = text(get(visitor, "visitor", "/signin?reason=Your+account+is+locked"))
    check("the sign-in page does not repeat words from the address",
          "Your account is locked" not in said)
    said = text(get(visitor, "visitor", "/pricing?reason=Call+555-0100+now"))
    check("nor does the pricing page", "555-0100" not in said)
    said = text(get(visitor, "visitor", "/auth/google/callback?error=Call+555-0100"))
    check("nor Google's error code", "555-0100" not in said)
    said = flat(text(get(visitor, "visitor", "/monitor?checked=Call+555-0100&fired=x")))
    check("nor the Monitor page's count of checked alerts", "555-0100" not in said
          and "Checked Call" not in said)
    said = flat(text(get(visitor, "visitor", "/monitor?checked=2&fired=1")))
    check("which still says a real count", "Checked 2 alerts. 1 triggered." in said)

    # Sign-in keys that went missing on a public site.
    keys = {k: _os.environ.pop(k) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
    try:
        page = text(get(visitor, "visitor", "/signin?next=/trades"))
        check("with no way in, a public sign-in page says so plainly",
              "Signing in is off for now" in page and "GOOGLE_CLIENT" not in page)
        page = text(get(visitor, "visitor", "/auth/google"))
        check("and so does the Google button's address", "GOOGLE_CLIENT" not in page
              and ".env" not in page)
    finally:
        _os.environ.update(keys)

    for path in sorted(p for (who, p) in SEEN if who == "visitor"):
        status, page = SEEN[("visitor", path)]
        found = leaks(page)
        check("%s keeps builder text to itself" % path, not found, found)

    # -----------------------------------------------------------------------
    head("EVERY LINK A VISITOR CAN FOLLOW")
    # -----------------------------------------------------------------------
    # Find and screener links start work this suite makes fail on purpose,
    # and the sign-in button leaves for Google.
    skip = ("/ideas?", "/screener?", "/auth/google")
    targets = set()
    for (who, path), (status, page) in list(SEEN.items()):
        if who == "visitor" and status == 200:
            targets |= links(page)
    targets = sorted(t for t in targets if not t.startswith(skip))
    broken = []
    for path in targets:
        r = get(visitor, "visitor", path)
        if r.status_code >= 400:
            broken.append((path, r.status_code))
    check("all %d of them answer" % len(targets), not broken, broken[:10])
    for path in targets:
        if ("visitor", path) in SEEN:
            found = leaks(SEEN[("visitor", path)][1])
            if found:
                check("%s keeps builder text to itself" % path, False, found)

    # -----------------------------------------------------------------------
    head("A FREE ACCOUNT")
    # -----------------------------------------------------------------------
    conn = db.connect(DB)
    free = A.upsert_google_user(conn, {"email": "free@example.com", "sub": "g-free",
                                       "name": "Free Person", "email_verified": True})
    conn.close()
    member = web.app.test_client()
    sign_in(member, free)
    for path in ("/", "/analyse?symbol=NVDA&interval=1d",
                 "/research?symbol=NVDA&interval=1d", "/api/analyse?symbol=NVDA",
                 "/analyse?symbol=ZZZZ&interval=1d", "/analyse?symbol=BOOM&interval=1d",
                 "/ideas", "/screener", "/screener?go=1&symbols=AAPL", "/portfolio",
                 "/trades", "/monitor", "/strategies", "/settings", "/account",
                 "/pricing", "/usage", "/api/alerts/status", "/stocks", "/stock/nvda",
                 "/record", "/backtests", "/billing/done", "/no-such-page"):
        get(member, "free", path)

    status, page = SEEN[("free", "/analyse?symbol=NVDA&interval=1d")]
    check("their analysis opens", status == 200, status)
    check("and says nothing about an API key", "API key" not in page
          and "ANTHROPIC" not in page)
    check("they can view it as JSON", "View as JSON" in page)
    status, page = SEEN[("free", "/api/analyse?symbol=NVDA")]
    check("whose risks say nothing about a key either", status == 200
          and "ANTHROPIC" not in page and "LLM" not in page, status)
    status, page = SEEN[("free", "/analyse?symbol=BOOM&interval=1d")]
    check("an analysis that broke says so in plain words", status == 500
          and "could not be finished. Try again in a minute." in page
          and "BOOM" in page and "do not exist" not in page, status)
    status, page = SEEN[("free", "/usage")]
    check("the operator's page refuses them without naming a setting",
          status == 403 and "runs this installation" in page, status)

    run_id = None
    conn = db.connect(DB)
    try:
        row = conn.execute("SELECT id FROM runs WHERE user_id=? ORDER BY id DESC",
                           (free.id,)).fetchone()
        run_id = row["id"] if row else None
    finally:
        conn.close()
    if run_id:
        get(member, "free", "/trades/simulate?run_id=%d" % run_id)
    shared = member.post("/share", data={"symbol": "NVDA", "interval": "1d"},
                         base_url=BASE)
    where = shared.headers.get("Location", "")
    check("a share is made", shared.status_code == 303 and "/s/" in where, where)
    if "/s/" in where:
        share_path = where[where.index("/s/"):]
        page = text(get(visitor, "visitor", share_path))
        check("and a visitor reads it as a test on past prices",
              "In tests on past prices" in page)
        picture = visitor.get(share_path + ".png", base_url=BASE)
        check("with its picture", picture.status_code == 200)
    page = text(get(visitor, "visitor", "/email/stop?u=%d&t=%s"
                    % (free.id, digest.token(web.app.secret_key, free.id))))
    check("the weekly email's stop page opens", "email" in page.lower())

    out = member.post("/signout", base_url=BASE)
    page = text(visitor.get(out.headers.get("Location", "/signin"), base_url=BASE))
    check("signing out says so", "You are signed out." in page)

    for (who, path), (status, page) in sorted(SEEN.items()):
        if who != "free":
            continue
        found = leaks(page)
        check("%s keeps builder text to itself" % path, not found, found)

    # -----------------------------------------------------------------------
    head("THE OWNER OF A COPY ON THEIR OWN MACHINE")
    # -----------------------------------------------------------------------
    # The same server, no longer public and with no sign-in: whoever opens it
    # is whoever runs it, and only they can add a key. The analysis is the
    # one cached for everyone above, which is what carries the hint.
    public_env = {k: _os.environ.pop(k) for k in ("STOCKBOT_PUBLIC_URL",
                                                  "STOCKBOT_BEHIND_PROXY",
                                                  "GOOGLE_CLIENT_ID",
                                                  "GOOGLE_CLIENT_SECRET")}
    try:
        owner = web.app.test_client()
        page = text(owner.get("/analyse?symbol=NVDA&interval=1d"))
        check("the owner is told how to score headlines better",
              "ANTHROPIC_API_KEY" in page and ".env" in page)
        page = text(owner.get("/research?symbol=NVDA&interval=1d"))
        check("and what the reasoning step needs",
              "What the evidence supports" in page and "ANTHROPIC_API_KEY" in page)
        check("and why each piece of research failed", "Max retries" in page)
        page = text(owner.get("/"))
        check("and that the screenshot box needs a key", "Screenshot reading needs "
              "an API key" in page)
    finally:
        _os.environ.update(public_env)

    head("A NUMBER THAT IS NOT ONE")
    # A NaN or an infinity reaching a page read "nan%", "+nan" or "inf".
    odd = [float("nan"), float("inf"), -float("inf"), None, "n/a"]
    check("every figure filter shows a dash for it",
          all(f(v) == "-" for f in (web._price, web._money, web._pct, web._signed)
              for v in odd),
          [(f.__name__, f(v)) for f in (web._price, web._money, web._pct, web._signed)
           for v in odd if f(v) != "-"])
    check("and real figures as before", (web._pct(0.25), web._signed(-0.004),
                                         web._money(1234.5)) == ("25%", "0.00", "1,234.50"))

    head("THE APP STORE TEXT")
    # What the owner pastes into App Store Connect is read by more people than
    # any page here, so it says the counts the way the site does, and offers
    # no download the Account page does not have.
    from bot import export as export_mod
    guide = open(_os.path.join(harness.ROOT, "mobile", "APP_STORE.md"), encoding="utf-8").read()

    def pasted(label):
        """The text in the code block after a **label** line of the guide."""
        after = guide[guide.index("**%s" % label):]
        return after.split("```", 2)[1].strip()

    store = flat(pasted("Promotional Text") + " " + pasted("Description:"))
    check("the promotional text fits App Store Connect's 170 characters",
          len(pasted("Promotional Text")) <= 170, len(pasted("Promotional Text")))
    check("it never says a strategy 'actually worked', or how often a setup "
          "appeared or worked 'before'",
          not [w for w in ("actually worked", "appeared before", "worked before")
               if w in store], store[:300])
    check("it says the counts are tests on past prices",
          "how often this setup came up in tests on past prices" in store
          and "did best in tests on past prices" in store)
    privacy = store[store.index("PRIVACY"):store.index("Orenth is analysis software")]
    said = [s for s in re.split(r"(?<=\.)\s+", privacy) if "download" in s]
    exported = {"journal": "trades", "theses": "theses", "watchlist": "watchlist",
                "alerts": "alerts"}
    named = [w for s in said for w in exported if w in s]
    check("it offers to download, by name, only what the Account page lets "
          "people download",
          said and named and "download them" not in privacy
          and all(exported[w] in export_mod.DATASETS for w in named), (said, named))

    head("OFFLINE")
    check("no page reached for the network", not reached, reached[:5])

finally:
    for m, name, real in REAL:
        setattr(m, name, real)
    for _k, _v in _saved_env.items():
        if _v is None:
            _os.environ.pop(_k, None)
        else:
            _os.environ[_k] = _v
    web.app.config["AUTH_MODE"] = None
    web.app.config["TESTING"] = True

print()
print("%d failure(s)" % len(fails))
print("WORDING OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
