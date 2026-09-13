"""Accounts, sessions, and the separation between one person's data and another's.

The important test in this file is not that signing in works. It is that two
people signed into the same installation cannot see, change or delete each
other's trades. Adding a login without that would be worse than having no login
at all, because it looks like a boundary while not being one.
"""
import os as _os
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import accounts as A
from bot import auth as auth_mod
from bot import database as db

c, DB = harness.isolated_web("accounts")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def sign_in_as(client, user):
    """Put a real session cookie in the client, as the callback would."""
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token
    return token



def visible(client, path):
    """A page's content with its form hints removed.

    Every symbol input carries placeholder="AAPL" as a hint. Searching the raw
    HTML for a ticker finds that and reports a leak that is not there, so the
    placeholders come out before the page is examined.
    """
    import re as _re
    body = client.get(path).data.decode("utf-8", "replace")
    body = _re.sub(r'placeholder="[^"]*"', "", body)
    body = _re.sub(r'<!--.*?-->', "", body, flags=_re.S)
    return body


print("=" * 72)
print("THE LOCAL ACCOUNT")
print("=" * 72)

conn = db.connect(DB)
local = A.local_user(conn)
again = A.local_user(conn)
check("a local account exists", local.id > 0)
check("asking twice returns the same one", local.id == again.id)
check("it is marked as local", local.is_local)
check("only one local account is ever made",
      len([u for u in A.list_users(conn) if u.is_local]) == 1)
conn.close()

check("no credentials means no sign-in required", web.auth_required() is False)
for path in ("/", "/trades", "/monitor", "/settings", "/account"):
    check("GET %s works with no login" % path, c.get(path).status_code == 200)

print()
print("=" * 72)
print("TWO PEOPLE, ONE INSTALLATION")
print("=" * 72)

conn = db.connect(DB)
alice = A.upsert_google_user(conn, {"email": "alice@example.com", "sub": "g-alice",
                                    "name": "Alice", "email_verified": True})
bob = A.upsert_google_user(conn, {"email": "bob@example.com", "sub": "g-bob",
                                  "name": "Bob", "email_verified": True})
check("two distinct accounts", alice.id != bob.id)
check("signing in again does not duplicate",
      A.upsert_google_user(conn, {"email": "alice@example.com", "sub": "g-alice",
                                  "name": "Alice R", "email_verified": True}).id
      == alice.id)
check("a changed name is taken", A.get_user(conn, alice.id).name == "Alice R")

# The subject id, not the email, is the identity: an address can be reassigned.
moved = A.upsert_google_user(conn, {"email": "alice.r@example.com", "sub": "g-alice",
                                    "name": "Alice R", "email_verified": True})
check("a changed address stays the same account", moved.id == alice.id)
check("and the new address is stored",
      A.get_user(conn, alice.id).email == "alice.r@example.com")

try:
    A.upsert_google_user(conn, {"email": "x@example.com", "sub": "z",
                                "email_verified": False})
    check("an unverified email is refused", False, "it was accepted")
except ValueError:
    check("an unverified email is refused", True)
conn.close()

# Give each of them data, through the web layer, as themselves.
sign_in_as(c, alice)
c.post("/trades/open", data={"symbol": "AAPL", "direction": "long",
                             "entry": "100", "stop": "95", "target": "120",
                             "quantity": "10"})
c.post("/watch/add", data={"symbol": "NVDA", "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "AAPL", "kind": "price_above",
                                   "threshold": "987654", "interval": "1d"})

sign_in_as(c, bob)
c.post("/trades/open", data={"symbol": "TSLA", "direction": "long",
                             "entry": "200", "stop": "190", "target": "230",
                             "quantity": "5"})
c.post("/watch/add", data={"symbol": "NVDA", "interval": "1d"})

conn = db.connect(DB)
a_trades = [r["symbol"] for r in db.list_trades(conn, user=alice.id)]
b_trades = [r["symbol"] for r in db.list_trades(conn, user=bob.id)]
print("   Alice holds %s, Bob holds %s" % (a_trades, b_trades))
check("each trade went to the right person",
      a_trades == ["AAPL"] and b_trades == ["TSLA"])
check("both can watch the same instrument",
      len(db.watchlist(conn, user=alice.id)) == 1
      and len(db.watchlist(conn, user=bob.id)) == 1)
alice_trade = db.list_trades(conn, user=alice.id)[0]["id"]
alice_alert = db.alert_list(conn, user=alice.id)[0]["id"]
conn.close()

