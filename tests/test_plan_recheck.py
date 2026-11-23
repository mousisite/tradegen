"""A paid plan whose renewal never arrived by webhook is read from Stripe itself.

Pro ends GRACE after the recorded period end unless Stripe says otherwise. A
webhook that stops arriving (a firewall answering Stripe with 403, a changed
signing secret) would otherwise end Pro for people Stripe had just charged.
This also covers the copy of the database taken before a new release.
"""
import contextlib
import io
import os as _os
import sqlite3
import subprocess
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

head("PLANS STRIPE CANNOT ANSWER FOR DO NOT HOLD UP THE REST")
# Customers left behind by switching from test keys to live ones, or deleted
# at Stripe while the webhook was failing: Stripe refuses them, or has no
# subscription for them. They stay Pro on record with the oldest ends, and
# were picked first every hour, for ever: with a run's worth of them, nobody
# else was read again, and a payer whose renewal webhook was lost lost Pro.
conn = db.connect(DB)
left = []
for i in range(billing.RECHECK_PER_RUN + 5):
    u = A.upsert_email_user(conn, "left%d@example.com" % i)
    billing.set_plan(conn, u.id, "pro", "active", "cus_left_%d" % i,
                     int(NOW - (200 - i) * DAY))
    left.append("cus_left_%d" % i)
payer = A.upsert_email_user(conn, "payer@example.com")
billing.set_plan(conn, payer.id, "pro", "active", "cus_payer", int(NOW - 2 * 3600))
conn.close()
heard = []


def stripe_left(method, path, data=None):
    customer = path.rsplit("customer=", 1)[-1]
    heard.append(customer)
    if customer.startswith("cus_left_"):
        if int(customer.rsplit("_", 1)[1]) % 2:
            raise billing.BillingError("Payments refused the request. Nothing was charged.")
        return {"data": []}
    if customer == "cus_payer":
        return {"data": [{"id": "sub_p", "status": "active", "created": int(NOW - 40 * DAY),
                          "current_period_end": int(NOW + 28 * DAY)}]}
    return stripe(method, path, data)


def run_at(when):
    del heard[:]
    conn = db.connect(DB)
    try:
        return billing.recheck(conn, when, call=stripe_left)
    finally:
        conn.close()


run_at(NOW)
first_run = list(heard)
check("a plan still inside its grace period is read before ones long past it",
      "cus_payer" in first_run, first_run)
p = user(payer)
check("so the payer whose renewal webhook was lost keeps Pro, to the new end",
      billing.is_pro(p) and p.plan_until == int(NOW + 28 * DAY), (p.plan, p.plan_until))
run_at(NOW + 3600)
second_run = list(heard)
unread = [c for c in left if c not in first_run]
check("the next run reads first those the last one did not reach",
      unread and all(c in second_run for c in unread), (unread, second_run))
check("and the ones Stripe cannot answer for are still read again in turn",
      set(left) <= set(first_run) | set(second_run)
      and any(c in first_run for c in second_run), second_run)
conn = db.connect(DB)
gone = A.get_user(conn, payer.id)
A.delete_user(conn, payer.id)
try:
    kept = conn.execute("SELECT COUNT(*) FROM plan_checks WHERE user_id=?",
                        (payer.id,)).fetchone()[0]
except sqlite3.OperationalError as exc:
    kept = str(exc)
conn.close()
check("when an account goes, so does the note of when its plan was read",
      gone is not None and kept == 0, kept)

head("A PLAN WITH NO RECORDED END IS READ TOO")
# A paid checkout says nothing of when its period ends. If the subscription's
# own messages never come, nothing else would ever read the plan again.
conn = db.connect(DB)
endless = A.upsert_email_user(conn, "endless@example.com")
billing.handle(conn, {"type": "checkout.session.completed", "created": int(NOW),
                      "data": {"object": {"client_reference_id": str(endless.id),
                                          "mode": "subscription", "customer": "cus_endless",
                                          "payment_status": "paid"}}})
conn.close()
answers = {"data": []}


def stripe_endless(method, path, data=None):
    customer = path.rsplit("customer=", 1)[-1]
    heard.append(customer)
    return answers if customer == "cus_endless" else {"data": []}


def reads_at(when):
    del heard[:]
    conn = db.connect(DB)
    try:
        billing.recheck(conn, when, call=stripe_endless, limit=1000)
    finally:
        conn.close()
    return heard.count("cus_endless")


