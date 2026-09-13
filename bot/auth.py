"""Signing in with Google.

The authorization code flow, with PKCE, exchanged server side. No JWT is
verified locally and none needs to be: the profile is read from Google's
userinfo endpoint over TLS using the access token, so the trust comes from the
connection to Google rather than from cryptography written here. Verifying an
id_token by hand, with the key rotation and the algorithm confusion that
invites, is exactly the kind of security code that should not be hand-rolled
when a plain HTTPS request answers the same question.

Credentials come from the environment, never from config.json, because
config.json is a file people edit, copy and paste into support requests.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = "openid email profile"
TIMEOUT = 15


class AuthError(RuntimeError):
    """Something went wrong signing in, phrased for the person who sees it."""


def client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def allowed_domains() -> Tuple[str, ...]:
    """Restrict sign-in to one or more email domains, if asked to.

    Set GOOGLE_ALLOWED_DOMAINS to a comma-separated list. Empty means anyone
    with a Google account may sign in, which is what a public product wants and
    emphatically not what an internal one does.
    """
    raw = (os.environ.get("GOOGLE_ALLOWED_DOMAINS") or "").strip()
    return tuple(d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip())


def configured() -> bool:
    """Whether Google sign-in can work at all."""
    return bool(client_id() and client_secret())


def why_not() -> str:
    """A plain sentence about what is missing, for the settings page."""
    if client_id() and client_secret():
        return ""
    missing = []
    if not client_id():
        missing.append("GOOGLE_CLIENT_ID")
    if not client_secret():
        missing.append("GOOGLE_CLIENT_SECRET")
    return ("Google sign-in is not set up: %s is not set in the environment or "
            ".env file." % " and ".join(missing))


# --- PKCE -------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_verifier() -> str:
    """A PKCE code verifier.

    Not strictly required for a confidential client that keeps a secret, but it
    costs one hash and closes an authorization-code interception hole, so there
    is no reason to leave it out.
    """
    return _b64(secrets.token_bytes(48))


def _challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """An unguessable value tying the callback to the request that started it."""
    return secrets.token_urlsafe(24)


# --- the flow ---------------------------------------------------------------

def authorization_url(redirect_uri: str, state: str, verifier: str,
                      login_hint: str = "") -> str:
    """Where to send the browser to sign in."""
    if not configured():
        raise AuthError(why_not())
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "online",
        # Ask every time rather than silently reusing a session, so switching
        # accounts on a shared machine is possible.
        "prompt": "select_account",
    }
    domains = allowed_domains()
    if len(domains) == 1:
        params["hd"] = domains[0]
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urlencode(params)


def exchange_code(code: str, redirect_uri: str, verifier: str) -> Dict:
    """Turn the one-time code into tokens, server to server."""
    if not configured():
        raise AuthError(why_not())
    try:
        response = requests.post(TOKEN_URL, timeout=TIMEOUT, data={
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        })
    except requests.RequestException as exc:
        raise AuthError("Could not reach Google to complete sign-in: %s" % exc)

    if response.status_code != 200:
        # Google's error body names the cause, and the two common ones are
        # worth translating because both are configuration, not user error.
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            detail = response.text[:200]
        if "redirect_uri_mismatch" in detail:
            raise AuthError(
                "Google rejected the redirect address. The exact address this "
                "app is using is %s, and that string has to appear in the "
                "OAuth client's authorised redirect URIs." % redirect_uri)
        if "invalid_client" in detail:
            raise AuthError("Google did not recognise the client id and secret.")
        raise AuthError("Google refused the sign-in: %s" % (detail or
                                                            response.status_code))
    return response.json()


def fetch_profile(access_token: str) -> Dict:
    """Who signed in, straight from Google."""
    try:
        response = requests.get(
            USERINFO_URL, timeout=TIMEOUT,
            headers={"Authorization": "Bearer %s" % access_token})
    except requests.RequestException as exc:
        raise AuthError("Could not read the Google profile: %s" % exc)
    if response.status_code != 200:
        raise AuthError("Google would not return the profile (%d)."
                        % response.status_code)
    profile = response.json()
    if not profile.get("email"):
        raise AuthError("That Google account has no email address attached.")
    return profile


def check_domain(email: str) -> None:
    """Refuse an address outside the permitted domains, if any are set."""
    domains = allowed_domains()
    if not domains:
        return
    domain = (email or "").rsplit("@", 1)[-1].lower()
    if domain not in domains:
        raise AuthError(
            "%s is not allowed to sign in here. This installation only accepts "
            "%s addresses." % (email, " or ".join("@" + d for d in domains)))


def sign_in(code: str, redirect_uri: str, verifier: str) -> Dict:
    """The whole exchange: code in, verified profile out."""
    tokens = exchange_code(code, redirect_uri, verifier)
    access = tokens.get("access_token")
    if not access:
        raise AuthError("Google returned no access token.")
    profile = fetch_profile(access)
    check_domain(profile.get("email", ""))
    return profile


# --- the cookie signing key -------------------------------------------------

def session_secret(path: Optional[str] = None) -> bytes:
    """A stable key for signing cookies, generated once and kept.

    Regenerating this on every start would sign everyone out on every restart.
    Hardcoding it would mean every copy of this app shares a key, so anyone
    holding the source could forge a session. So: read it from the environment
    if set, otherwise generate one and keep it in a file next to the database.
    """
    from_env = (os.environ.get("STOCKBOT_SECRET_KEY") or "").strip()
    if from_env:
        return from_env.encode("utf-8")

    if path is None:
        from . import database as db_mod
        path = os.path.join(os.path.dirname(db_mod.default_path()), "secret.key")

    try:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                existing = fh.read()
            # Deliberately not stripped. These are random bytes, not text, and
            # about one key in twenty begins or ends with a byte that happens
            # to be whitespace; stripping those would silently return a
            # different key from the one that was written, invalidating every
            # session issued since the file was created.
            if len(existing) >= 32:
                return existing
    except OSError:
        pass

    generated = secrets.token_bytes(48)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written before the mode is tightened; on Windows the mode is mostly
        # advisory, but on a server it matters.
        with open(path, "wb") as fh:
            fh.write(generated)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # An unwritable directory should not stop the app; sessions simply end
        # when the process does.
        pass
    return generated
