"""The iPhone app: what the site draws inside it, and the App Review sign-in.

The app is the live site in a native shell, and it adds "OrenthApp/1" to its
User-Agent. Inside it there must be no third-party visit counting, no Stripe
form, link, price or discount box (the App Store allows selling Pro there only
through Apple), nothing about Android or installing an app, and the small
script that reaches the phone. Once Pro is sold in the app, it is sold
through Apple alone: a "Manage subscription" there is Apple's, every way to
Pro leads to the app's own screen, and no page names a price StoreKit did not
give. Outside it, nothing changes.

Apple's reviewers cannot receive an email or a text, so one named account can
be signed in to with a fixed code. That door must not exist until it is set
up, must open only that one account, must never make it Pro, and must not
let anyone guess at the code.
"""
import logging
import os as _os
import re
import sys as _sys
import time
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
SMTP = {"SMTP_HOST": "smtp.example.test", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "MAIL_FROM": "codes@example.com"}
REVIEW = ("REVIEW_LOGIN_EMAIL", "REVIEW_LOGIN_CODE")
APPLE = ("APPLE_APP_ID", "APPLE_PRODUCT_IDS", "APPLE_SANDBOX_PRO", "APPLE_SANDBOX_USERS")
for key in list(STRIPE) + list(REVIEW) + list(APPLE) + list(SMTP) + ["STOCKBOT_ADMIN_EMAIL"]:
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

# Words that must never reach a page drawn inside the app. "Manage
# subscription" may, but only as Apple's (see apple_only below).
NEVER_IN_APP = ("stripe", "metricool", "android", "google play", "play store",
                "install the app", "install app", "install orenth",
                "add to home screen", "to your home screen", "/billing/checkout",
                "/billing/portal", "/billing/code", "discount code",
                "$9.99", "$79.99", "buy on the website", "subscribe on the website")
# What no page inside the app may say, the privacy policy included. It has
# to name Stripe and the website's visit counter among the services that get
# data, but another phone's system, or an app to install, has no place in an
# App Store app (rule 2.3.10), and the subscribe screen links to it.
OTHER_PLATFORMS = ("android", "google play", "play store", "install the app",
                   "install app", "install orenth", "add to home screen",
                   "to your home screen")
# A price written into a page. Inside the app only StoreKit may show one.
PRICE = re.compile(r"[$£€]\s?\d")
# A link to tap out of the app, to a site's pricing or billing pages. The
# page's own <link rel="canonical"> names its address, and is not one.
WEB_BUYING = re.compile(r"""<a\b[^>]*href=["']https?://[^"']*/(pricing|billing)""", re.I)
MANAGE = re.compile(r"<(a|button)\b([^>]*)>\s*Manage subscription", re.I)

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


def other_platforms(body):
    low = body.lower()
    return [w for w in OTHER_PLATFORMS if w in low]


