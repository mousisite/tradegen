"""Copies of the backups, kept somewhere other than the disk they were taken on.

The automatic backups are verified, but they sit on the same small disk as the
database, so losing that disk, or the hosting account it belongs to, loses both
at once. This copies each verified backup to a storage bucket that speaks the
S3 protocol, checks that the copy arrived whole, and keeps a sensible history
there. It is built for Cloudflare R2, and works with any store that speaks the
same protocol.

It is off until its settings are present, and until then nothing here runs or
reaches the network:

    BACKUP_S3_ENDPOINT            https://<account id>.r2.cloudflarestorage.com
    BACKUP_S3_BUCKET              the bucket's name
    BACKUP_S3_ACCESS_KEY_ID       from an API token that can read and write it
    BACKUP_S3_SECRET_ACCESS_KEY
    BACKUP_S3_REGION              optional, default "auto", which is what R2 uses
    BACKUP_S3_PREFIX              optional, default "orenth/": where in the bucket

The protocol is spoken with requests rather than an SDK. Five calls and a
signature are a small amount of code, and an SDK is a large dependency to add
for them. The signature is AWS Signature Version 4, and tests/test_offsite.py
checks it against the worked examples AWS publishes.

What is kept there: the newest three copies, the newest copy of each of the
last 30 days, and the oldest copy of each of the last 12 months. Only names
this code writes, directly under its own prefix, are ever deleted. Anything
else in the bucket is left alone, including a copy whose name was changed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

# Seconds to wait for a connection, and for each read once connected. A store
# that stops answering must not hold a copy, or a restore, for ever.
TIMEOUT = (10.0, 120.0)
# Tries per request, and the waits between them. Only a failure a second try
# could fix is tried again: no answer at all, or the store's own fault.
ATTEMPTS = 3
BACKOFF = (2.0, 8.0)

# The history kept there. At most 3 + 30 + 12 = 45 copies, fewer where the
# rules pick the same one.
KEEP_NEWEST = 3
KEEP_DAYS = 30
KEEP_MONTHS = 12

# Free space a download must leave behind, as for a backup: the restore folder
# may be on the same small disk as the live database.
HEADROOM = 50 * 1024 * 1024

SETTINGS = ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID",
            "BACKUP_S3_SECRET_ACCESS_KEY")

# Where the last result is written down for the operator's page, in the
# database's own key and value table, so a restart does not forget it.
LAST_KEY = "offsite_last"
GOOD_KEY = "offsite_last_good"

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# A backup's name as backup.py writes it. Only names like this are pruned.
_COPY = re.compile(r"^stockbot-(\d{4})-(\d{2})-(\d{2})-(\d{6})(?:-(\d+))?\.db$")
# What a stored name may be: no slash, so it can never reach outside the
# prefix or the folder it is fetched into, and nothing a URL must escape.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$")
_REGION = re.compile(r"^[A-Za-z0-9-]{1,40}$")
_PREFIX = re.compile(r"^[A-Za-z0-9._/-]*$")
_LOCAL = {"localhost", "127.0.0.1", "::1"}

# Hosts whose owner the privacy page can name.
_PROVIDERS = (("r2.cloudflarestorage.com", "Cloudflare"),
              ("amazonaws.com", "Amazon Web Services"),
              ("backblazeb2.com", "Backblaze"),
              ("wasabisys.com", "Wasabi"),
              ("digitaloceanspaces.com", "DigitalOcean"))


class OffsiteError(Exception):
    """A copy that could not be made, read or checked, said in plain words."""


# --- settings ----------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Where the copies go.

    The secret is left out of the printed form, so passing one of these to a
    log line or an error message cannot leak it.
    """
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str = field(repr=False)
    region: str = "auto"
    prefix: str = "orenth/"

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).netloc


def _join(names: List[str]) -> str:
    return names[0] if len(names) == 1 else "%s and %s" % (", ".join(names[:-1]), names[-1])


