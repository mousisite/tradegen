"""The iPhone app: what the site draws inside it.

The app is the live site in a native shell, and it adds "OrenthApp/1" to its
User-Agent. Inside it there must be no third-party visit counting, no Stripe
form, link, price or discount box (the App Store allows selling Pro there only
through Apple), nothing about Android or installing an app, and the small
script that reaches the phone. Outside it, nothing changes.
"""
import os as _os
import sys as _sys

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
          "STRIPE_PRICE_ID_YEARLY": "price_y", "STRIPE_WEBHOOK_SECRET": "whsec_test"}
for key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402

c, DB = harness.isolated_web("app-mode")
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"

APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 OrenthApp/1")
SAFARI_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 "
             "Mobile/15E148 Safari/604.1")
IN_APP = {"User-Agent": APP_UA}
IN_SAFARI = {"User-Agent": SAFARI_UA}

# Words that must never reach a page drawn inside the app.
NEVER_IN_APP = ("stripe", "metricool", "android", "google play", "play store",
                "install the app", "install app", "/billing/checkout",
                "/billing/portal", "/billing/code", "discount code",
                "$9.99", "$79.99", "manage subscription")

conn = db.connect(DB)
free = A.upsert_email_user(conn, "free@example.com")
paid = A.upsert_email_user(conn, "paid@example.com")
conn.close()


def sign_in(client, user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


def text(response):
    return response.data.decode("utf-8", "replace")


def leaks(body):
    low = body.lower()
    return [w for w in NEVER_IN_APP if w in low]


# A stand-in for Stripe, so nothing reaches the network, counting every call.
sent = []
PRICES = {"price_x": {"unit_amount": 999, "currency": "usd",
                      "recurring": {"interval": "month", "interval_count": 1}},
          "price_y": {"unit_amount": 7999, "currency": "usd",
                      "recurring": {"interval": "year", "interval_count": 1}}}


def fake_stripe(method, path, data=None):
    sent.append((method, path))
    if path.startswith("prices/"):
        return PRICES[path.split("/", 1)[1]]
    if path == "checkout/sessions":
        return {"url": "https://checkout.stripe.test/c/1", "id": "cs_test_1"}
    if path.startswith("promotion_codes"):
        return {"data": []}
    return {"url": "https://billing.stripe.test/p/1"}


real_stripe = billing._stripe
billing._stripe = fake_stripe

try:
    print("=" * 72)
    print("THE APP IS RECOGNISED BY ITS USER-AGENT, AND NOTHING ELSE")
    print("=" * 72)
    with web.app.test_request_context("/", headers=IN_APP):
        check("the app's User-Agent is the app", web._in_app() is True)
    with web.app.test_request_context("/", headers=IN_SAFARI):
        check("Safari on the same phone is not", web._in_app() is False)
    with web.app.test_request_context("/"):
        check("no User-Agent at all is not", web._in_app() is False)

    page = text(c.get("/privacy", headers=IN_APP))
    check("an app page loads the phone script",
          "/static/native.js" in page, "native.js missing")
    page = text(c.get("/privacy", headers=IN_SAFARI))
    check("a browser page does not", "native.js" not in page)
    script = c.get("/static/native.js")
    body = text(script)
    check("the script is served", script.status_code == 200, script.status_code)
    check("and does nothing outside the native app",
          "isNativePlatform" in body and "OrenthNative" in body)
    check("and passes on the notifications hook",
          "OrenthNotify" in body and "onAppStart" in body)
    script.close()

    print()
    print("=" * 72)
    print("NO THIRD-PARTY COUNTING INSIDE THE APP")
    print("=" * 72)
    outside = text(c.get("/", headers=IN_SAFARI))
    inside = text(c.get("/", headers=IN_APP))
    check("a signed-out visit is counted in a browser", "metricool" in outside)
    check("but never inside the app", "metricool" not in inside.lower())
    check("the front page inside the app says nothing it must not",
          not leaks(inside), leaks(inside))
    signin = text(c.get("/signin", headers=IN_APP))
    check("nor does the sign-in page", not leaks(signin), leaks(signin))

    print()
    print("=" * 72)
    print("BILLING ON: STRIPE STAYS OUT OF THE APP")
    print("=" * 72)
    _os.environ.update(STRIPE)
    check("billing is on for these checks", billing.ready())

    outside = c.get("/pricing", headers=IN_SAFARI)
    page = text(outside)
    check("a browser sees the prices", "$9.99" in page and "$79.99" in page,
          page[:200])
    del sent[:]
    inside = c.get("/pricing", headers=IN_APP)
    page = text(inside)
    check("the pricing page opens inside the app", inside.status_code == 200,
          inside.status_code)
    check("with no web price, Stripe or discount box", not leaks(page), leaks(page))
    check("and Stripe is not even asked for the prices", not sent, sent)
    check("the app's purchase slot is there, for Apple's buttons",
          "data-app-purchase" in page)

    for who, user in (("a free account", free), ("a Pro account", paid)):
        client = web.app.test_client()
        sign_in(client, user)
        if user is paid:
            conn = db.connect(DB)
            billing.set_plan(conn, paid.id, "pro", "active", "cus_paid")
            conn.close()
        for path in ("/", "/pricing", "/account", "/monitor", "/ideas"):
            response = client.get(path, headers=IN_APP)
            page = text(response)
            check("%s: %s inside the app shows no Stripe or Android" % (who, path),
                  response.status_code in (200, 402) and not leaks(page),
                  (response.status_code, leaks(page)))
        if user is paid:
            browser = text(client.get("/account", headers=IN_SAFARI))
            check("a Pro account still manages Stripe from a browser",
                  "Manage subscription" in browser)
        else:
            browser = text(client.get("/pricing", headers=IN_SAFARI))
            check("a free account still upgrades from a browser",
                  "/billing/checkout" in browser and "Discount code" in browser)
            app_page = text(client.get("/pricing", headers=IN_APP))
            check("but inside the app it gets only the purchase slot",
                  "data-app-purchase" in app_page
                  and "/billing/checkout" not in app_page)

    print()
    print("=" * 72)
    print("THE STRIPE ROUTES REFUSE INSIDE THE APP")
    print("=" * 72)
    client = web.app.test_client()
    sign_in(client, free)
    del sent[:]
    for path, form in (("/billing/checkout", {"plan": "monthly"}),
                       ("/billing/portal", {}),
                       ("/billing/code", {"code": "FRIEND"})):
        response = client.post(path, data=form, headers=IN_APP)
        check("%s is not found inside the app" % path, response.status_code == 404,
              response.status_code)
    check("and none of them reached Stripe", not sent, sent)
    response = client.post("/billing/checkout", data={"plan": "monthly"},
                           headers=IN_SAFARI)
    check("the same checkout still works in a browser",
          response.status_code == 303
          and "checkout.stripe.test" in response.headers.get("Location", ""),
          (response.status_code, response.headers.get("Location")))

finally:
    billing._stripe = real_stripe
    for key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL"]:
        _os.environ.pop(key, None)

print()
if fails:
    print("FAILED: %d" % len(fails))
    for name in fails:
        print("  - %s" % name)
    _sys.exit(1)
print("APP MODE OK")
