"""What changed on a watchlist since the session before: the daily calls the
background job writes down, the list the front page and the weekly email
make of them, and what that job must never do (spend a free account's looks,
analyse for a page view, or hand the free plan calls it does not check).

No network: prices, analyses and the popular checks are stand-ins.
"""
import os as _os
import re
import sqlite3
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
                                        "MAIL_POSTAL_ADDRESS", "ANTHROPIC_API_KEY"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402
from bot import digest  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot import public as public_mod  # noqa: E402

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


def flat(html):
    return " ".join(html.split())


c, DB = harness.isolated_web("since")
web._example_state["busy"] = True          # no example analysis in the background
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"
NOW = int(time.time())
DAY = 86400
TODAY = NOW // DAY


def person(email, seen=NOW):
    conn = db.connect(DB)
    try:
        who = A.upsert_email_user(conn, email)
        conn.execute("UPDATE users SET last_seen_ts=? WHERE id=?", (int(seen), who.id))
        conn.commit()
        return A.get_user(conn, who.id)
    finally:
        conn.close()


def watch(who, *symbols):
    conn = db.connect(DB)
    try:
        for sym in symbols:
            db.watch_add(conn, sym, "1d", user=who.id)
    finally:
        conn.close()


def sign_in(user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with c.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


def looks(who):
    conn = db.connect(DB)
    try:
        return billing.views_today(conn, who.id)
    finally:
        conn.close()


def rows_of(symbol):
    conn = db.connect(DB)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM watch_calls WHERE symbol=? ORDER BY day DESC", (symbol,))]
    finally:
        conn.close()


# Anything that analyses. Drawing a page must never reach it.
started = []


def must_not_run(what):
    def refuse(*_a, **_k):
        started.append(what)
        raise AssertionError("%s was started" % what)
    return refuse


real = {"analyse": web.engine.analyse, "cached": web.cached_analysis,
        "bars": market_mod.fetch_bars}
web.engine.analyse = must_not_run("an analysis")
web.cached_analysis = must_not_run("an analysis")
market_mod.fetch_bars = must_not_run("a price download")

try:
    head("WHERE YESTERDAY'S CALL IS KEPT")
    conn = db.connect(DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(watch_calls)")]
    check("a table of its own, holding symbols, calls and prices only",
          cols == ["symbol", "day", "action", "price", "prev_close", "at"], cols)
    conn.close()
    db.migrate(DB)
    db.migrate(DB)
    check("making it again changes nothing", rows_of("ZZX") == [])
    raw = sqlite3.connect(DB)
    raw.execute("DROP TABLE watch_calls")
    raw.commit()
    raw.close()
    db.migrate(DB)
    check("a database from before it gets it at start-up, nothing else touched",
          rows_of("ZZX") == [] and "watch_calls" in {r[0] for r in sqlite3.connect(DB).execute(
              "SELECT name FROM sqlite_master WHERE type='table'")})
    conn = db.connect(DB)
    db.watch_call_save(conn, "zzx", TODAY, "WAIT", 10.0, 9.5, NOW - 3600)
    db.watch_call_save(conn, "ZZX", TODAY, None, 10.4, 9.5, NOW)
    conn.close()
    got = rows_of("ZZX")
    check("a later check in the same session keeps the session's call when it read "
          "only the price", len(got) == 1 and got[0]["action"] == "WAIT"
          and got[0]["price"] == 10.4 and got[0]["at"] == NOW, got)
    conn = db.connect(DB)
    db.watch_call_save(conn, "ZZX", TODAY, "BUY", 10.6, 9.5, NOW)
    db.watch_call_save(conn, "ZZX", TODAY - 1, "AVOID", 9.5, 9.0, NOW - DAY)
    conn.close()
    got = rows_of("ZZX")
    check("a new call replaces it, and each session keeps its own row",
          [(r["day"], r["action"]) for r in got] == [(TODAY, "BUY"), (TODAY - 1, "AVOID")], got)
    conn = db.connect(DB)
    db.watch_call_save(conn, "ZZOLD", TODAY - 30, "WAIT", 1.0, 1.0, NOW - 30 * DAY)
    gone = db.watch_calls_prune(conn, NOW - digest.KEEP_DAYS * DAY)
    conn.execute("DELETE FROM watch_calls")
    conn.commit()
    conn.close()
    check("old checks are let go of", gone == 1 and rows_of("ZZOLD") == [], gone)

    head("WHAT THE BACKGROUND JOB CHECKS")
    _os.environ.update(STRIPE)               # billing on: the free plan has limits
    free = person("free@example.com")
    pro = person("pro@example.com")
    gone_away = person("gone@example.com", seen=NOW - 60 * DAY)
    reader = person("reader@example.com", seen=NOW - 60 * DAY)
    conn = db.connect(DB)
    billing.set_plan(conn, pro.id, "pro", "active", "cus_pro")
    digest.set_wanted(conn, reader.id, True)
    conn.close()
    watch(free, "NVDA", "ZZFREE", "ZZBOTH")
    watch(pro, "ZZBOTH", "ZZPRO", "TSLA")
    watch(gone_away, "ZZGONE")
    watch(reader, "ZZMAIL")
    many = person("many@example.com")
    watch(many, *["ZZQ%d" % k for k in range(10)])
    full = web._full_plan()
    conn = db.connect(DB)
    wanted = digest.watched(conn, full, NOW)
    conn.close()
    check("a popular stock's call is always wanted: its public check costs nothing more",
          wanted.get("NVDA") is True and wanted.get("TSLA") is True, wanted)
    check("on a free account, any other is read for its price only",
          wanted.get("ZZFREE") is False, wanted)
    check("on Pro, its call is checked too", wanted.get("ZZPRO") is True, wanted)
    check("one watched on both plans is checked for the Pro account",
          wanted.get("ZZBOTH") is True, wanted)
    check("an account nobody has opened for a month is left alone", "ZZGONE" not in wanted)
    check("one that takes the weekly email is not", "ZZMAIL" in wanted)
    check("only the first eight on a list, the ones the page and the email show",
          [s for s in wanted if s.startswith("ZZQ")] == ["ZZQ%d" % k for k in range(8)],
          sorted(wanted))
    conn = db.connect(DB)
    demo = web.demo_mod.user(conn)
    conn.close()
    # Seeding the example asks for prices and falls back to its own; that
    # is not a page, and not what the checks below are about.
    del started[:]
    watch(demo, "ZZDEMO")
    conn = db.connect(DB)
    check("nor the example account's", "ZZDEMO" not in digest.watched(conn, full, NOW))
    conn.close()

    asked = []

    def fake_check(symbol, with_call):
        asked.append((symbol, with_call))
        if symbol == "ZZBROKEN":
            raise market_mod.DataError("no data for ZZBROKEN")
        return {"action": "WAIT" if with_call else None, "day": TODAY,
                "price": 100.0, "prev_close": 98.0}

    watch(free, "ZZBROKEN")
    conn = db.connect(DB)
    out = digest.check_watched(conn, fake_check, full, now=NOW, limit=5, sleep=lambda s: None)
    conn.close()
    check("a run checks no more than its share, and one bad ticker does not stop it",
          [s for s, _ in asked] == ["NVDA", "TSLA", "ZZBOTH", "ZZBROKEN", "ZZFREE"]
          and out["checked"] == 4 and out["errors"] == ["ZZBROKEN: no data for ZZBROKEN"],
          (asked, out))
    conn = db.connect(DB)
    total = len(digest.watched(conn, full, NOW))
    out = digest.check_watched(conn, fake_check, full, now=NOW, limit=100,
                               sleep=lambda s: None)
    conn.close()
    check("the rest wait for the next run, and the one that failed is tried again",
          len(asked) == total + 1 and out["checked"] == total - 5
          and asked[5:].count(("ZZBROKEN", False)) == 1, (len(asked), total, out))
    check("asked for the call only where it is wanted",
          ("ZZFREE", False) in asked and ("ZZPRO", True) in asked
          and ("NVDA", True) in asked, asked)
    check("and nobody's free looks were spent", looks(free) == 0 and looks(many) == 0,
          (looks(free), looks(many)))
    check("the free account's instrument is kept with its price and no call",
          rows_of("ZZFREE")[0]["action"] is None and rows_of("ZZFREE")[0]["price"] == 100.0)
    del asked[:]
    conn = db.connect(DB)
    digest.check_watched(conn, fake_check, full, now=NOW + 3600, sleep=lambda s: None)
    conn.close()
    check("an hour later only the one that failed is asked again",
          asked == [("ZZBROKEN", False)], asked)
    del asked[:]
    conn = db.connect(DB)
    digest.check_watched(conn, fake_check, full, now=NOW + digest.CHECK_EVERY,
                         sleep=lambda s: None)
    conn.close()
    check("a few hours on, everything is checked again", len(asked) == total, len(asked))
    watch(pro, "ZZFREE")
    del asked[:]
    conn = db.connect(DB)
    digest.check_watched(conn, fake_check, full, now=NOW + digest.CHECK_EVERY + 60,
                         sleep=lambda s: None)
    conn.close()
    check("an instrument that gains a Pro watcher has its call checked at once",
          asked == [("ZZBROKEN", False), ("ZZFREE", True)]
          or asked == [("ZZFREE", True), ("ZZBROKEN", False)], asked)
    conn = db.connect(DB)
    db.watch_remove(conn, "ZZFREE", user=pro.id)
    conn.close()

    def throttled(symbol, with_call):
        asked.append(symbol)
        raise market_mod.DataThrottled("The market data source limits how often ...", 60)

    del asked[:]
    conn = db.connect(DB)
    out = digest.check_watched(conn, throttled, full, now=NOW + 3 * digest.CHECK_EVERY,
                               sleep=lambda s: None)
    conn.close()
    check("a source that says stop ends the run at once", len(asked) == 1
          and len(out["errors"]) == 1, (asked, out))

    head("THE CHECK ITSELF")
    conn = db.connect(DB)
    public_mod.save_verdict(conn, {"symbol": "NVDA", "name": "NVIDIA", "action": "BUY",
                                   "samples": 50, "worked": 30, "price": 180.0,
                                   "price_day": TODAY, "at": NOW - 600})
    conn.close()
    QUOTE = NOW - 120

    def fake_bars(symbol, interval="1d", lookback=None):
        fetched.append((symbol, interval, lookback))
        return NS(symbol=symbol, last_price=182.0, prev_close=176.0, gmt_offset=0,
                  meta={"regularMarketTime": QUOTE}, session_id=None)

    fetched, analysed = [], []

    def fake_analyse(symbol, cfg, interval=None, **kw):
        analysed.append(dict(kw, symbol=symbol, interval=interval))
        return NS(plan=NS(action="SHORT"), bars=fake_bars(symbol))

    market_mod.fetch_bars = fake_bars
    web.engine.analyse = fake_analyse
    try:
        got = web._watch_check("NVDA", True)
        check("a popular stock takes its call from the public check, analysing nothing",
              got == {"action": "BUY", "day": QUOTE // DAY, "price": 182.0,
                      "prev_close": 176.0} and not analysed, (got, analysed))
        check("and reads its price from a year of daily bars",
              fetched == [("NVDA", "1d", "1y")], fetched)
        del fetched[:]
        got = web._watch_check("ZZPRO", True)
        check("any other is analysed on daily bars with the standard settings",
              got["action"] == "SHORT" and analysed
              and analysed[0]["interval"] == "1d", (got, analysed))
        check("recorded in nobody's history, with no news and no calendar",
              analysed[0]["record"] is False and analysed[0]["with_news"] is False
              and analysed[0]["with_calendar"] is False, analysed)
        del analysed[:]
        got = web._watch_check("ZZFREE", False)
        check("one whose call is not wanted has its price read, and nothing analysed",
              got["action"] is None and got["price"] == 182.0 and not analysed, analysed)
        conn = db.connect(DB)
        public_mod.save_verdict(conn, {"symbol": "TSLA", "name": "Tesla", "action": "WAIT",
                                       "price": 300.0, "at": NOW - 2 * DAY})
        public_mod.save_verdict(conn, {"symbol": "BTC-USD", "name": "Bitcoin",
                                       "action": "AVOID", "price": 90000.0, "at": NOW - 600})
        conn.close()
        check("a popular check two days old is not passed off as today's call",
              web._watch_check("TSLA", True)["action"] is None)
        del fetched[:]
        got = web._watch_check("btc", True)
        check("an entry kept from before tickers were tidied is checked as what it means",
              got["action"] == "AVOID" and fetched and fetched[0][0] == "BTC-USD"
              and not analysed, (got, fetched))
        conn = db.connect(DB)
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        check("no run was written to anyone's history", runs == 0, runs)

        # The hourly job: the popular checks, then the watched instruments.
        # Everything checked just now but one, so it runs a single check.
        conn = db.connect(DB)
        conn.execute("UPDATE watch_calls SET at=?", (int(time.time()),))
        conn.execute("DELETE FROM watch_calls WHERE symbol='ZZPRO'")
        conn.commit()
        db.watch_call_save(conn, "ZZBROKEN", TODAY, None, 1.0, 1.0, int(time.time()))
        conn.close()
        real_refresh = public_mod.refresh
        public_mod.refresh = lambda conn, analyse, **kw: {"checked": 2, "errors": ["X: down"]}
        try:
            done = web._refresh_popular_job()
            check("the hourly popular job checks the watched instruments too",
                  done["checked"] == 3 and "X: down" in done["errors"]
                  and rows_of("ZZPRO") and rows_of("ZZPRO")[0]["action"] == "SHORT",
                  (done, rows_of("ZZPRO")))
            real_check = digest.check_watched

            def broken(*a, **k):
                raise RuntimeError("database is locked")

            digest.check_watched = broken
            web.app.logger.disabled = True
            try:
                done = web._refresh_popular_job()
            finally:
                digest.check_watched = real_check
                web.app.logger.disabled = False
            check("and its failure leaves the popular run's result standing",
                  done["checked"] == 2 and any("watched" in e for e in done["errors"]), done)
        finally:
            public_mod.refresh = real_refresh
    finally:
        market_mod.fetch_bars = must_not_run("a price download")
        web.engine.analyse = must_not_run("an analysis")

    head("WHAT CHANGED, FOR ONE PERSON")
    conn = db.connect(DB)
    conn.execute("DELETE FROM watch_calls")
    conn.execute("DELETE FROM watchlist")
    conn.commit()
    conn.close()
    ana = person("ana@example.com")
    conn = db.connect(DB)
    billing.set_plan(conn, ana.id, "pro", "active", "cus_ana")
    conn.close()
    watch(ana, "NVDA", "ZZUP", "ZZCALM", "ZZFLAT", "ZZNEW", "ZZOLD", "ZZALERT")
    conn = db.connect(DB)
    for sym, today, before, gap in (("NVDA", ("BUY", 183.0, 178.0), ("WAIT", 178.0, 175.0), 1),
                                    ("ZZUP", ("WAIT", 51.5, 50.0), ("WAIT", 50.0, 50.5), 3),
                                    ("ZZCALM", ("AVOID", 20.1, 20.0), ("AVOID", 20.0, 20.2), 1),
                                    ("ZZFLAT", ("WAIT", 30.0, 30.1), None, 1),
                                    ("ZZOLD", ("BUY", 10.0, 9.99), ("AVOID", 9.0, 9.0), 6),
                                    ("ZZALERT", ("WAIT", 5.0, 5.01), ("WAIT", 5.0, 5.0), 1)):
        db.watch_call_save(conn, sym, TODAY, today[0], today[1], today[2], NOW - 1800)
        if before:
            db.watch_call_save(conn, sym, TODAY - gap, before[0], before[1], before[2],
                               NOW - gap * DAY)
    db.event_add(conn, ana.id, "price_above", "ZZALERT", "ZZALERT rose above 5.00, at 5.02.",
                 5.02, "16 Nov 15:00 UTC")
    db.event_add(conn, ana.id, "stop_breach", "ZZALERT", "ZZALERT is at 5.00, past the stop "
                 "of 5.10 on your short position.", 5.0, "16 Nov 15:00 UTC", trade_id=1)
    db.event_add(conn, ana.id, "split", "ZZCALM", "ZZCALM split 2-for-1. Your alert's "
                 "level moved.", 1.0, "")
    old = db.event_add(conn, ana.id, "price_below", "ZZCALM", "ZZCALM fell below 21.", 20.0, "")
    conn.execute("UPDATE alert_events SET ts=? WHERE id=?", (NOW - 2 * DAY, old))
    db.event_add(conn, free.id, "price_above", "ZZCALM", "Someone else's alert.", 1.0, "")
    db.event_add(conn, ana.id, "price_above", "ZZELSE", "Not on the watchlist.", 1.0, "")
    conn.commit()
    said = digest.since_yesterday(conn, ana.id, True, NOW)
    conn.close()
    rows = {r["symbol"]: r for r in said["rows"]}
    check("a changed call is news", rows["NVDA"]["changed"] and rows["NVDA"]["was"] == "WAIT"
          and rows["NVDA"]["call"] == "BUY", rows.get("NVDA"))
    check("with the move since the previous close", abs(rows["NVDA"]["move"] - (183 / 178 - 1)) < 1e-9)
    check("so is a move of 2% or more with the call unchanged, across a weekend",
          "ZZUP" in rows and not rows["ZZUP"]["changed"] and rows["ZZUP"]["was"] == "WAIT")
    check("alerts and crossed stops of the last day are news, the split note is not",
          rows["ZZALERT"]["alerts"] == ["ZZALERT rose above 5.00, at 5.02."]
          and len(rows["ZZALERT"]["stops"]) == 1, rows.get("ZZALERT"))
    check("a call a week older is not compared with: a check was missed",
          "ZZOLD" not in rows or rows["ZZOLD"]["was"] is None, rows.get("ZZOLD"))
    check("nothing else is listed", set(rows) == {"NVDA", "ZZUP", "ZZALERT"}, sorted(rows))
    check("the calm ones are counted, and how many kept their call",
          said["quiet"] == 3 and said["kept"] == 1, said)
    check("so is one with no check yet", said["unchecked"] == 1, said)
    check("no other account's alert, no older one, nothing off the watchlist",
          not any("Someone else" in m or "fell below" in m or "Not on the" in m
                  for r in said["rows"] for m in r["alerts"]))
    lines = digest.notes(said)
    check("the rest is said in plain sentences",
          lines[0] == "The other 3 moved less than 2% since the previous close, with "
          "no alert or crossed stop." and lines[1] == "One instrument has no price to "
          "compare with yet.", lines)
    check("an email line reads like one", digest.change_line(rows["NVDA"])
          == "The daily call went from WAIT to BUY; up 2.81% since the previous close.",
          digest.change_line(rows["NVDA"]))
    check("a price that did not move is not called up 0.00%",
          digest.change_line(dict(rows["ZZALERT"], move=0.00001))
          == "Still WAIT; level with the previous close.",
          digest.change_line(dict(rows["ZZALERT"], move=0.00001)))

    conn = db.connect(DB)
    as_free = digest.since_yesterday(conn, ana.id, False, NOW)
    conn.close()
    rows_free = {r["symbol"]: r for r in as_free["rows"]}
    check("on the free plan a popular stock still shows its call",
          rows_free["NVDA"]["changed"])
    check("but no other does, and the page says why",
          rows_free["ZZUP"]["call"] is None and as_free["hidden"]
          and any("popular stocks and coins only" in s for s in digest.notes(as_free)),
          (rows_free.get("ZZUP"), digest.notes(as_free)))

    head("THE FRONT PAGE")
    sign_in(ana)
    t0 = time.time()
    r = c.get("/")
    took = time.time() - t0
    body = r.data.decode("utf-8", "replace")
    check("the front page draws, from what is stored", r.status_code == 200
          and not started and took < 3.0, (r.status_code, started, "%.1fs" % took))
    card = body[body.index("Since yesterday"):body.index("Your watchlist")]
    check("with a Since yesterday card above the watchlist", "Since yesterday" in body
          and body.index("Since yesterday") < body.index("Your watchlist"))
    check("a changed call says from what to what, in words",
          '<b class="fig">NVDA</b></a>: the daily call went from <span class="chip hold">'
          'WAIT</span> to <span class="chip long">BUY</span>.' in flat(card), flat(card)[:600])
    # 30 minutes old when written; a busy machine may take a minute to get here.
    check("with the move and when it was checked",
          'Up <span class="fig gain">2.81%</span> since the previous close.' in flat(card)
          and re.search(r"Checked 3[01]m ago\.", card))
    check("a popular stock opens its public page, any other an analysis",
          'href="/stock/nvda"' in card and 'href="/analyse?symbol=ZZUP&amp;interval=1d"' in card)
    check("an alert and a crossed stop are named, not repeated in full",
          '<span class="chip short">Stop crossed</span>' in card and "An alert fired." in card
          and "past the stop of 5.10" not in card)
    check("the calm ones in a sentence", "The other 3 moved less than 2%" in flat(card))
    check("and what the list is made of", "set against the one from the session before"
          in flat(card) and "last 24 hours" in card)
    mine = body[body.index("Your watchlist"):body.index("BUY calls in today")]
    check("the watchlist card shows the same daily check, not 'not analysed yet'",
          re.search(r'<b class="fig">ZZUP</b></a> <span class="chip hold">WAIT</span> <span '
                    r'class="faint">daily check 3[01]m ago at 51.5</span>', flat(mine))
          and "its daily check, made with the standard settings" in flat(mine), flat(mine)[:900])

    sign_in(free)
    watch(free, "ZZUP")
    body = c.get("/").data.decode("utf-8", "replace")
    card = body[body.index("Since yesterday"):body.index("Your watchlist")]
    mine = body[body.index("Your watchlist"):body.index("BUY calls in today")]
    check("a free account sees the move but no call on an instrument that is not popular",
          '<b class="fig">ZZUP</b></a>: Up <span class="fig gain">3.00%</span>' in flat(card)
          and "popular stocks and coins only" in card, flat(card)[:700])
    check("in both cards", '<b class="fig">ZZUP</b></a> <span class="faint">not analysed '
          'yet</span>' in flat(mine) and "the daily check for a popular stock" in flat(mine),
          flat(mine)[:700])

    bo = person("bo@example.com")
    sign_in(bo)
    body = c.get("/").data.decode("utf-8", "replace")
    check("an empty watchlist shows no such card", "Since yesterday" not in body)
    watch(bo, "ZZNEVER")
    body = c.get("/").data.decode("utf-8", "replace")
    check("a watchlist not checked yet says so",
          "One instrument has no price to compare with yet." in flat(body))
    check("and drawing it never analysed or fetched anything", not started, started)

    head("THE WEEKLY EMAIL")
    _os.environ.update(MAIL)
    _os.environ["MAIL_POSTAL_ADDRESS"] = "PO Box 1, Somewhere, CA 90000"
    conn = db.connect(DB)
    digest.set_wanted(conn, ana.id, True)
    conn.close()
    outbox = []
    # Sent whatever day the test runs on: the alerts it reads are today's.
    real_window = digest.sending_window
    digest.sending_window = lambda now: True
    conn = db.connect(DB)
    try:
        out = digest.run(conn, web.app.secret_key, "https://orenth.app",
                         lambda s: {"symbol": s, "action": "WAIT", "headline": "Wait."},
                         send=lambda to, subject, text, headers=None:
                         outbox.append((to, text)), now=NOW, full=full)
    finally:
        conn.close()
        digest.sending_window = real_window
    sent = {to: text for to, text in outbox}
    mail = sent.get("ana@example.com", "")
    check("the weekly email carries the same list", "SINCE YESTERDAY" in mail
          and "  NVDA   The daily call went from WAIT to BUY; up 2.81% since the previous "
          "close." in mail, mail[:900] or (out, digest.sending_window(NOW)))
    check("with the alert and the stop in full, since there is no page above them",
          "         Alert: ZZALERT rose above 5.00, at 5.02." in mail
          and "         Stop: ZZALERT is at 5.00, past the stop of 5.10" in mail)
    check("and the calm ones in a sentence", "The other 3 moved less than 2%" in mail)
    check("after the watchlist, before the popular setups",
          mail.index("YOUR WATCHLIST") < mail.index("SINCE YESTERDAY")
          < mail.index("STRONGEST SETUPS"))
finally:
    web.engine.analyse = real["analyse"]
    web.cached_analysis = real["cached"]
    market_mod.fetch_bars = real["bars"]
    for key in list(STRIPE) + list(MAIL) + ["MAIL_POSTAL_ADDRESS", "GOOGLE_CLIENT_ID",
                                           "GOOGLE_CLIENT_SECRET"]:
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("SINCE YESTERDAY OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