def _clean_endpoint(raw: str, bucket: str) -> Tuple[str, str]:
    """The endpoint as scheme://host[:port], or why it cannot be used."""
    shape = ("BACKUP_S3_ENDPOINT should be the address alone, like "
             "https://<account id>.r2.cloudflarestorage.com.")
    # A mistyped address must come back as a reason, never as an exception:
    # the privacy page, the operator's page and the backup job all read this.
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "", shape
    if parts.scheme not in ("https", "http") or not host:
        return "", shape
    if parts.scheme == "http" and host not in _LOCAL:
        # Plain http is for a test server on this machine, never for a store
        # across the internet: the copies hold every account's data.
        return "", ("BACKUP_S3_ENDPOINT must start with https://, because the "
                    "copies hold every account's data.")
    if parts.username or parts.password or parts.query or parts.fragment:
        return "", shape
    # Cloudflare shows a bucket's address with the bucket's name on the end,
    # which is the natural thing to paste; the name is in BACKUP_S3_BUCKET.
    path = parts.path.strip("/")
    if path and path != bucket:
        return "", shape
    netloc = "[%s]" % host if ":" in host else host
    if port and port != (443 if parts.scheme == "https" else 80):
        netloc += ":%d" % port
    return "%s://%s" % (parts.scheme, netloc), ""


def _clean_prefix(raw: Optional[str]) -> Tuple[str, str]:
    """The prefix ending in a slash, or "" for the top of the bucket."""
    if raw is None or not raw.strip():
        return "orenth/", ""
    prefix = raw.strip().strip("/")
    segments = prefix.split("/") if prefix else []
    # Dot segments would be folded away by the HTTP library before sending,
    # so the address signed would not be the address sent.
    if not _PREFIX.match(prefix) or any(s in ("", ".", "..") for s in segments):
        return "", ("BACKUP_S3_PREFIX may hold only letters, digits, dots, "
                    "dashes, underscores and single slashes.")
    return (prefix + "/") if prefix else "", ""


def load() -> Tuple[Optional[Settings], str]:
    """The settings, or None and the reason they cannot be used.

    None with no reason means none of them is set: copies are off, as meant.
    Some set and some not is a mistake worth saying, not a quiet off.
    """
    values = {k: (os.environ.get(k) or "").strip() for k in SETTINGS}
    if not any(values.values()):
        return None, ""
    missing = [k for k, v in values.items() if not v]
    if missing:
        return None, "%s %s not set, so nothing is copied." % (
            _join(missing), "is" if len(missing) == 1 else "are")
    bucket = values["BACKUP_S3_BUCKET"]
    if not _BUCKET.match(bucket):
        return None, ("BACKUP_S3_BUCKET should be the bucket's name alone: "
                      "letters, digits, dots and dashes.")
    endpoint, problem = _clean_endpoint(values["BACKUP_S3_ENDPOINT"], bucket)
    if problem:
        return None, problem
    region = (os.environ.get("BACKUP_S3_REGION") or "").strip() or "auto"
    if not _REGION.match(region):
        return None, "BACKUP_S3_REGION should be a region name, such as auto."
    prefix, problem = _clean_prefix(os.environ.get("BACKUP_S3_PREFIX"))
    if problem:
        return None, problem
    return Settings(endpoint=endpoint, bucket=bucket,
                    access_key=values["BACKUP_S3_ACCESS_KEY_ID"],
                    secret_key=values["BACKUP_S3_SECRET_ACCESS_KEY"],
                    region=region, prefix=prefix), ""


def provider() -> str:
    """Who holds the copies, for the privacy page, or "" while copies are off."""
    settings, _ = load()
    if settings is None:
        return ""
    host = urlsplit(settings.endpoint).hostname or ""
    for suffix, name in _PROVIDERS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return "A storage service"


def describe(backups_on: bool = True) -> str:
    """One line for the server's start-up summary."""
    settings, problem = load()
    if settings is None:
        return ("not running: %s" % problem) if problem else "off"
    if not backups_on:
        return "set up, but automatic backups are off, so nothing is copied"
    return "each backup copied to %s at %s" % (settings.bucket, settings.host)


# --- the signature -------------------------------------------------------------

def _quote(text: str, keep_slash: bool) -> str:
    """Percent-encode as the signature expects.

    Every byte except A-Z, a-z, 0-9 and - _ . ~ (and the slashes of a path)
    is escaped, in upper-case hex. quote() leaves exactly those alone.
    """
    return quote(text, safe="/" if keep_slash else "")


