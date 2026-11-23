"""Paid plans, through Stripe.

Nothing here is on until STRIPE_SECRET_KEY, STRIPE_PRICE_ID and
STRIPE_WEBHOOK_SECRET are all set. Until then everyone has everything, exactly
as before, so this can ship before the decision to start charging is made.

Card details never reach this server. Stripe's own checkout page takes the
payment and Stripe's own portal handles cancelling and changing cards. This
server stores only the Stripe customer id and whether the plan is active.

Which plan someone is on is decided by Stripe's webhook, which is signed with
the webhook secret, and never by anything the browser says. The page a buyer
lands on after paying also asks Stripe directly, server to server, so the
upgrade shows at once instead of whenever the webhook happens to arrive.

The prices shown are read from Stripe itself, so the page can never say one
amount while Stripe charges another.

Optional settings:
  STRIPE_PRICE_ID_YEARLY  a yearly price, offered beside the monthly one
  PRO_PRICE_LABEL         shown only if Stripe cannot be reached for the price
  FREE_ANALYSES_PER_DAY   different instruments a free account can open a day (3)
  FREE_FIND_RESULTS       rows a free account sees in each Find list (3)
  FREE_ALERTS             alerts a free account can have running (1)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

API = "https://api.stripe.com/v1/"

# past_due keeps Pro while Stripe retries a failed card, rather than cutting
# someone off on the first decline.
ACTIVE = ("active", "trialing", "past_due")

# A checkout can complete while a bank debit is still clearing. That one waits
# for the subscription event saying the payment went through.
PAID = ("paid", "no_payment_required")

# How long after its paid period ends a plan still marked active is believed.
# Stripe moves the end forward at every renewal and retries a failed delivery
# for three days, so a plan this far past its end is one whose cancellation
# never reached this server.
GRACE = 3 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_views (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    item     TEXT NOT NULL,
    PRIMARY KEY (user_id, day, item)
);
CREATE TABLE IF NOT EXISTS plan_events (
    user_id  INTEGER PRIMARY KEY,
    last_ts  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_checks (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    checked_ts  INTEGER NOT NULL
);
"""


class BillingError(RuntimeError):
    """Stripe could not be reached or refused, phrased for the person."""


def ready() -> bool:
    return all((os.environ.get(k) or "").strip()
               for k in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_ID",
                         "STRIPE_WEBHOOK_SECRET"))


def price_label() -> str:
    return (os.environ.get("PRO_PRICE_LABEL") or "").strip()


def _num(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key) or default))
    except ValueError:
        return default


def free_limits() -> Dict[str, int]:
    return {"analyses_per_day": _num("FREE_ANALYSES_PER_DAY", 3),
            "find_results": _num("FREE_FIND_RESULTS", 3),
            "alerts": _num("FREE_ALERTS", 1)}


def price_id_for(plan: str) -> str:
    """The Stripe price for a plan's name.

    Only ever a name from the form, mapped here: a price id sent by the
    browser could be any price on the account, including a cheaper one.
    """
    yearly = (os.environ.get("STRIPE_PRICE_ID_YEARLY") or "").strip()
    if plan == "yearly" and yearly:
        return yearly
    return os.environ["STRIPE_PRICE_ID"].strip()


SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "cad": "CA$", "aud": "A$"}
PRICE_TTL = 3600.0
_PRICES: Dict[str, tuple] = {}


