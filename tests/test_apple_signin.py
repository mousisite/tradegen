"""Sign in with Apple: the website's flow, the app's own sheet, and leaving.

Apple is stubbed at the HTTP boundary, as Google is in test_signin_flow.py.
Its keys, its token endpoint and its revocation endpoint answer from here,
with an RSA key made for this run standing in for Apple's signing key and an
EC P-256 key standing in for the .p8 file. Everything below that boundary is
the real code: the client secret is really signed, every token is really
verified, accounts are really made and joined, and sessions really start.
"""
import base64
import hashlib
import hmac
import json
import os as _os
import sys as _sys
import time
from urllib.parse import parse_qs, urlparse

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import requests as real_requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import web
from bot import accounts as A
from bot import apple as apple_mod
from bot import database as db
from bot import demo as demo_mod

c, DB = harness.isolated_web("apple")

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


# --- a stand-in for Apple ---------------------------------------------------

APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NEW_APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
P8 = ec.generate_private_key(ec.SECP256R1())
P8_PEM = P8.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode("ascii")

TEAM, KEY_ID, SERVICES, BUNDLE = ("TEAM123456", "KEY1234567", "app.orenth.web",
                                  "app.orenth.ios")


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk(key, kid):
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
            "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64(numbers.e.to_bytes(3, "big"))}


def sign(claims, key=APPLE_KEY, kid="apple-1", alg="RS256"):
    head = b64(json.dumps({"alg": alg, "kid": kid}).encode("utf-8"))
    body = b64(json.dumps(claims).encode("utf-8"))
    signing = ("%s.%s" % (head, body)).encode("ascii")
    if alg == "RS256":
        signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    elif alg == "HS256":
        # The classic forgery: HMAC keyed with the public key's own PEM.
        pem = APPLE_KEY.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        signature = hmac.new(pem, signing, hashlib.sha256).digest()
    else:
        signature = b""
    return "%s.%s.%s" % (head, body, b64(signature))


def claims_for(sub, aud, nonce=None, **extra):
    now = int(time.time())
    out = {"iss": apple_mod.ISSUER, "aud": aud, "iat": now, "exp": now + 600,
           "sub": sub, "email": "%s@example.com" % sub, "email_verified": "true",
           "is_private_email": "false", "auth_time": now,
           "nonce_supported": True}
    if nonce is not None:
        out["nonce"] = nonce
    out.update(extra)
    return out


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeApple:
    """Answers the way Apple does, and records what it was sent."""

    def __init__(self):
        self.keys = [jwk(APPLE_KEY, "apple-1")]
        self.key_gets = 0
        self.posts = []
        self.codes = {}
        self.revoke_status = 200
        self.down = False

    def get(self, url, timeout=None, headers=None):
        if self.down:
            raise real_requests.ConnectionError("no route to Apple")
        self.key_gets += 1
        if url != apple_mod.KEYS_URL:
            return FakeResponse(404, {})
        return FakeResponse(200, {"keys": list(self.keys)})

    def post(self, url, data=None, timeout=None, headers=None):
        self.posts.append({"url": url, "data": dict(data or {})})
        if self.down:
            raise real_requests.ConnectionError("no route to Apple")
        if url == apple_mod.TOKEN_URL:
            # One use each, as at Apple.
            who = self.codes.pop((data or {}).get("code"), None)
            if who is None:
                return FakeResponse(400, {"error": "invalid_grant"})
            claims = claims_for(who["sub"], data["client_id"], who.get("nonce"),
                                email=who.get("email", ""),
                                is_private_email="true" if who.get("relay")
                                else "false")
            return FakeResponse(200, {
                "access_token": "a.fake", "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": "r.%s" % who["sub"],
                "id_token": sign(claims)})
        if url == apple_mod.REVOKE_URL:
            return FakeResponse(self.revoke_status,
                                {} if self.revoke_status == 200
                                else {"error": "invalid_client"})
        return FakeResponse(404, {})


def fresh_keys():
    """Forget Apple's keys, as a process starting would."""
    apple_mod._KEYS.update({"keys": {}, "fetched": 0.0, "tried": 0.0})


FAKE = FakeApple()
_real_requests = apple_mod.requests
apple_mod.requests = FAKE

ENV = {"APPLE_TEAM_ID": TEAM, "APPLE_SIGNIN_KEY_ID": KEY_ID,
       # As a hosting dashboard keeps it: on one line, with literal \n.
       "APPLE_SIGNIN_PRIVATE_KEY": P8_PEM.strip().replace("\n", "\\n"),
       "APPLE_SERVICES_ID": SERVICES}


def apple_on(services=True):
    for key, value in ENV.items():
        if key != "APPLE_SERVICES_ID" or services:
            _os.environ[key] = value
    if not services:
        _os.environ.pop("APPLE_SERVICES_ID", None)
    _os.environ.pop("APPLE_BUNDLE_ID", None)


def apple_off():
    for key in list(ENV) + ["APPLE_BUNDLE_ID"]:
        _os.environ.pop(key, None)


def jwt_parts(token):
    head, body, sig = token.split(".")
    return json.loads(unb64(head)), json.loads(unb64(body)), unb64(sig), \
        ("%s.%s" % (head, body)).encode("ascii")