check("it is read, and one Stripe has nothing for is left alone",
      reads_at(NOW) == 1 and billing.is_pro(user(endless)))
check("but asked about once a day, not every hour",
      [reads_at(NOW + h * 3600) for h in (1, 12, 25)] == [0, 0, 1])
answers["data"] = [{"id": "sub_e", "status": "active", "created": int(NOW - 60),
                    "cancel_at_period_end": True,
                    "current_period_end": int(NOW + 30 * DAY)}]
reads_at(NOW + 2.5 * DAY)
e = user(endless)
check("its end is then known, and that it is cancelled",
      billing.is_pro(e) and (e.plan_until, e.plan_ends) == (int(NOW + 30 * DAY),) * 2,
      (e.plan_until, e.plan_ends))

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

head("THOSE COPIES GO ONCE THEY ARE A WEEK OLD")
# Each holds every account as it was, a deleted one included, and the privacy
# page says a backup holding a deleted account is gone in about a week. Kept
# by count alone, three copies stayed until three more releases, however
# many months that took.
WEEK_OLD = time.time() - 8 * DAY


def age_copies():
    for name in held():
        _os.utime(_os.path.join(copies, name), (WEEK_OLD, WEEK_OLD))


age_copies()
serve._copy_before_release(DB)          # a restart of the same release
check("a start deletes every copy more than a week old, the newest too",
      held() == [], held())

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

    def stop(self, timeout=2.0):
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
had_public = _os.environ.pop("STOCKBOT_PUBLIC_URL", None)
said = {}
try:
    started = serve.main([])
    timed = len(jobs)
    for address in ("orenth.example", "http://orenth.example", "HTTPS://orenth.example"):
        _os.environ["STOCKBOT_PUBLIC_URL"] = address
        with contextlib.redirect_stdout(io.StringIO()) as out:
            serve.main([])
        said[address] = "PUBLIC ADDRESS IS NOT HTTPS" in out.getvalue()
finally:
    _os.environ.pop("STOCKBOT_PUBLIC_URL", None)
    if had_public is not None:
        _os.environ["STOCKBOT_PUBLIC_URL"] = had_public
    (waitress.serve, web.start_alert_loop, web.start_background_jobs,
     scheduler_mod.Scheduler) = real
    web.app.config["DB_PATH"] = DB
new = sorted(set(held()) - set(before))
check("serve.main takes the copy for a new release, and notes the release",
      started == 0 and len(new) == 1 and release_noted() == "ffff6666",
      (started, new, release_noted()))
check("a public address that is not https is said loudly at the start, and only then",
      said == {"orenth.example": True, "http://orenth.example": True,
               "HTTPS://orenth.example": False}, said)
# Read once, as web.py is imported, so in a process of its own.
child = subprocess.run(
    [_sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import web; "
     "print(bool(web.app.config['SESSION_COOKIE_SECURE']), "
     "type(web.app.wsgi_app).__name__)", harness.ROOT],
    cwd=harness.ROOT, capture_output=True, text=True, timeout=120,
    env=dict(_os.environ, STOCKBOT_PUBLIC_URL="HTTPS://orenth.example",
             STOCKBOT_BEHIND_PROXY=""))
check("an https address typed in capitals still makes the cookies secure",
      child.stdout.split() == ["True", "ProxyFix"], (child.stdout, child.stderr[-300:]))
check("the timed backups are set up beside it", timed == 1, timed)
age_copies()
jobs[0]()                               # a timed backup, every 12 hours
check("each timed backup also deletes the copies a week old, so one goes "
      "however long the next release takes", held() == [], held())

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

head("A MISTYPED BACKUP SETTING NEVER STOPS THE SERVER OR ITS BACKUPS")
# 12h in the host's settings used to end every start in a traceback, with the
# site down; a blank one quietly switched backups off; and a mistyped number
# to keep failed every backup after it was taken and before it was copied
# anywhere else.
import contextlib  # noqa: E402
import io  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
from bot import offsite as offsite_mod  # noqa: E402

copied = []
intervals = []
stopped = []


class Kept(Held):
    def __init__(self, job, interval_seconds=0, **kw):
        Held.__init__(self, job, **kw)
        intervals.append(interval_seconds)

    def stop(self, timeout=0):
        stopped.append(timeout)


