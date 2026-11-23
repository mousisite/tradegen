"""Signing in with Apple.

Two ways in, one module. On the website the browser goes to Apple, and Apple
posts it back with a one-time code that this server trades for tokens, as it
does with Google. In the iPhone app the system's own Apple sheet signs the
person in, with Face ID, and the app sends the identity token here.

Unlike Google, the identity token is checked here, by hand, against Apple's
published keys. Apple has no userinfo endpoint to ask instead, and in the app
the token arrives from the phone rather than over this server's own
connection to Apple, so its signature is the only proof of who signed in.
That is the kind of code bot/auth.py is glad not to write, so it is kept as
narrow as it can be: one algorithm (RS256, so "none" and HS256 are refused
before any key is looked at), one issuer, keys fetched only from Apple's own
address and never from anything the token names, and every claim that
matters checked.

Settings, all from the environment:

  APPLE_TEAM_ID              the developer team, ten letters and digits
  APPLE_SIGNIN_KEY_ID        the id of the key below
  APPLE_SIGNIN_PRIVATE_KEY   the .p8 file's text; a pasted literal \\n is a newline
  APPLE_SERVICES_ID          the website's client id, a Services ID
  APPLE_BUNDLE_ID            the app's client id, app.orenth.ios unless set

The first three switch on Sign in with Apple in the app. With the fourth the
website gets its button too. With none of them nothing here is reachable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import quote, urlencode

import requests
from requests import RequestException

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from . import safejson

log = logging.getLogger(__name__)

ISSUER = "https://appleid.apple.com"
AUTHORIZE_URL = ISSUER + "/auth/authorize"
TOKEN_URL = ISSUER + "/auth/token"
REVOKE_URL = ISSUER + "/auth/revoke"
KEYS_URL = ISSUER + "/auth/keys"

DEFAULT_BUNDLE_ID = "app.orenth.ios"
# Hide My Email addresses. Real addresses that forward to the person's own
# inbox, so mail sent to one arrives; they are never joined to an account by
# address alone. Apple also gives out relay addresses at icloud.com, where
# real ones live too: only the token's is_private_email tells those apart.
RELAY_DOMAINS = ("privaterelay.appleid.com", "private.icloud.com")

TIMEOUT = 15
# Deleting an account waits on this, so it is shorter.
REVOKE_TIMEOUT = 10
# Apple accepts a client secret that lasts up to six months. An hour, made
# again as it runs out, limits what one copied out of a log would be worth.
SECRET_LIFETIME = 3600
# How far Apple's clock and this one may disagree.
SKEW = 120
# The oldest identity token accepted, by its issue time. Apple's own expiry is
# checked too; this only refuses one that claims to be far older.
TOKEN_MAX_AGE = 24 * 3600
# Apple may resend a notification it could not deliver, so these may be days
# old; older than this is a replay rather than a retry.
NOTICE_MAX_AGE = 7 * 24 * 3600
# Apple's keys are kept a day, and asked for again at most once a minute when
# a token names one not held, so made-up key ids cannot make this hammer Apple.
KEYS_TTL = 24 * 3600
KEYS_RETRY = 60
MAX_TOKEN = 8192
NAME_MAX = 60

_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class AppleError(RuntimeError):
    """Signing in with Apple did not work, phrased for the person who sees it."""


# --- settings -----------------------------------------------------------------

def _setting(name: str, default: str = "") -> str:
    """The first non-empty line of a setting, trimmed.

    A value pasted into a hosting dashboard easily carries a stray newline,
    and Apple answers that with "invalid_client" rather than naming it.
    """
    for line in (os.environ.get(name) or "").splitlines():
        if line.strip():
            return line.strip()
    return default


def team_id() -> str:
    return _setting("APPLE_TEAM_ID")


def key_id() -> str:
    return _setting("APPLE_SIGNIN_KEY_ID")


def services_id() -> str:
    """The website's client id."""
    return _setting("APPLE_SERVICES_ID")


