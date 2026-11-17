"""Pro bought in the iPhone app, through Apple: what Apple signs, who it
belongs to, and what the site does with it.

  - The checks on what Apple signs (bot/appstore.py), against Apple's own
    signed test data and certificate vectors from app-store-server-library-
    python (MIT licence, in tests/data/appstore with APPLE_LICENSE.txt), and
    against home-made chains that try every way round them.

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
import sys as _sys
import time
import unittest

HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path.insert(0, _os.path.dirname(HERE))
_sys.path.insert(0, HERE)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from bot import appstore as av  # noqa: E402

DATA = _os.path.join(HERE, "data", "appstore")
UTC = datetime.timezone.utc
BUNDLE = "app.orenth.ios"
APP_ID = 6740000001


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
