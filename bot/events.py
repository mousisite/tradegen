"""Daily counts of what happens on the site, and nothing about who did it.

Whoever runs the site needs to know whether people who arrive sign up, whether
those who sign up run an analysis, and whether anyone then sets an alert, logs
a paper trade or pays. Each of those is a number for a day and nothing more:
the table holds the day, the name of what happened and how many times. No
account, address, ticker or network address is stored with a count, so none of
it can be traced back to a person, and deleting an account leaves a day's total
as it was, because the total never said whose it was. One count is of the
server's own work instead, kept the same way: the alert checks it left while a
market was closed, so the requests that saves can be seen.

Every function here swallows its own failures. A count is worth far less than
the thing being counted, and a locked table or a full disk must never cost
someone their sign-in, their alert or their trade.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
from typing import Dict, Iterator, Optional

log = logging.getLogger(__name__)

# What is counted, in the order a person meets it, with the words the
# operator's page uses. A name outside this list is never written.
NAMES = (
    ("landing_view", "Front page views"),
    ("sign_up", "New accounts"),
    ("first_analysis", "First analyses"),
    ("watch_add", "Watchlist adds"),
    ("alert_add", "Alerts set"),
    ("paper_trade", "Paper trades"),
    ("turned_pro", "Turned Pro"),
)
# What the server itself does, counted the same way and shown with the
# machinery rather than in the table above: the alert and stop checks the
# background loop left because their market was closed (web._check_alerts_job).
MACHINERY = (
    ("closed_skips", "Checks skipped, market closed"),
)
_KNOWN = dict(NAMES + MACHINERY)

_TABLE = ("CREATE TABLE IF NOT EXISTS usage_counts ("
          "day TEXT NOT NULL, name TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, "
          "PRIMARY KEY (day, name))")

# How long a count opened on its own connection waits for a busy database.
# The front page and the analysis page call it, and a count lost to a long
# write elsewhere is better than either page waiting for it.
BUSY_MS = 1000


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _add(conn: sqlite3.Connection, name: str, n: int) -> None:
    conn.execute("INSERT INTO usage_counts (day, name, n) VALUES (?, ?, ?) "
                 "ON CONFLICT(day, name) DO UPDATE SET n = n + excluded.n",
                 (_today(), name, int(n)))
    conn.commit()


def bump(conn: sqlite3.Connection, name: str, n: int = 1) -> None:
    """Add n to today's count of name. Never raises.

    Called just after the thing it counts has been written and committed, on
    the same connection, so it commits nothing of anyone else's.
    """
    if name not in _KNOWN or n <= 0:
        return
    try:
        try:
            _add(conn, name, n)
        except sqlite3.OperationalError as exc:
            # The table is made the first time anything is counted, rather
            # than on every call: almost every call finds it there.
            if "no such table" not in str(exc):
                raise
            conn.execute(_TABLE)
            _add(conn, name, n)
    except Exception:
        log.exception("Could not count %s", name)
        try:
            conn.rollback()
        except Exception:
            pass


def _bump_at(path: Optional[str], name: str) -> None:
    """bump() on a connection of its own, waiting only briefly for the lock."""
    from . import database as db_mod
    try:
        conn = db_mod.connect(path)
    except Exception:
        log.exception("Could not open the database to count %s", name)
        return
    try:
        conn.execute("PRAGMA busy_timeout = %d" % BUSY_MS)
        bump(conn, name)
    except Exception:
        log.exception("Could not count %s", name)
    finally:
        conn.close()


# Robots that say what they are. Link previews in chat apps fetch the page as
# soon as someone pastes the address, so they are left out too. A robot that
# pretends to be a browser is still counted: the page says so.
_ROBOT = re.compile(r"bot|crawl|spider|slurp|preview|monitor|scan|curl|wget|"
                    r"python|java/|go-http|okhttp|headless|lighthouse|"
                    r"facebookexternalhit|whatsapp|telegram|embedly|pinterest|"
                    r"validator", re.I)


def is_robot(agent: str) -> bool:
    return not (agent or "").strip() or bool(_ROBOT.search(agent))


def landing_view(path: Optional[str], method: str, agent: str) -> None:
    """Count a view of the front page by someone not signed in."""
    if method != "GET" or is_robot(agent):
        return
    _bump_at(path, "landing_view")


def first_analysis(path: Optional[str], user_id: Optional[int],
                   run_id: Optional[int]) -> None:
    """Count a person's first analysis on the site, once.

    Called where a run has just been recorded for them. It is their first when
    no earlier run of theirs came from the site; a routine that ran before it
    does not hide it, and two first analyses at once count once, because only
    the earlier run finds nothing before it.
    """
    if not run_id or not user_id:
        return
    from . import database as db_mod
    try:
        conn = db_mod.connect(path)
    except Exception:
        log.exception("Could not open the database to count a first analysis")
        return
    try:
        conn.execute("PRAGMA busy_timeout = %d" % BUSY_MS)
        earlier = conn.execute("SELECT 1 FROM runs WHERE user_id=? AND id<? "
                               "AND source='web' LIMIT 1",
                               (int(user_id), int(run_id))).fetchone()
        if earlier is None:
            bump(conn, "first_analysis")
    except Exception:
        log.exception("Could not count a first analysis")
    finally:
        conn.close()


def new_account(conn: sqlite3.Connection, upsert, *args):
    """Run an account upsert, and count a sign-up when it made a new account.

    Account ids only grow and are never reused, so the account the call
    returns was made by it exactly when its id is above the highest one there
    before. Signing in again to an existing account counts nothing.
    """
    try:
        before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
    except sqlite3.Error:
        before = None
    user = upsert(conn, *args)
    if before is not None and getattr(user, "id", 0) > before:
        bump(conn, "sign_up")
    return user


# Accounts counted as turning Pro lately, held in memory only, so the page a
# buyer comes back to and Stripe's own message, which both record the same
# upgrade and often arrive within a second of each other, count it once.
_UPGRADED: Dict[int, float] = {}
_UPGRADED_LOCK = threading.Lock()
UPGRADE_MEMORY = 3600.0


def _pro_ids(conn: sqlite3.Connection):
    from . import billing as billing_mod
    marks = ",".join("?" * len(billing_mod.ACTIVE))
    return {int(r[0]) for r in conn.execute(
        "SELECT id FROM users WHERE plan='pro' AND plan_status IN (%s)" % marks,
        tuple(billing_mod.ACTIVE))}


@contextlib.contextmanager
def counting_upgrades(conn: sqlite3.Connection) -> Iterator[None]:
    """Count each account that goes from Free to Pro while the block runs.

    A renewal, or a card retried while the plan stays Pro, is not a change of
    plan and counts nothing.
    """
    try:
        before = _pro_ids(conn)
    except Exception:
        log.exception("Could not read who is on Pro")
        before = None
    yield
    if before is None:
        return
    try:
        turned = _pro_ids(conn) - before
    except Exception:
        log.exception("Could not read who is on Pro")
        return
    now = time.time()
    with _UPGRADED_LOCK:
        for old in [u for u, at in _UPGRADED.items() if now - at > UPGRADE_MEMORY]:
            del _UPGRADED[old]
        fresh = [u for u in turned if u not in _UPGRADED]
        for user_id in fresh:
            _UPGRADED[user_id] = now
    if fresh:
        bump(conn, "turned_pro", len(fresh))


def read(conn: sqlite3.Connection, days: int = 30) -> Dict:
    """The last `days` days of counts, newest day first, with totals.

    Only days on which something people do was counted are listed; the
    server's own counts are in the totals alone.
    """
    since = time.strftime("%Y-%m-%d",
                          time.gmtime(time.time() - (max(1, days) - 1) * 86400))
    try:
        rows = conn.execute("SELECT day, name, n FROM usage_counts WHERE day >= ? "
                            "ORDER BY day DESC", (since,)).fetchall()
    except sqlite3.OperationalError:
        rows = []                       # nothing counted yet, so no table
    people = dict(NAMES)
    by_day: Dict[str, Dict[str, int]] = {}
    total = {key: 0 for key in _KNOWN}
    for row in rows:
        day, name, n = row[0], row[1], int(row[2])
        if name not in _KNOWN:
            continue
        if name in people:
            by_day.setdefault(day, {})[name] = n
        total[name] += n
    return {"names": NAMES, "since": since, "total": total,
            "days": [{"day": day, "counts": counts} for day, counts in by_day.items()]}
