"""What the privacy page and the terms promise, held to what the code does.

Error reports: page loads are sampled for Sentry only when the owner asks for
it, and a sample never carries what was typed into a form, a cookie or
anything after a ?. This is checked with Sentry's own client and a transport
that only records, so nothing is sent anywhere.

No network: Sentry's transport is a list, and nothing else is reached.
"""
import json
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

# Settings that change what the page says, or where a report would go. A
# developer's own .env may hold real ones, so they are cleared again once web
# has been imported, which reads it.
CLEAR = ("SENTRY_DSN", "SENTRY_TRACES_RATE", "SENTRY_ENVIRONMENT", "SMTP_HOST",
         "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "STOCKBOT_PUBLIC_URL",
         "STOCKBOT_BEHIND_PROXY", "STOCKBOT_ADMIN_EMAIL", "STRIPE_SECRET_KEY",
         "STRIPE_PRICE_ID", "STRIPE_PRICE_ID_YEARLY", "STRIPE_WEBHOOK_SECRET",
         "RENDER", "FLY_APP_NAME")
for _k in CLEAR:
    _os.environ.pop(_k, None)

import harness  # noqa: E402

import sentry_sdk  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import database as db  # noqa: E402
from bot import digest  # noqa: E402
from bot import monitoring  # noqa: E402

for _k in CLEAR:
    _os.environ.pop(_k, None)
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "stub-secret"

c, DB = harness.isolated_web("privacy")
web.app.config["AUTH_MODE"] = None

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


def text(response):
    return response.data.decode("utf-8", "replace")


def flat(page):
    return " ".join(page.split())


def person(email, sub):
    conn = db.connect(DB)
    try:
        return A.upsert_google_user(conn, {"email": email, "sub": sub, "name": "Ann Private",
                                           "email_verified": True})
    finally:
        conn.close()


def signed_in(user):
    client = web.app.test_client()
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token
    return client


# Sentry's client, with a transport that keeps what it is given instead of
# sending it. monitoring.start() is the app's own; only the transport differs.
SENT = []


