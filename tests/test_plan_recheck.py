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
real_fingerprint = serve._code_fingerprint
check("without a release name, the code's own fingerprint stands in",
      real_fingerprint().startswith("code-") and len(real_fingerprint()) == 21,
      real_fingerprint())
serve._code_fingerprint = lambda: ""
serve._copy_before_release(DB)
check("no release name and no fingerprint: no copy", not _os.path.isdir(copies))
serve._code_fingerprint = real_fingerprint
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

head("THE SERVER ITSELF TAKES THAT COPY WHEN IT STARTS")
# serve.main([]) as the host runs it. Only web.main() sets DB_PATH, and
# serve.py never calls it: STOCKBOT_DB names the database, as render.yaml and
# the Dockerfile do. The copy was once asked of a path that was None there,
# and quietly never taken.
import waitress  # noqa: E402
import backup as backup_mod  # noqa: E402
from bot import scheduler as scheduler_mod  # noqa: E402

jobs = []


class Held:
    """Stands in for the timed backups' scheduler, and keeps its job."""

    def __init__(self, job, **_kw):
        jobs.append(job)

    def start(self):
        pass

    def stop(self):
        pass

    def check_now(self):
        pass


marker = _os.path.join(folder, "release.txt")


def release_noted():
    with open(marker, encoding="utf-8") as fh:
        return fh.read().strip()


before = held()
real = (waitress.serve, web.start_alert_loop, web.start_background_jobs,
        scheduler_mod.Scheduler)
waitress.serve = lambda *a, **k: None
web.start_alert_loop = lambda cfg: None
web.start_background_jobs = lambda: None
scheduler_mod.Scheduler = Held
web.app.config["DB_PATH"] = None
for key in [k for k in _os.environ if k.startswith("BACKUP_S3_")] + [
        "SENTRY_DSN", "STOCKBOT_BACKUP_DIR", "STOCKBOT_BACKUP_HOURS"]:
    _os.environ.pop(key, None)
_os.environ["RENDER_GIT_COMMIT"] = "ffff6666"
try:
    started = serve.main([])
finally:
    (waitress.serve, web.start_alert_loop, web.start_background_jobs,
     scheduler_mod.Scheduler) = real
    web.app.config["DB_PATH"] = DB
new = sorted(set(held()) - set(before))
check("serve.main takes the copy for a new release, and notes the release",
      started == 0 and len(new) == 1 and release_noted() == "ffff6666",
      (started, new, release_noted()))
check("the timed backups are set up beside it", len(jobs) == 1, jobs)

head("A COPY THAT DOES NOT VERIFY IS NOT KEPT")
before = held()
real_verify = backup_mod.verify
backup_mod.verify = lambda path: False
_os.environ["RENDER_GIT_COMMIT"] = "gggg7777"
try:
    serve._copy_before_release(DB)
finally:
    backup_mod.verify = real_verify
check("it is deleted, not left to be trusted", held() == before, (before, held()))
check("and the release is not noted, so the next start tries again",
      release_noted() == "ffff6666", release_noted())
_os.environ.pop("RENDER_GIT_COMMIT", None)

print()
print("%d failure(s)" % len(fails))
if fails:
    print("FAILURES:", fails)
_sys.exit(1 if fails else 0)
