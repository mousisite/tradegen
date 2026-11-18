"""Orenth in a real browser, at the size of a phone.

Every other suite reads the HTML the server sends. This one opens it in Edge,
through Playwright, the way a phone shows it: 360 and 430 pixels wide, three
device pixels to each, touch and no mouse, light and dark, signed out (which
is the example account on its pages), signed in to a free account, and
inside the iPhone app. On every page it measures what a phone shows wrong:

  - a page wider than the screen, which lets the whole page slide sideways;
  - a card of rows that scrolls sideways, or a button or link in a table
    that is a sideways scroll away;
  - a button, field or tab smaller than a finger (44 by 44 pixels);
  - a text field under 16 pixels, which makes an iPhone zoom in on it;
  - a ticker box that does not ask the phone for capitals throughout and
    no corrections, or a box for a price or a size without the decimal
    keypad;
  - a chart shrunk until its labels are too small to read;
  - text fainter than its background allows, in light and in dark;
  - the tab bar along the bottom: its five places, which one is current, and
    nothing of the page left underneath it.

Then it uses the site the way a person does: types a ticker and picks a
suggestion, sets up an alert and pauses, resumes and deletes it, reviews
and logs a paper trade and closes half of it, looks at the prices signed out
and signed in, signs in inside the app through the browser sheet, with the
app's half played by a stand-in, and deletes an account typing the word the
way a phone types it.

The server is this process: web.py on a free local port, on made-up prices,
with every other source a stand-in, and the browser may reach nothing but
it. Without Playwright or a browser (Render's build has neither) it says
SKIP and passes.

    python tests/test_browser.py
"""
import os as _os
import sys as _sys
import threading
import time
import zlib

HERE = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(HERE)
for _path in (ROOT, HERE):
    if _path not in _sys.path:
        _sys.path.insert(0, _path)

# A deployment as orenth.app runs it, minus https: Google and Stripe on, the
# proxy flag that makes it a public site, and nothing that sends mail or
# texts. Set before web is imported, which reads some of it at import.
# APPLE_APP_ID sells Pro in the iPhone app through Apple, where the server
# does that: the app's Pricing is then Apple's purchase screen, and the
# privacy page lists Apple; both are measured like every other page.
SITE_ENV = {"STOCKBOT_BEHIND_PROXY": "1",
            "STOCKBOT_SECRET_KEY": "browser-suite-only-not-a-real-key",
            "GOOGLE_CLIENT_ID": "1234.apps.googleusercontent.com",
            "GOOGLE_CLIENT_SECRET": "stub-secret",
            "STRIPE_SECRET_KEY": "sk_test_stub", "STRIPE_PRICE_ID": "price_month",
            "STRIPE_PRICE_ID_YEARLY": "price_year",
            "STRIPE_WEBHOOK_SECRET": "whsec_stub",
            "APPLE_APP_ID": "6740000001"}
SITE_UNSET = ("STOCKBOT_PUBLIC_URL", "ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_USER",
              "SMTP_PASSWORD", "MAIL_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
              "TWILIO_VERIFY_SID", "SENTRY_DSN", "STOCKBOT_ADMIN_EMAIL",
              "STOCKBOT_CONTACT", "REVIEW_LOGIN_EMAIL", "REVIEW_LOGIN_CODE",
              "APPLE_TEAM_ID", "APPLE_SIGNIN_KEY_ID", "APPLE_SIGNIN_PRIVATE_KEY",
              "APPLE_SERVICES_ID", "FREE_ANALYSES_PER_DAY", "FREE_ALERTS",
              "FREE_FIND_RESULTS")

APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 OrenthApp/1")

DAY = 86400


class Site(object):
    """The running site: its address, its database, and the people in it."""


