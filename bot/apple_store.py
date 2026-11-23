"""Pro bought through Apple, in the iPhone app: who owns which subscription,
and until when.

Apple's signed data arrives two ways, and both end in _apply_locked():
  - from the iPhone app, after a purchase, a restore or a page load: the
    transaction's jwsRepresentation, sent by the signed-in person;
  - from Apple's server: App Store Server Notifications V2.

One row per subscription, keyed by (environment, originalTransactionId), which
stays the same across renewals, upgrades, downgrades and resubscribes in the
subscription group. The rule for Pro is one line:

    Pro until max(expiresDate, grace period end), unless the latest
    transaction was refunded or revoked.

users.apple_until holds that time, in seconds, for each person, worked out
again inside the same write transaction as every change, so billing.is_pro()
stays a question about the user row alone.

Order. Apple does not promise to deliver notifications in order, retries
failed ones for days, and the app can send an old copy. Two gates keep a
subscription moving forward only:
  - The transaction: taken when it is a newer purchase (a renewal), or the
    stored transaction signed by Apple no earlier than the copy already
    stored, and never when it is marked isUpgraded. An old copy of a
    transaction, signed before its refund, cannot undo the refund; a renewal
    is never turned away because an older transaction was signed again since.
  - The status, grace period and auto-renew flag: only notifications carry
    them, and only a notification signed after the last one applied, about the
    transaction that is current, changes them.

Duplicates. notificationUUID is stored in the same database transaction as the
change, so a retried notification is applied once.

Who owns it. The person whose appAccountToken the purchase carries. Once a
subscription is linked to an account it stays there until someone signed in
on a phone with that Apple ID moves it on purpose (move_to), or the account
is deleted, so a second Orenth account signed in on the same iPhone cannot
simply take it. Except by paying: once it has run out, a new purchase made
signed in to another account is that account's, though a resubscribe keeps
the originalTransactionId and so lands on the same row.

Settings, all from the environment, and in-app purchase is off until the
first is set:

  APPLE_APP_ID         the app's numeric Apple ID, from App Store Connect >
                       App Information. Production notifications must name it.
  APPLE_PRODUCT_IDS    the subscriptions that are Pro, comma separated;
                       app.orenth.ios.pro.monthly,app.orenth.ios.pro.yearly
                       unless set
  APPLE_BUNDLE_ID      the app's bundle id, app.orenth.ios unless set; the
                       same setting Sign in with Apple uses
  APPLE_SANDBOX_PRO    whether test purchases (Sandbox: builds run from Xcode,
                       TestFlight, and App Review) give Pro: all (the default,
                       because App Review buys in the sandbox with the real
                       build), allowlist (only the accounts named in
                       APPLE_SANDBOX_USERS) or none
  APPLE_SANDBOX_USERS  email addresses, comma separated, for allowlist

The App Store Server API (an In-App Purchase key) is not used: what the app
sends and Apple's notifications are enough to give Pro and take it away.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .appstore import PRODUCTION, SANDBOX, VerificationError, Verifier

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS apple_tokens (
    token    TEXT PRIMARY KEY,              -- appAccountToken: random UUID v4, lower case
    user_id  INTEGER NOT NULL UNIQUE,
    created  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apple_subs (
    environment     TEXT NOT NULL,          -- 'Production' or 'Sandbox'
    original_tx_id  TEXT NOT NULL,
    user_id         INTEGER,                -- NULL until someone owns it
    product_id      TEXT NOT NULL,
    transaction_id  TEXT NOT NULL,          -- the current transaction
    purchase_ms     INTEGER NOT NULL,       -- its purchaseDate
    expires_ms      INTEGER NOT NULL,       -- its expiresDate
    revoked_ms      INTEGER NOT NULL DEFAULT 0,   -- its revocationDate (refund, revoke)
    tx_signed_ms    INTEGER NOT NULL,       -- signedDate of the copy stored
    grace_until_ms  INTEGER NOT NULL DEFAULT 0,   -- gracePeriodExpiresDate while in grace
    auto_renew      INTEGER NOT NULL DEFAULT -1,  -- 1 on, 0 off, -1 not known
    status          INTEGER NOT NULL DEFAULT 0,   -- Apple's 1..5, 0 not known
    note_signed_ms  INTEGER NOT NULL DEFAULT 0,   -- signedDate of the last notification applied
    app_token       TEXT NOT NULL DEFAULT '',
    moved_from      INTEGER,                -- the account it was last moved away from
    moved_ms        INTEGER NOT NULL DEFAULT 0,   -- when, so moves can be limited
    updated         INTEGER NOT NULL,
    PRIMARY KEY (environment, original_tx_id)
);
CREATE INDEX IF NOT EXISTS idx_apple_subs_user ON apple_subs(user_id);
CREATE TABLE IF NOT EXISTS apple_notifications (
    uuid            TEXT PRIMARY KEY,       -- notificationUUID
    type            TEXT NOT NULL,
    subtype         TEXT NOT NULL DEFAULT '',
    environment     TEXT NOT NULL DEFAULT '',
    original_tx_id  TEXT NOT NULL DEFAULT '',
    signed_ms       INTEGER NOT NULL,
    received        INTEGER NOT NULL,
    outcome         TEXT NOT NULL DEFAULT ''
);
"""

