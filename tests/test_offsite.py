"""Off-site copies of the backups, against a stand-in store on this machine.

Nothing here reaches the internet. A small S3-speaking server runs in a thread
and does what a real store does with each request: it checks the signature,
with its own few lines of the published method rather than the code under
test, checks the payload against its signed hash, and keeps the objects. The
signing code itself is checked against the worked examples AWS publishes.

What matters most is what fails quietly when wrong: a copy that never arrived
but is reported as kept, a history that deletes something it did not write, a
download that is not the backup, a secret that reaches a log, and a backup
broken by a copy that failed.
"""
import base64
import calendar
import contextlib
import hashlib
import hmac
import io
import logging
import os as _os
import re
import sqlite3
import sys as _sys
import tempfile
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote
from xml.sax.saxutils import escape

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

c, DB = harness.isolated_web("offsite")

import backup as backup_mod
from bot import database as db
from bot import offsite

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def text(response):
    return response.data.decode("utf-8", "replace")


SETTING_NAMES = offsite.SETTINGS + ("BACKUP_S3_REGION", "BACKUP_S3_PREFIX")


def settings_off():
    for key in SETTING_NAMES:
        _os.environ.pop(key, None)


# Importing web read any .env file there is. A real bucket named in one must
# never be reached from a test, so every setting starts out cleared.
settings_off()

KEY_ID = "AKIDOFFSITETEST"
SECRET = "s3cr3t/Only+For=This-Test-9f2c"
BUCKET = "orenth-test"
TODAY = date(2026, 11, 16)

# Quick tries, so the failure paths take milliseconds rather than seconds.
offsite.BACKOFF = (0.01, 0.01)

# Every line any part of the copy writes to the log, to look for the secret.
logged = []


class _Keep(logging.Handler):
    def emit(self, record):
        logged.append(self.format(record))


_catch = _Keep()
_catch.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(_catch)
logging.getLogger().setLevel(logging.DEBUG)


# --- the stand-in store ----------------------------------------------------------

AUTH = re.compile(r"^AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
                  r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})$")


def signature_problem(handler, body, store):
    """What a real store would say is wrong with a request's signature, or "".

    Written from the published method, not from bot/offsite.py, so a request
    signed differently from how it was sent is caught here.
    """
    found = AUTH.match(handler.headers.get("Authorization", ""))
    if not found:
        return "the Authorization header is not in the AWS4-HMAC-SHA256 shape"
    key_id, day, region, signed, signature = found.groups()
    if key_id != KEY_ID:
        return "an unknown access key"
    if region != store.region:
        return "the wrong region"
    stamp = handler.headers.get("x-amz-date", "")
    if not re.match(r"^\d{8}T\d{6}Z$", stamp) or stamp[:8] != day:
        return "x-amz-date is missing or does not match the scope"
    if abs(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")) - time.time()) > 900:
        return "x-amz-date is more than fifteen minutes out"
    names = signed.split(";")
    if names != sorted(names):
        return "the signed headers are not in order"
    for need in ("host", "x-amz-content-sha256", "x-amz-date"):
        if need not in names:
            return "%s is not signed" % need
    for name in handler.headers.keys():
        if name.lower().startswith("x-amz-") and name.lower() not in names:
            return "%s was sent but not signed" % name
    payload = handler.headers.get("x-amz-content-sha256", "")
    if payload != hashlib.sha256(body).hexdigest():
        return "the payload's hash does not match the body"
    raw_path, _, raw_query = handler.path.partition("?")
    pairs = sorted((quote(k, safe=""), quote(v, safe=""))
                   for k, v in parse_qsl(raw_query, keep_blank_values=True))
    request = "\n".join([
        handler.command, quote(unquote(raw_path), safe="/"),
        "&".join("%s=%s" % p for p in pairs),
        "".join("%s:%s\n" % (n, " ".join((handler.headers.get(n) or "").split()))
                for n in names),
        signed, payload])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, "%s/%s/s3/aws4_request" % (day, region),
                         hashlib.sha256(request.encode()).hexdigest()])
    key = ("AWS4" + SECRET).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return "" if hmac.compare_digest(expected, signature) else "the signature does not match"


