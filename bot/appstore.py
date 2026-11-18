"""Checking what Apple signs for in-app purchases.

Pro bought in the iPhone app reaches this server two ways: the app sends the
transaction StoreKit gave it, and Apple's own server sends App Store Server
Notifications (version 2). Both are JWS (header.payload.signature, base64url)
that Apple signed. The header names alg ES256 and carries x5c, three base64 DER
certificates: Apple's signing leaf, Apple's intermediate and Apple's root.

Only the cryptography package is used. The steps follow Apple's own App Store
Server Library (SignedDataVerifier in app-store-server-library-python 3.1.2):

1. The header says alg ES256, names no critical extension, and holds exactly
   three certificates.
2. The leaf and the intermediate from x5c are checked up to Apple Root CA - G3,
   which is built into this file and pinned by its SHA-256. The third
   certificate in x5c is never used: a chain that brings its own root proves
   nothing, because anyone can make a root.
3. The intermediate must carry Apple's marker extension 1.2.840.113635.100.6.2.1
   and the leaf 1.2.840.113635.100.6.11.1.
4. Every certificate must be valid at the payload's signedDate. Apple's library
   does the same when it does not check revocation online, so a notification
   that Apple retries days later still verifies. It also means a copy Apple
   signed long ago verifies for ever: what a copy may change is decided by
   bot/apple_store.py, never by the signature alone.
5. The ES256 signature (raw r||s, 64 bytes) must verify over "header.payload"
   with the leaf's P-256 key.

Then the payload is checked for this app: bundleId, environment (Production or
Sandbox; the production server must take Sandbox too, because App Review buys
in the sandbox with the production build), and, for Production notifications,
appAppleId.

Anything that cannot be read is simply not Apple's: every failure is a
VerificationError, never another exception, because all of this runs on
whatever anyone chooses to post.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import ExtensionOID

from . import safejson

# Apple Root CA - G3, from https://www.apple.com/certificateauthority/AppleRootCA-G3.cer
# SHA-256 63:34:3A:BF:B8:9A:6A:03:EB:B5:7E:9B:3F:5F:A7:BE:7C:4F:5C:75:6F:30:17:B3:A8:C4:88:C3:65:3E:91:79
# Valid 2014-04-30 to 2039-04-30.
APPLE_ROOT_CA_G3_SHA256 = "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"
APPLE_ROOT_CA_G3_B64 = (
    "MIICQzCCAcmgAwIBAgIILcX8iNLFS5UwCgYIKoZIzj0EAwMwZzEbMBkGA1UEAwwSQXBwbGUgUm9v"
    "dCBDQSAtIEczMSYwJAYDVQQLDB1BcHBsZSBDZXJ0aWZpY2F0aW9uIEF1dGhvcml0eTETMBEGA1UE"
    "CgwKQXBwbGUgSW5jLjELMAkGA1UEBhMCVVMwHhcNMTQwNDMwMTgxOTA2WhcNMzkwNDMwMTgxOTA2"
    "WjBnMRswGQYDVQQDDBJBcHBsZSBSb290IENBIC0gRzMxJjAkBgNVBAsMHUFwcGxlIENlcnRpZmlj"
    "YXRpb24gQXV0aG9yaXR5MRMwEQYDVQQKDApBcHBsZSBJbmMuMQswCQYDVQQGEwJVUzB2MBAGByqG"
    "SM49AgEGBSuBBAAiA2IABJjpLz1AcqTtkyJygRMc3RCV8cWjTnHcFBbZDuWmBSp3ZHtfTjjTuxxE"
    "tX/1H7YyYl3J6YRbTzBPEVoA/VhYDKX1DyxNB0cTddqXl5dvMVztK517IDvYuVTZXpmkOlEKMaNC"
    "MEAwHQYDVR0OBBYEFLuw3qFYM4iapIqZ3r6966/ayySrMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0P"
    "AQH/BAQDAgEGMAoGCCqGSM49BAMDA2gAMGUCMQCD6cHEFl4aXTQY2e3v9GwOAEZLuN+yRhHFD/3m"
    "eoyhpmvOwgPUnPWTxnS4at+qIxUCMG1mihDK1A3UT82NQz60imOlM27jbdoXt2QfyFMm+YhidDkL"
    "F1vLUagM6BgD56KyKA=="
)

LEAF_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
INTERMEDIATE_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")

# Critical extensions these checks understand. A certificate with any other
# critical extension is refused, as RFC 5280 asks.
_KNOWN_CRITICAL = {ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE,
                   ExtensionOID.EXTENDED_KEY_USAGE, ExtensionOID.CERTIFICATE_POLICIES}

MAX_JWS_LENGTH = 64 * 1024     # real ones are 3 to 10 KB
# The latest time a signedDate may name: the last millisecond of the year 9999.
MAX_MS = 253402300799999

PRODUCTION = "Production"
SANDBOX = "Sandbox"

# Failure kinds, named as in Apple's VerificationStatus.
VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
INVALID_APP_IDENTIFIER = "INVALID_APP_IDENTIFIER"
INVALID_CERTIFICATE = "INVALID_CERTIFICATE"
INVALID_CHAIN_LENGTH = "INVALID_CHAIN_LENGTH"
INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class VerificationError(Exception):
    """Signed data that is not from Apple, or not for this app."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__("%s%s" % (status, ": " + detail if detail else ""))
        self.status = status
        self.detail = detail