class Recorder(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            event = item.get_event() or item.get_transaction_event()
            if event is not None:
                SENT.append(event)

    def flush(self, timeout=None, callback=None):
        return None

    def kill(self):
        return None


REAL_INIT = sentry_sdk.init


def recorded_init(*args, **kwargs):
    kwargs["transport"] = Recorder
    return REAL_INIT(*args, **kwargs)


def samples():
    sentry_sdk.flush()
    return [e for e in SENT if e.get("type") == "transaction"]


PRIVATE_EMAIL = "ann.private@example.com"
PRIVATE_NOTE = "sold because my divorce lawyer said so"

try:
    ann = person("privacy-member@example.com", "g-ann")
    member = signed_in(ann)
    visitor = web.app.test_client()

    # -----------------------------------------------------------------------
    head("ERROR REPORTS: PAGE LOADS ONLY WHEN ASKED, NEVER WHAT WAS TYPED")
    # -----------------------------------------------------------------------
    sentry_sdk.init = recorded_init
    _os.environ["SENTRY_DSN"] = "https://public@o0.ingest.example.invalid/1"
    check("error reporting starts once a DSN is set", monitoring.start(release="test"))
    check("page loads are not sampled unless the owner asks",
          not sentry_sdk.get_client().options.get("traces_sample_rate"),
          sentry_sdk.get_client().options.get("traces_sample_rate"))
    check("and the server says none are", monitoring.status().get("sampled") == 0,
          monitoring.status())
    del SENT[:]
    for _ in range(40):
        member.get("/privacy")
    check("so pages that work send nothing", not samples(), len(samples()))
    page = flat(text(visitor.get("/privacy")))
    check("and the privacy page speaks only of what breaks",
          "When something breaks" in page and "page loads" not in page)

    _os.environ["SENTRY_TRACES_RATE"] = "1"
    monitoring.start(release="test")
    check("asked for, every page load is sampled",
          sentry_sdk.get_client().options.get("traces_sample_rate") == 1.0
          and monitoring.status().get("sampled") == 1.0, monitoring.status())
    del SENT[:]
    stop_token = digest.token(web.app.secret_key, ann.id)
    member.post("/trades/close", data={"trade_id": "1", "price": "195.5",
                                       "notes": PRIVATE_NOTE})
    member.get("/email/stop?u=%d&t=%s" % (ann.id, stop_token))
    visitor.post("/signin/email", data={"to": PRIVATE_EMAIL, "next": "/trades"},
                 headers={"Referer": "http://localhost/email/stop?u=%d&t=%s"
                                     % (ann.id, stop_token)})
    sent = samples()
    dumped = json.dumps(sent)
    check("each of those page loads is a sample", len(sent) == 3, len(sent))
    check("none carries what was typed into a form",
          PRIVATE_EMAIL not in dumped and PRIVATE_NOTE not in dumped
          and not any((e.get("request") or {}).get("data") for e in sent),
          [(e.get("transaction"), (e.get("request") or {}).get("data")) for e in sent])
    check("nor the token after a ? in the weekly email's stop link, even as "
          "the page someone came from",
          stop_token not in dumped
          and not any((e.get("request") or {}).get("query_string") for e in sent),
          [((e.get("request") or {}).get("query_string"),
            ((e.get("request") or {}).get("headers") or {}).get("Referer")) for e in sent])
    check("nor any cookie, or who was signed in",
          not any("cookies" in (e.get("request") or {}) for e in sent)
          and "privacy-member@example.com" not in dumped)
    check("but still says which page, and how long it took",
          all((e.get("request") or {}).get("url") and e.get("start_timestamp")
              and e.get("timestamp") for e in sent))
    _os.environ["SENTRY_TRACES_RATE"] = "0.05"
    monitoring.start(release="test")
    page = flat(text(visitor.get("/privacy")))
    check("the privacy page then says page loads are sampled, and what goes",
          "even when nothing breaks, about 5 in every 100 page loads: the page "
          "address without anything after a ?, and how long it took." in page,
          page[page.find("<td>Sentry"):][:700])
    _os.environ["SENTRY_TRACES_RATE"] = "not a number"
    monitoring.start(release="test")
    check("a rate that is not a number samples nothing, and still reports errors",
          monitoring.status() == {"enabled": True, "detail": "reporting to Sentry",
                                  "sampled": 0.0}, monitoring.status())

    # An error report keeps its query string, which usually names a stock,
    # but not when the query holds the stop link's token or Google's code.
    for query, kept in (("symbol=AAPL&interval=1d", True),
                        ("u=7&t=abc123", False),
                        ("state=xyz&code=4/0AbCdEf", False)):
        scrubbed = monitoring._scrub({"request": {"query_string": query}})
        said = scrubbed["request"]["query_string"]
        check("an error report's query %s %s" % (query, "is kept" if kept else "is removed"),
              (said == query) if kept else (said == "[removed]"), said)
    came_from = monitoring._scrub({"request": {"headers": {
        "Referer": "https://orenth.app/email/stop?u=7&t=abc123"}}})
    check("and the page it came from is named without its query",
          came_from["request"]["headers"]["Referer"] == "https://orenth.app/email/stop",
          came_from["request"]["headers"])

    # -----------------------------------------------------------------------
    head("A PAGE WITH ANYTHING OF YOURS ON IT IS SENT MARKED NOT TO BE KEPT")
    # -----------------------------------------------------------------------
    # Without it a browser keeps signed-in pages in its cache and for Back, so
    # on a shared computer the next person pressed Back after Sign out and
    # read the journal; the iPhone app's web view kept them on disk.
    for path in ("/trades", "/account", "/settings", "/monitor",
                 "/api/alerts/status", "/export/trades.csv"):
        r = member.get(path)
        said = r.headers.get("Cache-Control", "")
        check("signed in, %s is not to be stored anywhere" % path,
              r.status_code == 200 and "no-store" in said and "private" in said,
              (r.status_code, said))
    r = member.post("/signout")
    check("nor is the answer to signing out", "no-store" in r.headers.get("Cache-Control", ""),
          r.headers.get("Cache-Control"))
    for path in ("/privacy", "/terms", "/signin"):
        r = visitor.get(path)
        check("to a visitor, %s, the same for everybody, may be kept" % path,
              r.status_code == 200 and "no-store" not in r.headers.get("Cache-Control", ""),
              (r.status_code, r.headers.get("Cache-Control")))
    member = signed_in(ann)
    for path, said in (("/static/style.css", None), ("/sw.js", "no-cache")):
        r = member.get(path)
        check("%s keeps its own caching, signed in or not" % path,
              "no-store" not in (r.headers.get("Cache-Control") or "")
              and (said is None or r.headers.get("Cache-Control") == said),
              r.headers.get("Cache-Control"))
        r.close()
    page = flat(text(visitor.get("/privacy")))
    check("which is what the privacy page says",
          "Every page with anything of yours on it is sent marked not to be stored" in page)

    # -----------------------------------------------------------------------
    head("COOKIES: EVERY ONE, AND WHAT IS IN IT")
    # -----------------------------------------------------------------------
    # The page said the one cookie held an opaque token "and nothing else".
    # Flask's cookie is signed, not encrypted, and holds what a step under
    # way needs; Apple's sign-in on the website sets a second.
    from bot import invites  # noqa: E402
    conn = db.connect(DB)
    invite = invites.code_for(conn, ann.id)
    conn.close()
    arrived = web.app.test_client()
    arrived.get("/join/%s" % invite)
    with arrived.session_transaction() as sess:
        check("a visitor who came by an invite carries it in the cookie",
              sess.get("ref") == invite, dict(sess))
    page = flat(text(visitor.get("/privacy")))
    check("the page no longer says the cookie holds a token and nothing else",
          "nothing else" not in page and "No tracking cookies." in page)
    check("it says what else the cookie holds while something is under way",
          "the invite you arrived with, the email address or phone number a "
          "sign-in code was sent to" in page
          and "a note of an exit the journal just recorded" in page)
    check("and that it is signed, not encrypted",
          "signed so that it cannot be changed, but it is not encrypted" in page)
    check("with Apple off the website, no second cookie is spoken of",
          "second cookie" not in page)
    check("and with Apple not set up at all, no Apple sign-in either",
          "for a sign-in with Google," in page and "Google or Apple" not in page)
    real_web_ready = web.apple_mod.web_ready
    web.apple_mod.web_ready = lambda: True
    try:
        page = flat(text(visitor.get("/privacy")))
    finally:
        web.apple_mod.web_ready = real_web_ready
    check("with it on, the second one and how long it lasts",
          "a second cookie of Orenth's lasts ten minutes" in page
          and web.APPLE_FLOW_SECONDS == 10 * 60, page[page.find("No tracking cookies"):][:900])

    # -----------------------------------------------------------------------
    head("EMAIL AND NOTIFICATIONS: EVERYTHING THAT GOES OUT, NOT ONLY CODES")
    # -----------------------------------------------------------------------
    # The weekly email and alert emails go through the same mail service as
    # sign-in codes, and crossed stops go out with fired alerts, by email
    # and to phones (web._check_and_tell hands both lists to each).
    from bot import alerts as alerts_mod  # noqa: E402
    from bot import push as push_mod  # noqa: E402
    stop = alerts_mod.Fired(0, "AAPL", "stop_breach", 180.0, 175.0,
                            "AAPL is at 175.00, past the stop of 180.00 on your long "
                            "position.", "", ann.id, "11 Nov 21:00 UTC", 12)
    mailed = []
    conn = db.connect(DB)
    try:
        alerts_mod.deliver(conn, [stop], lambda to, subject, body: mailed.append(body),
                           lambda user_id: "ann@example.com")
    finally:
        conn.close()
    check("a crossed stop is emailed with the alerts",
          mailed and "past the stop of 180.00 on your long position" in mailed[0])
    check("and goes to phones in the same words",
          "past the stop of 180.00 on your long position"
          in push_mod.alert_note([stop]).body)
    page = flat(text(visitor.get("/privacy")))
    row = page[page.find("<td>The email delivery service</td>"):]
    row = row[:row.find("</tr>")]
    check("the mail service's row names the weekly email and alert emails too",
          "Your email address and the code, if you sign in by email" in row
          and "the weekly email or alert emails" in row
          and "the alerts that fired and the stops your positions crossed" in row
          and "Delivering sign-in codes and the emails you turned on" in row, row)
    check("what is kept says alert emails go to the sign-in address",
          "If you ask for alert emails on the Monitor page" in page)
    notes = page[page.find('id="notifications"'):]
    notes = notes[:notes.find("</p>")]
    check("the notifications paragraph says crossed stops go too, and what of them",
          "or a position in your journal crosses its stop" in notes
          and "For a stop, the text names the stock" in notes, notes)
    check("and what travels unencrypted beside the text",
          "only a short label" in notes and "travels unencrypted" in notes, notes)
    row = page[page.find("<td>Apple, Google, Mozilla or Microsoft</td>"):]
    row = row[:row.find("</tr>")]
    check("the push services' row says the same",
          "A fired alert's or crossed stop's text" in row
          and "Showing the alert or stop on your phone or computer" in row, row)

    # -----------------------------------------------------------------------
    head("WHAT EACH SIGN-IN KEEPS, AND WHO CARRIES AND RUNS THE SITE")
    # -----------------------------------------------------------------------
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, ann.id, "Mozilla/5.0 (iPhone) OrenthApp/1")
        kept = conn.execute("SELECT user_agent, expires_ts - created_ts AS lasts "
                            "FROM sessions WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    check("each sign-in keeps the browser's description of itself, for its life",
          kept["user_agent"] == "Mozilla/5.0 (iPhone) OrenthApp/1"
          and kept["lasts"] == A.SESSION_DAYS * 86400, dict(kept))
    page = flat(text(visitor.get("/privacy")))
    check("which the page says, whichever way you sign in",
          "each sign-in is kept with the description your browser or the app "
          "gives of itself (its User-Agent)" in page
          and "runs out after %d days" % A.SESSION_DAYS in page)
    check("so Google's list no longer ends 'That is the whole of it'",
          "That is the whole of it" not in page
          and "That is all Orenth gets from Google." in page)
    row = page[page.find("<td>Metricool</td>"):]
    row = row[:row.find("</tr>")]
    check("Metricool's row says it sees the network address, as any request does",
          "nothing that identifies you" not in row
          and "your network address and the description your browser gives" in row, row)
    check("a site reached directly names no proxy and no host",
          "Carrying traffic" not in page and "Running the server" not in page)
    page = flat(text(visitor.get("/privacy", headers={"CF-Connecting-IP": "203.0.113.5"})))
    row = page[page.find("<td>Cloudflare</td>"):]
    row = row[:row.find("</tr>")]
    check("a request that came through Cloudflare names it, and what it sees",
          "Everything your browser and Orenth send each other" in row
          and "Carrying traffic to the server" in row, row or page[-1500:])
    for name, env in (("Render", {"RENDER": "true"}), ("Fly.io", {"FLY_APP_NAME": "orenth"})):
        _os.environ.update(env)
        try:
            page = flat(text(visitor.get("/privacy")))
        finally:
            for key in env:
                _os.environ.pop(key, None)
        row = page[page.find("<td>%s</td>" % name):]
        row = row[:row.find("</tr>")]
        check("on %s, the host is named, with the disk it holds" % name,
              "the disk that holds the database and its backups" in row
              and "Running the server" in row, row or page[-1500:])

    # -----------------------------------------------------------------------
    head("COPIES OF THE DATABASE: THE ONES BEFORE EACH NEW VERSION TOO")
    # -----------------------------------------------------------------------
    # serve.py copies the whole database whenever a new version starts and
    # keeps the last few by count, not by age, so a deleted account can stay
    # in one for as long as the next versions take: not "about a week".
    import serve  # noqa: E402
    folder = _os.path.dirname(harness.DB_PATH)
    _os.environ["RENDER_GIT_COMMIT"] = "privacy-test-release"
    try:
        serve._copy_before_release(harness.DB_PATH)
    finally:
        _os.environ.pop("RENDER_GIT_COMMIT", None)
    copies = _os.path.join(folder, "backups", "before-release")
    check("a new version starting copies the database",
          _os.path.isdir(copies) and any(f.endswith(".db") for f in _os.listdir(copies)))
    page = flat(text(visitor.get("/privacy")))
    check("the page says so, and how long such a copy can stay",
          "The server also copies the database each time a new version of Orenth "
          "starts, and keeps the last few of those copies" in page
          and "can stay until newer versions replace it, however long that takes" in page)

    # -----------------------------------------------------------------------
    head("THE TERMS INSIDE THE IPHONE APP PROMISE NO BUTTON IT HIDES")
    # -----------------------------------------------------------------------
    from bot import billing  # noqa: E402
    IN_APP = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                            "OrenthApp/1"}
    STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
              "STRIPE_WEBHOOK_SECRET": "whsec_x"}
    _os.environ.update(STRIPE)
    try:
        conn = db.connect(DB)
        billing.set_plan(conn, ann.id, "pro", "active", "cus_ann")
        conn.close()
        member = signed_in(ann)
        account = text(member.get("/account", headers=IN_APP))
        check("inside the app the Account page has no way to cancel",
              "Manage subscription" not in account
              and member.post("/billing/portal", headers=IN_APP).status_code == 404)
        terms = flat(text(visitor.get("/terms", headers=IN_APP)))
        check("so the terms there do not send people to it",
              "cancel at any time from your Account page" not in terms
              and "A subscription taken on the website is cancelled from the Account "
                  "page there; you keep Pro until the end of the period" in terms,
              terms[terms.find("<h2>Pro</h2>"):][:600])
        terms = flat(text(visitor.get("/terms")))
        check("while in a browser they still do",
              "You can cancel at any time from your Account page; you keep Pro" in terms)
    finally:
        for _k in STRIPE:
            _os.environ.pop(_k, None)
finally:
    sentry_sdk.init = REAL_INIT
    for _k in ("SENTRY_DSN", "SENTRY_TRACES_RATE"):
        _os.environ.pop(_k, None)
    # A client with no DSN sends nothing, whatever runs after this.
    REAL_INIT(dsn=None)
    monitoring._enabled = False

print()
print("%d failure(s)" % len(fails))
print("PRIVACY OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