def web_sign_in(sub, email, relay=False, user=None, next_path="/trades",
                starter=None, app_challenge=""):
    """The whole website flow: start here, sign in at Apple, be posted back.

    Apple's post comes from appleid.apple.com, a cross-site POST, so the
    browser sends it only the SameSite=None flow cookie, never the session.
    A fresh client holding just that cookie is that request.
    """
    starter = starter or web.app.test_client()
    path = "/auth/apple?next=" + next_path
    if app_challenge:
        path += "&app=1&challenge=" + app_challenge
    start = starter.get(path)
    query = parse_qs(urlparse(start.headers.get("Location", "")).query)
    flow = starter.get_cookie("apple_flow", path="/auth/apple")
    code = "c.%s.%d" % (sub, len(FAKE.posts))
    FAKE.codes[code] = {"sub": sub, "email": email, "relay": relay,
                        "nonce": query.get("nonce", [""])[0]}
    browser = web.app.test_client()
    if flow is not None:
        browser.set_cookie("apple_flow", flow.decoded_value, path="/auth/apple")
    form = {"code": code, "state": query.get("state", [""])[0]}
    if user is not None:
        form["user"] = json.dumps(user)
    back = browser.post("/auth/apple/callback", data=form,
                        headers={"Origin": "https://appleid.apple.com",
                                 "Sec-Fetch-Site": "cross-site"})
    return back, browser, start


def people():
    conn = db.connect(DB)
    try:
        return {u.email: u for u in A.list_users(conn)
                if not u.is_local and u.provider != "demo"}
    finally:
        conn.close()


def identity_row(sub):
    conn = db.connect(DB)
    try:
        return conn.execute("SELECT * FROM identities WHERE provider='apple' AND "
                            "subject=?", (sub,)).fetchone()
    finally:
        conn.close()


web.app.config["AUTH_MODE"] = None