def start(serving=lambda *a, **k: None, **settings):
    """serve.main([]) with these settings, and the lines it printed."""
    del jobs[:], intervals[:], copied[:]
    real = (waitress.serve, web.start_alert_loop, web.start_background_jobs,
            scheduler_mod.Scheduler, offsite_mod.copy_in_background)
    waitress.serve = serving
    web.start_alert_loop = lambda cfg: None
    web.start_background_jobs = lambda: None
    scheduler_mod.Scheduler = Kept
    offsite_mod.copy_in_background = copied.append
    web.app.config["DB_PATH"] = None
    _os.environ.update(settings)
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            code = serve.main([])
            for job in jobs:
                job()
    finally:
        (waitress.serve, web.start_alert_loop, web.start_background_jobs,
         scheduler_mod.Scheduler, offsite_mod.copy_in_background) = real
        web.app.config["DB_PATH"] = DB
        for key in settings:
            _os.environ.pop(key, None)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return code, said.getvalue()


for typed in ("twelve", "12h", " ", "", "nan", "inf", "99999999"):
    code, said = start(STOCKBOT_BACKUP_HOURS=typed)
    check("hours between backups typed as %r are the usual twelve, and the log "
          "says so" % typed, code == 0 and intervals == [12 * 3600.0]
          and "STOCKBOT_BACKUP_HOURS is" in said, (code, intervals, said[-400:]))
code, said = start(STOCKBOT_BACKUP_HOURS="6")
check("a number is used as it is, with nothing to say", intervals == [6 * 3600.0]
      and "STOCKBOT_BACKUP_HOURS" not in said, (intervals, said[-400:]))
code, said = start(STOCKBOT_BACKUP_HOURS="0")
check("only 0 switches backups off", code == 0 and not jobs
      and "backups        off" in said, said[-400:])
code, said = start()
check("not set at all is twelve hours, with nothing to say",
      intervals == [12 * 3600.0] and "    setting" not in said, (intervals, said[-400:]))
for typed, kept in (("eight", 8), ("", 8), ("8.0", 8), ("nan", 8), ("3", 3),
                    ("0", 0)):
    _os.environ["STOCKBOT_BACKUP_KEEP"] = typed
    check("backups to keep typed as %r are %d" % (typed, kept),
          serve._backups_to_keep() == kept, serve._backups_to_keep())
_os.environ.pop("STOCKBOT_BACKUP_KEEP", None)
code, said = start(STOCKBOT_BACKUP_KEEP="eight")
check("with it mistyped a backup is still taken, and copied off the server",
      code == 0 and len(copied) == 1 and "STOCKBOT_BACKUP_KEEP is 'eight'" in said,
      (code, copied, said[-400:]))
real_prune = backup_mod.prune


def broken_prune(*_a, **_kw):
    raise RuntimeError("made up")


backup_mod.prune = broken_prune
try:
    code, said = start()
finally:
    backup_mod.prune = real_prune
check("nor does failing to delete old ones fail the new one", len(copied) == 1, copied)
from bot import export as export_mod  # noqa: E402

real_write = export_mod.write_csv


def broken_export(conn, name, fh, user=None):
    if name == "alerts":
        fh.write("half of it")
        raise ValueError("made up")
    return real_write(conn, name, fh, user)


export_mod.write_csv = broken_export
had = set(_os.listdir(backup_mod.folder()))
try:
    code, said = start()
finally:
    export_mod.write_csv = real_write
new = sorted(set(_os.listdir(backup_mod.folder())) - had)
check("nor does one export that cannot be written: the copy is checked and "
      "copied off the server, beside the exports that could be",
      len(copied) == 1 and _os.path.basename(copied[0]) in new
      and len([f for f in new if f.endswith(".csv")]) == len(export_mod.DATASETS) - 1
      and not any("-alerts-" in f for f in new)
      and "could not be written" in said, (copied, new, said[-300:]))
code, said = start(STOCKBOT_BEHIND_PROXY="yes please")
check("a proxy setting that is not understood is said to be off",
      code == 0 and "STOCKBOT_BEHIND_PROXY is 'yes please', which is read as off" in said,
      said[-400:])

head("NOR DOES A MISTYPED PORT OR NUMBER OF THREADS STOP IT")
listened = []


def listen(*_a, **kw):
    listened.append((kw["port"], kw["threads"]))


for typed in ("", " ", "many", "8.5", "0", "-1", "99999999"):
    del listened[:]
    try:
        code, said = start(serving=listen, PORT=typed, WEB_THREADS=typed)
    except (Exception, SystemExit) as exc:
        code, said = "%s: %s" % (type(exc).__name__, exc), ""
    check("typed as %r they are the usual 8000 and 8, and the log says so" % typed,
          code == 0 and listened == [(8000, 8)]
          and "PORT is" in said and "WEB_THREADS is" in said,
          (code, listened, said[-300:]))
