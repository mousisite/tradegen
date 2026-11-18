"""Signing in inside the iPhone app: the browser sheet hands the sign-in back.

Google refuses sign-in inside an app's web view, so the app opens the start
address in the system browser sheet with app=1 and the SHA-256 of a secret it
keeps. The sheet is never signed in: it is sent to the app's own address with
a one-time code, and the app's web view trades code and secret for a session.
This walks that path through the real routes, with Google stubbed at the HTTP
boundary as in test_signin_flow.py, then tries every way the last step should
fail: a code used twice, run out, made up, or with the wrong secret, a post
from another site, and a link from another site trying to start one.
"""
import hashlib
import os as _os
import re
import secrets
import sys as _sys
from urllib.parse import parse_qs, urlparse

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import accounts as A
from bot import auth as auth_mod
from bot import database as db

c, DB = harness.isolated_web("handoff")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeGoogle:
    def __init__(self, profile):
        self.profile = profile

    def post(self, url, timeout=None, data=None):
        return FakeResponse(200, {"access_token": "ya29.fake", "expires_in": 3599,
                                  "token_type": "Bearer"})

    def get(self, url, timeout=None, headers=None):
        return FakeResponse(200, self.profile)


def person(n):
    return {"sub": "g-%d" % n, "email": "person%d@example.com" % n,
            "email_verified": True, "name": "Person %d" % n}


FINISH = "app.orenth.ios://auth/finish"
SAME_SITE = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


def new_secret():
    verifier = secrets.token_urlsafe(32)
    return verifier, A.challenge_of(verifier)


def sheet_sign_in(profile, challenge, next_path="/trades", start_headers=None,
                  sheet=None):
    """The part in the browser sheet: start, Google, and back. Returns the
    sheet's last response and the sheet."""
    sheet = sheet or web.app.test_client()
    path = "/auth/google?app=1&challenge=%s&next=%s" % (challenge, next_path)
    sheet.get(path, headers=start_headers or {})
    with sheet.session_transaction() as sess:
        state = sess.get("oauth_state", "")
    original = auth_mod.requests
    auth_mod.requests = FakeGoogle(profile)
    try:
        back = sheet.get("/auth/google/callback?code=4/c&state=" + state)
    finally:
        auth_mod.requests = original
    return back, sheet


def code_of(response):
    location = response.headers.get("Location", "")
    if not location.startswith(FINISH + "?"):
        return ""
    return parse_qs(urlparse(location).query).get("code", [""])[0]


def finish(code, verifier, headers=None, next_path=""):
    webview = web.app.test_client()
    out = webview.post("/auth/app/finish", data={"code": code, "verifier": verifier,
                                                 "next": next_path},
                       headers=SAME_SITE if headers is None else headers)
    return out, webview


_os.environ["GOOGLE_CLIENT_ID"] = "1234.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "GOCSPX-fake-secret-value"
web.app.config["AUTH_MODE"] = None
FAILED = "That sign-in took too long or was already used. Try again."

