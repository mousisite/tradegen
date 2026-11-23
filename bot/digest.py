"""The weekly email: what Orenth says about your watchlist, every Monday.

Only to people who turned it on in their account, at most once a week, and
every one carries a one-click way to stop. US law (CAN-SPAM) wants a postal
address at the bottom of email like this, so nothing is sent until
MAIL_POSTAL_ADDRESS is set. A PO box or a virtual mailbox works, and keeps a
home address out of strangers' inboxes.

Also here: what changed on a watchlist since the session before, which the
front page shows every day and this email carries too.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import sqlite3
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import accounts, billing, codes, public
from . import database as db
from . import market
from .market import DataError, DataThrottled, DataUnavailable

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
    # As bytes: text with a letter outside ASCII raises here, and a mangled
    # link is a wrong one, not a fault.
    return hmac.compare_digest(token(secret, user_id).encode("ascii"),
                               (given or "").encode("utf-8", "replace"))


def can_receive(user) -> bool:
    return bool(user.email) and not user.email.endswith(".invalid") \
        and not user.is_local and user.provider != "demo"


def set_wanted(conn: sqlite3.Connection, user_id: int, on: bool) -> None:
    conn.execute("UPDATE users SET weekly_email=? WHERE id=?",
                 (1 if on else 0, user_id))
    conn.commit()


# A popular check older than this is not today's, as on the front page. One
# whose checks keep failing stays stored as it last was.
FRESH = 24 * 3600


def strongest(verdicts: List[Dict], n: int = 3,
              now: Optional[float] = None) -> Optional[List[Dict]]:
    """The popular BUYs that worked most often before, from checks of the last
    day only; None when there is no such check to choose from."""
    now = now or time.time()
    fresh = [v for v in verdicts if now - (v.get("at") or 0) <= FRESH]
    if not fresh:
        return None
    buys = [v for v in fresh if v.get("action") == "BUY" and v.get("samples")]
    return sorted(buys, key=lambda v: v["worked"] / v["samples"], reverse=True)[:n]


# --- what changed since yesterday --------------------------------------------
#
# For each instrument on a person's watchlist: whether its daily call changed
# since the session before, how far its price moved since the previous close,
# and the alerts and crossed stops of the last day. The daily calls are written
# down by the hourly background job (check_watched), never by a page view, and
# it never spends anyone's free looks: it is the server's own check, made the
# way a popular stock's is.

CHECK_EVERY = public.MAX_AGE      # as often as a popular stock is checked
CHECK_PER_RUN = 20                # the rest wait for the next hourly run
# Of those, how many may go to instruments whose last try failed, such as a
# mistyped or delisted ticker. Nothing is saved for one that cannot be priced,
# so without this twenty of them, never checked and so always first in line,
# took every run and no other watched instrument was checked again.
RETRY_PER_RUN = CHECK_PER_RUN // 4
# When each one's last try failed, for this process's life.
_FAILED: Dict[str, float] = {}
KEEP_DAYS = 14
RECENT = 86400                    # an alert or a crossed stop of the last day
MOVE_SHOWN = 0.02                 # a move this size is news on its own
# The session before lies at most this many days back: a weekend and a holiday.
# Compared with an older call, a week's change would read as a day's.
GAP_DAYS = 4


def reading(bars, action: Optional[str] = None) -> Dict:
    """One check of a watched instrument, as check_watched stores it."""
    day = public._price_day(bars)
    if day is None:
        raise ValueError("no trading day for %s" % getattr(bars, "symbol", "?"))
    return {"action": (action or "").upper() or None, "day": int(day),
            "price": float(bars.last_price), "prev_close": bars.prev_close}


def _readers(conn: sqlite3.Connection, now: float) -> List:
    """The accounts whose list someone will read: signed in within a
    session's life, taking the weekly email, or the local account. Checking
    the watchlists of accounts nobody opens would cost for nothing."""
    rows = conn.execute(
        "SELECT * FROM users WHERE provider <> 'demo' AND (is_local = 1 OR "
        "weekly_email = 1 OR last_seen_ts >= ?) ORDER BY id",
        (int(now) - accounts.SESSION_DAYS * 86400,)).fetchall()
    return [accounts._row_to_user(r) for r in rows]


def _first(conn: sqlite3.Connection, user_id: int) -> List[str]:
    """The instruments this list covers, in the order both show them.

    Each is Yahoo's symbol for what the watchlist entry opens, which is what
    the daily checks are kept under: a bare LINK saved before the exact mark
    opens the Chainlink coin (LINK-USD), and one saved exactly opens Interlink
    Electronics (LINK). Keyed by the bare ticker, the listing would have been
    checked as the coin and shown the coin's call.
    """
    out: List[str] = []
    for w in db.watchlist(conn, user=user_id)[:MAX_SYMBOLS]:
        try:
            symbol = market.normalise_symbol(w["symbol"], exact=bool(w["exact"]))
        except DataError:
            symbol = w["symbol"]
        if symbol not in out:
            out.append(symbol)
    return out


def watched(conn: sqlite3.Connection, full: Callable,
            now: Optional[float] = None) -> Dict[str, bool]:
    """Each instrument to check, and whether its daily call is wanted.

    The first MAX_SYMBOLS on each reader's watchlist, the ones the front page
    and the email show. A popular instrument's call is its public daily check,
    which costs nothing more. Any other is analysed only for an account the
    free plan leaves alone (full(person) is true): a free account opens three
    instruments a day, and a daily call on its whole watchlist from here would
    be a way round that. Its price is read either way.
    """
    now = now or time.time()
    out: Dict[str, bool] = {}
    for person in _readers(conn, now):
        symbols = _first(conn, person.id)
        plan = None
        for symbol in symbols:
            if symbol in public.NAMES:
                out[symbol] = True
                continue
            if plan is None:
                plan = bool(full(person))
            out[symbol] = out.get(symbol, False) or plan
    return out


def check_watched(conn: sqlite3.Connection, check: Callable[[str, bool], Dict],
                  full: Callable, now: Optional[float] = None,
                  limit: int = CHECK_PER_RUN, pause: float = public.PAUSE,
                  sleep: Callable[[float], None] = time.sleep) -> Dict:
    """Write today's row for each watched instrument that is due.

    check(symbol, with_call) returns a reading(): the price, the previous
    close, the trading day and, when with_call, the daily call. An instrument
    is due when it has no row, when its newest is CHECK_EVERY old, or when its
    call is wanted and the newest row has none. Never-checked first, then the
    oldest; a source that says stop ends the run, and the rest wait an hour.
    Those whose last try failed come after the rest, a few a run, the one
    tried longest ago first.
    """
    now = now or time.time()
    wanted = watched(conn, full, now)
    latest = {s: rows[0] for s, rows in db.watch_calls(conn, wanted, 1).items()}
    due = [s for s, with_call in wanted.items()
           if s not in latest or now - latest[s]["at"] >= CHECK_EVERY
           or (with_call and not latest[s]["action"])]
    due.sort(key=lambda s: (latest[s]["at"] if s in latest else 0, s))
    for symbol in [s for s in _FAILED if s not in wanted]:
        _FAILED.pop(symbol, None)               # off every list: forgotten
    retry = sorted((s for s in due if s in _FAILED), key=lambda s: (_FAILED[s], s))
    batch = [s for s in due if s not in _FAILED] + retry[:RETRY_PER_RUN]
    checked, errors = 0, []
    for n, symbol in enumerate(batch[:limit]):
        if n:
            sleep(pause)
        try:
            got = check(symbol, wanted[symbol])
            db.watch_call_save(conn, symbol, got["day"], got.get("action"),
                               got["price"], got.get("prev_close"), int(now))
            checked += 1
            _FAILED.pop(symbol, None)
        except DataThrottled as exc:
            errors.append("%s: %s" % (symbol, exc))
            break
        except Exception as exc:          # one bad ticker must not stop the run
            errors.append("%s: %s" % (symbol, exc))
            # A source that did not answer says nothing about the ticker.
            if not isinstance(exc, DataUnavailable):
                _FAILED[symbol] = now
    db.watch_calls_prune(conn, now - KEEP_DAYS * 86400)
    return {"checked": checked, "errors": errors}


def since_yesterday(conn: sqlite3.Connection, user_id: int, full: bool,
                    now: Optional[float] = None) -> Dict:
    """What changed on one person's watchlist since the session before.

    rows holds the instruments with something to say: the daily call changed,
    the price moved at least MOVE_SHOWN since the previous close, or an alert
    fired or a stop was crossed in the last day. quiet counts the others that
    were checked, and kept those of them whose call was compared and is the
    same; unchecked counts those with no check yet. A call is shown for a
    popular instrument, and for any other only when full: the free plan's
    calls are not checked here (watched()), and hidden says so.
    """
    now = now or time.time()
    symbols = _first(conn, user_id)
    out = {"rows": [], "quiet": 0, "kept": 0, "unchecked": 0,
           "hidden": not full and any(s not in public.NAMES for s in symbols),
           "watching": len(symbols)}
    if not symbols:
        return out
    history = db.watch_calls(conn, symbols, 2)
    recent = [e for e in db.event_list(conn, user_id, limit=200)
              if e["ts"] >= now - RECENT]
    for symbol in symbols:
        sessions = history.get(symbol) or []
        latest = sessions[0] if sessions else None
        before = sessions[1] if len(sessions) > 1 else None
        if before is not None and latest["day"] - before["day"] > GAP_DAYS:
            before = None
        shown = full or symbol in public.NAMES
        mine = [e for e in recent if e["symbol"] == symbol]
        row = {"symbol": symbol, "short": public.short(symbol),
               "slug": public.slug(symbol) if symbol in public.NAMES else "",
               "call": latest["action"] if latest and shown else None,
               "was": before["action"] if before and shown else None,
               "move": None, "price": latest["price"] if latest else None,
               "at": latest["at"] if latest else None,
               "day": latest["day"] if latest else None,
               "alerts": [e["message"] for e in mine
                          if e["kind"] not in ("stop_breach", "split")],
               "stops": [e["message"] for e in mine if e["kind"] == "stop_breach"]}
        row["changed"] = bool(row["call"] and row["was"] and row["call"] != row["was"])
        if latest is not None and latest["prev_close"]:
            row["move"] = latest["price"] / latest["prev_close"] - 1.0
        if (row["changed"] or row["alerts"] or row["stops"]
                or (row["move"] is not None and abs(row["move"]) >= MOVE_SHOWN)):
            out["rows"].append(row)
        elif row["move"] is None:
            out["unchecked"] += 1
        else:
            out["quiet"] += 1
            out["kept"] += 1 if row["call"] and row["was"] else 0
    return out


def change_line(row: Dict) -> str:
    """One row of since_yesterday() as a sentence, for the email."""
    parts = []
    if row["changed"]:
        parts.append("the daily call went from %s to %s" % (row["was"], row["call"]))
    elif row["call"] and row["was"]:
        parts.append("still %s" % row["call"])
    elif row["call"]:
        parts.append("daily call %s" % row["call"])
    if row["move"] is not None:
        pct = "%.2f" % (abs(row["move"]) * 100)
        parts.append("level with the previous close" if pct == "0.00" else
                     "%s %s%% since the previous close"
                     % ("up" if row["move"] > 0 else "down", pct))
    text = "; ".join(parts) or "no price to compare yet"
    return text[:1].upper() + text[1:] + "."


def notes(since: Dict) -> List[str]:
    """What since_yesterday() left out of its rows, in sentences."""
    out = []
    n, limit = since["quiet"], int(MOVE_SHOWN * 100)
    if n:
        who = (("The other one" if n == 1 else "The other %d" % n) if since["rows"]
               else ("The one checked" if n == 1 else "All %d checked" % n))
        line = ("%s moved less than %d%% since the previous close, with no alert "
                "or crossed stop." % (who, limit))
        if since["kept"] == n:
            line += (" Its daily call is" if n == 1 else " Their daily calls are")
            line += " the same as the session before."
        out.append(line)
    if since["unchecked"]:
        k = since["unchecked"]
        out.append("%s no price to compare with yet."
                   % ("One instrument has" if k == 1 else "%d instruments have" % k))
    if since["hidden"]:
        out.append("On a free account this list has the daily call of the "
                   "popular stocks and coins only. Opening any other shows "
                   "today's call.")
    return out


def _session(day: Optional[int]) -> str:
    """"Fri 20 Nov: " for a trading day, to open a row with; "" for none."""
    if day is None:
        return ""
    t = time.gmtime(int(day) * 86400)
    return "%s %d %s: " % (time.strftime("%a", t), t.tm_mday, time.strftime("%b", t))


def compose(user, rows: List[Dict], picks: Optional[List[Dict]], base: str,
            stop_url: str, since: Optional[Dict] = None) -> Tuple[str, str]:
    """Subject and plain-text body. Plain text reads like a person wrote it,
    which is also what keeps it out of spam folders. since is the person's
    since_yesterday(), said after their watchlist: each row with the session
    it is about, since on a Monday morning that is Friday's, not the day
    before. picks is strongest(), None when nothing was checked in time."""
    # BTC-USD reads as BTC, unless this email also names the fund listed as
    # BTC: then each goes by Yahoo's own symbol, as on the front page, so the
    # coin's call and the fund's are not two lines under one name.
    named_as: Dict[str, set] = {}
    for r in list(rows) + list((since or {}).get("rows") or []) + list(picks or []):
        named_as.setdefault(public.short(r["symbol"]), set()).add(r["symbol"])

    def label(symbol: str) -> str:
        short = public.short(symbol)
        return symbol if len(named_as.get(short, ())) > 1 else short

    # By name only: without one, the part of the address before the @ is
    # what display falls back to, and a hidden Apple address is random there.
    name = (user.name or "").strip()
    lines = ["Hi %s," % name if name else "Hi,", "",
             "Here is what Orenth says this week."]
    if rows:
        lines += ["", "YOUR WATCHLIST"]
        for r in rows:
            sym = label(r["symbol"])
            if r.get("action"):
                lines.append("  %-6s %-6s %s" % (sym, r["action"], r.get("headline", "")))
                if r.get("samples"):
                    lines.append("         " + public.counts_line(r))
            else:
                lines.append("  %-6s could not be checked this week." % sym)
    else:
        lines += ["", "Your watchlist is empty. Add stocks from any analysis "
                      "page and they will show up here."]
    if since and (since["rows"] or since["quiet"]):
        lines += ["", "SINCE THE TRADING DAY BEFORE"]
        for r in since["rows"]:
            lines.append("  %-6s %s%s" % (label(r["symbol"]), _session(r["day"]),
                                          change_line(r)))
            lines += ["         Alert: %s" % m for m in r["alerts"]]
            lines += ["         Stop: %s" % m for m in r["stops"]]
        lines += ["  %s" % s for s in notes(since)]
    lines += ["", "STRONGEST SETUPS AMONG POPULAR STOCKS"]
    if picks:
        for p in picks:
            lines.append("  %-6s BUY    worked %d of %d times in tests on past prices"
                         % (label(p["symbol"]), p["worked"], p["samples"]))
    elif picks is None:
        lines.append("  The popular stocks could not be checked today.")
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
    named = ["%s %s" % (label(r["symbol"]), r["action"].lower())
             for r in rows if r.get("action")][:3]
    subject = "Orenth this week: " + (", ".join(named) if named
                                      else "the strongest setups")
    return subject, "\n".join(lines) + "\n"


def run(conn: sqlite3.Connection, secret, base: str,
        look_up: Callable[[str], Optional[Dict]],
        send: Callable = codes.send_mail, now: Optional[float] = None,
        full: Optional[Callable] = None) -> Dict:
    """Send this week's email to everyone due one. Safe to call every hour.

    full(person) says whether the free plan leaves an account alone, for the
    daily calls in "since yesterday"; without it, only the plan decides,
    which leaves out the operator.
    """
    now = now or time.time()
    full = full or billing.is_pro
    if not ready() or not sending_window(now):
        return {"checked": 0}
    this_week = week(now)
    picks = strongest(public.verdicts(conn), now=now)
    seen: Dict[Tuple[str, bool], Optional[Dict]] = {}

    def check(symbol: str, exact: bool = False) -> Dict:
        # One look-up per symbol per run, however many people watch it. An
        # entry saved as Yahoo's own symbol (LINK for Interlink Electronics)
        # is looked up as that, apart from the coin a bare LINK is.
        key = (symbol, exact)
        if key not in seen:
            try:
                seen[key] = look_up(symbol, exact=True) if exact else look_up(symbol)
            except Exception:
                seen[key] = None
        return seen[key] or {"symbol": symbol}

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
            watched = db.watchlist(conn, user=user.id)[:MAX_SYMBOLS]
            stop = "%s/email/stop?u=%d&t=%s" % (base, user.id,
                                                token(secret, user.id))
            subject, body = compose(user, [check(w["symbol"], bool(w["exact"]))
                                           for w in watched], picks, base, stop,
                                    since_yesterday(conn, user.id, full(user), now))
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
