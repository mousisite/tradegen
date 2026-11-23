"""Who owns what.

Sign-in is the easy part of this. The hard part is that every row in the
database was written by a single-user app that assumed there was only ever one
person, so adding a login without giving those rows an owner would mean two
people signing in and reading each other's trades. That is worse than no login
at all, so ownership comes first and authentication is layered on top.

What is personal and what is shared is a deliberate split:

  personal   trades, watchlist, alerts, theses, and the analysis history
  shared     the measured strategy record

The strategy record is a set of measurements about how a strategy behaves on an
instrument. That is a fact about the market, not about the person who happened
to run the analysis, and every analysis anyone runs makes it better for
everyone. Keeping it shared is the one place where more users make the product
itself smarter rather than merely larger.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# Tables that belong to one person. Everything else is shared or derived.
# In the order they can be deleted: a trade opened from an analysis points at
# its run, so trades go before runs or the database refuses the delete.
OWNED_TABLES = ("trades", "runs", "watchlist", "alerts", "theses")

# The account the app uses when nobody has signed in, which is how it behaves
# on your own machine with no credentials configured.
LOCAL_EMAIL = "local@localhost"
LOCAL_NAME = "This computer"

SESSION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    picture       TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT 'local',
    provider_id   TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL DEFAULT 0,
    is_local      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_ts  INTEGER NOT NULL,
    expires_ts  INTEGER NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id   INTEGER PRIMARY KEY,
    data      TEXT NOT NULL,
    updated   INTEGER NOT NULL
);

-- A way into an account besides the one it was made with, so far only Apple.
-- users.provider and provider_id keep naming how the account was made, as
-- older versions of the app read them. The refresh token is only kept so that
-- deleting the account can ask Apple to end it, and it is useless without
-- this site's .p8 key, which is never in the database.
-- When a person agreed to their chart screenshots being sent to Anthropic
-- to be read. Asked once, before the first picture leaves, because the App
-- Store and plain fairness both want an explicit yes before anything a
-- person uploads goes to another company's AI. Withdrawn by deleting the row.
CREATE TABLE IF NOT EXISTS ai_consents (
    user_id  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    ts       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    subject           TEXT NOT NULL,
    email             TEXT NOT NULL DEFAULT '',
    is_private_email  INTEGER NOT NULL DEFAULT 0,
    refresh_token     TEXT NOT NULL DEFAULT '',
    client_id         TEXT NOT NULL DEFAULT '',
    created_ts        INTEGER NOT NULL,
    last_used_ts      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (provider, subject)
);

CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);

-- A way in that was taken off an account, as when someone stops using Sign
-- in with Apple here: only which account and which id. An address joins an
-- account only if no other id from that provider was ever tied to it, and
-- this is what remembers "ever" once the link itself is gone.
CREATE TABLE IF NOT EXISTS former_identities (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    ended_ts    INTEGER NOT NULL,
    PRIMARY KEY (user_id, provider, subject)
);

-- A sign-in finished in the iPhone app's browser sheet, waiting for the app's
-- own web view to collect it. Only the code's hash is kept.
CREATE TABLE IF NOT EXISTS app_handoffs (
    code_hash   TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    challenge   TEXT NOT NULL,
    next_url    TEXT NOT NULL DEFAULT '',
    created_ts  INTEGER NOT NULL,
    used_ts     INTEGER NOT NULL DEFAULT 0
);
"""

# The settings that are a person's own: their account, their risk, their costs
# and the thresholds they want calls made at. On a deployment these used to be
# one shared file, so anyone who signed in changed them for everybody. The
# settings that drive the server itself, like how often alerts are checked,
# stay global and are the operator's alone.
PERSONAL_SETTINGS = (
    "account_size", "risk_per_trade_pct", "max_position_pct",
    "cost_bps_equity", "cost_bps_crypto", "stop_atr_multiple",
    "target_atr_multiple", "min_conviction", "strong_conviction",
    "horizon_bars", "interval", "allow_shorts", "require_positive_expectancy",
    # A person's own futures cost, the journal's daily rules and how they
    # like their candles drawn.
    "cost_bps_futures", "daily_loss_limit_r", "max_trades_per_day",
    "last_entry_time", "calm_candles",
)