def canonical_query(query: Dict[str, str]) -> str:
    pairs = sorted((_quote(k, False), _quote(v, False)) for k, v in query.items())
    return "&".join("%s=%s" % pair for pair in pairs)


def canonical_request(method: str, path: str, query: Dict[str, str],
                      headers: Dict[str, str], payload_hash: str) -> Tuple[str, str]:
    """The request as the signature sees it, and the names of the signed headers.

    Every header passed is signed. The path is given unescaped and escaped
    here, the same way the request's own address is built.
    """
    lowered = {k.strip().lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(lowered)
    signed = ";".join(names)
    return "\n".join([method, _quote(path, True), canonical_query(query),
                      "".join("%s:%s\n" % (n, lowered[n]) for n in names),
                      signed, payload_hash]), signed


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, day: str, region: str, service: str = "s3") -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), day)
    return _hmac(_hmac(_hmac(key, region), service), "aws4_request")


def authorization(method: str, path: str, query: Dict[str, str],
                  headers: Dict[str, str], payload_hash: str, access_key: str,
                  secret_key: str, region: str, amz_date: str) -> str:
    """The Authorization header for one request: AWS Signature Version 4.

    headers must already hold host, x-amz-date and x-amz-content-sha256, and
    be exactly what is sent. amz_date is the x-amz-date value.
    """
    request, signed = canonical_request(method, path, query, headers, payload_hash)
    scope = "%s/%s/s3/aws4_request" % (amz_date[:8], region)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, amz_date[:8], region),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (access_key, scope, signed, signature))


# --- the bucket ----------------------------------------------------------------

def _error_code(response: requests.Response) -> str:
    """The store's own error code, from the XML it sends with a refusal."""
    try:
        found = re.search(r"<Code>([A-Za-z0-9.]{1,64})</Code>", response.text[:4096])
    except Exception:
        return ""
    return found.group(1) if found else ""


def _refused(response: requests.Response, settings: Settings, doing: str) -> OffsiteError:
    """A plain sentence for a request the store turned down."""
    status, code = response.status_code, _error_code(response)
    if code == "NoSuchBucket":
        text = "There is no bucket called %s at %s." % (settings.bucket, settings.host)
    elif code == "InvalidAccessKeyId":
        text = ("The storage does not recognise the access key in "
                "BACKUP_S3_ACCESS_KEY_ID.")
    elif code == "SignatureDoesNotMatch":
        text = ("The storage refused the secret key: BACKUP_S3_SECRET_ACCESS_KEY "
                "does not belong with BACKUP_S3_ACCESS_KEY_ID.")
    elif code == "RequestTimeTooSkewed":
        text = "The storage refused the request because this server's clock is wrong."
    elif status in (401, 403):
        text = ("The storage refused access to the bucket %s (%d%s). The API "
                "token needs Object Read & Write on it."
                % (settings.bucket, status, " " + code if code else ""))
    else:
        text = "The storage answered %d%s when asked %s." % (
            status, " (%s)" % code if code else "", doing)
    return OffsiteError(text)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node, name: str) -> str:
    for child in node:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