print()
print("   Bob is signed in. Nothing of Alice's may reach him.")
page = visible(c, "/trades")
check("Bob's journal shows his own trade", "TSLA" in page)
check("Bob's journal does not show Alice's", "AAPL" not in page, "AAPL leaked")

page = visible(c, "/monitor")
check("Bob sees no alert of Alice's", "AAPL" not in page, "AAPL leaked")
check("nor its threshold", "987654" not in page, "threshold leaked")

page = visible(c, "/portfolio")
check("Bob's portfolio excludes Alice's position", "AAPL" not in page)

export = c.get("/export/trades.csv").data.decode("utf-8", "replace")
check("Bob's export contains his trade", "TSLA" in export)
check("Bob's export excludes Alice's", "AAPL" not in export, "AAPL leaked in export")

r = c.post("/trades/close", data={"trade_id": alice_trade, "price": "120"})
check("Bob cannot close Alice's trade", r.status_code == 400, r.status_code)
r = c.post("/trades/delete", data={"trade_id": alice_trade})
check("Bob cannot delete Alice's trade", r.status_code == 400, r.status_code)
r = c.post("/monitor/alert/delete", data={"alert_id": alice_alert})
check("Bob cannot delete Alice's alert", r.status_code == 400, r.status_code)

conn = db.connect(DB)
check("Alice's trade survived all of that",
      len(db.list_trades(conn, user=alice.id)) == 1)
check("Alice's alert survived too",
      len(db.alert_list(conn, user=alice.id)) == 1)
conn.close()

# And the same in reverse, so the test is not accidentally one-directional.
sign_in_as(c, alice)
page = visible(c, "/trades")
check("Alice sees her own trade", "AAPL" in page)
check("Alice does not see Bob's", "TSLA" not in page, "TSLA leaked")

print()
print("=" * 72)
print("SESSIONS")
print("=" * 72)

conn = db.connect(DB)
token = A.start_session(conn, alice.id, "probe")
check("a session resolves to its user",
      A.user_for_session(conn, token).id == alice.id)
check("a made-up token resolves to nobody",
      A.user_for_session(conn, "not-a-real-token") is None)
check("an empty token resolves to nobody", A.user_for_session(conn, "") is None)

A.end_session(conn, token)
check("ending a session invalidates it", A.user_for_session(conn, token) is None)

expired = A.start_session(conn, alice.id, "old")
conn.execute("UPDATE sessions SET expires_ts=? WHERE token=?",
             (int(time.time()) - 10, expired))
conn.commit()
check("an expired session is refused", A.user_for_session(conn, expired) is None)
check("purging removes it", A.purge_expired(conn) >= 1)

A.end_all_sessions(conn, alice.id)          # start from a known state
t1 = A.start_session(conn, alice.id)
t2 = A.start_session(conn, alice.id)
check("signing out everywhere ends every one of them",
      A.end_all_sessions(conn, alice.id) == 2)
check("both are then dead",
      A.user_for_session(conn, t1) is None and A.user_for_session(conn, t2) is None)
conn.close()

sign_in_as(c, bob)
check("signed in, the journal loads", c.get("/trades").status_code == 200)
r = c.post("/signout")
check("signing out redirects", r.status_code == 302)
# Local mode is still on, so after signing out the local account takes over
# rather than the app becoming unusable.
check("the app still works after signing out", c.get("/trades").status_code == 200)

print()
print("=" * 72)
print("DELETING AN ACCOUNT")
print("=" * 72)

conn = db.connect(DB)
shared_before = conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
counts = A.owned_counts(conn, bob.id)
print("   Bob owns %s" % counts)
check("Bob owns something to delete", counts["trades"] >= 1)

removed = A.delete_user(conn, bob.id)
print("   removed %s" % removed)
check("his trades are gone", A.owned_counts(conn, bob.id)["trades"] == 0)
check("his account is gone", A.get_user(conn, bob.id) is None)
check("Alice is untouched", len(db.list_trades(conn, user=alice.id)) == 1)
check("the shared strategy record is untouched",
      conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
      == shared_before)

try:
    A.delete_user(conn, local.id)
    check("the local account cannot be deleted", False, "it was deleted")
except ValueError:
    check("the local account cannot be deleted", True)
try:
    A.delete_user(conn, 999999)
    check("deleting a missing account is refused", False)
except ValueError:
    check("deleting a missing account is refused", True)
conn.close()

print()
print("=" * 72)
print("THE GOOGLE FLOW")
print("=" * 72)