@dataclass
class User:
    id: int
    email: str
    name: str
    picture: str = ""
    provider: str = "local"
    is_local: bool = False
    plan: str = "free"
    plan_status: str = ""
    plan_until: int = 0           # when the paid period ends, 0 if not known
    bonus_until: int = 0          # free Pro from invites, until this time
    apple_until: int = 0          # Pro bought through Apple, until this time
    weekly_email: bool = False
    alert_email: bool = False
    created_ts: int = 0

    @property
    def display(self) -> str:
        return self.name or self.email.split("@")[0]

    @property
    def contact(self) -> str:
        return self.name if self.provider == "phone" else self.email

    @property
    def initial(self) -> str:
        return (self.display or "?")[0].upper()


def _row_to_user(row: sqlite3.Row) -> User:
    keys = row.keys()
    return User(id=row["id"], email=row["email"], name=row["name"],
                picture=row["picture"], provider=row["provider"],
                is_local=bool(row["is_local"]),
                plan=row["plan"] if "plan" in keys else "free",
                plan_status=row["plan_status"] if "plan_status" in keys else "",
                plan_until=row["plan_until"] if "plan_until" in keys else 0,
                bonus_until=row["bonus_until"] if "bonus_until" in keys else 0,
                apple_until=row["apple_until"] if "apple_until" in keys else 0,
                alert_email=bool(row["alert_email"]) if "alert_email" in keys
                else False,
                weekly_email=bool(row["weekly_email"]) if "weekly_email" in keys
                else False,
                created_ts=row["created_ts"] if "created_ts" in keys else 0)


# --- schema -----------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the account tables and give every existing row an owner.

    Run on every connect. The backfill happens once: rows written before there
    were accounts belong to whoever was using the machine, which is the local
    account.
    """
    conn.executescript(_SCHEMA)

    # Plan columns, added in place to databases made before there were plans.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in (("plan", "TEXT NOT NULL DEFAULT 'free'"),
                        ("plan_status", "TEXT NOT NULL DEFAULT ''"),
                        ("stripe_customer", "TEXT NOT NULL DEFAULT ''"),
                        ("plan_until", "INTEGER NOT NULL DEFAULT 0"),
                        ("invite_code", "TEXT NOT NULL DEFAULT ''"),
                        ("invited_by", "INTEGER NOT NULL DEFAULT 0"),
                        ("bonus_until", "INTEGER NOT NULL DEFAULT 0"),
                        ("weekly_email", "INTEGER NOT NULL DEFAULT 0"),
                        ("alert_email", "INTEGER NOT NULL DEFAULT 0"),
                        ("digest_week", "TEXT NOT NULL DEFAULT ''"),
                        # Pro bought in the iPhone app, worked out by
                        # bot/apple_store.py from Apple's signed data.
                        ("apple_until", "INTEGER NOT NULL DEFAULT 0")):
        if column not in have:
            conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (column, ddl))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invite ON "
                 "users(invite_code) WHERE invite_code <> ''")
                        # Free weeks given for inviting friends, counted as
                        # they are given (bot/invites.py).
                        ("invite_rewards", "INTEGER NOT NULL DEFAULT 0"),

    local = _ensure_local(conn)

    _rebuild_watchlist_if_needed(conn, local.id)

    for table in OWNED_TABLES:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if not existing:
            continue                     # table not created yet
        if "user_id" not in existing:
            # No REFERENCES clause: SQLite cannot add a column with a foreign
            # key to an existing table, and the application enforces it anyway.
            conn.execute("ALTER TABLE %s ADD COLUMN user_id INTEGER NOT NULL "
                         "DEFAULT %d" % (table, local.id))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_%s_user ON %s(user_id)"
                         % (table, table))
        else:
            # A row can still arrive unowned if an older code path wrote it.
            conn.execute("UPDATE %s SET user_id=? WHERE user_id IS NULL OR "
                         "user_id=0" % table, (local.id,))
    conn.commit()



def _rebuild_watchlist_if_needed(conn: sqlite3.Connection, local_id: int) -> None:
    """Re-key an old watchlist so two people can watch the same instrument.

    The original table made symbol the primary key, which was right for one
    user and becomes a collision the moment there are two. SQLite cannot alter
    a primary key, so the table is rebuilt. Idempotent: it checks the shape
    first and does nothing once the key is already composite.
    """
    info = list(conn.execute("PRAGMA table_info(watchlist)"))
    if not info:
        return
    by_name = {r["name"]: r for r in info}
    already = ("user_id" in by_name and by_name["user_id"]["pk"] > 0)
    if already:
        return

    # exact is the column database._migrate has just added, before this runs:
    # left out here, the rebuilt table would lack it until the next start.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_rekeyed (
            symbol     TEXT NOT NULL,
            interval   TEXT NOT NULL DEFAULT '1d',
            added_ts   INTEGER NOT NULL,
            note       TEXT,
            last_run   INTEGER,
            user_id    INTEGER NOT NULL DEFAULT 1,
            exact      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, symbol)
        )""")
    owner = "user_id" if "user_id" in by_name else str(int(local_id))
    conn.execute(
        "INSERT OR IGNORE INTO watchlist_rekeyed "
        "(symbol, interval, added_ts, note, last_run, user_id) "
        "SELECT symbol, interval, added_ts, note, last_run, %s FROM watchlist"
        % owner)
    conn.execute("DROP TABLE watchlist")
    conn.execute("ALTER TABLE watchlist_rekeyed RENAME TO watchlist")
    conn.commit()