class Bucket:
    """One bucket, spoken to with signed requests over the S3 protocol.

    Addresses are path-style, https://host/bucket/key, which R2 and every other
    S3-compatible store accept. Every request has a timeout, and is tried again
    when it got no answer or the store said the fault was its own.
    """

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None):
        self.settings = settings
        self.http = session or requests.Session()
        # Signing is done here. Without an auth of its own, requests would
        # replace the signature with a password from a .netrc file naming
        # the same host.
        self.http.auth = lambda prepared: prepared

    def close(self) -> None:
        self.http.close()

    def _send(self, method: str, key: str = "", query: Optional[Dict[str, str]] = None,
              upload: Optional[Tuple[str, int, str]] = None,
              stream: bool = False) -> requests.Response:
        """Send one signed request; upload is (file, size, sha256) for a PUT."""
        s = self.settings
        path = "/" + s.bucket + ("/" + key if key else "")
        query = query or {}
        url = s.endpoint + _quote(path, True) + ("?" + canonical_query(query) if query else "")
        payload = upload[2] if upload else EMPTY_SHA256
        problem = ""
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF)) - 1])
            # Signed afresh each try: a signature is only good for minutes.
            amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            headers = {"Host": s.host, "x-amz-date": amz_date,
                       "x-amz-content-sha256": payload}
            if upload:
                # The fingerprint travels with the copy, so a download can be
                # checked against the backup itself, not only against its size.
                headers["x-amz-meta-sha256"] = upload[2]
            headers["Authorization"] = authorization(
                method, path, query, headers, payload, s.access_key, s.secret_key,
                s.region, amz_date)
            # A redirect is reported, never followed: the signed request names
            # this host, and would be sent on to another one.
            try:
                if upload:
                    headers["Content-Length"] = str(upload[1])
                    # Streamed from the file, not read into memory: a backup is
                    # the size of the whole database.
                    with open(upload[0], "rb") as body:
                        response = self.http.request(method, url, data=body,
                                                     headers=headers, timeout=TIMEOUT,
                                                     allow_redirects=False)
                else:
                    response = self.http.request(method, url, headers=headers,
                                                 timeout=TIMEOUT, stream=stream,
                                                 allow_redirects=False)
            except requests.exceptions.SSLError:
                raise OffsiteError("The storage's certificate could not be checked, "
                                   "so nothing was sent to %s." % s.host)
            except requests.Timeout:
                problem = "did not answer in time"
                continue
            except requests.ConnectionError:
                problem = "could not be reached"
                continue
            status = response.status_code
            if status >= 500 or status == 429 or (
                    status == 400 and _error_code(response) == "RequestTimeout"):
                problem = "answered %d" % status
                response.close()
                continue
            return response
        raise OffsiteError("The storage at %s %s, %d times in a row."
                           % (s.host, problem, ATTEMPTS))

    def put(self, key: str, path: str, size: int, sha256: str) -> None:
        with self._send("PUT", key, upload=(path, size, sha256)) as response:
            if response.status_code not in (200, 201):
                raise _refused(response, self.settings, "to store the copy")

    def head(self, key: str) -> Optional[Dict]:
        """The stored size and fingerprint of one object, or None if absent."""
        with self._send("HEAD", key) as response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise _refused(response, self.settings, "about the copy")
            try:
                size = int(response.headers.get("Content-Length", ""))
            except ValueError:
                size = -1
            return {"size": size,
                    "sha256": response.headers.get("x-amz-meta-sha256", "").strip()}

    def list(self, prefix: str) -> List[Dict]:
        """Every object under a prefix, as {key, size, modified}."""
        found: List[Dict] = []
        token = ""
        # Bounded, so a store that keeps saying "more" cannot loop for ever.
        for _page in range(1000):
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            with self._send("GET", query=query) as response:
                if response.status_code != 200:
                    raise _refused(response, self.settings, "for the list of copies")
                try:
                    root = ElementTree.fromstring(response.content)
                except ElementTree.ParseError:
                    raise OffsiteError("The storage's list of copies could not be read.")
            for item in root:
                if _local_name(item.tag) != "Contents":
                    continue
                try:
                    size = int(_child_text(item, "Size") or 0)
                except ValueError:
                    size = 0
                found.append({"key": _child_text(item, "Key"), "size": size,
                              "modified": _child_text(item, "LastModified")})
            token = _child_text(root, "NextContinuationToken")
            if _child_text(root, "IsTruncated").lower() != "true" or not token:
                return found
        raise OffsiteError("The storage's list of copies did not end.")

    def delete(self, key: str) -> None:
        with self._send("DELETE", key) as response:
            # Already gone is the outcome that was wanted.
            if response.status_code not in (200, 204, 404):
                raise _refused(response, self.settings, "to remove an old copy")

    def fetch(self, key: str, target: str) -> Dict:
        """Download one object to target, checking its size and fingerprint.

        Written under a temporary name and moved into place only once it checks
        out, so a broken download never sits there looking like a backup.
        """
        partial = target + ".part"
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF)) - 1])
            with self._send("GET", key, stream=True) as response:
                if response.status_code == 404:
                    raise OffsiteError("There is no copy called %s there."
                                       % key[len(self.settings.prefix):])
                if response.status_code != 200:
                    raise _refused(response, self.settings, "for the copy")
                expected = response.headers.get("x-amz-meta-sha256", "").strip()
                length = response.headers.get("Content-Length", "")
                if length.isdigit():
                    free = shutil.disk_usage(os.path.dirname(os.path.abspath(target))).free
                    if free < int(length) + HEADROOM:
                        raise OffsiteError("There is not enough free disk space here "
                                           "for that copy, so it was not downloaded.")
                digest, size = hashlib.sha256(), 0
                try:
                    with open(partial, "wb") as out:
                        for chunk in response.iter_content(1 << 16):
                            out.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                except (requests.Timeout, requests.ConnectionError,
                        requests.exceptions.ChunkedEncodingError):
                    _remove(partial)
                    continue
                except BaseException:
                    _remove(partial)
                    raise
            if length.isdigit() and int(length) != size:
                _remove(partial)
                continue
            if expected and expected != digest.hexdigest():
                _remove(partial)
                raise OffsiteError("The download does not match the backup that was "
                                   "uploaded, so it was not kept.")
            os.replace(partial, target)
            return {"size": size, "sha256": digest.hexdigest(), "checked": bool(expected)}
        raise OffsiteError("The download broke off %d times in a row." % ATTEMPTS)


