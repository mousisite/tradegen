"""Invite links: a friend who joins through one gets a week of Pro, and so
does the person who sent it.

The week is a date on the account, not a Stripe subscription, so nobody is
asked for a card and nothing renews. Weeks stack: two friends, two weeks.

The reward is paid when the friend's account is created through the link,
so it could be farmed with throwaway accounts. Each of those needs its own
Google account or inbox, since aliases of one inbox (me+1@, m.e@ on Gmail)
earn nothing, and an inviter is rewarded MAX_REWARDS times in all. Rewards are
counted on the inviter's own account as they are given (users.invite_rewards),
so a friend who deletes their account and joins again uses up another one.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import time
from typing import Optional

from . import codes

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


def _inbox_taken(conn: sqlite3.Connection, user_id: int, email: str) -> bool:
    """Whether another account here already reaches this address's inbox."""
    inbox = codes.inbox_of(email)
    return any(codes.inbox_of(other["email"]) == inbox for other in
               conn.execute("SELECT email FROM users WHERE id<>?", (user_id,)))


def accept(conn: sqlite3.Connection, user_id: int, code: str,
           now: Optional[float] = None) -> bool:
    """Credit an invite to someone who has just joined. True if it counted.

    Only a brand-new account counts, only once, and never your own link. A
    new address for an inbox that already has an account is not a new
    person: that covers inviting yourself as me+1@ and an inviter's dozen
    aliases alike.
    """
    now = int(now or time.time())
    who = inviter(conn, code)
    row = conn.execute("SELECT created_ts, invited_by, provider, is_local, email "
                       "FROM users WHERE id=?", (user_id,)).fetchone()
    if who is None or row is None or who == user_id or row["invited_by"] \
            or row["is_local"] or row["provider"] == "demo" \
            or now - int(row["created_ts"]) > FRESH \
            or _inbox_taken(conn, user_id, row["email"]):
        return False
    cur = conn.execute("UPDATE users SET invited_by=? WHERE id=? AND invited_by=0",
                       (who, user_id))
    if cur.rowcount != 1:           # credited already, by a sign-in at the same moment
        conn.commit()
        return False
    _add_week(conn, user_id, now)
    # Friends who joined before rewards were counted were each rewarded.
    before = joined(conn, who) - 1
    conn.execute("UPDATE users SET invite_rewards=MAX(invite_rewards, ?) + 1, "
                 "bonus_until=MAX(bonus_until, ?) + ? "
                 "WHERE id=? AND MAX(invite_rewards, ?) < ?",
                 (before, now, BONUS_DAYS * 86400, who, before, MAX_REWARDS))
    conn.commit()
    return True
