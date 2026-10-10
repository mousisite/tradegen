"""Popular pages, sharing, invites and the weekly email."""
import calendar
import os as _os
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
# The discount banner asks Stripe in the background; these checks are about
# other things, and must not race it.
_os.environ["PROMO_CODE"] = "none"

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
        "sure": "The app looked at 83 times this same setup happened before. "
                "It worked 22 of those times. When it worked it made about 2.0 "
                "times what you put at risk."})
    check("the counts are not said twice on the page",
          said["sure"].startswith("When it worked"), said["sure"])

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
    public.save_verdict(conn, snap)
    conn.close()

    r = c.get("/stock/nvda")
    page = r.data.decode("utf-8", "replace")
    check("the page shows the call", r.status_code == 200 and "Avoid" in page
          and "It worked 22 of them" in page, r.status_code)
    check("titled the way people search", "NVIDIA (NVDA): buy, wait or avoid?" in page)
    check("open to search engines", "noindex" not in page)
    check("its link preview is its own card", "/stock/nvda/card.png" in page)
    r = c.get("/stock/nvda/card.png")
    check("the card is a real picture",
          r.status_code == 200 and r.mimetype == "image/png"
          and r.data[:8] == b"\x89PNG\r\n\x1a\n", r.status_code)
    check("that a cache may keep for an hour",
          "max-age=3600" in r.headers.get("Cache-Control", ""))
    page = c.get("/stocks").data.decode("utf-8", "replace")
    check("the list shows every popular symbol",
          all(">%s<" % public.short(s) in page for s, _ in public.POPULAR))
    check("crypto reads without its -USD", ">BTC<" in page and "BTC-USD<" not in page)
    sitemap = c.get("/sitemap.xml").data.decode()
    check("the sitemap lists the list and every page",
          "/stocks</loc>" in sitemap and "/stock/nvda</loc>" in sitemap
          and "/stock/btc-usd</loc>" in sitemap)

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
          r.status_code == 200 and "Does RSI reversion work?" in page
          and "/stock/nvda" in page and "/stock/amd" in page, r.status_code)
    check("and how many it kept money on", "kept money on 1 of the" in page)
    check("open to search engines once tested", "noindex" not in page)
    page = c.get("/backtests").data.decode("utf-8", "replace")
    check("the list ranks tested strategies, leaving out too few trades",
          "RSI reversion" in page and "Trend stack" not in page)
    page = c.get("/stock/nvda").data.decode("utf-8", "replace")
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
    invites.MAX_REWARDS = cap
    conn.close()
    sign_in(host)
    page = c.get("/account").data.decode("utf-8", "replace")
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
          and "It worked 22 of them" in body)
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

    def broken(*a, **k):
        raise RuntimeError("smtp down")

    out = digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
                     send=broken, now=monday + 14 * 86400)
    check("a failed send is reported", len(out["errors"]) == 1, out)
    digest.run(conn, web.app.secret_key, "https://orenth.app", look_up,
               send=send, now=monday + 14 * 86400 + 1800)
    check("and tried again later that week", len(outbox) == 3)
    conn.close()

    sign_out()
    url = "/email/stop?u=%d&t=%s" % (reader.id, digest.token(web.app.secret_key, reader.id))
    page = c.get(url).data.decode("utf-8", "replace")
    check("opening the stop link only asks",
          "Stop the weekly email?" in page and fresh(reader.id).weekly_email)
    check("a forged stop link is refused",
          c.post("/email/stop?u=%d&t=%s" % (reader.id, "0" * 32)).status_code == 404)
    r = c.post(url, data={"List-Unsubscribe": "One-Click"})
    check("pressing it, or the inbox button, stops it",
          r.status_code == 200 and not fresh(reader.id).weekly_email)
    for key in list(MAIL) + ["MAIL_POSTAL_ADDRESS"]:
        _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("BACKGROUND JOBS")
    print("=" * 72)
    jobs = web.start_background_jobs()
    check("the server starts the daily checks and the weekly email",
          set(jobs) == {"popular", "weekly"} and all(j.running for j in jobs.values()))
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
