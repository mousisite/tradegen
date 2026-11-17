"""A paid plan whose renewal never arrived by webhook is read from Stripe itself.

Pro ends GRACE after the recorded period end unless Stripe says otherwise. A
webhook that stops arriving (a firewall answering Stripe with 403, a changed
signing secret) would otherwise end Pro for people Stripe had just charged.
This also covers the copy of the database taken before a new release.
"""
import os as _os
import sqlite3
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print("\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72)


STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
          "STRIPE_WEBHOOK_SECRET": "whsec_test"}
for key in list(STRIPE) + ["STOCKBOT_ADMIN_EMAIL", "RENDER_GIT_COMMIT"]:
    _os.environ.pop(key, None)

import web  # noqa: E402
import serve  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import billing  # noqa: E402
from bot import database as db  # noqa: E402

c, DB = harness.isolated_web("plan-recheck")
NOW = time.time()
DAY = 86400

conn = db.connect(DB)
renewed = A.upsert_email_user(conn, "renewed@example.com")
cancelled = A.upsert_email_user(conn, "cancelled@example.com")
current = A.upsert_email_user(conn, "current@example.com")
unknown = A.upsert_email_user(conn, "unknown@example.com")
billing.set_plan(conn, renewed.id, "pro", "active", "cus_renewed", int(NOW - 5 * DAY))
billing.set_plan(conn, cancelled.id, "pro", "active", "cus_cancelled", int(NOW - 4 * DAY))
billing.set_plan(conn, current.id, "pro", "active", "cus_current", int(NOW + 20 * DAY))
billing.set_plan(conn, unknown.id, "pro", "active", "cus_unknown", int(NOW - 2 * DAY))
conn.close()

asked = []


def stripe(method, path, data=None):
    asked.append((method, path))
    customer = path.rsplit("customer=", 1)[-1]
    if customer == "cus_renewed":
        return {"data": [
            {"id": "sub_old", "status": "canceled", "created": int(NOW - 400 * DAY),
             "current_period_end": int(NOW - 300 * DAY)},
            # The newer API keeps the period on the items.
            {"id": "sub_new", "status": "active", "created": int(NOW - 40 * DAY),
             "items": {"data": [{"current_period_end": int(NOW + 25 * DAY)}]}}]}
    if customer == "cus_cancelled":
        return {"data": [{"id": "sub_c", "status": "canceled",
                          "created": int(NOW - 60 * DAY),
                          "current_period_end": int(NOW - 4 * DAY)}]}
    if customer == "cus_unknown":
        return {"data": []}
    raise AssertionError("asked about %s" % customer)


def user(u):
    conn = db.connect(DB)
    try:
        return A.get_user(conn, u.id)
    finally:
        conn.close()


head("WITH BILLING OFF NOTHING IS ASKED")
conn = db.connect(DB)
got = billing.recheck(conn, NOW, call=stripe)
conn.close()
check("no Stripe call while billing is off", got == {"checked": 0, "errors": []}
      and not asked, (got, asked))

for key, value in STRIPE.items():
    _os.environ[key] = value

head("WHO IS READ AGAIN")
conn = db.connect(DB)
due = [r["id"] for r in billing.lapsing(conn, NOW)]
conn.close()
check("plans past their end, or ending within a day, are read again",
      renewed.id in due and cancelled.id in due and unknown.id in due, due)
check("a plan with weeks left is not", current.id not in due, due)
check("before the recheck, a renewal never told by webhook has ended Pro",
      not billing.is_pro(user(renewed)))

head("WHAT STRIPE SAYS IS RECORDED")
conn = db.connect(DB)
got = billing.recheck(conn, NOW, call=stripe)
conn.close()
check("two were read and recorded, none failed", got["checked"] == 2 and not got["errors"], got)
r = user(renewed)
check("a renewal Stripe charged keeps Pro, to the new period's end",
      billing.is_pro(r) and r.plan_until == int(NOW + 25 * DAY), (r.plan, r.plan_until))
x = user(cancelled)
check("a cancellation Stripe knows of ends Pro", x.plan == "free"
      and x.plan_status == "canceled" and not billing.is_pro(x), (x.plan, x.plan_status))
u = user(unknown)
check("a customer with no subscription at Stripe is left to the grace period",
      u.plan == "pro" and billing.is_pro(u), (u.plan, u.plan_until))
check("only reads: every call was a GET of subscriptions",
      all(m == "GET" and p.startswith("subscriptions?") for m, p in asked), asked)

head("A FAILED READ DOES NOT STOP THE REST")
conn = db.connect(DB)
billing.set_plan(conn, renewed.id, "pro", "active", "cus_renewed", int(NOW - 5 * DAY))


def flaky(method, path, data=None):
    if path.endswith("cus_unknown"):
        raise billing.BillingError("Payments could not be reached just now.")
    return stripe(method, path, data)


got = billing.recheck(conn, NOW, call=flaky)
conn.close()
check("the failure is kept, and the others are still read",
      got["checked"] == 1 and len(got["errors"]) == 1, got)
check("and the renewal is recorded again", billing.is_pro(user(renewed)))

head("A STALE WEBHOOK AFTERWARDS DOES NOT UNDO IT")
conn = db.connect(DB)
outcome = billing.handle(conn, {"type": "customer.subscription.updated",
                                "created": int(NOW - 3600),
                                "data": {"object": {"customer": "cus_renewed",
                                                    "status": "canceled"}}})
conn.close()
check("an older 'canceled' delivered late is stale", outcome == "stale"
      and billing.is_pro(user(renewed)), outcome)

head("THE JOB IS STARTED WITH THE OTHERS")
check("start_background_jobs schedules it hourly",
      "_recheck_plans_job" in open(web.__file__, encoding="utf-8").read()
      and hasattr(web, "_recheck_plans_job"))

head("A COPY OF THE DATABASE BEFORE A NEW RELEASE")
folder = _os.path.dirname(DB)
copies = _os.path.join(folder, "backups", "before-release")
serve._copy_before_release(DB)
check("no release named: no copy", not _os.path.isdir(copies))
_os.environ["RENDER_GIT_COMMIT"] = "aaaa1111"
serve._copy_before_release(DB)
made = sorted(_os.listdir(copies)) if _os.path.isdir(copies) else []
check("a new release gets a copy", len([m for m in made if m.endswith(".db")]) == 1, made)


def held():
    return sorted(m for m in _os.listdir(copies) if m.endswith(".db"))


first = held()
opened = sqlite3.connect("file:%s?mode=ro" % _os.path.join(copies, first[0]), uri=True)
try:
    users = opened.execute("SELECT COUNT(*) FROM users").fetchone()[0]
finally:
    opened.close()          # Windows will not delete a file held open
check("that opens and holds the accounts", users >= 4, users)
serve._copy_before_release(DB)
check("a restart of the same release takes none", held() == first, held())
for n, release in enumerate(("bbbb2222", "cccc3333", "dddd4444", "eeee5555")):
    time.sleep(1.1)       # the copies are named to the second
    _os.environ["RENDER_GIT_COMMIT"] = release
    serve._copy_before_release(DB)
check("only the last few are kept", len(held()) == serve.KEEP_RELEASE_COPIES, held())
_os.environ.pop("RENDER_GIT_COMMIT", None)

print()
print("%d failure(s)" % len(fails))
if fails:
    print("FAILURES:", fails)
_sys.exit(1 if fails else 0)
