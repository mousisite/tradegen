"""Pro bought in the iPhone app, through Apple: what Apple signs, who it
belongs to, and what the site does with it.

  - The checks on what Apple signs (bot/appstore.py), against Apple's own
    signed test data and certificate vectors from app-store-server-library-
    python (MIT licence, in tests/data/appstore with APPLE_LICENSE.txt), and
    against home-made chains that try every way round them.
  - JSON that anyone can send, measured before it is read (bot/safejson.py),
    so nesting too deep for Python is refused, never a 500.
  - Who has Pro (bot/apple_store.py): purchases, renewals, grace periods,
    refunds and their reversal, notifications out of order or twice, old
    copies sent again, one Apple ID shared by two accounts, test purchases,
    moving a subscription, and deleting an account.
  - What the site does with it (web.py): Apple's notification address, the
    app's state, transactions and move addresses, Stripe checkout refused to
    someone already paying Apple, the subscribe screen inside the app and
    what the website says instead, and the warning before deleting an
    account that Apple still bills.

Apple's tests cannot sign with Apple's key either: they swap in a test root
(testCA.der) and sign with a key under it, and so do these.

Runs with unittest, as a script like every other suite:
    python tests/test_appstore.py
"""
import base64
import datetime
import hashlib
import json
import os as _os
import re
import sqlite3
import sys as _sys
import tempfile
import threading
import time
import unittest
import uuid

HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path.insert(0, _os.path.dirname(HERE))
_sys.path.insert(0, HERE)

# The settings these tests set themselves, cleared of anything the shell had.
APPLE_SETTINGS = ("APPLE_APP_ID", "APPLE_PRODUCT_IDS", "APPLE_BUNDLE_ID",
                  "APPLE_SANDBOX_PRO", "APPLE_SANDBOX_USERS")
for _key in APPLE_SETTINGS:
    _os.environ.pop(_key, None)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from bot import accounts as A  # noqa: E402
from bot import apple_store as st  # noqa: E402
from bot import appstore as av  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402
from bot import safejson  # noqa: E402

DATA = _os.path.join(HERE, "data", "appstore")
UTC = datetime.timezone.utc
BUNDLE = "app.orenth.ios"
APP_ID = 6740000001
MONTHLY = "app.orenth.ios.pro.monthly"
YEARLY = "app.orenth.ios.pro.yearly"
PRODUCTS = [MONTHLY, YEARLY]
DAY = 86400 * 1000


def now_ms():
    return int(time.time() * 1000)


# --- helpers: Apple's files, and a home-made chain shaped like Apple's ---------

def read_text(*parts):
    with open(_os.path.join(DATA, *parts), encoding="utf-8") as f:
        return f.read().strip()


def read_bytes(*parts):
    with open(_os.path.join(DATA, *parts), "rb") as f:
        return f.read()


with open(_os.path.join(DATA, "apple_x509_vectors.json"), encoding="utf-8") as _f:
    V = json.load(_f)
APPLE_TEST_ROOT = x509.load_der_x509_certificate(read_bytes("testCA.der"))


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_part(token, index):
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def replace_payload(token, payload):
    """Same header and signature, different payload: a tampered JWS."""
    head, _, sig = token.split(".")
    return head + "." + b64url(json.dumps(payload).encode()) + "." + sig


def replace_header(token, header, signature=None):
    _, body, sig = token.split(".")
    return b64url(json.dumps(header).encode()) + "." + body + "." + (sig if signature is None else signature)


def at(ts):
    return datetime.datetime.fromtimestamp(ts, tz=UTC)


def cert(name):
    return x509.load_der_x509_certificate(base64.b64decode(V[name]))


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


class Chain:
    """Root -> intermediate -> leaf, with Apple's marker extensions by default."""

    def __init__(self, leaf_oid=True, intermediate_oid=True, intermediate_ca=True,
                 leaf_ca=False, leaf_curve=None, key_ids=True, key_usage=True,
                 leaf_not_before=None, leaf_not_after=None, extra_leaf_ext=None,
                 name="Test"):
        now = datetime.datetime.now(UTC)
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        self.int_key = ec.generate_private_key(ec.SECP384R1())
        self.leaf_key = ec.generate_private_key(leaf_curve or ec.SECP256R1())
        start, end = now - datetime.timedelta(days=400), now + datetime.timedelta(days=400)

        root = (x509.CertificateBuilder().subject_name(_name(name + " Root")).issuer_name(_name(name + " Root"))
                .public_key(self.root_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(end + datetime.timedelta(days=3000))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.root_key.public_key()), critical=False))
        self.root = root.sign(self.root_key, hashes.SHA384())

        inter = (x509.CertificateBuilder().subject_name(_name(name + " Intermediate")).issuer_name(self.root.subject)
                 .public_key(self.int_key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(start).not_valid_after(end + datetime.timedelta(days=2000))
                 .add_extension(x509.BasicConstraints(ca=intermediate_ca, path_length=0 if intermediate_ca else None),
                                critical=True))
        if key_usage:
            inter = inter.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                                        critical=True)
        if key_ids:
            inter = (inter.add_extension(x509.SubjectKeyIdentifier.from_public_key(self.int_key.public_key()),
                                         critical=False)
                     .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.root_key.public_key()),
                                    critical=False))
        if intermediate_oid:
            inter = inter.add_extension(x509.UnrecognizedExtension(av.INTERMEDIATE_OID, b"\x05\x00"), critical=False)
        self.intermediate = inter.sign(self.root_key, hashes.SHA384())

        leaf = (x509.CertificateBuilder().subject_name(_name(name + " Leaf")).issuer_name(self.intermediate.subject)
                .public_key(self.leaf_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(leaf_not_before or start).not_valid_after(leaf_not_after or end)
                .add_extension(x509.BasicConstraints(ca=leaf_ca, path_length=None), critical=True))
        if key_usage:
            leaf = leaf.add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False),
                                      critical=True)
        if key_ids:
            leaf = leaf.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.int_key.public_key()),
                                      critical=False)
        if leaf_oid:
            leaf = leaf.add_extension(x509.UnrecognizedExtension(av.LEAF_OID, b"\x05\x00"), critical=False)
        if extra_leaf_ext is not None:
            leaf = leaf.add_extension(extra_leaf_ext, critical=True)
        self.leaf = leaf.sign(self.int_key, hashes.SHA384())

    @staticmethod
    def _b64(c):
        return base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()

    def x5c(self):
        return [self._b64(self.leaf), self._b64(self.intermediate), self._b64(self.root)]

    def sign(self, payload, alg="ES256", header=None, x5c=None, key=None, der_signature=False):
        head = {"alg": alg, "x5c": x5c if x5c is not None else self.x5c()}
        if header:
            head.update(header)
        signing_input = b64url(json.dumps(head).encode()) + "." + b64url(json.dumps(payload).encode())
        der = (key or self.leaf_key).sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        if der_signature:
            return signing_input + "." + b64url(der)
        r, s = decode_dss_signature(der)
        return signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def apple_test_verifier(**kw):
    """Like Apple's tests: their test root swapped in, strict checks off
    ('We don't have authority identifiers on test certs')."""
    args = dict(bundle_id="com.example", app_apple_id=1234, roots=[APPLE_TEST_ROOT], strict=False)
    args.update(kw)
    return av.Verifier(**args)


# --- what Apple signs --------------------------------------------------------------

class AppleTestResources(unittest.TestCase):
    """Apple's signed test data from app-store-server-library-python/tests/resources."""

    def test_notification_decodes(self):
        note = apple_test_verifier().notification(read_text("mock_signed_data", "testNotification"))
        self.assertEqual(note["notificationType"], "TEST")
        self.assertEqual(note["environment"], "Sandbox")
        self.assertEqual(note["notificationUUID"], "9ad56bd2-0bc6-42e0-af24-fd996d87a1e6")

    def test_transaction_and_renewal_info_decode(self):
        v = apple_test_verifier()
        self.assertEqual(v.transaction(read_text("mock_signed_data", "transactionInfo"))["environment"], "Sandbox")
        self.assertEqual(v.renewal_info(read_text("mock_signed_data", "renewalInfo"))["environment"], "Sandbox")

    def test_wrong_bundle_id(self):
        with self.assertRaises(av.VerificationError) as ctx:
            apple_test_verifier().notification(read_text("mock_signed_data", "wrongBundleId"))
        self.assertEqual(ctx.exception.status, av.INVALID_APP_IDENTIFIER)
        with self.assertRaises(av.VerificationError) as ctx:
            apple_test_verifier(bundle_id="com.examplex").transaction(read_text("mock_signed_data", "transactionInfo"))
        self.assertEqual(ctx.exception.status, av.INVALID_APP_IDENTIFIER)

    def test_missing_x5c(self):
        with self.assertRaises(av.VerificationError) as ctx:
            apple_test_verifier().notification(read_text("mock_signed_data", "missingX5CHeaderClaim"))
        self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)

    def test_production_only_server_refuses_sandbox(self):
        v = apple_test_verifier(environments=("Production",))
        for call, name in ((v.notification, "testNotification"), (v.transaction, "transactionInfo"),
                           (v.renewal_info, "renewalInfo")):
            with self.assertRaises(av.VerificationError) as ctx:
                call(read_text("mock_signed_data", name))
            self.assertEqual(ctx.exception.status, av.INVALID_ENVIRONMENT, name)

    def test_wrong_app_apple_id(self):
        with self.assertRaises(av.VerificationError) as ctx:
            apple_test_verifier(app_apple_id=1235).notification(read_text("mock_signed_data", "testNotification"))
        self.assertEqual(ctx.exception.status, av.INVALID_APP_IDENTIFIER)

    def test_wrong_root_real_apple_root(self):
        v = apple_test_verifier(roots=[av.apple_root()])
        with self.assertRaises(av.VerificationError) as ctx:
            v.notification(read_text("mock_signed_data", "testNotification"))
        self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)

    def test_strict_mode_needs_key_identifiers(self):
        # Apple's test certificates have no authority key identifiers, which is
        # why Apple's tests turn strict checks off. Real Apple chains have them.
        with self.assertRaises(av.VerificationError):
            apple_test_verifier(strict=True).notification(read_text("mock_signed_data", "testNotification"))

    def test_tampered_payload(self):
        token = read_text("mock_signed_data", "testNotification")
        payload = decode_part(token, 1)
        payload["notificationType"] = "DID_RENEW"
        with self.assertRaises(av.VerificationError) as ctx:
            apple_test_verifier().notification(replace_payload(token, payload))
        self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)

    def test_alg_none_and_hs256(self):
        token = read_text("mock_signed_data", "testNotification")
        header = decode_part(token, 0)
        for alg, sig in (("none", ""), ("HS256", b64url(b"x" * 32)), ("ES384", None)):
            forged = replace_header(token, dict(header, alg=alg), sig)
            with self.assertRaises(av.VerificationError) as ctx:
                apple_test_verifier().notification(forged)
            self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE, alg)

    def test_malformed(self):
        v = apple_test_verifier()
        for bad in ("a.b.c.d", "a.b.c", "", "a.b", "e30.e30.", "!!.e30.e30", None, 7, ["a.b.c"]):
            with self.assertRaises(av.VerificationError) as ctx:
                v.notification(bad)
            self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE, bad)


