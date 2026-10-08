"""Invite links: a friend who joins through one gets a week of Pro, and so
does the person who sent it.

The week is a date on the account, not a Stripe subscription, so nobody is
asked for a card and nothing renews. Weeks stack: two friends, two weeks.

The reward is paid when the friend's account is created through the link,
so it could be farmed with throwaway accounts. Each of those needs its own
Google account or inbox, and an inviter's reward stops after MAX_REWARDS, so
the most that farming can win is a few free weeks.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import time
from typing import Optional

BONUS_DAYS = 7
MAX_REWARDS = 10
# An account this new counts as having joined through the link it came from.
FRESH = 30 * 60

_CODE = re.compile(r"^[0-9a-f]{8}$")


def code_for(conn: sqlite3.Connection, user_id: int) -> str:
    """This person's invite code, made the first time it is asked for."""
    row = conn.execute("SELECT invite_code FROM users WHERE id=?",
                       (user_id,)).fetchone()
    if row and row["invite_code"]:
        return row["invite_code"]
    while True:
        code = secrets.token_hex(4)
        try:
            conn.execute("UPDATE users SET invite_code=? WHERE id=?",
                         (code, user_id))
            conn.commit()
            return code
        except sqlite3.IntegrityError:      # taken; vanishingly rare
            continue


def inviter(conn: sqlite3.Connection, code: str) -> Optional[int]:
    code = (code or "").strip().lower()
    if not _CODE.match(code):
        return None
    row = conn.execute("SELECT id FROM users WHERE invite_code=?",
                       (code,)).fetchone()
    return int(row["id"]) if row else None


def joined(conn: sqlite3.Connection, user_id: int) -> int:
    """How many people have joined through this person's link."""
    return conn.execute("SELECT COUNT(*) FROM users WHERE invited_by=?",
                        (user_id,)).fetchone()[0]


def _add_week(conn: sqlite3.Connection, user_id: int, now: int) -> None:
    conn.execute("UPDATE users SET bonus_until=MAX(bonus_until, ?) + ? "
                 "WHERE id=?", (now, BONUS_DAYS * 86400, user_id))


def accept(conn: sqlite3.Connection, user_id: int, code: str,
           now: Optional[float] = None) -> bool:
    """Credit an invite to someone who has just joined. True if it counted.

    Only a brand-new account counts, only once, and never your own link.
    """
    now = int(now or time.time())
    who = inviter(conn, code)
    row = conn.execute("SELECT created_ts, invited_by, provider, is_local "
                       "FROM users WHERE id=?", (user_id,)).fetchone()
    if who is None or row is None or who == user_id or row["invited_by"] \
            or row["is_local"] or row["provider"] == "demo" \
            or now - int(row["created_ts"]) > FRESH:
        return False
    conn.execute("UPDATE users SET invited_by=? WHERE id=?", (who, user_id))
    _add_week(conn, user_id, now)
    if joined(conn, who) <= MAX_REWARDS:
        _add_week(conn, who, now)
    conn.commit()
    return True
