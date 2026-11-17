"""Notifications to phones and browsers when an alert fires.

An alert that shows only on an open page, or in an inbox, is easy to miss.
This sends a short notice to each device a person has switched on, through
the push service that device already listens to:

  Web Push  Chrome, Edge, Firefox and Safari, on computers and phones (on an
            iPhone, once Orenth is on the Home Screen). Every request is
            signed with the server's VAPID key (RFC 8292), and the notice is
            encrypted for that one browser (RFC 8291, the aes128gcm coding of
            RFC 8188), so the push service in the middle carries it without
            being able to read it. It needs no setup: the key pair is made once
            and kept next to the database, like the cookie key, unless
            VAPID_PRIVATE_KEY supplies one.
  APNs      The Orenth iPhone app, through Apple's push service over HTTP/2,
            with token auth. Off unless APPLE_TEAM_ID, APNS_KEY_ID and
            APNS_PRIVATE_KEY are all set. Apple can read these, as it can any
            app's notifications, and the privacy page says so.

Every cryptographic step is the cryptography package's. What is written here
is only the RFCs' arrangement of those parts, and tests/test_push.py checks it
against RFC 8291's own worked example.

Nothing here starts on its own. web.py hands each round of fired alerts to
deliver_later(), which sends them from one background thread, so neither the
alert loop nor a page ever waits on a push service. What became of each
notice is kept on the device's row: when one last went through, and the last
error, in words the person can read; the service's code goes to the log. A
device its push service calls gone is forgotten, and so is one that keeps
failing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import hashes, hmac, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _NO_CRYPTO = ""
except ImportError:             # an install made before it was a requirement
    _NO_CRYPTO = ("the cryptography package is not installed; run setup again "
                  "or pip install -r requirements.txt")

log = logging.getLogger(__name__)

# The push services browsers use. A subscription names the address the server
# will post to, and a page can hand over any address it likes, so only these
# are accepted: anything else would let a visitor point the server at itself,
# or at a machine on its private network. Chrome, Samsung Internet and Opera
# use Google's; Firefox, Mozilla's; Safari, Apple's; Edge, Microsoft's.
PUSH_SERVICES = ("fcm.googleapis.com", "android.googleapis.com",
                 "push.services.mozilla.com", "push.apple.com",
                 "notify.windows.com")

MAX_DEVICES = 10            # per person; the one quiet longest makes way
MAX_FAILURES = 8            # notices failed in a row before a device is forgotten
ALERT_TTL = 6 * 3600        # how long a push service keeps trying a phone that is off
TEST_TTL = 10 * 60
VAPID_LIFETIME = 12 * 3600  # RFC 8292 allows a token up to a day
APNS_REUSE = 40 * 60        # Apple wants its token renewed within the hour,
APNS_FLOOR = 20 * 60        # and refuses one renewed more often than every 20 minutes
APNS_PAUSE = 60             # seconds held back after Apple asks to slow down
TIMEOUT = 10.0
CONNECT_TIMEOUT = 5.0
BODY_BYTES = 1500           # a notice's text; both services take about 4 KB in all
SHOWN = 4                   # alerts listed in one notice before "and N more"
RECORD_SIZE = 4096

APNS_HOSTS = {"production": "https://api.push.apple.com",
              "sandbox": "https://api.sandbox.push.apple.com"}
DEFAULT_BUNDLE = "app.orenth.ios"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    address      TEXT NOT NULL,
    p256dh       TEXT NOT NULL DEFAULT '',
    auth         TEXT NOT NULL DEFAULT '',
    environment  TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    session_ref  TEXT NOT NULL DEFAULT '',
    created_ts   INTEGER NOT NULL,
    last_ok_ts   INTEGER,
    last_error   TEXT,
    failures     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_devices_address ON push_devices(kind, address);
CREATE INDEX IF NOT EXISTS idx_push_devices_user ON push_devices(user_id);
"""
# kind is 'web' or 'apns'. address is the push service's URL for a browser,
# or the iPhone's device token in hex. p256dh and auth are the browser's
# public key and secret, which the notice is encrypted for. environment is
# where Apple knows an iPhone token: production for TestFlight and the App
# Store, sandbox for a build run from Xcode. session_ref is a hash of the
# sign-in that switched it on, so signing out forgets it. ON DELETE CASCADE
# lets a version of the app that knows nothing of this table still delete
# an account that has a row in it.


class PushError(ValueError):
    """Something wrong with what a page sent, phrased for the person."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# --- base64url ----------------------------------------------------------------

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    """Base64url with or without padding, or plain base64. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("not text")
    text = re.sub(r"\s+", "", text).rstrip("=").replace("+", "-").replace("/", "_")
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- what a page may hand over --------------------------------------------------