class AppleCertificateVectors(unittest.TestCase):
    """The certificate vectors from Apple's test_x509_verification.py."""

    def chain(self, *names):
        return [V[n] for n in names]

    def test_valid_chain(self):
        key = av.verify_chain(self.chain("LEAF_CERT_BASE64_ENCODED", "INTERMEDIATE_CA_BASE64_ENCODED",
                                         "ROOT_CA_BASE64_ENCODED"),
                              at(V["EFFECTIVE_DATE"]), [cert("ROOT_CA_BASE64_ENCODED")], strict=False)
        expected = ("-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE4rWBxGmFbnPIPQI0zsBKzLxsj8pD\n"
                    "2vqbr0yPISUx2WQyxmrNql9fhK8YEEyYFV7++p5i4YUSR/o9uQIgCPIhrA==\n-----END PUBLIC KEY-----\n")
        self.assertEqual(key.public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo).decode(), expected)

    def test_wrong_oids(self):
        for names in (("LEAF_CERT_FOR_INTERMEDIATE_CA_INVALID_OID_BASE64_ENCODED",
                       "INTERMEDIATE_CA_INVALID_OID_BASE64_ENCODED", "ROOT_CA_BASE64_ENCODED"),
                      ("LEAF_CERT_INVALID_OID_BASE64_ENCODED", "INTERMEDIATE_CA_BASE64_ENCODED",
                       "ROOT_CA_BASE64_ENCODED")):
            with self.assertRaises(av.VerificationError) as ctx:
                av.verify_chain(self.chain(*names), at(V["EFFECTIVE_DATE"]),
                                [cert("ROOT_CA_BASE64_ENCODED")], strict=False)
            self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)

    def test_chain_length_and_garbage(self):
        root = [cert("ROOT_CA_BASE64_ENCODED")]
        with self.assertRaises(av.VerificationError) as ctx:
            av.verify_chain(self.chain("INTERMEDIATE_CA_BASE64_ENCODED", "ROOT_CA_BASE64_ENCODED"),
                            at(V["EFFECTIVE_DATE"]), root, strict=False)
        self.assertEqual(ctx.exception.status, av.INVALID_CHAIN_LENGTH)
        for first in ("abc", base64.b64encode(b"abc").decode(), "@@@@"):
            with self.assertRaises(av.VerificationError) as ctx:
                av.verify_chain([first, V["INTERMEDIATE_CA_BASE64_ENCODED"], V["ROOT_CA_BASE64_ENCODED"]],
                                at(V["EFFECTIVE_DATE"]), root, strict=False)
            self.assertEqual(ctx.exception.status, av.INVALID_CERTIFICATE, first)

    def test_test_chain_against_real_root(self):
        with self.assertRaises(av.VerificationError) as ctx:
            av.verify_chain(self.chain("LEAF_CERT_BASE64_ENCODED", "INTERMEDIATE_CA_BASE64_ENCODED",
                                       "ROOT_CA_BASE64_ENCODED"),
                            at(V["EFFECTIVE_DATE"]), [av.apple_root()], strict=False)
        self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)

    def test_expired_chain(self):
        with self.assertRaises(av.VerificationError):
            av.verify_chain(self.chain("LEAF_CERT_BASE64_ENCODED", "INTERMEDIATE_CA_BASE64_ENCODED",
                                       "ROOT_CA_BASE64_ENCODED"),
                            at(2280946846), [cert("ROOT_CA_BASE64_ENCODED")], strict=False)

    def real(self):
        return self.chain("REAL_APPLE_SIGNING_CERTIFICATE_BASE64_ENCODED",
                          "REAL_APPLE_INTERMEDIATE_BASE64_ENCODED", "REAL_APPLE_ROOT_BASE64_ENCODED")

    def test_real_apple_chain_strict_with_pinned_root(self):
        key = av.verify_chain(self.real(), at(V["EFFECTIVE_DATE"]), [av.apple_root()], strict=True)
        self.assertIsInstance(key.curve, ec.SECP256R1)

    def test_real_apple_chain_outside_leaf_validity(self):
        for ts in (datetime.datetime(2027, 12, 1, tzinfo=UTC), datetime.datetime(2025, 9, 1, tzinfo=UTC)):
            with self.assertRaises(av.VerificationError):
                av.verify_chain(self.real(), ts, [av.apple_root()], strict=True)

    def test_real_apple_chain_against_test_root(self):
        with self.assertRaises(av.VerificationError):
            av.verify_chain(self.real(), at(V["EFFECTIVE_DATE"]), [APPLE_TEST_ROOT], strict=False)

    def test_broken_chain(self):
        mixed = [V["REAL_APPLE_SIGNING_CERTIFICATE_BASE64_ENCODED"], V["INTERMEDIATE_CA_BASE64_ENCODED"],
                 V["ROOT_CA_BASE64_ENCODED"]]
        with self.assertRaises(av.VerificationError):
            av.verify_chain(mixed, at(V["EFFECTIVE_DATE"]), [cert("ROOT_CA_BASE64_ENCODED")], strict=False)
        swapped = [V["REAL_APPLE_INTERMEDIATE_BASE64_ENCODED"], V["REAL_APPLE_SIGNING_CERTIFICATE_BASE64_ENCODED"],
                   V["REAL_APPLE_ROOT_BASE64_ENCODED"]]
        with self.assertRaises(av.VerificationError):
            av.verify_chain(swapped, at(V["EFFECTIVE_DATE"]), [av.apple_root()], strict=True)

    def test_third_certificate_is_never_trusted(self):
        attacker = Chain(name="Attacker")
        # The real leaf and intermediate with an attacker's root in x5c[2]: still
        # Apple's chain, because x5c[2] is ignored.
        av.verify_chain(self.real()[:2] + [attacker.x5c()[2]], at(V["EFFECTIVE_DATE"]), [av.apple_root()])
        # A whole chain under the attacker's own root: refused.
        with self.assertRaises(av.VerificationError):
            av.verify_chain(attacker.x5c(), datetime.datetime.now(UTC), [av.apple_root()])


class PinnedRoot(unittest.TestCase):
    def test_pinned_root_matches_apple_download(self):
        root = av.apple_root()
        der = root.public_bytes(serialization.Encoding.DER)
        self.assertEqual(hashlib.sha256(der).hexdigest(), av.APPLE_ROOT_CA_G3_SHA256)
        self.assertEqual(der, read_bytes("AppleRootCA-G3.cer"))
        self.assertEqual(der, base64.b64decode(V["REAL_APPLE_ROOT_BASE64_ENCODED"]))
        self.assertEqual(root.subject.rfc4514_string(),
                         "C=US,O=Apple Inc.,OU=Apple Certification Authority,CN=Apple Root CA - G3")

    def test_altered_root_is_refused(self):
        original = av.APPLE_ROOT_CA_G3_B64
        try:
            av.APPLE_ROOT_CA_G3_B64 = base64.b64encode(cert("ROOT_CA_BASE64_ENCODED").public_bytes(
                serialization.Encoding.DER)).decode()
            with self.assertRaises(RuntimeError):
                av.apple_root()
        finally:
            av.APPLE_ROOT_CA_G3_B64 = original

    def test_the_default_verifier_trusts_only_apple_strictly(self):
        v = av.Verifier(BUNDLE, APP_ID)
        self.assertTrue(v.strict)
        self.assertEqual([r.public_bytes(serialization.Encoding.DER) for r in v.roots],
                         [read_bytes("AppleRootCA-G3.cer")])
        self.assertEqual(v.environments, ("Production", "Sandbox"))