check("with no credentials it is not configured", not auth_mod.configured())
check("and it says which one is missing", "GOOGLE_CLIENT_ID" in auth_mod.why_not())

_os.environ["GOOGLE_CLIENT_ID"] = "test-id.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret-value"
web.app.config["AUTH_MODE"] = None
try:
    check("credentials switch it on", auth_mod.configured())
    check("and sign-in becomes required", web.auth_required() is True)

    verifier = auth_mod.new_verifier()
    state = auth_mod.new_state()
    check("a verifier is long enough to be a verifier", 43 <= len(verifier) <= 128)
    check("two verifiers differ", verifier != auth_mod.new_verifier())
    check("two states differ", state != auth_mod.new_state())

    url = auth_mod.authorization_url("http://localhost:8000/auth/google/callback",
                                     state, verifier)
    for piece in ("accounts.google.com", "code_challenge_method=S256",
                  "response_type=code", "scope=openid", "state=" + state):
        check("the authorize url has %s" % piece, piece in url)
    check("the secret is not in the url", "test-secret-value" not in url)
    check("the verifier is not in the url", verifier not in url)

    # A fresh client, so no session exists yet.
    anon = web.app.test_client()
    r = anon.get("/")
    check("an anonymous visitor is sent to sign in",
          r.status_code == 302 and "/signin" in r.headers.get("Location", ""))
    r = anon.get("/api/alerts/status")
    check("the api answers 401 rather than redirecting", r.status_code == 401)
    check("the sign-in page is reachable", anon.get("/signin").status_code == 200)
    check("static files stay reachable",
          anon.get("/static/style.css").status_code == 200)

    r = anon.get("/auth/google/callback?code=abc&state=forged")
    check("a forged state is refused", r.status_code == 400)
    r = anon.get("/auth/google/callback?state=x")
    check("a callback with no code is refused", r.status_code == 400)

    _os.environ["GOOGLE_ALLOWED_DOMAINS"] = "example.com"
    check("a domain restriction is read", auth_mod.allowed_domains() == ("example.com",))
    auth_mod.check_domain("someone@example.com")
    check("an allowed domain passes", True)
    try:
        auth_mod.check_domain("someone@elsewhere.com")
        check("a disallowed domain is refused", False, "it was allowed")
    except auth_mod.AuthError:
        check("a disallowed domain is refused", True)
    del _os.environ["GOOGLE_ALLOWED_DOMAINS"]
    auth_mod.check_domain("anyone@anywhere.com")
    check("with no restriction anyone passes", True)
finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

check("removing the credentials restores local mode", web.auth_required() is False)

print()
print("=" * 72)
print("THE COOKIE KEY")
print("=" * 72)

import tempfile
key_path = _os.path.join(tempfile.mkdtemp(), "secret.key")
first = auth_mod.session_secret(key_path)
second = auth_mod.session_secret(key_path)
check("a key is generated", len(first) >= 32)
check("it is stable across calls", first == second)

# A key is random bytes, and roughly one in twenty begins or ends with a byte
# that happens to be whitespace. Reading one back must return exactly what was
# written, so this forces the cases that would otherwise show up as an
# occasional mysterious sign-out.
# 0x20 space, 0x09 tab, 0x0a newline, 0x0d return, 0x0b and 0x0c the two
# vertical forms. Built numerically so the bytes are unmistakable.
for edge in (bytes([n]) for n in (0x20, 0x09, 0x0A, 0x0D, 0x0B, 0x0C)):
    import secrets as _secrets
    path = _os.path.join(tempfile.mkdtemp(), "secret.key")
    written = edge + _secrets.token_bytes(46) + edge
    with open(path, "wb") as fh:
        fh.write(written)
    check("a key wrapped in %r survives being read back" % edge,
          auth_mod.session_secret(path) == written,
          "%d bytes written, %d read" % (len(written),
                                         len(auth_mod.session_secret(path))))
check("it is not a fixed constant",
      auth_mod.session_secret(_os.path.join(tempfile.mkdtemp(), "k")) != first)

_os.environ["STOCKBOT_SECRET_KEY"] = "a-key-from-the-environment-that-is-long"
check("the environment wins",
      auth_mod.session_secret(key_path).decode() ==
      "a-key-from-the-environment-that-is-long")
del _os.environ["STOCKBOT_SECRET_KEY"]


print()
print("=" * 72)
print("READY TO BE PUT ON THE INTERNET")
print("=" * 72)

