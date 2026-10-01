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

Optional settings:
  PRO_PRICE_LABEL         what the pricing page says, e.g. "$9 a month"
  FREE_ANALYSES_PER_DAY   different instruments a free account can open a day (5)
  FREE_FIND_RESULTS       rows a free account sees in each Find list (3)
  FREE_ALERTS             alerts a free account can have running (1)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
from typing import Dict, Optional

API = "https://api.stripe.com/v1/"

# past_due keeps Pro while Stripe retries a failed card, rather than cutting
# someone off on the first decline.
ACTIVE = ("active", "trialing", "past_due")

# A checkout can complete while a bank debit is still clearing. That one waits
# for the subscription event saying the payment went through.
PAID = ("paid", "no_payment_required")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_views (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    item     TEXT NOT NULL,
    PRIMARY KEY (user_id, day, item)
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


def free_limits() -> Dict[str, int]:
    def num(key: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(key) or default))
        except ValueError:
            return default
    return {"analyses_per_day": num("FREE_ANALYSES_PER_DAY", 5),
            "find_results": num("FREE_FIND_RESULTS", 3),
            "alerts": num("FREE_ALERTS", 1)}


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
    return getattr(user, "plan", "free") == "pro" and \
        getattr(user, "plan_status", "") in ACTIVE


# --- the free allowance ----------------------------------------------------------

def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def may_view(conn: sqlite3.Connection, user_id: int, item: str) -> bool:
    """Whether a free account can open this instrument today.

    Counted per different instrument, not per page load: opening the same one
    again, refreshing, or moving between its analysis and research pages costs
    nothing more, so the allowance is about breadth, not about clicking.
    """
    ensure_schema(conn)
    if conn.execute("SELECT 1 FROM usage_views WHERE user_id=? AND day=? AND "
                    "item=?", (user_id, _today(), item)).fetchone():
        return True
    return views_today(conn, user_id) < free_limits()["analyses_per_day"]


def count_view(conn: sqlite3.Connection, user_id: int, item: str) -> None:
    """Record an instrument as opened today, once it has actually been shown."""
    ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO usage_views (user_id, day, item) "
                 "VALUES (?, ?, ?)", (user_id, _today(), item))
    conn.commit()


def views_today(conn: sqlite3.Connection, user_id: int) -> int:
    ensure_schema(conn)
    return conn.execute("SELECT COUNT(*) FROM usage_views WHERE user_id=? AND "
                        "day=?", (user_id, _today())).fetchone()[0]


# --- plan state ------------------------------------------------------------------

def customer_of(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT stripe_customer FROM users WHERE id=?",
                       (user_id,)).fetchone()
    return (row["stripe_customer"] if row else "") or ""


def set_plan(conn: sqlite3.Connection, user_id: int, plan: str, status: str,
             customer: str = "", until: int = 0) -> None:
    conn.execute(
        "UPDATE users SET plan=?, plan_status=?, "
        "stripe_customer=CASE WHEN ?<>'' THEN ? ELSE stripe_customer END, "
        "plan_until=CASE WHEN ?>0 THEN ? ELSE plan_until END WHERE id=?",
        (plan, status, customer, customer, until, until, user_id))
    conn.commit()


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


def checkout_url(user, base: str, customer: str = "", call=None) -> str:
    """Where to send someone to pay, on Stripe's own page."""
    data = {
        "mode": "subscription",
        "line_items[0][price]": os.environ["STRIPE_PRICE_ID"],
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
    return (call or _stripe)("POST", "checkout/sessions", data)["url"]


def confirm_checkout(conn: sqlite3.Connection, session_id: str, user_id: int,
                     call=None) -> bool:
    """Ask Stripe whether this person's checkout finished, and record it."""
    if not session_id.startswith("cs_"):
        return False
    found = (call or _stripe)("GET", "checkout/sessions/" + session_id)
    if str(found.get("client_reference_id")) != str(user_id):
        return False
    if found.get("status") != "complete" or found.get("payment_status") not in PAID:
        return False
    set_plan(conn, user_id, "pro", "active", found.get("customer") or "")
    return True


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
    """Apply one verified event. Safe to receive twice."""
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        who = str(obj.get("client_reference_id")
                  or (obj.get("metadata") or {}).get("user_id") or "")
        if not who.isdigit():
            return "ignored"
        if obj.get("payment_status") not in PAID:
            return "awaiting payment"
        set_plan(conn, int(who), "pro", "active", obj.get("customer") or "")
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
        set_plan(conn, user_id, plan, status, customer, _period_end(obj))
        return plan

    return "ignored"
