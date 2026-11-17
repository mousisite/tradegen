"""The iPhone app: what the site draws inside it, and the App Review sign-in.

The app is the live site in a native shell, and it adds "OrenthApp/1" to its
User-Agent. Inside it there must be no third-party visit counting, no Stripe
form, link, price or discount box (the App Store allows selling Pro there only
through Apple), nothing about Android or installing an app, and the small
script that reaches the phone. Outside it, nothing changes.

Apple's reviewers cannot receive an email or a text, so one named account can
be signed in to with a fixed code. That door must not exist until it is set
up, must open only that one account, must never make it Pro, and must not
let anyone guess at the code.
"""
import os as _os
import sys as _sys
from urllib.parse import urlparse

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
REVIEW = ("REVIEW_LOGIN_EMAIL", "REVIEW_LOGIN_CODE")
for key in list(STRIPE) + list(REVIEW) + ["STOCKBOT_ADMIN_EMAIL"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402
from bot import invites  # noqa: E402

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

    print()
    print("=" * 72)
    print("THE REVIEW SIGN-IN DOES NOT EXIST UNTIL IT IS SET UP")
    print("=" * 72)
    visitor = web.app.test_client()
    check("no settings: the page is not found",
          visitor.get("/signin/review").status_code == 404)
    check("nor can anything be posted to it",
          visitor.post("/signin/review", data={"email": "x@example.com",
                                               "code": "x" * 20}).status_code == 404)
    _os.environ["REVIEW_LOGIN_EMAIL"] = "appreview@example.com"
    _os.environ["REVIEW_LOGIN_CODE"] = "short-code"
    check("a code shorter than %d characters keeps it off" % web.REVIEW_CODE_MIN,
          visitor.get("/signin/review").status_code == 404)

    CODE = "Kq3-x8Rw-2mZ7-pL5v-T9"
    _os.environ["REVIEW_LOGIN_CODE"] = CODE
    web._RATE.clear()
    page = visitor.get("/signin/review", headers=IN_APP)
    body = text(page)
    check("set up, it shows a small form", page.status_code == 200
          and 'name="email"' in body and 'name="code"' in body, page.status_code)
    check("which asks search engines to leave it out", 'content="noindex"' in body)
    for path in ("/", "/signin", "/sitemap.xml", "/robots.txt", "/privacy"):
        check("and no page links to it (%s)" % path,
              "/signin/review" not in text(visitor.get(path)))

    print()
    print("=" * 72)
    print("WRONG ANSWERS ALL GET THE SAME REPLY")
    print("=" * 72)
    wrong_code = visitor.post("/signin/review", data={
        "email": "appreview@example.com", "code": CODE + "x"})
    wrong_email = visitor.post("/signin/review", data={
        "email": "someone@example.com", "code": CODE})
    empty = visitor.post("/signin/review", data={})
    check("a wrong code is refused", wrong_code.status_code == 400,
          wrong_code.status_code)
    check("a wrong address is refused", wrong_email.status_code == 400,
          wrong_email.status_code)
    check("an empty form is refused", empty.status_code == 400, empty.status_code)
    check("with the same words for all three",
          web.REVIEW_ERROR in text(wrong_code) and web.REVIEW_ERROR in text(wrong_email)
          and web.REVIEW_ERROR in text(empty))
    with visitor.session_transaction() as sess:
        check("and nobody is signed in", web.SESSION_KEY not in sess)

    print()
    print("=" * 72)
    print("THE RIGHT ANSWER OPENS THE ONE REVIEW ACCOUNT, AS FREE")
    print("=" * 72)
    web._RATE.clear()
    # An invite link opened earlier in the same browser would otherwise give
    # the new account a free week of Pro.
    conn = db.connect(DB)
    ref = invites.code_for(conn, free.id)
    conn.close()
    with visitor.session_transaction() as sess:
        sess["ref"] = ref
    right = visitor.post("/signin/review", data={
        "email": "  AppReview@Example.com ", "code": CODE}, headers=IN_APP)
    check("the address in any case, with the code, signs in",
          right.status_code == 302, right.status_code)
    check("and lands on the front page",
          urlparse(right.headers.get("Location", "")).path == "/",
          right.headers.get("Location"))
    conn = db.connect(DB)
    row = conn.execute("SELECT * FROM users WHERE email=?",
                       ("appreview@example.com",)).fetchone()
    reviewer = A.get_user(conn, row["id"]) if row else None
    conn.close()
    check("the account was made on first use", reviewer is not None)
    check("with the email provider, not local and not the example",
          reviewer is not None and reviewer.provider == "email"
          and not reviewer.is_local)
    check("and it is not Pro", reviewer is not None and reviewer.plan == "free"
          and not billing.is_pro(reviewer))
    check("not even from the invite link in the browser",
          reviewer is not None and reviewer.bonus_until == 0)
    account = visitor.get("/account", headers=IN_APP)
    check("the session works: the account page opens",
          account.status_code == 200 and "appreview@example.com" in text(account),
          account.status_code)
    check("and a signed-in visitor is sent on from the review page",
          visitor.get("/signin/review").status_code == 302)

    again = web.app.test_client()
    web._RATE.clear()
    second = again.post("/signin/review", data={
        "email": "appreview@example.com", "code": CODE})
    conn = db.connect(DB)
    count = conn.execute("SELECT COUNT(*) FROM users WHERE email=?",
                         ("appreview@example.com",)).fetchone()[0]
    conn.close()
    check("signing in again reaches the same account",
          second.status_code == 302 and count == 1, (second.status_code, count))

    print()
    print("=" * 72)
    print("A FIXED CODE NEVER OPENS ANYONE ELSE'S ACCOUNT")
    print("=" * 72)
    for label, email, prepare in (
            ("the local account", A.LOCAL_EMAIL, None),
            ("the example account", "example@orenth.invalid", "demo"),
            ("an operator", "boss@example.com", "operator"),
            ("an account made with Google", "googler@example.com", "google")):
        web._RATE.clear()
        conn = db.connect(DB)
        if prepare == "demo":
            from bot import demo
            demo.user(conn)
        elif prepare == "operator":
            A.upsert_email_user(conn, "boss@example.com")
            _os.environ["STOCKBOT_ADMIN_EMAIL"] = "boss@example.com"
        elif prepare == "google":
            A.upsert_google_user(conn, {"sub": "g-1", "email": "googler@example.com",
                                        "email_verified": True, "name": "G"})
        conn.close()
        _os.environ["REVIEW_LOGIN_EMAIL"] = email
        stranger = web.app.test_client()
        tried = stranger.post("/signin/review", data={"email": email, "code": CODE})
        with stranger.session_transaction() as sess:
            signed = web.SESSION_KEY in sess
        check("%s cannot be opened with it" % label,
              tried.status_code == 400 and not signed
              and web.REVIEW_ERROR in text(tried), (tried.status_code, signed))
        _os.environ.pop("STOCKBOT_ADMIN_EMAIL", None)
    _os.environ["REVIEW_LOGIN_EMAIL"] = "appreview@example.com"

    print()
    print("=" * 72)
    print("GUESSING IS LIMITED PER NETWORK")
    print("=" * 72)
    web._RATE.clear()
    guesser = web.app.test_client()
    budget = web.REVIEW_TRIES_PER_HOUR * 3
    answers = [guesser.post("/signin/review", data={
        "email": "appreview@example.com", "code": "guess-%02d-xxxxxxxxxx" % n},
        environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code
        for n in range(budget + 2)]
    check("the first %d wrong tries are each just refused" % budget,
          answers[:budget] == [400] * budget, answers)
    check("after that the network is told to wait", answers[budget:] == [429, 429],
          answers[budget:])
    late = guesser.post("/signin/review", data={
        "email": "appreview@example.com", "code": CODE},
        environ_base={"REMOTE_ADDR": "203.0.113.9"})
    check("even the right code waits once the limit is reached",
          late.status_code == 429, late.status_code)
    elsewhere = web.app.test_client().post("/signin/review", data={
        "email": "appreview@example.com", "code": CODE},
        environ_base={"REMOTE_ADDR": "198.51.100.7"})
    check("while another network can still sign in",
          elsewhere.status_code == 302, elsewhere.status_code)
finally:
    billing._stripe = real_stripe
    for key in list(STRIPE) + list(REVIEW) + ["STOCKBOT_ADMIN_EMAIL"]:
        _os.environ.pop(key, None)

print()
if fails:
    print("FAILED: %d" % len(fails))
    for name in fails:
        print("  - %s" % name)
    _sys.exit(1)
print("APP MODE OK")