class HomeMadeChains(unittest.TestCase):
    """Negative cases Apple's vectors do not cover, on chains made here."""

    def payload(self, **kw):
        p = {"bundleId": BUNDLE, "environment": "Sandbox", "signedDate": int(time.time() * 1000)}
        p.update(kw)
        return p

    def verifier(self, chain, **kw):
        return av.Verifier(BUNDLE, APP_ID, roots=[chain.root], **kw)

    def test_good_chain_strict(self):
        c = Chain()
        self.assertEqual(self.verifier(c).transaction(c.sign(self.payload()))["bundleId"], BUNDLE)

    def test_bad_chains(self):
        cases = {
            "leaf without Apple OID": Chain(leaf_oid=False),
            "intermediate without Apple OID": Chain(intermediate_oid=False),
            "intermediate not a CA": Chain(intermediate_ca=False),
            "leaf is a CA": Chain(leaf_ca=True),
            "leaf key P-384": Chain(leaf_curve=ec.SECP384R1()),
            "unknown critical extension": Chain(extra_leaf_ext=x509.UnrecognizedExtension(
                x509.ObjectIdentifier("1.2.3.4.5"), b"\x05\x00")),
            "no key identifiers (strict)": Chain(key_ids=False),
            "no key usage (strict)": Chain(key_usage=False),
        }
        for name, c in cases.items():
            with self.assertRaises(av.VerificationError, msg=name):
                token = c.sign(self.payload()) if name != "leaf key P-384" else c.sign(
                    self.payload(), key=Chain().leaf_key)
                self.verifier(c).transaction(token)

    def test_wrong_root(self):
        c, other = Chain(), Chain(name="Other")
        with self.assertRaises(av.VerificationError):
            av.Verifier(BUNDLE, APP_ID, roots=[other.root]).transaction(c.sign(self.payload()))

    def test_signed_by_wrong_key(self):
        c = Chain()
        with self.assertRaises(av.VerificationError):
            self.verifier(c).transaction(c.sign(self.payload(), key=ec.generate_private_key(ec.SECP256R1())))

    def test_der_signature_refused(self):
        c = Chain()
        with self.assertRaises(av.VerificationError):
            self.verifier(c).transaction(c.sign(self.payload(), der_signature=True))

    def test_non_canonical_signature_refused(self):
        # r || 0x00 || s is the same numbers in 65 bytes; only exact 64 bytes is ES256.
        c = Chain()
        head, body, sig = c.sign(self.payload()).split(".")
        raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        padded = b64url(raw[:32] + b"\x00" + raw[32:])
        with self.assertRaises(av.VerificationError):
            self.verifier(c).transaction(head + "." + body + "." + padded)

    def test_header_alg_is_checked_even_when_es256_signature_is_valid(self):
        # Really signed with ES256 by a good chain, but the header claims another
        # algorithm: refused, so nothing downstream can be misled by the header.
        c = Chain()
        for alg in ("none", "ES512", "es256", None):
            with self.assertRaises(av.VerificationError):
                self.verifier(c).transaction(c.sign(self.payload(), alg=alg))

    def test_validity_is_checked_at_signed_date(self):
        now = datetime.datetime.now(UTC)
        # Leaf valid last year only: a payload signed then still verifies today,
        # as with Apple's library when online checks are off.
        old = Chain(leaf_not_before=now - datetime.timedelta(days=380),
                    leaf_not_after=now - datetime.timedelta(days=200))
        signed = int((now - datetime.timedelta(days=300)).timestamp() * 1000)
        self.verifier(old).transaction(old.sign(self.payload(signedDate=signed)))
        # Signed after the leaf expired: refused.
        with self.assertRaises(av.VerificationError):
            self.verifier(old).transaction(old.sign(self.payload()))
        # No signedDate: checked now, refused.
        p = self.payload()
        del p["signedDate"]
        with self.assertRaises(av.VerificationError):
            self.verifier(old).transaction(old.sign(p))

    def test_bad_signed_date_and_headers(self):
        c = Chain()
        v = self.verifier(c)
        for bad in (self.payload(signedDate="1700000000000"), self.payload(signedDate=True),
                    self.payload(signedDate=0), self.payload(signedDate=-5),
                    self.payload(signedDate=10 ** 30), self.payload(signedDate=av.MAX_MS + 1)):
            with self.assertRaises(av.VerificationError) as ctx:
                v.transaction(c.sign(bad))
            self.assertEqual(ctx.exception.status, av.VERIFICATION_FAILURE)
        with self.assertRaises(av.VerificationError):
            v.transaction(c.sign(self.payload(), header={"crit": ["b64"], "b64": False}))
        # Duplicate keys in the payload could be read two ways: refused.
        token = c.sign(self.payload())
        head, _, _ = token.split(".")
        body = b64url(b'{"bundleId":"x","bundleId":"app.orenth.ios","environment":"Sandbox"}')
        signing_input = head + "." + body
        r, s = decode_dss_signature(c.leaf_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        with self.assertRaises(av.VerificationError):
            v.transaction(signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big")))

    def test_unreadable_input_is_only_ever_a_verification_error(self):
        # Parsed before any signature is checked, so anyone can send these.
        c = Chain()
        v = self.verifier(c)
        deep = b64url(b"[" * 100000 + b"]" * 100000)
        head = c.sign(self.payload()).split(".")[0]
        for token in (head + "." + deep + ".AAAA", deep + "." + deep + ".AAAA",
                      head + "." + b64url(b"\xff\xfe") + ".AAAA",
                      c.sign(self.payload(), x5c=["", "", ""]),
                      c.sign(self.payload(), x5c="not a list"),
                      c.sign(self.payload(), x5c=[1, 2, 3]),
                      "x" * (av.MAX_JWS_LENGTH + 1)):
            with self.assertRaises(av.VerificationError):
                v.transaction(token)

    def test_environment_rules(self):
        c = Chain()
        v = self.verifier(c)
        self.assertEqual(v.transaction(c.sign(self.payload(environment="Production")))["environment"], "Production")
        for env in ("Xcode", "LocalTesting", None, "sandbox"):
            with self.assertRaises(av.VerificationError) as ctx:
                v.transaction(c.sign(self.payload(environment=env)))
            self.assertEqual(ctx.exception.status, av.INVALID_ENVIRONMENT, env)

    def test_notification_checks_nested_data(self):
        c, other = Chain(), Chain(name="Other")
        v = self.verifier(c)
        now = int(time.time() * 1000)
        tx = {"bundleId": BUNDLE, "environment": "Production", "signedDate": now,
              "transactionId": "2", "originalTransactionId": "1"}
        base = {"notificationType": "DID_RENEW", "notificationUUID": "u-1", "signedDate": now, "version": "2.0",
                "data": {"bundleId": BUNDLE, "appAppleId": APP_ID, "environment": "Production",
                         "signedTransactionInfo": c.sign(tx), "status": 1}}
        self.assertEqual(v.notification(c.sign(base))["transaction"]["transactionId"], "2")
        # Production notification with the wrong app id, or none.
        for app_id in (APP_ID + 1, None, str(APP_ID), True):
            bad = json.loads(json.dumps(base))
            bad["data"]["appAppleId"] = app_id
            with self.assertRaises(av.VerificationError) as ctx:
                v.notification(c.sign(bad))
            self.assertEqual(ctx.exception.status, av.INVALID_APP_IDENTIFIER)
        # Nested transaction signed under another root.
        bad = json.loads(json.dumps(base))
        bad["data"]["signedTransactionInfo"] = other.sign(tx)
        with self.assertRaises(av.VerificationError):
            v.notification(c.sign(bad))
        # Nested transaction for another app.
        bad = json.loads(json.dumps(base))
        bad["data"]["signedTransactionInfo"] = c.sign(dict(tx, bundleId="com.other"))
        with self.assertRaises(av.VerificationError) as ctx:
            v.notification(c.sign(bad))
        self.assertEqual(ctx.exception.status, av.INVALID_APP_IDENTIFIER)
        # Nested transaction from the other environment.
        bad = json.loads(json.dumps(base))
        bad["data"]["signedTransactionInfo"] = c.sign(dict(tx, environment="Sandbox"))
        with self.assertRaises(av.VerificationError) as ctx:
            v.notification(c.sign(bad))
        self.assertEqual(ctx.exception.status, av.INVALID_ENVIRONMENT)
        # A notification with no UUID, type or date.
        for key in ("notificationUUID", "notificationType", "signedDate"):
            bad = json.loads(json.dumps(base))
            del bad[key]
            with self.assertRaises(av.VerificationError):
                v.notification(c.sign(bad))
        # Sandbox notifications usually carry no appAppleId: accepted.
        sb = json.loads(json.dumps(base))
        sb["data"].pop("appAppleId")
        sb["data"]["environment"] = "Sandbox"
        sb["data"]["signedTransactionInfo"] = c.sign(dict(tx, environment="Sandbox"))
        self.assertEqual(v.notification(c.sign(sb))["environment"], "Sandbox")

    def test_production_needs_app_apple_id(self):
        with self.assertRaises(ValueError):
            av.Verifier(BUNDLE, None)


class JSONNesting(unittest.TestCase):
    """bot/safejson.py: JSON that anyone can send is measured before json.loads
    reads it, so nesting too deep for Python is refused as a ValueError, like
    any other text that is not JSON, and never becomes a RecursionError."""

    def test_the_limit(self):
        limit = safejson.MAX_DEPTH
        deepest = "[" * limit + "]" * limit
        self.assertEqual(safejson.loads(deepest), json.loads(deepest))
        self.assertTrue(issubclass(safejson.TooDeep, ValueError))
        for deep in ("[" + deepest + "]", "[" * 3000 + "]" * 3000,
                     '{"a":' * 3000 + "1" + "}" * 3000,
                     "[" * 100000 + "]" * 100000, "[" * 5000 + '"'):
            with self.assertRaises(safejson.TooDeep):
                safejson.loads(deep)
            with self.assertRaises(safejson.TooDeep):
                safejson.loads(deep.encode("utf-8"))

    def test_brackets_in_strings_are_text(self):
        for value in (["[" * 5000], {"{" * 3000: "]" * 3000},
                      ['"' + "[" * 500, "x\\", [[1]] * 200],
                      ["\\" + "[" * 300, '\\"' * 200 + "{" * 200],
                      [[["é[" * 100]]]):
            text = json.dumps(value)
            self.assertEqual(safejson.loads(text), value)
            self.assertEqual(safejson.loads(text.encode("utf-8")), value)

    def test_bytes_are_read_as_json_reads_them(self):
        # json.loads takes UTF-16 and UTF-32 too; brackets are measured in the
        # text it would read, not in the bytes.
        shallow = json.dumps({"jws": ["a.b.c", "[" * 100]})
        deep = "[" * 3000 + "]" * 3000
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                         "utf-32", "utf-32-le", "utf-32-be"):
            self.assertEqual(safejson.loads(shallow.encode(encoding)), json.loads(shallow), encoding)
            with self.assertRaises(safejson.TooDeep, msg=encoding):
                safejson.loads(deep.encode(encoding))

    def test_what_is_not_json_stays_a_value_error(self):
        for junk in ('"' + "[" * 5000,                     # a string that never ends
                     "]" * 5000 + "[" * 5000 + "]" * 5000,
                     "{" * 40 + "nope", "", b"\xff\xfe\x00", "﻿[]"):
            with self.assertRaises(ValueError):
                safejson.loads(junk)

    def test_options_reach_json_loads(self):
        self.assertEqual(safejson.loads('{"a": 1, "a": 2}', object_pairs_hook=list),
                         [("a", 1), ("a", 2)])


# --- who has Pro ---------------------------------------------------------------------

STORE_CHAIN = Chain(name="Store")


def fresh_db():
    """An in-memory database with the server's whole schema, Apple's included."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._prepare(conn)
    return conn


class StoreBase(unittest.TestCase):
    chain = STORE_CHAIN

    def setUp(self):
        for key in APPLE_SETTINGS:
            _os.environ.pop(key, None)
        self.addCleanup(lambda: [_os.environ.pop(k, None) for k in APPLE_SETTINGS])
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        self.ann = A.upsert_email_user(self.conn, "ann@example.com").id
        self.ben = A.upsert_email_user(self.conn, "ben@example.com").id
        self.v = av.Verifier(BUNDLE, APP_ID, roots=[self.chain.root])
        self.t0 = now_ms() - 2 * DAY
        self.ann_token = st.account_token(self.conn, self.ann)
        self.ben_token = st.account_token(self.conn, self.ben)

    # --- signed things -------------------------------------------------------
    def tx(self, txid="2000", otid="1000", product=MONTHLY, purchase=None, expires=None,
           token="ann", env="Production", signed=None, **extra):
        """A JWSTransaction. signed is Apple's signedDate for this copy."""
        purchase = self.t0 if purchase is None else purchase
        payload = {"transactionId": txid, "originalTransactionId": otid, "productId": product,
                   "bundleId": BUNDLE, "environment": env, "type": "Auto-Renewable Subscription",
                   "purchaseDate": purchase, "expiresDate": purchase + 30 * DAY if expires is None else expires,
                   "signedDate": now_ms() if signed is None else signed, "inAppOwnershipType": "PURCHASED"}
        if token:
            payload["appAccountToken"] = {"ann": self.ann_token, "ben": self.ben_token}.get(token, token)
        payload.update(extra)
        return self.chain.sign(payload)

    def note(self, ntype, tx, subtype=None, status=1, signed=None, renewal=None, env="Production", uid=None):
        """A V2 notification around a transaction. signed is the notification's signedDate."""
        signed = now_ms() if signed is None else signed
        data = {"bundleId": BUNDLE, "environment": env, "signedTransactionInfo": tx, "status": status}
        if env == "Production":
            data["appAppleId"] = APP_ID
        if renewal is not None:
            renewal = dict({"environment": env, "signedDate": signed, "originalTransactionId": "1000",
                            "autoRenewStatus": 1, "productId": MONTHLY}, **renewal)
            data["signedRenewalInfo"] = self.chain.sign(renewal)
        payload = {"notificationType": ntype, "notificationUUID": uid or str(uuid.uuid4()),
                   "signedDate": signed, "version": "2.0", "data": data}
        if subtype:
            payload["subtype"] = subtype
        return self.chain.sign(payload)

    def at(self, ntype, when, subtype=None, status=1, renewal=None, **tx_kw):
        """A notification and its transaction, both signed at the same moment."""
        return self.note(ntype, self.tx(signed=when, **tx_kw), subtype, status, when, renewal)

    # --- shortcuts -----------------------------------------------------------
    def send(self, signed_payload):
        return st.handle_notification(self.conn, self.v, signed_payload, PRODUCTS)

    def app(self, user, *jws):
        return st.handle_app_transactions(self.conn, self.v, user, list(jws), PRODUCTS)

    def pro(self, user):
        return st.pro_until_ms(self.conn, user) > now_ms()

    def user(self, user_id):
        return A.get_user(self.conn, user_id)


class Tokens(StoreBase):
    def test_token_is_random_stable_and_per_person(self):
        self.assertEqual(st.account_token(self.conn, self.ann), self.ann_token)
        self.assertNotEqual(self.ann_token, self.ben_token)
        self.assertEqual(uuid.UUID(self.ann_token).version, 4)
        self.assertEqual(self.ann_token, self.ann_token.lower())

    def test_upper_case_token_from_storekit_matches(self):
        self.app(self.ben, self.tx(token=self.ann_token.upper()))
        self.assertTrue(self.pro(self.ann))      # the token decides, not who sent it
        self.assertFalse(self.pro(self.ben))


class ProOnTheUserRow(StoreBase):
    """users.apple_until follows every change, so is_pro() needs nothing else."""

    def test_apple_until_follows_purchase_and_refund(self):
        os_keys = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
                   "STRIPE_WEBHOOK_SECRET": "whsec_x"}
        _os.environ.update(os_keys)
        self.addCleanup(lambda: [_os.environ.pop(k, None) for k in os_keys])
        self.assertFalse(billing.is_pro(self.user(self.ann)))
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        ann = self.user(self.ann)
        self.assertEqual(ann.apple_until, (self.t0 + 30 * DAY + 999) // 1000)
        self.assertTrue(billing.is_pro(ann))
        self.assertEqual(ann.plan, "free")              # Stripe's plan is untouched
        self.assertFalse(billing.is_pro(self.user(self.ben)))
        refund_at = now_ms() - 1000
        self.send(self.at("REFUND", refund_at, status=5, revocationDate=refund_at))
        self.assertEqual(self.user(self.ann).apple_until, 0)
        self.assertFalse(billing.is_pro(self.user(self.ann)))

    def test_a_past_apple_until_is_not_pro(self):
        conn = self.conn
        conn.execute("UPDATE users SET apple_until=? WHERE id=?", (int(time.time()) - 5, self.ann))
        os_keys = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
                   "STRIPE_WEBHOOK_SECRET": "whsec_x"}
        _os.environ.update(os_keys)
        self.addCleanup(lambda: [_os.environ.pop(k, None) for k in os_keys])
        self.assertFalse(billing.is_pro(self.user(self.ann)))
        conn.execute("UPDATE users SET apple_until=? WHERE id=?", (int(time.time()) + 60, self.ann))
        self.assertTrue(billing.is_pro(self.user(self.ann)))