# A continuation token made of the characters an address must escape, so the
# listing's paging proves the query is signed as it is sent.
TOKEN_MARK = "+/="


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store = None

    def log_message(self, *_args):
        pass

    def _send(self, status, body=b"", headers=None, length=None):
        try:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body) if length is None else length))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except OSError:
            pass                       # the client gave up waiting: expected here

    def _error(self, status, code):
        self._send(status, ('<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code>'
                            '<Message>stand-in</Message></Error>' % code).encode(),
                   {"Content-Type": "application/xml"})

    def _serve(self):
        store = self.store
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        store.seen.append((self.command, self.path))
        store.wire.append(self.path + "\n" + "\n".join("%s: %s" % kv for kv in self.headers.items()))
        if store.slow > 0:
            store.slow -= 1
            time.sleep(store.slow_for)
        if store.fail:
            status = store.fail.pop(0)
            return self._error(status, {403: "AccessDenied", 429: "SlowDown",
                                        503: "SlowDown"}.get(status, "InternalError"))
        problem = signature_problem(self, body, store)
        if problem:
            store.problems.append(problem)
            return self._error(403, "SignatureDoesNotMatch")
        raw_path, _, raw_query = self.path.partition("?")
        parts = unquote(raw_path).split("/", 2)
        bucket = parts[1] if len(parts) > 1 else ""
        key = parts[2] if len(parts) > 2 else ""
        if bucket != BUCKET:
            return self._error(404, "NoSuchBucket")
        if self.command == "GET" and not key:
            return self._list(dict(parse_qsl(raw_query, keep_blank_values=True)))
        if self.command == "PUT":
            store.objects[key] = {
                "body": body, "sha256": self.headers.get("x-amz-meta-sha256", ""),
                "modified": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
            return self._send(200, headers={"ETag": '"%s"' % hashlib.md5(body).hexdigest()})
        if self.command == "DELETE":
            store.objects.pop(key, None)
            return self._send(204)
        item = store.objects.get(key)
        if item is None:
            return self._error(404, "NoSuchKey")
        data = item["body"]
        if store.corrupt and data:
            data = data[:-1] + bytes([data[-1] ^ 1])
        headers = {"x-amz-meta-sha256": item["sha256"]} if item["sha256"] else {}
        if self.command == "HEAD":
            return self._send(200, headers={} if store.quiet_head else headers,
                              length=len(data) + (1 if store.wrong_size else 0))
        return self._send(200, data, headers)

    do_GET = do_PUT = do_HEAD = do_DELETE = _serve

    def _list(self, query):
        store = self.store
        prefix = query.get("prefix", "")
        keys = sorted(k for k in store.objects if k.startswith(prefix))
        start = 0
        token = query.get("continuation-token", "")
        if token:
            after = base64.b64decode(token[len(TOKEN_MARK):]).decode()
            start = len([k for k in keys if k <= after])
        page = keys[start:start + store.page]
        more = start + store.page < len(keys)
        rows = "".join("<Contents><Key>%s</Key><LastModified>%s</LastModified>"
                       "<Size>%d</Size><StorageClass>STANDARD</StorageClass></Contents>"
                       % (escape(k), store.objects[k]["modified"], len(store.objects[k]["body"]))
                       for k in page)
        follow = ("<NextContinuationToken>%s%s</NextContinuationToken>"
                  % (TOKEN_MARK, base64.b64encode(page[-1].encode()).decode())) if more else ""
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               '<Name>%s</Name><Prefix>%s</Prefix><KeyCount>%d</KeyCount>'
               '<MaxKeys>1000</MaxKeys><IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>'
               % (BUCKET, escape(prefix), len(page), "true" if more else "false", rows, follow))
        self._send(200, xml.encode(), {"Content-Type": "application/xml"})