def push_service(endpoint: str) -> bool:
    """Whether an address is https on a push service that browsers use."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except (TypeError, ValueError):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or port not in (None, 443) \
            or parts.username is not None or parts.password is not None:
        return False
    return any(host == known or host.endswith("." + known) for known in PUSH_SERVICES)


def clean_subscription(raw) -> Tuple[str, str, str]:
    """The endpoint and keys of a browser's PushSubscription.toJSON(), checked.

    Returns (endpoint, p256dh, auth) with the keys in base64url. The key must
    be a real P-256 point and the secret 16 bytes, or nothing can be
    encrypted for it. Raises PushError.
    """
    if not isinstance(raw, dict):
        raise PushError("That is not a browser's subscription.")
    endpoint = raw.get("endpoint")
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2000 \
            or re.search(r"[\s\\]", endpoint):
        raise PushError("That subscription has no usable address.")
    if not push_service(endpoint):
        raise PushError("That browser's push service is not one Orenth sends to.")
    if _NO_CRYPTO:
        raise PushError("Notifications are not available on this server.")
    try:
        public = unb64url(keys.get("p256dh") or "")
        secret = unb64url(keys.get("auth") or "")
        if len(public) != 65 or public[0] != 4 or len(secret) != 16:
            raise ValueError("wrong sizes")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except (TypeError, ValueError):
        raise PushError("That subscription's keys cannot be used.")
    return endpoint, b64url(public), b64url(secret)


def clean_token(raw) -> str:
    """An iPhone's APNs device token, as lower-case hex. Raises PushError.

    32 bytes today; Apple says the length may change, so up to 100 is taken.
    """
    token = raw.strip().lower() if isinstance(raw, str) else ""
    if not re.fullmatch(r"(?:[0-9a-f]{2}){32,100}", token):
        raise PushError("That is not an iPhone's device token.")
    return token


def address_of(data) -> Tuple[str, str]:
    """(kind, address) named in a request, tidied but not checked, or ('', '')."""
    if not isinstance(data, dict):
        return "", ""
    kind = data.get("kind")
    if kind == "web" and isinstance(data.get("endpoint"), str):
        return "web", data["endpoint"].strip()[:2000]
    if kind == "apns" and isinstance(data.get("token"), str):
        return "apns", data["token"].strip().lower()[:200]
    return "", ""


def device_label(user_agent: str) -> str:
    """A short name for a device, from its browser's description of itself."""
    ua = user_agent or ""
    if "OrenthApp/" in ua:
        return "iPhone app"
    system = next((name for marker, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
        ("CrOS", "Chromebook"), ("Macintosh", "Mac"), ("Windows", "Windows"),
        ("Linux", "Linux")) if marker in ua), "")
    browser = next((name for marker, name in (
        ("Edg", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
        ("Firefox/", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
        ("Chrome/", "Chrome"), ("Safari/", "Safari")) if marker in ua), "")
    if browser and system:
        return "%s on %s" % (browser, system)
    return browser or system or "A browser"


def clean_label(raw, user_agent: str = "") -> str:
    """The name a page gave its device, tidied, or one made from its browser."""
    text = raw if isinstance(raw, str) else ""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", text)).strip()[:40]
    return text or device_label(user_agent)


# --- the devices ------------------------------------------------------------------

def register(conn: sqlite3.Connection, user_id: int, kind: str, address: str,
             p256dh: str = "", auth: str = "", environment: str = "",
             label: str = "", session_ref: str = "") -> int:
    """Keep a device for this person, taking it from anyone who had it before.

    A device belongs to one account. Switching notifications on where someone
    else had them moves the device, so a shared phone never shows the
    previous owner's alerts. Past MAX_DEVICES, the device quiet the longest
    makes way. Returns the device's id.
    """
    ensure_schema(conn)
    now = int(time.time())
    # On a conflict, every column on the right still holds the row's old
    # value, so the CASEs can tell a device staying put from one moving.
    conn.execute(
        "INSERT INTO push_devices (user_id, kind, address, p256dh, auth, "
        "environment, label, session_ref, created_ts) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(kind, address) DO UPDATE SET "
        "created_ts=CASE WHEN user_id=excluded.user_id THEN created_ts "
        "ELSE excluded.created_ts END, "
        "last_ok_ts=CASE WHEN user_id=excluded.user_id THEN last_ok_ts ELSE NULL END, "
        "user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
        "environment=CASE WHEN excluded.environment<>'' THEN excluded.environment "
        "ELSE environment END, label=excluded.label, "
        "session_ref=excluded.session_ref, last_error=NULL, failures=0",
        (int(user_id), kind, address, p256dh, auth, environment, label[:40],
         session_ref, now))
    device = int(conn.execute("SELECT id FROM push_devices WHERE kind=? AND "
                              "address=?", (kind, address)).fetchone()[0])
    others = conn.execute(
        "SELECT id FROM push_devices WHERE user_id=? AND id<>? "
        "ORDER BY COALESCE(last_ok_ts, created_ts) DESC, id DESC",
        (int(user_id), device)).fetchall()
    for extra in others[MAX_DEVICES - 1:]:
        conn.execute("DELETE FROM push_devices WHERE id=?", (extra[0],))
    conn.commit()
    return device


def devices(conn: sqlite3.Connection, user_id: int) -> List[sqlite3.Row]:
    """One person's devices, oldest first."""
    ensure_schema(conn)
    return list(conn.execute("SELECT * FROM push_devices WHERE user_id=? "
                             "ORDER BY created_ts, id", (int(user_id),)))


def owns(conn: sqlite3.Connection, user_id: int, kind: str, address: str) -> bool:
    """Whether this device is one of this person's."""
    ensure_schema(conn)
    return conn.execute("SELECT 1 FROM push_devices WHERE user_id=? AND kind=? "
                        "AND address=?", (int(user_id), kind, address)).fetchone() is not None


def forget(conn: sqlite3.Connection, user_id: int, kind: str, address: str) -> int:
    """Stop sending to one of this person's devices. Nobody else's is touched."""
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM push_devices WHERE user_id=? AND kind=? "
                       "AND address=?", (int(user_id), kind, address))
    conn.commit()
    return cur.rowcount


def release(conn: sqlite3.Connection, kind: str, address: str) -> int:
    """Stop sending to a device, whoever had it, for the device itself.

    The browser or phone that asks is the only thing that knows its own
    address, so asking proves it is that device. It is how a shared phone
    stops showing the last person's alerts once someone else signs in.
    """
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM push_devices WHERE kind=? AND address=?",
                       (kind, address))
    conn.commit()
    return cur.rowcount