class Lifecycle(StoreBase):
    def test_subscribe_and_renew(self):
        self.assertEqual(self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY")), (200, st.APPLIED))
        self.assertTrue(self.pro(self.ann))
        self.assertFalse(self.pro(self.ben))
        renew_at = self.t0 + 30 * DAY
        self.send(self.at("DID_RENEW", renew_at, txid="2001", purchase=renew_at))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), renew_at + 30 * DAY)

    def test_expired_ends_pro(self):
        purchase = now_ms() - 40 * DAY
        self.send(self.at("SUBSCRIBED", purchase, "INITIAL_BUY", purchase=purchase))
        self.assertFalse(self.pro(self.ann))            # that period already ended
        fresh = now_ms() - DAY
        self.send(self.at("DID_RENEW", fresh, txid="2001", purchase=fresh))
        self.assertTrue(self.pro(self.ann))
        self.send(self.at("EXPIRED", now_ms(), "VOLUNTARY", status=2, txid="2001", purchase=fresh,
                          expires=now_ms() - 1000))
        self.assertFalse(self.pro(self.ann))
        self.assertEqual(self.user(self.ann).apple_until, (now_ms() - 1000 + 999) // 1000)

    def test_duplicate_notification_applies_once(self):
        payload = self.note("SUBSCRIBED", self.tx(), "INITIAL_BUY", uid="same-uuid")
        self.assertEqual(self.send(payload), (200, st.APPLIED))
        self.assertEqual(self.send(payload), (200, st.DUPLICATE))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apple_notifications").fetchone()[0], 1)

    def test_out_of_order_delivery(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        refund_at = now_ms() - 10000
        self.send(self.at("REFUND", refund_at, status=5, revocationDate=refund_at, revocationReason=0))
        self.assertFalse(self.pro(self.ann))
        # A retry of an older notification arrives after the refund: no effect.
        late = self.at("DID_CHANGE_RENEWAL_STATUS", self.t0 + 5, "AUTO_RENEW_ENABLED")
        self.assertEqual(self.send(late), (200, st.STALE))
        self.assertFalse(self.pro(self.ann))
        # An old copy from the app, signed before the refund: no effect either.
        self.assertEqual(self.app(self.ann, self.tx(signed=self.t0 + 5))[0]["outcome"], st.STALE)
        self.assertFalse(self.pro(self.ann))

    def test_old_transaction_cannot_block_a_late_refund(self):
        # The server was down when the refund happened. Before Apple's retry
        # arrives, the app sends an earlier renewal, freshly signed. It must not
        # move anything forward, or the refund would later look stale.
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY", txid="2001"))
        refund_at = now_ms() - 10000
        refund = self.at("REFUND", refund_at, status=5, txid="2001", revocationDate=refund_at)
        older = self.tx(txid="2000", purchase=self.t0 - 30 * DAY, signed=now_ms())
        self.assertEqual(self.app(self.ann, older)[0]["outcome"], st.STALE)
        self.assertEqual(self.send(refund), (200, st.APPLIED))
        self.assertFalse(self.pro(self.ann))

    def test_a_renewal_is_not_turned_away_by_a_fresh_copy_of_the_old_period(self):
        # The app sends this period's transaction, signed just now. Then Apple's
        # renewal notification arrives, signed earlier (a retry): it is a newer
        # purchase, so it moves Pro forward whatever its signature's date.
        purchase = now_ms() - 29 * DAY
        self.send(self.at("SUBSCRIBED", purchase, "INITIAL_BUY", purchase=purchase))
        renew_at = purchase + 29 * DAY - 3600 * 1000
        renewal = self.at("DID_RENEW", renew_at, txid="2001", purchase=renew_at)
        self.app(self.ann, self.tx(purchase=purchase, signed=now_ms()))
        self.assertEqual(self.send(renewal), (200, st.APPLIED))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), renew_at + 30 * DAY)
        # And a copy of the renewal signed before the stored one changes nothing.
        self.assertEqual(self.app(self.ann, self.tx(txid="2001", purchase=renew_at,
                                                    signed=renew_at - 1))[0]["outcome"], st.STALE)

    def test_refund_reversed(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.send(self.at("REFUND", now_ms() - 2000, status=5, revocationDate=now_ms() - 2000))
        self.assertFalse(self.pro(self.ann))
        self.send(self.at("REFUND_REVERSED", now_ms(), status=1))
        self.assertTrue(self.pro(self.ann))

    def test_a_refunded_renewal_stays_refunded(self):
        # Pro for the old period does not come back with the new one refunded.
        self.send(self.at("SUBSCRIBED", self.t0 - 30 * DAY, "INITIAL_BUY", purchase=self.t0 - 30 * DAY))
        self.send(self.at("DID_RENEW", self.t0, txid="2001", purchase=self.t0))
        refund_at = now_ms() - 1000
        self.send(self.at("REFUND", refund_at, status=5, txid="2001", purchase=self.t0,
                          revocationDate=refund_at))
        self.assertFalse(self.pro(self.ann))
        old = self.tx(txid="2000", purchase=self.t0 - 30 * DAY, expires=now_ms() + 5 * DAY)
        self.assertEqual(self.app(self.ann, old)[0]["outcome"], st.STALE)
        self.assertFalse(self.pro(self.ann))

    def test_revoke(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.send(self.at("REVOKE", now_ms(), status=5, revocationDate=now_ms()))
        self.assertFalse(self.pro(self.ann))

    def test_grace_period(self):
        purchase = now_ms() - 31 * DAY
        self.send(self.at("SUBSCRIBED", purchase, "INITIAL_BUY", purchase=purchase))
        self.assertFalse(self.pro(self.ann))
        grace_end = now_ms() + 15 * DAY
        self.send(self.at("DID_FAIL_TO_RENEW", now_ms() - 3000, "GRACE_PERIOD", status=4, purchase=purchase,
                          renewal={"isInBillingRetryPeriod": True, "gracePeriodExpiresDate": grace_end}))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), grace_end)
        self.send(self.at("GRACE_PERIOD_EXPIRED", now_ms() - 2000, status=3, purchase=purchase,
                          renewal={"isInBillingRetryPeriod": True}))
        self.assertFalse(self.pro(self.ann))
        recovered = now_ms() - 1000
        self.send(self.at("DID_RENEW", recovered, "BILLING_RECOVERY", txid="2001", purchase=recovered))
        self.assertTrue(self.pro(self.ann))

    def test_grace_date_counts_only_while_status_says_grace(self):
        # Renewal info can still name a grace end after grace is over; only
        # status 4 (Billing Grace Period) extends Pro.
        purchase = now_ms() - 31 * DAY
        self.send(self.at("SUBSCRIBED", purchase, "INITIAL_BUY", purchase=purchase))
        self.send(self.at("EXPIRED", now_ms(), "BILLING_RETRY", status=2, purchase=purchase,
                          renewal={"gracePeriodExpiresDate": now_ms() + 5 * DAY, "autoRenewStatus": 0}))
        self.assertFalse(self.pro(self.ann))

    def test_billing_retry_without_grace(self):
        purchase = now_ms() - 31 * DAY
        self.send(self.at("SUBSCRIBED", purchase, "INITIAL_BUY", purchase=purchase))
        self.send(self.at("DID_FAIL_TO_RENEW", now_ms(), status=3, purchase=purchase,
                          renewal={"isInBillingRetryPeriod": True}))
        self.assertFalse(self.pro(self.ann))
        # Apple may still charge while it retries, so the deletion warning knows.
        self.assertIsNone(st.subscription(self.conn, self.ann))
        found = st.subscription(self.conn, self.ann, counted_only=False)
        self.assertTrue(found and found["retrying"] and found["renews"])

    def test_cancel_keeps_pro_to_end_of_period(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.send(self.at("DID_CHANGE_RENEWAL_STATUS", now_ms(), "AUTO_RENEW_DISABLED",
                          renewal={"autoRenewStatus": 0}))
        self.assertTrue(self.pro(self.ann))
        self.assertEqual(self.conn.execute("SELECT auto_renew FROM apple_subs").fetchone()["auto_renew"], 0)
        self.assertFalse(st.subscription(self.conn, self.ann)["renews"])

    def test_renewal_extended(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.send(self.at("RENEWAL_EXTENDED", now_ms(), expires=self.t0 + 45 * DAY))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), self.t0 + 45 * DAY)

    def test_monthly_to_yearly_at_the_next_renewal(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.send(self.at("DID_CHANGE_RENEWAL_PREF", now_ms() - 5000, "UPGRADE",
                          renewal={"autoRenewProductId": YEARLY}))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), self.t0 + 30 * DAY)
        renew_at = self.t0 + 30 * DAY
        self.send(self.at("DID_RENEW", now_ms(), txid="2001", product=YEARLY, purchase=renew_at,
                          expires=renew_at + 365 * DAY))
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), renew_at + 365 * DAY)
        self.assertEqual(self.conn.execute("SELECT product_id FROM apple_subs").fetchone()[0], YEARLY)

    def test_upgraded_and_older_transactions_do_not_shorten(self):
        self.send(self.at("SUBSCRIBED", self.t0 + 10, "INITIAL_BUY", txid="2005", purchase=self.t0 + 10))
        until = st.pro_until_ms(self.conn, self.ann)
        self.assertEqual(self.app(self.ann, self.tx(txid="2004", purchase=self.t0 - 30 * DAY,
                                                    expires=self.t0))[0]["outcome"], st.STALE)
        self.assertEqual(self.app(self.ann, self.tx(txid="2005", purchase=self.t0 + 10, expires=self.t0 + 11,
                                                    isUpgraded=True))[0]["outcome"], st.STALE)
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), until)
        # A first sight of an upgraded transaction is not a subscription.
        self.assertEqual(self.app(self.ann, self.tx(otid="7000", txid="7001", isUpgraded=True))[0]["outcome"],
                         st.IGNORED)

    def test_types_that_change_nothing(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        before = st.pro_until_ms(self.conn, self.ann)
        for ntype, sub in (("PRICE_INCREASE", "ACCEPTED"), ("DID_CHANGE_RENEWAL_PREF", "DOWNGRADE"),
                           ("REFUND_DECLINED", None), ("CONSUMPTION_REQUEST", None), ("OFFER_REDEEMED", None),
                           ("RENEWAL_EXTENSION", "SUMMARY"), ("METADATA_UPDATE", None)):
            self.assertEqual(self.send(self.at(ntype, now_ms(), sub))[0], 200)
        self.assertEqual(st.pro_until_ms(self.conn, self.ann), before)

    def test_test_notification_and_bad_input(self):
        test_note = self.chain.sign({"notificationType": "TEST", "notificationUUID": "t-1", "signedDate": now_ms(),
                                     "data": {"bundleId": BUNDLE, "environment": "Production",
                                              "appAppleId": APP_ID}})
        self.assertEqual(self.send(test_note), (200, st.IGNORED))
        self.assertEqual(self.send("not.a.jws")[0], 400)
        forger = Chain(name="Forger")
        forged = forger.sign({"notificationType": "SUBSCRIBED", "notificationUUID": "f-1", "signedDate": now_ms(),
                              "data": {"bundleId": BUNDLE, "environment": "Production", "appAppleId": APP_ID,
                                       "signedTransactionInfo": forger.sign({})}})
        self.assertEqual(self.send(forged), (400, av.VERIFICATION_FAILURE))
        self.assertFalse(self.pro(self.ann))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apple_notifications "
                                           "WHERE uuid='f-1'").fetchone()[0], 0)

    def test_unknown_product_ignored(self):
        self.assertEqual(self.send(self.at("SUBSCRIBED", now_ms(), product="app.orenth.ios.coins")),
                         (200, st.IGNORED))
        self.assertFalse(self.pro(self.ann))
        self.assertEqual(self.app(self.ann, self.tx(product="app.orenth.ios.coins"))[0]["outcome"], st.IGNORED)
        self.assertEqual(self.app(self.ann, self.tx(type="Consumable"))[0]["outcome"], st.IGNORED)

    def test_a_forged_or_foreign_transaction_from_the_app_is_invalid(self):
        forger = Chain(name="Forger")
        forged = forger.sign({"transactionId": "1", "originalTransactionId": "1", "productId": MONTHLY,
                              "bundleId": BUNDLE, "environment": "Production",
                              "type": "Auto-Renewable Subscription", "purchaseDate": now_ms(),
                              "expiresDate": now_ms() + 30 * DAY, "signedDate": now_ms(),
                              "appAccountToken": self.ann_token})
        out = self.app(self.ann, forged, self.tx(bundleId="com.other.app"), "x.y.z")
        self.assertEqual([r["outcome"] for r in out], [st.INVALID] * 3)
        self.assertFalse(self.pro(self.ann))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apple_subs").fetchone()[0], 0)