def _ensure_local(conn: sqlite3.Connection) -> User:
    row = conn.execute("SELECT * FROM users WHERE is_local=1 LIMIT 1").fetchone()
    if row is not None:
        return _row_to_user(row)
    now = int(time.time())
    cur = conn.execute(
        "INSERT OR IGNORE INTO users (email, name, provider, created_ts, "
        "last_seen_ts, is_local) VALUES (?,?,?,?,?,1)",
        (LOCAL_EMAIL, LOCAL_NAME, "local", now, now))
    if not cur.lastrowid:
        row = conn.execute("SELECT * FROM users WHERE email=?",
                           (LOCAL_EMAIL,)).fetchone()
        return _row_to_user(row)
    conn.commit()
    return _row_to_user(conn.execute("SELECT * FROM users WHERE id=?",
                                     (cur.lastrowid,)).fetchone())


def local_user(conn: sqlite3.Connection) -> User:
    """The account used when authentication is switched off."""
    return _ensure_local(conn)


# --- users ------------------------------------------------------------------

def upsert_google_user(conn: sqlite3.Connection, profile: Dict) -> User:
    """Create or update the account behind a verified Google profile.

    The email is the identity, but the Google subject id is what is actually
    matched on where one is already known: an email address can be reassigned
    within a workspace, and the subject id cannot. It is looked for on the
    account Google made or took over, then among the ways in linked to an
    account Apple made.
    """
    email = (profile.get("email") or "").strip().lower()
    if not email:
        raise ValueError("Google did not return an email address.")
    if not profile.get("email_verified", True):
        raise ValueError("That Google account has an unverified email address.")

    sub = str(profile.get("sub") or "")
    name = (profile.get("name") or "").strip()
    picture = (profile.get("picture") or "").strip()
    now = int(time.time())

    row = link = None
    if sub:
        row = conn.execute("SELECT * FROM users WHERE provider='google' AND "
                           "provider_id=?", (sub,)).fetchone()
        if row is None:
            link = conn.execute("SELECT * FROM identities WHERE provider='google' "
                                "AND subject=?", (sub,)).fetchone()
            if link is not None:
                row = conn.execute("SELECT * FROM users WHERE id=?",
                                   (link["user_id"],)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Matching by address is how an account made with an emailed code, or
        # with Apple, is joined to Google. An account ever tied to another
        # Google identity is different: its address has been given to someone
        # new, and the old owner's journal is not theirs to take over.
        if row is not None and sub and _tied_to_another(conn, row, "google", sub):
            raise ValueError("That email address belongs to a different Google "
                             "account here. Sign in with that one instead.")

    if row is None:
        cur = conn.execute(
            "INSERT INTO users (email, name, picture, provider, provider_id, "
            "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,?,0)",
            (email, name, picture, "google", sub, now, now))
        conn.commit()
        user_id = int(cur.lastrowid)
    elif row["provider"] == "apple" and sub:
        # An account Apple made keeps saying so: its Apple ID is how Apple's
        # notices about that Apple Account find it, and what stops another
        # Apple ID joining it by address. Google is linked beside it, as an
        # Apple ID is linked to an account Google made, and the account keeps
        # its own address.
        user_id = int(row["id"])
        conn.execute("UPDATE users SET name=?, picture=?, last_seen_ts=? WHERE id=?",
                     (name or row["name"], picture or row["picture"], now, user_id))
        if link is None:
            # OR IGNORE: two sign-ins arriving together link it once.
            conn.execute(
                "INSERT OR IGNORE INTO identities (user_id, provider, subject, email, "
                "created_ts, last_used_ts) VALUES (?,?,?,?,?,?)",
                (user_id, "google", sub, email, now, now))
        else:
            conn.execute("UPDATE identities SET email=?, last_used_ts=? WHERE id=?",
                         (email, now, link["id"]))
        conn.commit()
    else:
        user_id = int(row["id"])
        conn.execute(
            "UPDATE users SET email=?, name=?, picture=?, provider='google', "
            "provider_id=?, last_seen_ts=? WHERE id=?",
            (email, name or row["name"], picture or row["picture"], sub or
             row["provider_id"], now, user_id))
        conn.commit()

    return _row_to_user(conn.execute("SELECT * FROM users WHERE id=?",
                                     (user_id,)).fetchone())


def upsert_email_user(conn: sqlite3.Connection, email: str) -> User:
    """The account behind an email address proved by a one-time code.

    The address is the identity, as it is for Google. Someone who signed up
    with Google and later signs in with an emailed code reaches the same
    account, because both proved they control that inbox.
    """
    email = (email or "").strip().lower()
    now = int(time.time())
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (email, name, picture, provider, provider_id, "
            "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,?,0)",
            (email, "", "", "email", email, now, now))
        conn.commit()
        user_id = int(cur.lastrowid)
    else:
        if row["is_local"] or row["provider"] == "demo":
            raise ValueError("That address cannot be signed in to.")
        user_id = int(row["id"])
        conn.execute("UPDATE users SET last_seen_ts=? WHERE id=?", (now, user_id))
        conn.commit()
    return get_user(conn, user_id)


def upsert_phone_user(conn: sqlite3.Connection, phone: str) -> User:
    """The account behind a phone number proved by a texted code.

    The users table needs an email, and a phone account has none, so it gets
    one under .invalid: a top-level domain reserved never to exist, so the
    placeholder can never be mistaken for, or delivered to, a real inbox.
    """
    now = int(time.time())
    row = conn.execute("SELECT * FROM users WHERE provider='phone' AND "
                       "provider_id=?", (phone,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (email, name, picture, provider, provider_id, "
            "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,?,0)",
            ("phone%s@users.invalid" % phone.lstrip("+"), phone, "", "phone",
             phone, now, now))
        conn.commit()
        user_id = int(cur.lastrowid)
    else:
        user_id = int(row["id"])
        conn.execute("UPDATE users SET last_seen_ts=? WHERE id=?", (now, user_id))
        conn.commit()
    return get_user(conn, user_id)


# Apple's Hide My Email addresses: real, and forwarded to the person's own
# inbox, but made for this site alone, so never a reason to join two accounts.
# Apple also hands out relay addresses at icloud.com, which only the token's
# is_private_email marks as such.
APPLE_RELAY = ("@privaterelay.appleid.com", "@private.icloud.com")


def upsert_apple_user(conn: sqlite3.Connection, identity: Dict, name: str = "",
                      refresh_token: str = "", client_id: str = "") -> User:
    """The account behind a verified Apple identity, made if there is none.

    Apple's subject id is the identity: it stays the same for one person on
    this site, whatever their address. It is looked for in three places, in
    order: the link made last time; the account Apple itself made here, which
    remembers the subject, so someone who took their consent back and signs in
    again reaches it; and, only when Apple says the address is verified and it
    is not a private relay address, an account already using that address,
    unless another Apple ID was ever tied to that account. A local or example
    account is never joined. Anything else is a new account.

    The name comes from Apple only on a person's very first sign-in here, so
    it is kept then, and never written over a name already there.
    """
    sub = str(identity.get("sub") or "")
    if not sub:
        raise ValueError("Apple did not say who signed in.")
    email = (identity.get("email") or "").strip().lower()
    verified = bool(identity.get("email_verified"))
    private = bool(identity.get("is_private_email")) or email.endswith(APPLE_RELAY)
    now = int(time.time())

    if conn.in_transaction:
        conn.commit()
    # One write transaction, so two sign-ins arriving together for someone
    # new make one account between them, not two.
    conn.execute("BEGIN IMMEDIATE")
    try:
        link = conn.execute("SELECT * FROM identities WHERE provider='apple' AND "
                            "subject=?", (sub,)).fetchone()
        user_id = int(link["user_id"]) if link is not None else None
        if user_id is None:
            made = conn.execute("SELECT id FROM users WHERE provider='apple' AND "
                                "provider_id=?", (sub,)).fetchone()
            user_id = int(made["id"]) if made is not None else None
        if user_id is None and email and verified and not private:
            row = conn.execute("SELECT * FROM users WHERE email=?",
                               (email,)).fetchone()
            if row is not None and not row["is_local"] and row["provider"] != "demo":
                # Ever tied to another Apple ID: the address has since been
                # given to someone new, as with Google above.
                if _tied_to_another(conn, row, "apple", sub):
                    raise ValueError("That email address belongs to a different "
                                     "Apple account here. Sign in with that one "
                                     "instead.")
                user_id = int(row["id"])

        if user_id is None:
            address = email if email and verified else ""
            taken = address and conn.execute("SELECT 1 FROM users WHERE email=?",
                                              (address,)).fetchone()
            if not address or taken:
                # Nothing Apple vouched for, or an address this account may
                # not share. Under .invalid, like a phone account's, so it is
                # never mistaken for an inbox.
                address = "apple-%s@users.invalid" % hashlib.sha256(
                    sub.encode("utf-8")).hexdigest()[:16]
            try:
                cur = conn.execute(
                    "INSERT INTO users (email, name, picture, provider, "
                    "provider_id, created_ts, last_seen_ts, is_local) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (address, name, "", "apple", sub, now, now))
            except sqlite3.IntegrityError as exc:
                raise ValueError("That Apple account could not be given an "
                                 "account here. Try another way in.") from exc
            user_id = int(cur.lastrowid)
        else:
            if name:
                conn.execute("UPDATE users SET name=? WHERE id=? AND name=''",
                             (name, user_id))
            conn.execute("UPDATE users SET last_seen_ts=? WHERE id=?",
                         (now, user_id))

        if link is None:
            conn.execute(
                "INSERT INTO identities (user_id, provider, subject, email, "
                "is_private_email, refresh_token, client_id, created_ts, "
                "last_used_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, "apple", sub, email, 1 if private else 0,
                 refresh_token or "", client_id if refresh_token else "", now, now))
        else:
            conn.execute("UPDATE identities SET email=?, is_private_email=?, "
                         "last_used_ts=? WHERE id=?",
                         (email or link["email"], 1 if private else 0, now,
                          link["id"]))
            if refresh_token:
                conn.execute("UPDATE identities SET refresh_token=?, client_id=? "
                             "WHERE id=?", (refresh_token, client_id, link["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_user(conn, user_id)


def _tied_to_another(conn: sqlite3.Connection, row: sqlite3.Row, provider: str,
                     subject: str) -> bool:
    """Whether an account was ever tied to an id from this provider other
    than this one: the one that made it, one linked to it now, or one taken
    off it since. Only "never" lets an address bring a new id in: a person
    who signed up with Google still reaches their account with Apple, and
    one who signed up with Apple with Google, but only the first time."""
    if row["provider"] == provider and row["provider_id"] not in ("", subject):
        return True
    for table in ("identities", "former_identities"):
        if conn.execute("SELECT 1 FROM %s WHERE user_id=? AND provider=? AND "
                        "subject<>?" % table,
                        (row["id"], provider, subject)).fetchone() is not None:
            return True
    return False


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[User]:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def list_users(conn: sqlite3.Connection) -> List[User]:
    return [_row_to_user(r) for r in
            conn.execute("SELECT * FROM users ORDER BY created_ts")]


# --- more than one way in ---------------------------------------------------

_METHOD_NAMES = {"google": "Google", "apple": "Apple", "email": "Email",
                 "phone": "Phone"}


def sign_in_methods(conn: sqlite3.Connection, user: User) -> List[str]:
    """The ways into this account, as names to show: how it was made first."""
    names = []
    first = _METHOD_NAMES.get(user.provider)
    if first:
        names.append(first)
    for row in conn.execute("SELECT provider FROM identities WHERE user_id=? "
                            "ORDER BY id", (user.id,)):
        label = _METHOD_NAMES.get(row["provider"], row["provider"].capitalize())
        if label not in names:
            names.append(label)
    return names


def apple_tokens(conn: sqlite3.Connection, user_id: int) -> List[Tuple[int, str, str]]:
    """(identity id, refresh token, client id) for each Apple sign-in to end."""
    return [(int(r["id"]), r["refresh_token"], r["client_id"]) for r in conn.execute(
        "SELECT id, refresh_token, client_id FROM identities WHERE user_id=? AND "
        "provider='apple' AND refresh_token<>''", (user_id,))]


def identity_owner(conn: sqlite3.Connection, provider: str,
                   subject: str) -> Optional[sqlite3.Row]:
    """The account a sign-in method belongs to, and when it was last used."""
    return conn.execute("SELECT user_id, last_used_ts FROM identities WHERE "
                        "provider=? AND subject=?", (provider, subject)).fetchone()


def made_by(conn: sqlite3.Connection, provider: str,
            subject: str) -> Optional[sqlite3.Row]:
    """The account a sign-in method made, in identity_owner's shape, for a
    method no longer linked to it. When the account was last signed in to
    stands in for when the method was last used."""
    return conn.execute("SELECT id AS user_id, last_seen_ts AS last_used_ts "
                        "FROM users WHERE provider=? AND provider_id=?",
                        (provider, subject)).fetchone()


def can_still_sign_in(conn: sqlite3.Connection, user_id: int, provider: str,
                      subject: str) -> bool:
    """Whether an account keeps a way in once this one sign-in method is gone.

    It does when something else made it or took it over (Google, an emailed
    code, a text), when another method is linked, or when its address is a
    real inbox an emailed code or Google could still reach. A private relay
    address stops working with the Apple Account behind it.
    """
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["is_local"] or row["provider"] == "demo":
        return True
    if not (row["provider"] == provider and row["provider_id"] == subject):
        return True
    other = conn.execute("SELECT 1 FROM identities WHERE user_id=? AND NOT "
                         "(provider=? AND subject=?)",
                         (user_id, provider, subject)).fetchone()
    if other is not None:
        return True
    email = (row["email"] or "").lower()
    if email.endswith(".invalid") or email.endswith(APPLE_RELAY):
        return False
    # A relay address at icloud.com looks like a real one; Apple said which.
    link = conn.execute("SELECT email, is_private_email FROM identities WHERE "
                        "provider=? AND subject=?", (provider, subject)).fetchone()
    return not (link is not None and link["is_private_email"]
                and (link["email"] or "").lower() == email)


def unlink_identity(conn: sqlite3.Connection, provider: str, subject: str,
                    ending: Optional[Callable[[str], None]] = None) -> Optional[int]:
    """Take a sign-in method off its account, and sign that account out.

    Its stored token goes with it. Which account it was tied to is kept, so
    that the address cannot later bring another id from that provider in.
    Every session ends, because the method being withdrawn is to be treated
    as the person signing out, and nothing records which method opened which
    session. ending, when given, is called with each of those sessions'
    tokens first, for what that sign-in switched on elsewhere. Returns the
    account's id.
    """
    row = identity_owner(conn, provider, subject)
    if row is None:
        return None
    if ending is not None:
        for (token,) in conn.execute("SELECT token FROM sessions WHERE user_id=?",
                                     (row["user_id"],)).fetchall():
            ending(token)
    conn.execute("INSERT OR REPLACE INTO former_identities (user_id, provider, "
                 "subject, ended_ts) VALUES (?,?,?,?)",
                 (row["user_id"], provider, subject, int(time.time())))
    conn.execute("DELETE FROM identities WHERE provider=? AND subject=?",
                 (provider, subject))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
    conn.commit()
    return int(row["user_id"])


# --- sessions ---------------------------------------------------------------

def start_session(conn: sqlite3.Connection, user_id: int,
                  user_agent: str = "") -> str:
    """Issue an opaque session token.

    Opaque and stored, rather than a self-contained signed cookie, so that
    signing out actually ends the session everywhere instead of relying on one
    browser to throw its copy away.
    """
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_ts, expires_ts, "
        "user_agent) VALUES (?,?,?,?,?)",
        (token, int(user_id), now, now + SESSION_DAYS * 86400,
         (user_agent or "")[:200]))
    conn.execute("UPDATE users SET last_seen_ts=? WHERE id=?", (now, int(user_id)))
    conn.commit()
    return token


def user_for_session(conn: sqlite3.Connection, token: str) -> Optional[User]:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token=? AND s.expires_ts > ?",
        (token, int(time.time()))).fetchone()
    return _row_to_user(row) if row else None


def session_age(conn: sqlite3.Connection, token: str) -> Optional[int]:
    """Seconds since this session began, or None for a token that is not one."""
    if not token:
        return None
    row = conn.execute("SELECT created_ts FROM sessions WHERE token=?",
                       (token,)).fetchone()
    return int(time.time()) - int(row[0]) if row else None


def end_session(conn: sqlite3.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def end_all_sessions(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
    conn.commit()
    return cur.rowcount


def purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_ts <= ?",
                       (int(time.time()),))
    conn.commit()
    return cur.rowcount


# --- handing a sign-in to the app -------------------------------------------
#
# Google refuses to sign anyone in inside an app's web view, so the iPhone app
# signs in in the system browser sheet, which keeps cookies of its own. The
# sheet is not signed in; it is handed a one-time code for the app instead,
# through the app's own address. The app started all this with the SHA-256 of
# a secret it kept, the verifier, and only that secret makes the code work:
# another app that registered the same address and caught the code has
# nothing to use it with. The PKCE idea, applied to the last step.
#
# What it cannot do is tell which app started a sign-in. An app that opens
# the start address in a sheet of its own, with a secret of its own, is
# handed a code that its secret redeems, if the person signs in there. Only
# a return address the Orenth app alone can claim, an https Universal Link,
# closes that; DEPLOY.md lists it as a known limitation.

HANDOFF_SECONDS = 120
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def valid_challenge(challenge: str) -> bool:
    """A base64url SHA-256, as the app sends it: 43 characters."""
    return bool(_CHALLENGE.match(challenge or ""))


def challenge_of(verifier_secret: str) -> str:
    """base64url(SHA-256(verifier)), without padding, as the app computes it."""
    digest = hashlib.sha256(verifier_secret.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _handoff_key(handoff_token: str) -> str:
    return hashlib.sha256(handoff_token.encode("utf-8")).hexdigest()


def new_handoff(conn: sqlite3.Connection, user_id: int, challenge: str,
                next_url: str = "") -> str:
    """A one-time code for the app, good for two minutes. Only its hash is kept."""
    if not valid_challenge(challenge):
        raise ValueError("That sign-in was started without the app's check.")
    now = int(time.time())
    handoff_token = secrets.token_urlsafe(32)
    # Codes past their two minutes, used or not, have nothing left to do.
    conn.execute("DELETE FROM app_handoffs WHERE created_ts < ?",
                 (now - HANDOFF_SECONDS,))
    conn.execute("INSERT INTO app_handoffs (code_hash, user_id, challenge, "
                 "next_url, created_ts) VALUES (?,?,?,?,?)",
                 (_handoff_key(handoff_token), int(user_id), challenge,
                  next_url or "", now))
    conn.commit()
    return handoff_token


def redeem_handoff(conn: sqlite3.Connection, handoff_token: str,
                   verifier_secret: str) -> Optional[Tuple[int, str]]:
    """(user id, next) for a code that is real, unused, in time and comes with
    the right verifier; otherwise None.

    Any attempt uses the code up, right or wrong, so a code someone else
    caught cannot be tried against guesses, and two attempts at once cannot
    both get in: only one of them marks it used.
    """
    if not handoff_token or len(handoff_token) > 128:
        return None
    key = _handoff_key(handoff_token)
    now = int(time.time())
    row = conn.execute("SELECT * FROM app_handoffs WHERE code_hash=?",
                       (key,)).fetchone()
    if row is None:
        return None
    cur = conn.execute("UPDATE app_handoffs SET used_ts=? WHERE code_hash=? AND "
                       "used_ts=0", (now, key))
    conn.commit()
    if cur.rowcount != 1 or row["created_ts"] < now - HANDOFF_SECONDS:
        return None
    if not _VERIFIER.match(verifier_secret or ""):
        return None
    if not hmac.compare_digest(challenge_of(verifier_secret).encode("ascii"),
                               row["challenge"].encode("ascii")):
        return None
    return int(row["user_id"]), row["next_url"]


# --- sending a screenshot to Anthropic ----------------------------------------

def ai_consent(conn: sqlite3.Connection, user_id: int) -> int:
    """When this person agreed to screenshots going to Anthropic, or 0."""
    row = conn.execute("SELECT ts FROM ai_consents WHERE user_id=?",
                       (int(user_id),)).fetchone()
    return int(row["ts"]) if row else 0


def give_ai_consent(conn: sqlite3.Connection, user_id: int) -> None:
    """Record the yes, keeping the first time it was given."""
    conn.execute("INSERT OR IGNORE INTO ai_consents (user_id, ts) VALUES (?, ?)",
                 (int(user_id), int(time.time())))
    conn.commit()


def withdraw_ai_consent(conn: sqlite3.Connection, user_id: int) -> None:
    """Take the yes back: the next screenshot asks again."""
    conn.execute("DELETE FROM ai_consents WHERE user_id=?", (int(user_id),))
    conn.commit()


# --- deleting an account ----------------------------------------------------

def delete_user(conn: sqlite3.Connection, user_id: int) -> Dict[str, int]:
    """Remove a person and everything of theirs.

    Shared measurements stay, because they are not theirs to take: they
    describe instruments, not people, and were contributed to a common record.
    """
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("No account with id %d." % user_id)
    if user.is_local:
        raise ValueError("The local account cannot be deleted.")

    removed = {}
    run_ids = [r["id"] for r in
               conn.execute("SELECT id FROM runs WHERE user_id=?", (user_id,))]
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        for child in ("run_signals", "run_news"):
            cur = conn.execute("DELETE FROM %s WHERE run_id IN (%s)"
                               % (child, marks), run_ids)
            removed[child] = cur.rowcount
    # Parts of trades closed in parts point at their trade, so they go first.
    cur = conn.execute("DELETE FROM trade_exits WHERE trade_id IN "
                       "(SELECT id FROM trades WHERE user_id=?)", (user_id,))
    removed["trade_exits"] = cur.rowcount
    for table in OWNED_TABLES:
        cur = conn.execute("DELETE FROM %s WHERE user_id=?" % table, (user_id,))
        removed[table] = cur.rowcount
    conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM ai_consents WHERE user_id=?", (user_id,))
    from . import billing, codes, public
    billing.ensure_schema(conn)
    conn.execute("DELETE FROM usage_views WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM plan_events WHERE user_id=?", (user_id,))
    # Sign-in codes are filed under the address or number they went to, with
    # the network address that asked for them, so they go with the account.
    codes.ensure_schema(conn)
    targets = [user.email]
    if user.provider == "phone":
        number = conn.execute("SELECT provider_id FROM users WHERE id=?",
                              (user_id,)).fetchone()[0]
        if number:
            targets.append(number)
    cur = conn.execute("DELETE FROM login_codes WHERE target IN (%s)"
                       % ",".join("?" * len(targets)), targets)
    removed["login_codes"] = cur.rowcount
    public.ensure_schema(conn)
    conn.execute("DELETE FROM shares WHERE user_id=?", (user_id,))
    from .database import _EVENTS
    conn.executescript(_EVENTS)
    cur = conn.execute("DELETE FROM alert_events WHERE user_id=?", (user_id,))
    removed["alert_events"] = cur.rowcount
    # Other ways in, with any token kept for them, and codes still waiting
    # for the app. Both point at the account, so they go before it.
    cur = conn.execute("DELETE FROM identities WHERE user_id=?", (user_id,))
    removed["identities"] = cur.rowcount
    conn.execute("DELETE FROM former_identities WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM app_handoffs WHERE user_id=?", (user_id,))
    # The phones and browsers notifications went to. Before the users row,
    # which they point at.
    from . import push
    push.ensure_schema(conn)
    cur = conn.execute("DELETE FROM push_devices WHERE user_id=?", (user_id,))
    removed["push_devices"] = cur.rowcount
    # Pro bought through Apple: the account's token goes, and its
    # subscriptions are linked to no account, kept until their time runs out
    # so that a copy of a refunded purchase cannot give Pro again. Apple
    # keeps billing until the person cancels with Apple, which the Account
    # page says before this runs; renewals then arrive unowned.
    from . import apple_store
    apple_store.forget_user(conn, user_id)
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    return removed


def set_alert_email(conn: sqlite3.Connection, user_id: int, on: bool) -> None:
    """Whether fired alerts and crossed stops are also emailed to this person."""
    ensure_schema(conn)
    conn.execute("UPDATE users SET alert_email=? WHERE id=?", (1 if on else 0, user_id))
    conn.commit()


def settings_for(conn: sqlite3.Connection, user_id: int) -> Dict:
    """One person's saved settings, or nothing if they never changed any."""
    row = conn.execute("SELECT data FROM user_settings WHERE user_id=?",
                       (user_id,)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in PERSONAL_SETTINGS}


def save_settings(conn: sqlite3.Connection, user_id: int, values: Dict) -> None:
    """Store a person's own settings. Anything not personal is ignored."""
    clean = {k: v for k, v in values.items() if k in PERSONAL_SETTINGS}
    conn.execute(
        "INSERT INTO user_settings (user_id, data, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, "
        "updated=excluded.updated",
        (user_id, json.dumps(clean), int(time.time())))
    conn.commit()


def clear_settings(conn: sqlite3.Connection, user_id: int) -> None:
    """Back to the app's defaults for this person."""
    conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
    conn.commit()


def owned_counts(conn: sqlite3.Connection, user_id: int) -> Dict[str, int]:
    """How much of the database belongs to one person."""
    out = {}
    for table in OWNED_TABLES:
        out[table] = conn.execute(
            "SELECT COUNT(*) FROM %s WHERE user_id=?" % table,
            (user_id,)).fetchone()[0]
    return out