# Google fetches the privacy policy itself, anonymously, before it will let an
# external OAuth client out of testing. If these need a login, registration
# cannot be completed.
_os.environ["GOOGLE_CLIENT_ID"] = "deploy-check.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "deploy-check-secret"
web.app.config["AUTH_MODE"] = None
try:
    stranger = web.app.test_client()
    for path in ("/privacy", "/terms", "/healthz", "/signin", "/static/style.css"):
        check("%s is readable without signing in" % path,
              stranger.get(path).status_code == 200)
    for path in ("/", "/trades", "/account", "/export/trades.csv", "/settings"):
        r = stranger.get(path)
        check("%s still needs an account" % path,
              r.status_code == 302 and "/signin" in r.headers.get("Location", ""),
              r.status_code)

    body = stranger.get("/privacy").data.decode("utf-8", "replace")
    for claim in ("openid", "profile", "No analytics or tracking",
                  "No selling, renting or sharing"):
        check("the privacy page states: %s" % claim[:34], claim in body)
    terms_body = stranger.get("/terms").data.decode("utf-8", "replace")
    check("the terms say it is not advice",
          "not financial advice" in terms_body)
    check("and that most day trading loses money",
          "loses money for the large majority" in terms_body)

    health = stranger.get("/healthz").get_json()
    check("the health check reports ok", health.get("ok") is True)
    check("and leaks nothing about anyone",
          set(health) <= {"ok", "service", "detail"}, health)
finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

# The callback address has to be predictable, because it must be typed into
# Google's console exactly and a mismatch is the usual cause of failure.
_os.environ["STOCKBOT_PUBLIC_URL"] = "https://stockbot.example.com"
try:
    with web.app.test_request_context("/"):
        uri = web._redirect_uri()
    check("the callback is built from the public address",
          uri == "https://stockbot.example.com/auth/google/callback", uri)
    _os.environ["GOOGLE_REDIRECT_URI"] = "https://override.example.com/cb"
    with web.app.test_request_context("/"):
        check("an explicit override wins",
              web._redirect_uri() == "https://override.example.com/cb")
    del _os.environ["GOOGLE_REDIRECT_URI"]
finally:
    del _os.environ["STOCKBOT_PUBLIC_URL"]

# Every dependency the container installs must be one that is actually here,
# and every third-party import must be one the container installs. The second
# half is the one that bites: a missing pin lets the image build and start, and
# fails only when the code path that needs it first runs.
import ast as _ast
import importlib.metadata as _md

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
req_path = _os.path.join(ROOT, "requirements.txt")
pinned = [l.split("#")[0].strip() for l in open(req_path, encoding="utf-8")
          if l.strip() and not l.startswith("#")]
pinned = [l for l in pinned if l]
check("requirements.txt lists something", len(pinned) >= 5)

req_names = set()
for line in pinned:
    name, _, want = line.partition("==")
    req_names.add(name.lower().replace("-", "_"))
    check("%s is pinned to a version" % name, bool(want), line)
    try:
        have = _md.version(name)
        check("%s pin matches what was tested" % name, have == want,
              "pinned %s, have %s" % (want, have))
    except _md.PackageNotFoundError:
        check("%s is actually installed" % name, False)

_STDLIB = set(_sys.stdlib_module_names)
_LOCAL = {"bot", "web", "analyze", "paper", "menu", "serve", "harness", "setup"}
_ALIAS = {"pil": "pillow", "dotenv": "python_dotenv"}

imported = {}
for folder, _dirs, files in _os.walk(ROOT):
    if any(skip in folder for skip in (".runtime", "__pycache__", ".git", "tests")):
        continue
    for fname in files:
        if not fname.endswith(".py"):
            continue
        full = _os.path.join(folder, fname)
        try:
            tree = _ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError:
            check("%s parses" % fname, False)
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    imported.setdefault(a.name.split(".")[0], set()).add(fname)
            elif isinstance(node, _ast.ImportFrom) and node.level == 0 and node.module:
                imported.setdefault(node.module.split(".")[0], set()).add(fname)

third_party = {n: w for n, w in imported.items()
               if n not in _STDLIB and n not in _LOCAL}
check("the scan found the imports at all", len(third_party) >= 5)
for name, where in sorted(third_party.items()):
    check("%s is in requirements.txt" % name,
          _ALIAS.get(name.lower(), name.lower()) in req_names,
          "imported by %s" % ", ".join(sorted(where)[:3]))

print()
print("%d failure(s)" % len(fails))
print("ACCOUNTS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