class Ownership(StoreBase):
    def test_notification_before_app_call_links_by_token(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY"))
        self.assertTrue(self.pro(self.ann))
        self.assertEqual(self.app(self.ann, self.tx())[0]["outcome"], st.APPLIED)

    def test_no_token_links_to_the_person_who_sends_it(self):
        # e.g. an offer code redeemed in the App Store: no appAccountToken.
        self.assertEqual(self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY", token=None)), (200, st.UNOWNED))
        self.assertFalse(self.pro(self.ben))
        self.assertEqual(self.app(self.ben, self.tx(token=None))[0]["outcome"], st.APPLIED)
        self.assertTrue(self.pro(self.ben))
        # Later, a different account signed in on the same phone cannot take it.
        self.assertEqual(self.app(self.ann, self.tx(token=None))[0]["outcome"], st.OTHER_ACCOUNT)
        self.assertFalse(self.pro(self.ann))

    def test_a_token_nobody_has_does_not_give_it_to_the_sender_over_its_owner(self):
        # A token that is not any account's (made up, or a deleted account's):
        # it belongs to whoever sends it, as with no token.
        stray = str(uuid.uuid4())
        self.assertEqual(self.app(self.ben, self.tx(token=stray))[0]["outcome"], st.APPLIED)
        self.assertTrue(self.pro(self.ben))

    def test_someone_elses_token(self):
        # Ann bought on this Apple ID. Ben signs in on the same phone and taps
        # Subscribe: StoreKit hands back the existing transaction, with Ann's token.
        self.app(self.ann, self.tx())
        self.assertEqual(self.app(self.ben, self.tx())[0]["outcome"], st.OTHER_ACCOUNT)
        self.assertTrue(self.pro(self.ann))
        self.assertFalse(self.pro(self.ben))
        # Ben asks, on purpose, to move it here.
        moved = st.move_to(self.conn, self.v, self.ben, self.tx(), PRODUCTS)
        self.assertEqual(moved, {"outcome": st.MOVED, "previous": self.ann})
        self.assertTrue(self.pro(self.ben))
        self.assertFalse(self.pro(self.ann))
        self.assertTrue(billing.is_pro(self.user(self.ben)) or not billing.ready())
        self.assertEqual(self.user(self.ann).apple_until, 0)
        row = self.conn.execute("SELECT user_id, moved_from, moved_ms FROM apple_subs").fetchone()
        self.assertEqual((row["user_id"], row["moved_from"]), (self.ben, self.ann))
        self.assertGreater(row["moved_ms"], 0)
        # Renewals stay with Ben, though they still carry Ann's token.
        renew_at = self.t0 + 30 * DAY
        self.send(self.at("DID_RENEW", renew_at, txid="2001", purchase=renew_at))
        self.assertTrue(self.pro(self.ben))
        self.assertFalse(self.pro(self.ann))

    def test_moving_back_and_forth_is_limited(self):
        self.app(self.ann, self.tx())
        self.assertEqual(st.move_to(self.conn, self.v, self.ben, self.tx(), PRODUCTS)["outcome"], st.MOVED)
        again = st.move_to(self.conn, self.v, self.ann, self.tx(), PRODUCTS)
        self.assertEqual(again["outcome"], st.TOO_SOON)
        self.assertGreater(again["after"], time.time() + 29 * 86400)
        self.assertTrue(self.pro(self.ben))
        self.assertEqual(st.move_to(self.conn, self.v, self.ben, self.tx(), PRODUCTS)["outcome"],
                         st.ALREADY_HERE)
        # A month on, it may move again.
        self.conn.execute("UPDATE apple_subs SET moved_ms=?", (now_ms() - 31 * DAY,))
        self.conn.commit()
        self.assertEqual(st.move_to(self.conn, self.v, self.ann, self.tx(), PRODUCTS)["outcome"], st.MOVED)
        self.assertTrue(self.pro(self.ann))

    def test_only_the_running_transaction_can_move_it(self):
        # An old, ended period, or a refunded one, moves nothing: what moves a
        # subscription is what the phone holds now.
        old = self.tx(txid="1999", purchase=self.t0 - 30 * DAY)
        self.app(self.ann, self.tx(signed=self.t0 + 1000))
        self.assertEqual(st.move_to(self.conn, self.v, self.ben, old, PRODUCTS)["outcome"], st.NOT_CURRENT)
        ended = self.tx(otid="5000", txid="5001", purchase=now_ms() - 40 * DAY, token="ann")
        self.app(self.ann, ended)
        self.assertEqual(st.move_to(self.conn, self.v, self.ben, ended, PRODUCTS)["outcome"], st.NOT_CURRENT)
        refund_at = now_ms() - 1000
        self.send(self.at("REFUND", refund_at, status=5, revocationDate=refund_at))
        # A copy from before the refund, and Apple's own copy since, which
        # names the refund: neither moves it.
        for copy in (self.tx(signed=refund_at - 5000), self.tx(revocationDate=refund_at)):
            self.assertEqual(st.move_to(self.conn, self.v, self.ben, copy, PRODUCTS)["outcome"],
                             st.NOT_CURRENT)
        self.assertFalse(self.pro(self.ben))
        self.assertFalse(self.pro(self.ann))
        with self.assertRaises(av.VerificationError):
            st.move_to(self.conn, self.v, self.ben, Chain(name="Forger").sign({}), PRODUCTS)
        self.assertEqual(st.move_to(self.conn, self.v, self.ben, self.tx(product="other.product"),
                                    PRODUCTS)["outcome"], st.IGNORED)

    def test_moving_an_unowned_subscription_claims_it(self):
        self.send(self.at("SUBSCRIBED", self.t0, "INITIAL_BUY", token=None))
        moved = st.move_to(self.conn, self.v, self.ben, self.tx(token=None), PRODUCTS)
        self.assertEqual(moved["outcome"], st.ALREADY_HERE)
        self.assertTrue(self.pro(self.ben))

    def test_deleted_account(self):
        self.app(self.ann, self.tx())
        A.delete_user(self.conn, self.ann)
        # The subscription is linked to nobody, and the token is gone. The row
        # stays while its time runs (test_a_refund_outlives_the_account).
        rows = self.conn.execute("SELECT user_id, app_token, moved_from FROM apple_subs").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(None, "", None)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apple_tokens WHERE user_id=?",
                                           (self.ann,)).fetchone()[0], 0)
        # Apple keeps renewing until the person cancels; the renewal has no owner now.
        renew = self.t0 + DAY
        self.assertEqual(self.send(self.at("DID_RENEW", renew, txid="2001", purchase=renew))[1], st.UNOWNED)
        # The same person makes a new account and taps Restore: it follows them.
        self.assertEqual(self.app(self.ben, self.tx(txid="2001", purchase=renew))[0]["outcome"], st.APPLIED)
        self.assertTrue(self.pro(self.ben))

    def test_a_refund_outlives_the_account(self):
        # Ann buys a year, and her app sends the purchase, which can be copied
        # off the phone. Apple refunds it; then she deletes her account. A copy
        # signed before the refund still verifies, and sent from a new account
        # it must not give the refunded year back.
        year = self.t0 + 365 * DAY
        bought = self.tx(product=YEARLY, expires=year, signed=self.t0)
        self.app(self.ann, bought)
        refund_at = now_ms() - 1000
        self.send(self.at("REFUND", refund_at, status=5, product=YEARLY, expires=year,
                          revocationDate=refund_at))
        self.assertFalse(self.pro(self.ann))
        A.delete_user(self.conn, self.ann)
        again = A.upsert_email_user(self.conn, "ann@example.com").id
        self.assertEqual(self.app(again, bought)[0]["outcome"], st.STALE)
        self.assertFalse(self.pro(again))
        self.assertEqual(self.user(again).apple_until, 0)
        # Nor in anyone else's.
        self.assertNotEqual(self.app(self.ben, bought)[0]["outcome"], st.APPLIED)
        self.assertFalse(self.pro(self.ben))

    def test_what_has_run_out_is_not_kept(self):
        # Once a subscription's time has run out no copy of it gives Pro, so
        # nothing about it is kept when nobody owns it.
        old = now_ms() - 400 * DAY
        self.app(self.ann, self.tx(otid="3000", txid="3001", purchase=old))
        self.app(self.ann, self.tx(otid="4000", txid="4001"))
        A.delete_user(self.conn, self.ann)
        kept = lambda: sorted(r[0] for r in self.conn.execute("SELECT original_tx_id FROM apple_subs"))
        self.assertEqual(kept(), ["4000"])
        # The running one goes once its time, grace included, is up. A row
        # someone owns stays, however old.
        self.app(self.ben, self.tx(otid="5000", txid="5001", purchase=old, token="ben"))
        self.assertEqual(st.prune_unowned(self.conn), 0)
        self.conn.execute("UPDATE apple_subs SET expires_ms=?, grace_until_ms=? WHERE original_tx_id='4000'",
                          (now_ms() - 1000, now_ms() + DAY))
        self.assertEqual(st.prune_unowned(self.conn), 0)
        self.conn.execute("UPDATE apple_subs SET grace_until_ms=0 WHERE original_tx_id='4000'")
        self.assertEqual(st.prune_unowned(self.conn), 1)
        self.assertEqual(kept(), ["5000"])

    def test_deleting_the_previous_owner_forgets_the_move(self):
        self.app(self.ann, self.tx())
        st.move_to(self.conn, self.v, self.ben, self.tx(), PRODUCTS)
        A.delete_user(self.conn, self.ann)
        row = self.conn.execute("SELECT user_id, moved_from FROM apple_subs").fetchone()
        self.assertEqual((row["user_id"], row["moved_from"]), (self.ben, None))
        self.assertTrue(self.pro(self.ben))