def _fail(status: str, detail: str = ""):
    raise VerificationError(status, detail)


def apple_root() -> x509.Certificate:
    """Apple Root CA - G3, after checking the copy in this file is the real one."""
    der = base64.b64decode(APPLE_ROOT_CA_G3_B64, validate=True)
    if hashlib.sha256(der).hexdigest() != APPLE_ROOT_CA_G3_SHA256:
        raise RuntimeError("The built-in Apple root certificate has been altered.")
    return x509.load_der_x509_certificate(der)


# --- the JWS itself ------------------------------------------------------------

_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def _b64url(part: str) -> bytes:
    if not _B64URL.fullmatch(part) or len(part) % 4 == 1:
        _fail(VERIFICATION_FAILURE, "not base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out


def _json_object(raw: bytes) -> Dict[str, Any]:
    try:
        value = safejson.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        # Duplicate keys could be read two ways; nesting too deep for Python
        # to read is not something Apple sends (bot/safejson.py).
        raise VerificationError(VERIFICATION_FAILURE, "not JSON") from exc
    if not isinstance(value, dict):
        _fail(VERIFICATION_FAILURE, "not a JSON object")
    return value


def split_jws(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
    """Header, payload, the signed bytes and the signature. Nothing is trusted yet."""
    if not isinstance(token, str) or not token or len(token) > MAX_JWS_LENGTH:
        _fail(VERIFICATION_FAILURE, "missing or too long")
    parts = token.split(".")
    if len(parts) != 3:
        _fail(VERIFICATION_FAILURE, "not three parts")
    header = _json_object(_b64url(parts[0]))
    payload = _json_object(_b64url(parts[1]))
    signature = _b64url(parts[2])
    return header, payload, (parts[0] + "." + parts[1]).encode("ascii"), signature


def _ms_to_datetime(ms: int) -> datetime.datetime:
    # Added to the epoch rather than read through the platform's gmtime, which
    # on some systems refuses dates past the year 3000.
    try:
        return _EPOCH + datetime.timedelta(milliseconds=ms)
    except OverflowError as exc:
        raise VerificationError(VERIFICATION_FAILURE, "time out of range") from exc


def _signed_at(payload: Dict[str, Any], now: float) -> datetime.datetime:
    """When the certificates must have been valid: Apple's signedDate."""
    for key in ("signedDate", "receiptCreationDate"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 0 < value <= MAX_MS:
                _fail(VERIFICATION_FAILURE, "%s is not a time" % key)
            return _ms_to_datetime(value)
    return datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)


# --- the certificate chain -----------------------------------------------------

def _load_cert(b64: Any) -> x509.Certificate:
    if not isinstance(b64, str):
        _fail(INVALID_CERTIFICATE, "certificate is not a string")
    try:
        return x509.load_der_x509_certificate(base64.b64decode(b64, validate=True))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise VerificationError(INVALID_CERTIFICATE, "unreadable certificate") from exc


def _ext(cert: x509.Certificate, oid: x509.ObjectIdentifier):
    try:
        return cert.extensions.get_extension_for_oid(oid)
    except x509.ExtensionNotFound:
        return None


def _no_unknown_critical(cert: x509.Certificate, name: str) -> None:
    for extension in cert.extensions:
        if extension.critical and extension.oid not in _KNOWN_CRITICAL:
            _fail(VERIFICATION_FAILURE, "%s has an unknown critical extension" % name)


def _check_intermediate(cert: x509.Certificate, strict: bool) -> None:
    _no_unknown_critical(cert, "intermediate")
    bc = _ext(cert, ExtensionOID.BASIC_CONSTRAINTS)
    if bc is None or not bc.value.ca:
        _fail(VERIFICATION_FAILURE, "intermediate is not a CA")
    if strict and not bc.critical:
        _fail(VERIFICATION_FAILURE, "intermediate basic constraints not critical")
    ku = _ext(cert, ExtensionOID.KEY_USAGE)
    if ku is None and strict:
        _fail(VERIFICATION_FAILURE, "intermediate has no key usage")
    if ku is not None and not ku.value.key_cert_sign:
        _fail(VERIFICATION_FAILURE, "intermediate may not sign certificates")
    if _ext(cert, INTERMEDIATE_OID) is None:
        _fail(VERIFICATION_FAILURE, "intermediate lacks Apple's marker 1.2.840.113635.100.6.2.1")


def _check_leaf(cert: x509.Certificate, strict: bool) -> ec.EllipticCurvePublicKey:
    _no_unknown_critical(cert, "leaf")
    bc = _ext(cert, ExtensionOID.BASIC_CONSTRAINTS)
    if bc is not None and bc.value.ca:
        _fail(VERIFICATION_FAILURE, "leaf is a CA")
    ku = _ext(cert, ExtensionOID.KEY_USAGE)
    if ku is None and strict:
        _fail(VERIFICATION_FAILURE, "leaf has no key usage")
    if ku is not None and not ku.value.digital_signature:
        _fail(VERIFICATION_FAILURE, "leaf may not sign")
    if _ext(cert, LEAF_OID) is None:
        _fail(VERIFICATION_FAILURE, "leaf lacks Apple's marker 1.2.840.113635.100.6.11.1")
    key = cert.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        _fail(VERIFICATION_FAILURE, "leaf key is not P-256, so cannot sign ES256")
    return key


def _check_key_ids(child: x509.Certificate, parent: x509.Certificate) -> None:
    """Strict mode: the child names its issuer's key, as Apple's real certificates do."""
    aki = _ext(child, ExtensionOID.AUTHORITY_KEY_IDENTIFIER)
    ski = _ext(parent, ExtensionOID.SUBJECT_KEY_IDENTIFIER)
    if aki is None or ski is None or aki.value.key_identifier != ski.value.digest:
        _fail(VERIFICATION_FAILURE, "authority key identifier does not match")


_NOT_ISSUED = (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm)


def verify_chain(x5c: Any, at: datetime.datetime, roots: Sequence[x509.Certificate],
                 strict: bool = True) -> ec.EllipticCurvePublicKey:
    """The leaf's public key, once x5c[0] and x5c[1] lead to one of our roots."""
    if not roots:
        _fail(INVALID_CERTIFICATE, "no trusted roots")
    if not isinstance(x5c, list) or len(x5c) != 3:
        _fail(INVALID_CHAIN_LENGTH, "x5c must hold exactly three certificates")
    leaf = _load_cert(x5c[0])
    intermediate = _load_cert(x5c[1])
    if not isinstance(x5c[2], str):           # present, but never trusted or used
        _fail(INVALID_CERTIFICATE, "certificate is not a string")

    root = None
    for candidate in roots:
        try:
            intermediate.verify_directly_issued_by(candidate)
        except _NOT_ISSUED:
            continue
        root = candidate
        break
    if root is None:
        _fail(VERIFICATION_FAILURE, "intermediate was not issued by a trusted root")
    try:
        leaf.verify_directly_issued_by(intermediate)
    except _NOT_ISSUED as exc:
        raise VerificationError(VERIFICATION_FAILURE, "leaf was not issued by the intermediate") from exc

    for name, cert in (("leaf", leaf), ("intermediate", intermediate), ("root", root)):
        if not cert.not_valid_before_utc <= at <= cert.not_valid_after_utc:
            _fail(VERIFICATION_FAILURE, "%s not valid at %s" % (name, at.isoformat()))

    _check_intermediate(intermediate, strict)
    key = _check_leaf(leaf, strict)
    if strict:
        _check_key_ids(leaf, intermediate)
        _check_key_ids(intermediate, root)
    return key


def _verify_es256(key: ec.EllipticCurvePublicKey, signed: bytes, signature: bytes) -> None:
    if len(signature) != 64:                  # JWS ES256 is raw r||s, not DER
        _fail(VERIFICATION_FAILURE, "ES256 signature must be 64 bytes")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise VerificationError(VERIFICATION_FAILURE, "bad signature") from exc


def decode_signed(token: str, roots: Sequence[x509.Certificate], strict: bool = True,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """The payload of a JWS that Apple signed. Raises VerificationError otherwise."""
    try:
        header, payload, signed, signature = split_jws(token)
        if header.get("alg") != "ES256":
            _fail(VERIFICATION_FAILURE, "alg must be ES256")
        if "crit" in header:
            _fail(VERIFICATION_FAILURE, "unsupported critical header")
        if "x5c" not in header:
            _fail(VERIFICATION_FAILURE, "no x5c certificate chain")
        at = _signed_at(payload, time.time() if now is None else now)
        key = verify_chain(header["x5c"], at, roots, strict)
        _verify_es256(key, signed, signature)
    except VerificationError:
        raise
    except Exception as exc:
        # A certificate or value that no check above expected. Whatever it
        # is, it was not shown to be Apple's.
        raise VerificationError(VERIFICATION_FAILURE,
                                "unreadable (%s)" % type(exc).__name__) from exc
    return payload


# --- this app's rules ------------------------------------------------------------

class Verifier:
    """Checks Apple's signed data for one app.

    environments lists what is accepted. The production server takes both
    Production and Sandbox, because App Review buys in the sandbox while using
    the production build; whatever a Sandbox purchase unlocks is decided later,
    not here.
    """

    def __init__(self, bundle_id: str, app_apple_id: Optional[int] = None,
                 environments: Iterable[str] = (PRODUCTION, SANDBOX),
                 roots: Optional[Sequence[x509.Certificate]] = None,
                 strict: bool = True, clock=time.time):
        self.bundle_id = bundle_id
        self.app_apple_id = app_apple_id
        self.environments = tuple(environments)
        if PRODUCTION in self.environments and not app_apple_id:
            raise ValueError("appAppleId is required to accept Production notifications")
        self.roots = list(roots) if roots is not None else [apple_root()]
        self.strict = strict
        self.clock = clock

    def _decode(self, token: str) -> Dict[str, Any]:
        return decode_signed(token, self.roots, self.strict, self.clock())

    def _environment(self, value: Any) -> str:
        if value not in self.environments:
            _fail(INVALID_ENVIRONMENT, "environment %r not accepted" % (value,))
        return value

    def transaction(self, jws: str, environment: Optional[str] = None) -> Dict[str, Any]:
        """A JWSTransaction, from the app (jwsRepresentation) or from a notification."""
        tx = self._decode(jws)
        if tx.get("bundleId") != self.bundle_id:
            _fail(INVALID_APP_IDENTIFIER, "bundleId")
        env = self._environment(tx.get("environment"))
        if environment is not None and env != environment:
            _fail(INVALID_ENVIRONMENT, "transaction and notification disagree")
        return tx

    def renewal_info(self, jws: str, environment: Optional[str] = None) -> Dict[str, Any]:
        """A JWSRenewalInfo. It carries no bundleId; Apple checks only its environment."""
        info = self._decode(jws)
        env = self._environment(info.get("environment"))
        if environment is not None and env != environment:
            _fail(INVALID_ENVIRONMENT, "renewal info and notification disagree")
        return info

    def notification(self, signed_payload: str) -> Dict[str, Any]:
        """A V2 notification, with its signed transaction and renewal info decoded too.

        Returns the payload plus "environment", "transaction" and "renewal" keys
        (the last two None when the notification has none).
        """
        payload = self._decode(signed_payload)
        body = None
        for key in ("data", "summary", "appData", "externalPurchaseToken"):
            if isinstance(payload.get(key), dict):
                body = payload[key]
                break
        if body is None or body.get("bundleId") != self.bundle_id:
            _fail(INVALID_APP_IDENTIFIER, "bundleId")
        env = body.get("environment")
        if env is None and "externalPurchaseId" in body:
            env = SANDBOX if str(body["externalPurchaseId"]).startswith("SANDBOX") else PRODUCTION
        # Production notifications always name the app's Apple ID; sandbox ones
        # usually do not. One that names an Apple ID must name this app's.
        app_id = body.get("appAppleId")
        if (env == PRODUCTION and app_id != self.app_apple_id) or (
                app_id is not None and self.app_apple_id is not None
                and app_id != self.app_apple_id):
            _fail(INVALID_APP_IDENTIFIER, "appAppleId")
        env = self._environment(env)
        for key in ("notificationType", "notificationUUID"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                _fail(VERIFICATION_FAILURE, "%s missing" % key)
        if isinstance(payload.get("signedDate"), bool) or not isinstance(payload.get("signedDate"), int):
            _fail(VERIFICATION_FAILURE, "signedDate missing")
        tx = body.get("signedTransactionInfo")
        info = body.get("signedRenewalInfo")
        out = dict(payload)
        out["environment"] = env
        out["transaction"] = self.transaction(tx, env) if tx else None
        out["renewal"] = self.renewal_info(info, env) if info else None
        return out