class Store:
    """A store holding objects in memory, on a free port of this machine."""

    def __init__(self):
        self.region = "auto"
        self.objects = {}
        self.seen = []
        self.wire = []
        self.problems = []
        self.fail = []
        self.slow = 0
        self.slow_for = 0.0
        self.page = 1000
        self.wrong_size = False
        self.quiet_head = False
        self.corrupt = False
        handler = type("StoreHandler", (_Handler,), {"store": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def count(self, method):
        return len([1 for m, _ in self.seen if m == method])


store = Store()


def settings_on(**extra):
    _os.environ.update({"BACKUP_S3_ENDPOINT": store.endpoint, "BACKUP_S3_BUCKET": BUCKET,
                        "BACKUP_S3_ACCESS_KEY_ID": KEY_ID,
                        "BACKUP_S3_SECRET_ACCESS_KEY": SECRET})
    _os.environ.update(extra)


def meta(key):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


print("=" * 72)
print("THE SIGNATURE, AGAINST AWS'S PUBLISHED EXAMPLES")
print("=" * 72)

# From AWS's "Signature Calculations for the Authorization Header: Transferring
# Payload in a Single Chunk" for S3, which works each one through to the end.
AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_DATE, AWS_HOST = "20130524T000000Z", "examplebucket.s3.amazonaws.com"
EMPTY = offsite.EMPTY_SHA256


def aws(method, path, query, extra, payload):
    headers = dict({"Host": AWS_HOST, "x-amz-date": AWS_DATE,
                    "x-amz-content-sha256": payload}, **extra)
    return offsite.authorization(method, path, query, headers, payload, AWS_KEY,
                                 AWS_SECRET, "us-east-1", AWS_DATE)


get_object = aws("GET", "/test.txt", {}, {"Range": "bytes=0-9"}, EMPTY)
check("GET Object: the signature AWS published",
      get_object.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
      get_object)
check("and the credential and signed headers it lists",
      "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request" in get_object
      and "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date," in get_object)
request, _ = offsite.canonical_request(
    "GET", "/test.txt", {}, {"Host": AWS_HOST, "Range": "bytes=0-9",
                             "x-amz-date": AWS_DATE, "x-amz-content-sha256": EMPTY}, EMPTY)
check("its canonical request hashes to AWS's value",
      hashlib.sha256(request.encode()).hexdigest()
      == "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972")

welcome = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
put_object = aws("PUT", "/test$file.text", {},
                 {"Date": "Fri, 24 May 2013 00:00:00 GMT",
                  "x-amz-storage-class": "REDUCED_REDUNDANCY"}, welcome)
check("PUT Object, a name that must be escaped and a signed payload",
      put_object.endswith("Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"),
      put_object)
check("GET Bucket Lifecycle, a query with no value",
      aws("GET", "/", {"lifecycle": ""}, {}, EMPTY).endswith(
          "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"))
check("Get Bucket (List Objects), a query in two parts",
      aws("GET", "/", {"max-keys": "2", "prefix": "J"}, {}, EMPTY).endswith(
          "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))
# From AWS's "Examples of how to derive a signing key for Signature Version 4".
check("the signing key, derived as AWS's own example derives it",
      offsite.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215",
                          "us-east-1", "iam").hex()
      == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")
check("a query is escaped and sorted the way the signature expects",
      offsite.canonical_query({"prefix": "orenth/", "list-type": "2",
                               "continuation-token": "a+b/c=="})
      == "continuation-token=a%2Bb%2Fc%3D%3D&list-type=2&prefix=orenth%2F")

print()
print("=" * 72)
print("SETTINGS")
print("=" * 72)

settings_off()
check("with none of them set, copies are off and nothing is wrong",
      offsite.load() == (None, ""))
check("the start-up line says off", offsite.describe() == "off")
check("and the privacy page names nobody", offsite.provider() == "")

_os.environ["BACKUP_S3_BUCKET"] = BUCKET
loaded, problem = offsite.load()
check("with only some set, they are not used", loaded is None)
check("and it says which are missing",
      "BACKUP_S3_ENDPOINT" in problem and "BACKUP_S3_SECRET_ACCESS_KEY" in problem
      and "BACKUP_S3_BUCKET" not in problem, problem)
check("the start-up line says it is not running",
      offsite.describe().startswith("not running: "), offsite.describe())

settings_on(BACKUP_S3_ENDPOINT="http://abc123.r2.cloudflarestorage.com")
loaded, problem = offsite.load()
check("plain http to another machine is refused: the copies hold people's data",
      loaded is None and "https://" in problem, problem)

settings_on(BACKUP_S3_ENDPOINT="https://ABC123.r2.cloudflarestorage.com/orenth-test/")
loaded, problem = offsite.load()
check("the address Cloudflare shows, with the bucket on the end, is accepted",
      loaded is not None and loaded.endpoint == "https://abc123.r2.cloudflarestorage.com",
      (loaded, problem))
check("R2's region and the default prefix apply",
      loaded is not None and loaded.region == "auto" and loaded.prefix == "orenth/")
check("the privacy page can name Cloudflare", offsite.provider() == "Cloudflare")
check("the secret is never in the settings' printed form",
      loaded is not None and SECRET not in repr(loaded) and KEY_ID in repr(loaded))
check("the start-up line names the bucket and host",
      offsite.describe() == "each backup copied to orenth-test at "
                            "abc123.r2.cloudflarestorage.com", offsite.describe())
check("and says when the backups that feed it are off",
      "automatic backups are off" in offsite.describe(backups_on=False))

settings_on(BACKUP_S3_ENDPOINT="https://abc123.r2.cloudflarestorage.com/some/path")
check("an address with any other path is refused",
      offsite.load()[0] is None and "address alone" in offsite.load()[1])
settings_on(BACKUP_S3_ENDPOINT="https://abc123.r2.cloudflarestorage.com:443")
check("a default port is dropped, so the host signed is the host sent",
      offsite.load()[0].host == "abc123.r2.cloudflarestorage.com")
for typo in ("https://[abc123.r2.cloudflarestorage.com", "https://abc.example:99999",
             "abc123.r2.cloudflarestorage.com"):
    settings_on(BACKUP_S3_ENDPOINT=typo)
    check("a mistyped address is a reason, not a crash: %s" % typo,
          offsite.load()[0] is None and "address alone" in offsite.load()[1],
          offsite.load())
    check("and the pages that read the settings still open",
          c.get("/privacy").status_code == 200 and c.get("/usage").status_code == 200)
settings_on(BACKUP_S3_BUCKET="orenth test/../x")
check("a bucket name that is not one is refused", offsite.load()[0] is None)
settings_on(BACKUP_S3_PREFIX="/backups/orenth")
check("a prefix is tidied into a folder", offsite.load()[0].prefix == "backups/orenth/")
settings_on(BACKUP_S3_PREFIX="backups/../elsewhere")
check("a prefix that climbs out of itself is refused",
      offsite.load()[0] is None and "BACKUP_S3_PREFIX" in offsite.load()[1])
settings_on(BACKUP_S3_PREFIX="/")
check("a lone slash means the top of the bucket", offsite.load()[0].prefix == "")
_os.environ.pop("BACKUP_S3_PREFIX")
settings_on(BACKUP_S3_REGION="eu-west-1")
check("another region can be named", offsite.load()[0].region == "eu-west-1")
_os.environ.pop("BACKUP_S3_REGION")

print()
print("=" * 72)
print("NOTHING RUNS WITHOUT SETTINGS")
print("=" * 72)

conn = db.connect(DB)
db.open_trade(conn, "AAPL", "1d", 1, entry=100, stop=95, target1=120, quantity=10, user=1)
conn.close()

folder = tempfile.mkdtemp(prefix="stockbot-offsite-test-")
path = backup_mod.take(folder)
name = _os.path.basename(path)
check("a real backup to copy", backup_mod.verify(path))

settings_off()
before = len(store.seen)
skipped = offsite.copy_backup(path)
check("with no settings a copy does nothing", skipped.get("skipped") is True, skipped)
check("starts no thread", offsite.copy_in_background(path) is None)
_os.environ["BACKUP_S3_BUCKET"] = BUCKET
check("nor with only some of them", offsite.copy_in_background(path) is None)
check("and nothing at all reaches the store", len(store.seen) == before, store.seen[before:])
check("nor is anything written down", meta(offsite.LAST_KEY) is None)
settings_off()

print()
print("=" * 72)
print("A COPY, STORED AND CHECKED")
print("=" * 72)

settings_on()
result = offsite.copy_backup(path, today=TODAY)
key = "orenth/" + name
with open(path, "rb") as fh:
    raw = fh.read()
check("the copy succeeds", result["ok"] is True, result)
check("it is stored under the prefix, by the backup's own name", key in store.objects,
      sorted(store.objects))
check("byte for byte", store.objects.get(key, {}).get("body") == raw)
check("with the backup's fingerprint beside it",
      store.objects.get(key, {}).get("sha256") == hashlib.sha256(raw).hexdigest())
check("its size there was checked after storing it",
      [m for m, _ in store.seen[-3:]] == ["PUT", "HEAD", "GET"], store.seen[-3:])
check("every request carried a signature the store accepted", store.problems == [],
      store.problems)
check("the result gives the size and how many copies are there",
      result["size"] == len(raw) and result["kept"] == 1 and result["removed"] == 0, result)
check("and that the fingerprint there matched too", result["fingerprint"] is True, result)
check("it is written down for the operator's page",
      '"ok": true' in (meta(offsite.LAST_KEY) or "")
      and meta(offsite.GOOD_KEY) == meta(offsite.LAST_KEY))
check("the local backup is untouched", backup_mod.verify(path))

conn = db.connect(DB)
shown = offsite.status(conn)
conn.close()
check("the page's reading says on, and the last copy worked",
      shown["state"] == "on" and shown["last"]["ok"] and shown["last"]["name"] == name
      and shown["bucket"] == BUCKET, shown)

# A store that checks signatures properly must refuse a wrong secret, or the
# test above proves nothing about the signing.
wrong = offsite.Settings(endpoint=store.endpoint, bucket=BUCKET, access_key=KEY_ID,
                         secret_key="not-the-secret", region="auto", prefix="orenth/")
refused = offsite.copy_backup(path, settings=wrong, db_path=_os.path.join(folder, "none.db"))
check("a wrong secret is refused by the store", refused["ok"] is False
      and store.problems == ["the signature does not match"], store.problems)
check("and said plainly", "secret key" in refused["detail"], refused["detail"])
store.problems.clear()

print()
print("=" * 72)
print("WHEN THE STORE FAILS")
print("=" * 72)

puts = store.count("PUT")
store.fail = [503, 503]
result = offsite.copy_backup(path, today=TODAY)
check("two refusals from a busy store are tried again", result["ok"] is True, result)
check("three tries in all", store.count("PUT") - puts == 3, store.count("PUT") - puts)

store.fail = [429]
check("a store asking to slow down is tried again",
      offsite.copy_backup(path, today=TODAY)["ok"] is True)

good_before = meta(offsite.GOOD_KEY)
puts = store.count("PUT")
store.fail = [500, 500, 500]
result = offsite.copy_backup(path, today=TODAY)
check("three failures in a row give up", result["ok"] is False
      and store.count("PUT") - puts == 3, result)
check("and say so plainly", "answered 500, 3 times in a row" in result["detail"],
      result["detail"])
check("the failure is written down", '"ok": false' in (meta(offsite.LAST_KEY) or ""))
check("without forgetting the last good copy", meta(offsite.GOOD_KEY) == good_before)
check("and the local backup is untouched", backup_mod.verify(path))

puts = store.count("PUT")
store.fail = [403]
result = offsite.copy_backup(path, today=TODAY)
check("a refusal is not tried again: it would only be refused again",
      result["ok"] is False and store.count("PUT") - puts == 1)
check("it says what the key needs", "Object Read & Write" in result["detail"],
      result["detail"])

store.wrong_size = True
result = offsite.copy_backup(path, today=TODAY)
store.wrong_size = False
check("a copy whose size there does not match is a failure",
      result["ok"] is False and "bytes, but the backup is" in result["detail"], result)

store.quiet_head = True
result = offsite.copy_backup(path, today=TODAY)
store.quiet_head = False
check("a store that does not say the fingerprint back is checked by size alone",
      result["ok"] is True and result["fingerprint"] is False, result)
page = text(c.get("/usage"))
check("and the page claims no more than that",
      "the same size" in page and "the same fingerprint" not in page)

real_timeout = offsite.TIMEOUT
offsite.TIMEOUT = (2.0, 0.3)
store.slow, store.slow_for = 3, 1.2
started = time.time()
try:
    offsite.Bucket(offsite.load()[0]).list("orenth/")
    check("a store that does not answer in time is given up on", False, "it answered")
except offsite.OffsiteError as exc:
    check("a store that does not answer in time is given up on",
          "did not answer in time, 3 times" in str(exc), exc)
took = time.time() - started
check("within the timeout for each try, not the store's pace", took < 3.0, "%.1fs" % took)
offsite.TIMEOUT = real_timeout
store.slow = 0

probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
closed = probe.server_address[1]
probe.server_close()
settings_on(BACKUP_S3_ENDPOINT="http://127.0.0.1:%d" % closed)
result = offsite.copy_backup(path, today=TODAY, db_path=_os.path.join(folder, "none.db"))
check("a store that cannot be reached is a plain failure",
      result["ok"] is False and "could not be reached" in result["detail"], result)
settings_on()

result = offsite.copy_backup(_os.path.join(folder, "gone.db"), today=TODAY,
                             db_path=_os.path.join(folder, "none.db"))
check("a backup that is not there is a failure, not a crash",
      result["ok"] is False and "could not be read" in result["detail"], result)
odd = _os.path.join(folder, "my backup.db")
with open(odd, "wb") as fh:
    fh.write(raw)
before = len(store.seen)
result = offsite.copy_backup(odd, today=TODAY, db_path=_os.path.join(folder, "none.db"))
check("a name an address would have to escape is refused before sending",
      result["ok"] is False and len(store.seen) == before, result)

print()
print("=" * 72)
print("WHAT IS KEPT THERE")
print("=" * 72)

daily = []
for back in range(400):
    day = TODAY - timedelta(days=back)
    for clock in ("031500", "151500"):
        daily.append("stockbot-%s-%s.db" % (day.isoformat(), clock))
strangers = ["notes.txt", "stockbot-manual.db", "stockbot-2026-02-30-000000.db",
             "keep-2025-01-01.db"]
keep, drop = offsite.plan(daily + strangers, TODAY)
months = {(TODAY.year * 12 + TODAY.month - 1 - n) for n in range(12)}
first_of_month = {"stockbot-%04d-%02d-01-031500.db" % (m // 12, m % 12 + 1) for m in months}
evenings = {"stockbot-%s-151500.db" % (TODAY - timedelta(days=n)).isoformat()
            for n in range(30)}
check("the newest of each of the last 30 days is kept", evenings <= set(keep))
check("the first of each of the last 12 months is kept", first_of_month <= set(keep))
check("and today's other copy, as one of the newest three",
      "stockbot-%s-031500.db" % TODAY.isoformat() in keep)
check("that is all: 30 days, 12 months and one more", len(keep) == 43, len(keep))
check("everything else this code wrote goes", len(drop) == 800 - 43, len(drop))
check("nothing older than twelve months is kept",
      all(n >= "stockbot-2025-12-01" for n in keep), keep[:3])
check("a name it did not write is in neither list",
      not set(strangers) & (set(keep) | set(drop)))
check("several in one second: the newest of the day is the highest suffix",
      offsite.plan(["stockbot-2026-11-10-120000.db", "stockbot-2026-11-10-120000-2.db",
                    "stockbot-2026-11-10-120000-10.db", "stockbot-2026-11-11-000000.db",
                    "stockbot-2026-11-12-000000.db", "stockbot-2026-11-13-000000.db"],
                   TODAY)[1] == ["stockbot-2026-11-10-120000-2.db"])
future = "stockbot-2027-01-01-000000.db"
check("a copy dated after today is kept, not guessed at",
      future in offsite.plan(daily + [future], TODAY)[0])
check("with a clock years ahead, the newest three still stay",
      len(offsite.plan(daily, date(2031, 1, 1))[0]) == 3)
check("nothing listed, nothing to do", offsite.plan([], TODAY) == ([], []))

store.objects.clear()
for n in daily:
    store.objects["orenth/" + n] = {"body": b"x", "sha256": "", "modified": "2026-11-16T00:00:00.000Z"}
foreign = ["other/stockbot-2025-01-01-000000.db", "orenth/notes.txt",
           "orenth/keep-2025-01-01.db", "orenth/older/stockbot-2025-01-01-000000.db",
           "stockbot-2025-01-01-000000.db"]
for k in foreign:
    store.objects[k] = {"body": b"y", "sha256": "", "modified": "2026-11-16T00:00:00.000Z"}
store.page = 100
lists = store.count("GET")
pruned = offsite.prune(offsite.Bucket(offsite.load()[0]), today=TODAY)
left = {k[len("orenth/"):] for k in store.objects if k.startswith("orenth/")}
check("the store keeps exactly what the history says", left & set(daily) == set(keep),
      len(left & set(daily)))
check("it read every page of a long list", store.count("GET") - lists == 9,
      store.count("GET") - lists)
# The 43 the history keeps and the renamed copy; the notes file is no copy.
check("and says how many it removed and how many copies are there",
      pruned == {"removed": 757, "kept": 43 + 1}, pruned)
check("in words that count properly",
      offsite.tally(pruned) == "44 copies there, 757 old ones removed."
      and offsite.tally({"kept": 1, "removed": 1}) == "1 copy there, 1 old one removed.")
check("nothing outside its prefix, in a deeper folder or of another name is touched",
      all(k in store.objects for k in foreign))
check("every request was signed as sent, paging tokens included", store.problems == [],
      store.problems[:3])

# A backup copied by hand that the history would drop stays until the next
# copy: deleting what was just stored would make the copy pointless.
old = _os.path.join(folder, "stockbot-2024-06-01-120000.db")
with open(old, "wb") as fh:
    fh.write(raw)
result = offsite.copy_backup(old, today=TODAY)
check("an old backup copied by hand is not deleted straight after",
      result["ok"] and "orenth/stockbot-2024-06-01-120000.db" in store.objects, result)
store.page = 1000

print()
print("=" * 72)
print("BRINGING ONE BACK")
print("=" * 72)

store.objects.clear()
check("a fresh copy to fetch", offsite.copy_backup(path, today=TODAY)["ok"])
restore = tempfile.mkdtemp(prefix="stockbot-offsite-restore-")
fetched, got = offsite.fetch_copy(name, restore, offsite.load()[0])
check("it is downloaded under its own name", fetched == _os.path.join(restore, name))
with open(fetched, "rb") as fh:
    check("byte for byte the backup that was copied", fh.read() == raw)
check("and checked against the fingerprint stored with it", got["checked"] is True, got)
check("it opens as an intact backup", backup_mod.verify(fetched))
again = tempfile.mkdtemp(prefix="stockbot-offsite-restore-")
check("the bucket's own folder name may be given with it",
      offsite.fetch_copy("orenth/" + name, again, offsite.load()[0])[0].endswith(name))

for bad, why in (("../../etc/passwd", "not the name of a copy"),
                 ("stockbot-1999-01-01-000000.db", "no copy called"),
                 (name, "already a file")):
    try:
        offsite.fetch_copy(bad, restore, offsite.load()[0])
        check("fetching %r is refused" % bad, False, "it was fetched")
    except offsite.OffsiteError as exc:
        check("fetching %r is refused: %s" % (bad, why), why in str(exc), exc)

store.corrupt = True
spoilt = tempfile.mkdtemp(prefix="stockbot-offsite-restore-")
try:
    offsite.fetch_copy(name, spoilt, offsite.load()[0])
    check("a download that is not the backup is refused", False, "it was kept")
except offsite.OffsiteError as exc:
    check("a download that is not the backup is refused", "does not match" in str(exc), exc)
store.corrupt = False
check("and nothing is left behind that could be mistaken for it", _os.listdir(spoilt) == [],
      _os.listdir(spoilt))

print()
print("=" * 72)
print("IN THE BACKGROUND")
print("=" * 72)

thread = offsite.copy_in_background(path)
check("with settings, a copy starts in a thread of its own", thread is not None)
thread.join(15)
check("and finishes", not thread.is_alive())
check("having written down that it worked", '"ok": true' in (meta(offsite.LAST_KEY) or ""))

store.fail = [500, 500, 500]
thread = offsite.copy_in_background(path)
thread.join(15)
check("a failing copy ends quietly, never in the backup's way",
      not thread.is_alive() and '"ok": false' in (meta(offsite.LAST_KEY) or ""))
check("the log says it failed",
      any(line.startswith("ERROR bot.offsite Off-site copy of") for line in logged))
store.fail = []

print()
print("=" * 72)
print("THE COMMAND LINE")
print("=" * 72)


def run(*argv):
    out = io.StringIO()
    code = None
    with contextlib.redirect_stdout(out):
        try:
            code = backup_mod.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


code, said = run("--help")
check("--help describes the three off-site commands in words",
      code == 0 and "--offsite FILE" in said and "--offsite-list" in said
      and "--fetch NAME" in said and "restore from" in said, said[-600:])

store.objects.clear()
code, said = run("--offsite", path)
check("--offsite checks the backup and copies it", code == 0
      and "integrity  ok" in said and "stored and checked" in said, said)
code, said = run("--offsite-list")
check("--offsite-list lists it", code == 0 and name in said and "1 in all" in said, said)
cli_restore = tempfile.mkdtemp(prefix="stockbot-offsite-cli-")
code, said = run("--fetch", name, "--out", cli_restore)
check("--fetch downloads it, checks it and verifies the database in it", code == 0
      and "matches the backup that was uploaded" in said and "integrity  ok" in said, said)

corrupt = _os.path.join(folder, "stockbot-2026-11-15-000000.db")
with open(corrupt, "wb") as fh:
    fh.write(b"not a database at all" * 50)
puts = store.count("PUT")
code, said = run("--offsite", corrupt)
check("a backup that does not verify is not copied",
      code == 1 and store.count("PUT") == puts and "not copied" in said, said)
renamed = _os.path.join(folder, "keep-for-good.db")
with open(renamed, "wb") as fh:
    fh.write(raw)
code, said = run("--offsite", renamed)
check("a renamed copy is said to be kept for good", code == 0 and "never deletes it" in said,
      said)

settings_off()
code, said = run("--offsite-list")
check("with no settings it says they are not set up, and where the steps are",
      code == 1 and "not set up" in said and "DEPLOY.md" in said, said)
settings_on()

print()
print("=" * 72)
print("THE OPERATOR'S PAGE AND THE PRIVACY PAGE")
print("=" * 72)

settings_off()
page = text(c.get("/usage"))
check("off: the page says the backups are on one disk only",
      "not copied anywhere else" in page)
_os.environ["BACKUP_S3_BUCKET"] = BUCKET
page = text(c.get("/usage"))
check("half set up: it says so, and what is missing",
      "Off-site copies are not running" in page and "BACKUP_S3_ENDPOINT" in page)
settings_on()
check("a good copy to show", offsite.copy_backup(path, today=TODAY)["ok"])
page = text(c.get("/usage"))
section = page[page.index("Copies kept elsewhere"):]
section = section[:section.index("</section>")]
check("on: when, what, how big, and that it was checked",
      "just now" in section and name in section and "MB" in section
      and "Checked" in section and BUCKET in section
      and "the same size" in section and "the same fingerprint" in section, section[:400])
check("and what the history keeps", "the last 30 days" in section
      and "the last 12 months" in section)
store.fail = [403]
offsite.copy_backup(path, today=TODAY)
page = text(c.get("/usage"))
section = page[page.index("Copies kept elsewhere"):]
section = section[:section.index("</section>")]
check("after a failure: that it failed, why, and the last good copy",
      "Failed" in section and "Object Read &amp; Write" in section
      and "The last good copy" in section, section[:600])
check("the secret is never on the page", SECRET not in page)

settings_off()
privacy = text(c.get("/privacy"))
check("the privacy page lists backup storage as off until it is switched on",
      "Only if copies of the backups are kept off this server" in privacy)
check("and says nothing of how long copies are kept there",
      "12 months" not in privacy)
settings_on(BACKUP_S3_ENDPOINT="https://abc123.r2.cloudflarestorage.com")
privacy = text(c.get("/privacy"))
check("once on, it names Cloudflare", "<td>Cloudflare</td>" in privacy)
check("and how long a deleted account's data can stay in the copies",
      "the last 30 days" in privacy and "the last 12 months" in privacy)
check("without the bucket or its address", "abc123" not in privacy and BUCKET not in privacy)
settings_on()

print()
print("=" * 72)
print("THE SECRET STAYS SECRET")
print("=" * 72)

check("the log was written to", len(logged) > 5, len(logged))
check("but never with the secret", not [line for line in logged if SECRET in line])
check("it never crossed the wire: only a signature made with it",
      store.wire and not [w for w in store.wire if SECRET in w])
check("nor is it written down with a result",
      SECRET not in (meta(offsite.LAST_KEY) or "") + (meta(offsite.GOOD_KEY) or ""))
check("the store saw no request it could not verify", store.problems == [], store.problems)

settings_off()
store.server.shutdown()
logging.getLogger().removeHandler(_catch)

print()
print("%d failure(s)" % len(fails))
print("OFFSITE OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