AUTO_RENEWABLE = "Auto-Renewable Subscription"
STATUS_GRACE = 4             # Apple's status: in the Billing Grace Period
STATUS_RETRY = 3             # Apple's status: billing retry, Pro already ended
DAY_MS = 86400 * 1000
# Apple keeps trying a failed renewal for up to 60 days. Until then, a person
# who has not cancelled can still be charged.
RETRY_DAYS = 60
# How often one subscription may be moved from one account to another.
MOVE_EVERY_DAYS = 30
# Notifications are remembered this long, to spot Apple's retries. Apple stops
# retrying one within about a week.
KEEP_NOTIFICATIONS_DAYS = 30

# Outcomes, as words the app and the log can show.
APPLIED = "applied"              # the subscription changed, or was confirmed
STALE = "stale"                  # older than what is stored: nothing changed
OTHER_ACCOUNT = "other_account"  # genuine, but owned by a different Orenth account
UNOWNED = "unowned"              # genuine, but nobody is known to own it yet
IGNORED = "ignored"              # not one of our subscriptions, or no transaction
DUPLICATE = "duplicate"          # a notification already applied
INVALID = "invalid"              # not signed by Apple, or not for this app
# Outcomes of moving a subscription to another account.
MOVED = "moved"
ALREADY_HERE = "already_here"
TOO_SOON = "too_soon"
NOT_CURRENT = "not_current"

DEFAULT_PRODUCTS = ("app.orenth.ios.pro.monthly", "app.orenth.ios.pro.yearly")
SANDBOX_POLICIES = ("all", "allowlist", "none")
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_APP_ID = re.compile(r"^[0-9]{1,19}$")


# --- settings ------------------------------------------------------------------

def _setting(name: str, default: str = "") -> str:
    """The first non-empty line of a setting, trimmed: a value pasted into a
    hosting dashboard easily carries a stray newline."""
    for line in (os.environ.get(name) or "").splitlines():
        if line.strip():
            return line.strip()
    return default


def app_id() -> Optional[int]:
    """The app's numeric Apple ID, or None when unset or not a number."""
    text = _setting("APPLE_APP_ID")
    if not _APP_ID.match(text):
        return None
    value = int(text)
    return value if value > 0 else None


def enabled() -> bool:
    """Whether Pro is sold in the iPhone app. Off until APPLE_APP_ID is set."""
    return app_id() is not None


def bundle_id() -> str:
    """The app's bundle id, shared with Sign in with Apple."""
    from . import apple
    return apple.bundle_id()


def product_ids() -> List[str]:
    """The subscriptions that are Pro, in the order the app shows them."""
    raw = (os.environ.get("APPLE_PRODUCT_IDS") or "").strip()
    names = re.split(r"[\s,]+", raw) if raw else list(DEFAULT_PRODUCTS)
    out: List[str] = []
    for name in names:
        if _PRODUCT_ID.match(name) and name not in out:
            out.append(name)
    return out


def sandbox_policy() -> str:
    """all, allowlist or none: who test purchases give Pro to."""
    value = _setting("APPLE_SANDBOX_PRO", "all").lower()
    return value if value in SANDBOX_POLICIES else "all"


def sandbox_users() -> set:
    """The accounts, by email, that test purchases give Pro under allowlist."""
    return {part.strip().lower()
            for part in re.split(r"[\s,;]+", os.environ.get("APPLE_SANDBOX_USERS") or "")
            if part.strip()}