def describe_price(price_id: str, call=None) -> Optional[Dict]:
    """What a price costs, as Stripe has it, e.g. "$9.99 a month"."""
    hit = _PRICES.get(price_id)
    if hit and time.time() - hit[0] < PRICE_TTL:
        return hit[1]
    try:
        found = (call or _stripe)("GET", "prices/" + price_id)
    except BillingError:
        return hit[1] if hit else None
    cents = found.get("unit_amount")
    recurring = found.get("recurring") or {}
    if cents is None or recurring.get("interval") not in ("month", "year") \
            or recurring.get("interval_count", 1) != 1:
        return None
    currency = (found.get("currency") or "usd").lower()
    amount = SYMBOLS.get(currency, currency.upper() + " ") + (
        "%d" % (cents // 100) if cents % 100 == 0 else "%.2f" % (cents / 100.0))
    info = {"cents": int(cents), "interval": recurring["interval"],
            "label": "%s a %s" % (amount, recurring["interval"])}
    _PRICES[price_id] = (time.time(), info)
    return info


def offers(call=None) -> Dict:
    """The plans on sale, and how much paying yearly saves, rounded down."""
    monthly = describe_price(price_id_for("monthly"), call)
    if monthly is None and price_label():
        monthly = {"cents": 0, "interval": "month", "label": price_label()}
    yearly = None
    if (os.environ.get("STRIPE_PRICE_ID_YEARLY") or "").strip():
        yearly = describe_price(price_id_for("yearly"), call)
        # A monthly price pasted into the yearly slot would sell monthly as yearly.
        if yearly and yearly["interval"] != "year":
            yearly = None
    save = 0
    if monthly and yearly and monthly["cents"] and monthly["interval"] == "month" \
            and yearly["interval"] == "year":
        save = max(0, int(100 - 100.0 * yearly["cents"] / (monthly["cents"] * 12)))
    return {"monthly": monthly, "yearly": yearly, "save": save}


# --- a discount code someone typed ------------------------------------------
#
# Never shown anywhere on the site: the code is passed on by word of mouth,
# and the pricing page has an empty box for it.

def _describe(coupon: Dict) -> tuple:
    """What a coupon gives, in words: ("half price", "for your first year")."""
    if coupon.get("percent_off"):
        pct = float(coupon["percent_off"])
        what = "half price" if pct == 50 else "%g%% off" % pct
    else:
        what = "$%g off" % (int(coupon.get("amount_off") or 0) / 100.0)
    months = coupon.get("duration_in_months")
    how_long = {"forever": "forever", "once": "on the first payment"}.get(
        coupon.get("duration"),
        "for your first year" if months == 12 else "for %s months" % months)
    return what, how_long


# A code that does not work, remembered for a few minutes, so the same wrong
# code typed again does not cost another call to Stripe.
MISS_TTL = 300.0
_MISSES: Dict[str, float] = {}
_MISSES_LOCK = threading.Lock()


def _missed(key: str) -> None:
    now = time.time()
    with _MISSES_LOCK:
        if len(_MISSES) >= 1000:
            for old in [k for k, at in _MISSES.items() if now - at >= MISS_TTL]:
                del _MISSES[old]
            if len(_MISSES) >= 1000:
                _MISSES.clear()
        _MISSES[key] = now


def promotion(code: str, call=None) -> Optional[Dict]:
    """A typed code that would apply right now: Stripe's id and what it gives.

    Stripe matches the code whatever its capitals. A code that is switched
    off or has used up its redemptions is turned away here, so the person
    hears it on our page rather than as a refusal on Stripe's.
    """
    code = code.strip()
    if not code:
        return None
    key = code.lower()
    with _MISSES_LOCK:
        missed_at = _MISSES.get(key)
    if missed_at is not None and time.time() - missed_at < MISS_TTL:
        return None
    from urllib.parse import quote
    call = call or _stripe
    found = (call("GET", "promotion_codes?active=true&limit=1&code="
                  + quote(code)).get("data") or [])
    promo = found[0] if found else None
    coupon = promo.get("coupon") if promo else None
    if promo and not isinstance(coupon, dict):
        # Newer API versions name the coupon instead of nesting it.
        ref = (promo.get("promotion") or {}).get("coupon")
        coupon = ref if isinstance(ref, dict) else (
            call("GET", "coupons/" + ref) if ref else None)
    if not (promo and coupon and promo.get("active") and coupon.get("valid", True)):
        _missed(key)
        return None
    for most, used in ((promo.get("max_redemptions"), promo.get("times_redeemed")),
                       (coupon.get("max_redemptions"), coupon.get("times_redeemed"))):
        if most and int(used or 0) >= int(most):
            _missed(key)
            return None
    what, how_long = _describe(coupon)
    return {"id": promo.get("id", ""), "code": promo.get("code") or code,
            "what": what, "how_long": how_long}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def is_pro(user, operator: bool = False) -> bool:
    """Whether this person has everything. Everyone does while billing is off."""
    if not ready():
        return True
    if user is None:
        return False
    if user.is_local or operator:
        return True
    if getattr(user, "bonus_until", 0) > time.time():     # invite reward
        return True
    # Bought in the iPhone app through Apple (bot/apple_store.py keeps it).
    if getattr(user, "apple_until", 0) > time.time():
        return True
    if getattr(user, "plan", "free") != "pro" or \
            getattr(user, "plan_status", "") not in ACTIVE:
        return False
    # No known end means none was ever reported, as for plans recorded before
    # the end was kept, so only a known end that is long gone takes Pro away.
    until = int(getattr(user, "plan_until", 0) or 0)
    return not until or time.time() < until + GRACE


# --- the free allowance ----------------------------------------------------------

def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def look_item(symbol: str, exact: bool = False) -> str:
    """What a free look is counted against: the instrument, as Yahoo names it.

    Whatever its bar size, and however it was typed, as far as the text alone
    can say: $tsla and NASDAQ:TSLA are TSLA, and BTC is the coin. LINK exactly
    (Interlink Electronics) is not the bare LINK (the Chainlink coin), so each
    is a look of its own. A company's name is only known to be its ticker once
    the analysis has found it: show_view moves the look there.
    """
    from . import market
    try:
        item = market.normalise_symbol(symbol, exact=exact)
    except market.DataError:
        item = symbol.strip().upper()
    return item.replace("|", " ")


def _instrument(item: str) -> str:
    """The instrument a counted row stands for. Rows written before looks were
    counted per instrument also hold the bar size, as AAPL|1d or LINK|1d|exact."""
    symbol, bar, rest = item.partition("|")
    return look_item(symbol, rest.endswith("|exact")) if bar else item


def _opened(conn: sqlite3.Connection, user_id: int, day: str) -> Dict[str, List[str]]:
    """The instruments opened on a day, each with the rows that say so."""
    out: Dict[str, List[str]] = {}
    for row in conn.execute("SELECT item FROM usage_views WHERE user_id=? AND day=?",
                            (user_id, day)):
        out.setdefault(_instrument(row[0]), []).append(row[0])
    return out


def may_view(conn: sqlite3.Connection, user_id: int, item: str) -> bool:
    """Whether a free account can open this instrument today.

    Counted per different instrument, not per page load: opening the same one
    again, refreshing, changing its bar size, or moving between its analysis
    and research pages costs nothing more, so the allowance is about breadth,
    not about clicking.
    """
    ensure_schema(conn)
    if opened_today(conn, user_id, item):
        return True
    return views_today(conn, user_id) < free_limits()["analyses_per_day"]


def opened_today(conn: sqlite3.Connection, user_id: int, item: str) -> bool:
    """Whether this instrument already counts as one of today's looks."""
    ensure_schema(conn)
    return _instrument(item) in _opened(conn, user_id, _today())


def reserve_view(conn: sqlite3.Connection, user_id: int, item: str) -> str:
    """Take one of today's looks for an instrument, before it is analysed.

    Returns "new" when a look was taken just now, "seen" when the instrument
    was already opened today and costs nothing more, and "" when none are
    left. The count and the taking happen in one write transaction: checking
    first and counting only after the analysis let ten requests sent together
    all read the same count and all get through.
    """
    ensure_schema(conn)
    if conn.in_transaction:
        conn.commit()
    day, item = _today(), _instrument(item)
    conn.execute("BEGIN IMMEDIATE")
    try:
        opened = _opened(conn, user_id, day)
        if item in opened:
            taken = "seen"
        elif len(opened) >= free_limits()["analyses_per_day"]:
            taken = ""
        else:
            conn.execute("INSERT INTO usage_views (user_id, day, item) "
                         "VALUES (?, ?, ?)", (user_id, day, item))
            taken = "new"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return taken


def release_view(conn: sqlite3.Connection, user_id: int, item: str) -> None:
    """Give back a look taken by reserve_view when nothing was shown for it."""
    ensure_schema(conn)
    conn.execute("DELETE FROM usage_views WHERE user_id=? AND day=? AND item=?",
                 (user_id, _today(), _instrument(item)))
    conn.commit()


def count_view(conn: sqlite3.Connection, user_id: int, item: str) -> None:
    """Record an instrument as opened today, once it has actually been shown."""
    show_view(conn, user_id, item, item)


def show_view(conn: sqlite3.Connection, user_id: int, asked: str, shown: str) -> None:
    """Count what was shown, and nothing for the words it was asked for by.

    "tesla" takes its look as TESLA, which is all the text says. The analysis
    then turns out to be TSLA, so the look moves there: what every link on
    the page opens is already counted, and one opened before by its ticker is
    not counted twice. Moving can only ever lower the count.
    """
    ensure_schema(conn)
    if conn.in_transaction:
        conn.commit()
    day, asked, shown = _today(), _instrument(asked), _instrument(shown)
    conn.execute("BEGIN IMMEDIATE")
    try:
        opened = _opened(conn, user_id, day)
        if shown not in opened:
            conn.execute("INSERT OR IGNORE INTO usage_views (user_id, day, item) "
                         "VALUES (?, ?, ?)", (user_id, day, shown))
        for row in opened.get(asked, []) if asked != shown else []:
            conn.execute("DELETE FROM usage_views WHERE user_id=? AND day=? AND item=?",
                         (user_id, day, row))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def views_today(conn: sqlite3.Connection, user_id: int) -> int:
    """How many different instruments this account has opened today."""
    ensure_schema(conn)
    return len(_opened(conn, user_id, _today()))


# --- plan state ------------------------------------------------------------------

def customer_of(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT stripe_customer FROM users WHERE id=?",
                       (user_id,)).fetchone()
    return (row["stripe_customer"] if row else "") or ""


# until is when the paid period ends, 0 when the change does not say. A plan
# that turns into Pro starts with no known end, so an end left over from an
# earlier subscription cannot cut the new one short before Stripe names its
# own. SQLite reads the old row on the right of every assignment.
_SET_PLAN = ("UPDATE users SET plan=?, plan_status=?, "
             "stripe_customer=CASE WHEN ?<>'' THEN ? ELSE stripe_customer END, "
             "plan_until=CASE WHEN ?>0 THEN ? WHEN ?='pro' AND plan<>'pro' "
             "THEN 0 ELSE plan_until END WHERE id=?")


def set_plan(conn: sqlite3.Connection, user_id: int, plan: str, status: str,
             customer: str = "", until: int = 0) -> None:
    conn.execute(_SET_PLAN, (plan, status, customer, customer, until, until,
                             plan, user_id))
    conn.commit()


def _apply(conn: sqlite3.Connection, user_id: int, created: int, plan: str,
           status: str, customer: str = "", until: int = 0,
           in_order: bool = True) -> bool:
    """Record a plan change unless something newer has already been recorded.

    Stripe does not deliver events in order, and retries a failed delivery
    for days, so an old "active" can arrive after the cancellation that
    replaced it. created is the Stripe time of what is being applied, and the
    newest one applied so far is kept per person. The check and the write are
    one transaction, so two deliveries handled at once cannot both pass.
    in_order=False applies it anyway and only moves that time forward, for a
    state read live from Stripe rather than carried by an event.
    """
    ensure_schema(conn)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT last_ts FROM plan_events WHERE user_id=?",
                           (user_id,)).fetchone()
        if in_order and created and row is not None and created < int(row[0]):
            conn.commit()
            return False
        conn.execute(_SET_PLAN, (plan, status, customer, customer, until, until,
                                 plan, user_id))
        if created:
            conn.execute("INSERT INTO plan_events (user_id, last_ts) VALUES (?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET "
                         "last_ts=MAX(last_ts, excluded.last_ts)",
                         (user_id, created))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def _stamp(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# --- talking to Stripe -----------------------------------------------------------

def _stripe(method: str, path: str, data: Optional[Dict] = None) -> Dict:
    import requests
    try:
        r = requests.request(method, API + path, data=data, timeout=20,
                             auth=(os.environ["STRIPE_SECRET_KEY"], ""))
    except requests.RequestException as exc:
        raise BillingError("Payments could not be reached just now. Nothing "
                           "was charged; try again in a minute.") from exc
    if r.status_code == 404 and method == "DELETE":
        return {"deleted": True}                    # already gone is done
    if r.status_code >= 400:
        raise BillingError("Payments refused the request. Nothing was charged.")
    return r.json()


def checkout_url(user, base: str, customer: str = "", call=None,
                 price_id: str = "", promotion: str = "") -> str:
    """Where to send someone to pay, on Stripe's own page."""
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id or price_id_for("monthly"),
        "line_items[0][quantity]": "1",
        "success_url": base + "/billing/done?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": base + "/pricing",
        "client_reference_id": str(user.id),
        "allow_promotion_codes": "true",
        "metadata[user_id]": str(user.id),
        # So a subscription event that arrives before the checkout one can
        # still be matched to the person.
        "subscription_data[metadata][user_id]": str(user.id),
    }
    if customer:
        data["customer"] = customer
    elif user.email and not user.email.endswith(".invalid"):
        data["customer_email"] = user.email
    if promotion:
        # Applied before Stripe's page opens, because its own box sits behind
        # a link people miss. Stripe takes one or the other, never both.
        del data["allow_promotion_codes"]
        data["discounts[0][promotion_code]"] = promotion
    return (call or _stripe)("POST", "checkout/sessions", data)["url"]


def confirm_checkout(conn: sqlite3.Connection, session_id: str, user_id: int,
                     call=None) -> bool:
    """Ask Stripe whether this person's checkout finished, and record it.

    A finished checkout stays complete and paid for ever, so on its own it
    proves only that a payment was once made: opening this page again after
    cancelling used to bring Pro back for good. The subscription it started is
    read with it, and only one still running counts.
    """
    if not session_id.startswith("cs_"):
        return False
    found = (call or _stripe)("GET", "checkout/sessions/" + session_id
                              + "?expand[]=subscription")
    if str(found.get("client_reference_id")) != str(user_id):
        return False
    if found.get("status") != "complete" or found.get("payment_status") not in PAID:
        return False
    sub = found.get("subscription")
    if not isinstance(sub, dict) or sub.get("status") not in ACTIVE:
        return False
    # Events created before this checkout began describe an earlier
    # subscription, so the checkout's own time is where ordering starts.
    _apply(conn, user_id, _stamp(found.get("created")), "pro", sub["status"],
           found.get("customer") or "", _period_end(sub), in_order=False)
    return True


# How long before a recorded period end a plan is read again from Stripe
# itself, and how many are read per run. A renewal is normally told by
# webhook, but a webhook can stop arriving without anyone noticing: a firewall
# rule in front of the site answering Stripe with 403, or a changed signing
# secret. Without this, every paying customer would stop being Pro GRACE after
# their period ended, though Stripe had charged them.
RECHECK_BEFORE = 86400
RECHECK_PER_RUN = 25


def lapsing(conn: sqlite3.Connection, now: Optional[float] = None,
            limit: int = RECHECK_PER_RUN) -> list:
    """Pro plans whose recorded period has ended, or ends within a day.

    Plans still inside their grace period come first, since they can still
    be kept, then the ones read longest ago (plan_checks). Picked by end date
    alone, the same plans came first every hour for ever when Stripe could not
    answer for them, such as test-mode customers left after switching to live
    keys, and a run's worth of those meant nobody else was ever read.
    """
    ensure_schema(conn)
    now = now or time.time()
    return conn.execute(
        "SELECT u.id AS id, u.stripe_customer AS stripe_customer, "
        "u.plan_until AS plan_until FROM users u "
        "LEFT JOIN plan_checks c ON c.user_id=u.id WHERE u.plan='pro' "
        "AND u.plan_status IN ('active', 'trialing', 'past_due') AND u.plan_until>0 "
        "AND u.plan_until<? AND u.stripe_customer<>'' "
        "ORDER BY u.plan_until+?<?, COALESCE(c.checked_ts, 0), u.plan_until LIMIT ?",
        (int(now + RECHECK_BEFORE), GRACE, int(now), int(limit))).fetchall()


def _current(subscriptions: List[Dict]) -> Optional[Dict]:
    """The subscription that decides the plan: a running one, the one whose
    period ends last if there are several, else the newest of the rest."""
    running = [s for s in subscriptions if s.get("status") in ACTIVE]
    if running:
        return max(running, key=_period_end)
    if subscriptions:
        return max(subscriptions, key=lambda s: _stamp(s.get("created")))
    return None


def recheck(conn: sqlite3.Connection, now: Optional[float] = None,
            call=None, limit: int = RECHECK_PER_RUN) -> Dict:
    """Read the plans that are lapsing with no renewal recorded from Stripe.

    Whatever Stripe says now is recorded: a renewal moves the end forward,
    a cancellation ends Pro, and an account Stripe knows nothing about is left
    for the grace period to decide. Nothing here charges or changes anything
    at Stripe; it only reads. Every plan asked about, answered or not, goes to
    the back of the queue.
    """
    if not ready():
        return {"checked": 0, "errors": []}
    now = now or time.time()
    checked, errors = 0, []
    for row in lapsing(conn, now, limit):
        customer = row["stripe_customer"]
        try:
            found = (call or _stripe)(
                "GET", "subscriptions?status=all&limit=10&customer=" + customer)
            sub = _current(found.get("data") or [])
            if sub is None:
                continue
            status = sub.get("status") or ""
            plan = "pro" if status in ACTIVE else "free"
            _apply(conn, int(row["id"]), int(now), plan, status, customer,
                   _period_end(sub), in_order=False)
            checked += 1
        except Exception as exc:          # one customer must not stop the rest
            errors.append("user %d: %s" % (int(row["id"]), exc))
        finally:
            conn.execute("INSERT INTO plan_checks (user_id, checked_ts) VALUES (?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET "
                         "checked_ts=excluded.checked_ts", (int(row["id"]), int(now)))
            conn.commit()
    return {"checked": checked, "errors": errors}


def portal_url(customer: str, return_url: str, call=None) -> str:
    """Stripe's own page for cancelling, changing card, and receipts."""
    return (call or _stripe)("POST", "billing_portal/sessions",
                             {"customer": customer,
                              "return_url": return_url})["url"]


def close_customer(customer: str, call=None) -> None:
    """Remove someone from Stripe when they delete their account.

    Deleting the customer cancels their subscriptions at once, so nobody is
    charged for an account that no longer exists.
    """
    (call or _stripe)("DELETE", "customers/" + customer)


# --- the webhook -----------------------------------------------------------------

def verify(payload: bytes, header: str, secret: str,
           now: Optional[float] = None, tolerance: int = 300) -> bool:
    """Check Stripe's signature. Anyone can post to the webhook address."""
    stamp, signatures = None, []
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            stamp = value
        elif key == "v1":
            signatures.append(value)
    try:
        ts = int(stamp or "")
    except ValueError:
        return False
    # An old, captured request replayed later is refused.
    if not signatures or abs((now or time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"),
                        ("%d." % ts).encode("utf-8") + payload,
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def _period_end(sub: Dict) -> int:
    end = sub.get("current_period_end")
    if not end:
        # Newer API versions keep the period on the subscription's items.
        items = ((sub.get("items") or {}).get("data") or [{}])
        end = items[0].get("current_period_end") if items else 0
    try:
        return int(end or 0)
    except (TypeError, ValueError):
        return 0


def handle(conn: sqlite3.Connection, event: Dict) -> str:
    """Apply one verified event. Safe to receive twice, and out of order."""
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    created = _stamp(event.get("created"))

    if kind == "checkout.session.completed":
        who = str(obj.get("client_reference_id")
                  or (obj.get("metadata") or {}).get("user_id") or "")
        if not who.isdigit():
            return "ignored"
        if obj.get("payment_status") not in PAID:
            return "awaiting payment"
        if not _apply(conn, int(who), created, "pro", "active",
                      obj.get("customer") or ""):
            return "stale"
        return "pro"

    if kind.startswith("customer.subscription."):
        customer = obj.get("customer") or ""
        row = conn.execute("SELECT id FROM users WHERE stripe_customer=?",
                           (customer,)).fetchone() if customer else None
        user_id = int(row["id"]) if row else None
        if user_id is None:
            who = str((obj.get("metadata") or {}).get("user_id") or "")
            user_id = int(who) if who.isdigit() else None
        if user_id is None:
            return "unknown customer"
        status = "canceled" if kind.endswith(".deleted") else (obj.get("status") or "")
        plan = "pro" if status in ACTIVE else "free"
        if not _apply(conn, user_id, created, plan, status, customer,
                      _period_end(obj)):
            return "stale"
        return plan

    return "ignored"
