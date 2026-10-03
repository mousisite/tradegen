"""Paid plans: off by default, signed webhooks only, and limits that hold."""
import hashlib
import hmac
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
for key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL", "PRO_TRIAL_DAYS"]:
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

    def fake_stripe(method, path, data=None):
        sent.append((method, path, dict(data or {})))
        if path.startswith("prices/"):
            return PRICES[path.split("/", 1)[1]]
        if path == "checkout/sessions":
            return {"url": "https://checkout.stripe.test/c/1", "id": "cs_test_1"}
        if path.startswith("checkout/sessions/"):
            return {"status": "complete", "client_reference_id": str(paid.id),
                    "customer": "cus_paid", "payment_status": "paid"}
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

    conn = db.connect(DB)
    db.alert_add(conn, "AAPL", "price_above", 999999.0, user=free.id)
    conn.close()
    r = c.post("/monitor/alert/add", data={"symbol": "MSFT", "kind": "price_above",
                                           "threshold": "1"})
    check("a free account cannot run more alerts than its allowance",
          r.status_code == 402, r.status_code)

    # Find shows a free account the top of each list.
    real_find = web.ideas_mod.find

    def fake_find(market, horizon, cfg, risk):
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

    sign_in(paid)
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
    print("PRICES, PLANS AND THE FREE TRIAL")
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
    check("offers a first-timer the free trial",
          "Free for 7 days" in page and "Start free trial" in page)
    check("and says plainly that Pro is not better odds", "not better odds" in page)
    c.post("/billing/checkout", data={"plan": "yearly"})
    _, _, data = sent[-1]
    check("choosing yearly checks out at the yearly price, with the trial",
          data["line_items[0][price]"] == "price_y"
          and data.get("subscription_data[trial_period_days]") == "7", data)

    sign_in(free)
    c.post("/billing/checkout", data={"plan": "monthly"})
    check("someone who has paid before gets no second trial",
          "subscription_data[trial_period_days]" not in sent[-1][2])
    check("and is not offered one",
          "Free for 7 days" not in c.get("/pricing").data.decode("utf-8", "replace"))

    _os.environ["PRO_TRIAL_DAYS"] = "0"
    sign_in(newcomer)
    c.post("/billing/checkout")
    check("a trial length of 0 switches trials off",
          "subscription_data[trial_period_days]" not in sent[-1][2])
    _os.environ.pop("PRO_TRIAL_DAYS", None)

    with c.session_transaction() as sess:
        sess.clear()
    page = c.get("/pricing").data.decode("utf-8", "replace")
    check("a visitor is invited to try it free", "Try Pro free for 7 days" in page)
finally:
    billing._stripe = real
    for key in list(STRIPE) + ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]:
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("BILLING OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