# --- what is kept there ----------------------------------------------------------

def managed(name: str) -> bool:
    """Whether the kept history decides when this copy is deleted.

    True for the names backup.py writes. Any other name, such as a copy
    renamed by hand to keep it for good, is never deleted by the app.
    """
    return bool(_COPY.match(name))


def plan(names: Iterable[str], today: date) -> Tuple[List[str], List[str]]:
    """Which copies to keep and which to delete, by the dates in their names.

    Kept: the newest KEEP_NEWEST whatever their dates, so a clock gone wrong
    can never leave nothing recent; the newest copy of each of the last
    KEEP_DAYS days; and the oldest copy of each of the last KEEP_MONTHS
    months. A name that is not one backup.py writes is in neither list, so
    it is never deleted. Both lists run oldest first.
    """
    copies = []
    for name in set(names):
        found = _COPY.match(name)
        if not found:
            continue
        try:
            day = date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except ValueError:
            continue                     # not a real date, so not ours to judge
        copies.append(((day, found.group(4), int(found.group(5) or 0)), day, name))
    copies.sort()
    keep = {name for _, _, name in copies[-KEEP_NEWEST:]} if KEEP_NEWEST else set()
    newest_of_day: Dict[date, str] = {}
    oldest_of_month: Dict[Tuple[int, int], str] = {}
    for _, day, name in copies:
        newest_of_day[day] = name
        oldest_of_month.setdefault((day.year, day.month), name)
    # A copy dated after today counts as recent: it is kept, never guessed at.
    for day, name in newest_of_day.items():
        if (today - day).days < KEEP_DAYS:
            keep.add(name)
    this_month = today.year * 12 + today.month - 1
    for (year, month), name in oldest_of_month.items():
        if this_month - (year * 12 + month - 1) < KEEP_MONTHS:
            keep.add(name)
    return ([name for _, _, name in copies if name in keep],
            [name for _, _, name in copies if name not in keep])


def _names_under(bucket: Bucket, prefix: str) -> List[str]:
    """The names directly under the prefix: nothing in a deeper folder."""
    names = []
    for item in bucket.list(prefix):
        key = item["key"]
        rest = key[len(prefix):] if key.startswith(prefix) else ""
        if rest and "/" not in rest:
            names.append(rest)
    return names


def prune(bucket: Bucket, just_copied: str = "", today: Optional[date] = None) -> Dict:
    """Delete the copies the history no longer needs.

    Returns how many copies are there afterwards and how many were removed.
    The copy just made is never deleted, even when it is an old backup copied
    by hand that the history would not otherwise keep.
    """
    prefix = bucket.settings.prefix
    names = _names_under(bucket, prefix)
    # A listing may lag a moment behind a write on some stores.
    if just_copied and just_copied not in names:
        names.append(just_copied)
    _, drop = plan(names, today or date.today())
    removed = 0
    for name in drop:
        if name == just_copied:
            continue
        bucket.delete(prefix + name)
        removed += 1
    return {"kept": len([n for n in names if n.endswith(".db")]) - removed,
            "removed": removed}


