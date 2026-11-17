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
    real_web_ready = web.apple_mod.web_ready
    web.apple_mod.web_ready = lambda: True
    try:
        page = flat(text(visitor.get("/privacy")))
    finally:
        web.apple_mod.web_ready = real_web_ready
    check("with it on, the second one and how long it lasts",
          "a second cookie of Orenth's lasts ten minutes" in page
          and web.APPLE_FLOW_SECONDS == 10 * 60, page[page.find("No tracking cookies"):][:900])
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