try:
    section("NOTHING ABOUT APPLE WHILE IT IS UNSET")

    apple_off()
    web.app.config["AUTH_MODE"] = "google"        # a deployment, as a visitor
    visitor = web.app.test_client()
    page = visitor.get("/signin").data.decode("utf-8", "replace")
    check("the sign-in page has no Apple button", "with Apple" not in page
          and "abtn" not in page)
    check("and loads no app script when nothing opens elsewhere",
          "signin-app.js" not in page)
    privacy = visitor.get("/privacy").data.decode("utf-8", "replace")
    check("the privacy page says nothing of Apple sign-in",
          "with Apple" not in privacy and "privaterelay" not in privacy)
    for path in ("/auth/apple", "/auth/apple/nonce"):
        check("GET %s does not exist" % path,
              visitor.get(path).status_code == 404)
    for path in ("/auth/apple/callback", "/auth/apple/native",
                 "/auth/apple/notifications", "/auth/app/finish"):
        check("POST %s does not exist" % path,
              visitor.post(path).status_code == 404)
    check("and nobody is told how to set it up", "APPLE_" not in page
          and "APPLE_" not in privacy)
    check("nothing is ready", not apple_mod.native_ready()
          and not apple_mod.web_ready() and apple_mod.why_not() == "")
    web.app.config["AUTH_MODE"] = None

    section("THE CLIENT SECRET")

    apple_on()
    check("a key pasted on one line, with literal \\n, is read",
          apple_mod.native_ready() and apple_mod.web_ready())
    secret = apple_mod.client_secret(SERVICES)
    header, claims, signature, signing = jwt_parts(secret)
    print("      header %s" % header)
    print("      claims %s" % {k: v for k, v in claims.items() if k not in ("iat", "exp")})
    check("it is ES256 with the key's id", header == {"alg": "ES256", "kid": KEY_ID},
          header)
    check("issued by the team, for Apple, about this client",
          claims["iss"] == TEAM and claims["aud"] == "https://appleid.apple.com"
          and claims["sub"] == SERVICES, claims)
    check("issued now", abs(claims["iat"] - time.time()) < 5, claims["iat"])
    lifetime = claims["exp"] - claims["iat"]
    check("lasting about an hour, far inside Apple's six months",
          0 < lifetime <= 3600 and lifetime <= 180 * 86400, lifetime)
    try:
        P8.public_key().verify(
            encode_dss_signature(int.from_bytes(signature[:32], "big"),
                                 int.from_bytes(signature[32:], "big")),
            signing, ec.ECDSA(hashes.SHA256()))
        good = len(signature) == 64
    except InvalidSignature:
        good = False
    check("and really signed by the .p8 key, as r and s side by side", good)
    check("it is reused while it lasts", apple_mod.client_secret(SERVICES) == secret)
    check("another client id gets its own",
          jwt_parts(apple_mod.client_secret(BUNDLE))[1]["sub"] == BUNDLE)

    _os.environ["APPLE_SIGNIN_PRIVATE_KEY"] = "".join(
        line for line in P8_PEM.splitlines() if "-----" not in line)
    check("a key pasted without its BEGIN and END lines still works",
          apple_mod.native_ready())
    _os.environ["APPLE_SIGNIN_PRIVATE_KEY"] = "not a key at all"
    check("a key that cannot be read switches Apple off",
          not apple_mod.native_ready() and "readable" in apple_mod.why_not())
    _os.environ["APPLE_SIGNIN_PRIVATE_KEY"] = ENV["APPLE_SIGNIN_PRIVATE_KEY"]

    section("CHECKING A TOKEN APPLE SIGNED")

    fresh_keys()
    nonce = apple_mod.new_nonce()
    issued = apple_mod.nonce_hash(nonce)
    good = sign(claims_for("001.abc.1", BUNDLE, nonce))
    who = apple_mod.verify_identity(good, BUNDLE, issued)
    check("a good token is accepted", who["sub"] == "001.abc.1", who)
    check("and Apple's keys were fetched once", FAKE.key_gets == 1, FAKE.key_gets)
    apple_mod.verify_identity(sign(claims_for("001.abc.1", BUNDLE, nonce)), BUNDLE,
                              issued)
    check("then reused from the cache", FAKE.key_gets == 1, FAKE.key_gets)
    hashed = apple_mod.verify_identity(sign(claims_for("001.abc.1", BUNDLE, issued)),
                                       BUNDLE, issued)
    check("a nonce passed to Apple already hashed is the same nonce",
          hashed["sub"] == "001.abc.1")
    check("\"true\" in quotes counts as true",
          who["email_verified"] is True and who["is_private_email"] is False)

    def refused(label, token, audience=BUNDLE, issued_hash=issued):
        try:
            apple_mod.verify_identity(token, audience, issued_hash)
        except apple_mod.AppleError:
            check(label, True)
            return
        except Exception as exc:          # anything else would be a 500
            check(label, False, "raised %r" % exc)
            return
        check(label, False, "accepted")

    refused("a bad signature is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce), key=STRANGER_KEY))
    refused("another client's token is refused",
            sign(claims_for("001.abc.1", "com.someone.else", nonce)))
    refused("another issuer is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce, iss="https://evil.example")))
    refused("an expired token is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce, exp=int(time.time()) - 600)))
    refused("a token dated in the future is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce, iat=int(time.time()) + 3600)))
    refused("a token from long ago is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce, iat=int(time.time()) - 3 * 86400)))
    refused("the wrong nonce is refused",
            sign(claims_for("001.abc.1", BUNDLE, apple_mod.new_nonce())))
    refused("no nonce at all is refused", sign(claims_for("001.abc.1", BUNDLE)))
    refused("alg none is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce), alg="none"))
    refused("HS256 keyed with Apple's public key is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce), alg="HS256"))
    refused("no subject is refused",
            sign(claims_for("", BUNDLE, nonce)))
    for label, junk in (("an empty token", ""), ("one part", "abc"),
                        ("two parts", "a.b"), ("letters base64 never uses",
                                                "a!.b.c"),
                        ("text that is not ASCII", "\u00e9.\u00e9.\u00e9"),
                        ("not a string", None), ("a very long one", "a." * 9000 + "a")):
        refused("%s is refused, not a crash" % label, junk)

    # As if the keys were last fetched more than a minute ago.
    apple_mod._KEYS["tried"] = time.time() - 61
    gets = FAKE.key_gets
    refused("a key Apple does not list is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce), kid="made-up"))
    check("Apple was asked again for its keys", FAKE.key_gets == gets + 1,
          FAKE.key_gets - gets)
    refused("a second made-up key a moment later is refused",
            sign(claims_for("001.abc.1", BUNDLE, nonce), kid="made-up-2"))
    check("without asking Apple again within the minute",
          FAKE.key_gets == gets + 1, FAKE.key_gets - gets)

    # Apple rotates its keys. A token signed with the new one arrives before
    # the cache has seen it: the keys are fetched again, and it verifies.
    FAKE.keys = [jwk(APPLE_KEY, "apple-1"), jwk(NEW_APPLE_KEY, "apple-2")]
    apple_mod._KEYS["tried"] = time.time() - 61
    rotated = apple_mod.verify_identity(
        sign(claims_for("001.abc.1", BUNDLE, nonce), key=NEW_APPLE_KEY,
             kid="apple-2"), BUNDLE, issued)
    check("a key Apple has just added is fetched and used",
          rotated["sub"] == "001.abc.1")

    FAKE.down = True
    fresh_keys()
    refused("with Apple out of reach and no keys held, nothing gets in",
            sign(claims_for("001.abc.1", BUNDLE, nonce)))
    FAKE.down = False

    section("THE WEBSITE: STARTING, AND APPLE'S POST BACK FROM ITS OWN SITE")

    fresh_keys()
    starter = web.app.test_client()
    start = starter.get("/auth/apple?next=/trades")
    where = urlparse(start.headers.get("Location", ""))
    query = parse_qs(where.query)
    check("starting sends the browser to Apple", start.status_code == 302 and
          "%s://%s%s" % (where.scheme, where.netloc, where.path)
          == apple_mod.AUTHORIZE_URL, start.headers.get("Location"))
    for key, want in (("client_id", SERVICES), ("response_type", "code"),
                      ("scope", "name email"), ("response_mode", "form_post"),
                      ("redirect_uri", "http://localhost/auth/apple/callback")):
        check("with %s=%s" % (key, want), query.get(key) == [want], query.get(key))
    check("and a state and a nonce", query.get("state") and query.get("nonce"))
    check("the scope's space is sent as %20, as Apple documents",
          "scope=name%20email" in start.headers["Location"])
    set_cookie = [h for h in start.headers.getlist("Set-Cookie")
                  if h.startswith("apple_flow=")]
    cookie = set_cookie[0] if set_cookie else ""
    print("      %s..." % cookie.split(";")[0][:30])
    print("      %s" % "; ".join(p.strip() for p in cookie.split(";")[1:]))
    for part in ("Secure", "HttpOnly", "SameSite=None", "Path=/auth/apple"):
        check("the flow cookie is %s" % part, part in cookie, cookie)
    check("and the redirect is never cached",
          start.headers.get("Cache-Control") == "no-store")
    check("the state is not left in the session cookie",
          not any(h.startswith("session=") for h in start.headers.getlist("Set-Cookie")))

    back, browser, _start = web_sign_in(
        "000111.aaaa.0101", "robin@example.com",
        user={"name": {"firstName": "Robin", "lastName": "Vale"},
              "email": "robin@example.com"})
    check("Apple's post back signs the person in", back.status_code == 302 and
          back.headers.get("Location", "").endswith("/trades"),
          (back.status_code, back.headers.get("Location")))
    check("its cookie is spent", any(h.startswith("apple_flow=;") or
                                     h.startswith('apple_flow="";')
                                     for h in back.headers.getlist("Set-Cookie")),
          back.headers.getlist("Set-Cookie"))
    account = browser.get("/account").data.decode("utf-8", "replace")
    check("the session works on the next page", "robin@example.com" in account)
    check("with the name Apple sent the first time", "Robin Vale" in account)
    check("and the account page says how they signed in",
          "Signed in with Apple" in account)

    exchange = [p for p in FAKE.posts if p["url"] == apple_mod.TOKEN_URL][-1]["data"]
    print("      what the server sent Apple:")
    for key in sorted(exchange):
        print("        %-14s %s" % (key, "<signed, not printed>"
                                     if key == "client_secret" else exchange[key]))
    check("the code was traded with the Services ID",
          exchange["client_id"] == SERVICES and
          exchange["grant_type"] == "authorization_code", exchange)
    check("and the same redirect_uri Apple was sent",
          exchange["redirect_uri"] == "http://localhost/auth/apple/callback")
    check("and a client secret made for that client",
          jwt_parts(exchange["client_secret"])[1]["sub"] == SERVICES)
    row = identity_row("000111.aaaa.0101")
    check("the refresh token is kept, for ending it later",
          row is not None and row["refresh_token"] == "r.000111.aaaa.0101"
          and row["client_id"] == SERVICES)

    replay = browser.post("/auth/apple/callback",
                          data={"code": "anything", "state": query["state"][0]})
    check("the same post back cannot be used twice", replay.status_code == 400,
          replay.status_code)

    before = len(FAKE.posts)
    outsider = web.app.test_client()
    forged = outsider.post("/auth/apple/callback",
                           data={"code": "c.x", "state": query["state"][0]},
                           headers={"Sec-Fetch-Site": "cross-site"})
    check("without the flow cookie the post back is refused",
          forged.status_code == 400, forged.status_code)
    check("and says to start again",
          "did not match this browser" in forged.data.decode("utf-8", "replace"))
    check("without asking Apple anything", len(FAKE.posts) == before)
    check("and nobody was signed in",
          outsider.get("/account").status_code == 302)

    starter = web.app.test_client()
    starter.get("/auth/apple")
    flow = starter.get_cookie("apple_flow", path="/auth/apple")
    wrong = web.app.test_client()
    wrong.set_cookie("apple_flow", flow.decoded_value, path="/auth/apple")
    out = wrong.post("/auth/apple/callback", data={"code": "c", "state": "guessed"})
    check("a flow cookie with the wrong state is refused", out.status_code == 400)

    wrong.set_cookie("apple_flow", flow.decoded_value[:-3] + "AAA", path="/auth/apple")
    out = wrong.post("/auth/apple/callback", data={"code": "c", "state": "x"})
    check("a cookie that was tampered with is refused", out.status_code == 400)

    starter = web.app.test_client()
    start = starter.get("/auth/apple")
    flow = starter.get_cookie("apple_flow", path="/auth/apple")
    cancel = web.app.test_client()
    cancel.set_cookie("apple_flow", flow.decoded_value, path="/auth/apple")
    out = cancel.post("/auth/apple/callback", data={
        "error": "user_cancelled_authorize",
        "state": parse_qs(urlparse(start.headers["Location"]).query)["state"][0]})
    check("cancelling at Apple says so", out.status_code == 400 and
          "cancelled" in out.data.decode("utf-8", "replace"))

    section("THE NAME ARRIVES ONCE")

    web_sign_in("000111.aaaa.0101", "robin@example.com")
    web_sign_in("000111.aaaa.0101", "robin@example.com",
                user={"name": {"firstName": "Someone", "lastName": "Else"}})
    robin = people()["robin@example.com"]
    check("signing in again without a name keeps the first one",
          robin.name == "Robin Vale", robin.name)
    check("and a name posted later does not replace it", robin.name == "Robin Vale")
    check("one account, however many sign-ins",
          sum(1 for u in people().values() if u.provider == "apple") == 1)

    noisy = apple_mod.name_from_post(json.dumps(
        {"name": {"firstName": "Ann\n<b>", "lastName": "x" * 500}}))
    check("a posted name is plain text of a sane length",
          "\n" not in noisy and len(noisy) <= 130, noisy)
    check("a name that is not JSON is ignored",
          apple_mod.name_from_post("{not json") == "")
    try:
        nested = apple_mod.name_from_post("[" * 999 + "]" * 999)
    except RecursionError:
        nested = None
    check("a name nested deeper than Python reads is ignored, not a crash",
          nested == "", nested)

    section("WHICH ACCOUNT AN APPLE ID REACHES")

    conn = db.connect(DB)
    try:
        dana = A.upsert_google_user(conn, {"sub": "g-dana", "email": "dana@example.com",
                                           "email_verified": True, "name": "Dana"})
        linked = A.upsert_apple_user(conn, {"sub": "a-dana", "email": "dana@example.com",
                                            "email_verified": True,
                                            "is_private_email": False})
        check("a verified address that is not a relay joins its account",
              linked.id == dana.id, (linked.id, dana.id))
        check("and the account now has both ways in",
              A.sign_in_methods(conn, A.get_user(conn, dana.id)) == ["Google", "Apple"],
              A.sign_in_methods(conn, A.get_user(conn, dana.id)))
        again = A.upsert_apple_user(conn, {"sub": "a-dana", "email": "",
                                           "email_verified": False})
        check("the same Apple ID comes back to it, address or not",
              again.id == dana.id)

        relay = A.upsert_apple_user(conn, {"sub": "a-relay",
                                           "email": "k2x9@privaterelay.appleid.com",
                                           "email_verified": True,
                                           "is_private_email": True})
        check("a private relay address makes its own account", relay.id != dana.id)
        check("and the account is reachable at that address, which forwards",
              relay.email == "k2x9@privaterelay.appleid.com", relay.email)

        sam = A.upsert_email_user(conn, "sam@example.com")
        unverified = A.upsert_apple_user(conn, {"sub": "a-sam", "email": "sam@example.com",
                                                "email_verified": False})
        check("an address Apple has not verified joins nothing",
              unverified.id != sam.id)
        check("and the new account does not take that address",
              unverified.email.endswith("@users.invalid"), unverified.email)

        icloud = A.upsert_email_user(conn, "hidden.one@icloud.com")
        marked = A.upsert_apple_user(conn, {"sub": "a-icloud",
                                            "email": "hidden.one@icloud.com",
                                            "email_verified": True,
                                            "is_private_email": True})
        check("an icloud.com address Apple marks private joins nothing",
              marked.id != icloud.id)
        coded = A.upsert_email_user(conn, "x1@private.icloud.com")
        unmarked = A.upsert_apple_user(conn, {"sub": "a-pic",
                                              "email": "x1@private.icloud.com",
                                              "email_verified": True})
        check("nor does an address at private.icloud.com, marked or not",
              unmarked.id != coded.id)
        check("the token reads a private.icloud.com address as hidden",
              apple_mod.is_relay("X1@Private.iCloud.com")
              and not apple_mod.is_relay("someone@icloud.com"))

        A.upsert_email_user(conn, "zz9@privaterelay.appleid.com")
        hidden = A.upsert_apple_user(conn, {"sub": "a-zz9",
                                            "email": "zz9@privaterelay.appleid.com",
                                            "email_verified": True,
                                            "is_private_email": True})
        check("a relay address never joins an account already using it",
              hidden.email.endswith("@users.invalid"), hidden.email)

        local = A.local_user(conn)
        not_local = A.upsert_apple_user(conn, {"sub": "a-local", "email": local.email,
                                               "email_verified": True})
        check("the local account is never joined", not_local.id != local.id
              and not not_local.is_local)
        example = demo_mod.user(conn)
        not_demo = A.upsert_apple_user(conn, {"sub": "a-demo", "email": example.email,
                                              "email_verified": True})
        check("the example account is never joined", not_demo.id != example.id
              and not_demo.provider == "apple")

        try:
            A.upsert_apple_user(conn, {"sub": "a-new-owner", "email": "dana@example.com",
                                       "email_verified": True})
            taken = False
        except ValueError:
            taken = True
        check("an address already joined to another Apple ID cannot take the "
              "account over", taken)

        A.unlink_identity(conn, "apple", "a-relay")
        back_again = A.upsert_apple_user(conn, {"sub": "a-relay",
                                                "email": "k2x9@privaterelay.appleid.com",
                                                "email_verified": True,
                                                "is_private_email": True})
        check("after taking consent back, signing in again reaches the same account",
              back_again.id == relay.id, (back_again.id, relay.id))
    finally:
        conn.close()

    section("THE APP'S OWN APPLE SHEET")

    web._RATE.clear()
    phone = web.app.test_client()
    got = phone.get("/auth/apple/nonce")
    check("the app can ask for a nonce", got.status_code == 200, got.status_code)
    check("which is never cached", got.headers.get("Cache-Control") == "no-store")
    nonce = got.get_json()["nonce"]
    with phone.session_transaction() as sess:
        kept = sess.get("apple_nonce", {})
    check("the session keeps its hash, not the nonce",
          kept.get("hash") == apple_mod.nonce_hash(nonce) and nonce not in json.dumps(kept))
    session_cookie = phone.get_cookie("session")
    FAKE.codes["n.code.1"] = {"sub": "002.native.1", "email": "nat@example.com"}
    token = sign(claims_for("002.native.1", BUNDLE, nonce, email="nat@example.com"))
    body = {"identityToken": token, "authorizationCode": "n.code.1",
            "givenName": "Sam", "familyName": "Reyes", "email": "", "next": "/trades"}
    answer = phone.post("/auth/apple/native", json=body,
                        headers={"Origin": "http://localhost",
                                 "Sec-Fetch-Site": "same-origin"})
    check("the identity token signs the app in", answer.status_code == 200
          and answer.get_json() == {"ok": True, "next": "/trades"},
          (answer.status_code, answer.get_json()))
    page = phone.get("/account").data.decode("utf-8", "replace")
    check("and the session works", "nat@example.com" in page and "Sam Reyes" in page)
    redeemed = [p for p in FAKE.posts if p["data"].get("code") == "n.code.1"]
    check("the app's code was traded with the bundle id",
          redeemed and redeemed[0]["data"]["client_id"] == BUNDLE and
          "redirect_uri" not in redeemed[0]["data"] and
          jwt_parts(redeemed[0]["data"]["client_secret"])[1]["sub"] == BUNDLE)
    row = identity_row("002.native.1")
    check("and its refresh token kept against that client",
          row is not None and row["refresh_token"] == "r.002.native.1"
          and row["client_id"] == BUNDLE)

    again = phone.post("/auth/apple/native", json=body,
                       headers={"Origin": "http://localhost"})
    check("the same token cannot sign in twice", again.status_code == 400)
    replayer = web.app.test_client()
    replayer.set_cookie("session", session_cookie.decoded_value)
    replay = replayer.post("/auth/apple/native", json=body,
                           headers={"Origin": "http://localhost"})
    check("not even with the session cookie from before it was used",
          replay.status_code == 400 and "already used" in replay.get_json()["error"],
          (replay.status_code, replay.get_json()))

    def native(token_for, origin="http://localhost", fetch_site="same-origin",
               ask=True):
        client = web.app.test_client()
        issued_nonce = client.get("/auth/apple/nonce").get_json()["nonce"] if ask else "x"
        headers = {}
        if origin:
            headers["Origin"] = origin
        if fetch_site:
            headers["Sec-Fetch-Site"] = fetch_site
        return client.post("/auth/apple/native", headers=headers, json={
            "identityToken": token_for(issued_nonce), "authorizationCode": ""})

    out = native(lambda n: sign(claims_for("002.native.2", BUNDLE,
                                           apple_mod.nonce_hash(n))))
    check("a nonce the app hashed first is accepted too", out.status_code == 200,
          out.get_json())
    out = native(lambda n: sign(claims_for("002.native.2", BUNDLE, "other")))
    check("the wrong nonce is refused", out.status_code == 400)
    out = native(lambda n: sign(claims_for("002.native.2", SERVICES, n)))
    check("a token for the website is refused in the app", out.status_code == 400)
    out = native(lambda n: sign(claims_for("002.native.2", BUNDLE, n)),
                 origin="https://evil.example", fetch_site="cross-site")
    check("a post from another site is refused", out.status_code == 403)
    out = native(lambda n: sign(claims_for("002.native.2", BUNDLE, n)),
                 origin="https://evil.example", fetch_site="")
    check("an Origin from another site alone is enough to refuse",
          out.status_code == 403)
    out = native(lambda n: sign(claims_for("002.native.2", BUNDLE, n)), ask=False)
    check("a session that was never given a nonce is refused",
          out.status_code == 400)
    slow = web.app.test_client()
    slow_nonce = slow.get("/auth/apple/nonce").get_json()["nonce"]
    with slow.session_transaction() as sess:
        sess["apple_nonce"] = dict(sess["apple_nonce"],
                                   at=int(time.time()) - apple_mod.NONCE_SECONDS - 5)
    out = slow.post("/auth/apple/native", headers={"Origin": "http://localhost"},
                    json={"identityToken": sign(claims_for("002.native.3", BUNDLE,
                                                           slow_nonce))})
    check("a nonce older than ten minutes is refused", out.status_code == 400)
    huge = web.app.test_client().post(
        "/auth/apple/native", headers={"Origin": "http://localhost"},
        json={"identityToken": "x" * 100000})
    check("a body far bigger than any sign-in is turned away",
          huge.status_code == 413, huge.status_code)
    check("the app with no code still signs in, with nothing to revoke later",
          identity_row("002.native.2") is not None
          and identity_row("002.native.2")["refresh_token"] == "")

    section("THE APP'S BROWSER SHEET, WITH APPLE ON THE WEBSITE")

    verifier = "v" * 20 + "-" + "w" * 22
    challenge = A.challenge_of(verifier)
    back, sheet, _ = web_sign_in("003.sheet.1", "sheet@example.com",
                                 app_challenge=challenge)
    location = back.headers.get("Location", "")
    check("the sheet is sent back to the app with a code",
          location.startswith("app.orenth.ios://auth/finish?code="), location)
    check("and the sheet itself is not signed in",
          sheet.get("/account").status_code == 302)
    code = parse_qs(urlparse(location).query).get("code", [""])[0]
    webview = web.app.test_client()
    done = webview.post("/auth/app/finish", data={"code": code, "verifier": verifier},
                        headers={"Origin": "http://localhost"})
    check("the app's web view trades it for a session",
          done.status_code == 302 and done.headers["Location"].endswith("/trades"),
          (done.status_code, done.headers.get("Location")))
    check("and is signed in", "sheet@example.com"
          in webview.get("/account").data.decode("utf-8", "replace"))

    starter = web.app.test_client()
    start = starter.get("/auth/apple?app=1&challenge=" + challenge)
    flow = starter.get_cookie("apple_flow", path="/auth/apple")
    cancel = web.app.test_client()
    cancel.set_cookie("apple_flow", flow.decoded_value, path="/auth/apple")
    out = cancel.post("/auth/apple/callback", data={
        "error": "user_cancelled_authorize",
        "state": parse_qs(urlparse(start.headers["Location"]).query)["state"][0]})
    check("cancelling in the sheet tells the app, not an error page",
          out.headers.get("Location") ==
          "app.orenth.ios://auth/finish?error=cancelled", out.headers.get("Location"))

    section("THE SIGN-IN PAGE")

    web.app.config["AUTH_MODE"] = "google"
    page = web.app.test_client().get("/signin").data.decode("utf-8", "replace")
    check("the website shows Continue with Apple", "Continue with Apple" in page)
    check("as Apple's own button", 'class="abtn"' in page and "<svg" in page)
    check("with the app's script", "signin-app.js" in page)
    check("and says what Apple shares", "hide your email address" in page)
    apple_on(services=False)
    page = web.app.test_client().get("/signin").data.decode("utf-8", "replace")
    check("with only the app's half set, the website has no Apple button",
          "Continue with Apple" not in page)
    check("and /auth/apple does not exist",
          web.app.test_client().get("/auth/apple").status_code == 404)
    in_app = web.app.test_client().get("/signin", headers={
        "User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 OrenthApp/1"})
    page = in_app.data.decode("utf-8", "replace")
    check("but the app shows it, for its own Apple sheet",
          "Continue with Apple" in page and 'data-web="0"' in page
          and 'data-native="1"' in page)
    check("the operator is told the website half is missing",
          "APPLE_SERVICES_ID" in apple_mod.why_not())
    check("while a visitor is not", "APPLE_SERVICES_ID" not in page)
    privacy = web.app.test_client().get("/privacy").data.decode("utf-8", "replace")
    check("the privacy page says what Apple shares",
          "If you sign in with Apple" in privacy and "privaterelay.appleid.com" in privacy)
    apple_on()
    web.app.config["AUTH_MODE"] = None

    section("LEAVING: APPLE IS ASKED TO END IT")

    web._RATE.clear()
    back, leaver, _ = web_sign_in("004.leave.1", "leaver@example.com")
    before = len(FAKE.posts)
    gone = leaver.post("/account/delete", data={"confirm": "delete"})
    revokes = [p for p in FAKE.posts[before:] if p["url"] == apple_mod.REVOKE_URL]
    check("deleting the account asks Apple to revoke its token",
          len(revokes) == 1, len(revokes))
    if revokes:
        sent = revokes[0]["data"]
        check("the refresh token, for the client it came from",
              sent["token"] == "r.004.leave.1" and sent["client_id"] == SERVICES
              and sent["token_type_hint"] == "refresh_token", sent)
        check("with a client secret for that client",
              jwt_parts(sent["client_secret"])[1]["sub"] == SERVICES)
    check("and the account is gone", gone.status_code == 302
          and "leaver@example.com" not in people())
    check("with its link to Apple", identity_row("004.leave.1") is None)

    back, leaver, _ = web_sign_in("004.leave.2", "leaver2@example.com")
    FAKE.down = True
    try:
        gone = leaver.post("/account/delete", data={"confirm": "delete"})
    finally:
        FAKE.down = False
    check("with Apple out of reach the account is still deleted",
          gone.status_code == 302 and "leaver2@example.com" not in people())
    check("and nothing of it is kept to try again", identity_row("004.leave.2") is None)

    conn = db.connect(DB)
    try:
        doomed = A.upsert_apple_user(conn, {"sub": "a-doomed", "email": "d@example.com",
                                            "email_verified": True}, "", "r.d",
                                     SERVICES)
        A.new_handoff(conn, doomed.id, challenge)
        A.delete_user(conn, doomed.id)
        left = (conn.execute("SELECT COUNT(*) FROM identities WHERE user_id=?",
                             (doomed.id,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM app_handoffs WHERE user_id=?",
                             (doomed.id,)).fetchone()[0])
        check("delete_user removes its identities and app codes", left == (0, 0), left)
        older = A.upsert_apple_user(conn, {"sub": "a-older", "email": "o@example.com",
                                           "email_verified": True}, "", "r.o",
                                    SERVICES)
        A.new_handoff(conn, older.id, challenge)
        # What the version before this one does: it knows nothing of the new
        # tables, so the database has to clear them itself.
        conn.execute("DELETE FROM users WHERE id=?", (older.id,))
        conn.commit()
        left = (conn.execute("SELECT COUNT(*) FROM identities WHERE user_id=?",
                             (older.id,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM app_handoffs WHERE user_id=?",
                             (older.id,)).fetchone()[0])
        check("and the previous version's plain delete clears them too",
              left == (0, 0), left)
        A.ensure_schema(conn)
        A.ensure_schema(conn)
        check("the new tables are safe to create again", True)
    finally:
        conn.close()

    section("APPLE'S NOTIFICATIONS")

    def notify(kind, sub, aud=BUNDLE, as_text=True, key=APPLE_KEY, at=None):
        now = int(time.time())
        events = {"type": kind, "sub": sub,
                  "event_time": (at if at is not None else now) * 1000}
        claims = {"iss": apple_mod.ISSUER, "aud": aud, "iat": now, "jti": "j%d" % now,
                  "events": json.dumps(events) if as_text else events}
        return web.app.test_client().post(
            "/auth/apple/notifications", json={"payload": sign(claims, key=key)})

    # Dana: Google, with Apple linked. Signed in, so there is a session to end.
    conn = db.connect(DB)
    try:
        A.upsert_apple_user(conn, {"sub": "a-dana", "email": "dana@example.com",
                                   "email_verified": True})
        dana_session = A.start_session(conn, dana.id)
    finally:
        conn.close()
    out = notify("consent-revoked", "a-dana")
    check("a consent-revoked notification is accepted", out.status_code == 200,
          out.status_code)
    check("it unlinks Apple", identity_row("a-dana") is None)
    conn = db.connect(DB)
    try:
        check("and signs the account out",
              A.user_for_session(conn, dana_session) is None)
        check("but the account stays, Google still opens it",
              A.get_user(conn, dana.id) is not None)
    finally:
        conn.close()

    conn = db.connect(DB)
    try:
        A.upsert_apple_user(conn, {"sub": "a-dana", "email": "dana@example.com",
                                   "email_verified": True})
    finally:
        conn.close()
    out = notify("account-delete", "a-dana", as_text=False)
    check("a deleted Apple Account, events as an object, is accepted",
          out.status_code == 200)
    conn = db.connect(DB)
    try:
        check("Google's account keeps its journal: only Apple is unlinked",
              A.get_user(conn, dana.id) is not None and identity_row("a-dana") is None)
    finally:
        conn.close()

    back, hider, _ = web_sign_in("005.hidden.1", "q7@privaterelay.appleid.com",
                                 relay=True)
    check("someone who hid their address signs in",
          "q7@privaterelay.appleid.com" in people())
    out = notify("account-deleted", "005.hidden.1")
    check("Apple's own spelling, account-deleted, is accepted", out.status_code == 200)
    check("an account only that Apple Account could open is deleted with it",
          "q7@privaterelay.appleid.com" not in people())

    # A hidden address at icloud.com looks like a real one; the token said.
    web_sign_in("005.icloud.1", "q8@icloud.com", relay=True)
    notify("account-delete", "005.icloud.1")
    check("so is one whose icloud.com address Apple said was hidden",
          "q8@icloud.com" not in people())

    back, kept_client, _ = web_sign_in("005.real.1", "real@example.com")
    notify("account-delete", "005.real.1")
    check("one with a real address it can still be reached at is kept",
          "real@example.com" in people() and identity_row("005.real.1") is None)

    web_sign_in("005.late.1", "late@example.com")
    out = notify("consent-revoked", "005.late.1", at=int(time.time()) - 3600)
    check("a notice older than the latest sign-in changes nothing",
          out.status_code == 200 and identity_row("005.late.1") is not None)

    out = notify("email-disabled", "005.late.1")
    check("a forwarding change is acknowledged and changes nothing",
          out.status_code == 200 and identity_row("005.late.1") is not None)
    out = notify("consent-revoked", "005.late.1", key=STRANGER_KEY)
    check("a notification Apple did not sign is refused",
          out.status_code == 400 and identity_row("005.late.1") is not None)
    out = notify("consent-revoked", "005.late.1", aud="com.someone.else")
    check("one for another app is refused", out.status_code == 400)
    junk = web.app.test_client().post("/auth/apple/notifications", data="not json")
    check("a body that is not JSON is refused", junk.status_code == 400)
    huge = web.app.test_client().post("/auth/apple/notifications",
                                      json={"payload": "x" * 100000})
    check("and one far bigger than any notice is turned away",
          huge.status_code == 413, huge.status_code)
    other_kind = web.app.test_client().post(
        "/auth/apple/notifications",
        json={"payload": sign(claims_for("005.late.1", BUNDLE, apple_mod.new_nonce()))})
    check("an identity token posted as a notification is refused",
          other_kind.status_code == 400)

    def status_of(path, **kwargs):
        """A post's status, counting an error that escapes as the 500 it is
        on the site."""
        try:
            return web.app.test_client().post(path, **kwargs).status_code
        except RecursionError:
            return 500

    # Nested past what Python reads, and unsigned: anyone can send these.
    deep = "[" * 2500 + "]" * 2500
    nested_token = "%s.%s.%s" % (b64(deep.encode("ascii")), b64(b"{}"), b64(b"x"))
    check("a token whose header is nested thousands deep is refused, not a 500",
          status_of("/auth/apple/notifications",
                    json={"payload": nested_token}) == 400)
    deep_body = "[" * 20000 + "]" * 20000
    check("so is a notification body nested thousands deep",
          status_of("/auth/apple/notifications", data=deep_body,
                    content_type="application/json") == 400)
    check("and the same body posted to the app's Apple sign-in",
          status_of("/auth/apple/native", data=deep_body,
                    content_type="application/json",
                    headers={"Origin": "http://localhost"}) == 400)

finally:
    apple_mod.requests = _real_requests
    apple_off()
    web.app.config["AUTH_MODE"] = None

print()
print("%d failure(s)" % len(fails))
print("APPLE SIGN-IN OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
