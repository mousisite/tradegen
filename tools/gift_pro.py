"""Give someone free Pro, for a giveaway winner or a friend.

    python tools/gift_pro.py winner@example.com 365
    python tools/gift_pro.py +15555550100 365

Adds days to the account's free Pro, the same way invite weeks are added, so
no card is asked for, nothing is charged and nothing renews. The person needs
an Orenth account first: they sign up free, then send you the email address
or phone number they signed in with.

On the server, run it from Render's Shell tab for the stockbot service.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import database as db  # noqa: E402


def gift(conn, who: str, days: int, now: float = None) -> int:
    """Extend free Pro by `days` and return when it now ends."""
    who = who.strip().lower()
    if who.startswith("+"):
        # Phone accounts are stored under a made-up address; see accounts.py.
        who = "phone%s@users.invalid" % who.lstrip("+")
    row = conn.execute("SELECT id, bonus_until FROM users WHERE email=?",
                       (who,)).fetchone()
    if row is None:
        raise ValueError("No Orenth account uses %s. Ask them to sign up "
                         "first, then run this again." % who)
    now = int(now or time.time())
    until = max(now, int(row["bonus_until"] or 0)) + int(days) * 86400
    conn.execute("UPDATE users SET bonus_until=? WHERE id=?", (until, row["id"]))
    conn.commit()
    return until


def main(argv) -> int:
    if len(argv) != 2 or not argv[1].isdigit() or not 1 <= int(argv[1]) <= 3650:
        print(__doc__)
        return 2
    conn = db.connect()
    try:
        until = gift(conn, argv[0], int(argv[1]))
    except ValueError as exc:
        print(exc)
        return 1
    finally:
        conn.close()
    print("Done. %s has free Pro until %s."
          % (argv[0], time.strftime("%d %b %Y", time.gmtime(until))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