try:
    section("THE WHOLE WAY: SHEET, APP ADDRESS, WEB VIEW")

    verifier, challenge = new_secret()
    back, sheet = sheet_sign_in(person(1), challenge)
    location = back.headers.get("Location", "")
    print("      the sheet was sent to %s..." % location[:48])
    check("the sheet is sent to the app's address with a code",
          back.status_code == 302 and location.startswith(FINISH + "?code="),
          location)
    check("and told not to keep a copy", back.headers.get("Cache-Control") == "no-store")
    with sheet.session_transaction() as sess:
        check("the sheet is not signed in", web.SESSION_KEY not in sess)
        check("and keeps none of the sign-in's state",
              not any(k.startswith("oauth_") for k in sess), list(sess.keys()))
    code = code_of(back)
    check("the code is long and random", len(code) >= 43, code)

    conn = db.connect(DB)
    try:
        rows = conn.execute("SELECT * FROM app_handoffs").fetchall()
    finally:
        conn.close()
    check("only the code's hash is stored",
          len(rows) == 1 and rows[0]["code_hash"] == hashlib.sha256(
              code.encode()).hexdigest() and code not in str(dict(rows[0])))
    check("with the app's challenge and where to go",
          rows[0]["challenge"] == challenge and rows[0]["next_url"] == "/trades")

    done, webview = finish(code, verifier, next_path="/somewhere-else")
    check("the web view trades code and verifier for a session",
          done.status_code == 302, done.status_code)
    check("and lands where the sign-in was started for",
          done.headers.get("Location", "").endswith("/trades"),
          done.headers.get("Location"))
    page = webview.get("/account").data.decode("utf-8", "replace")
    check("the web view is signed in", "person1@example.com" in page)

    again, _ = finish(code, verifier)
    check("the same code cannot be used twice", again.status_code == 400,
          again.status_code)
    check("and says so plainly", FAILED in again.data.decode("utf-8", "replace"))

    section("WHAT A CAUGHT CODE IS WORTH")

    verifier, challenge = new_secret()
    back, _ = sheet_sign_in(person(2), challenge)
    code = code_of(back)
    stolen, thief = finish(code, secrets.token_urlsafe(32))
    check("without the app's verifier the code is refused", stolen.status_code == 400)
    check("and the thief is not signed in", thief.get("/account").status_code == 302)
    late, _ = finish(code, verifier)
    check("a wrong try uses the code up, so it cannot be guessed at",
          late.status_code == 400)

    made_up, _ = finish(secrets.token_urlsafe(32), verifier)
    check("a made-up code is refused", made_up.status_code == 400)
    check("as is no code at all", finish("", verifier)[0].status_code == 400)
    verifier, challenge = new_secret()
    code = code_of(sheet_sign_in(person(2), challenge)[0])
    check("as is a verifier of the wrong shape",
          finish(code, "short")[0].status_code == 400)

    verifier, challenge = new_secret()
    code = code_of(sheet_sign_in(person(2), challenge)[0])
    conn = db.connect(DB)
    try:
        conn.execute("UPDATE app_handoffs SET created_ts=created_ts-121 WHERE "
                     "code_hash=?", (hashlib.sha256(code.encode()).hexdigest(),))
        conn.commit()
    finally:
        conn.close()
    expired, _ = finish(code, verifier)
    check("a code older than two minutes is refused", expired.status_code == 400)
    conn = db.connect(DB)
    try:
        A.new_handoff(conn, A.list_users(conn)[-1].id, challenge)
        old = conn.execute("SELECT COUNT(*) FROM app_handoffs WHERE created_ts < "
                           "strftime('%s','now') - 120").fetchone()[0]
    finally:
        conn.close()
    check("old codes are deleted when a new one is made", old == 0, old)

    section("THE LAST STEP COMES FROM THE APP'S OWN PAGE")

    verifier, challenge = new_secret()
    code = code_of(sheet_sign_in(person(3), challenge)[0])
    out, other = finish(code, verifier, headers={"Origin": "https://evil.example",
                                                "Sec-Fetch-Site": "cross-site"})
    check("a post from another site is refused", out.status_code == 403)
    check("and signs nobody in", other.get("/account").status_code == 302)
    out, _ = finish(code, verifier, headers={"Origin": "https://evil.example"})
    check("an Origin from another site alone is enough", out.status_code == 403)
    out, _ = finish(code, verifier, headers={"Origin": "null"})
    check("so is an Origin of null", out.status_code == 403)
    out, _ = finish(code, verifier,
                    headers={"Referer": "https://evil.example/page"})
    check("with neither, a Referer from another site is refused",
          out.status_code == 403)
    out, quiet = finish(code, verifier, headers={})
    check("a browser that sends none of them is let through",
          out.status_code == 302 and quiet.get("/account").status_code == 200)
    verifier, challenge = new_secret()
    code = code_of(sheet_sign_in(person(3), challenge)[0])
    out, _ = finish(code, verifier, headers={"Referer": "http://localhost/signin"})
    check("as is one whose Referer is this site", out.status_code == 302)

    section("WHEN THE SHEET'S SIGN-IN DOES NOT FINISH")

    def sheet_callback(query, challenge, state=None):
        sheet = web.app.test_client()
        sheet.get("/auth/google?app=1&challenge=" + challenge)
        with sheet.session_transaction() as sess:
            real_state = sess.get("oauth_state", "")
        return sheet.get("/auth/google/callback?%s&state=%s"
                         % (query, real_state if state is None else state))

    _verifier, challenge = new_secret()
    out = sheet_callback("error=access_denied", challenge)
    check("cancelling at Google tells the app, not an error page",
          out.headers.get("Location") == FINISH + "?error=cancelled",
          out.headers.get("Location"))
    out = sheet_callback("code=4/c", challenge, state="forged")
    check("a state that does not match tells the app it ran out",
          out.headers.get("Location") == FINISH + "?error=expired",
          out.headers.get("Location"))
    out = sheet_callback("code=", challenge)
    check("no code from Google tells the app it failed",
          out.headers.get("Location") == FINISH + "?error=failed")

    # The rule from 00dfec5: an address given to someone new cannot take
    # over the account of the Google identity that had it.
    taken = dict(person(1), sub="g-someone-new")
    back, _ = sheet_sign_in(taken, challenge)
    check("an account rule saying no tells the app it was refused",
          back.headers.get("Location") == FINISH + "?error=refused",
          back.headers.get("Location"))

    bad = web.app.test_client().get("/auth/google?app=1&challenge=too-short")
    check("a start without a proper challenge goes back to the app",
          bad.status_code == 302 and
          bad.headers.get("Location") == FINISH + "?error=failed",
          bad.headers.get("Location"))

    section("A LINK FROM ANOTHER SITE CANNOT START ONE")

    verifier, challenge = new_secret()
    back, sheet = sheet_sign_in(person(4), challenge,
                                start_headers={"Sec-Fetch-Site": "cross-site"})
    location = back.headers.get("Location", "")
    check("it becomes an ordinary sign-in in that browser",
          not location.startswith(FINISH) and location.endswith("/trades"), location)
    check("which hands nothing to any app",
          sheet.get("/account").status_code == 200)

    plain = web.app.test_client()
    plain.get("/auth/google?next=/trades")
    with plain.session_transaction() as sess:
        check("a sign-in on the website carries no app check",
              "oauth_app" not in sess)

    section("NOT TOO MANY TRIES FROM ONE NETWORK")

    web._RATE.clear()
    statuses = [finish(secrets.token_urlsafe(32), "x" * 43)[0].status_code
                for _ in range(31)]
    check("thirty tries an hour from one network are answered",
          statuses[:30] == [400] * 30, statuses[:30])
    check("the thirty-first is turned away", statuses[30] == 429, statuses[30])
    web._RATE.clear()

    section("THE SIGN-IN PAGE AND ITS SCRIPT")

    web.app.config["AUTH_MODE"] = "google"
    page = web.app.test_client().get("/signin").data.decode("utf-8", "replace")
    check("the page loads the app's script", "signin-app.js" in page)
    check("tells it where to finish", 'data-finish="/auth/app/finish"' in page)
    check("and marks the Google button for it", 'data-app-via="google"' in page)
    check("with no Apple set up, no Apple button", "Continue with Apple" not in page)
    # The script is deferred. In the app a tap before it has run must start
    # nothing: Google refuses sign-in inside the app's own view, and the app
    # would send it to Safari, where the sign-in cannot come back. So there the
    # button goes nowhere, and the script starts the sign-in from data-start.
    button = re.compile(r'<a class="gbtn" data-app-via="google"\s+href="([^"]*)"\s+'
                        r'data-start="([^"]*)"([^>]*)>')
    on_web = button.search(page)
    check("on the website the Google button is a link to Google's sign-in",
          on_web and on_web.group(1).startswith("/auth/google")
          and on_web.group(2) == on_web.group(1) and "role=" not in on_web.group(3),
          on_web and on_web.groups())
    app_page = web.app.test_client().get("/signin?next=/trades", headers={
        "User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 OrenthApp/1"}
    ).data.decode("utf-8", "replace")
    in_app = button.search(app_page)
    check("in the app it goes nowhere by itself, a button whose sign-in starts from "
          "data-start, next page and all",
          in_app and in_app.group(1) == "#" and in_app.group(2).startswith("/auth/google?")
          and "next=/trades" in in_app.group(2) and 'role="button"' in in_app.group(3),
          in_app and in_app.groups())
    script = web.app.test_client().get("/static/signin-app.js")
    text = script.data.decode("utf-8", "replace")
    check("the script is served", script.status_code == 200)
    for needle, why in (
            ("isNativePlatform()", "does nothing outside the app"),
            ("crypto.getRandomValues", "makes the verifier from real randomness"),
            ("crypto.subtle.digest('SHA-256'", "sends only the verifier's SHA-256"),
            ("presentationStyle: 'popover'", "opens the system browser sheet"),
            ("'appUrlOpen'", "hears the app's address being opened"),
            ("getLaunchUrl", "checks the address the app was launched with"),
            ("Browser.close()", "closes the sheet"),
            ("app.orenth.ios://auth/finish", "knows the app's address"),
            ("sessionStorage", "keeps the verifier in the page only"),
            ("SignInWithApple", "uses Apple's own sheet when the app has it")):
        check("the script %s" % why, needle in text, needle)
    web.app.config["AUTH_MODE"] = None

finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

print()
print("%d failure(s)" % len(fails))
print("APP HANDOFF OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