def describe() -> str:
    """One line for the start-up log."""
    if not enabled():
        if _setting("APPLE_APP_ID"):
            return ("off: APPLE_APP_ID must be the app's number from App Store "
                    "Connect, digits only")
        return "off: APPLE_APP_ID is not set"
    policy = sandbox_policy()
    said = {"all": "test purchases give Pro to anyone who makes one",
            "allowlist": "test purchases give Pro only to the %d accounts in "
                         "APPLE_SANDBOX_USERS" % len(sandbox_users()),
            "none": "test purchases give nobody Pro"}[policy]
    typed = _setting("APPLE_SANDBOX_PRO")
    if typed and typed.lower() not in SANDBOX_POLICIES:
        said += " (APPLE_SANDBOX_PRO is not all, allowlist or none)"
    return "on for %s; %s" % (", ".join(product_ids()) or "no products", said)


_VERIFIER: Dict[Tuple[str, Optional[int]], Verifier] = {}
_VERIFIER_LOCK = threading.Lock()


def verifier() -> Verifier:
    """The checker for this app's signed data, built from the settings.

    Production and Sandbox both, against Apple's pinned root, with every
    strict check on. Only called while in-app purchase is on.
    """
    key = (bundle_id(), app_id())
    with _VERIFIER_LOCK:
        found = _VERIFIER.get(key)
        if found is None:
            found = Verifier(key[0], key[1])
            _VERIFIER.clear()
            _VERIFIER[key] = found
        return found


# --- the tables ----------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def prepare(conn: sqlite3.Connection) -> None:
    """At start-up: the tables, and every Apple subscriber's Pro worked out
    again, so a change to APPLE_SANDBOX_PRO or its list takes effect at once.

    users.apple_until must exist already; accounts.ensure_schema adds it.
    """
    ensure_schema(conn)
    _write(conn, _refresh_everyone_locked)