code, said = start(serving=listen, PORT="10000", WEB_THREADS=" 6 ")
check("numbers are used as they are, with nothing to say",
      listened[-1] == (10000, 6) and "    setting" not in said, (listened, said[-300:]))

head("NOR DOES A SETTING OF THE ANALYSIS THAT CANNOT BE USED")
for key, typed in (("STOCKBOT_INTERVAL", "daily"), ("STOCKBOT_INTERVAL", " "),
                   ("STOCKBOT_ACCOUNT_SIZE", "nan"), ("STOCKBOT_ACCOUNT_SIZE", "-5"),
                   ("STOCKBOT_RISK_PCT", "50"), ("STOCKBOT_RISK_PCT", "1%")):
    try:
        code, said = start(**{key: typed})
    except Exception as exc:
        code = "%s: %s" % (type(exc).__name__, exc)
    check("%s typed as %r is left out" % (key, typed), code == 0, code)
_os.environ["STOCKBOT_RISK_PCT"] = "2.5"
check("one that can be is used",
      web.config_mod.load(web.app.config.get("CFG_PATH"))["risk_per_trade_pct"] == 2.5)
_os.environ.pop("STOCKBOT_RISK_PCT")

head("ERROR REPORTING STARTS BEFORE THE APP IS LOADED")
# A release that could not be loaded looped on restart with the site down, and
# error reporting heard nothing of it.
import builtins  # noqa: E402
from bot import monitoring as monitoring_mod  # noqa: E402

order = []
real_import, real_start = builtins.__import__, monitoring_mod.start


def watched(name, *args, **kwargs):
    if name == "web":
        order.append("app")
    return real_import(name, *args, **kwargs)


builtins.__import__ = watched
monitoring_mod.start = lambda *a, **k: order.append("reports")
try:
    start()
finally:
    builtins.__import__, monitoring_mod.start = real_import, real_start
check("the reports first, then the app", order[:2] == ["reports", "app"], order)

head("A COPY BEFORE A RELEASE THAT FAILS IS AN ERROR IN THE LOG")
import logging  # noqa: E402

errors = []


class Errors(logging.Handler):
    def emit(self, record):
        errors.append((record.levelname, record.getMessage()))


handler = Errors(level=logging.ERROR)
logging.getLogger("serve").addHandler(handler)
real_take, real_prune = backup_mod.take, backup_mod.prune


def no_room(*_a, **_kw):
    raise OSError("made up")


backup_mod.take = no_room
_os.environ["RENDER_GIT_COMMIT"] = "hhhh8888"
try:
    serve._copy_before_release(DB)
    backup_mod.prune = no_room
    serve._prune_release_copies(DB)
finally:
    backup_mod.take, backup_mod.prune = real_take, real_prune
    _os.environ.pop("RENDER_GIT_COMMIT", None)
    logging.getLogger("serve").removeHandler(handler)
check("so error reporting hears of it, and of old ones that cannot be deleted",
      [level for level, _ in errors] == ["ERROR", "ERROR"]
      and "before this release failed" in errors[0][1], errors)

head("THE HOST STOPPING THE SERVER STOPS IT IN ORDER")
# SIGTERM, which the host sends at every deploy, used to be ignored until the
# process was killed half a minute later.
from waitress.server import create_server  # noqa: E402


def told_to_stop(app, **settings):
    """A real waitress server, sent SIGTERM as soon as it is answering."""
    settings.update(host="127.0.0.1", port=0)
    # With no handler of our own the signal would end this suite instead.
    if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.SIG_IGN, None):
        raise RuntimeError("nothing handles SIGTERM")
    server = create_server(app, **settings)
    threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGTERM)).start()
    server.run()
    server.close()


del stopped[:]
began = time.time()
try:
    code, said = start(serving=told_to_stop)
except (Exception, SystemExit) as exc:
    code = "%s: %s" % (type(exc).__name__, exc)
check("it ends by itself, and cleanly", code == 0, code)
check("within seconds", time.time() - began < 15, time.time() - began)
check("after waiting for a backup being written", stopped and stopped[0] >= 10, stopped)

head("WHO IS ASKING, THROUGH THE REAL SERVER")
# waitress deletes the proxy's headers unless told otherwise, and every
# visitor then had the proxy's own address, over http.
import json  # noqa: E402
import subprocess  # noqa: E402

