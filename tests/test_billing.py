"""Paid plans: off by default, signed webhooks only, and limits that hold."""
import hashlib
import hmac
import io
import json
import os as _os
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
          "STRIPE_PRICE_ID_YEARLY": "price_y",
          "STRIPE_WEBHOOK_SECRET": "whsec_test", "PRO_PRICE_LABEL": "$9 a month"}
for key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402
from bot.market import DataError  # noqa: E402

c, DB = harness.isolated_web("billing")
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"

conn = db.connect(DB)
free = A.upsert_email_user(conn, "free@example.com")
paid = A.upsert_email_user(conn, "paid@example.com")
conn.close()


def sign_in(user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with c.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


def signed(payload, secret="whsec_test", ts=None):
    ts = int(ts or time.time())
    sig = hmac.new(secret.encode(), ("%d." % ts).encode() + payload,
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, sig)


def png_bytes(size=(900, 500)):
    """A small screenshot-shaped image, as an upload."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (250, 249, 245)).save(buf, format="PNG")
    buf.seek(0)
    return buf


try:
    print("=" * 72)
    print("WHILE BILLING IS OFF, NOTHING CHANGES")
    print("=" * 72)
    check("billing is off without keys", not billing.ready())
    check("so everyone has everything", billing.is_pro(free))
    check("and the pricing page does not exist", c.get("/pricing").status_code == 404)
    check("nor the webhook", c.post("/billing/webhook", data=b"{}").status_code == 404)

    _os.environ.update(STRIPE)

    # A stand-in for Stripe from here on, so nothing reaches the network.
    sent = []
    PRICES = {"price_x": {"unit_amount": 999, "currency": "usd",
                          "recurring": {"interval": "month", "interval_count": 1}},
              "price_y": {"unit_amount": 7999, "currency": "usd",
                          "recurring": {"interval": "year", "interval_count": 1}},
              "price_round": {"unit_amount": 900, "currency": "usd",
                              "recurring": {"interval": "month", "interval_count": 1}}}

    # The subscription the paid account's checkout started, as Stripe holds it
    # now. A checkout stays "complete" and "paid" for ever; this is what says
    # whether the plan is still running.
    SUB = {"id": "sub_paid", "object": "subscription", "status": "active",
           "current_period_end": int(time.time()) + 30 * 86400}

    def fake_stripe(method, path, data=None):
        sent.append((method, path, dict(data or {})))
        if path.startswith("prices/"):
            return PRICES[path.split("/", 1)[1]]
        if path == "checkout/sessions":
            return {"url": "https://checkout.stripe.test/c/1", "id": "cs_test_1"}
        if path.startswith("checkout/sessions/"):
            found = {"status": "complete", "client_reference_id": str(paid.id),
                     "customer": "cus_paid", "payment_status": "paid",
                     "created": int(time.time()) - 600, "subscription": SUB["id"]}
            if "expand[]=subscription" in path:
                found["subscription"] = dict(SUB)
            return found
        if method == "DELETE":
            return {"deleted": True}
        return {"url": "https://billing.stripe.test/p/1"}

    def stripe_down(method, path, data=None):
        raise billing.BillingError("down")

    def words_of(answer):
        return " ".join(answer.data.decode("utf-8", "replace").split())

    real = billing._stripe
    billing._stripe = fake_stripe

    print()
    print("=" * 72)
    print("WHO IS PRO")
    print("=" * 72)
    check("with keys set, billing is on", billing.ready())
    check("a new account is free", not billing.is_pro(free))
    check("the operator always has everything", billing.is_pro(free, operator=True))
    conn = db.connect(DB)
    check("the local account always has everything",
          billing.is_pro(A.local_user(conn)))
    billing.set_plan(conn, paid.id, "pro", "active", "cus_paid")
    check("an active subscription is Pro", billing.is_pro(A.get_user(conn, paid.id)))
    billing.set_plan(conn, paid.id, "pro", "past_due")
    check("a failed card keeps Pro while Stripe retries",
          billing.is_pro(A.get_user(conn, paid.id)))
    billing.set_plan(conn, paid.id, "free", "canceled")
    check("a cancelled one is not", not billing.is_pro(A.get_user(conn, paid.id)))
    check("and the customer number survives the change",
          billing.customer_of(conn, paid.id) == "cus_paid")
    # Pro bought in the iPhone app is kept on the user row by apple_store.
    conn.execute("UPDATE users SET apple_until=? WHERE id=?",
                 (int(time.time()) + 3600, free.id))
    check("Pro bought through Apple is Pro until it ends",
          billing.is_pro(A.get_user(conn, free.id)))
    conn.execute("UPDATE users SET apple_until=? WHERE id=?",
                 (int(time.time()) - 1, free.id))
    check("and not a moment after", not billing.is_pro(A.get_user(conn, free.id)))
    conn.execute("UPDATE users SET apple_until=0 WHERE id=?", (free.id,))
    conn.commit()
    conn.close()

    print()
    print("=" * 72)
    print("THE WEBHOOK BELIEVES ONLY STRIPE")
    print("=" * 72)
    clearing = {"type": "checkout.session.completed",
                "data": {"object": {"client_reference_id": str(free.id),
                                    "customer": "cus_free",
                                    "payment_status": "unpaid"}}}
    conn = db.connect(DB)
    check("a bank payment still clearing does not unlock Pro yet",
          billing.handle(conn, clearing) == "awaiting payment"
          and not billing.is_pro(A.get_user(conn, free.id)))
    conn.close()
    body = json.dumps({"type": "checkout.session.completed",
                       "data": {"object": {"client_reference_id": str(free.id),
                                           "customer": "cus_free",
                                           "payment_status": "paid"}}}).encode()
    check("a correct signature verifies",
          billing.verify(body, signed(body), "whsec_test"))
    check("a different secret does not",
          not billing.verify(body, signed(body, "whsec_other"), "whsec_test"))
    check("an altered body does not",
          not billing.verify(body + b" ", signed(body), "whsec_test"))
    check("an old captured request does not",
          not billing.verify(body, signed(body, ts=time.time() - 3600), "whsec_test"))
    check("a missing header does not", not billing.verify(body, "", "whsec_test"))
    check("nor one with a letter outside ASCII", not billing.verify(
        body, signed(body).split("v1=")[0] + "v1=\u00e9", "whsec_test"))

    r = c.post("/billing/webhook", data=body,
               headers={"Stripe-Signature": signed(body, "whsec_forged")})
    check("a forged event is refused", r.status_code == 400, r.status_code)
    conn = db.connect(DB)
    check("and changes nothing", not billing.is_pro(A.get_user(conn, free.id)))
    conn.close()

    r = c.post("/billing/webhook", data=body,
               headers={"Stripe-Signature": signed(body)})
    check("a signed checkout event is accepted", r.status_code == 200, r.status_code)
    conn = db.connect(DB)
    check("and makes that person Pro", billing.is_pro(A.get_user(conn, free.id)))
    conn.close()
    deep = b"[" * 3000 + b"]" * 3000
    try:
        status = c.post("/billing/webhook", data=deep,
                        headers={"Stripe-Signature": signed(deep)}).status_code
    except RecursionError:                  # TESTING hands the server's error on
        status = 500
    check("a signed body nested too deep to read is refused as not JSON",
          status == 400, status)

    cancel = json.dumps({"type": "customer.subscription.deleted",
                         "data": {"object": {"customer": "cus_free",
                                             "status": "canceled"}}}).encode()
    c.post("/billing/webhook", data=cancel,
           headers={"Stripe-Signature": signed(cancel)})
    c.post("/billing/webhook", data=cancel,
           headers={"Stripe-Signature": signed(cancel)})
    conn = db.connect(DB)
    check("cancelling, even twice, puts them back on Free",
          not billing.is_pro(A.get_user(conn, free.id)))
    early = {"type": "customer.subscription.created",
             "data": {"object": {"customer": "cus_new", "status": "active",
                                 "metadata": {"user_id": str(paid.id)}}}}
    check("a subscription that arrives before checkout is matched by its metadata",
          billing.handle(conn, early) == "pro" and billing.is_pro(A.get_user(conn, paid.id)))
    billing.set_plan(conn, paid.id, "free", "canceled")
    conn.close()

    print()
    print("=" * 72)
    print("EVENTS THAT ARRIVE LATE OR OUT OF ORDER")
    print("=" * 72)
    now = int(time.time())

    def event(kind, created, **obj):
        return {"type": kind, "created": created, "data": {"object": obj}}

    conn = db.connect(DB)
    late = A.upsert_email_user(conn, "late@example.com")
    check("a paid checkout makes them Pro",
          billing.handle(conn, event("checkout.session.completed", 1500,
                                     client_reference_id=str(late.id),
                                     customer="cus_late", payment_status="paid"))
          == "pro" and billing.is_pro(A.get_user(conn, late.id)))
    check("cancelling makes them Free",
          billing.handle(conn, event("customer.subscription.deleted", 2000,
                                     customer="cus_late", status="canceled"))
          == "free" and not billing.is_pro(A.get_user(conn, late.id)))
    said = billing.handle(conn, event("customer.subscription.updated", 1000,
                                      customer="cus_late", status="active",
                                      current_period_end=now - 86400))
    check("an older 'active', retried after the cancellation, is ignored",
          said == "stale" and not billing.is_pro(A.get_user(conn, late.id)), said)
    check("and so is a late copy of the checkout itself",
          billing.handle(conn, event("checkout.session.completed", 1400,
                                     client_reference_id=str(late.id),
                                     customer="cus_late", payment_status="paid"))
          == "stale" and not billing.is_pro(A.get_user(conn, late.id)))
    check("while a newer one still counts",
          billing.handle(conn, event("customer.subscription.updated", 3000,
                                     customer="cus_late", status="active",
                                     current_period_end=now + 30 * 86400))
          == "pro" and billing.is_pro(A.get_user(conn, late.id)))
    check("the end of the paid period is kept",
          A.get_user(conn, late.id).plan_until == now + 30 * 86400)

    # If the cancellation never arrives at all, the paid period still ends.
    billing.handle(conn, event("customer.subscription.updated", 4000,
                               customer="cus_late", status="active",
                               current_period_end=now - 4 * 86400))
    check("a plan four days past its paid period, with no word since, is over",
          not billing.is_pro(A.get_user(conn, late.id)))
    billing.handle(conn, event("customer.subscription.updated", 5000,
                               customer="cus_late", status="active",
                               current_period_end=now - 86400))
    check("one a day past it is still Pro, while Stripe retries the renewal",
          billing.is_pro(A.get_user(conn, late.id)))
    billing.handle(conn, event("customer.subscription.deleted", 6000,
                               customer="cus_late", status="canceled"))
    conn.execute("UPDATE users SET plan_until=? WHERE id=?",
                 (now - 365 * 86400, late.id))
    conn.commit()
    billing.handle(conn, event("checkout.session.completed", 7000,
                               client_reference_id=str(late.id),
                               customer="cus_late", payment_status="paid"))
    back = A.get_user(conn, late.id)
    check("subscribing again is Pro, whatever ended a year ago",
          billing.is_pro(back) and back.plan_until == 0, back.plan_until)

    print()
    print("   An event about one subscription never ends another that is paid for:")
    twice = A.upsert_email_user(conn, "twice@example.com")
    ends = now + 30 * 86400
    at_stripe = {}

    def two_subscriptions(method, path, data=None):
        customer = path.rsplit("customer=", 1)[-1]
        sent.append((method, path, dict(data or {})))
        if at_stripe.get(customer) == "down":
            raise billing.BillingError("down")
        if at_stripe.get(customer) == "deleted":
            raise billing.BillingRefused("gone", 404)
        return {"data": at_stripe.get(customer, [])}

    def says(kind, created, **obj):
        return billing.handle(conn, event(kind, created, **obj), call=two_subscriptions)

    def is_pro():
        return billing.is_pro(A.get_user(conn, twice.id))

    # The first checkout stopped at the bank's check, the second was paid.
    abandoned = {"id": "sub_A", "status": "incomplete_expired", "created": 900}
    running = {"id": "sub_B", "status": "active", "created": 1000,
               "current_period_end": ends}
    says("checkout.session.completed", 1000, client_reference_id=str(twice.id),
         customer="cus_B", payment_status="paid")
    at_stripe.update(cus_A=[abandoned], cus_B=[running])
    said = says("customer.subscription.updated", 80000, id="sub_A", customer="cus_A",
                status="incomplete_expired", metadata={"user_id": str(twice.id)})
    kept = A.get_user(conn, twice.id)
    check("an abandoned first checkout expiring does not end the paid one",
          said == "pro" and billing.is_pro(kept) and kept.plan_until == ends, said)
    check("nor takes the place of the customer who pays",
          billing.customer_of(conn, twice.id) == "cus_B")
    at_stripe["cus_B"] = [dict(abandoned), dict(running)]
    said = says("customer.subscription.deleted", 80100, id="sub_A", customer="cus_B",
                status="canceled")
    check("nor does cancelling one of two on the same customer", said == "pro" and is_pro())
    said = says("customer.subscription.created", 80100, id="sub_B", customer="cus_B",
                status="incomplete")
    check("nor a late 'incomplete' of the same second as 'active'", said == "pro" and is_pro())
    at_stripe["cus_B"] = "down"
    try:
        said = says("customer.subscription.deleted", 80200, customer="cus_B",
                    status="canceled")
    except billing.BillingError:
        said = "again later"
    check("with Stripe out of reach nothing is decided, and the event comes again",
          said == "again later" and is_pro(), said)
    billing._stripe = stripe_down
    ended = json.dumps(event("customer.subscription.deleted", 80200, customer="cus_B",
                             status="canceled")).encode()
    r = c.post("/billing/webhook", data=ended, headers={"Stripe-Signature": signed(ended)})
    check("which is what its answer tells Stripe", r.status_code == 503, r.status_code)
    billing._stripe = fake_stripe
    at_stripe["cus_B"] = [dict(running, status="canceled")]
    said = says("customer.subscription.deleted", 80200, customer="cus_B", status="canceled")
    check("while the end of the only one ends Pro", said == "free" and not is_pro(), said)
    says("customer.subscription.updated", 80300, customer="cus_B", status="active",
         current_period_end=ends)
    at_stripe["cus_B"] = "deleted"
    said = says("customer.subscription.deleted", 80400, customer="cus_B", status="canceled")
    check("as it does for a customer deleted at Stripe", said == "free" and not is_pro(), said)
    before = len(sent)
    says("customer.subscription.updated", 80500, customer="cus_B", status="unpaid")
    check("and nobody on Free is asked about", len(sent) == before)
    conn.close()

    print()
    print("=" * 72)
    print("THE FREE ALLOWANCE")
    print("=" * 72)
    sign_in(free)
    check("a free account sees the Go Pro button on every page",
          "Go Pro" in c.get("/account").data.decode("utf-8", "replace"))
    real_analysis = web.cached_analysis

    def no_such_ticker(*a, **k):
        raise DataError("No data for APPL.")

    web.cached_analysis = no_such_ticker
    try:
        c.get("/analyse?symbol=APPL&interval=1d")
        c.get("/api/analyse?symbol=APPL")
        conn = db.connect(DB)
        check("a mistyped ticker does not use up a free look",
              billing.views_today(conn, free.id) == 0)
        conn.close()
    finally:
        web.cached_analysis = real_analysis
    with web.app.test_request_context("/"):
        conn = db.connect(DB)
        web.g.user, web.g.demo = A.get_user(conn, free.id), False
        conn.close()
        web._spend_view("aapl")
        web._spend_view("AAPL")
    conn = db.connect(DB)
    check("one that is shown does, once however often it is opened",
          billing.views_today(conn, free.id) == 1)

    limit = billing.free_limits()["analyses_per_day"]
    for n in range(limit):
        billing.count_view(conn, free.id, "SYM%d|1d" % n)
    check("the allowance can be used up", billing.reserve_view(conn, free.id, "NEW|1d") == "")
    check("but an instrument already opened today stays open",
          billing.reserve_view(conn, free.id, "SYM0|1d") == "seen")
    conn.close()

    r = c.get("/analyse?symbol=MSFT&interval=1d")
    page = r.data.decode("utf-8", "replace")
    check("past the allowance, an analysis shows the pricing page instead",
          r.status_code == 402 and "free allowance" in page, r.status_code)
    check("which offers the upgrade", 'action="/billing/checkout"' in page)
    check("the JSON API keeps the same allowance",
          c.get("/api/analyse?symbol=MSFT").status_code == 402)

    touched = []

    def must_not_run(*a, **k):
        touched.append(a)
        raise DataError("This should not have been analysed.")

    real_research = web.engine.research
    real_read = web.read_chart
    had_key = _os.environ.get("ANTHROPIC_API_KEY")
    web.cached_analysis = web.engine.research = must_not_run
    web.read_chart = must_not_run
    _os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    try:
        r = c.post("/thesis/save", data={"symbol": "ZZZZ", "interval": "1d"})
        check("saving a thesis is held to the same allowance",
              r.status_code == 402 and not touched, r.status_code)
        r = c.post("/analyse/image", data={"chart": (png_bytes(), "c.png")},
                   content_type="multipart/form-data")
        check("and a screenshot is not sent to the paid reader once it is used up",
              r.status_code == 402 and not touched, r.status_code)
    finally:
        web.cached_analysis, web.engine.research = real_analysis, real_research
        web.read_chart = real_read
        if had_key is None:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _os.environ["ANTHROPIC_API_KEY"] = had_key
    conn = db.connect(DB)
    check("and nothing was saved", db.thesis_list(conn, user=free.id) == [])
    conn.close()

    # Ten requests sent together used to all read the same count and all get
    # through. Each look is now taken before the analysis runs.
    import threading
    conn = db.connect(DB)
    racer = A.upsert_email_user(conn, "racer@example.com")
    conn.close()
    got = []
    gate = threading.Barrier(8)

    def look(n):
        with web.app.test_request_context("/"):
            conn = db.connect(DB)
            web.g.user, web.g.demo = A.get_user(conn, racer.id), False
            conn.close()
            gate.wait()
            if web._allowance_left("RACE%d" % n):
                got.append(n)
                web._spend_view("RACE%d" % n)

    threads = [threading.Thread(target=look, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.connect(DB)
    check("eight looks asked for at once get no more than the allowance",
          len(got) == limit, len(got))
    check("and exactly that many are counted",
          billing.views_today(conn, racer.id) == limit)
    conn.close()

    print()
    print("   A look is one instrument, whatever was typed and whatever the bars:")
    from types import SimpleNamespace
    from bot import report as report_mod
    from bot import workflows

    from bot import market

    MEANT = {"TESLA": "TSLA", "TESLAINC": "TSLA", "BITCOIN": "BTC-USD"}

    def found_by_name(symbol, interval, *a, **k):
        if symbol.upper() == "NOTHING":
            raise DataError("No data for NOTHING.")
        own = market.normalise_symbol(symbol, exact=k.get("exact", False))
        return SimpleNamespace(bars=SimpleNamespace(symbol=MEANT.get(own, own)),
                               plan=None, signals=[],
                               composite=0, regime=None, calibration=None, sentiment=None)

    def account(email):
        conn = db.connect(DB)
        try:
            return A.upsert_email_user(conn, email)
        finally:
            conn.close()

    def used(who):
        conn = db.connect(DB)
        try:
            return billing.views_today(conn, who.id)
        finally:
            conn.close()

    def opens(symbol, interval="1d", exact=""):
        return c.get("/api/analyse", query_string={"symbol": symbol, "interval": interval,
                                                   "exact": exact}).status_code

    real_json = report_mod.to_json
    report_mod.to_json = lambda *a: "{}"
    web.cached_analysis = found_by_name
    try:
        namer = account("namer@example.com")
        sign_in(namer)
        got = [opens(s) for s in ("tesla", "TSLA", "$tsla", "NASDAQ:TSLA", "Tesla Inc")]
        check("one stock asked for by its name and by its ticker is one look",
              got == [200] * 5 and used(namer) == 1, (got, used(namer)))
        got = [opens("TSLA", bar) for bar in ("1h", "15m", "1wk")]
        check("and so it is on every bar size", got == [200] * 3 and used(namer) == 1,
              (got, used(namer)))
        got = [opens(s) for s in ("bitcoin", "BTC", "BTC-USD", "btcusdt", "BTC/USD")]
        check("as is a coin, however it is written", got == [200] * 5 and used(namer) == 2,
              (got, used(namer)))
        check("a name that finds nothing costs nothing",
              opens("nothing") == 404 and used(namer) == 2, used(namer))
        check("a listing a coin's ticker hides is a look of its own",
              opens("LINK", exact="1") == 200 and used(namer) == 3, used(namer))
        check("the fourth instrument is refused", opens("MSFT") == 402 and opens("link") == 402)
        check("while those already opened stay open, by name too",
              [opens("tesla"), opens("TSLA", "1h"), opens("bitcoin"),
               opens("LINK", exact="1")] == [200] * 4 and used(namer) == 3, used(namer))

        # Rows from the hours before this release hold the bar size, and the
        # words typed. Nobody loses a look over them on the day it changes.
        older = account("older@example.com")
        sign_in(older)
        conn = db.connect(DB)
        for item in ("AAPL|1d", "AAPL|1h", "TESLA|1d", "LINK|1d|exact"):
            conn.execute("INSERT INTO usage_views (user_id, day, item) VALUES (?, ?, ?)",
                         (older.id, billing._today(), item))
        conn.commit()
        conn.close()
        check("looks counted the old way are counted by instrument too", used(older) == 3,
              used(older))
        check("what they opened opens again at no cost",
              [opens("AAPL", "15m"), opens("LINK", exact="1"), opens("tesla")] == [200] * 3
              and used(older) == 3, used(older))
        check("and a name becomes the instrument it stands for",
              opens("TSLA") == 200 and used(older) == 3 and opens("MSFT") == 402, used(older))

        # The routines keep the same count as the pages.
        routine = account("routine@example.com")
        conn = db.connect(DB)
        looks = workflows._Looks(conn, routine.id)
        taken = looks.take("tesla")
        looks.shown("tesla", "TSLA")
        check("a routine's look is the instrument's as well",
              taken == "new" and looks.take("TSLA") == "seen" and used(routine) == 1,
              (taken, used(routine)))
        conn.close()
    finally:
        web.cached_analysis = real_analysis
        report_mod.to_json = real_json

    # A link on someone else's site arrives with the session cookie, so it
    # could spend a free account's looks.
    conn = db.connect(DB)
    linked = A.upsert_email_user(conn, "linked@example.com")
    billing.count_view(conn, linked.id, "SEEN|1d")
    conn.close()
    sign_in(linked)
    ran = []

    def note_and_fail(symbol, *a, **k):
        ran.append(symbol)
        raise DataError("No data for %s." % symbol)

    web.cached_analysis = note_and_fail
    try:
        # Google sends a fresh sign-in back through its own redirect, which
        # the browser reports as another site.
        c.get("/analyse?symbol=WANTED&interval=1d",
              headers={"Sec-Fetch-Site": "cross-site"})
        check("the page someone signed in to reach still opens after Google",
              ran == ["WANTED"], ran)
        ran.clear()
        conn = db.connect(DB)
        conn.execute("UPDATE sessions SET created_ts=created_ts-600 WHERE user_id=?",
                     (linked.id,))
        conn.commit()
        conn.close()
        r = c.get("/analyse?symbol=LINKED&interval=1d",
                  headers={"Sec-Fetch-Site": "cross-site"})
        check("a link from another site does not spend a free look",
              r.status_code == 200 and not ran
              and "another site" in r.data.decode("utf-8", "replace"), r.status_code)
        c.get("/analyse?symbol=SEEN&interval=1d", headers={"Sec-Fetch-Site": "cross-site"})
        check("but opens one already looked at today", ran == ["SEEN"], ran)
        c.get("/analyse?symbol=LINKED&interval=1d",
              headers={"Sec-Fetch-Site": "same-origin"})
        check("and from this site it opens as usual", ran[-1:] == ["LINKED"], ran)
        conn = db.connect(DB)
        check("a look taken for something that could not be shown is handed back",
              billing.views_today(conn, linked.id) == 1)
        conn.close()
    finally:
        web.cached_analysis = real_analysis
    sign_in(free)

    conn = db.connect(DB)
    db.alert_add(conn, "AAPL", "price_above", 999999.0, user=free.id)
    conn.close()
    r = c.post("/monitor/alert/add", data={"symbol": "MSFT", "kind": "price_above",
                                           "threshold": "1"})
    check("a free account cannot run more alerts than its allowance",
          r.status_code == 402, r.status_code)

    # Find shows a free account the top of each list.
    real_find = web.ideas_mod.find

    def fake_find(market, horizon, cfg, risk, **_kw):
        row = {"symbol": "X", "action": "BUY", "price": 10.0, "entry": 10.0,
               "stop": 9.0, "target": 12.0, "probability": 0.4, "samples": 100,
               "reliable": True, "gap_multiple": 1.5, "expectancy": 0.2}
        rows = [dict(row, symbol="S%d" % i, rank=i + 1) for i in range(8)]
        return {"passed": rows, "watch": rows[:5], "dropped": [], "summary": "s",
                "interval": "1d", "scanned": 8, "seconds": 1.0, "avoided": 0,
                "warning": "", "risk_note": "", "risk_label": "Balanced",
                "ranked_as": "edge", "wrong_shape": 0, "scanned_at": time.time(),
                "cut_short": False, "asked_for": 8}

    web.ideas_mod.find = fake_find
    try:
        page = c.get("/ideas?market=stocks&horizon=medium&risk=balanced").data.decode()
        shown = sum(1 for i in range(8) if ">S%d<" % i in page)
        check("a free account sees the top of the Find list, as far as its plan says",
              shown == billing.free_limits()["find_results"], shown)
        check("and is told how many more Pro would show", "Pro shows all of them" in page)
        conn = db.connect(DB)
        billing.set_plan(conn, free.id, "pro", "active")
        conn.close()
        page = c.get("/ideas?market=stocks&horizon=medium&risk=balanced").data.decode()
        check("a Pro account sees all of it", ">S7<" in page
              and "Pro shows all of them" not in page)
        conn = db.connect(DB)
        billing.set_plan(conn, free.id, "free", "canceled")
        conn.close()

        real_demo = web._demo_shortlist
        web._demo_shortlist = lambda market, horizon, risk: fake_find(market, horizon, None, risk)
        with c.session_transaction() as sess:
            sess.clear()
        try:
            page = c.get("/ideas?market=stocks&horizon=medium&risk=balanced").data.decode()
            check("a visitor sees no more than a free account would",
                  ">S7<" not in page and "Pro shows all of them" in page)
        finally:
            web._demo_shortlist = real_demo
            sign_in(free)

        # The counts in the free box are the uncut ones, one per list, and
        # only a scan that read its whole list is called "the full scan".
        import html as _html
        import re as _re

        def words(body):
            body = _re.sub(r"<script.*?</script>", "", body, flags=_re.S)
            return _re.sub(r"\s+", " ", _html.unescape(_re.sub(r"<[^>]+>", " ", body)))

        def fake_running(market, horizon, cfg, risk, **_kw):
            found = fake_find(market, horizon, cfg, risk)
            return dict(found, passed=[dict(r, rank=None) for r in found["passed"]],
                        passed_total=8, watch_total=14, watch_shown=5,
                        partial=True, running=True, complete=False,
                        looked_at=60, asked_for_now=150)

        def fake_done(market, horizon, cfg, risk, **_kw):
            return dict(fake_find(market, horizon, cfg, risk), passed_total=8,
                        watch_total=5, watch_shown=5, complete=True, running=False)

        web.ideas_mod.find = fake_running
        text = words(c.get("/ideas?market=stocks&horizon=medium&risk=balanced").data.decode())
        check("while a scan runs, the free box says 'so far', never 'the full scan'",
              "So far this scan has found 8 worth a look and 14 worth watching" in text
              and "The full scan found" not in text and "So far, 8 worth a look" in text,
              text[text.find("Free plan"):text.find("Free plan") + 160])
        check("and does not promise all of a list Pro also shortens",
              "Pro shows all of them" not in text
              and "the 5 strongest worth watching" in text)
        web.ideas_mod.find = fake_done
        text = words(c.get("/ideas?market=stocks&horizon=medium&risk=balanced").data.decode())
        check("a finished scan's free box counts each list on its own",
              "The full scan found 8 worth a look and 5 worth watching" in text,
              text[text.find("Free plan"):text.find("Free plan") + 160])
        check("and the lead agrees with it", "Showing 3 of 8 worth a look" in text)
    finally:
        web.ideas_mod.find = real_find

    print()
    print("=" * 72)
    print("PAYING, THROUGH A STAND-IN FOR STRIPE")
    print("=" * 72)
    sign_in(paid)
    r = c.post("/billing/checkout")
    check("upgrading sends them to Stripe's own page",
          r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe"),
          r.status_code)
    _, _, data = sent[-1]
    check("for this person and this price",
          data["client_reference_id"] == str(paid.id)
          and data["line_items[0][price]"] == "price_x")
    conn = db.connect(DB)
    known = billing.customer_of(conn, paid.id)
    conn.close()
    check("and reuses their Stripe customer instead of making a second one",
          known and data.get("customer") == known, (known, data.get("customer")))
    billing.checkout_url(type("U", (), {"id": 90, "email": "new@example.com"})(),
                         "https://x", call=fake_stripe)
    check("a first-time buyer has their email filled in for them",
          sent[-1][2].get("customer_email") == "new@example.com")
    billing.checkout_url(type("U", (), {"id": 91, "email": "phone1555@users.invalid"})(),
                         "https://x", call=fake_stripe)
    check("but a made-up placeholder address is never sent to Stripe",
          "customer_email" not in sent[-1][2])

    r = c.get("/billing/done?session_id=cs_test_1")
    check("coming back from Stripe confirms it with Stripe directly",
          "You have Pro" in r.data.decode("utf-8", "replace"))
    conn = db.connect(DB)
    check("and they are Pro straight away", billing.is_pro(A.get_user(conn, paid.id)))
    conn.close()

    sign_in(free)
    r = c.get("/billing/done?session_id=cs_test_1")
    check("someone else's checkout cannot upgrade them",
          "You have Pro" not in r.data.decode("utf-8", "replace"))
    got = [c.get("/billing/done" + q) for q in ("", "?session_id=nonsense",
                                                "?session_id=cs_test_1",
                                                "?session_id=cs_x/../y")]
    check("the return page speaks of no payment to someone who made none",
          [(g.status_code, g.headers.get("Location")) for g in got]
          == [(302, "/pricing")] * 4, [g.status_code for g in got])
    billing._stripe = stripe_down
    page = words_of(c.get("/billing/done?session_id=cs_test_1"))
    check("when Stripe cannot be asked it says so, and offers to check again",
          "We are confirming your payment" in page and "Payment received" not in page
          and 'href="/billing/done?session_id=cs_test_1">Check again' in page)

    def still_clearing(method, path, data=None):
        return {"status": "complete", "client_reference_id": str(free.id),
                "payment_status": "unpaid", "subscription": {"status": "incomplete"}}

    billing._stripe = still_clearing
    check("and so it does while a payment is still clearing",
          "We are confirming your payment"
          in words_of(c.get("/billing/done?session_id=cs_test_2")))
    billing._stripe = lambda *a, **k: (_ for _ in ()).throw(
        billing.BillingRefused("no such checkout", 404))
    check("but not for a checkout Stripe does not know",
          c.get("/billing/done?session_id=cs_test_3").status_code == 302)
    billing._stripe = fake_stripe
    visitor = web.app.test_client()
    r = visitor.get("/billing/done?session_id=cs_test_1")
    check("a buyer signed out on the way back signs in first",
          r.status_code == 302 and "/signin" in r.headers["Location"]
          and "cs_test_1" in r.headers["Location"], r.headers.get("Location"))

    print()
    print("   A finished checkout is not a running plan:")
    check("the return page reads the subscription along with the checkout",
          any(p.startswith("checkout/sessions/cs_test_1")
              and "expand[]=subscription" in p for _, p, _ in sent))
    conn = db.connect(DB)
    check("and keeps when its paid period ends",
          A.get_user(conn, paid.id).plan_until == SUB["current_period_end"])
    conn.close()
    sign_in(paid)
    gone = json.dumps({"type": "customer.subscription.deleted",
                       "created": int(time.time()),
                       "data": {"object": {"id": "sub_paid", "customer": "cus_paid",
                                           "status": "canceled"}}}).encode()
    c.post("/billing/webhook", data=gone, headers={"Stripe-Signature": signed(gone)})
    SUB["status"] = "canceled"
    r = c.get("/billing/done?session_id=cs_test_1")
    conn = db.connect(DB)
    check("opening the return address again after cancelling does not bring Pro back",
          "You have Pro" not in r.data.decode("utf-8", "replace")
          and not billing.is_pro(A.get_user(conn, paid.id)))
    SUB["status"] = "active"
    check("nor does a reply from Stripe that leaves the subscription out",
          not billing.confirm_checkout(
              conn, "cs_test_1", paid.id,
              call=lambda m, p, d=None: {"status": "complete", "subscription": "sub_paid",
                                         "client_reference_id": str(paid.id),
                                         "payment_status": "paid"}))
    # Pro again for the checks that follow.
    billing.set_plan(conn, paid.id, "pro", "active", "cus_paid")
    conn.close()

    r = c.post("/billing/portal")
    check("managing the subscription goes to Stripe's portal",
          r.status_code == 303 and "billing.stripe" in r.headers["Location"])
    page = c.get("/account").data.decode("utf-8", "replace")
    check("the account page shows the plan", "Manage subscription" in page)
    check("and a Pro account is not asked to upgrade", "Go Pro" not in page)

    conn = db.connect(DB)
    leaver = A.upsert_email_user(conn, "leaver@example.com")
    billing.set_plan(conn, leaver.id, "pro", "active", "cus_leaver")
    conn.close()
    sign_in(leaver)
    billing._stripe = stripe_down
    r = c.post("/account/delete", data={"confirm": "delete"})
    conn = db.connect(DB)
    check("if Stripe cannot be reached, a paying account is not deleted",
          r.status_code == 502 and A.get_user(conn, leaver.id) is not None,
          r.status_code)
    conn.close()
    billing._stripe = fake_stripe
    r = c.post("/account/delete", data={"confirm": "delete"})
    conn = db.connect(DB)
    check("deleting an account cancels its subscription in Stripe",
          ("DELETE", "customers/cus_leaver", {}) in sent)
    check("and then deletes the account", A.get_user(conn, leaver.id) is None)
    # Deleting the customer makes Stripe say its subscription ended.
    said = billing.handle(conn, event("customer.subscription.deleted", now,
                                      customer="cus_leaver", status="canceled",
                                      metadata={"user_id": str(leaver.id)}))
    again = billing.handle(conn, event("checkout.session.completed", now,
                                       client_reference_id=str(leaver.id),
                                       customer="cus_leaver", payment_status="paid"))
    applied = billing._apply(conn, leaver.id, now, "free", "canceled")
    kept = conn.execute("SELECT COUNT(*) FROM plan_events WHERE user_id=?",
                        (leaver.id,)).fetchone()[0]
    check("what Stripe says of it afterwards leaves nothing behind",
          (said, again) == ("unknown customer",) * 2 and not applied and kept == 0,
          (said, again, applied, kept))
    conn.close()

    print()
    print("   What a plan does next is said, as Stripe tells it:")

    def dated(days):
        return web._day(now + days * 86400)

    conn = db.connect(DB)
    teller = A.upsert_email_user(conn, "teller@example.com")
    conn.close()
    sign_in(teller)
    ends_at = now + 20 * 86400

    def told(created, **obj):
        conn = db.connect(DB)
        try:
            billing.handle(conn, event("customer.subscription.updated", created,
                                       metadata={"user_id": str(teller.id)},
                                       customer="cus_teller", **obj),
                           call=lambda *a, **k: {"data": []})
        finally:
            conn.close()
        return [words_of(c.get(p)) for p in ("/account", "/pricing")]

    pages = told(100, status="active", current_period_end=ends_at)
    check("with nothing said of renewing, only what is paid for is claimed",
          all("It is paid until %s." % dated(20) in p and "Pro renews on" not in p
              and "is cancelled" not in p for p in pages))
    pages = told(200, status="active", current_period_end=ends_at,
                 cancel_at_period_end=False, cancel_at=None)
    check("a plan that renews says when, on the account and the pricing page",
          all("Pro renews on %s." % dated(20) in p for p in pages))
    for at, extra in ((300, {"cancel_at_period_end": True}),
                      (400, {"cancel_at_period_end": False, "cancel_at": ends_at})):
        pages = told(at, status="active", current_period_end=ends_at, **extra)
        check("one cancelled says when it ends, %s" % sorted(extra),
              all("is cancelled: Pro stays on until %s, and then ends." % dated(20) in p
                  and "Pro renews on" not in p for p in pages))
    check("and none of them is told it has no limits",
          "No limits" not in pages[0] and "as many alerts" in pages[0])
    pages = told(500, status="past_due", current_period_end=ends_at,
                 cancel_at_period_end=False)
    check("while a payment is being tried again, no renewal is promised",
          all("renews on" not in p for p in pages))
    pages = told(600, status="canceled", cancel_at_period_end=False)
    check("a Free account that paid before still reaches its receipts",
          "Receipts and billing" in pages[0] and 'action="/billing/portal"' in pages[0]
          and c.post("/billing/portal").status_code == 303)
    conn = db.connect(DB)
    billing.handle(conn, event("checkout.session.completed", 700, mode="payment",
                               client_reference_id=str(teller.id),
                               customer="cus_teller", payment_status="paid"))
    check("a paid checkout that is not of a plan makes nobody Pro",
          not billing.is_pro(A.get_user(conn, teller.id)))
    billing.handle(conn, event("checkout.session.completed", 800, mode="subscription",
                               client_reference_id=str(teller.id),
                               customer="cus_teller", payment_status="paid"))
    back = A.get_user(conn, teller.id)
    check("while one of a plan does, with nothing left over of the one cancelled",
          billing.is_pro(back) and (back.plan_ends, billing.paid_until(back)) == (-1, ("", 0)),
          (back.plan_ends, billing.paid_until(back)))
    billing.set_plan(conn, teller.id, "free", "canceled")
    conn.execute("UPDATE users SET bonus_until=? WHERE id=?",
                 (now + 5 * 86400, teller.id))
    conn.commit()
    conn.close()
    page = words_of(c.get("/pricing"))
    check("Pro from an invite is said to be free, with its end, and not to renew",
          "It is free until %s, and then your account goes back to Free" % dated(5) in page
          and "Pro renews automatically" not in page and "Receipts and billing" in page)
    check("as it is on the account page",
          "Free from invites until %s." % dated(5) in words_of(c.get("/account")))
    conn = db.connect(DB)
    conn.execute("UPDATE users SET bonus_until=0 WHERE id=?", (teller.id,))
    conn.commit()
    conn.close()
    check("while whoever may buy is told that it renews",
          "Pro renews automatically" in words_of(c.get("/pricing"))
          and "Pro renews automatically" in words_of(web.app.test_client().get("/pricing")))

    print()
    print("   One setting missing never traps someone who pays:")
    conn = db.connect(DB)
    stuck = A.upsert_email_user(conn, "stuck@example.com")
    billing.set_plan(conn, stuck.id, "pro", "active", "cus_stuck")
    conn.close()
    sign_in(stuck)
    check("with everything set there is nothing to report",
          billing.half_set() == "" and billing.describe() == "on")
    for missing in ("STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET"):
        del _os.environ[missing]
        check("%s missing is said by name" % missing,
              billing.half_set() == missing + " is not set"
              and missing in billing.describe() and billing.describe().startswith("OFF"),
              billing.describe())
        check("the account page still leads to Stripe's own",
              "Manage subscription" in words_of(c.get("/account"))
              and c.post("/billing/portal").status_code == 303)
        _os.environ["STOCKBOT_ADMIN_EMAIL"] = "stuck@example.com"
        check("and the operator's page says what is wrong",
              "Paid plans are off:</b> %s is not set" % missing in words_of(c.get("/usage")))
        del _os.environ["STOCKBOT_ADMIN_EMAIL"]
        _os.environ[missing] = STRIPE[missing]
    del _os.environ["STRIPE_PRICE_ID"]
    ended = json.dumps({"type": "customer.subscription.deleted", "created": now,
                        "data": {"object": {"customer": "cus_stuck",
                                            "status": "canceled"}}}).encode()
    r = c.post("/billing/webhook", data=ended, headers={"Stripe-Signature": signed(ended)})
    check("Stripe's messages are still recorded",
          r.status_code == 200 and r.get_json()["outcome"] == "free", r.status_code)
    _os.environ["STRIPE_PRICE_ID"] = STRIPE["STRIPE_PRICE_ID"]
    conn = db.connect(DB)
    billing.set_plan(conn, stuck.id, "pro", "active", "cus_stuck")
    conn.close()
    del _os.environ["STRIPE_SECRET_KEY"]
    r = c.post("/account/delete", data={"confirm": "delete"})
    conn = db.connect(DB)
    check("without the key a paying account is kept, not left being charged",
          r.status_code == 502 and A.get_user(conn, stuck.id) is not None, r.status_code)
    conn.close()
    _os.environ["STRIPE_SECRET_KEY"] = STRIPE["STRIPE_SECRET_KEY"]
    del _os.environ["STRIPE_WEBHOOK_SECRET"]
    c.post("/account/delete", data={"confirm": "delete"})
    check("and with it, deleting cancels the subscription whatever else is missing",
          ("DELETE", "customers/cus_stuck", {}) in sent)
    _os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE["STRIPE_WEBHOOK_SECRET"]
    check("a page with no mistake to report says nothing of one",
          "Paid plans are off" not in words_of(c.get("/usage")))

    print()
    print("=" * 72)
    print("PRICES AND PLANS")
    print("=" * 72)
    check("the yearly plan maps to the yearly price",
          billing.price_id_for("yearly") == "price_y")
    check("anything else gets the monthly one, even a price id sent by a browser",
          billing.price_id_for("monthly") == "price_x"
          and billing.price_id_for("price_round") == "price_x")
    billing._PRICES.clear()
    offer = billing.offers()
    check("prices are read from Stripe, cents and all",
          offer["monthly"]["label"] == "$9.99 a month"
          and offer["yearly"]["label"] == "$79.99 a year", offer)
    check("and what yearly saves is worked out, rounded down", offer["save"] == 33,
          offer["save"])
    check("a whole-dollar price has no .00",
          billing.describe_price("price_round")["label"] == "$9 a month")
    _os.environ["STRIPE_PRICE_ID_YEARLY"] = "price_round"
    check("a monthly price put in the yearly slot by mistake is not offered as yearly",
          billing.offers()["yearly"] is None)
    _os.environ["STRIPE_PRICE_ID_YEARLY"] = "price_y"
    billing._PRICES.clear()
    billing._stripe = stripe_down
    check("if Stripe cannot be reached, the written label stands in",
          billing.offers()["monthly"]["label"] == "$9 a month")
    tried = []

    def counted_down(method, path, data=None):
        tried.append(path)
        raise billing.BillingError("down")

    billing._stripe = counted_down
    billing._PRICES.clear()
    for _ in range(3):
        billing.offers()
    check("and it is asked once a minute, not by every page",
          tried == ["prices/price_x", "prices/price_y"], tried)
    long_ago = time.time() - billing.PRICE_TTL - 1
    for pid in ("price_x", "price_y"):
        billing._PRICES[pid] = (long_ago, {"cents": 999, "interval": "month",
                                           "label": "$9.99 a month"})
    del tried[:]
    said = [billing.offers()["monthly"]["label"] for _ in range(3)]
    check("with the last price it gave shown meanwhile",
          said == ["$9.99 a month"] * 3 and len(tried) == 2, (said, tried))
    billing._stripe = fake_stripe
    for pid, (at, info) in list(billing._PRICES.items()):
        billing._PRICES[pid] = (at - billing.PRICE_RETRY - 1, info)
    check("and a minute later it is asked again",
          billing.offers()["yearly"]["label"] == "$79.99 a year")
    billing._PRICES.clear()
    asked_with, keys = [], []

    import requests as requests_mod
    real_request = requests_mod.request
    requests_mod.request = lambda method, url, **k: (
        asked_with.append(k["timeout"]), keys.append(k["auth"]),
        (_ for _ in ()).throw(requests_mod.ConnectionError("slow")))[0]
    _os.environ["STRIPE_SECRET_KEY"] = "sk_test_x\r\n"
    try:
        for path in ("prices/price_x", "subscriptions?x=1"):
            try:
                real("GET", path)
            except billing.BillingError:
                pass
    finally:
        requests_mod.request = real_request
        _os.environ["STRIPE_SECRET_KEY"] = STRIPE["STRIPE_SECRET_KEY"]
    check("a key pasted with a line break after it still works",
          keys == [("sk_test_x", "")] * 2, keys)
    check("a price is waited for briefly, anything else as before",
          asked_with == [billing.PRICE_TIMEOUT, 20] and billing.PRICE_TIMEOUT < 8,
          asked_with)

    def coupon(**k):
        return " ".join(billing._describe(dict({"duration": "once"}, **k)))

    check("a discount is worded as money is written",
          [coupon(amount_off=550, currency="usd"), coupon(amount_off=250, currency="eur"),
           coupon(amount_off=1234567, currency="usd"), coupon(amount_off=500),
           coupon(percent_off=25, duration="repeating", duration_in_months=1),
           coupon(percent_off=25, duration="repeating", duration_in_months=3)]
          == ["$5.50 off on the first payment", "€2.50 off on the first payment",
              "$12345.67 off on the first payment", "$5 off on the first payment",
              "25% off for 1 month", "25% off for 3 months"],
          [coupon(amount_off=550, currency="usd"), coupon(amount_off=250, currency="eur")])

    conn = db.connect(DB)
    newcomer = A.upsert_email_user(conn, "newcomer@example.com")
    conn.close()
    sign_in(newcomer)
    page = c.get("/pricing").data.decode("utf-8", "replace")
    check("the pricing page shows both prices",
          "$9.99 a month" in page and "$79.99 a year" in page and "save 33%" in page)
    check("with no free trial", "Upgrade to Pro" in page and "free trial" not in page
          and "Free for" not in page)
    check("and says plainly that Pro is not better odds", "not better odds" in page)
    said = " ".join(page.split())
    check("it promises of each Find list what Pro shows of it",
          "Every Find result worth a look, and the %d strongest worth watching"
          % web.ideas_mod.WATCH_SHOWN in said and "in every Find list" in said)
    check("and names the daily calls on a watchlist",
          "A daily call on the first %d stocks or coins you watch" % web.WATCH_SHOWN in said
          and web.WATCH_SHOWN == web.digest_mod.MAX_SYMBOLS)
    c.post("/billing/checkout", data={"plan": "yearly"})
    _, _, data = sent[-1]
    check("choosing yearly checks out at the yearly price, with no trial",
          data["line_items[0][price]"] == "price_y"
          and "subscription_data[trial_period_days]" not in data, data)

    with c.session_transaction() as sess:
        sess.clear()
    page = c.get("/pricing").data.decode("utf-8", "replace")
    check("a visitor is asked to make an account, not offered a trial",
          "Create account to upgrade" in page and "Try Pro free" not in page)

    print()
    print("=" * 72)
    print("A SECRET DISCOUNT CODE")
    print("=" * 72)
    state = {"used": 37, "nested": False, "asked": ""}

    def promo_stripe(method, path, data=None):
        coupon = {"id": "co_1", "percent_off": 50, "duration": "repeating",
                  "duration_in_months": 12, "max_redemptions": 100,
                  "times_redeemed": state["used"], "valid": True}
        if path.startswith("promotion_codes?"):
            state["asked"] = path
            promo = {"id": "promo_1", "code": "HALF", "active": True,
                     "max_redemptions": None, "times_redeemed": state["used"]}
            if state["nested"]:
                promo["coupon"] = coupon
            else:
                promo["promotion"] = {"type": "coupon", "coupon": "co_1"}
            return {"data": [promo]}
        if path == "coupons/co_1":
            return coupon
        return fake_stripe(method, path, data)

    billing._stripe = promo_stripe
    sign_in(newcomer)
    page = c.get("/pricing").data.decode("utf-8", "replace")
    check("the pricing page has an empty box for a code",
          'name="code"' in page and "HALF" not in page)
    check("and nothing anywhere names it",
          "HALF" not in c.get("/").data.decode("utf-8", "replace"))
    before = len(sent)
    r = c.post("/billing/checkout", data={"apply": "1", "code": "half"})
    page = r.data.decode("utf-8", "replace")
    check("pressing Apply says the code worked, and what it gives",
          r.status_code == 200 and "works: Pro" in page
          and "half price for your first year" in page)
    check("keeps it in the box, and sends no one to pay yet",
          'value="half"' in page
          and not any(p == "checkout/sessions" for _, p, _ in sent[before:]))
    check("the page can check a code without reloading",
          'data-check="/billing/code"' in c.get("/pricing").data.decode("utf-8", "replace"))
    before = len(sent)
    r = c.post("/billing/code", data={"code": "half"})
    said = r.data.decode("utf-8", "replace")
    check("which answers with just the line that goes under the box",
          r.status_code == 200 and "works: Pro" in said and "half price for your first year" in said
          and "<html" not in said
          and not any(p == "checkout/sessions" for _, p, _ in sent[before:]))
    r = c.post("/billing/checkout", data={"plan": "monthly", "code": "half "})
    _, _, data = sent[-1]
    check("a typed code is applied before Stripe's page opens",
          r.status_code == 303 and data.get("discounts[0][promotion_code]") == "promo_1"
          and "allow_promotion_codes" not in data, data)
    check("asking Stripe only for that code, and only if it is active",
          "code=half" in state["asked"] and "active=true" in state["asked"])
    state["nested"] = True
    c.post("/billing/checkout", data={"plan": "monthly", "code": "HALF"})
    check("older Stripe accounts nest the coupon, and that works too",
          sent[-1][2].get("discounts[0][promotion_code]") == "promo_1")
    c.post("/billing/checkout", data={"plan": "monthly", "code": ""})
    check("an empty box still lets Stripe's page take a code",
          sent[-1][2].get("allow_promotion_codes") == "true"
          and "discounts[0][promotion_code]" not in sent[-1][2])
    state["used"] = 100
    before = len(sent)
    r = c.post("/billing/checkout", data={"plan": "monthly", "code": "HALF"})
    check("a code that has run out says so, and sends no one to pay",
          r.status_code == 400 and "does not work, or it has run out"
          in r.data.decode("utf-8", "replace")
          and not any(p == "checkout/sessions" for _, p, _ in sent[before:]))
    r = c.post("/billing/checkout", data={"apply": "1", "code": "NOPE"})
    check("and so does a wrong one when Apply is pressed",
          r.status_code == 400 and "does not work" in r.data.decode("utf-8", "replace"))
    r = c.post("/billing/code", data={"code": "NOPE"})
    check("in place too", r.status_code == 400
          and "does not work" in r.data.decode("utf-8", "replace"))
    check("every such line is marked, so the page draws nothing else in its place",
          r.headers.get("X-Orenth-Part") == "code")
    r = web.app.test_client().post("/billing/code", data={"code": "half"})
    check("which the sign-in page, for a sign-in that has ended, is not",
          r.status_code == 302 and "/signin" in r.headers["Location"]
          and "X-Orenth-Part" not in r.headers, r.status_code)
    page = c.get("/pricing").data.decode("utf-8", "replace")
    check("the line's place is one screen readers watch, and both plans have headings",
          'data-said aria-live="polite"' in page
          and '<h2 class="eyebrow">Free</h2>' in page and '<h2 class="eyebrow">Pro</h2>' in page)

    print()
    print("   What Stripe will not take:")
    conn = db.connect(DB)
    gone = A.upsert_email_user(conn, "gone@example.com")
    billing.set_plan(conn, gone.id, "free", "canceled", "cus_gone")
    conn.close()
    sign_in(gone)
    stripe_has = {"customer": {"deleted": True}, "first_only": False, "limited_to": None}

    def picky_stripe(method, path, data=None):
        if path.startswith("customers/"):
            if stripe_has["customer"] is None:
                raise billing.BillingRefused("no such customer", 404)
            return stripe_has["customer"]
        if path == "checkout/sessions":
            sent.append((method, path, dict(data or {})))
            if data.get("customer") and stripe_has["customer"] in (None, {"deleted": True}):
                raise billing.BillingRefused("Payments refused the request.")
            if stripe_has["first_only"] and "discounts[0][promotion_code]" in data:
                raise billing.BillingRefused("Payments refused the request.")
            return {"url": "https://checkout.stripe.test/c/2"}
        if path.startswith("promotion_codes?"):
            return {"data": [{"id": "promo_2", "code": "FRIEND", "active": True,
                              "customer": stripe_has["limited_to"],
                              "coupon": {"percent_off": 25, "duration": "once",
                                         "valid": True}}]}
        return fake_stripe(method, path, data)

    billing._stripe = picky_stripe
    for customer, what in (({"deleted": True}, "deleted at Stripe"),
                           (None, "Stripe never heard of")):
        stripe_has["customer"] = customer
        r = c.post("/billing/checkout", data={"plan": "monthly"})
        check("an account whose customer was %s can still upgrade" % what,
              r.status_code == 303 and "customer" not in sent[-1][2]
              and sent[-1][2].get("customer_email") == "gone@example.com", r.status_code)
    stripe_has.update(customer={"id": "cus_gone"}, first_only=True)
    before = len(sent)
    r = c.post("/billing/checkout", data={"plan": "monthly", "code": "friend"})
    check("a code Stripe refuses at the last step is said to be the trouble",
          r.status_code == 400 and "would not take that code" in words_of(r)
          and 'value="friend"' in words_of(r), r.status_code)
    check("and a customer Stripe still has is never replaced by a second one",
          [d.get("customer") for _, p, d in sent[before:] if p == "checkout/sessions"]
          == ["cus_gone"])
    stripe_has.update(first_only=False, limited_to="cus_friend")
    r = c.post("/billing/code", data={"code": "friend"})
    check("a code made for one customer does not work for another", r.status_code == 400)
    stripe_has["limited_to"] = "cus_gone"
    r = c.post("/billing/code", data={"code": "friend"})
    check("but does for its own", r.status_code == 200 and "works: Pro" in words_of(r))
    billing._stripe = fake_stripe

    print()
    print("=" * 72)
    print("A DISCOUNT CODE CANNOT BE GUESSED AT SPEED")
    print("=" * 72)
    asked = []

    def no_such_codes(method, path, data=None):
        if path.startswith("promotion_codes?"):
            asked.append(path)
            return {"data": []}
        return fake_stripe(method, path, data)

    billing._stripe = no_such_codes
    billing._MISSES.clear()
    conn = db.connect(DB)
    guesser = A.upsert_email_user(conn, "guesser@example.com")
    conn.close()
    sign_in(guesser)
    first = c.post("/billing/code", data={"code": "GUESS0"}).status_code
    c.post("/billing/code", data={"code": "guess0"})
    check("the same wrong code twice asks Stripe once",
          first == 400 and len(asked) == 1, len(asked))
    for n in range(1, web.CODE_CHECKS_PER_HOUR - 1):
        c.post("/billing/code", data={"code": "GUESS%d" % n})
    before = len(asked)
    r = c.post("/billing/code", data={"code": "GUESS99"})
    check("past ten tries in an hour, a code is not even checked",
          r.status_code == 429 and len(asked) == before
          and "Too many codes" in r.data.decode("utf-8", "replace"), r.status_code)
    r = c.post("/billing/checkout", data={"plan": "monthly", "code": "GUESS100"})
    check("and the checkout form counts against the same tries",
          r.status_code == 429 and len(asked) == before, r.status_code)
    r = c.post("/billing/checkout", data={"plan": "monthly", "code": ""})
    check("while paying full price is never held up", r.status_code == 303,
          r.status_code)
    billing._stripe = fake_stripe

    print()
    print("   Nor can one account use up what Stripe allows everyone:")
    conn = db.connect(DB)
    looper = A.upsert_email_user(conn, "looper@example.com")
    billing.set_plan(conn, looper.id, "free", "canceled", "cus_looper")
    conn.close()
    sign_in(looper)
    web._RATE.clear()
    # The last one of each is past the budget.
    for what, ask, inside, past in (
            ("checkout", lambda: c.post("/billing/checkout"), 303, 429),
            ("the portal", lambda: c.post("/billing/portal"), 303, 429),
            ("the return page", lambda: c.get("/billing/done?session_id=cs_test_1"), 302, 200)):
        before = len(sent)
        got = [ask().status_code for _ in range(web.STRIPE_ASKS_PER_HOUR)]
        calls = len([s for s in sent[before:] if not s[1].startswith("prices/")])
        last = ask()
        check("%s is asked of Stripe %d times an hour and no more"
              % (what, web.STRIPE_ASKS_PER_HOUR),
              set(got) == {inside} and last.status_code == past
              and calls == web.STRIPE_ASKS_PER_HOUR
              and not [s for s in sent[before:][calls:] if not s[1].startswith("prices/")],
              (set(got), last.status_code, calls))
    check("and the return page then says neither that it was paid nor that it was not",
          "We are confirming your payment" in words_of(last))
    web._RATE.clear()

    print()
    print("   A wrong signing secret is heard of:")
    from bot import monitoring
    noted, warned = [], []

    import logging

    class Warned(logging.Handler):
        def emit(self, record):
            warned.append(record.getMessage())

    real_note, handlers = monitoring.note, web.app.logger.handlers[:]
    monitoring.note = lambda message, **k: noted.append(message)
    web.app.logger.handlers = [Warned()]
    web._REFUSED.clear()
    try:
        for _ in range(20):
            c.post("/billing/webhook", data=body,
                   headers={"Stripe-Signature": signed(body, "whsec_forged")})
    finally:
        monitoring.note, web.app.logger.handlers = real_note, handlers
    check("refused messages from Stripe are logged, a few an hour, and reported once",
          len(warned) == web.REFUSALS_SAID and len(noted) == 1
          and "STRIPE_WEBHOOK_SECRET" in noted[0], (len(warned), noted))
    web._REFUSED["stripe"] = (time.time() - 3601, 20)
    check("and counted afresh the next hour", web._refusals("stripe") == 1)
    _os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE["STRIPE_WEBHOOK_SECRET"] + "\n"
    quiet = json.dumps({"type": "nothing.to.do"}).encode()
    r = c.post("/billing/webhook", data=quiet, headers={"Stripe-Signature": signed(quiet)})
    check("a secret pasted with a line break after it still checks out",
          r.status_code == 200, r.status_code)
    _os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE["STRIPE_WEBHOOK_SECRET"]

    print()
    print("=" * 72)
    print("PAID AND SLOW REQUESTS HAVE AN HOURLY BUDGET")
    print("=" * 72)
    from types import SimpleNamespace as NS
    from bot.vision import ChartRead

    conn = db.connect(DB)
    heavy = A.upsert_email_user(conn, "heavy@example.com")
    billing.set_plan(conn, heavy.id, "pro", "active", "cus_heavy")
    conn.close()
    sign_in(heavy)
    had_key = _os.environ.get("ANTHROPIC_API_KEY")
    _os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    real_read, real_research = web.read_chart, web.engine.research
    web.read_chart = lambda png: ChartRead(symbol="AAPL")
    try:
        got = [c.post("/analyse/image", data={"chart": (png_bytes(), "c.png"), "ai_ok": "1"},
                      content_type="multipart/form-data").status_code
               for _ in range(web.IMAGE_READS_PER_HOUR)]
        check("a Pro account can read screenshots", set(got) == {302}, got)
        r = c.post("/analyse/image", data={"chart": (png_bytes(), "c.png"), "ai_ok": "1"},
                   content_type="multipart/form-data")
        check("but not without end, since every read is paid for",
              r.status_code == 429, r.status_code)

        found = NS(thesis=NS(balance=0.3, verdict="Leans bullish",
                             bull=NS(points=[]), bear=NS(points=[]),
                             falsifiers=["A close below 90"], all_sources=[]))
        web.cached_analysis = lambda *a, **k: NS(bars=NS(last_price=101.5))
        web.engine.research = lambda *a, **k: found
        saved_budget, web.THESES_PER_HOUR = web.THESES_PER_HOUR, 3
        r = c.post("/thesis/save", data={"symbol": "acme", "interval": "1d",
                                         "note": "n" * 5000})
        conn = db.connect(DB)
        kept = db.thesis_list(conn, user=heavy.id)
        conn.close()
        check("a thesis saves", r.status_code == 302 and len(kept) == 1, r.status_code)
        check("with a long note cut to a reminder's length",
              kept and len(kept[0]["user_note"]) == web.NOTE_LIMIT)

        def locked(*a, **k):
            import sqlite3
            raise sqlite3.OperationalError("database is locked: /data/stockbot.db")

        import logging

        class Keep(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        logged, handlers = [], web.app.logger.handlers[:]
        web.app.logger.handlers = [Keep()]
        web.engine.research = locked
        try:
            r = c.post("/thesis/save", data={"symbol": "ACME", "interval": "1d"})
        finally:
            web.app.logger.handlers = handlers
        page = r.data.decode("utf-8", "replace")
        check("a failure says so plainly, without the error's own text",
              r.status_code == 500 and "could not be saved" in page
              and "stockbot.db" not in page and "database is locked" not in page,
              r.status_code)
        check("while the error itself goes to the log",
              any("Saving a thesis for ACME failed" in m for m in logged), logged)
        web.engine.research = lambda *a, **k: found
        r = c.post("/thesis/save", data={"symbol": "ACME", "interval": "forever"})
        check("a timeframe that does not exist is refused", r.status_code == 400)
        c.post("/thesis/save", data={"symbol": "ACME", "interval": "1d"})
        r = c.post("/thesis/save", data={"symbol": "ACME", "interval": "1d"})
        check("and saving is held to an hourly budget too", r.status_code == 429,
              r.status_code)
        web.THESES_PER_HOUR = saved_budget
    finally:
        web.read_chart, web.engine.research = real_read, real_research
        web.cached_analysis = real_analysis
        if had_key is None:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _os.environ["ANTHROPIC_API_KEY"] = had_key
finally:
    billing._stripe = real
    for key in list(STRIPE) + ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]:
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("BILLING OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