def forget_ids(conn: sqlite3.Connection, user_id: int, ids: Sequence[int]) -> int:
    ensure_schema(conn)
    ids = [int(i) for i in ids][:50]
    if not ids:
        return 0
    cur = conn.execute("DELETE FROM push_devices WHERE user_id=? AND id IN (%s)"
                       % ",".join("?" * len(ids)), [int(user_id)] + ids)
    conn.commit()
    return cur.rowcount


def forget_session(conn: sqlite3.Connection, session_ref: str) -> int:
    """Forget the devices a sign-in switched on, when it signs out."""
    if not session_ref:
        return 0
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM push_devices WHERE session_ref=?", (session_ref,))
    conn.commit()
    return cur.rowcount


# --- the server's keys --------------------------------------------------------------

@dataclass(frozen=True)
class VapidKeys:
    private: "ec.EllipticCurvePrivateKey"
    public: str             # the raw P-256 point, base64url: what a page subscribes with


_VAPID: Dict[Tuple[str, str], VapidKeys] = {}
_VAPID_LOCK = threading.Lock()
_WHY = {"web": ""}
_WARNED: set = set()


def key_path() -> str:
    """Where the generated key is kept: beside the database, like the cookie key."""
    from . import database as db
    return os.path.join(os.path.dirname(db.default_path()), "vapid.pem")


def vapid_keys() -> Optional[VapidKeys]:
    """The server's VAPID key pair, or None when there is none (why_not says why)."""
    told = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    path = key_path()
    with _VAPID_LOCK:
        hit = _VAPID.get((told, path))
        if hit is None:
            try:
                hit = _load_vapid(told, path)
            except Exception as exc:          # a bad setting or an unwritable disk
                _WHY["web"] = str(exc)
                _warn_once("vapid", "Browser notifications are off: %s" % exc)
                return None
            _VAPID.clear()
            _VAPID[(told, path)] = hit
            _WHY["web"] = ""
    return hit


def _load_vapid(told: str, path: str) -> VapidKeys:
    if _NO_CRYPTO:
        raise RuntimeError(_NO_CRYPTO)
    if told:
        try:
            private = private_key(told)
        except Exception:
            raise ValueError("VAPID_PRIVATE_KEY is not a P-256 private key")
    else:
        private = _kept_key(path)
    public = b64url(_raw_public(private))
    given = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip().rstrip("=")
    if given and given != public:
        # The public half is worked out from the private one, so a mismatch
        # means the pair was pasted wrong. Browsers are given the right one.
        _warn_once("vapid-public", "VAPID_PUBLIC_KEY does not belong to "
                   "VAPID_PRIVATE_KEY; using the public key the private one has")
    return VapidKeys(private, public)


