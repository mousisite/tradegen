"""Put the App Review account back on Free before the next submission.

    python tools/reset_test_purchases.py
    python tools/reset_test_purchases.py someone@example.com

App Review buys Pro in Apple's sandbox, for free, with the review account. If
that test subscription is still running when the next build goes to review,
the next reviewer finds nothing to buy and can reject the app. This forgets
the account's test (Sandbox) subscriptions, so it is Free again unless it has
Pro some other way. Real purchases (Production) are never touched.

With no address, it resets REVIEW_LOGIN_EMAIL, the review account.

On the server, run it from Render's Shell tab for the stockbot service.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import apple_store  # noqa: E402
from bot import database as db  # noqa: E402


def reset(conn, who: str) -> int:
    """Forget one account's test purchases; how many there were."""
    who = (who or "").strip().lower()
    if who.startswith("+"):
        # Phone accounts are stored under a made-up address; see accounts.py.
        who = "phone%s@users.invalid" % who.lstrip("+")
    row = conn.execute("SELECT id FROM users WHERE email=?", (who,)).fetchone()
    if row is None:
        raise ValueError("No Orenth account uses %s." % (who or "that address"))
    return apple_store.forget_test_purchases(conn, int(row["id"]))


def main(argv) -> int:
    who = argv[0] if argv else (os.environ.get("REVIEW_LOGIN_EMAIL") or "").strip()
    if len(argv) > 1 or not who:
        print(__doc__)
        return 2
    conn = db.connect()
    try:
        removed = reset(conn, who)
    except ValueError as exc:
        print(exc)
        return 1
    finally:
        conn.close()
    print("Done. %s: %d test subscription%s forgotten. It is on Free again, unless "
          "it has Pro some other way." % (who, removed, "" if removed == 1 else "s"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