CHILD = r"""
import http.client, json, os, socket, sys, threading
sys.path[:0] = [%r, %r]
import harness
harness.isolate("serve-child")
import types
keys = []
sys.modules["dotenv"] = types.ModuleType("dotenv")
sys.modules["dotenv"].load_dotenv = keys.append
import serve, web
from flask import jsonify, request
from waitress.server import create_server

@web.app.route("/asking")
def asking():
    return jsonify(address=request.remote_addr, scheme=request.scheme,
                   host=request.host, root=request.script_root)

web.PUBLIC_ENDPOINTS.add("asking")
server = create_server(web.app, host="127.0.0.1", port=0, threads=4,
                       **serve._server_settings(web.behind_proxy()))
port = server.effective_port
threading.Thread(target=server.run, daemon=True).start()

def ask(**headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    sent = {"Host": "orenth.example"}
    sent.update({k.replace("_", "-"): v for k, v in headers.items()})
    conn.request("GET", "/asking", headers=sent)
    answer = json.loads(conn.getresponse().read())
    conn.close()
    return answer

out = {"one": ask(X_Forwarded_For="203.0.113.7", X_Forwarded_Proto="https"),
       "spoofed": ask(X_Forwarded_For="198.51.100.9, 203.0.113.7",
                      X_Forwarded_Proto="https", X_Forwarded_Host="evil.example",
                      X_Forwarded_Prefix="/evil"),
       "plain": ask(), "dotenv": len(keys)}

raw = socket.create_connection(("127.0.0.1", port), timeout=20)
raw.sendall(b"POST /trades/open HTTP/1.1\r\nHost: orenth.example\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: 300000000\r\n\r\nsymbol=")
raw.settimeout(5)
try:
    out["huge"] = raw.recv(100).split(b"\r\n")[0].decode()
except OSError as exc:
    out["huge"] = str(exc)
raw.close()
print("ANSWER " + json.dumps(out))
sys.stdout.flush()
os._exit(0)
""" % (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
       _os.path.dirname(_os.path.abspath(__file__)))


def through_waitress(**settings):
    env = {k: v for k, v in _os.environ.items()
           if k not in ("STOCKBOT_BEHIND_PROXY", "STOCKBOT_PUBLIC_URL")}
    env.update(settings)
    # It ends without tidying up, so its folders go inside one that is tidied.
    import tempfile
    room = tempfile.mkdtemp(prefix="stockbot-test-serve-child-")
    env.update(TEMP=room, TMP=room, TMPDIR=room)
    ran = subprocess.run([_sys.executable, "-c", CHILD], env=env, timeout=120,
                         capture_output=True, text=True)
    for line in ran.stdout.splitlines():
        if line.startswith("ANSWER "):
            return json.loads(line[len("ANSWER "):])
    return {"one": {}, "spoofed": {}, "plain": {}, "dotenv": None, "huge": ran.stderr[-500:]}


on = through_waitress(STOCKBOT_BEHIND_PROXY="1")
check("behind a proxy the app sees the visitor's address, scheme and host",
      on["one"] == {"address": "203.0.113.7", "scheme": "https",
                    "host": "orenth.example", "root": ""}, on)
check("what a visitor wrote in front of the proxy's own answer is not believed, "
      "nor a host or path of their choosing", on["spoofed"] == on["one"], on["spoofed"])
check("and a request the proxy added nothing to is as it came",
      on["plain"] == {"address": "127.0.0.1", "scheme": "http",
                      "host": "orenth.example", "root": ""}, on["plain"])
for label, settings in (("with no proxy", {}),
                        ("with no proxy and an https public address",
                         {"STOCKBOT_PUBLIC_URL": "https://orenth.example"})):
    off = through_waitress(**settings)
    check("%s, nobody can write their own address or scheme" % label,
          off["one"] == off["spoofed"] == off["plain"]
          and off["plain"].get("address") == "127.0.0.1"
          and off["plain"].get("scheme") == "http", off)
check("and a test run never reads the keys in a .env file", on["dotenv"] == 0, on["dotenv"])
check("a body of 300 MB is refused before it is sent",
      on["huge"].startswith("HTTP/1.") and " 413 " in on["huge"], on["huge"])

print()
print("%d failure(s)" % len(fails))
if fails:
    print("FAILURES:", fails)
_sys.exit(1 if fails else 0)
