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

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

# Tables that belong to one person. Everything else is shared or derived.
OWNED_TABLES = ("runs", "trades", "watchlist", "alerts", "theses")

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
)


@dataclass
class User:
    id: int
    email: str
    name: str
    picture: str = ""
    provider: str = "local"
    is_local: bool = False

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
    return User(id=row["id"], email=row["email"], name=row["name"],
                picture=row["picture"], provider=row["provider"],
                is_local=bool(row["is_local"]))


# --- schema -----------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the account tables and give every existing row an owner.

    Run on every connect. The backfill happens once: rows written before there
    were accounts belong to whoever was using the machine, which is the local
    account.
    """
    conn.executescript(_SCHEMA)

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_rekeyed (
            symbol     TEXT NOT NULL,
            interval   TEXT NOT NULL DEFAULT '1d',
            added_ts   INTEGER NOT NULL,
            note       TEXT,
            last_run   INTEGER,
            user_id    INTEGER NOT NULL DEFAULT 1,
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
    within a workspace, and the subject id cannot.
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

    row = None
    if sub:
        row = conn.execute("SELECT * FROM users WHERE provider='google' AND "
                           "provider_id=?", (sub,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    if row is None:
        cur = conn.execute(
            "INSERT INTO users (email, name, picture, provider, provider_id, "
            "created_ts, last_seen_ts, is_local) VALUES (?,?,?,?,?,?,?,0)",
            (email, name, picture, "google", sub, now, now))
        conn.commit()
        user_id = int(cur.lastrowid)
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


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[User]:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def list_users(conn: sqlite3.Connection) -> List[User]:
    return [_row_to_user(r) for r in
            conn.execute("SELECT * FROM users ORDER BY created_ts")]


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
    for table in OWNED_TABLES:
        cur = conn.execute("DELETE FROM %s WHERE user_id=?" % table, (user_id,))
        removed[table] = cur.rowcount
    conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    return removed


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