# --- copying, listing and fetching -------------------------------------------------

def _fingerprint(path: str) -> Tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _mb(size: int) -> str:
    return "%.1f MB" % (size / 1048576.0)


def tally(result: Dict) -> str:
    """How many copies a successful copy left there, and how many it removed."""
    kept, removed = result.get("kept") or 0, result.get("removed") or 0
    return "%d %s there, %d old %s removed." % (
        kept, "copy" if kept == 1 else "copies", removed, "one" if removed == 1 else "ones")


def copy_backup(path: str, settings: Optional[Settings] = None,
                db_path: Optional[str] = None, today: Optional[date] = None) -> Dict:
    """Copy one verified backup off-site, check it arrived whole, then prune.

    Never raises. The result says what happened, is written down for the
    operator's page, and a failure is logged as an error. Whatever happens
    here, the local backup is left exactly as it was.
    """
    if settings is None:
        settings, problem = load()
        if settings is None:
            if problem:
                log.warning("Off-site copy skipped: %s", problem)
            return {"ok": False, "skipped": True,
                    "detail": problem or "Off-site copies are off."}
    name = os.path.basename(path)
    started = time.time()
    result: Dict = {"at": started, "name": name, "size": 0, "ok": False,
                    "detail": "", "kept": None, "removed": 0}
    bucket = Bucket(settings)
    try:
        if not _NAME.match(name):
            raise OffsiteError("%s cannot be stored under that name. Use only "
                               "letters, digits, dots, dashes and underscores." % name)
        size, sha256 = _fingerprint(path)
        result["size"] = size
        key = settings.prefix + name
        bucket.put(key, path, size, sha256)
        there = bucket.head(key)
        if there is None:
            raise OffsiteError("The copy was not there when checked straight "
                               "after it was stored.")
        if there["size"] != size:
            raise OffsiteError("The copy there is %d bytes, but the backup is %d."
                               % (there["size"], size))
        if there["sha256"] and there["sha256"] != sha256:
            raise OffsiteError("The copy there is not the backup just stored.")
        # Some stores do not hand the fingerprint back when asked about an
        # object; then only its size could be checked, and the page says so.
        result.update(ok=True, detail="Stored and checked.",
                      fingerprint=bool(there["sha256"]))
        try:
            result.update(prune(bucket, name, today))
        except OffsiteError as exc:
            result["note"] = "Old copies were not cleared out: %s" % exc
        except Exception as exc:
            log.exception("Clearing out old off-site copies failed")
            result["note"] = "Old copies were not cleared out (%s)." % type(exc).__name__
    except OffsiteError as exc:
        result["detail"] = str(exc)
    except requests.RequestException as exc:
        # Before OSError, which requests' own errors also count as.
        result["detail"] = "The storage could not be reached (%s)." % type(exc).__name__
    except OSError as exc:
        result["detail"] = "The backup could not be read: %s." % (
            exc.strerror or type(exc).__name__)
    except Exception as exc:
        log.exception("The off-site copy of %s failed unexpectedly", name)
        result["detail"] = ("It failed unexpectedly (%s); the server log has the "
                            "details." % type(exc).__name__)
    finally:
        bucket.close()
    result["seconds"] = round(time.time() - started, 1)
    _record(result, db_path)
    if result["ok"]:
        log.info("Off-site copy of %s (%s) stored in %s and checked. %s", name,
                 _mb(result["size"]), settings.bucket,
                 result.get("note") or tally(result))
    else:
        # An error, so error reporting picks it up when that is on.
        log.error("Off-site copy of %s failed: %s", name, result["detail"])
    return result


# One copy at a time. Backups are hours apart and a copy takes seconds, so this
# only matters when a copy is slow and the next backup comes round.
_COPYING = threading.Lock()


def _copy_quietly(path: str, settings: Settings, db_path: Optional[str]) -> None:
    with _COPYING:
        try:
            copy_backup(path, settings=settings, db_path=db_path)
        except BaseException:              # a thread's failure would go unseen
            log.exception("The off-site copy of %s stopped", os.path.basename(path))


