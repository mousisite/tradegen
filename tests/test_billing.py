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
        web._spend_view("aapl", "1d")
        web._spend_view("AAPL", "1d")
    conn = db.connect(DB)
    check("one that is shown does, once however often it is opened",
          billing.views_today(conn, free.id) == 1)

    limit = billing.free_limits()["analyses_per_day"]
    for n in range(limit):
        billing.count_view(conn, free.id, "SYM%d|1d" % n)
    check("the allowance can be used up", not billing.may_view(conn, free.id, "NEW|1d"))
    check("but an instrument already opened today stays open",
          billing.may_view(conn, free.id, "SYM0|1d"))
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
            if web._allowance_left("RACE%d" % n, "1d"):
                got.append(n)
                web._spend_view("RACE%d" % n, "1d")

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
        check("a free account sees the top of the Find list", 0 < shown <= 6, shown)
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
    conn.close()

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
    billing._stripe = fake_stripe

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