def _write(conn: sqlite3.Connection, work: Callable[[sqlite3.Connection], Any]):
    """Run work(conn) in one write transaction, so concurrent deliveries take turns."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = work(conn)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- the appAccountToken ---------------------------------------------------------

def _canonical_token(value: Any) -> str:
    """A UUID in one spelling (StoreKit's uuidString is upper case), or ''."""
    if not isinstance(value, str) or len(value) > 64:
        return ""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return ""


def account_token(conn: sqlite3.Connection, user_id: int) -> str:
    """This person's appAccountToken: made once, at random, and kept.

    Random rather than derived from the account id or email, so the token says
    nothing about the person and cannot be guessed. The same one goes with
    every purchase, so renewals and restores always point back here.
    """
    ensure_schema(conn)
    for _attempt in range(3):
        row = conn.execute("SELECT token FROM apple_tokens WHERE user_id=?",
                           (user_id,)).fetchone()
        if row:
            return row[0]
        # OR IGNORE: two pages asking at once make one token between them.
        conn.execute("INSERT OR IGNORE INTO apple_tokens (token, user_id, created) "
                     "VALUES (?, ?, ?)", (str(uuid.uuid4()), user_id, int(time.time())))
        conn.commit()
    raise RuntimeError("Could not make an App Store account token.")


def _user_for_token(conn: sqlite3.Connection, token: str) -> Optional[int]:
    if not token:
        return None
    row = conn.execute("SELECT user_id FROM apple_tokens WHERE token=?", (token,)).fetchone()
    return int(row[0]) if row else None


# --- who has Pro, and until when -------------------------------------------------

def _counted(conn: sqlite3.Connection, user_id: int) -> Tuple[str, ...]:
    """The environments whose subscriptions give this person Pro."""
    policy = sandbox_policy()
    if policy == "all":
        return (PRODUCTION, SANDBOX)
    if policy == "allowlist":
        row = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        if row is not None and (row[0] or "").lower() in sandbox_users():
            return (PRODUCTION, SANDBOX)
    return (PRODUCTION,)


def _until_locked(conn: sqlite3.Connection, user_id: int) -> int:
    counted = _counted(conn, user_id)
    best = 0
    for env, expires, grace, revoked in conn.execute(
            "SELECT environment, expires_ms, grace_until_ms, revoked_ms FROM apple_subs "
            "WHERE user_id=?", (user_id,)):
        if env not in counted or revoked:
            continue
        best = max(best, int(expires), int(grace))
    return best


def pro_until_ms(conn: sqlite3.Connection, user_id: int) -> int:
    """When this person's Pro through Apple ends, in ms; 0 for none.

    Compare it with now. Test purchases count as APPLE_SANDBOX_PRO says.
    """
    ensure_schema(conn)
    return _until_locked(conn, user_id)


def _refresh_locked(conn: sqlite3.Connection, user_id: int) -> None:
    """users.apple_until for one person, in seconds, rounded up."""
    until = _until_locked(conn, user_id)
    conn.execute("UPDATE users SET apple_until=? WHERE id=?",
                 ((until + 999) // 1000, user_id))


def _refresh_everyone_locked(conn: sqlite3.Connection) -> int:
    people = {int(r[0]) for r in conn.execute(
        "SELECT DISTINCT user_id FROM apple_subs WHERE user_id IS NOT NULL")}
    people |= {int(r[0]) for r in conn.execute(
        "SELECT id FROM users WHERE apple_until<>0")}
    for user_id in people:
        _refresh_locked(conn, user_id)
    return len(people)


def subscription(conn: sqlite3.Connection, user_id: int,
                 counted_only: bool = True) -> Optional[Dict[str, Any]]:
    """This person's Apple subscription, for the pages, or None.

    counted_only: only one that gives Pro now. Otherwise any Apple could
    still charge for: running, or in billing retry, whether or not test
    purchases count. Of several, the one that lasts longest.
    """
    ensure_schema(conn)
    counted = _counted(conn, user_id)
    now = _now_ms()
    best: Optional[Dict[str, Any]] = None
    for row in conn.execute(
            "SELECT environment, product_id, expires_ms, grace_until_ms, revoked_ms, "
            "auto_renew, status FROM apple_subs WHERE user_id=?", (user_id,)):
        env, product, expires, grace, revoked, auto_renew, status = row
        if revoked or (counted_only and env not in counted):
            continue
        until = max(int(expires), int(grace))
        retrying = (int(status) == STATUS_RETRY and int(expires) > now - RETRY_DAYS * DAY_MS)
        if until <= now and (counted_only or not retrying):
            continue
        found = {"until": (until + 999) // 1000, "product": product,
                 "test": env == SANDBOX, "renews": int(auto_renew) != 0,
                 # A renewal failed and Pro is on only until Apple stops
                 # waiting for the payment: until is that day, not a renewal.
                 "grace": int(status) == STATUS_GRACE and int(grace) > int(expires),
                 "retrying": until <= now}
        if best is None or found["until"] > best["until"]:
            best = found
    return best


def may_still_charge(conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether Apple may yet charge this person for Pro that has ended.

    A renewal failed, Apple is still retrying it (for up to RETRY_DAYS), and
    renewing has not been turned off: once the payment goes through, Pro is
    back and billed. For checkout on the website, so nobody pays Stripe and
    then Apple for the same Pro. Test purchases cost nothing and are left out.
    """
    ensure_schema(conn)
    now = _now_ms()
    for expires, grace, auto_renew, status in conn.execute(
            "SELECT expires_ms, grace_until_ms, auto_renew, status FROM apple_subs "
            "WHERE user_id=? AND environment=? AND revoked_ms=0", (user_id, PRODUCTION)):
        if (int(status) == STATUS_RETRY and int(auto_renew) != 0
                and max(int(expires), int(grace)) <= now
                and int(expires) > now - RETRY_DAYS * DAY_MS):
            return True
    return False


# --- applying what Apple signed --------------------------------------------------

def _bought_anew(row: sqlite3.Row, tx: Dict[str, Any], purchase: int) -> bool:
    """Whether tx is someone paying again for a subscription that had run out.

    A resubscribe keeps the originalTransactionId, so on an Apple ID shared
    by two Orenth accounts it lands on the row the first one owned. Apple
    marks it a PURCHASE, made after the stored period, grace or refund ended.
    A renewal, a billing retry that worked, and an upgrade of a running
    subscription are not: those stay with the owner, as a move does.
    """
    ended = max(row["expires_ms"], row["grace_until_ms"])
    if row["revoked_ms"]:
        ended = min(ended, row["revoked_ms"])
    return (tx.get("transactionReason") == "PURCHASE" and not tx.get("isUpgraded")
            and purchase > row["purchase_ms"] and ended <= purchase)


def _apply_locked(conn: sqlite3.Connection, env: str, tx: Dict[str, Any],
                  products: Iterable[str], from_app_user: Optional[int] = None,
                  note: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Apply one verified transaction inside an open write transaction.

    note is the verified notification it came with (None when the app sent
    it); from_app_user is who sent it from the app (None for Apple's server).
    Returns (outcome, originalTransactionId).
    """
    otid, txid = tx.get("originalTransactionId"), tx.get("transactionId")
    purchase, expires = _int(tx.get("purchaseDate")), _int(tx.get("expiresDate"))
    if (tx.get("type") != AUTO_RENEWABLE or tx.get("productId") not in set(products)
            or not _text_id(otid) or not _text_id(txid) or not purchase or not expires):
        return IGNORED, otid[:64] if isinstance(otid, str) else ""

    row = conn.execute("SELECT * FROM apple_subs WHERE environment=? AND original_tx_id=?",
                       (env, otid)).fetchone()
    if row is None and tx.get("isUpgraded"):
        return IGNORED, otid

    token = _canonical_token(tx.get("appAccountToken"))
    token_user = _user_for_token(conn, token)
    # Who owns it: a stored owner stays, unless the subscription had run out
    # and this is someone paying for it again, signed in to another account;
    # otherwise the token says; otherwise the signed-in person who sent it
    # from the app (a purchase made with no token, such as an offer code
    # redeemed in the App Store).
    previous: Optional[int] = None
    if row is not None and row["user_id"] is not None:
        owner: Optional[int] = int(row["user_id"])
        if token_user is not None and token_user != owner and _bought_anew(row, tx, purchase):
            previous, owner = owner, token_user
    elif token_user is not None:
        owner = token_user
    else:
        owner = from_app_user

    data = (note or {}).get("data") or {}
    status = data.get("status") if _int(data.get("status")) else None
    renewal = (note or {}).get("renewal") or {}
    note_ms = _int((note or {}).get("signedDate"))
    tx_ms = _int(tx.get("signedDate"))
    grace = _int(renewal.get("gracePeriodExpiresDate")) if status == STATUS_GRACE else 0
    now = int(time.time())

    if row is None:
        conn.execute(
            "INSERT INTO apple_subs (environment, original_tx_id, user_id, product_id, "
            "transaction_id, purchase_ms, expires_ms, revoked_ms, tx_signed_ms, "
            "grace_until_ms, auto_renew, status, note_signed_ms, app_token, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (env, otid, owner, tx["productId"], txid, purchase, expires,
             _int(tx.get("revocationDate")), tx_ms, grace,
             _int(renewal["autoRenewStatus"]) if "autoRenewStatus" in renewal else -1,
             status or 0, note_ms, token, now))
        changed = True
    else:
        fields: Dict[str, Any] = {}
        same = txid == row["transaction_id"]
        newer = purchase > row["purchase_ms"]
        # A newer purchase is a renewal: always forward. The same transaction
        # only from a copy Apple signed no earlier than the one stored, so a
        # copy from before a refund cannot undo it.
        if not tx.get("isUpgraded") and (newer or (same and tx_ms >= row["tx_signed_ms"])):
            fields.update(product_id=tx["productId"], transaction_id=txid,
                          purchase_ms=purchase, expires_ms=expires,
                          revoked_ms=_int(tx.get("revocationDate")), tx_signed_ms=tx_ms)
            if token:
                fields["app_token"] = token
            if newer:
                # A new period was bought or renewed: an earlier grace period is over.
                fields.update(status=0, grace_until_ms=0)
        current = fields.get("transaction_id", row["transaction_id"]) == txid
        if note is not None and current and note_ms >= row["note_signed_ms"]:
            fields.update(status=status or 0, note_signed_ms=note_ms, grace_until_ms=grace)
            if "autoRenewStatus" in renewal:
                fields["auto_renew"] = _int(renewal["autoRenewStatus"])
        changed = bool(fields)
        if (row["user_id"] is None or previous is not None) and owner is not None:
            fields["user_id"] = owner
        if fields:
            fields["updated"] = now
            conn.execute("UPDATE apple_subs SET %s WHERE environment=? AND original_tx_id=?"
                         % ", ".join("%s=?" % k for k in fields),
                         list(fields.values()) + [env, otid])

    if owner is not None:
        _refresh_locked(conn, owner)
    if previous is not None:
        _refresh_locked(conn, previous)
    if from_app_user is not None and owner is not None and owner != from_app_user:
        return OTHER_ACCOUNT, otid
    if owner is None:
        return UNOWNED, otid
    return (APPLIED if changed else STALE), otid


def handle_notification(conn: sqlite3.Connection, verifier: Verifier, signed_payload: str,
                        products: Iterable[str]) -> Tuple[int, str]:
    """An App Store Server Notification V2. Returns (HTTP status for Apple, outcome).

    200 for anything handled, including duplicates and types that change
    nothing, and only once it is committed. 400 when the signature or the app
    does not check out: Apple then retries (1, 12, 24, 48 and 72 hours later,
    production only), which leaves time to fix a wrong setting. A database
    error propagates, so the caller answers 500 and Apple retries.
    """
    ensure_schema(conn)
    try:
        note = verifier.notification(signed_payload)
    except VerificationError as exc:
        return 400, exc.status
    products = list(products)

    def work(c: sqlite3.Connection) -> str:
        if c.execute("SELECT 1 FROM apple_notifications WHERE uuid=?",
                     (note["notificationUUID"],)).fetchone():
            return DUPLICATE
        if note.get("transaction"):
            outcome, otid = _apply_locked(c, note["environment"], note["transaction"],
                                          products, note=note)
        else:
            outcome, otid = IGNORED, ""     # TEST, summaries, external purchase tokens
        c.execute("INSERT INTO apple_notifications (uuid, type, subtype, environment, "
                  "original_tx_id, signed_ms, received, outcome) VALUES (?,?,?,?,?,?,?,?)",
                  (note["notificationUUID"], note["notificationType"],
                   str(note.get("subtype") or ""), note["environment"], otid,
                   note["signedDate"], int(time.time()), outcome))
        return outcome

    return 200, _write(conn, work)


def handle_app_transactions(conn: sqlite3.Connection, verifier: Verifier, user_id: int,
                            jws_list: List[str], products: Iterable[str]) -> List[Dict[str, Any]]:
    """Transactions the signed-in person's app sent (purchase, restore, page load).

    Each comes back with its outcome. The app finishes a StoreKit transaction
    once it has an answer here other than invalid, so nothing is lost if
    this request fails.
    """
    ensure_schema(conn)
    products = list(products)
    results: List[Dict[str, Any]] = []
    for jws in jws_list[:20]:
        try:
            tx = verifier.transaction(jws)
        except VerificationError as exc:
            results.append({"outcome": INVALID, "reason": exc.status})
            continue
        outcome, _ = _write(conn, lambda c, tx=tx: _apply_locked(
            c, tx["environment"], tx, products, user_id))
        results.append({"outcome": outcome,
                        "transactionId": tx.get("transactionId") if _text_id(tx.get("transactionId")) else "",
                        "environment": tx["environment"]})
    return results


def move_to(conn: sqlite3.Connection, verifier: Verifier, user_id: int, jws: str,
            products: Iterable[str]) -> Dict[str, Any]:
    """Move an Apple subscription to this account, on purpose.

    The transaction must be the subscription's current one, still running,
    read by this person's iPhone from its own entitlements: so it is someone
    signed in on a phone with that Apple ID asking. A subscription moves at
    most once in MOVE_EVERY_DAYS, except back to the account it was moved
    away from, which may be the one that pays and is told by email how to take
    it back. The account that gave it up then waits as anyone does, so a
    subscription cannot be passed to and fro. Returns {"outcome", "previous"}: previous
    is the account it moved away from, to be told, or None. Raises
    VerificationError for anything Apple did not sign for this app.
    """
    ensure_schema(conn)
    products = list(products)
    tx = verifier.transaction(jws)
    env = tx["environment"]

    def work(c: sqlite3.Connection) -> Dict[str, Any]:
        outcome, otid = _apply_locked(c, env, tx, products, user_id)
        if outcome == IGNORED:
            return {"outcome": IGNORED, "previous": None}
        row = c.execute("SELECT * FROM apple_subs WHERE environment=? AND original_tx_id=?",
                        (env, otid)).fetchone()
        now = _now_ms()
        if row is None or row["transaction_id"] != tx.get("transactionId") \
                or row["revoked_ms"] \
                or max(row["expires_ms"], row["grace_until_ms"]) <= now:
            return {"outcome": NOT_CURRENT, "previous": None}
        previous = row["user_id"]
        if previous == user_id:
            return {"outcome": ALREADY_HERE, "previous": None}
        going_back = row["moved_from"] == user_id
        if previous is not None and row["moved_ms"] and not going_back \
                and now - row["moved_ms"] < MOVE_EVERY_DAYS * DAY_MS:
            return {"outcome": TOO_SOON, "previous": None,
                    "after": (row["moved_ms"] + MOVE_EVERY_DAYS * DAY_MS) // 1000}
        # Back home, moved_from goes on naming home, not the account it left,
        # or that one could take it straight back in its turn.
        c.execute("UPDATE apple_subs SET user_id=?, moved_from=?, moved_ms=?, updated=? "
                  "WHERE environment=? AND original_tx_id=?",
                  (user_id, user_id if going_back else previous, now,
                   int(time.time()), env, otid))
        _refresh_locked(c, user_id)
        if previous is not None:
            _refresh_locked(c, int(previous))
        return {"outcome": MOVED, "previous": int(previous) if previous is not None else None}

    return _write(conn, work)


# --- housekeeping ----------------------------------------------------------------

def forget_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Account deletion: the person's token goes, and their subscriptions are
    linked to no account.

    A subscription's row stays, unlinked, until its time has run out. A copy
    of a transaction verifies for ever (bot/appstore.py), and the row is what
    remembers a refund: without it, a copy taken before the refund, sent
    from any new account, would give Pro again for the whole period. Once
    the time has run out no copy can give Pro, so a row past it goes now,
    and prune_unowned() takes the rest when their time comes.

    Apple keeps billing until the person cancels with Apple. Renewals then
    arrive with nobody to own them, and follow the person if they restore
    their purchases into a new account. The caller has made sure the tables
    exist, and commits: making them here would commit its half-done deletion.
    """
    conn.execute("DELETE FROM apple_tokens WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM apple_subs WHERE user_id=? AND MAX(expires_ms, grace_until_ms)<=?",
                 (user_id, _now_ms()))
    conn.execute("UPDATE apple_subs SET user_id=NULL, app_token='', moved_from=NULL, updated=? "
                 "WHERE user_id=?", (int(time.time()), user_id))
    conn.execute("UPDATE apple_subs SET moved_from=NULL WHERE moved_from=?", (user_id,))


def forget_test_purchases(conn: sqlite3.Connection, user_id: int) -> int:
    """Put one account back where it was before its test purchases.

    For the App Review account, which a reviewer leaves with Pro bought in
    Apple's sandbox: the next reviewer must find something to buy. Only its
    Sandbox subscriptions go. Unless it has real ones, it also gets a new
    appAccountToken, so a test subscription still renewing in the sandbox
    cannot find its way back. Returns how many were forgotten.
    """
    ensure_schema(conn)

    def work(c: sqlite3.Connection) -> int:
        cur = c.execute("DELETE FROM apple_subs WHERE user_id=? AND environment=?",
                        (user_id, SANDBOX))
        if c.execute("SELECT 1 FROM apple_subs WHERE user_id=? LIMIT 1",
                     (user_id,)).fetchone() is None:
            c.execute("DELETE FROM apple_tokens WHERE user_id=?", (user_id,))
        _refresh_locked(c, user_id)
        return cur.rowcount

    return _write(conn, work)


def prune_notifications(conn: sqlite3.Connection,
                        days: int = KEEP_NOTIFICATIONS_DAYS) -> int:
    """Forget notifications Apple can no longer be retrying."""
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM apple_notifications WHERE received < ?",
                       (int(time.time()) - days * 86400,))
    conn.commit()
    return cur.rowcount


def prune_unowned(conn: sqlite3.Connection) -> int:
    """Forget subscriptions no account owns once their time, grace included,
    has run out: no copy of them can give Pro any more. A deleted account's
    end here. A renewal or a restore after that is taken as new."""
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM apple_subs WHERE user_id IS NULL "
                       "AND MAX(expires_ms, grace_until_ms)<=?", (_now_ms(),))
    conn.commit()
    return cur.rowcount