def _kept_key(path: str) -> "ec.EllipticCurvePrivateKey":
    """The generated key pair, made the first time and read every time after.

    Created with O_EXCL, so two processes starting at once cannot each write
    a different key: the second reads the first's. A file that exists but
    cannot be read is left alone for a person to look at, never replaced,
    because every browser that subscribed is tied to the key inside it.
    """
    try:
        with open(path, "rb") as fh:
            kept = fh.read()
        if kept.strip():
            return private_key(kept.decode("ascii"))
        # Empty: a start-up stopped between making the file and writing it.
        # No browser can have subscribed to a key that never existed, so it
        # is made again, once the file is old enough not to be another
        # process still writing it.
        if time.time() - os.path.getmtime(path) < 60:
            raise ValueError("the key file %s is still being written" % path)
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        raise ValueError(str(exc) if "key file" in str(exc)
                         else "the key file %s could not be read" % path)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        with open(path, "rb") as fh:
            return private_key(fh.read().decode("ascii"))
    except OSError as exc:
        # Kept only in memory it would change on every restart, and every
        # browser subscribed to the old one would go quiet. Better off.
        raise OSError("the key file %s could not be written (%s)"
                      % (path, exc.strerror or type(exc).__name__))
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return key


def private_key(text: str) -> "ec.EllipticCurvePrivateKey":
    """A P-256 private key from a setting or a file.

    Takes PEM, with real line breaks or with \\n typed in their place as a
    hosting dashboard's one-line box needs; the PEM's base64 body alone, as a
    .p8 file holds it; or the bare 32-byte number in base64url, which is what
    most Web Push tools print. Raises ValueError.
    """
    text = text.strip().strip("\"'").replace("\\n", "\n").replace("\r", "")
    if "-----BEGIN" in text:
        key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
    else:
        raw = unb64url(text)
        if len(raw) == 32:
            key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        else:
            key = serialization.load_der_private_key(raw, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("not a P-256 key")
    return key


def _raw_public(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def _warn_once(what: str, message: str) -> None:
    if what not in _WARNED:
        _WARNED.add(what)
        log.warning(message)


def why_not() -> Dict[str, str]:
    """Why each way of notifying is off, for whoever runs the server."""
    vapid_keys()
    missing = [name for name in ("APPLE_TEAM_ID", "APNS_KEY_ID", "APNS_PRIVATE_KEY")
               if not (os.environ.get(name) or "").strip()]
    if missing:
        apns = "%s not set" % ", ".join(missing)
    elif not apns_ready():
        apns = ("APNS_PRIVATE_KEY is not a P-256 key, or the h2 package that "
                "HTTP/2 needs is not installed")
    else:
        apns = ""
    return {"web": _WHY["web"], "apns": apns}


# --- signing ------------------------------------------------------------------------

def es256_jwt(header: Dict, claims: Dict, key) -> str:
    """A JSON Web Token signed with ES256, as VAPID and APNs both want it.

    The cryptography package returns an ECDSA signature in DER; a JWT carries
    the two numbers side by side instead, 32 bytes each (RFC 7518, 3.4).
    """
    def part(obj: Dict) -> str:
        return b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    signing = part(header) + "." + part(claims)
    r, s = decode_dss_signature(key.sign(signing.encode("ascii"),
                                         ec.ECDSA(hashes.SHA256())))
    return signing + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def subject(site: str) -> str:
    """Who the push services may contact about this server (RFC 8292's sub)."""
    contact = (os.environ.get("STOCKBOT_CONTACT") or "").strip()
    if contact.startswith(("mailto:", "https://")):
        return contact
    if "@" in contact:
        return "mailto:" + contact
    return site


def vapid_header(endpoint: str, keys: VapidKeys, sub: str,
                 now: Optional[float] = None) -> str:
    """The Authorization header for one push service (RFC 8292).

    aud is the push service's origin, exp at most 12 hours away, and k the
    public key the browser subscribed with, which is how the service knows
    the request comes from the server the browser trusts.
    """
    now = int(time.time() if now is None else now)
    host = (urlsplit(endpoint).hostname or "").lower()
    claims = {"aud": "https://" + host, "exp": now + VAPID_LIFETIME, "sub": sub}
    token = es256_jwt({"typ": "JWT", "alg": "ES256"}, claims, keys.private)
    return "vapid t=%s, k=%s" % (token, keys.public)


# --- encrypting for one browser (RFC 8291) --------------------------------------------

def _hmac(key: bytes, data: bytes) -> bytes:
    mac = hmac.HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def content_keys(ecdh_secret: bytes, auth: bytes, ua_public: bytes,
                 as_public: bytes, salt: bytes) -> Tuple[bytes, bytes]:
    """The content key and nonce for one message (RFC 8291, section 3.4).

    Three HKDF steps, each a single HMAC because every output is shorter than
    one SHA-256 block: the browser's secret is mixed with the ECDH secret and
    both public keys, and the message's random salt with that.
    """
    prk_key = _hmac(auth, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    return cek, nonce


def encrypt(plaintext: bytes, ua_public: bytes, auth: bytes,
            salt: Optional[bytes] = None, server_key=None,
            record_size: int = RECORD_SIZE) -> bytes:
    """One aes128gcm message that only the browser holding ua_public can read.

    A fresh key pair and salt for every message, as RFC 8291 asks; salt and
    server_key are parameters only so the RFC's example can be reproduced.
    The body is the header (salt, record size, the server's one-off public
    key) and a single record ending in the 0x02 delimiter.
    """
    salt = os.urandom(16) if salt is None else salt
    server_key = ec.generate_private_key(ec.SECP256R1()) if server_key is None else server_key
    as_public = _raw_public(server_key)
    browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    secret = server_key.exchange(ec.ECDH(), browser)
    cek, nonce = content_keys(secret, auth, ua_public, as_public, salt)
    record = plaintext + b"\x02"
    if len(record) + 16 > record_size:
        raise ValueError("a notice must fit one record of %d bytes" % record_size)
    header = salt + struct.pack("!IB", record_size, len(as_public)) + as_public
    return header + AESGCM(cek).encrypt(nonce, record, None)


# --- what is said ---------------------------------------------------------------------

@dataclass
class Note:
    """One notification, in the shape each service wants it."""
    title: str
    body: str
    url: str = "/monitor"
    tag: str = "alerts"         # a newer notice with the same tag replaces the older
    ttl: int = ALERT_TTL
    urgency: str = "high"

    def for_web(self) -> bytes:
        """What the service worker reads in its push event."""
        return json.dumps({"title": self.title, "body": self.body, "url": self.url,
                           "tag": self.tag}, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def for_apns(self) -> bytes:
        """What the iPhone shows. url sits beside aps, where the app reads it."""
        return json.dumps({"aps": {"alert": {"title": self.title, "body": self.body},
                                   "sound": "default", "thread-id": "orenth-alerts"},
                           "url": self.url}, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    @property
    def topic(self) -> str:
        """The Web Push Topic header: up to 32 characters of base64url."""
        clean = re.sub(r"[^A-Za-z0-9_-]", "", self.tag)
        if 0 < len(clean) <= 32:
            return clean
        return b64url(hashlib.sha256(self.tag.encode("utf-8")).digest())[:32]


def _fit(text: str, limit: int) -> str:
    """Text cut to at most limit bytes of UTF-8, on a whole character."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit - 3].decode("utf-8", "ignore").rstrip() + "…"


def _tag(hits) -> str:
    """Which notices replace each other: the same alert or stop firing again.

    A round with several fires is tagged by the set of them, so it replaces
    nothing else but is replaced by the same set firing again.
    """
    def one(hit) -> str:
        if getattr(hit, "kind", "") == "stop_breach" and getattr(hit, "trade_id", None):
            return "stop-%d" % int(hit.trade_id)
        return "alert-%d" % int(getattr(hit, "alert_id", 0) or 0)

    if len(hits) == 1:
        return one(hits[0])
    joined = ",".join(sorted(one(h) for h in hits))
    return "alerts-" + hashlib.sha256(joined.encode("ascii")).hexdigest()[:16]


def alert_note(hits) -> Note:
    """One notice for one person's fires this round, in the alert email's words.

    The email (alerts.deliver) says each fire's message and "(price as of"
    when the price was set; so does this, one line each, so a phone never
    says something the inbox does not. The title is the email's subject
    without the message, which the body already holds.
    """
    hits = list(hits)
    lines = ["%s (price as of %s)" % (h.message, h.as_of or "unknown")
             for h in hits[:SHOWN]]
    if len(hits) > SHOWN:
        lines.append("And %d more on your Monitor page." % (len(hits) - SHOWN))
    title = "Orenth" if len(hits) == 1 else "Orenth: %d alerts" % len(hits)
    return Note(title=title, body=_fit("\n".join(lines), BODY_BYTES),
                url="/monitor", tag=_tag(hits))


def switched_on_note() -> Note:
    """What a person sends themselves to see that a device gets them."""
    return Note(title="Notifications are on",
                body="This is how a fired alert will reach this device.",
                url="/monitor", tag="test", ttl=TEST_TTL)


# --- sending ----------------------------------------------------------------------------

@dataclass
class Sent:
    """What became of one notice to one device.

    error is what the person reads under their device on the Settings page
    and after "Send a test", so it is plain words. detail is the service's
    own code or the exception's name, for the server log only.
    """
    ok: bool
    error: str = ""
    drop: bool = False          # the device is gone for good: forget it
    blame: bool = True          # counts toward forgetting a device that keeps failing
    stop: bool = False          # no use trying this service again this round
    environment: str = ""       # APNs: where the token turned out to live
    detail: str = ""            # for the log: a status code, Apple's reason, an exception


WebPost = Callable[[str, bytes, Dict[str, str]], int]
ApnsPost = Callable[[str, str, bytes, Dict[str, str]], Tuple[int, str]]


def _web_post(url: str, body: bytes, headers: Dict[str, str]) -> int:
    """POST one encrypted notice to a push service; returns the HTTP status.

    Never follows a redirect: the address was checked, wherever it might
    send the request on to was not.
    """
    import requests
    response = requests.post(url, data=body, headers=headers, allow_redirects=False,
                             timeout=(CONNECT_TIMEOUT, TIMEOUT))
    return response.status_code


def send_web(row, note: Note, keys: VapidKeys, sub: str,
             post: Optional[WebPost] = None) -> Sent:
    """One notice to one browser, through its push service."""
    if not push_service(row["address"]):
        # Checked when it was saved; checked again here, so a row from before
        # a service left the list is never posted to.
        return Sent(False, "not a push service Orenth sends to", drop=True)
    try:
        body = encrypt(note.for_web(), unb64url(row["p256dh"]), unb64url(row["auth"]))
    except (TypeError, ValueError):
        return Sent(False, "its keys cannot be used", drop=True)
    headers = {"TTL": str(int(note.ttl)), "Urgency": note.urgency,
               "Topic": note.topic, "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream",
               "Authorization": vapid_header(row["address"], keys, sub)}
    try:
        status = (post or _web_post)(row["address"], body, headers)
    except Exception as exc:
        return Sent(False, "the notification service did not answer just now",
                    blame=False, stop=True, detail=type(exc).__name__)
    if 200 <= status < 300:
        return Sent(True)
    if status in (404, 410):
        return Sent(False, "the browser has stopped taking notifications from Orenth",
                    drop=True, detail=str(status))
    if status == 429:
        return Sent(False, "the notification service asked Orenth to wait a little",
                    blame=False, stop=True, detail=str(status))
    if status >= 500:
        return Sent(False, "the notification service had a fault just now", blame=False,
                    stop=True, detail=str(status))
    return Sent(False, "the notification service turned it away", detail=str(status))


# --- APNs ------------------------------------------------------------------------------

def _first_line(raw: Optional[str]) -> str:
    for line in (raw or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def apns_settings() -> Dict[str, str]:
    return {"team": _first_line(os.environ.get("APPLE_TEAM_ID")),
            "key_id": _first_line(os.environ.get("APNS_KEY_ID")),
            "key": (os.environ.get("APNS_PRIVATE_KEY") or "").strip(),
            "topic": _first_line(os.environ.get("APNS_BUNDLE_ID")) or DEFAULT_BUNDLE}


_APNS_KEYS: Dict[str, object] = {}


def _apns_key(text: str):
    """The .p8 signing key, read once per value of the setting; None if unusable."""
    if text not in _APNS_KEYS:
        try:
            _APNS_KEYS.clear()
            _APNS_KEYS[text] = private_key(text)
        except Exception:
            _APNS_KEYS[text] = None
            _warn_once("apns-key", "APNS_PRIVATE_KEY is not a P-256 private key, "
                                   "so the iPhone app gets no notifications")
    return _APNS_KEYS[text]


def _http2() -> bool:
    try:
        import h2  # noqa: F401  needed by httpx2 for HTTP/2, which Apple requires
        import httpx2  # noqa: F401
    except ImportError:
        return False
    return True


def apns_ready() -> bool:
    """Whether the iPhone app can be sent notifications at all."""
    s = apns_settings()
    if _NO_CRYPTO or not (s["team"] and s["key_id"] and s["key"]):
        return False
    return _apns_key(s["key"]) is not None and _http2()


_PROVIDER = {"token": "", "at": 0.0, "for": ()}
_PROVIDER_LOCK = threading.Lock()
_APNS_PAUSED = {"until": 0.0}


def provider_token(now: Optional[float] = None, renew: bool = False) -> str:
    """Apple's bearer token for this server, kept and reused.

    Apple wants one no older than an hour and refuses one replaced more
    often than every 20 minutes (TooManyProviderTokenUpdates), so one is
    reused for 40 minutes. renew asks for a fresh one after Apple called the
    last expired or invalid; inside the 20 minutes the old one is kept.
    """
    s = apns_settings()
    now = time.time() if now is None else now
    ident = (s["team"], s["key_id"], hashlib.sha256(s["key"].encode("utf-8")).hexdigest())
    with _PROVIDER_LOCK:
        age = now - _PROVIDER["at"]
        if _PROVIDER["token"] and _PROVIDER["for"] == ident \
                and (age < APNS_FLOOR or (age < APNS_REUSE and not renew)):
            return _PROVIDER["token"]
        token = es256_jwt({"alg": "ES256", "kid": s["key_id"]},
                          {"iss": s["team"], "iat": int(now)}, _apns_key(s["key"]))
        _PROVIDER.update({"token": token, "at": now, "for": ident})
        return token


_CLIENT: Dict[str, object] = {"apns": None}
_CLIENT_LOCK = threading.Lock()


def _apns_client():
    """One HTTP/2 connection pool to Apple, shared by every send."""
    with _CLIENT_LOCK:
        if _CLIENT["apns"] is None:
            import httpx2
            _CLIENT["apns"] = httpx2.Client(
                http2=True, timeout=httpx2.Timeout(TIMEOUT, connect=CONNECT_TIMEOUT))
        return _CLIENT["apns"]


def _apns_post(environment: str, token: str, payload: bytes,
               headers: Dict[str, str]) -> Tuple[int, str]:
    """POST one notice to Apple; returns the status and Apple's reason, if any."""
    response = _apns_client().post("%s/3/device/%s" % (APNS_HOSTS[environment], token),
                                   content=payload, headers=headers)
    reason = ""
    if response.status_code != 200:
        try:
            reason = str((response.json() or {}).get("reason") or "")
        except ValueError:
            reason = ""
    return response.status_code, reason[:60]


# Reasons Apple gives that are about the device, not about this server.
_GONE = ("Unregistered", "DeviceTokenNotForTopic")


def send_apns(row, note: Note, post: Optional[ApnsPost] = None) -> Sent:
    """One notice to the iPhone app, through Apple.

    Production first, where TestFlight and App Store builds live; a token
    Apple does not know there is tried in the sandbox, where a build run
    from Xcode lives, and wherever it works is remembered on the device.
    """
    if time.time() < _APNS_PAUSED["until"]:
        return Sent(False, "held back: Apple asked Orenth to wait a little",
                    blame=False, stop=True)
    try:
        clean_token(row["address"])         # it goes into the request's path
    except PushError:
        return Sent(False, "not an iPhone's device token", drop=True)
    s = apns_settings()
    post = post or _apns_post
    known = row["environment"] if row["environment"] in APNS_HOSTS else ""
    order = [known or "production"]
    order.append("sandbox" if order[0] == "production" else "production")
    payload = note.for_apns()
    for environment in order:
        renewed = False
        while True:
            headers = {"authorization": "bearer " + provider_token(),
                       "apns-topic": s["topic"], "apns-push-type": "alert",
                       "apns-priority": "10",
                       "apns-expiration": str(int(time.time()) + int(note.ttl)),
                       "apns-collapse-id": note.tag[:64]}
            try:
                status, reason = post(environment, row["address"], payload, headers)
            except Exception as exc:
                return Sent(False, "Apple's notification service did not answer just now",
                            blame=False, stop=True, detail=type(exc).__name__)
            if status == 403 and reason in ("ExpiredProviderToken",
                                            "InvalidProviderToken") and not renewed:
                renewed = True
                before = provider_token()
                if provider_token(renew=True) != before:
                    continue                    # once more, with a fresh token
            break
        if status == 200:
            return Sent(True, environment=environment)
        if status == 400 and reason == "BadDeviceToken":
            continue                            # perhaps the other environment's
        said = ("%s %s" % (status, reason)).strip()
        if status == 410 or reason in _GONE:
            return Sent(False, "Apple says the app is no longer on that iPhone",
                        drop=True, detail=said)
        if status == 429:
            _APNS_PAUSED["until"] = time.time() + APNS_PAUSE
            return Sent(False, "Apple asked Orenth to wait a little", blame=False,
                        stop=True, detail=said)
        if status == 403 or status >= 500 or (status == 400 and reason):
            # The key, the bundle id, or Apple itself: the same for every device.
            return Sent(False, "Apple turned it away for a reason on Orenth's side or "
                               "Apple's, not this iPhone's", blame=False, stop=True,
                        detail=said)
        return Sent(False, "Apple turned it away", detail=said)
    return Sent(False, "Apple does not know this device", drop=True)


# --- a round ------------------------------------------------------------------------------

def _service(row) -> str:
    if row["kind"] == "apns":
        return "apns"
    return (urlsplit(row["address"]).hostname or "").lower()


def _record(conn: sqlite3.Connection, row, result: Sent) -> str:
    """Write down what became of a notice; 'sent', 'failed' or 'dropped'."""
    device = int(row["id"])
    if result.ok:
        conn.execute("UPDATE push_devices SET last_ok_ts=?, last_error=NULL, "
                     "failures=0, environment=CASE WHEN ?<>'' THEN ? ELSE "
                     "environment END WHERE id=?",
                     (int(time.time()), result.environment, result.environment, device))
        conn.commit()
        return "sent"
    failures = int(row["failures"] or 0) + (1 if result.blame else 0)
    if result.drop or failures >= MAX_FAILURES:
        conn.execute("DELETE FROM push_devices WHERE id=?", (device,))
        conn.commit()
        return "dropped"
    conn.execute("UPDATE push_devices SET last_error=?, failures=? WHERE id=?",
                 (result.error[:200], failures, device))
    conn.commit()
    return "failed"


def _send_all(conn: sqlite3.Connection, rows, note: Note, site: str,
              halted: Optional[set] = None, web_post: Optional[WebPost] = None,
              apns_post: Optional[ApnsPost] = None) -> Dict:
    """One notice to each of these devices, each outcome written down.

    halted holds services that failed in a way every device on them would
    (a timeout, an outage, a request to slow down), so the rest of the round
    skips them instead of waiting on each in turn.
    """
    halted = set() if halted is None else halted
    keys = vapid_keys() if any(r["kind"] == "web" for r in rows) else None
    sub = subject(site)
    out = {"devices": len(rows), "sent": 0, "failed": 0, "dropped": 0, "errors": []}
    for row in rows:
        service = _service(row)
        if service in halted:
            result = Sent(False, "not tried this time: the notification service had "
                                 "just failed for another device", blame=False)
        elif row["kind"] == "web":
            result = (send_web(row, note, keys, sub, web_post) if keys is not None
                      else Sent(False, "notifications to browsers are paused on "
                                       "Orenth's side", blame=False,
                                detail="no VAPID key"))
        elif row["kind"] == "apns":
            result = (send_apns(row, note, apns_post) if apns_ready()
                      else Sent(False, "notifications to the iPhone app are paused on "
                                       "Orenth's side", blame=False,
                                detail="APNs is not set up"))
        else:
            result = Sent(False, "not a kind of device Orenth knows", drop=True)
        if result.stop:
            halted.add(service)
        outcome = _record(conn, row, result)
        out[outcome] += 1
        if not result.ok:
            out["errors"].append({"id": int(row["id"]), "label": row["label"],
                                  "error": result.error})
            # The service's name and the row's id, never its address or token;
            # the code or exception is said here, and only here.
            log.warning("push: device %d on %s %s: %s%s", int(row["id"]), service,
                        outcome, result.error,
                        " (%s)" % result.detail if result.detail else "")
    return out


def deliver(conn: sqlite3.Connection, fired, site: str,
            web_post: Optional[WebPost] = None,
            apns_post: Optional[ApnsPost] = None) -> Dict:
    """Notify each person's devices of what fired for them this round.

    One notice per device per person, however many fired. site is the
    public address, the contact for the push services when STOCKBOT_CONTACT
    is not set. The plan's limit on alerts was applied before anything got
    here, and only alerts with an owner are sent.
    """
    ensure_schema(conn)
    by_user: Dict[int, List] = {}
    for hit in fired:
        if getattr(hit, "user_id", None) is not None:
            by_user.setdefault(int(hit.user_id), []).append(hit)
    totals = {"people": 0, "devices": 0, "sent": 0, "failed": 0, "dropped": 0}
    halted: set = set()
    for user_id, hits in by_user.items():
        rows = devices(conn, user_id)
        if not rows:
            continue
        totals["people"] += 1
        result = _send_all(conn, rows, alert_note(hits), site, halted,
                           web_post, apns_post)
        for key in ("devices", "sent", "failed", "dropped"):
            totals[key] += result[key]
    return totals


def send_test(conn: sqlite3.Connection, user_id: int, site: str, kind: str = "",
              address: str = "", web_post: Optional[WebPost] = None,
              apns_post: Optional[ApnsPost] = None) -> Dict:
    """"Notifications are on", to this person's devices, or to the one named."""
    rows = devices(conn, user_id)
    if kind and address:
        rows = [r for r in rows if r["kind"] == kind and r["address"] == address]
    return _send_all(conn, rows, switched_on_note(), site, None, web_post, apns_post)


# --- off the alert loop's thread ------------------------------------------------------------

_QUEUE: "queue.Queue" = queue.Queue(maxsize=200)
_WORKER: Dict[str, Optional[threading.Thread]] = {"thread": None}
_WORKER_LOCK = threading.Lock()


def deliver_later(db_path: Optional[str], fired, site: str) -> bool:
    """Hand a round's fires to the sending thread, and return at once.

    The alert loop and the pages call this, and none of them may wait on a
    push service, or fail because one did. Returns whether anything was
    queued.
    """
    hits = [h for h in fired if getattr(h, "user_id", None) is not None]
    if not hits:
        return False
    with _WORKER_LOCK:
        thread = _WORKER["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_drain, name="orenth-push", daemon=True)
            _WORKER["thread"] = thread
            thread.start()
    try:
        _QUEUE.put_nowait((db_path, hits, site))
    except queue.Full:
        log.warning("push: %d notices dropped, the sending queue is full", len(hits))
        return False
    return True


def _drain() -> None:
    from . import database as db
    while True:
        db_path, hits, site = _QUEUE.get()
        try:
            conn = db.connect(db_path)
            try:
                deliver(conn, hits, site)
            finally:
                conn.close()
        except Exception:
            log.exception("push: a round of notifications failed")
        finally:
            _QUEUE.task_done()


def wait_idle(timeout: float = 10.0) -> bool:
    """Wait until everything queued has been sent. For tests and shutting down."""
    end = time.time() + timeout
    with _QUEUE.all_tasks_done:
        while _QUEUE.unfinished_tasks:
            left = end - time.time()
            if left <= 0:
                return False
            _QUEUE.all_tasks_done.wait(left)
    return True