class Sandbox(StoreBase):
    def test_sandbox_recorded_and_counted_by_default(self):
        self.app(self.ann, self.tx(env="Sandbox"))
        self.assertEqual(self.conn.execute("SELECT environment FROM apple_subs").fetchone()["environment"],
                         "Sandbox")
        self.assertEqual(st.sandbox_policy(), "all")
        self.assertTrue(self.pro(self.ann))
        self.assertTrue(st.subscription(self.conn, self.ann)["test"])

    def test_sandbox_and_production_are_separate(self):
        self.app(self.ann, self.tx(env="Sandbox"))
        self.app(self.ann, self.tx(env="Production"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apple_subs").fetchone()[0], 2)

    def test_policy_none_and_allowlist(self):
        self.app(self.ann, self.tx(env="Sandbox"))
        self.app(self.ben, self.tx(env="Sandbox", otid="3000", txid="3001", token="ben"))
        _os.environ["APPLE_SANDBOX_PRO"] = "none"
        self.assertFalse(self.pro(self.ann))
        self.assertFalse(self.pro(self.ben))
        _os.environ["APPLE_SANDBOX_PRO"] = "Allowlist"
        _os.environ["APPLE_SANDBOX_USERS"] = "someone@example.com, BEN@example.com"
        self.assertFalse(self.pro(self.ann))
        self.assertTrue(self.pro(self.ben))
        # Production purchases count whatever the setting says.
        _os.environ["APPLE_SANDBOX_PRO"] = "none"
        self.app(self.ann, self.tx(env="Production"))
        self.assertTrue(self.pro(self.ann))

    def test_a_changed_policy_reaches_the_user_rows_at_start_up(self):
        self.app(self.ann, self.tx(env="Sandbox"))
        self.assertGreater(self.user(self.ann).apple_until, time.time())
        _os.environ["APPLE_SANDBOX_PRO"] = "none"
        st.prepare(self.conn)
        self.assertEqual(self.user(self.ann).apple_until, 0)
        _os.environ["APPLE_SANDBOX_PRO"] = "all"
        st.prepare(self.conn)
        self.assertGreater(self.user(self.ann).apple_until, time.time())

    def test_a_typo_in_the_setting_keeps_the_default(self):
        _os.environ["APPLE_SANDBOX_PRO"] = "nobody"
        self.assertEqual(st.sandbox_policy(), "all")

    def test_the_review_account_goes_back_to_free(self):
        # A reviewer bought Pro in the sandbox; the next one must find it Free.
        self.app(self.ann, self.tx(env="Sandbox"))
        self.assertTrue(self.pro(self.ann))
        import importlib
        tool = importlib.import_module("tools.reset_test_purchases")
        self.assertEqual(tool.reset(self.conn, " ANN@example.com "), 1)
        self.assertFalse(self.pro(self.ann))
        self.assertEqual(self.user(self.ann).apple_until, 0)
        # It has a new token, so the sandbox renewal does not come back to it.
        self.assertNotEqual(st.account_token(self.conn, self.ann), self.ann_token)
        renew_at = self.t0 + DAY
        self.assertEqual(self.send(self.note("DID_RENEW", self.tx(env="Sandbox", txid="2001",
                                                                 purchase=renew_at),
                                             env="Sandbox"))[1], st.UNOWNED)
        self.assertFalse(self.pro(self.ann))
        # Real purchases are never touched, and keep their token.
        self.app(self.ben, self.tx(env="Production", otid="4000", txid="4001", token="ben"))
        self.app(self.ben, self.tx(env="Sandbox", otid="4100", txid="4101", token="ben"))
        self.assertEqual(st.forget_test_purchases(self.conn, self.ben), 1)
        self.assertTrue(self.pro(self.ben))
        self.assertEqual(st.account_token(self.conn, self.ben), self.ben_token)
        with self.assertRaises(ValueError):
            tool.reset(self.conn, "nobody@example.com")


class Settings(unittest.TestCase):
    def setUp(self):
        for key in APPLE_SETTINGS:
            _os.environ.pop(key, None)
        self.addCleanup(lambda: [_os.environ.pop(k, None) for k in APPLE_SETTINGS])

    def test_off_until_the_app_id_is_set(self):
        self.assertFalse(st.enabled())
        self.assertIn("APPLE_APP_ID is not set", st.describe())
        for bad in ("abc", "-5", "0", "674 000", "6740000001x"):
            _os.environ["APPLE_APP_ID"] = bad
            self.assertFalse(st.enabled(), bad)
        _os.environ["APPLE_APP_ID"] = " 6740000001\n"
        self.assertTrue(st.enabled())
        self.assertEqual(st.app_id(), 6740000001)
        self.assertIn("on for app.orenth.ios.pro.monthly, app.orenth.ios.pro.yearly", st.describe())

    def test_products_and_bundle(self):
        self.assertEqual(st.product_ids(), PRODUCTS)
        _os.environ["APPLE_PRODUCT_IDS"] = " a.b.c , a.b.c,bad/id!, x_y-z.1 "
        self.assertEqual(st.product_ids(), ["a.b.c", "x_y-z.1"])
        self.assertEqual(st.bundle_id(), BUNDLE)
        _os.environ["APPLE_BUNDLE_ID"] = "com.example.other"
        self.assertEqual(st.bundle_id(), "com.example.other")

    def test_the_verifier_follows_the_settings(self):
        _os.environ["APPLE_APP_ID"] = str(APP_ID)
        v = st.verifier()
        self.assertEqual((v.bundle_id, v.app_apple_id, v.strict), (BUNDLE, APP_ID, True))
        self.assertIs(st.verifier(), v)
        _os.environ["APPLE_APP_ID"] = str(APP_ID + 1)
        self.assertEqual(st.verifier().app_apple_id, APP_ID + 1)


class Concurrency(unittest.TestCase):
    def test_same_notification_delivered_four_times_at_once(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        _os.remove(path)
        try:
            db.connect(path).close()          # the whole schema, as the server has it
            v = av.Verifier(BUNDLE, APP_ID, roots=[STORE_CHAIN.root])
            t0 = now_ms() - DAY
            tx = STORE_CHAIN.sign({"transactionId": "1", "originalTransactionId": "1", "productId": MONTHLY,
                                   "bundleId": BUNDLE, "environment": "Production", "signedDate": t0,
                                   "type": "Auto-Renewable Subscription", "purchaseDate": t0,
                                   "expiresDate": t0 + 30 * DAY})
            payload = STORE_CHAIN.sign({"notificationType": "SUBSCRIBED", "subtype": "INITIAL_BUY",
                                        "notificationUUID": "race", "signedDate": t0,
                                        "data": {"bundleId": BUNDLE, "environment": "Production",
                                                 "appAppleId": APP_ID, "signedTransactionInfo": tx,
                                                 "status": 1}})
            results = []

            def deliver():
                conn = db.connect(path)
                try:
                    results.append(st.handle_notification(conn, v, payload, PRODUCTS)[1])
                finally:
                    conn.close()

            threads = [threading.Thread(target=deliver) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), sorted([st.UNOWNED] + [st.DUPLICATE] * 3))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    _os.remove(path + suffix)
                except OSError:
                    pass


# --- what the site does with it ----------------------------------------------------

import harness  # noqa: E402

STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
          "STRIPE_PRICE_ID_YEARLY": "price_y", "STRIPE_WEBHOOK_SECRET": "whsec_test"}
for _key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL", "SMTP_HOST", "SMTP_USER",
                            "SMTP_PASSWORD", "MAIL_FROM"]:
    _os.environ.pop(_key, None)

CLIENT, WEB_DB = harness.isolated_web("appstore")
import web  # noqa: E402

