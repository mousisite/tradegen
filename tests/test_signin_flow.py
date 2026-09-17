"""The Google sign-in path that every user actually takes.

Everything else about sign-in is tested by rejecting things: a forged state, a
missing code, a disallowed domain. Those matter, but none of them exercises the
path a real person walks, and an untested happy path is the one that fails on
the morning it first has users.

Google is stubbed at the HTTP boundary rather than higher up, so everything
below that boundary is the real code: the token exchange builds its real form
body, the profile is read by the real parser, the account is created by the
real upsert, the session cookie is set by the real route, and the next request
is authenticated by the real before_request hook.
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import accounts as A
from bot import auth as auth_mod
from bot import database as db

c, DB = harness.isolated_web("signin")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


# --- a stub of Google, at the HTTP boundary ---------------------------------

class FakeResponse:
    def __init__(self, status, payload, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeGoogle:
    """Records what it was asked and answers the way Google would."""

    def __init__(self, profile=None, token_status=200, token_body=None,
                 profile_status=200):
        self.profile = profile or {
            "sub": "104729384756102938475",
            "email": "trader@example.com",
            "email_verified": True,
            "name": "Dana Okafor",
            "picture": "https://lh3.googleusercontent.com/a/fake",
        }
        self.token_status = token_status
        self.token_body = token_body
        self.profile_status = profile_status
        self.token_posts = []
        self.profile_gets = []

    def post(self, url, timeout=None, data=None):
        self.token_posts.append({"url": url, "data": dict(data or {})})
        if self.token_status != 200:
            return FakeResponse(self.token_status, self.token_body or
                                {"error": "invalid_grant"})
        return FakeResponse(200, self.token_body if self.token_body is not None
                            else {"access_token": "ya29.fake-access-token",
                                  "expires_in": 3599,
                                  "token_type": "Bearer",
                                  "id_token": "fake.id.token"})

    def get(self, url, timeout=None, headers=None):
        self.profile_gets.append({"url": url, "headers": dict(headers or {})})
        if self.profile_status != 200:
            return FakeResponse(self.profile_status, {})
        return FakeResponse(200, self.profile)


def with_google(fake):
    """Swap requests inside bot/auth.py only, and hand back a restore."""
    original = auth_mod.requests
    auth_mod.requests = fake
    return lambda: setattr(auth_mod, "requests", original)


def start_flow(client):
    """Begin sign-in and return the state the server put in the cookie."""
    response = client.get("/auth/google")
    with client.session_transaction() as sess:
        return response, sess.get("oauth_state"), sess.get("oauth_verifier")


_os.environ["GOOGLE_CLIENT_ID"] = "1234.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "GOCSPX-fake-secret-value"
web.app.config["AUTH_MODE"] = None

try:
    print("=" * 72)
    print("SIGNING IN, ALL THE WAY THROUGH")
    print("=" * 72)

    visitor = web.app.test_client()

    # The front door answers rather than redirecting. A crawler handed a
    # redirect to a login form has nothing to index, and the address on every
    # shared link is this one.
    front = visitor.get("/")
    check("the front door answers a stranger", front.status_code == 200,
          front.status_code)
    check("and it says what the app is, not just a login box",
          "back-tests its own advice" in front.data.decode("utf-8", "replace"))
    check("a stranger still cannot reach the journal",
          visitor.get("/trades").status_code == 302)

    # Crawlers and link-preview scrapers never sign in. When these started
    # returning a redirect to the sign-in page, search engines read the whole
    # site as a dead end and nothing was indexable at all.
    for path in ("/robots.txt", "/sitemap.xml", "/privacy", "/terms"):
        check("a stranger can read %s" % path,
              visitor.get(path).status_code == 200,
              visitor.get(path).status_code)

    sitemap = visitor.get("/sitemap.xml").data.decode()
    check("the sitemap names the sign-in page", "/signin" in sitemap)
    check("and does not leak a page behind the login",
          "/trades" not in sitemap and "/portfolio" not in sitemap)

    # Without these a link pasted on TikTok, Discord or Reddit renders as a
    # bare URL, which is the difference between a click and a scroll past.
    card = visitor.get("/signin").data.decode("utf-8", "replace")
    for tag in ('name="description"', 'property="og:title"',
                'property="og:image"', 'name="twitter:card"'):
        check("the page carries %s" % tag, tag in card)
    # X and several chat clients drop a card whose image is not https, and
    # behind a TLS terminator the app sees a plain http request, so these must
    # be built from the public address rather than the observed one.
    _os.environ["STOCKBOT_PUBLIC_URL"] = "https://orenth.app"
    try:
        secure = visitor.get("/signin").data.decode("utf-8", "replace")
    finally:
        del _os.environ["STOCKBOT_PUBLIC_URL"]
    for tag in ("og:url", "og:image", "twitter:image"):
        line = [l for l in secure.splitlines() if tag in l][0]
        check("%s is an https address on the real domain" % tag,
              'content="https://orenth.app' in line, line.strip())
    check("and the preview address carries no query string",
          "?" not in [l for l in secure.splitlines() if "og:url" in l][0])

    check("the preview image exists on disk",
          _os.path.exists(_os.path.join(_os.path.dirname(__file__), "..",
                                        "static", "preview.png")))

    response, state, verifier = start_flow(visitor)
    check("starting sign-in redirects to Google", response.status_code == 302)
    check("a state was stored for this browser", bool(state))
    check("a verifier was stored too", bool(verifier))
    check("the state is in the url Google was sent",
          state in response.headers.get("Location", ""))

    google = FakeGoogle()
    restore = with_google(google)
    try:
        landed = visitor.get("/auth/google/callback?code=4/real-looking-code"
                             "&state=" + state)
    finally:
        restore()

    check("the callback redirects rather than erroring",
          landed.status_code == 302, landed.status_code)
    check("and lands on the app, not back at sign-in",
          "/signin" not in landed.headers.get("Location", ""),
          landed.headers.get("Location"))

    print()
    print("   what the server sent Google:")
    posted = google.token_posts[0]
    for key in sorted(posted["data"]):
        shown = posted["data"][key]
        if key == "client_secret":
            shown = "<not printed>"
        print("      %-16s %s" % (key, str(shown)[:44]))

    check("the token request went to Google's token endpoint",
          posted["url"] == auth_mod.TOKEN_URL, posted["url"])
    check("it sent the authorization code",
          posted["data"].get("code") == "4/real-looking-code")
    check("it sent grant_type=authorization_code",
          posted["data"].get("grant_type") == "authorization_code")
    check("it sent the client secret server to server",
          posted["data"].get("client_secret") == "GOCSPX-fake-secret-value")
    check("it sent the PKCE verifier",
          posted["data"].get("code_verifier") == verifier)
    # Google compares the redirect_uri sent to the authorize endpoint with the
    # one sent to the token endpoint and refuses the exchange if they differ by
    # a single character. It is percent-encoded in the url, so parse rather
    # than pattern-match.
    from urllib.parse import parse_qs, urlparse
    authorize_query = parse_qs(urlparse(response.headers["Location"]).query)
    sent_at_authorize = authorize_query.get("redirect_uri", [""])[0]
    sent_at_exchange = posted["data"].get("redirect_uri")
    print("      authorize redirect_uri  %s" % sent_at_authorize)
    print("      exchange  redirect_uri  %s" % sent_at_exchange)
    check("the redirect_uri is identical in both requests",
          sent_at_authorize == sent_at_exchange,
          "%r vs %r" % (sent_at_authorize, sent_at_exchange))
    check("and it is the callback route",
          sent_at_authorize.endswith("/auth/google/callback"), sent_at_authorize)
    check("the PKCE challenge is derived from the verifier that is later sent",
          authorize_query.get("code_challenge", [""])[0]
          == auth_mod._challenge(verifier))

    got = google.profile_gets[0]
    check("the profile was read from the userinfo endpoint",
          got["url"] == auth_mod.USERINFO_URL, got["url"])
    check("with the access token as a bearer token",
          got["headers"].get("Authorization") == "Bearer ya29.fake-access-token",
          got["headers"])

    print()
    print("   what the server did with it:")
    conn = db.connect(DB)
    people = [u for u in A.list_users(conn) if not u.is_local]
    conn.close()
    check("exactly one account was created", len(people) == 1, len(people))
    if people:
        person = people[0]
        print("      %s <%s> via %s" % (person.name, person.email, person.provider))
        check("with the name Google gave", person.name == "Dana Okafor")
        check("and the email", person.email == "trader@example.com")
        check("and the picture", person.picture.endswith("/a/fake"))
        check("recorded as a Google account", person.provider == "google")
        check("not marked as the local account", not person.is_local)

    print()
    print("   and the browser is now signed in:")
    home = visitor.get("/")
    check("the app opens without a redirect", home.status_code == 200,
          home.status_code)
    body = home.data.decode("utf-8", "replace")
    check("the page knows who it is", "Dana" in body or "D" in body)

    account_page = visitor.get("/account").data.decode("utf-8", "replace")
    check("the account page shows the signed-in address",
          "trader@example.com" in account_page)
    check("and offers a way out", "Sign out" in account_page)

    with visitor.session_transaction() as sess:
        check("the one-time state was consumed", "oauth_state" not in sess)
        check("and so was the verifier", "oauth_verifier" not in sess)

    print()
    print("   replaying the same callback must not work twice:")
    replay = visitor.get("/auth/google/callback?code=4/real-looking-code"
                         "&state=" + state)
    check("a replayed callback is refused", replay.status_code == 400,
          replay.status_code)

    print()
    print("=" * 72)
    print("SIGNING OUT, AND BACK IN")
    print("=" * 72)

    visitor.post("/signout")
    check("signed out, the front door goes back to the landing page",
          visitor.get("/").status_code == 200)
    check("and the journal is behind the login again",
          visitor.get("/trades").status_code == 302)

    response, state, verifier = start_flow(visitor)
    google2 = FakeGoogle()
    restore = with_google(google2)
    try:
        visitor.get("/auth/google/callback?code=second&state=" + state)
    finally:
        restore()

    conn = db.connect(DB)
    people = [u for u in A.list_users(conn) if not u.is_local]
    conn.close()
    check("signing in again reuses the same account", len(people) == 1, len(people))
    check("and the app opens", visitor.get("/").status_code == 200)

    print()
    print("=" * 72)
    print("TWO PEOPLE THROUGH THE REAL FLOW")
    print("=" * 72)

    second = web.app.test_client()
    response, state2, _ = start_flow(second)
    other = FakeGoogle(profile={
        "sub": "222222222222222222", "email": "someone.else@example.com",
        "email_verified": True, "name": "Sam Reyes", "picture": "",
    })
    restore = with_google(other)
    try:
        second.get("/auth/google/callback?code=other&state=" + state2)
    finally:
        restore()

    conn = db.connect(DB)
    people = {u.email: u for u in A.list_users(conn) if not u.is_local}
    conn.close()
    check("now there are two accounts", len(people) == 2, sorted(people))

    # The first person logs a trade; the second must not see it.
    visitor.post("/trades/open", data={"symbol": "AAPL", "direction": "long",
                                       "entry": "100", "stop": "95",
                                       "quantity": "10"})
    import re
    page = re.sub(r'placeholder="[^"]*"', "",
                  second.get("/trades").data.decode("utf-8", "replace"))
    check("the second person's journal is their own", "AAPL" not in page,
          "AAPL leaked between real sign-ins")
    mine = re.sub(r'placeholder="[^"]*"', "",
                  visitor.get("/trades").data.decode("utf-8", "replace"))
    check("the first person still sees theirs", "AAPL" in mine)

    print()
    print("=" * 72)
    print("WHEN GOOGLE SAYS NO")
    print("=" * 72)

    def attempt(fake, label, expect_status=400):
        client = web.app.test_client()
        _resp, st, _v = start_flow(client)
        restore = with_google(fake)
        try:
            out = client.get("/auth/google/callback?code=c&state=" + st)
        finally:
            restore()
        check(label, out.status_code == expect_status, out.status_code)
        return out

    out = attempt(FakeGoogle(token_status=400,
                             token_body={"error": "redirect_uri_mismatch"}),
                  "a redirect mismatch is refused")
    check("and the message names the address the app used",
          "redirect" in out.data.decode("utf-8", "replace").lower())

    out = attempt(FakeGoogle(token_status=401,
                             token_body={"error": "invalid_client"}),
                  "bad credentials are refused")
    check("and say the client was not recognised",
          "did not recognise" in out.data.decode("utf-8", "replace"))

    attempt(FakeGoogle(token_body={"expires_in": 1}),
            "a response with no access token is refused")
    attempt(FakeGoogle(profile_status=403),
            "a refused profile read is handled")
    attempt(FakeGoogle(profile={"sub": "x", "name": "No Email"}),
            "a profile with no email is refused")
    attempt(FakeGoogle(profile={"sub": "x", "email": "u@example.com",
                                "email_verified": False, "name": "Unverified"}),
            "an unverified address is refused")

    _os.environ["GOOGLE_ALLOWED_DOMAINS"] = "acme.com"
    try:
        out = attempt(FakeGoogle(profile={"sub": "y", "email": "x@example.com",
                                          "email_verified": True, "name": "Outsider"}),
                      "an address outside the allowed domain is refused")
        check("and says which domain is allowed",
              "@acme.com" in out.data.decode("utf-8", "replace"))
    finally:
        del _os.environ["GOOGLE_ALLOWED_DOMAINS"]

    conn = db.connect(DB)
    after = [u for u in A.list_users(conn) if not u.is_local]
    conn.close()
    check("none of those failures created an account", len(after) == 2, len(after))

    print()
    print("=" * 72)
    print("A NETWORK THAT IS NOT THERE")
    print("=" * 72)

    class Broken:
        class RequestException(Exception):
            pass

        def post(self, *a, **k):
            raise self.RequestException("connection refused")

        def get(self, *a, **k):
            raise self.RequestException("connection refused")

    broken = Broken()
    # auth.py catches requests.RequestException, so the stub must expose one
    # that is actually the class it catches.
    broken.RequestException = auth_mod.requests.RequestException
    client = web.app.test_client()
    _r, st, _v = start_flow(client)
    restore = with_google(broken)
    try:
        out = client.get("/auth/google/callback?code=c&state=" + st)
    finally:
        restore()
    check("an unreachable Google is an error page, not a crash",
          out.status_code == 400, out.status_code)
    check("and it says what could not be reached",
          "Could not reach Google" in out.data.decode("utf-8", "replace"))

finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

print()
print("%d failure(s)" % len(fails))
print("SIGN-IN FLOW OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