def copy_in_background(path: str, db_path: Optional[str] = None) -> Optional[threading.Thread]:
    """Start copying one backup off-site, and return at once.

    Called right after a local backup is taken and verified. The copy runs in
    a thread of its own, so a slow or unreachable store can never hold up, or
    fail, the backup itself. Returns the thread, or None when copies are off.
    """
    settings, problem = load()
    if settings is None:
        if problem:
            log.warning("Off-site copy skipped: %s", problem)
        return None
    thread = threading.Thread(target=_copy_quietly, args=(path, settings, db_path),
                              name="orenth-offsite", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        # The backup that called this succeeded, and must not be reported
        # as failed because its copy could not be started.
        log.exception("Could not start the off-site copy of %s", os.path.basename(path))
        return None
    return thread


def list_copies(settings: Settings) -> List[Dict]:
    """The copies under the prefix, newest first, as {name, size, modified}."""
    bucket = Bucket(settings)
    try:
        listed = bucket.list(settings.prefix)
    finally:
        bucket.close()
    out = []
    for item in listed:
        key = item["key"]
        rest = key[len(settings.prefix):] if key.startswith(settings.prefix) else ""
        if rest and "/" not in rest:
            out.append(dict(item, name=rest))
    out.sort(key=lambda i: (i["modified"], i["name"]), reverse=True)
    return out


def fetch_copy(name: str, out_dir: str, settings: Settings) -> Tuple[str, Dict]:
    """Download one copy into a folder, for a restore. Returns its path.

    The name is one from list_copies(). It never overwrites a file already
    there: that may be the only good copy someone has.
    """
    name = (name or "").strip()
    if settings.prefix and name.startswith(settings.prefix):
        name = name[len(settings.prefix):]
    if not _NAME.match(name):
        raise OffsiteError("That is not the name of a copy. Use one of the names "
                           "the list of copies shows.")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, name)
    if os.path.exists(target):
        raise OffsiteError("There is already a file at %s. Move it, or fetch into "
                           "another folder." % target)
    bucket = Bucket(settings)
    try:
        return target, bucket.fetch(settings.prefix + name, target)
    finally:
        bucket.close()


# --- what the operator's page says --------------------------------------------------

def _record(result: Dict, db_path: Optional[str]) -> None:
    """Write the result down for the operator's page. Never raises.

    A plain connection rather than database.connect(), which would bring the
    schema up to date first: this runs from a background thread and from the
    command line, and only ever touches one row of a table every version has.
    """
    from . import database as db_mod
    path = db_path or db_mod.default_path()
    if not os.path.exists(path):
        return
    try:
        conn = sqlite3.connect(path, timeout=15.0)
        try:
            text = json.dumps(result)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                         (LAST_KEY, text))
            if result.get("ok"):
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                             (GOOD_KEY, text))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("Could not write down how the off-site copy went")


def _read(conn: sqlite3.Connection, key: str) -> Optional[Dict]:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def backup_hours() -> float:
    """Hours between automatic backups, read as serve.py reads them; 0 when off."""
    try:
        return max(0.0, float(os.environ.get("STOCKBOT_BACKUP_HOURS", "12") or 0))
    except ValueError:
        return 0.0


def status(conn: sqlite3.Connection) -> Dict:
    """Everything the operator's page shows about off-site copies.

    state is "off" (no settings), "broken" (some, but not usable) or "on".
    Read from the settings and the database only: opening the page never
    reaches the storage.
    """
    settings, problem = load()
    out = {"state": "on" if settings else ("broken" if problem else "off"),
           "problem": problem, "bucket": "", "host": "", "prefix": "",
           "last": None, "last_good": None, "backup_hours": backup_hours(),
           "keep_newest": KEEP_NEWEST, "keep_days": KEEP_DAYS,
           "keep_months": KEEP_MONTHS}
    if settings is not None:
        out.update(bucket=settings.bucket, host=settings.host, prefix=settings.prefix)
    if out["state"] != "off":
        # Settings broken since the last copy still leave that copy worth
        # knowing about.
        out.update(last=_read(conn, LAST_KEY), last_good=_read(conn, GOOD_KEY))
    return out