def leaks(body):
    low = body.lower()
    found = [w for w in NEVER_IN_APP if w in low]
    if PRICE.search(body):
        found.append("a price: " + PRICE.search(body).group(0))
    if WEB_BUYING.search(body):
        found.append("a link to buy on the web: " + WEB_BUYING.search(body).group(0))
    found += ["a Manage subscription that is not Apple's"
              for _tag, attrs in MANAGE.findall(body) if "data-iap-manage" not in attrs]
    return found


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
    check("and the website's privacy page still names every push service's systems",
          '"Chrome on Android"' in page and "Google's for Chrome and Android" in " ".join(
              page.split()))
    page = text(c.get("/privacy", headers=IN_APP))
    check("while inside the app it names no other phone's system",
          not other_platforms(page) and '"iPhone app"' in page, other_platforms(page))
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
    print("INSIDE THE APP, THE BAR ALONG THE BOTTOM IS THE WAY ROUND")
    print("=" * 72)
    # App Review turns down an app that is its website as it is (rule 4.2).
    # A row of a website's links under every page, and a skip link, read as
    # one: inside the app the footer keeps Terms and Privacy, and the bar's
    # More holds the rest.
    PUBLIC_PAGES = {"/stocks", "/backtests", "/record"}

    def links_in(page, start, end):
        at = page.find(start)
        part = page[at:page.find(end, at)] if at >= 0 else ""
        return re.findall(r'href="([^"]+)"', part)

    member = web.app.test_client()
    sign_in(member, free)
    for who, client in (("a visitor", c), ("a free account", member)):
        on_web = text(client.get("/", headers=IN_SAFARI))
        inside = text(client.get("/", headers=IN_APP))
        footer_web = links_in(on_web, "<footer>", "</footer>")
        footer_app = links_in(inside, "<footer>", "</footer>")
        more_web = links_in(on_web, '<details class="more">', "</details>")
        more_app = links_in(inside, '<details class="more">', "</details>")
        check("%s in a browser: the footer links the public pages, and a skip link "
              "leads the page" % who, PUBLIC_PAGES <= set(footer_web)
              and 'class="skip"' in on_web, footer_web)
        check("%s inside the app: the footer has only Terms and Privacy" % who,
              footer_app == ["/terms", "/privacy"], footer_app)
        check("%s inside the app: no skip link" % who, 'class="skip"' not in inside)
        check("%s inside the app: the bar's More has the public pages instead" % who,
              PUBLIC_PAGES <= set(more_app) and not PUBLIC_PAGES & set(more_web),
              (more_app, more_web))
        check("%s inside the app: the bar has the four places and More" % who,
              re.findall(r'<nav class="tabbar"[^>]*>', inside)
              and [h for h in links_in(inside, '<nav class="tabbar"', '<details')]
              == ["/", "/ideas", "/trades", "/monitor"])
    # The screenshot box spoke of clicking, dragging and Ctrl+V. What it says,
    # not the page's script, which is the same in both.
    def shown(page):
        return " ".join(re.sub(r"(?is)<script\b.*?</script>", " ", page).split())

    _os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    try:
        on_web = shown(text(member.get("/", headers=IN_SAFARI)))
        inside = shown(text(member.get("/", headers=IN_APP)))
    finally:
        _os.environ.pop("ANTHROPIC_API_KEY", None)
    check("in a browser the screenshot box says click, drag or paste",
          "Click to browse, drag an image in, or paste with Ctrl+V" in on_web)
    check("inside the app it says tap, or take a photo",
          "Tap to pick one, or take a photo of a chart." in inside
          and "Ctrl+V" not in inside and "Click to browse" not in inside)

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
        for path in ("/", "/pricing", "/account", "/monitor", "/ideas", "/settings"):
            response = client.get(path, headers=IN_APP)
            page = text(response)
            check("%s: %s inside the app shows no Stripe or Android" % (who, path),
                  response.status_code in (200, 402) and not leaks(page),
                  (response.status_code, leaks(page)))
        for path in ("/privacy", "/terms"):
            page = text(client.get(path, headers=IN_APP))
            check("%s: %s inside the app names no other phone's system" % (who, path),
                  not other_platforms(page), other_platforms(page))
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
    print("NO FILE TO DOWNLOAD INSIDE THE APP, AND ONE LINE SAYING WHERE")
    print("=" * 72)
    # The app's one web view shows a CSV or JSON answer as bare text, with
    # no way to keep it and no way back but a swipe. The website keeps every
    # download, and the app says so where the buttons were.
    conn = db.connect(DB)
    db.open_trade(conn, "AAPL", "1d", 1, 100.0, 95.0, user=free.id)
    conn.close()
    FILES = re.compile(r'href="([^"]*(?:/export/|\.csv|/api/analyse)[^"]*)"')
    for path in ("/account", "/settings", "/record"):
        on_web = text(client.get(path, headers=IN_SAFARI))
        in_app = text(client.get(path, headers=IN_APP))
        check("%s in a browser offers its files" % path, FILES.search(on_web) is not None)
        check("%s inside the app offers none" % path, FILES.search(in_app) is None,
              FILES.findall(in_app))
        check("and says they are on the Orenth website", "on the Orenth website"
              in " ".join(in_app.split()))
    for path, said in (("/pricing", "analysis history from the website, any time"),
                       ("/privacy", "from the Account and Settings pages of the Orenth "
                                    "website, in a browser"),
                       ("/terms", "export your data from the Account page of the "
                                  "Orenth website")):
        in_app = " ".join(text(client.get(path, headers=IN_APP)).split())
        check("%s inside the app says downloads are on the website" % path,
              said in in_app)

    print()
    print("=" * 72)
    print("PRO SOLD IN THE APP: THROUGH APPLE, AND NOTHING ELSE")
    print("=" * 72)
    from bot import apple_store  # noqa: E402
    page = text(client.get("/pricing", headers=IN_APP))
    check("while it is off, the app's screen says so and offers nothing",
          "Upgrading is not available in the app yet" in page
          and "Restore Purchases" not in page and not leaks(page), leaks(page))
    check("and the app's bar has no Go Pro it cannot sell",
          ">Go Pro<" not in text(client.get("/monitor", headers=IN_APP)))

    _os.environ["APPLE_APP_ID"] = "6740000001"
    conn = db.connect(DB)
    applesub = A.upsert_email_user(conn, "apple-sub@example.com")
    apple_store.ensure_schema(conn)
    now_ms = int(time.time() * 1000)
    conn.execute("INSERT INTO apple_subs (environment, original_tx_id, user_id, product_id, "
                 "transaction_id, purchase_ms, expires_ms, tx_signed_ms, updated) "
                 "VALUES ('Production', 'app-mode-1', ?, 'app.orenth.ios.pro.yearly', "
                 "'app-mode-1', ?, ?, ?, ?)",
                 (applesub.id, now_ms - 86400000, now_ms + 300 * 86400000, now_ms,
                  int(time.time())))
    conn.commit()
    apple_store.prepare(conn)
    conn.close()
    try:
        for who, user in (("a free account", free), ("a Stripe Pro account", paid),
                          ("an Apple Pro account", applesub)):
            client = web.app.test_client()
            sign_in(client, user)
            # The Terms too: the subscribe screen links to them.
            for path in ("/", "/pricing", "/account", "/monitor", "/ideas", "/terms",
                         "/settings"):
                response = client.get(path, headers=IN_APP)
                page = text(response)
                check("%s: %s inside the app sells only through Apple" % (who, path),
                      response.status_code in (200, 402) and not leaks(page),
                      (response.status_code, leaks(page)))
                check("%s: %s inside the app carries the purchase script" % (who, path),
                      "/static/app-purchase.js" in page)
            # And the Privacy Policy, which it links to as well.
            page = text(client.get("/privacy", headers=IN_APP))
            check("%s: /privacy inside the app names no other phone's system" % who,
                  not other_platforms(page), other_platforms(page))
            pricing = text(client.get("/pricing", headers=IN_APP))
            if user is free:
                check("a free account gets Apple's subscribe screen",
                      "data-iap-shop" in pricing and "Restore Purchases" in pricing
                      and "stdeula" in pricing and 'href="/privacy"' in pricing
                      and 'href="/terms"' in pricing)
                for page_path in ("/monitor", "/account"):
                    found = text(client.get(page_path, headers=IN_APP))
                    links = re.findall(r'href="([^"]*)"[^>]*>\s*(?:Go Pro|See Pro)\s*<', found)
                    check("its way to Pro on %s is the app's own screen" % page_path,
                          links and set(links) == {"/pricing"}, links)
                browser = text(client.get("/pricing", headers=IN_SAFARI))
                check("while a browser still gets the website's checkout, and no app script",
                      "/billing/checkout" in browser and "app-purchase.js" not in browser)
            elif user is paid:
                check("a Stripe Pro account is sold nothing in the app",
                      "data-iap-shop" not in pricing and "already has Pro" in pricing)
            else:
                check("an Apple Pro account is told it has Pro through Apple",
                      "You have Pro through Apple" in pricing
                      and "data-iap-manage" in pricing and "data-iap-shop" not in pricing)
                account = text(client.get("/account", headers=IN_APP))
                check("and its account page manages it with Apple, before deleting",
                      0 <= account.find("keeps billing through Apple")
                      < account.find("data-iap-manage", account.find("keeps billing"))
                      < account.find('action="/account/delete"'))
        visitor = web.app.test_client()
        page = text(visitor.get("/pricing", headers=IN_APP))
        check("signed out, the app asks for an account first and sells nothing",
              "Create free account" in page and "data-iap-shop" not in page
              and "app-purchase.js" not in page and not leaks(page), leaks(page))
    finally:
        _os.environ.pop("APPLE_APP_ID", None)

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

    print()
    print("=" * 72)
    print("APP REVIEW TYPES ITS DETAILS WHERE THE APP ASKS FOR AN EMAIL")
    print("=" * 72)
    # The visible way in that takes an email is Continue with email, whose
    # code box takes six digits. The review address typed there must reach
    # the review page, not the owner's inbox.
    _os.environ.update(SMTP)
    mailed = []
    real_send = web.codes_mod.send_email_code
    web.codes_mod.send_email_code = lambda conn, to, ip, key: mailed.append(to)
    try:
        web._RATE.clear()
        reviewer = web.app.test_client()
        typed = reviewer.post("/signin/email", data={"to": " AppReview@Example.com "},
                              headers=IN_APP)
        check("the review address on Continue with email opens the review page",
              typed.status_code == 302
              and urlparse(typed.headers.get("Location", "")).path == "/signin/review",
              (typed.status_code, typed.headers.get("Location")))
        check("and is sent no email", not mailed, mailed)
        page = text(reviewer.get("/signin/review", headers=IN_APP))
        check("which has the address filled in, and waits for the code",
              'value="appreview@example.com"' in page
              and re.search(r'id="code"[^>]*autofocus', page) is not None
              and not re.search(r'id="email"[^>]*autofocus', page))
        signed = reviewer.post("/signin/review", headers=IN_APP, data={
            "email": "appreview@example.com", "code": CODE})
        check("and the code signs in there", signed.status_code == 302,
              signed.status_code)
        other = web.app.test_client().post("/signin/email",
                                           data={"to": "someone@example.com"})
        check("any other address still gets its code by email",
              other.status_code == 302 and mailed == ["someone@example.com"]
              and urlparse(other.headers.get("Location", "")).path
              == "/signin/email/code", (other.status_code, mailed))
        _os.environ["REVIEW_LOGIN_CODE"] = "short-code"
        web.app.test_client().post("/signin/email", data={"to": "appreview@example.com"})
        check("and with the review sign-in off, so does the review address",
              mailed == ["someone@example.com", "appreview@example.com"], mailed)
        _os.environ["REVIEW_LOGIN_CODE"] = CODE
    finally:
        web.codes_mod.send_email_code = real_send
        for key in SMTP:
            _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("GOOGLE NEVER TAKES THE REVIEW ACCOUNT, AND THE LOG SAYS WHY")
    print("=" * 72)
    # Google joining it made its provider "google", after which the review
    # code was refused as for a stranger's account, App Review could not get
    # in, and nothing said why.
    logged = []

    class Grab(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    grab = Grab(level=logging.WARNING)
    web.app.logger.addHandler(grab)
    real_google = web.auth_mod.sign_in

    def google_says(email, sub):
        web.auth_mod.sign_in = lambda code, uri, verifier: {
            "sub": sub, "email": email, "email_verified": True, "name": "Someone"}
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["oauth_state"] = "state-" + sub
            sess["oauth_verifier"] = "v" * 43
        back = client.get("/auth/google/callback?state=state-%s&code=c" % sub,
                          headers=IN_APP)
        with client.session_transaction() as sess:
            return back, web.SESSION_KEY in sess

    def provider_of(email):
        conn = db.connect(DB)
        try:
            row = conn.execute("SELECT provider FROM users WHERE email=?",
                               (email,)).fetchone()
            return row["provider"] if row else None
        finally:
            conn.close()

    try:
        back, signed = google_says("AppReview@example.com", "g-review")
        check("Google with the review address is refused, in words",
              back.status_code == 400 and "kept for Apple" in text(back) and not signed,
              (back.status_code, signed))
        check("and the review account is still the email kind",
              provider_of("appreview@example.com") == "email",
              provider_of("appreview@example.com"))
        check("the log says why", any("Refused Google sign-in to the App Review account"
                                      in m for m in logged), logged)
        web._RATE.clear()
        still = web.app.test_client().post("/signin/review", data={
            "email": "appreview@example.com", "code": CODE})
        check("so the review sign-in still opens it", still.status_code == 302,
              still.status_code)
        check("and start-up says it is on", web.review_status() == "on",
              web.review_status())

        # An address Google made an account for before it was named the
        # review address is not the review account: its owner keeps Google,
        # and the code is refused for it, with the cause in the log.
        conn = db.connect(DB)
        A.upsert_google_user(conn, {"sub": "g-owner", "email": "owner@example.com",
                                    "email_verified": True, "name": "Owner"})
        conn.close()
        _os.environ["REVIEW_LOGIN_EMAIL"] = "owner@example.com"
        del logged[:]
        back, signed = google_says("owner@example.com", "g-owner")
        check("an account Google made before still signs in with Google",
              back.status_code == 302 and signed, back.status_code)
        web._RATE.clear()
        refused = web.app.test_client().post("/signin/review", data={
            "email": "owner@example.com", "code": CODE})
        check("while the review code is refused for it, as any wrong try",
              refused.status_code == 400 and web.REVIEW_ERROR in text(refused),
              refused.status_code)
        check("and the log names the cause",
              any("cannot open its account" in m and "made or joined with Google "
                  "sign-in" in m for m in logged), logged)
        check("and so does start-up",
              web.review_status().startswith("on, but its code cannot open its "
                                             "account: the account at "
                                             "REVIEW_LOGIN_EMAIL was made or joined "
                                             "with Google sign-in"),
              web.review_status())
        _os.environ["REVIEW_LOGIN_CODE"] = "short-code"
        check("start-up says a review sign-in left half set up is off",
              web.review_status().startswith("off: it needs"), web.review_status())
        for key in REVIEW:
            _os.environ.pop(key, None)
        check("and one never set up is plainly off", web.review_status() == "off",
              web.review_status())
    finally:
        web.auth_mod.sign_in = real_google
        web.app.logger.removeHandler(grab)

    print()
    print("=" * 72)
    print("THE REVIEW NOTES' PATHS WORK FOR THE REVIEW ACCOUNT")
    print("=" * 72)
    # What the owner pastes into App Store Connect for App Review, from
    # mobile/APP_STORE.md. Its share step went through a link only the
    # signed-out front page has, its alert step ran into the free plan's
    # limit, and its paths went through a top Menu the app hides.
    guide = open(_os.path.join(harness.ROOT, "mobile", "APP_STORE.md"),
                 encoding="utf-8").read()
    notes = guide[guide.index("### App Review Information"):].split("```", 2)[1].strip()
    blocks = notes.split("\n\n")
    check("the notes start by saying where the review credentials work",
          blocks[0].startswith("SIGNING IN TO THE REVIEW ACCOUNT")
          and "work only on Orenth's Review sign-in page" in blocks[0]
          and "Continue with email" in blocks[0])
    check("then every native feature, before anything else",
          blocks[1].startswith("NATIVE FEATURES"))
    check("they leave room under App Store Connect's 4,000 bytes to fill them in",
          len(notes.encode("utf-8")) < 3800, len(notes.encode("utf-8")))
    check("no path goes through the top Menu, which the app hides",
          not re.search(r"\bMenu\b", notes), re.findall(r".{0,30}\bMenu\b.{0,30}", notes))
    check("nor through a link only the signed-out front page has",
          "Today's checks on popular stocks and coins" not in notes)

    # Part 4 step 5: the owner's sample alert, then paused. The notes then
    # have the reviewer add one of their own and check it.
    _os.environ["REVIEW_LOGIN_EMAIL"] = "appreview@example.com"
    _os.environ["REVIEW_LOGIN_CODE"] = CODE
    web._RATE.clear()
    rev = web.app.test_client()
    signed = rev.post("/signin/review", headers=IN_APP, data={
        "email": "appreview@example.com", "code": CODE})
    check("the review account signs in", signed.status_code == 302, signed.status_code)
    sample = rev.post("/monitor/alert/add", headers=IN_APP, data={
        "symbol": "MSFT", "kind": "price_above", "threshold": "100000",
        "interval": "1d", "repeat": "0", "note": "sample"})
    conn = db.connect(DB)
    sample_id = conn.execute(
        "SELECT a.id FROM alerts a JOIN users u ON u.id = a.user_id "
        "WHERE u.email='appreview@example.com' AND a.note='sample'").fetchone()["id"]
    conn.close()
    second = rev.post("/monitor/alert/add", headers=IN_APP, data={
        "symbol": "AAPL", "kind": "price_above", "threshold": "1", "interval": "1d",
        "repeat": "0"})
    check("left running, the sample alert would send the reviewer's own to the Pro "
          "screen", sample.status_code == 302 and second.status_code == 402,
          (sample.status_code, second.status_code))
    rev.post("/monitor/alert/pause", headers=IN_APP, data={"alert_id": str(sample_id)})
    own = rev.post("/monitor/alert/add", headers=IN_APP, data={
        "symbol": "AAPL", "kind": "price_above", "threshold": "1", "interval": "1d",
        "repeat": "0"})
    check("paused, it leaves room for the reviewer's alert", own.status_code == 302,
          own.status_code)
    monitor = text(rev.get("/monitor", headers=IN_APP))
    check("and the Monitor page has what the notes name",
          "Notifications on this device" in monitor and "Check them all now" in monitor
          and "Price rises above" in monitor)
    account = text(rev.get("/account", headers=IN_APP))
    page = text(rev.get("/", headers=IN_APP))
    more = page[page.find('<details class="more">'):]
    check("More has Account, whose page has See Pro, Sign out and Leaving",
          'href="/account"' in more[:more.find("</details>")]
          and ">See Pro<" in account and ">Sign out<" in account
          and "<h2>Leaving</h2>" in account)
    check("the front page has NVDA to analyse and share", 'symbol=NVDA' in page)
finally:
    billing._stripe = real_stripe
    for key in list(STRIPE) + list(REVIEW) + ["STOCKBOT_ADMIN_EMAIL"]:
        _os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# The iPhone project in mobile/. Xcode cannot run here, so these read the
# committed files the way Xcode and Capacitor will, and hold them to what was
# decided: a decision undone by a later edit fails here, not at App Review.
# ---------------------------------------------------------------------------
import json  # noqa: E402
import plistlib  # noqa: E402
import re  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402

MOBILE = _os.path.join(harness.ROOT, "mobile")
IOS_APP = _os.path.join(MOBILE, "ios", "App", "App")


def read(*parts):
    with open(_os.path.join(MOBILE, *parts), encoding="utf-8") as fh:
        return fh.read()


print()
print("=" * 72)
print("THE IPHONE PROJECT IS SET UP AS DECIDED")
print("=" * 72)
config = json.loads(read("capacitor.config.json"))
paper = re.search(r"--paper:\s*(#[0-9a-fA-F]{6})",
                  open(_os.path.join(harness.ROOT, "static", "style.css"),
                       encoding="utf-8").read()).group(1).lower()
check("bundle id and name", config.get("appId") == "app.orenth.ios"
      and config.get("appName") == "Orenth", config)
check("the app shows the live site", config.get("server", {}).get("url")
      == "https://orenth.app", config.get("server"))
check("and never another one", not config.get("server", {}).get("allowNavigation"))
check("a failed load shows the offline page",
      config["server"].get("errorPath") == "offline.html"
      and _os.path.exists(_os.path.join(MOBILE, "www", "offline.html")))
agent = config.get("ios", {}).get("appendUserAgent", "")
check("the app's User-Agent is the one the site looks for",
      agent == "OrenthApp/1" and web.APP_AGENT in agent + " ", agent)
check("content runs edge to edge", config["ios"].get("contentInset") == "never")
check("the launch colour is the site's paper",
      config.get("backgroundColor", "").lower() == paper
      and config["plugins"]["SplashScreen"].get("backgroundColor", "").lower() == paper,
      (config.get("backgroundColor"), paper))
splash = config["plugins"]["SplashScreen"]
check("the launch screen hides itself quickly, with no spinner",
      splash.get("launchAutoHide") is True and splash.get("launchShowDuration") == 800
      and splash.get("showSpinner") is False, splash)
bar = config["plugins"]["StatusBar"]
check("the status bar sits above the page and follows the phone",
      bar.get("overlaysWebView") is False and bar.get("style") == "DEFAULT", bar)
check("notifications show while the app is open",
      config["plugins"]["PushNotifications"].get("presentationOptions")
      == ["badge", "sound", "banner", "list"])

offline = read("www", "offline.html")
check("the offline page fetches nothing",
      not re.search(r"""(src|href)\s*=\s*["']?https?:""", offline, re.I)
      and "@import" not in offline and "<link" not in offline.lower())
check("and says what is wrong in plain words",
      "No connection" in offline and "needs the internet" in offline
      and "Try again" in offline)
check("and tries the site again", "https://orenth.app/" in offline)
check("and has a dark version", "prefers-color-scheme: dark" in offline)
check("and takes the launch screen away", "'SplashScreen', 'hide'" in offline)

package = json.loads(read("package.json"))
pins = dict(package.get("dependencies", {}), **package.get("devDependencies", {}))
WANT = {"@capacitor/core": "8.6.2", "@capacitor/cli": "8.6.2", "@capacitor/ios": "8.6.2",
        "@capacitor/app": "8.1.2", "@capacitor/browser": "8.0.5",
        "@capacitor/push-notifications": "8.1.3", "@capacitor/splash-screen": "8.0.2",
        "@capacitor/status-bar": "8.0.4", "@capacitor/share": "8.0.3",
        "@capacitor/haptics": "8.0.2", "@capawesome/capacitor-apple-sign-in": "0.1.5",
        "@capacitor/assets": "4.0.0"}
check("every package is pinned to the decided version",
      all(pins.get(name) == version for name, version in WANT.items()),
      {n: pins.get(n) for n in WANT if pins.get(n) != WANT[n]})
check("and nothing floats", not [v for v in pins.values() if not re.match(r"^\d+\.\d+\.\d+$", v)],
      pins)
lock = json.loads(read("package-lock.json"))
check("the lock file matches",
      all(lock["packages"].get("node_modules/" + name, {}).get("version") == version
          for name, version in WANT.items()))

with open(_os.path.join(IOS_APP, "Info.plist"), "rb") as fh:
    info = plistlib.load(fh)
check("Info.plist: 64-bit devices, not armv7",
      info.get("UIRequiredDeviceCapabilities") == ["arm64"],
      info.get("UIRequiredDeviceCapabilities"))
check("Info.plist: only standard encryption (HTTPS)",
      info.get("ITSAppUsesNonExemptEncryption") is False)
schemes = [s for t in info.get("CFBundleURLTypes", []) for s in t.get("CFBundleURLSchemes", [])]
check("Info.plist: app.orenth.ios links open the app", schemes == ["app.orenth.ios"], schemes)
check("Info.plist: the home screen says Orenth", info.get("CFBundleDisplayName") == "Orenth")
check("Info.plist: says why it may use the camera and save photos",
      len(info.get("NSCameraUsageDescription", "")) > 20
      and len(info.get("NSPhotoLibraryAddUsageDescription", "")) > 20)
check("Info.plist: one window, made by SceneDelegate",
      info["UIApplicationSceneManifest"]["UIApplicationSupportsMultipleScenes"] is False)

with open(_os.path.join(IOS_APP, "PrivacyInfo.xcprivacy"), "rb") as fh:
    privacy = plistlib.load(fh)
check("privacy manifest: no tracking, no tracking domains, no reasoned APIs",
      privacy == {"NSPrivacyTracking": False, "NSPrivacyTrackingDomains": [],
                  "NSPrivacyAccessedAPITypes": []}, privacy)

board = ET.parse(_os.path.join(IOS_APP, "Base.lproj", "Main.storyboard")).getroot()
screens = [(v.get("customClass"), v.get("customModule"), v.get("customModuleProvider"))
           for v in board.iter("viewController")]
check("Main.storyboard names MainViewController in the App module",
      screens == [("MainViewController", "App", "target")], screens)
scene = read("ios", "App", "App", "SceneDelegate.swift")
check("SceneDelegate builds the window around MainViewController",
      "rootViewController = MainViewController()" in scene
      and "CAPBridgeViewController()" not in scene)
delegate = read("ios", "App", "App", "AppDelegate.swift")
check("AppDelegate passes the push token and its failure to Capacitor",
      ".capacitorDidRegisterForRemoteNotifications, object: deviceToken" in delegate
      and ".capacitorDidFailToRegisterForRemoteNotifications, object: error" in delegate)
controller = read("ios", "App", "App", "MainViewController.swift")
check("MainViewController registers the sign-in sheet, swipe back and pull to refresh",
      "registerPluginInstance(WebAuthPlugin())" in controller
      and "allowsBackForwardNavigationGestures = true" in controller
      and "refreshControl" in controller and "observe(\\.isLoading" in controller)
plugin = read("ios", "App", "App", "WebAuthPlugin.swift")
check("WebAuthPlugin is the WebAuth plugin with a start method",
      'jsName: String = "WebAuth"' in plugin
      and 'CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise)' in plugin
      and '"CANCELLED"' in plugin and "ASWebAuthenticationPresentationContextProviding" in plugin)

purchase = read("ios", "App", "App", "AppPurchasePlugin.swift")
check("MainViewController registers the App Store plugin at launch, beside the sign-in sheet",
      controller.find("registerPluginInstance(WebAuthPlugin())")
      < controller.find("registerPluginInstance(AppPurchasePlugin())")
      < controller.find("guard let webView"))
methods = re.findall(r'CAPPluginMethod\(name: "(\w+)", returnType: CAPPluginReturnPromise\)', purchase)
check("AppPurchasePlugin is the AppPurchase plugin, with its eight methods",
      'jsName: String = "AppPurchase"' in purchase and "import StoreKit" in purchase
      and sorted(methods) == sorted(["products", "purchase", "entitlements", "restore",
                                     "unfinished", "finish", "manageSubscriptions",
                                     "storefront"]), methods)
page_script = open(_os.path.join(harness.ROOT, "static", "app-purchase.js"), encoding="utf-8").read()
called = set(re.findall(r"\bap\('(\w+)'", page_script))
check("every method the page calls is one the plugin has", called and called <= set(methods),
      called - set(methods))
check("it listens to StoreKit from launch and keeps events until a page listens",
      "Transaction.updates" in purchase and "retainUntilConsumed: true" in purchase
      and "override public func load()" in purchase)
check("every purchase carries the account's token, and none goes without one",
      "[.appAccountToken(token)]" in purchase and "UUID(uuidString: tokenText)" in purchase
      and '"appAccountToken must be a UUID", "BAD_INPUT"' in purchase)
check("the sheet shows over the app's window on iOS 17, and iOS 15 still buys",
      "#available(iOS 17.0, *)" in purchase and "purchase(confirmIn: scene, options: options)" in purchase
      and "product.purchase(options: options)" in purchase)
check("a transaction is finished only when the page asks, after the server",
      purchase.count(".finish()") == 1
      and purchase.index(".finish()") > purchase.index("@objc func finish("))
check("the server gets Apple's signed copy of each transaction",
      purchase.count('"jws": result.jwsRepresentation') == 1)
check("restore and manage are Apple's own (AppStore.sync, showManageSubscriptions)",
      "try await AppStore.sync()" in purchase
      and "AppStore.showManageSubscriptions(in: scene)" in purchase)

pbx = read("ios", "App", "App.xcodeproj", "project.pbxproj")
check("Xcode project: iPhone only",
      pbx.count("TARGETED_DEVICE_FAMILY = 1;") == 2 and '"1,2"' not in pbx)
for name, phase in (("MainViewController.swift", "Sources"),
                    ("WebAuthPlugin.swift", "Sources"),
                    ("AppPurchasePlugin.swift", "Sources"),
                    ("PrivacyInfo.xcprivacy", "Resources")):
    check("Xcode project: %s is built into the app" % name,
          pbx.count("/* %s in %s */" % (name, phase)) == 2
          and pbx.count("path = %s;" % name) == 1, name)
check("Xcode project: no value left as undefined", "= undefined;" not in pbx)
check("Xcode project: deployment target stays at iOS 15.0",
      set(re.findall(r"IPHONEOS_DEPLOYMENT_TARGET = ([\d.]+);", pbx)) == {"15.0"})
check("Xcode project: the bundle id is app.orenth.ios",
      set(re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", pbx)) == {"app.orenth.ios"})

from PIL import Image  # noqa: E402

CATALOG = _os.path.join(IOS_APP, "Assets.xcassets")
for folder in ("AppIcon.appiconset", "Splash.imageset"):
    listed = json.loads(read("ios", "App", "App", "Assets.xcassets", folder, "Contents.json"))
    names = [i["filename"] for i in listed["images"] if i.get("filename")]
    on_disk = sorted(n for n in _os.listdir(_os.path.join(CATALOG, folder)) if n.endswith(".png"))
    check("%s lists exactly the pictures it holds" % folder, sorted(names) == on_disk,
          (sorted(names), on_disk))
with Image.open(_os.path.join(CATALOG, "AppIcon.appiconset", "AppIcon-512@2x.png")) as icon:
    check("the App Store icon is 1024 x 1024 with no alpha channel at all",
          icon.size == (1024, 1024) and icon.mode == "RGB"
          and "transparency" not in icon.info, (icon.size, icon.mode))
splashes = json.loads(read("ios", "App", "App", "Assets.xcassets", "Splash.imageset",
                           "Contents.json"))["images"]
check("the launch screen has a dark version",
      any(a.get("value") == "dark" for i in splashes for a in i.get("appearances", [])))

print()
if fails:
    print("FAILED: %d" % len(fails))
    for name in fails:
        print("  - %s" % name)
    _sys.exit(1)
print("APP MODE OK")
