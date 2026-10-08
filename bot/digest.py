"""The weekly email: what Orenth says about your watchlist, every Monday.

Only to people who turned it on in their account, at most once a week, and
every one carries a one-click way to stop. US law (CAN-SPAM) wants a postal
address at the bottom of email like this, so nothing is sent until
MAIL_POSTAL_ADDRESS is set. A PO box or a virtual mailbox works, and keeps a
home address out of strangers' inboxes.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import sqlite3
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import accounts, codes, public
from . import database as db

# Monday, 13:00 UTC: morning across the US, before the market opens. Tuesday
# counts too, so a server that was down on Monday still sends that week.
SEND_HOUR = 13
MAX_SYMBOLS = 8


def postal_address() -> str:
    return (os.environ.get("MAIL_POSTAL_ADDRESS") or "").strip()


def ready() -> bool:
    return codes.email_ready() and bool(postal_address())


def week(now: float) -> str:
    year, number, _ = datetime.datetime.utcfromtimestamp(now).isocalendar()
    return "%d-W%02d" % (year, number)


def sending_window(now: float) -> bool:
    t = time.gmtime(now)
    return (t.tm_wday == 0 and t.tm_hour >= SEND_HOUR) or t.tm_wday == 1


def token(secret, user_id: int) -> str:
    """Proves a stop link was made here, so nobody can unsubscribe others."""
    key = secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
    return hmac.new(key, ("weekly-email:%d" % user_id).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def check_token(secret, user_id: int, given: str) -> bool:
    return hmac.compare_digest(token(secret, user_id), given or "")


def can_receive(user) -> bool:
    return bool(user.email) and not user.email.endswith(".invalid") \
        and not user.is_local and user.provider != "demo"


def set_wanted(conn: sqlite3.Connection, user_id: int, on: bool) -> None:
    conn.execute("UPDATE users SET weekly_email=? WHERE id=?",
                 (1 if on else 0, user_id))
    conn.commit()


def strongest(verdicts: List[Dict], n: int = 3) -> List[Dict]:
    """The popular BUYs that worked most often before."""
    buys = [v for v in verdicts if v.get("action") == "BUY" and v.get("samples")]
    return sorted(buys, key=lambda v: v["worked"] / v["samples"], reverse=True)[:n]


def compose(user, rows: List[Dict], picks: List[Dict], base: str,
            stop_url: str) -> Tuple[str, str]:
    """Subject and plain-text body. Plain text reads like a person wrote it,
    which is also what keeps it out of spam folders."""
    lines = ["Hi %s," % user.display, "", "Here is what Orenth says this week."]
    if rows:
        lines += ["", "YOUR WATCHLIST"]
        for r in rows:
            sym = public.short(r["symbol"])
            if r.get("action"):
                lines.append("  %-6s %-6s %s" % (sym, r["action"], r.get("headline", "")))
                if r.get("samples"):
                    lines.append("         " + public.counts_line(r))
            else:
                lines.append("  %-6s could not be checked this week." % sym)
    else:
        lines += ["", "Your watchlist is empty. Add stocks from any analysis "
                      "page and they will show up here."]
    lines += ["", "STRONGEST SETUPS AMONG POPULAR STOCKS"]
    if picks:
        for p in picks:
            lines.append("  %-6s BUY    worked %d of %d times before"
                         % (public.short(p["symbol"]), p["worked"], p["samples"]))
    else:
        lines.append("  None this week. Nothing on the popular list clears the "
                     "bar, so Orenth says wait. That is a normal answer.")
    lines += ["", "See them all: %s/stocks" % base, "",
              "Not financial advice. Every figure is measured from past "
              "prices, and the past can stop describing the future.", "",
              "You get this because you turned on the weekly email in your "
              "Orenth account.",
              "Stop these emails: %s" % stop_url,
              postal_address()]
    named = ["%s %s" % (public.short(r["symbol"]), r["action"].lower())
             for r in rows if r.get("action")][:3]
    subject = "Orenth this week: " + (", ".join(named) if named
                                      else "the strongest setups")
    return subject, "\n".join(lines) + "\n"


def run(conn: sqlite3.Connection, secret, base: str,
        look_up: Callable[[str], Optional[Dict]],
        send: Callable = codes.send_mail, now: Optional[float] = None) -> Dict:
    """Send this week's email to everyone due one. Safe to call every hour."""
    now = now or time.time()
    if not ready() or not sending_window(now):
        return {"checked": 0}
    this_week = week(now)
    picks = strongest(public.verdicts(conn))
    seen: Dict[str, Optional[Dict]] = {}

    def check(symbol: str) -> Dict:
        # One look-up per symbol per run, however many people watch it.
        if symbol not in seen:
            try:
                seen[symbol] = look_up(symbol)
            except Exception:
                seen[symbol] = None
        return seen[symbol] or {"symbol": symbol}

    sent, errors = 0, []
    due = conn.execute("SELECT * FROM users WHERE weekly_email=1 AND "
                       "digest_week<>?", (this_week,)).fetchall()
    for row in due:
        user = accounts._row_to_user(row)
        if not can_receive(user):
            continue
        before = row["digest_week"]
        # Claimed before sending, so two runs at once cannot both send.
        if not conn.execute("UPDATE users SET digest_week=? WHERE id=? AND "
                            "digest_week=?", (this_week, user.id, before)).rowcount:
            continue
        conn.commit()
        try:
            symbols = [w["symbol"] for w in db.watchlist(conn, user=user.id)]
            stop = "%s/email/stop?u=%d&t=%s" % (base, user.id,
                                                token(secret, user.id))
            subject, body = compose(user, [check(s) for s in symbols[:MAX_SYMBOLS]],
                                    picks, base, stop)
            send(user.email, subject, body,
                 {"List-Unsubscribe": "<%s>" % stop,
                  "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
            sent += 1
        except Exception as exc:
            # Give it back, so the next run tries again this week.
            conn.execute("UPDATE users SET digest_week=? WHERE id=?",
                         (before, user.id))
            conn.commit()
            errors.append("user %d: %s" % (user.id, exc))
    return {"checked": sent, "errors": errors}