web.app.config["AUTH_MODE"] = None
APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148 OrenthApp/1")
SAFARI_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1")
IN_APP = {"User-Agent": APP_UA}
# What WKWebView sends with a fetch from a page on this site.
FROM_PAGE = {"User-Agent": APP_UA, "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
WEB_CHAIN = Chain(name="Web")
PRICES = {"price_x": {"unit_amount": 999, "currency": "usd",
                      "recurring": {"interval": "month", "interval_count": 1}},
          "price_y": {"unit_amount": 7999, "currency": "usd",
                      "recurring": {"interval": "year", "interval_count": 1}}}
# Words that must never reach a page drawn inside the app (as test_app_mode).
NEVER_IN_APP = ("stripe", "/billing/checkout", "/billing/portal", "/billing/code",
                "discount code", "$9.99", "$79.99", "metricool")
# A price written into a page: inside the app only StoreKit may show one.
PRICE = re.compile(r"[$£€]\s?\d")


class WebBase(unittest.TestCase):
    def setUp(self):
        self.sent_to_stripe = []
        self.mails = []
        saved = {k: _os.environ.get(k) for k in APPLE_SETTINGS + tuple(STRIPE)
                 + ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}

        def put_back():
            for key, value in saved.items():
                if value is None:
                    _os.environ.pop(key, None)
                else:
                    _os.environ[key] = value
        self.addCleanup(put_back)
        for key in APPLE_SETTINGS:
            _os.environ.pop(key, None)
        _os.environ.update(STRIPE)
        _os.environ["APPLE_APP_ID"] = str(APP_ID)
        _os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
        _os.environ["GOOGLE_CLIENT_SECRET"] = "x"
        web._RATE.clear()

        real = (st.verifier, billing._stripe, web.codes_mod.email_ready, web.codes_mod.send_mail)

        def restore():
            st.verifier, billing._stripe = real[0], real[1]
            web.codes_mod.email_ready, web.codes_mod.send_mail = real[2], real[3]
        self.addCleanup(restore)
        # Apple's test root swapped in, as Apple's own tests do.
        st.verifier = lambda: av.Verifier(BUNDLE, APP_ID, roots=[WEB_CHAIN.root])
        billing._stripe = self.fake_stripe
        web.codes_mod.email_ready = lambda: True
        web.codes_mod.send_mail = lambda to, subject, body, headers=None: self.mails.append(
            (to, subject, body))

        tag = uuid.uuid4().hex[:8]
        # Each test's own subscription: the tests share one database, as the
        # server does, and a subscription keeps the owner it first had.
        self.otid = "9" + uuid.uuid4().hex[:11]
        conn = db.connect(WEB_DB)
        try:
            self.ann = A.upsert_email_user(conn, "ann-%s@example.com" % tag)
            self.ben = A.upsert_email_user(conn, "ben-%s@example.com" % tag)
        finally:
            conn.close()

    def fake_stripe(self, method, path, data=None):
        self.sent_to_stripe.append((method, path))
        if path.startswith("prices/"):
            return PRICES[path.split("/", 1)[1]]
        if path == "checkout/sessions":
            return {"url": "https://checkout.stripe.test/c/1", "id": "cs_test_1"}
        if path.startswith("promotion_codes"):
            return {"data": []}
        return {"url": "https://billing.stripe.test/p/1"}

    def client(self, user=None):
        client = web.app.test_client()
        if user is not None:
            conn = db.connect(WEB_DB)
            try:
                token = A.start_session(conn, user.id, "test")
            finally:
                conn.close()
            with client.session_transaction() as sess:
                sess[web.SESSION_KEY] = token
        return client

    def state(self, client):
        r = client.get("/api/apple/state", headers=FROM_PAGE)
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()

    def tx(self, token, txid=None, otid=None, env="Production", product=YEARLY,
           purchase=None, expires=None, signed=None, **extra):
        otid = self.otid if otid is None else otid
        txid = otid + "1" if txid is None else txid
        purchase = now_ms() - DAY if purchase is None else purchase
        payload = {"transactionId": txid, "originalTransactionId": otid, "productId": product,
                   "bundleId": BUNDLE, "environment": env, "type": "Auto-Renewable Subscription",
                   "purchaseDate": purchase,
                   "expiresDate": purchase + 365 * DAY if expires is None else expires,
                   "signedDate": now_ms() if signed is None else signed}
        if token:
            payload["appAccountToken"] = token
        payload.update(extra)
        return WEB_CHAIN.sign(payload)

    def note(self, ntype, tx, uid=None, status=1, signed=None, env="Production", renewal=None):
        data = {"bundleId": BUNDLE, "environment": env, "signedTransactionInfo": tx, "status": status}
        if env == "Production":
            data["appAppleId"] = APP_ID
        if renewal is not None:
            data["signedRenewalInfo"] = WEB_CHAIN.sign(dict({"environment": env,
                                                            "signedDate": now_ms()}, **renewal))
        return WEB_CHAIN.sign({"notificationType": ntype, "notificationUUID": uid or str(uuid.uuid4()),
                               "signedDate": now_ms() if signed is None else signed, "version": "2.0",
                               "data": data})

    def post(self, client, path, body, headers=FROM_PAGE):
        return client.post(path, json=body, headers=headers)

    def apple_until(self, user):
        conn = db.connect(WEB_DB)
        try:
            return A.get_user(conn, user.id).apple_until
        finally:
            conn.close()

    def text(self, response):
        return response.data.decode("utf-8", "replace")

    def leaks(self, body):
        low = body.lower()
        return [w for w in NEVER_IN_APP if w in low]


class Routes(WebBase):
    def test_off_until_the_app_id_is_set(self):
        _os.environ.pop("APPLE_APP_ID")
        client = self.client(self.ann)
        self.assertEqual(CLIENT.post("/apple/notifications", json={"signedPayload": "a.b.c"}).status_code, 404)
        state = self.state(client)
        self.assertEqual((state["enabled"], state["appAccountToken"], state["products"]), (False, "", []))
        r = self.post(client, "/api/apple/transactions", {"jws": [self.tx(None)]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.post(client, "/api/apple/move", {"jws": "x"}).status_code, 404)

    def test_needs_sign_in(self):
        anon = self.client()
        self.assertEqual(anon.get("/api/apple/state").status_code, 401)
        self.assertEqual(self.post(anon, "/api/apple/transactions", {"jws": []}).status_code, 401)
        self.assertEqual(self.post(anon, "/api/apple/move", {"jws": "x"}).status_code, 401)

    def test_state_mints_one_token_and_is_never_cached(self):
        client = self.client(self.ann)
        r = client.get("/api/apple/state", headers=FROM_PAGE)
        state = r.get_json()
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual((state["enabled"], state["pro"], state["source"]), (True, False, ""))
        self.assertEqual(uuid.UUID(state["appAccountToken"]).version, 4)
        self.assertEqual(state["products"], PRODUCTS)
        self.assertEqual(self.state(client)["appAccountToken"], state["appAccountToken"])
        self.assertNotEqual(self.state(self.client(self.ben))["appAccountToken"], state["appAccountToken"])
        cross = client.get("/api/apple/state", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross.status_code, 403)

    def test_purchase_from_the_app(self):
        client = self.client(self.ann)
        token = self.state(client)["appAccountToken"]
        r = self.post(client, "/api/apple/transactions", {"jws": [self.tx(token.upper())]})
        out = r.get_json()
        self.assertEqual(r.status_code, 200, out)
        self.assertEqual((out["pro"], out["source"]), (True, "apple"))
        self.assertEqual(out["results"], [{"outcome": "applied", "transactionId": self.otid + "1",
                                           "environment": "Production"}])
        self.assertGreater(out["appleUntil"], now_ms())
        self.assertEqual(out["appAccountToken"], "")
        self.assertGreater(self.apple_until(self.ann), time.time() + 300 * 86400)
        # The same transaction again (a page load, a restore): confirmed, nothing new.
        again = self.post(client, "/api/apple/transactions", {"jws": self.tx(token)}).get_json()
        self.assertEqual(again["results"][0]["outcome"], "applied")

    def test_what_the_app_sends_is_checked(self):
        client = self.client(self.ann)
        token = self.state(client)["appAccountToken"]
        forged = Chain(name="Forger").sign({"transactionId": "1", "originalTransactionId": "1",
                                            "productId": YEARLY, "bundleId": BUNDLE,
                                            "environment": "Production",
                                            "type": "Auto-Renewable Subscription",
                                            "purchaseDate": now_ms(), "expiresDate": now_ms() + 365 * DAY,
                                            "signedDate": now_ms(), "appAccountToken": token})
        out = self.post(client, "/api/apple/transactions", {"jws": [forged, "x.y.z"]}).get_json()
        self.assertEqual([r["outcome"] for r in out["results"]], ["invalid", "invalid"])
        self.assertFalse(out["pro"])
        self.assertEqual(self.apple_until(self.ann), 0)
        for bad in ({"jws": 5}, {"jws": [5]}, {"jws": ["x"] * 21}, {}):
            self.assertEqual(self.post(client, "/api/apple/transactions", bad).status_code, 400, bad)
        r = client.post("/api/apple/transactions", data="jws=x", headers=FROM_PAGE,
                        content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 400)
        big = {"jws": ["a" * 300000]}
        self.assertEqual(self.post(client, "/api/apple/transactions", big).status_code, 413)

    def test_another_site_cannot_post(self):
        client = self.client(self.ann)
        token = self.state(client)["appAccountToken"]
        for headers in ({"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                        {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site", "Origin": "https://www.localhost"},
                        {"Origin": "null"}):
            r = self.post(client, "/api/apple/transactions", {"jws": [self.tx(token)]},
                          headers=dict(headers, **{"User-Agent": APP_UA}))
            self.assertEqual(r.status_code, 403, headers)
            self.assertEqual(self.post(client, "/api/apple/move", {"jws": self.tx(token)},
                                       headers=headers).status_code, 403, headers)
        self.assertEqual(self.apple_until(self.ann), 0)

    def test_web_pro_is_reported_so_the_app_sells_nothing(self):
        conn = db.connect(WEB_DB)
        billing.set_plan(conn, self.ben.id, "pro", "active", "cus_ben")
        conn.close()
        state = self.state(self.client(self.ben))
        self.assertEqual((state["pro"], state["source"]), (True, "web"))

    def test_one_apple_id_two_accounts(self):
        ann, ben = self.client(self.ann), self.client(self.ben)
        token = self.state(ann)["appAccountToken"]
        self.post(ann, "/api/apple/transactions", {"jws": [self.tx(token)]})
        out = self.post(ben, "/api/apple/transactions", {"jws": [self.tx(token)]}).get_json()
        self.assertEqual((out["pro"], out["results"][0]["outcome"]), (False, "other_account"))
        moved = self.post(ben, "/api/apple/move", {"jws": self.tx(token)}).get_json()
        self.assertEqual((moved["pro"], moved["source"], moved["outcome"]), (True, "apple", "moved"))
        self.assertEqual(self.apple_until(self.ann), 0)
        self.assertEqual(len(self.mails), 1)
        to, subject, body = self.mails[0]
        self.assertEqual(to, self.ann.email)
        self.assertIn("moved to another", subject)
        self.assertIn(self.ann.email, body)
        self.assertNotIn(self.ben.email, body)
        # Moving it straight back is refused for 30 days, and nobody is emailed.
        back = self.post(ann, "/api/apple/move", {"jws": self.tx(token)}).get_json()
        self.assertEqual((back["outcome"], back["pro"]), ("too_soon", False))
        self.assertGreater(back["after"], time.time())
        self.assertEqual(len(self.mails), 1)
        forged = Chain(name="Forger").sign({"transactionId": "1"})
        r = self.post(ann, "/api/apple/move", {"jws": forged})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.post(ann, "/api/apple/move", {"jws": ["x"]}).status_code, 400)

    def test_moves_are_rate_limited(self):
        client = self.client(self.ann)
        codes = [self.post(client, "/api/apple/move", {"jws": "x.y.z"}).status_code
                 for _ in range(web.APPSTORE_MOVES_PER_HOUR + 1)]
        self.assertEqual(codes, [400] * web.APPSTORE_MOVES_PER_HOUR + [429])

    def test_transactions_are_rate_limited(self):
        client = self.client(self.ann)
        limit = web.APPSTORE_POSTS_PER_HOUR
        codes = [self.post(client, "/api/apple/transactions", {"jws": []}).status_code
                 for _ in range(limit + 1)]
        self.assertEqual(codes[:limit], [200] * limit)
        self.assertEqual(codes[limit], 429)

    def test_test_purchases_follow_the_setting(self):
        client = self.client(self.ann)
        token = self.state(client)["appAccountToken"]
        _os.environ["APPLE_SANDBOX_PRO"] = "none"
        out = self.post(client, "/api/apple/transactions",
                        {"jws": [self.tx(token, env="Sandbox")]}).get_json()
        self.assertEqual((out["results"][0]["outcome"], out["pro"]), ("applied", False))
        self.assertEqual(self.apple_until(self.ann), 0)
        _os.environ["APPLE_SANDBOX_PRO"] = "all"
        out = self.post(client, "/api/apple/transactions",
                        {"jws": [self.tx(token, env="Sandbox")]}).get_json()
        self.assertTrue(out["pro"])


class Notifications(WebBase):
    def send(self, payload, **kw):
        return CLIENT.post("/apple/notifications", json={"signedPayload": payload}, **kw)

    def test_signed_notifications_are_taken_once(self):
        token = self.state(self.client(self.ann))["appAccountToken"]
        payload = self.note("SUBSCRIBED", self.tx(token, otid="7000", txid="7001"), uid="n-%s" % token)
        r = self.send(payload)
        self.assertEqual((r.status_code, r.get_json()["outcome"]), (200, "applied"))
        self.assertGreater(self.apple_until(self.ann), time.time())
        r = self.send(payload)
        self.assertEqual((r.status_code, r.get_json()["outcome"]), (200, "duplicate"))

    def test_from_apple_with_no_cookie_and_any_headers(self):
        # Sign-in is required on this server, and Apple sends no cookie, no
        # Origin; even a browser's cross-site marker does not stop it.
        self.assertTrue(web.auth_required())
        payload = self.note("SUBSCRIBED", self.tx(None, otid="7100", txid="7101"))
        r = self.send(payload, headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://apple.com"})
        self.assertEqual((r.status_code, r.get_json()["outcome"]), (200, "unowned"))

    def test_refused_unless_apple_signed_it(self):
        forged = Chain(name="Forger")
        tx = forged.sign({"transactionId": "1"})
        payload = forged.sign({"notificationType": "SUBSCRIBED", "notificationUUID": "f-1",
                               "signedDate": now_ms(),
                               "data": {"bundleId": BUNDLE, "appAppleId": APP_ID,
                                        "environment": "Production", "signedTransactionInfo": tx}})
        self.assertEqual(self.send(payload).status_code, 400)
        self.assertEqual(self.send("a.b.c").status_code, 400)
        self.assertEqual(CLIENT.post("/apple/notifications", json={}).status_code, 400)
        self.assertEqual(CLIENT.post("/apple/notifications", data="nope").status_code, 400)
        self.assertEqual(CLIENT.post("/apple/notifications", json={"signedPayload": 7}).status_code, 400)
        big = {"signedPayload": "a" * (200 * 1024)}
        self.assertEqual(CLIENT.post("/apple/notifications", json=big).status_code, 413)
        wrong_app = WEB_CHAIN.sign({"notificationType": "TEST", "notificationUUID": "w-1",
                                    "signedDate": now_ms(),
                                    "data": {"bundleId": BUNDLE, "appAppleId": APP_ID + 1,
                                             "environment": "Production"}})
        r = self.send(wrong_app)
        self.assertEqual((r.status_code, r.get_json()["outcome"]), (400, "INVALID_APP_IDENTIFIER"))

    def test_a_database_that_will_not_take_it_answers_500(self):
        real = st.handle_notification

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        web.apple_store_mod.handle_notification = locked
        try:
            r = self.send(self.note("TEST", None))
        finally:
            web.apple_store_mod.handle_notification = real
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.is_json)

    def test_order_does_not_matter(self):
        token = self.state(self.client(self.ann))["appAccountToken"]
        t0 = now_ms() - 10 * DAY
        bought = self.tx(token, otid="7200", txid="7201", purchase=t0, signed=t0)
        self.send(self.note("SUBSCRIBED", bought, signed=t0))
        refund_at = now_ms() - 60000
        refunded = self.tx(token, otid="7200", txid="7201", purchase=t0, signed=refund_at,
                           revocationDate=refund_at)
        self.assertEqual(self.send(self.note("REFUND", refunded, status=5, signed=refund_at))
                         .get_json()["outcome"], "applied")
        self.assertEqual(self.apple_until(self.ann), 0)
        # Apple retries the purchase's notification, and the app sends its old copy.
        self.assertEqual(self.send(self.note("DID_CHANGE_RENEWAL_STATUS", bought, signed=t0 + 1))
                         .get_json()["outcome"], "stale")
        out = self.post(self.client(self.ann), "/api/apple/transactions", {"jws": [bought]}).get_json()
        self.assertEqual((out["results"][0]["outcome"], out["pro"]), ("stale", False))
        self.assertEqual(self.apple_until(self.ann), 0)
        # Apple reverses the refund.
        back = self.tx(token, otid="7200", txid="7201", purchase=t0, signed=now_ms())
        self.send(self.note("REFUND_REVERSED", back))
        self.assertGreater(self.apple_until(self.ann), time.time())

    def test_repeated_failures_are_said_loudly(self):
        notes = []
        from bot import monitoring
        real = monitoring.note
        monitoring.note = lambda message, **context: notes.append((message, context))
        web._APPSTORE_REFUSED.clear()
        try:
            for _ in range(4):
                self.send("a.b.c")
        finally:
            monitoring.note = real
        self.assertEqual(len(notes), 1)
        self.assertIn("APPLE_APP_ID", notes[0][0])


class DeepJSON(WebBase):
    """JSON nested thousands deep, which json.loads cannot read without running
    out of recursion: anyone can post it to Apple's address, and any signed-in
    page to the app's. Each is refused as not JSON, as the site answers it: a
    400, with no traceback in the log and nothing for an error report."""

    DEEP = "[" * 3000 + "]" * 3000

    def setUp(self):
        super().setUp()
        # As on the site, where an error escaping a route is a 500 and an ERROR
        # in the log rather than an exception handed to the test.
        self.addCleanup(web.app.config.__setitem__, "TESTING", web.app.config["TESTING"])
        web.app.config["TESTING"] = False

    def refused(self, client, path, body, headers=None):
        with self.assertNoLogs(web.app.logger, level="ERROR"):
            r = client.post(path, data=body, content_type="application/json",
                            headers=headers or {})
        self.assertEqual(r.status_code, 400, (path, r.status_code))
        self.assertTrue(r.is_json, path)
        return r.get_json()

    def test_apples_address_from_anyone(self):
        for body in (self.DEEP, "[" * 30000 + "]" * 30000,
                     '{"signedPayload": %s}' % self.DEEP):
            self.assertEqual(self.refused(CLIENT, "/apple/notifications", body),
                             {"error": "No signedPayload."})

    def test_the_apps_posts(self):
        client = self.client(self.ann)
        for path in ("/api/apple/transactions", "/api/apple/move"):
            for body in (self.DEEP, '{"jws": %s}' % self.DEEP):
                self.assertEqual(self.refused(client, path, body, FROM_PAGE),
                                 {"error": "Send it as JSON."})
        # However often: the hourly limit on moves is for real ones.
        for _ in range(web.APPSTORE_MOVES_PER_HOUR + 3):
            self.refused(client, "/api/apple/move", self.DEEP, FROM_PAGE)
        token = self.state(client)["appAccountToken"]
        r = self.post(client, "/api/apple/transactions", {"jws": [self.tx(token)]})
        self.assertEqual((r.status_code, r.get_json()["pro"]), (200, True))


class Pages(WebBase):
    def make_apple_pro(self, user, renews=True, env="Production"):
        client = self.client(user)
        token = self.state(client)["appAccountToken"]
        otid = uuid.uuid4().hex[:12]
        self.post(client, "/api/apple/transactions",
                  {"jws": [self.tx(token, otid=otid, txid=otid + "1", env=env)]})
        if not renews:
            payload = self.note("DID_CHANGE_RENEWAL_STATUS",
                                self.tx(token, otid=otid, txid=otid + "1", env=env),
                                renewal={"autoRenewStatus": 0}, env=env)
            CLIENT.post("/apple/notifications", json={"signedPayload": payload})
        return client

    def test_the_app_sells_nothing_while_it_is_off(self):
        _os.environ.pop("APPLE_APP_ID")
        page = self.text(self.client(self.ann).get("/pricing", headers=IN_APP))
        self.assertIn("data-app-purchase", page)
        self.assertIn("Upgrading is not available in the app yet", page)
        for word in ("Restore Purchases", "data-iap-shop", "app-purchase.js", "stdeula"):
            self.assertNotIn(word, page)
        self.assertIsNone(PRICE.search(page))
        self.assertFalse(self.leaks(page), self.leaks(page))
        # Nor does the top bar offer Pro the app cannot sell.
        self.assertNotIn(">Go Pro<", self.text(self.client(self.ann).get("/monitor", headers=IN_APP)))

    def test_the_subscribe_screen_for_a_free_account(self):
        client = self.client(self.ann)
        page = self.text(client.get("/pricing", headers=IN_APP))
        for needed in ('data-iap-shop data-products="app.orenth.ios.pro.monthly,app.orenth.ios.pro.yearly"',
                       "data-iap-restore", "Restore Purchases",
                       "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/",
                       "Terms of Use (EULA)", 'href="/terms"', 'href="/privacy"', "Privacy Policy",
                       "renews automatically", "at least 24 hours before the", "Settings &gt; Apple Account &gt;",
                       "/static/app-purchase.js", "data-iap-move",
                       "As many stocks and coins a day as you like"):
            self.assertIn(needed, page, needed)
        # No price at all until StoreKit gives one, and nothing of Stripe's.
        self.assertIsNone(PRICE.search(page))
        self.assertFalse(self.leaks(page), self.leaks(page))
        self.assertFalse(self.sent_to_stripe)
        # Every page in the app carries the script; a browser never does.
        self.assertIn("/static/app-purchase.js", self.text(client.get("/monitor", headers=IN_APP)))
        self.assertIn(">Go Pro<", self.text(client.get("/monitor", headers=IN_APP)))
        self.assertNotIn("app-purchase.js", self.text(client.get("/pricing", headers={"User-Agent": SAFARI_UA})))

    def test_signed_out_in_the_app(self):
        page = self.text(self.client().get("/pricing", headers=IN_APP))
        self.assertIn("Create free account", page)
        self.assertIn("Terms of Use (EULA)", page)
        for word in ("data-iap-shop", "data-iap-restore", "app-purchase.js"):
            self.assertNotIn(word, page)
        self.assertFalse(self.leaks(page), self.leaks(page))

    def test_an_apple_subscriber_in_the_app(self):
        client = self.make_apple_pro(self.ann)
        page = self.text(client.get("/pricing", headers=IN_APP))
        self.assertIn("You have Pro through Apple", page)
        self.assertIn("It renews on", page)
        self.assertIn('data-iap-manage>Manage subscription', page)
        self.assertNotIn("data-iap-shop", page)
        self.assertFalse(self.leaks(page), self.leaks(page))
        account = self.text(client.get("/account", headers=IN_APP))
        self.assertIn("Through\n        Apple", account)
        self.assertIn("data-iap-manage", account)
        self.assertFalse(self.leaks(account), self.leaks(account))

    def test_a_web_subscriber_in_the_app_is_sold_nothing(self):
        conn = db.connect(WEB_DB)
        billing.set_plan(conn, self.ben.id, "pro", "active", "cus_ben")
        conn.close()
        page = self.text(self.client(self.ben).get("/pricing", headers=IN_APP))
        self.assertIn("Your account already has Pro", page)
        self.assertIn("paid for on the Orenth website", page)
        self.assertNotIn("data-iap-shop", page)
        self.assertFalse(self.leaks(page), self.leaks(page))

    def test_the_website_points_apple_subscribers_to_apple(self):
        client = self.make_apple_pro(self.ann)
        page = self.text(client.get("/pricing", headers={"User-Agent": SAFARI_UA}))
        self.assertIn(web.APPLE_PRO_SAID.replace("'", "&#39;").replace(">", "&gt;"), page)
        self.assertNotIn('action="/billing/checkout"', page)
        account = self.text(client.get("/account", headers={"User-Agent": SAFARI_UA}))
        self.assertIn("https://apps.apple.com/account/subscriptions", account)

    def test_stripe_checkout_is_refused_while_apple_bills(self):
        client = self.make_apple_pro(self.ann)
        del self.sent_to_stripe[:]
        r = client.post("/billing/checkout", data={"plan": "monthly"}, headers={"User-Agent": SAFARI_UA})
        self.assertEqual(r.status_code, 409)
        self.assertIn("You have Pro through Apple", self.text(r))
        self.assertFalse([s for s in self.sent_to_stripe if s[1] == "checkout/sessions"])
        # Someone without Apple Pro still gets to Stripe.
        r = self.client(self.ben).post("/billing/checkout", data={"plan": "monthly"},
                                       headers={"User-Agent": SAFARI_UA})
        self.assertEqual(r.status_code, 303)

    def test_deleting_an_account_apple_still_bills(self):
        client = self.make_apple_pro(self.ann)
        page = self.text(client.get("/account", headers=IN_APP))
        notice = page.index("keeps billing through Apple")
        manage = page.index("data-iap-manage", notice)
        delete = page.index('action="/account/delete"')
        self.assertLess(notice, manage)
        self.assertLess(manage, delete)
        self.assertIn("Deleting your Orenth account does not", page)
        self.assertFalse(self.leaks(page), self.leaks(page))
        web_page = self.text(client.get("/account", headers={"User-Agent": SAFARI_UA}))
        self.assertIn('href="https://apps.apple.com/account/subscriptions" data-iap-manage', web_page)
        # And deleting still happens at once.
        r = client.post("/account/delete", data={"confirm": "delete"}, headers=IN_APP)
        self.assertEqual(r.status_code, 302)
        conn = db.connect(WEB_DB)
        try:
            self.assertIsNone(A.get_user(conn, self.ann.id))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM apple_tokens WHERE user_id=?",
                                          (self.ann.id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM apple_subs WHERE user_id=?",
                                          (self.ann.id,)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_what_a_deleted_account_bought_is_forgotten_once_it_has_run_out(self):
        client = self.client(self.ann)
        token = self.state(client)["appAccountToken"]
        self.post(client, "/api/apple/transactions", {"jws": [self.tx(token)]})
        r = client.post("/account/delete", data={"confirm": "delete"}, headers=IN_APP)
        self.assertEqual(r.status_code, 302)
        conn = db.connect(WEB_DB)
        try:
            row = lambda: conn.execute("SELECT user_id, app_token FROM apple_subs WHERE original_tx_id=?",
                                       (self.otid,)).fetchone()
            self.assertEqual(tuple(row()), (None, ""))
            real = web.billing_mod.recheck
            web.billing_mod.recheck = lambda c: {}
            try:
                web._recheck_plans_job()
                self.assertIsNotNone(row())          # its year is still running
                conn.execute("UPDATE apple_subs SET expires_ms=? WHERE original_tx_id=?",
                             (now_ms() - 1000, self.otid))
                conn.commit()
                web._recheck_plans_job()
            finally:
                web.billing_mod.recheck = real
            self.assertIsNone(row())
        finally:
            conn.close()

    def test_no_warning_without_an_apple_subscription(self):
        page = self.text(self.client(self.ben).get("/account", headers=IN_APP))
        self.assertNotIn("data-apple-billing", page)
        self.assertNotIn("data-iap-manage", page)

    def test_a_cancelled_apple_subscription_says_so(self):
        client = self.make_apple_pro(self.ann, renews=False)
        page = self.text(client.get("/account", headers=IN_APP))
        self.assertIn("set not to renew", page)
        self.assertNotIn("keeps billing through Apple", page)
        pricing = self.text(client.get("/pricing", headers=IN_APP))
        self.assertIn("will not renew", pricing)

    def test_paying_twice_is_said(self):
        client = self.make_apple_pro(self.ann)
        conn = db.connect(WEB_DB)
        billing.set_plan(conn, self.ann.id, "pro", "active", "cus_ann")
        conn.close()
        for path, headers in (("/pricing", IN_APP), ("/account", IN_APP),
                              ("/pricing", {"User-Agent": SAFARI_UA}),
                              ("/account", {"User-Agent": SAFARI_UA})):
            page = self.text(client.get(path, headers=headers))
            self.assertIn("paying for Pro", page, (path, headers))
            self.assertIn("twice", page, (path, headers))
            if headers is IN_APP:
                self.assertFalse(self.leaks(page), self.leaks(page))
        page = self.text(self.make_apple_pro(self.ben).get("/account", headers=IN_APP))
        self.assertNotIn("twice", page)

    def test_a_test_purchase_is_labelled(self):
        client = self.make_apple_pro(self.ann, env="Sandbox")
        self.assertIn("from a test purchase", self.text(client.get("/pricing", headers=IN_APP)))

    def test_the_legal_pages_say_what_apple_purchases_keep(self):
        privacy = self.text(CLIENT.get("/privacy"))
        self.assertIn("anonymous account token", privacy)
        self.assertIn("Apple takes the payment", privacy)
        # What deleting the account leaves of it, and for how long.
        words = " ".join(privacy.split())
        self.assertIn("kept, linked to no account, until the time it paid for has run out", words)
        self.assertNotIn("deletes Orenth's record of any Pro bought through Apple", words)
        terms = self.text(CLIENT.get("/terms"))
        self.assertIn("Bought in the iPhone app", terms)
        self.assertNotIn("stripe", self.text(CLIENT.get("/terms", headers=IN_APP)).lower())


class Script(unittest.TestCase):
    """static/app-purchase.js, read the way the app will run it."""

    def test_script_contract(self):
        with open(_os.path.join(_os.path.dirname(HERE), "static", "app-purchase.js"),
                  encoding="utf-8") as f:
            js = f.read()
        for needed in ("nativePromise", "'AppPurchase'", "isNativePlatform", "'transaction'",
                       "ap('unfinished')", "ap('finish'", "result.outcome === 'invalid'",
                       "appAccountToken: state.appAccountToken", "if (state.pro)",
                       "ap('restore')", "ap('manageSubscriptions')", "ap('entitlements')",
                       "/api/apple/transactions", "/api/apple/move", "/api/apple/state",
                       "textContent"):
            self.assertIn(needed, js, needed)
        # StoreKit's words go in as text, never as markup.
        self.assertNotIn("innerHTML", js)
        # Restore asks for the Apple Account's password: only from its button.
        self.assertEqual(js.count("ap('restore')"), 1)
        self.assertLess(js.index("function restore()"), js.index("ap('restore')"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