def bundle_id() -> str:
    """The app's client id, which is what Apple puts in the app's tokens."""
    return _setting("APPLE_BUNDLE_ID", DEFAULT_BUNDLE_ID)


def _key_text() -> str:
    """The .p8 key as PEM text, however it was pasted.

    Hosting dashboards often keep a value on one line, so a literal \\n is
    read as a newline, and a key pasted without its BEGIN and END lines is
    given them back.
    """
    secret_text = (os.environ.get("APPLE_SIGNIN_PRIVATE_KEY") or "").strip()
    secret_text = secret_text.strip('"').strip("'").replace("\\n", "\n")
    secret_text = secret_text.replace("\r\n", "\n").strip()
    if secret_text and "-----BEGIN" not in secret_text:
        body = "".join(secret_text.split())
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        secret_text = ("-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----"
                       % "\n".join(lines))
    return secret_text


# The parsed key, kept against a hash of its text so that a changed setting is
# read again and an unreadable one is reported once rather than on every page.
_KEY: Dict[str, object] = {}
_KEY_LOCK = threading.Lock()


def _signing_key() -> Optional[ec.EllipticCurvePrivateKey]:
    """The .p8 key, or None when it is unset or cannot be read."""
    secret_pem = _key_text()
    if not secret_pem:
        return None
    digest = hashlib.sha256(secret_pem.encode("utf-8")).hexdigest()
    with _KEY_LOCK:
        if digest in _KEY:
            return _KEY[digest]          # type: ignore[return-value]
        try:
            key = serialization.load_pem_private_key(secret_pem.encode("ascii"),
                                                     password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            key = None
        if not isinstance(key, ec.EllipticCurvePrivateKey) \
                or key.curve.name != "secp256r1":
            log.warning("APPLE_SIGNIN_PRIVATE_KEY is not a readable Apple .p8 "
                        "key, so Sign in with Apple is off.")
            key = None
        _KEY.clear()
        _KEY[digest] = key
        return key


def native_ready() -> bool:
    """Whether Sign in with Apple can work in the app."""
    return bool(team_id() and key_id() and _signing_key() is not None)


def web_ready() -> bool:
    """Whether the website can offer Sign in with Apple as well."""
    return bool(services_id()) and native_ready()


def why_not() -> str:
    """What is missing, for the operator's start-up log. Empty when all is
    set, and when none of it is: then Apple sign-in is simply not wanted."""
    names = ("APPLE_TEAM_ID", "APPLE_SIGNIN_KEY_ID", "APPLE_SIGNIN_PRIVATE_KEY",
             "APPLE_SERVICES_ID")
    if not any((os.environ.get(name) or "").strip() for name in names):
        return ""
    missing = [name for name, value in (("APPLE_TEAM_ID", team_id()),
                                        ("APPLE_SIGNIN_KEY_ID", key_id()),
                                        ("APPLE_SIGNIN_PRIVATE_KEY", _key_text()))
               if not value]
    if missing:
        return ("Sign in with Apple is off: %s %s not set."
                % (" and ".join(missing), "is" if len(missing) == 1 else "are"))
    if _signing_key() is None:
        return ("Sign in with Apple is off: APPLE_SIGNIN_PRIVATE_KEY is not a "
                "readable .p8 key.")
    if not services_id():
        return ("Sign in with Apple works in the app only: without "
                "APPLE_SERVICES_ID the website has no Apple button.")
    return ""


# --- small pieces -------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(part: str) -> bytes:
    """Strict base64url: the standard decoder skips characters it does not
    know, and nothing that loose belongs in a signature check."""
    if not isinstance(part, str) or not _B64URL.match(part):
        raise AppleError("That sign-in token is not one Apple made.")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (ValueError, TypeError) as exc:
        raise AppleError("That sign-in token is not one Apple made.") from exc


def _json_part(part: str) -> Dict:
    # Read before any signature is checked, so anyone's: JSON nested too deep
    # for Python to read is refused like any other junk (bot/safejson.py).
    try:
        value = safejson.loads(_unb64(part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppleError("That sign-in token is not one Apple made.") from exc
    if not isinstance(value, dict):
        raise AppleError("That sign-in token is not one Apple made.")
    return value


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _truthy(value) -> bool:
    """Apple says true as a JSON true in some tokens and as "true" in others."""
    return value is True or (isinstance(value, str)
                             and value.strip().lower() == "true")


def is_relay(email: str) -> bool:
    """Whether an address is plainly one of Apple's Hide My Email addresses."""
    address = (email or "").strip().lower()
    return any(address.endswith("@" + domain) for domain in RELAY_DOMAINS)


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def nonce_hash(nonce: str) -> str:
    """What is kept of a nonce: its SHA-256, in lowercase hex."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def nonce_matches(claim, issued_hash: str) -> bool:
    """Whether a token's nonce is the one this server handed out.

    The nonce goes to Apple either as it was issued (the website does that,
    and Apple's plugin for the app passes it on unchanged) or already hashed,
    as Apple's own sample code does. Both are the same nonce.
    """
    if not isinstance(claim, str) or not claim or not issued_hash:
        return False
    expected = issued_hash.encode("ascii", "replace")
    as_issued = hmac.compare_digest(nonce_hash(claim).encode("ascii"), expected)
    as_hashed = hmac.compare_digest(claim.lower().encode("utf-8"), expected)
    return as_issued or as_hashed


def seal_flow(flow: Dict, key: bytes) -> str:
    """A website sign-in's state as a cookie value only this server can make.

    HMAC-SHA256 over the JSON, with the time it was made inside, so a value
    cannot be altered, forged or kept past its time. Not secret: there is
    nothing in it a person could use.
    """
    body = _b64(json.dumps(dict(flow, t=int(time.time())),
                           separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(key, b"apple-flow." + body.encode("ascii"), hashlib.sha256)
    return "%s.%s" % (body, _b64(mac.digest()))


def open_flow(value, key: bytes, max_age: int) -> Dict:
    """The state a sealed cookie value holds: {} when it is not one this
    server made, was changed, or is older than max_age seconds."""
    if not isinstance(value, str) or value.count(".") != 1 or len(value) > 4096:
        return {}
    body, mac = value.split(".")
    if not _B64URL.match(body) or not _B64URL.match(mac):
        return {}
    want = hmac.new(key, b"apple-flow." + body.encode("ascii"), hashlib.sha256)
    try:
        if not hmac.compare_digest(want.digest(), _unb64(mac)):
            return {}
        flow = safejson.loads(_unb64(body).decode("utf-8"))
    except (AppleError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(flow, dict) or not _number(flow.get("t")):
        return {}
    if not 0 <= time.time() - flow["t"] <= max_age:
        return {}
    return flow


# How long a nonce handed to the app stays good.
NONCE_SECONDS = 10 * 60
# Nonces already used to sign in, by hash, for as long as they could still be
# good. The session holding a nonce lives in a cookie, which can be sent
# twice; this is what makes each nonce good for one sign-in.
_SPENT: Dict[str, float] = {}
_SPENT_LOCK = threading.Lock()
SPENT_KEEP = NONCE_SECONDS + SKEW


def spend_nonce(issued_hash: str) -> bool:
    """Mark a nonce used. False if it already was."""
    now = time.time()
    with _SPENT_LOCK:
        if len(_SPENT) > 1000:
            for key in [k for k, at in _SPENT.items() if now - at > SPENT_KEEP]:
                del _SPENT[key]
        if issued_hash in _SPENT:
            return False
        _SPENT[issued_hash] = now
        return True


def _clean_name(part) -> str:
    if not isinstance(part, str):
        return ""
    text = "".join(ch for ch in part if ch.isprintable())
    return " ".join(text.split())[:NAME_MAX]


def name_from(given, family) -> str:
    """A person's name from its two halves, as plain text of a sane length."""
    return " ".join(p for p in (_clean_name(given), _clean_name(family)) if p)


def name_from_post(raw_user: str) -> str:
    """The name in Apple's first post back to the website.

    Apple sends it once, the first time someone signs in here, and unsigned:
    it is only ever used as the account's display name, never to decide who
    someone is.
    """
    if not raw_user or len(raw_user) > 2000:
        return ""
    try:
        data = safejson.loads(raw_user)
    except ValueError:
        return ""
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, dict):
        return ""
    return name_from(name.get("firstName"), name.get("lastName"))


# --- the client secret --------------------------------------------------------

_SECRETS: Dict[Tuple[str, ...], Tuple[str, int]] = {}
_SECRETS_LOCK = threading.Lock()


def client_secret(client_id: str) -> str:
    """The signed statement Apple takes in place of a client password.

    A JWT signed with the .p8 key (ES256): this team, this key, for this
    client id. It is kept and reused until a few minutes before it runs out.
    """
    key = _signing_key()
    if key is None or not team_id() or not key_id():
        raise AppleError("Sign in with Apple is not set up on this site.")
    now = int(time.time())
    held_key = (team_id(), key_id(), client_id,
                hashlib.sha256(_key_text().encode("utf-8")).hexdigest())
    # Locals that hold a secret are named so, because error reports strip
    # values by name (bot/monitoring.py).
    with _SECRETS_LOCK:
        held_secret = _SECRETS.get(held_key)
        if held_secret and held_secret[1] - 300 > now:
            return held_secret[0]
    header = {"alg": "ES256", "kid": key_id()}
    claims = {"iss": team_id(), "iat": now, "exp": now + SECRET_LIFETIME,
              "aud": ISSUER, "sub": client_id}
    signing_input = "%s.%s" % (
        _b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8")))
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    # A JWT carries the two numbers side by side, 32 bytes each, not the DER
    # structure the library returns.
    r, s = decode_dss_signature(der)
    secret_jwt = "%s.%s" % (signing_input,
                            _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big")))
    with _SECRETS_LOCK:
        _SECRETS[held_key] = (secret_jwt, now + SECRET_LIFETIME)
    return secret_jwt


# --- Apple's public keys ------------------------------------------------------

_KEYS: Dict[str, object] = {"keys": {}, "fetched": 0.0, "tried": 0.0}
_KEYS_LOCK = threading.Lock()


def _fetch_keys() -> Dict[str, rsa.RSAPublicKey]:
    """Apple's current signing keys, by key id. Only RSA keys for RS256."""
    try:
        response = requests.get(KEYS_URL, timeout=TIMEOUT,
                                headers={"Accept": "application/json"})
    except RequestException as exc:
        raise AppleError("Could not reach Apple to check the sign-in. Try "
                         "again in a minute.") from exc
    if response.status_code != 200:
        raise AppleError("Apple did not answer just now. Try again in a minute.")
    try:
        body = response.json()
    except ValueError as exc:
        raise AppleError("Apple did not answer just now. Try again in a minute.") \
            from exc
    keys = {}
    for jwk in (body.get("keys") or []) if isinstance(body, dict) else []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
            continue
        if jwk.get("alg", "RS256") != "RS256" or jwk.get("use", "sig") != "sig":
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            n = int.from_bytes(_unb64(jwk["n"]), "big")
            e = int.from_bytes(_unb64(jwk["e"]), "big")
            public = rsa.RSAPublicNumbers(e, n).public_key()
        except (KeyError, TypeError, ValueError, AppleError):
            continue
        if public.key_size >= 2048:
            keys[kid] = public
    return keys


def _apple_key(kid: str) -> Optional[rsa.RSAPublicKey]:
    """The key Apple signed with, fetching Apple's keys when it is not held."""
    with _KEYS_LOCK:
        now = time.time()
        held = _KEYS["keys"]
        fresh = now - _KEYS["fetched"] < KEYS_TTL
        if kid in held and fresh:
            return held[kid]
        if now - _KEYS["tried"] >= KEYS_RETRY:
            _KEYS["tried"] = now
            try:
                fetched = _fetch_keys()
            except AppleError:
                # Keys Apple has stopped listing still verify what they
                # signed, so a stale copy beats no answer.
                if kid not in held:
                    raise
                fetched = {}
            if fetched:
                _KEYS["keys"], _KEYS["fetched"] = fetched, now
        return _KEYS["keys"].get(kid)


# --- checking what Apple signed -----------------------------------------------

NOT_APPLES = "That sign-in could not be confirmed with Apple. Try again."


def _verified_claims(token, audiences: Iterable[str], max_age: int,
                     need_exp: bool = True) -> Dict:
    """The claims of a JWT Apple signed for one of these client ids.

    Raises AppleError for anything else: another algorithm, a key Apple does
    not list, a bad signature, another issuer or audience, or a token that is
    expired or dated in the future.
    """
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
        raise AppleError(NOT_APPLES)
    token_parts = token.split(".")
    if len(token_parts) != 3 or not all(_B64URL.match(p) for p in token_parts):
        raise AppleError(NOT_APPLES)
    header = _json_part(token_parts[0])
    # Only RS256 and only Apple's keys. "none", and HS256 keyed with Apple's
    # public key, are the classic forgeries; both stop here.
    if header.get("alg") != "RS256" or "crit" in header:
        raise AppleError(NOT_APPLES)
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 100:
        raise AppleError(NOT_APPLES)
    public = _apple_key(kid)
    if public is None:
        raise AppleError(NOT_APPLES)
    try:
        public.verify(_unb64(token_parts[2]),
                      ("%s.%s" % (token_parts[0], token_parts[1])).encode("ascii"),
                      padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise AppleError(NOT_APPLES) from exc
    claims = _json_part(token_parts[1])
    if claims.get("iss") != ISSUER:
        raise AppleError(NOT_APPLES)
    allowed = {a for a in audiences if a}
    if not isinstance(claims.get("aud"), str) or claims["aud"] not in allowed:
        raise AppleError(NOT_APPLES)
    now = time.time()
    exp, iat = claims.get("exp"), claims.get("iat")
    if need_exp and (not _number(exp) or exp <= now - SKEW):
        raise AppleError("That Apple sign-in took too long. Try again.")
    if not _number(iat) or iat > now + SKEW or iat < now - max_age:
        raise AppleError(NOT_APPLES)
    return claims


def verify_identity(identity_token, audience: str, issued_hash: str = "",
                    check_nonce: bool = True) -> Dict:
    """Who an Apple identity token says signed in, once it checks out.

    Returns sub (Apple's id for this person, which never changes for this
    site), email, email_verified and is_private_email. The nonce is checked
    against the hash this server kept when it issued it; only a second token
    for a sign-in already proved by its nonce skips that.
    """
    claims = _verified_claims(identity_token, (audience,), TOKEN_MAX_AGE)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise AppleError(NOT_APPLES)
    if check_nonce and not nonce_matches(claims.get("nonce"), issued_hash):
        raise AppleError("That sign-in did not match this page. Start again.")
    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
    email = email.strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        email = ""
    return {"sub": sub, "email": email,
            "email_verified": _truthy(claims.get("email_verified")),
            "is_private_email": _truthy(claims.get("is_private_email"))
            or is_relay(email)}


def read_notification(payload) -> Dict:
    """A server-to-server notification from Apple, once its signature checks out.

    Returns type, sub and at (when it happened, in seconds). Apple's documents
    show the events claim as an object and the ones it sends hold a JSON
    string; both are read, and event_time in milliseconds is converted.
    """
    claims = _verified_claims(payload, (services_id(), bundle_id()),
                              NOTICE_MAX_AGE, need_exp=False)
    events = claims.get("events")
    if isinstance(events, str):
        try:
            events = safejson.loads(events)
        except ValueError as exc:
            raise AppleError(NOT_APPLES) from exc
    if not isinstance(events, dict):
        raise AppleError(NOT_APPLES)
    kind, sub = events.get("type"), events.get("sub")
    if not isinstance(kind, str) or not isinstance(sub, str) or not sub:
        raise AppleError(NOT_APPLES)
    at = events.get("event_time")
    if not _number(at):
        at = claims["iat"]
    if at > 1e11:
        at = at / 1000.0
    return {"type": kind, "sub": sub, "at": int(at)}


# --- talking to Apple ---------------------------------------------------------

def authorization_url(redirect_uri: str, state: str, nonce: str) -> str:
    """Where to send the browser to sign in on the website.

    Asking for the name and email address obliges Apple to post the answer
    back (form_post), from appleid.apple.com, which is why the callback
    cannot rely on the session cookie.
    """
    params = {"client_id": services_id(), "redirect_uri": redirect_uri,
              "response_type": "code", "scope": "name email",
              "response_mode": "form_post", "state": state, "nonce": nonce}
    return AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote)


def _error_of(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else ""
    return error if isinstance(error, str) else ""


def redeem(code, client_id: str, redirect_uri: str = "") -> Dict:
    """Trade a one-time authorization code for Apple's tokens.

    The website's codes belong to the Services ID and name the address Apple
    sent the browser back to; the app's belong to the bundle id and name none.
    The answer holds an id_token, still to be checked, and the refresh token
    that lets this account's Apple sign-in be ended when it is deleted.
    """
    if not isinstance(code, str) or not code or len(code) > 1024:
        raise AppleError("Apple returned no sign-in code.")
    secret_form = {"client_id": client_id, "client_secret": client_secret(client_id),
                   "code": code, "grant_type": "authorization_code"}
    if redirect_uri:
        secret_form["redirect_uri"] = redirect_uri
    try:
        response = requests.post(TOKEN_URL, data=secret_form, timeout=TIMEOUT,
                                 headers={"Accept": "application/json"})
    except RequestException as exc:
        raise AppleError("Could not reach Apple to finish signing in. Try again "
                         "in a minute.") from exc
    if response.status_code != 200:
        error = _error_of(response)
        if error == "invalid_client":
            log.error("Apple refused this site's Sign in with Apple settings "
                      "(invalid_client) for client id %s", client_id)
            raise AppleError("Sign in with Apple is not working on this site "
                             "just now. Try another way in.")
        if error == "invalid_grant":
            raise AppleError("That Apple sign-in had run out or was already "
                             "used. Try again.")
        log.warning("Apple's token endpoint answered %d (%s)",
                    response.status_code, error or "no reason given")
        raise AppleError("Apple did not finish the sign-in. Try again.")
    try:
        token_body = response.json()
    except ValueError as exc:
        raise AppleError("Apple did not finish the sign-in. Try again.") from exc
    if not isinstance(token_body, dict):
        raise AppleError("Apple did not finish the sign-in. Try again.")
    return token_body


def revoke(refresh_token: str, client_id: str) -> bool:
    """Ask Apple to end this site's access for one person. True when it did.

    Never raises: it runs while an account is being deleted, which goes ahead
    whatever Apple says. What went wrong is logged, without the token.
    """
    try:
        secret_form = {"client_id": client_id,
                       "client_secret": client_secret(client_id),
                       "token": refresh_token, "token_type_hint": "refresh_token"}
    except AppleError:
        log.warning("Could not ask Apple to end a sign-in: Sign in with Apple "
                    "is not set up any more.")
        return False
    try:
        response = requests.post(REVOKE_URL, data=secret_form,
                                 timeout=REVOKE_TIMEOUT,
                                 headers={"Accept": "application/json"})
    except Exception as exc:
        # Anything at all: the deletion waiting on this must go ahead.
        log.warning("Could not reach Apple to end a sign-in: %s",
                    type(exc).__name__)
        return False
    if response.status_code != 200:
        log.warning("Apple answered %d (%s) when asked to end a sign-in",
                    response.status_code, _error_of(response) or "no reason given")
        return False
    return True