def build():
    """Set the server up on made-up data. Returns a Site; nothing is served yet."""
    for key, value in SITE_ENV.items():
        _os.environ[key] = value
    for key in SITE_UNSET:
        _os.environ.pop(key, None)

    import harness
    db_path, cfg_path = harness.isolate("browser")

    import numpy as np
    import requests

    import web
    from bot import accounts as A
    from bot import alerts as alerts_mod
    from bot import billing
    from bot import catalysts
    from bot import database as db
    from bot import engine
    from bot import fundamentals
    from bot import ideas as ideas_mod
    from bot import market as market_mod
    from bot import options as options_mod
    from bot import portfolio as portfolio_mod
    from bot import public
    from bot import risk as risk_mod
    from bot import screener as screener_mod
    from bot import sec
    from bot import sentiment as sentiment_mod
    from bot import workflows as workflows_mod
    from bot.market import Bars

    harness._point_web_at(db_path, cfg_path)
    # web.py reads .env when imported, and a developer's copy may hold keys.
    for key in SITE_UNSET:
        _os.environ.pop(key, None)
    web.app.config["AUTH_MODE"] = None
    # Errors drawn as visitors see them, not raised into the test.
    web.app.config["TESTING"] = False
    web.app.config["PROPAGATE_EXCEPTIONS"] = False
    # The stand-ins fail on purpose, and the server says so in its log. Here
    # that is noise around the results, not news.
    import logging
    for name in ("bot", "bot.engine", "werkzeug"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    web.app.logger.disabled = True

    now = int(time.time())

    def bars_for(symbol, interval="1d", lookback=None):
        """A random walk of its own for each symbol, ending at yesterday's close."""
        if symbol == "ZZZZ":
            raise market_mod.SymbolNotFound("no instrument found for 'ZZZZ'. "
                                            "Check the ticker.")
        if symbol == "BOOM":
            raise RuntimeError("the made-up price source broke on purpose")
        seed = zlib.crc32(symbol.encode()) % 10000
        rng = np.random.RandomState(seed)
        n = 1250
        coin = symbol.endswith("-USD")
        vol = 0.03 if coin else 0.018
        start = START.get(symbol, 30000.0 if coin else 50.0 + seed % 400)
        close = start * np.exp(np.cumsum(rng.normal(0.0004, vol, n)))
        prev = np.concatenate([[close[0]], close[:-1]])
        open_ = prev * np.exp(rng.normal(0.0, vol * 0.25, n))
        high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.4, n)))
        low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.4, n)))
        last = (now // DAY - 1) * DAY + 14 * 3600 + 1800
        ts = np.array([last - (n - 1 - k) * DAY for k in range(n)], dtype=np.int64)
        gmt = 0 if coin else -18000
        return Bars(symbol=symbol, name=NAMES.get(symbol, symbol + " Inc"),
                    asset_class="crypto" if coin else "equity", currency="USD",
                    exchange="CCC" if coin else "NasdaqGS", interval="1d",
                    timestamp=ts, open=open_, high=high, low=low, close=close,
                    volume=np.exp(rng.normal(15.0, 0.4, n)),
                    session_id=((ts + gmt) // DAY).astype(np.int64), gmt_offset=gmt,
                    prev_close=float(close[-2]), live_price=None, market_open=False,
                    last_bar_time="2026-11-13 21:00",
                    meta={"regularMarketTime": int(ts[-1])}, dividend_factor=None)

    def news(symbol, name="", limit=25):
        return [sentiment_mod.Item("%s beats estimates and raises guidance" % symbol,
                                   "Example Wire", "news", 2.0),
                sentiment_mod.Item("Analysts cut targets on %s after a weak quarter"
                                   % symbol, "Example Wire", "news", 30.0)]

    def chatter(symbol, limit=30):
        return [sentiment_mod.Item("$%s looks strong into the close" % symbol,
                                   "StockTwits", "social", 1.0, tagged="Bullish")]

    def refused(*_a, **_k):
        raise requests.ConnectionError("this suite is offline")

    def search(query, limit=6):
        typed = (query or "").strip().upper()
        if not typed:
            return []
        out = [{"symbol": s, "name": n, "where": w, "exact": False}
               for s, n, w in SEARCHABLE if s.startswith(typed) or typed in n.upper()]
        return out[:limit]

    def screen(symbols=None, universe="most_actives", filters=None, deep=False,
               limit=40, require_known=True, budget=None):
        found = [screener_mod.Candidate(
            symbol=sym, name=NAMES.get(sym, sym + " Inc"), price=price,
            change_pct=change, volume=12_000_000, market_cap=cap, pe=pe,
            off_high=off, sector="Technology")
            for sym, price, change, cap, pe, off in SCREENED]
        return screener_mod.ScreenResult(
            universe=screener_mod.UNIVERSES.get(universe, "your list"),
            scanned=60, passed=found, rejected=60 - len(found),
            filters=screener_mod._describe(dict(filters or {})), deep=False)

    def scan(market="stocks", horizon="medium", cfg=None, risk="balanced", **_k):
        return dict(IDEAS, scanned_at=time.time() - 300)

    def stripe(method, path, data=None):
        if path.startswith("prices/"):
            return PRICES[path.split("/", 1)[1]]
        if path.startswith("promotion_codes"):
            return {"data": []}
        raise billing.BillingError("Payments could not be reached just now.")

    site = Site()
    site.reached = []

    def no_network(self, method, url, *a, **k):
        site.reached.append("%s %s" % (method, url))
        raise requests.ConnectionError("this suite is offline: %s" % url)

    stubs = [(m, "fetch_bars", bars_for) for m in (market_mod, portfolio_mod, risk_mod,
                                                    options_mod, workflows_mod,
                                                    alerts_mod)]
    stubs += [(engine, "fetch_bars", lambda s, iv: bars_for(s, iv)),
              (engine, "resolve", lambda s: {"symbol": s.strip().upper(),
                                             "name": NAMES.get(s.strip().upper(),
                                                               s.strip().upper() + " Inc")}),
              (market_mod, "split_history", lambda symbol: []),
              (market_mod, "search", search),
              (catalysts, "fetch", lambda symbol: catalysts.Calendar(symbol=symbol)),
              (sentiment_mod, "gather_news", news),
              (sentiment_mod, "gather_social", chatter),
              (fundamentals, "load", refused), (sec, "financial_history", refused),
              (sec, "recent_material", refused), (risk_mod, "profile", refused),
              (options_mod, "load_chain", refused),
              (screener_mod, "run", screen), (ideas_mod, "find", scan),
              (billing, "_stripe", stripe),
              (requests.sessions.Session, "request", no_network)]
    for module, name, fake in stubs:
        setattr(module, name, fake)

    db.migrate(db_path)
    conn = db.connect(db_path)
    try:
        # The popular checks, which /stocks, /stock/<sym> and /record show.
        real_due = public.due
        public.due = lambda conn_, now=None: ["NVDA", "AAPL", "BTC-USD"]
        try:
            public.refresh(conn, web._public_snapshot, pause=0,
                           sleep=lambda _s: None, bars=lambda s: bars_for(s))
        finally:
            public.due = real_due
        free = A.upsert_google_user(conn, {"email": "free@example.com", "sub": "g-free",
                                           "name": "Free Person",
                                           "email_verified": True})
        _seed(db, conn, free.id, now)
        # Built now, so every page of the example account is ready.
        from bot import demo
        demo.user(conn)
    finally:
        conn.close()

    # The front page's example, as the background refresh leaves it.
    web._example_state.update({"busy": True, "at": time.time(), "data": {
        "symbol": "NVDA", "samples": 59, "worked": 25, "fall": True,
        "action": "AVOID", "headline": "Leave this one alone.", "sure": "",
        "price": 156.14, "at": time.time()}})

    site.web, site.db, site.A, site.DB = web, db, A, db_path
    site.free = free
    site.session_cookie = web.app.config.get("SESSION_COOKIE_NAME", "session")
    return site


# Where each made-up price starts, for a coin whose price is nothing like
# Bitcoin's. Everything else starts somewhere between 50 and 450.
START = {"BTC-USD": 30000.0, "ETH-USD": 1800.0, "SHIB-USD": 0.000012}

# Names, so the pages read like the real thing, and some long ones to test
# what a long name does to a narrow screen.
NAMES = {"NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc.", "TSLA": "Tesla, Inc.",
         "MSFT": "Microsoft Corporation", "BTC-USD": "Bitcoin USD",
         "ETH-USD": "Ethereum USD", "SHIB-USD": "Shiba Inu USD",
         "BRK-B": "Berkshire Hathaway Inc. New",
         "GOOGL": "Alphabet Inc. Class A Common Stock",
         "TQQQ": "ProShares UltraPro QQQ (3x leveraged daily)"}
SEARCHABLE = [("NVDA", "NVIDIA Corporation", "NasdaqGS"),
              ("NVDL", "GraniteShares 2x Long NVDA Daily ETF", "NasdaqGM"),
              ("AAPL", "Apple Inc.", "NasdaqGS"), ("TSLA", "Tesla, Inc.", "NasdaqGS"),
              ("MSFT", "Microsoft Corporation", "NasdaqGS"),
              ("BTC-USD", "Bitcoin", "coin"), ("ETH-USD", "Ethereum", "coin"),
              ("BRK-B", "Berkshire Hathaway Inc. New", "NYSE")]
SCREENED = [("NVDA", 181.24, 0.0213, 4.41e12, 52.3, 0.081),
            ("BRK-B", 497.12, -0.0042, 1.07e12, 13.1, 0.034),
            ("GOOGL", 251.61, 0.0007, 3.04e12, 26.9, 0.012),
            ("TQQQ", 98.4, 0.0631, 2.71e10, None, 0.214)]
IDEAS = {"complete": True, "partial": False, "running": False, "interval": "1d",
         "scanned": 148, "asked_for": 150, "avoided": 97, "failed": ["XYZW", "QQQQ"],
         "ranked_as": "how much each trade made on average after costs",
         "summary": "Four setups are firing on instruments where the same setup has "
                    "made money after costs before.",
         "passed": [{"rank": 1, "symbol": "NVDA", "action": "BUY", "price": 181.24,
                     "entry": 181.24, "stop": 171.9, "gap_multiple": 1.4,
                     "probability": 0.56, "samples": 61, "reliable": True,
                     "accounts": "unreadable"},
                    {"rank": 2, "symbol": "BRK-B", "action": "WAIT", "price": 497.12,
                     "entry": 489.5, "stop": 471.25, "gap_multiple": 3.2,
                     "probability": 0.51, "samples": 38, "reliable": True},
                    {"rank": 3, "symbol": "SHIB-USD", "action": "SHORT",
                     "price": 0.00001234, "entry": 0.00001234, "stop": 0.00001377,
                     "gap_multiple": None, "probability": 0.48, "samples": 24,
                     "reliable": False}],
         "watch": [{"symbol": "TQQQ", "price": 98.4, "probability": 0.53, "samples": 44,
                    "conviction": 0.1},
                   {"symbol": "ETH-USD", "price": 3874.55, "probability": 0.5,
                    "samples": 30, "conviction": -0.3}],
         "dropped": []}
PRICES = {"price_month": {"unit_amount": 999, "currency": "usd",
                          "recurring": {"interval": "month", "interval_count": 1}},
          "price_year": {"unit_amount": 7999, "currency": "usd",
                         "recurring": {"interval": "year", "interval_count": 1}}}


# A note typed or pasted without a space, which nothing can break between
# words: it has to wrap inside its card on a phone, not run off it.
LONG_NOTE = "#BTC_weekly_close_above_the_last_cycle_high_would_confirm_the_breakout"
# An address as long as some work addresses are, on the account page.
LONG_EMAIL = ("maximilian.featherstonehaugh.research@lloydsbankinggroup"
              ".example.co.uk")


def _seed(db, conn, user_id, now):
    """A free account with something on every page: trades open and closed,
    an alert that fired and one paused, a watchlist, a plan to review and a
    phone that gets its notifications."""
    nvda = db.open_trade(conn, "NVDA", "1d", 1, 176.5, 168.0, 194.0, quantity=40,
                         risk_amount=340.0, notes="Bought the pullback.",
                         user=user_id)
    db.open_trade(conn, "SHIB-USD", "1d", -1, 0.00001234, 0.00001377, 0.00001011,
                  quantity=12_500_000, user=user_id)
    # A position worth a seven-figure sum: the longest figures a page shows.
    db.open_trade(conn, "BTC-USD", "1d", 1, 64250.0, 58900.0, 79000.0, quantity=21.5,
                  user=user_id)
    closed = db.open_trade(conn, "BRK-B", "1d", 1, 480.0, 465.0, 510.0, quantity=10,
                           user=user_id)
    db.exit_trade(conn, closed, 492.5, 4, "target", user=user_id)
    db.exit_trade(conn, closed, 471.0, None, "stop", user=user_id)
    db.exit_trade(conn, nvda, 181.0, 10, "manual", user=user_id)

    fired = db.alert_add(conn, "AAPL", "price_above", 250.0, note="Breakout level",
                         user=user_id, cap=None)
    db.alert_fire(conn, fired, 251.3, "AAPL rose above 250.00: it is 251.30")
    paused = db.alert_add(conn, "BTC-USD", "price_below", 92000.0, note=LONG_NOTE,
                          user=user_id, cap=None)
    db.alert_set_active(conn, paused, False, user=user_id)
    db.event_add(conn, user_id, "alert", "AAPL", "AAPL rose above 250.00: it is "
                 "251.30", value=251.3, as_of="13 Nov 21:00 UTC", alert_id=fired)
    db.watch_add(conn, "MSFT", "1d", note="Waiting for earnings", user=user_id)
    db.watch_add(conn, "ETH-USD", "1d", note=LONG_NOTE, user=user_id)
    # Settings lists it, with the button to switch it off.
    from bot import push
    push.register(conn, user_id, "apns", "a1" * 32, environment="production",
                  label="iPhone")
    conn.execute(
        "INSERT INTO runs (ts, symbol, name, asset_class, interval, price, "
        "market_open, action, entry, stop, target1, user_id, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now - 120, "TSLA", "Tesla, Inc.", "equity", "1d", 412.5, 0, "BUY", 412.5,
         396.0, 445.0, user_id, "web"))
    conn.commit()


def serve(app):
    """Serve app on a free port of this machine, in a thread. Returns (server, url)."""
    from werkzeug.serving import WSGIRequestHandler, make_server

    class Quiet(WSGIRequestHandler):
        def log(self, *_a, **_k):
            pass

    # Port 0: the system picks a free one, and the server says which.
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.port


def session_token(site):
    conn = site.db.connect(site.DB)
    try:
        return site.A.start_session(conn, site.free.id, "browser suite")
    finally:
        conn.close()


def session_cookie(site, base, token=None):
    """A browser cookie that signs the free account in, as signing in would,
    or whoever the session token given belongs to."""
    web = site.web
    signer = web.app.session_interface.get_signing_serializer(web.app)
    return {"name": site.session_cookie,
            "value": signer.dumps({web.SESSION_KEY: token or session_token(site)}),
            "url": base, "httpOnly": True, "sameSite": "Lax"}


# ---------------------------------------------------------------------------
# What a phone shows wrong, measured in the page.
# ---------------------------------------------------------------------------

# Everything that opens is measured open: an alert's edit form, a figure's
# help, the More menu. width is the screen's, not the page's: a phone lays a
# page that is too wide out wider and lets it slide.
MEASURE = r"""
(width) => {
  const shut = [...document.querySelectorAll('details:not([open])')];
  shut.forEach(d => { d.open = true; });
  try { return measure(); } finally { shut.forEach(d => { d.open = false; }); }

  function measure() {
    const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
      ((typeof el.className === 'string' && el.className.trim())
        ? '.' + el.className.trim().split(/\s+/).join('.') : '');
    const hidden = (el) => {
      if (el.closest('.sr, [hidden], .skip')) return true;
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || s.display === 'none') return true;
      const r = el.getBoundingClientRect();
      return r.width < 1 || r.height < 1;
    };
    // Inside a box that scrolls sideways on its own, as wide tables do.
    const held = (el) => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        if (getComputedStyle(p).overflowX !== 'visible') { return true; }
      }
      return false;
    };
    const out = {wide: document.scrollingElement.scrollWidth, sticking: [], small: [],
                 fonts: [], keys: [], cards: [], away: [], unnamed: [], tiny: [],
                 bar: null};
    if (out.wide > width) {
      for (const el of document.body.querySelectorAll('*')) {
        if (out.sticking.length >= 4 || hidden(el) || held(el)) { continue; }
        const r = el.getBoundingClientRect();
        const up = el.parentElement;
        if (r.right > width + 0.5 && !(up !== document.body &&
            up.getBoundingClientRect().right > width + 0.5)) {
          out.sticking.push(name(el) + ' reaching ' + Math.round(r.right));
        }
      }
    }
    // Buttons, fields, openers and the bars' links: a finger is 44 pixels.
    const controls = 'button, input, select, textarea, summary, a.btn, .tabbar a, ' +
                     '.menu-panel a, .pills a, .jump a, a.me, a.wordmark';
    for (const el of document.querySelectorAll(controls)) {
      if (el.type === 'hidden' || el.type === 'file' || hidden(el)) { continue; }
      const r = el.getBoundingClientRect();
      if (r.width < 43.5 || r.height < 43.5) {
        out.small.push(name(el) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
    }
    // Under 16 pixels, an iPhone zooms in on a field when it is tapped.
    for (const el of document.querySelectorAll('input, select, textarea')) {
      if (el.type === 'hidden' || el.type === 'file' || hidden(el)) { continue; }
      const size = parseFloat(getComputedStyle(el).fontSize);
      if (size < 16) { out.fonts.push(name(el) + ' at ' + size); }
    }
    // A ticker box asks the phone for capitals, no corrections and a Go
    // key; a box for a price or a size asks for the decimal keypad.
    const ticker = {autocapitalize: 'characters', autocomplete: 'off',
                    autocorrect: 'off', spellcheck: 'false', enterkeyhint: 'go'};
    for (const el of document.querySelectorAll('input[name=symbol], input[name=symbols]')) {
      if (el.type === 'hidden' || hidden(el)) { continue; }
      const off = Object.keys(ticker).filter(k => el.getAttribute(k) !== ticker[k]);
      if (off.length) { out.keys.push(name(el) + ' without ' + off.join(', ')); }
    }
    for (const el of document.querySelectorAll(
        'input:is([name=entry], [name=stop], [name=target], [name=quantity], ' +
        '[name=price], [name=trail], [name=threshold])')) {
      if (el.type === 'hidden' || hidden(el)) { continue; }
      if (el.getAttribute('inputmode') !== 'decimal') {
        out.keys.push(name(el) + ' without the decimal keypad');
      }
    }
    // A table of rows to act on is a card per row on a phone, which never
    // scrolls sideways; a table of figures may, but a button or a link in
    // it must not be a sideways scroll away.
    for (const wrap of document.querySelectorAll('.tablewrap')) {
      if (hidden(wrap) || wrap.scrollWidth <= wrap.clientWidth + 1) { continue; }
      const table = wrap.querySelector(':scope > table');
      if (table && table.classList.contains('stack')) {
        out.cards.push(name(table) + ' ' + wrap.scrollWidth + ' wide in ' + wrap.clientWidth);
      }
      const edge = wrap.getBoundingClientRect().right;
      for (const el of wrap.querySelectorAll('button, a, input, select, summary')) {
        const r = el.getBoundingClientRect();
        if (el.type !== 'hidden' && r.width >= 1 && r.right > edge + 1) {
          out.away.push(name(el) + ' "' + el.textContent.trim().slice(0, 20) +
                        '" at ' + Math.round(r.left));
        }
      }
    }
    // In a card of sentences, one to a line, each sentence is under its
    // column's name: every cell after the first, but for the row's buttons.
    for (const td of document.querySelectorAll(
        'table.stack.one > tbody > tr > td:not(:first-child)')) {
      if (hidden(td) || td.querySelector('button, form')) { continue; }
      const said = getComputedStyle(td, '::before').content;
      if (['none', 'normal', '""'].includes(said)) {
        out.unnamed.push(name(td.closest('table')) + ' "' + td.textContent.trim().slice(0, 24) + '"');
      }
    }
    // A chart's labels, as tall as they come out on the screen: a chart
    // drawn wide and shrunk to a phone shrinks its words with it.
    for (const svg of document.querySelectorAll('svg.chart')) {
      if (hidden(svg)) { continue; }
      for (const t of svg.querySelectorAll('text')) {
        const m = t.getScreenCTM();
        const px = parseFloat(getComputedStyle(t).fontSize) * (m ? Math.hypot(m.a, m.b) : 1);
        if (px < 8) {
          out.tiny.push(name(svg) + ' "' + t.textContent.trim() + '" at ' + px.toFixed(1));
          break;
        }
      }
    }
    const bar = document.querySelector('nav.tabbar');
    if (bar && getComputedStyle(bar).display !== 'none') {
      const items = [...bar.querySelectorAll(':scope > a, :scope > details > summary')];
      const menu = document.querySelector('.topbar .menu');
      out.bar = {
        label: bar.getAttribute('aria-label'),
        items: items.map(e => {
          const r = e.getBoundingClientRect();
          return {text: e.textContent.trim(), w: r.width, h: r.height};
        }),
        current: items.filter(e => e.getAttribute('aria-current') === 'page')
                      .map(e => e.textContent.trim()),
        more: !!bar.querySelector(':scope > details > summary.on'),
        menu: !!menu && getComputedStyle(menu).display !== 'none'
      };
    }
    return out;
  }
}
"""

# Text against what is behind it, as WCAG's contrast ratio: 4.5 to 1 at
# least, or 3 to 1 for large text. Colours that change with the theme ease
# in over a fifth of a second, so they are stopped first and read as they
# end up, not half way.
CONTRAST = r"""
() => {
  const still = document.createElement('style');
  still.textContent = '*, *::before, *::after { transition: none !important; ' +
                      'animation: none !important; }';
  document.head.appendChild(still);
  const shut = [...document.querySelectorAll('details:not([open])')];
  shut.forEach(d => { d.open = true; });
  try { return measure(); } finally {
    shut.forEach(d => { d.open = false; });
    still.remove();
  }

  function measure() {
    const rgb = (c) => {
      const m = /rgba?\(([^)]+)\)/.exec(c);
      if (!m) { return null; }
      const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    };
    const over = (top, under) => [0, 1, 2].map(i => top[i] * top[3] + under[i] * (1 - top[3]))
                                          .concat([1]);
    const lum = (c) => {
      const f = (v) => { v /= 255;
                         return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
    };
    const grounds = new Map();
    const ground = (el) => {
      if (!el) { return rgb(getComputedStyle(document.documentElement).backgroundColor)
                        || [255, 255, 255, 1]; }
      if (!grounds.has(el)) {
        const own = rgb(getComputedStyle(el).backgroundColor);
        const under = ground(el.parentElement);
        grounds.set(el, own && own[3] > 0 ? over(own, under) : under);
      }
      return grounds.get(el);
    };
    const low = [];
    const seen = new Set();
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const el = walk.currentNode.parentElement;
      if (!el || seen.has(el) || !walk.currentNode.textContent.trim()) { continue; }
      seen.add(el);
      if (el.closest('.sr, [hidden], .skip, script, style, svg, [disabled]')) { continue; }
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || s.display === 'none') { continue; }
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) { continue; }
      const bg = ground(el);
      const fg = over(rgb(s.color) || [0, 0, 0, 1], bg);
      const a = lum(fg), b = lum(bg);
      const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      const size = parseFloat(s.fontSize);
      const need = size >= 24 || (size >= 18.66 && parseInt(s.fontWeight, 10) >= 700)
                   ? 3 : 4.5;
      if (ratio < need) {
        low.push('"' + walk.currentNode.textContent.trim().slice(0, 30) + '" ' +
                 ratio.toFixed(2) + ' to 1');
      }
    }
    return low;
  }
}
"""

# With a notch and a home bar pretended: the bars clear them, and nothing of
# the page ends up under the tab bar or the fired-alert bar under the notch.
NOTCH = r"""
() => {
  const top = document.querySelector('.topbar');
  const bar = document.querySelector('.tabbar');
  const fire = document.getElementById('firebar');
  const out = {topPad: parseFloat(getComputedStyle(top).paddingTop),
               barPad: parseFloat(getComputedStyle(bar).paddingBottom)};
  window.scrollTo(0, document.scrollingElement.scrollHeight);
  const b = bar.getBoundingClientRect();
  out.barTop = b.top; out.barBottom = b.bottom; out.screen = innerHeight;
  out.lastLine = document.querySelector('footer .legal').getBoundingClientRect().bottom;
  out.topBottom = top.getBoundingClientRect().bottom;
  if (fire) {
    fire.hidden = false;
    window.scrollTo(0, 600);
    out.fireTop = fire.getBoundingClientRect().top;
    fire.hidden = true;
  }
  // The research page's bar of sections, once it has stuck under the top bar.
  const jump = document.getElementById('jump');
  if (jump && !jump.hidden) {
    window.scrollTo(0, jump.getBoundingClientRect().top + scrollY + 900);
    out.jumpTop = jump.getBoundingClientRect().top;
    // And with an alert's bar up as well: the two do not stick in the same
    // place, one over the other.
    if (fire) {
      fire.hidden = false;
      window.scrollTo(0, scrollY + 300);
      const f = fire.getBoundingClientRect(), j = jump.getBoundingClientRect();
      out.bothOverlap = Math.max(0, Math.min(f.bottom, j.bottom) - Math.max(f.top, j.top));
      fire.hidden = true;
    }
  }
  window.scrollTo(0, 0);
  return out;
}
"""

# The longest figure a card might have to hold, in the first figure of the
# portfolio's: it wraps in its column rather than widen the page.
LONG_FIGURE = r"""
(width) => {
  document.querySelector('.figs .f .v').textContent = '-2,551,934,343,331.83';
  return document.scrollingElement.scrollWidth;
}
"""

# The places along the bottom, in order, and the one each page is in. More
# is current, by its colour, on a page it holds; the others by aria-current.
TABS = ["Analyse", "Find", "Journal", "Monitor", "More"]

# (address, who opens it, the tab it is in). who: everyone, a visitor (the
# example account, where it has one), or the free account (in a browser and
# in the app). "-" for the page that stands alone, with no tab bar at all.
PAGES = [
    ("/", "all", "Analyse"),
    ("/analyse?symbol=NVDA&interval=1d", "all", "Analyse"),
    ("/research?symbol=NVDA&interval=1d", "all", "Analyse"),
    ("/ideas", "all", "Find"),
    ("/ideas?market=stocks&horizon=medium&risk=balanced", "all", "Find"),
    ("/screener", "all", "More"),
    ("/screener?preset=quality_value", "all", "More"),
    ("/portfolio", "all", "More"),
    ("/trades", "all", "Journal"),
    ("/trades/simulate?run_id={run}", "member", "Journal"),
    ("/monitor", "all", "Monitor"),
    ("/strategies", "all", "More"),
    ("/settings", "all", "More"),
    ("/account", "member", "More"),
    ("/account", "visitor", None),            # sent to sign in
    ("/signin", "visitor", None),
    ("/pricing", "all", None),
    ("/record", "all", None),
    ("/backtests", "all", None),
    ("/backtest/pin-bar", "all", None),
    ("/stocks", "all", None),
    ("/stock/nvda", "all", None),
    ("{share}", "all", None),
    ("/terms", "all", "More"),
    ("/privacy", "all", "More"),
    ("/offline", "all", "-"),
    ("/no-such-page", "all", None),
    ("/analyse?symbol=BOOM&interval=1d", "member", None),    # the error page
    ("/analyse?symbol=ZZZZ&interval=1d", "visitor", None),   # what the example refuses
]

# The app's half of signing in inside the app: Capacitor saying it is the
# native app, and the bridge's browser sheet, whose answer the test server
# mints. native.js puts its own bridge in place; the sheet stays the
# stand-in's, since the real one opens Apple's sheet and wants https.
FAKE_APP = r"""
(() => {
  const sheet = (url) => window.orenthMintHandoff(url)
    .then(code => 'app.orenth.ios://auth/finish?code=' + encodeURIComponent(code));
  let bridge = {available: true, webAuth: sheet};
  Object.defineProperty(window, 'OrenthNative', {
    configurable: true,
    get() { return bridge; },
    set(v) { bridge = Object.assign({}, v, {webAuth: sheet}); }
  });
  window.Capacitor = {
    isNativePlatform: () => true,
    isPluginAvailable: () => false,
    getPlatform: () => 'ios',
    Plugins: {}
  };
})();
"""


def main():
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

    def skip(why):
        print("SKIP  test_browser: %s. Nothing was checked in a browser." % why)
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                   # not installed, or broken
        return skip("Playwright is not installed here (%s)" % type(exc).__name__)

    started = time.time()
    try:
        driver = sync_playwright().start()
    except Exception as exc:
        return skip("Playwright could not start (%s)" % type(exc).__name__)
    try:
        # The installed Edge first, which is what this machine has; then the
        # Chromium Playwright can download for itself.
        browser, which = None, ""
        for channel in ("msedge", None):
            try:
                browser = (driver.chromium.launch(channel=channel) if channel
                           else driver.chromium.launch())
                which = channel or "chromium"
                break
            except Exception:
                continue
        if browser is None:
            return skip("no browser could be started (Edge, or Playwright's Chromium)")
        try:
            site = build()
            server, base = serve(site.web.app)
            try:
                run(browser, site, base, check, head)
            finally:
                server.shutdown()
        finally:
            browser.close()
    finally:
        driver.stop()

    head("OFFLINE")
    check("the server reached for nothing outside this machine", not site.reached,
          site.reached[:5])
    took = time.time() - started
    print()
    print("%d failure(s) in %.0fs, in %s" % (len(fails), took, which))
    print("BROWSER OK" if not fails else "FAILURES: %s" % fails)
    return 1 if fails else 0


def run(browser, site, base, check, head):
    """Every check, against the site at base."""
    web = site.web

    # A shared call to look at, and the plan the review page reviews.
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = session_token(site)
    shared = client.post("/share", data={"symbol": "NVDA", "interval": "1d"})
    where = shared.headers.get("Location", "")
    share_path = where[where.index("/s/"):] if "/s/" in where else "/s/none"
    conn = site.db.connect(site.DB)
    try:
        run_id = conn.execute("SELECT id FROM runs WHERE user_id=? AND symbol='TSLA'",
                              (site.free.id,)).fetchone()[0]
    finally:
        conn.close()
    blocked = []

    def context(width=390, ua=None, signed_in=False, scheme="light"):
        kw = dict(viewport={"width": width, "height": 800}, device_scale_factor=3,
                  is_mobile=True, has_touch=True, color_scheme=scheme,
                  service_workers="block")
        if ua:
            kw["user_agent"] = ua
        ctx = browser.new_context(**kw)
        ctx.set_default_timeout(15000)

        # This server only: fonts, the visit counter and the rest are refused.
        def gate(route):
            if route.request.url.startswith(base + "/"):
                route.continue_()
            else:
                blocked.append(route.request.url.split("?")[0])
                route.abort()
        ctx.route("**/*", gate)
        if signed_in:
            ctx.add_cookies([session_cookie(site, base)])
        return ctx

    # -----------------------------------------------------------------------
    head("EVERY PAGE AT 360 AND 430 WIDE, LIGHT AND DARK")
    # -----------------------------------------------------------------------
    wide, small, fonts, tabs, moved = [], [], [], [], []
    keys, cards, away, unnamed, tiny, faint = [], [], [], [], [], []
    views, longest = 0, 0
    for who, ua in (("visitor", None), ("free account", None), ("app", APP_UA)):
        member = who != "visitor"
        ctx = context(360, ua=ua, signed_in=member)
        page = ctx.new_page()
        for path, whom, tab in PAGES:
            if whom == ("visitor" if member else "member"):
                continue
            path = path.format(run=run_id, share=share_path)
            page.set_viewport_size({"width": 360, "height": 800})
            page.emulate_media(color_scheme="light")
            page.goto(base + path)
            opened_at = page.evaluate("Math.round(scrollY)")
            if opened_at:
                moved.append("%s %s opened scrolled to %d" % (who, path, opened_at))
            if who == "app" and path == "/":
                check("inside the app the page is marked as the app's",
                      page.evaluate("document.documentElement.classList.contains('app')"))
            for width, scheme in ((360, "light"), (360, "dark"), (430, "dark"),
                                  (430, "light")):
                if width != page.viewport_size["width"]:
                    page.set_viewport_size({"width": width, "height": 800})
                page.emulate_media(color_scheme=scheme)
                m = page.evaluate(MEASURE, width)
                views += 1
                where = "%s %s at %d %s" % (who, path, width, scheme)
                if m["wide"] > width:
                    wide.append("%s is %d wide: %s" % (where, m["wide"],
                                                        ", ".join(m["sticking"])))
                small += ["%s: %s" % (where, s) for s in m["small"]]
                fonts += ["%s: %s" % (where, f) for f in m["fonts"]]
                keys += ["%s: %s" % (where, k) for k in m["keys"]]
                cards += ["%s: %s" % (where, c) for c in m["cards"]]
                away += ["%s: %s" % (where, a) for a in m["away"]]
                unnamed += ["%s: %s" % (where, u) for u in m["unnamed"]]
                tiny += ["%s: %s" % (where, t) for t in m["tiny"]]
                # The colours do not change with the width, only the theme.
                if width == 360:
                    faint += ["%s: %s" % (where, f) for f in page.evaluate(CONTRAST)]
                problem = tab_problem(m["bar"], tab)
                if problem:
                    tabs.append("%s: %s" % (where, problem))
        if who == "free account":
            page.set_viewport_size({"width": 360, "height": 800})
            page.goto(base + "/portfolio")
            longest = page.evaluate(LONG_FIGURE, 360)
        ctx.close()
    check("no page opens scrolled away from its top", not moved, moved[:5])
    check("no page is wider than the screen (%d page views)" % views, not wide, wide[:5])
    check("a figure as long as -2,551,934,343,331.83 wraps in its column instead "
          "of widening the page", longest <= 360, longest)
    check("no card of rows scrolls sideways, even with a note typed without a space",
          not cards, cards[:5])
    check("no button or link in a table is a sideways scroll away", not away, away[:5])
    check("in a card of sentences, each one is under its column's name (the privacy "
          "page's list of where data goes, every row of it)", not unnamed, unnamed[:5])
    check("every button, field, opener and tab is at least 44 by 44", not small, small[:5])
    check("no field's text is under 16 pixels on a touch screen", not fonts, fonts[:5])
    check("every ticker box asks for capitals, no corrections and a Go key, and every "
          "price and size box for the decimal keypad", not keys, keys[:5])
    check("no chart is shrunk until its labels are under 8 pixels", not tiny, tiny[:5])
    check("no text is fainter than 4.5 to 1 against its background (3 to 1 if large), "
          "light or dark", not faint, faint[:5])
    check("the tab bar is there with its five places, 44 or more each, the right one "
          "current, and no Menu above it", not tabs, tabs[:5])

    # -----------------------------------------------------------------------
    head("A NOTCH AND A HOME BAR")
    # -----------------------------------------------------------------------
    ctx = context(390, signed_in=True)
    page = ctx.new_page()
    try:
        ctx.new_cdp_session(page).send(
            "Emulation.setSafeAreaInsetsOverride",
            {"insets": {"top": 47, "bottom": 34, "left": 0, "right": 0}})
        pretended = True
    except Exception as exc:
        print("   --   this browser cannot pretend to have a notch (%s)"
              % type(exc).__name__)
        pretended = False
    if pretended:
        clear, under, fired, jumped, both = [], [], [], [], []
        for path in ("/monitor", "/trades", "/privacy", "/analyse?symbol=NVDA&interval=1d",
                     "/research?symbol=NVDA&interval=1d"):
            page.goto(base + path)
            n = page.evaluate(NOTCH)
            if n["topPad"] != 47 or n["barPad"] != 34 or n["barBottom"] != n["screen"]:
                clear.append((path, n))
            if n["lastLine"] > n["barTop"] + 0.5:
                under.append((path, round(n["lastLine"]), round(n["barTop"])))
            if n.get("fireTop") is not None and n["fireTop"] < n["topBottom"] - 1:
                fired.append((path, n["fireTop"], n["topBottom"]))
            if n.get("jumpTop") is not None and n["jumpTop"] < n["topBottom"] - 1:
                jumped.append((path, n["jumpTop"], n["topBottom"]))
            if n.get("bothOverlap"):
                both.append((path, round(n["bothOverlap"])))
        check("the top bar clears the notch and the tab bar the home bar", not clear,
              clear[:2])
        check("scrolled to the end, the page's last line is above the tab bar",
              not under, under[:3])
        check("a fired alert's bar sits under the top bar, not under the notch",
              not fired, fired[:3])
        check("and so does the research page's bar of sections, once it sticks",
              not jumped, jumped[:3])
        check("with an alert's bar up too, the two do not stick one over the other",
              not both, both[:3])
    ctx.close()

    # -----------------------------------------------------------------------
    head("A TICKER, TYPED")
    # -----------------------------------------------------------------------
    ctx = context(390, signed_in=True)
    page = ctx.new_page()
    try:
        page.goto(base + "/")
        box = page.locator("#symbol")
        box.tap()
        check("the tab bar steps aside while a box is typed in",
              page.evaluate("getComputedStyle(document.querySelector('.tabbar')).visibility")
              == "hidden")
        asked = page.evaluate("""(() => { const b = document.getElementById('symbol');
            return ['autocapitalize', 'autocomplete', 'autocorrect', 'spellcheck',
                    'enterkeyhint'].map(k => b.getAttribute(k)); })()""")
        check("the box asks the phone for capitals, no corrections or suggestions, "
              "and a Go key", asked == ["characters", "off", "off", "false", "go"], asked)
        box.press_sequentially("NV", delay=40)
        option = page.locator("ul.suggest li", has_text="NVIDIA Corporation")
        option.wait_for(state="visible")
        shown = page.locator("ul.suggest li b").all_inner_texts()
        check("typing NV offers NVDA under the box", "NVDA" in shown, shown)
        spot = option.bounding_box()
        check("and the suggestion fits on the screen and a finger",
              spot["x"] >= 0 and spot["x"] + spot["width"] <= 390 and spot["height"] >= 44,
              spot)
        option.tap()
        page.wait_for_url(lambda url: "/analyse?" in url and "symbol=NVDA" in url)
        check("tapping it opens NVDA's analysis",
              page.locator("h1.sym").inner_text().strip() == "NVDA", page.url)
        check("with Analyse current in the tab bar",
              page.locator(".tabbar a[aria-current='page']").inner_text().strip()
              == "Analyse")
    except Exception as exc:
        check("typing a ticker and opening its analysis", False, exc)
    ctx.close()

    # -----------------------------------------------------------------------
    head("AN ALERT: SET, PAUSED, RESUMED, DELETED")
    # -----------------------------------------------------------------------
    ctx = context(390, signed_in=True)
    page = ctx.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(base + "/monitor")
        page.locator("#a-symbol").tap()
        page.locator("#a-symbol").press_sequentially("MSFT", delay=20)
        page.keyboard.press("Escape")
        page.select_option("#a-kind", "price_above")
        page.locator("#a-threshold").fill("450")
        check("the value box asks for the decimal keypad",
              page.get_attribute("#a-threshold", "inputmode") == "decimal")
        with page.expect_navigation():
            page.get_by_role("button", name="Add alert").tap()
        row = page.locator("tr", has=page.locator("form[action$='/monitor/alert/delete']")
                           ).filter(has_text="MSFT")
        check("the alert is listed, watching", row.count() == 1
              and "Watching" in row.inner_text(), page.url)
        acts = row.get_by_role("button", name="Pause").bounding_box()
        check("and its buttons are on the screen, not a sideways scroll away",
              acts["x"] + acts["width"] <= 390, acts)
        with page.expect_navigation():
            row.get_by_role("button", name="Pause").tap()
        check("Pause pauses it", "Paused" in row.inner_text())
        with page.expect_navigation():
            row.get_by_role("button", name="Resume").tap()
        check("Resume sets it watching again", "Watching" in row.inner_text())
        with page.expect_navigation():
            row.get_by_role("button", name="Delete").tap()
        check("Delete, confirmed, removes it", row.count() == 0)
    except Exception as exc:
        check("setting, pausing, resuming and deleting an alert", False, exc)
    ctx.close()

    # -----------------------------------------------------------------------
    head("A PAPER TRADE: REVIEWED, LOGGED, HALF CLOSED")
    # -----------------------------------------------------------------------
    ctx = context(390, signed_in=True)
    page = ctx.new_page()
    try:
        page.goto(base + "/trades/simulate?run_id=%d" % run_id)
        said = page.locator("main").inner_text()
        check("the review shows the plan: TSLA long at 412.5, stop 396, 6 units",
              "TSLA" in said and "412.5" in said and "396" in said and "6 units" in said,
              said[:300])
        with page.expect_navigation():
            page.get_by_role("button", name="Confirm paper trade").tap()
        check("Confirm logs it in the journal and says so",
              "/trades" in page.url and "Logged as a paper trade"
              in page.locator(".saved").first.inner_text(), page.url)
        row = page.locator("tr", has=page.locator("form[action$='/trades/close']")
                           ).filter(has_text="TSLA")
        check("it is under Open", row.count() == 1)
        close = row.get_by_role("button", name="Close")
        spot = close.bounding_box()
        check("its Close button is on the screen", spot["x"] + spot["width"] <= 390, spot)
        check("the size and price boxes ask for the decimal keypad",
              row.locator("input[name=quantity]").get_attribute("inputmode") == "decimal"
              and row.locator("input[name=price]").get_attribute("inputmode") == "decimal")
        row.locator("input[name=quantity]").fill("3")
        row.locator("input[name=price]").fill("420")
        row.locator("select[name=reason]").select_option("target")
        with page.expect_navigation():
            close.tap()
        said = page.locator(".saved").first.inner_text()
        check("closing 3 of 6 at 420 is recorded, with 3 still open",
              "Recorded: 3 of TSLA closed at 420" in said and "3 of it is still open" in said,
              said)
        check("and the trade shows 3 of 6 still open",
              "3 of 6 still open" in row.inner_text(), row.inner_text()[:200])
    except Exception as exc:
        check("reviewing, logging and closing part of a paper trade", False, exc)
    ctx.close()

    # -----------------------------------------------------------------------
    head("THE PRICES, SIGNED OUT AND SIGNED IN")
    # -----------------------------------------------------------------------
    for member in (False, True):
        ctx = context(390, signed_in=member)
        page = ctx.new_page()
        try:
            page.goto(base + "/pricing")
            said = page.locator("main").inner_text()
            check("%s: Pro is $9.99 a month or $79.99 a year"
                  % ("signed in" if member else "signed out"),
                  "$9.99 a month" in said and "$79.99 a year" in said, said[:200])
            if not member:
                check("signed out: the way on is an account, and no code box",
                      page.get_by_role("link", name="Create account to upgrade").is_visible()
                      and page.locator("#code").count() == 0)
            else:
                check("signed in: Upgrade to Pro and the code box",
                      page.get_by_role("button", name="Upgrade to Pro").is_visible()
                      and page.locator("#code").is_visible())
                page.locator("#code").fill("FRIEND10")
                address = page.url
                page.get_by_role("button", name="Apply").tap()
                answer = page.locator(".codesaid")
                answer.wait_for(state="visible")
                check("Apply answers under the box, without leaving the page",
                      page.url == address and answer.inner_text().strip() != "",
                      (page.url, answer.inner_text()))
        except Exception as exc:
            check("the pricing page %s" % ("signed in" if member else "signed out"),
                  False, exc)
        ctx.close()

    # -----------------------------------------------------------------------
    head("SIGNING IN INSIDE THE APP")
    # -----------------------------------------------------------------------
    minted = []

    def mint(source, url):
        """The sheet's answer: Google said yes, and the site hands a code back."""
        from urllib.parse import parse_qs, urlparse
        asked = parse_qs(urlparse(url).query)
        conn = site.db.connect(site.DB)
        try:
            code = site.A.new_handoff(conn, site.free.id, asked.get("challenge", [""])[0],
                                      asked.get("next", [""])[0])
        finally:
            conn.close()
        minted.append(url)
        return code

    ctx = context(390, ua=APP_UA)
    ctx.add_init_script(FAKE_APP)
    ctx.expose_binding("orenthMintHandoff", mint)
    page = ctx.new_page()
    try:
        page.goto(base + "/signin")
        check("the app's bridge is in place", page.evaluate(
            "!!(window.OrenthNative && window.OrenthNative.available "
            "&& window.OrenthNative.appStarted)"))
        with page.expect_navigation(url=lambda url: url.rstrip("/") == base):
            page.locator("a.gbtn[data-app-via=google]").tap()
        check("Continue with Google opened the sheet with the app's check",
              len(minted) == 1 and "app=1" in minted[0] and "challenge=" in minted[0],
              minted)
        check("and the code it handed back signed the app in, on the front page",
              page.locator("a.me").count() == 1
              and "Free Person" in page.locator("a.me").inner_text(), page.url)
        page.goto(base + "/account")
        check("the account page opens, as that account",
              "free@example.com" in page.locator("main").inner_text())
    except Exception as exc:
        check("signing in inside the app", False, exc)
    ctx.close()

    # -----------------------------------------------------------------------
    head("LEAVING, ON A PHONE")
    # -----------------------------------------------------------------------
    # An account of its own, with a long address, so the others stay. The
    # word is typed as an iPhone types it unless told otherwise: a capital
    # first, and a space after a word picked from the suggestions.
    conn = site.db.connect(site.DB)
    try:
        leaver = site.A.upsert_google_user(conn, {"email": LONG_EMAIL, "sub": "g-leaver",
                                                  "name": "Leaving Person",
                                                  "email_verified": True})
        token = site.A.start_session(conn, leaver.id, "browser suite")
    finally:
        conn.close()
    ctx = context(360)
    ctx.add_cookies([session_cookie(site, base, token)])
    page = ctx.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(base + "/account")
        across = page.evaluate("document.scrollingElement.scrollWidth")
        check("a long email address wraps on the account page instead of widening it",
              across <= 360, across)
        box = page.locator("#confirm")
        asked = [box.get_attribute(k) for k in ("autocapitalize", "autocorrect",
                                                "spellcheck")]
        check("the box for the word delete asks for no capitals and no corrections",
              asked == ["none", "off", "false"], asked)
        box.tap()
        box.press_sequentially("Delete ", delay=20)
        with page.expect_navigation():
            page.get_by_role("button", name="Delete my account").tap()
        conn = site.db.connect(site.DB)
        try:
            gone = site.A.get_user(conn, leaver.id) is None
        finally:
            conn.close()
        check("typed as Delete with a space after it, the account is deleted",
              gone and "/signin" in page.url, page.url)
    except Exception as exc:
        check("deleting an account on a phone", False, exc)
    ctx.close()

    print()
    print("   (refused %d requests for other sites: %s)"
          % (len(blocked), ", ".join(sorted(set(u.split("/")[2] for u in blocked
                                                 if "//" in u)))))


def tab_problem(bar, want):
    """What is wrong with the tab bar on a page that should be in tab want."""
    if want == "-":
        return "a tab bar on a page that stands alone" if bar else None
    if bar is None:
        return "no tab bar"
    if bar["label"] != "Sections":
        return "labelled %r" % bar["label"]
    names = [item["text"] for item in bar["items"]]
    if names != TABS:
        return "places %s" % names
    small = [i for i in bar["items"] if i["w"] < 44 or i["h"] < 44]
    if small:
        return "small: %s" % small
    if bar["menu"]:
        return "Menu still in the top bar"
    if want == "More":
        right = bar["current"] == [] and bar["more"]
    else:
        right = bar["current"] == ([want] if want else []) and not bar["more"]
    if not right:
        return "current %s, More %s, wanted %s" % (bar["current"], bar["more"], want)
    return None


if __name__ == "__main__":
    _sys.exit(main())
