"""Charts, error reporting, backups and the usage view.

The parts that matter here are the ones that fail quietly if they are wrong: a
chart drawn to a scale it does not state, an error report carrying somebody's
email address, a backup that is never verified, and a usage page that shows one
user another user's activity.
"""
import os as _os
import re
import sys as _sys
import tempfile
import xml.dom.minidom

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import accounts as A
from bot import charts
from bot import database as db
from bot import monitoring
from bot import usage

c, DB = harness.isolated_web("ops")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


print("=" * 72)
print("CHARTS")
print("=" * 72)

rising = [100.0 + i for i in range(60)]
def _valid(markup):
    try:
        xml.dom.minidom.parseString(markup)
        return True
    except Exception:
        return False


svg = charts.price_chart(rising, labels=("start", "end"), title="Test")
check("a price chart is produced", bool(svg))
check("the price chart is valid XML", _valid(svg))
check("it is an svg", svg.startswith("<svg") and svg.endswith("</svg>"))
check("a rising series is drawn in the gain colour", "var(--gain)" in svg)
check("a falling series is drawn in the loss colour",
      "var(--loss)" in charts.price_chart(list(reversed(rising))))

# Every gridline must be labelled with the value it sits at, or the chart is
# drawing to a scale it never states.
labels = re.findall(r'class="clab">([^<]+)</text>', svg)
gridlines = svg.count('stroke="var(--hairline)"')
print("   %d gridlines, %d labels" % (gridlines, len(labels)))
check("every gridline carries a label", len(labels) >= gridlines, labels)
numeric = [l for l in labels if re.match(r"^-?[\d,.]+$", l)]
check("the labels are numbers from the series", len(numeric) >= 2, labels)
if numeric:
    values = [float(n.replace(",", "")) for n in numeric]
    check("the axis spans the data",
          min(values) <= min(rising) and max(values) >= max(rising),
          "axis %s data %s-%s" % (values, min(rising), max(rising)))

check("too few points draws nothing", charts.price_chart([1.0, 2.0]) == "")
check("an empty series draws nothing", charts.price_chart([]) == "")
check("a flat series still draws", bool(charts.price_chart([5.0] * 30)))
check("non-finite values are dropped",
      bool(charts.price_chart([1.0, float("nan"), 2.0, 3.0, 4.0, 5.0])))

bars = charts.bar_chart([2020, 2021, 2022, 2023], [10.0, 20.0, 15.0, 30.0],
                        title="Revenue")
check("a bar chart is produced", bool(bars))
check("the bar chart is valid XML", _valid(bars))
check("one bar per year", bars.count("<rect") == 4, bars.count("<rect"))
check("each bar names its value", bars.count("<title>") == 4)
check("the first and last year are labelled", "2020" in bars and "2023" in bars)

# The operator's analyses-per-day chart: days as epoch seconds, counts.
import time  # noqa: E402
days = charts.bar_chart([1794355200, 1794441600, 1794528000], [3.0, 12.0, 7.0],
                        title="Analyses per day",
                        label=lambda ts: time.strftime("%d %b", time.gmtime(ts)).lstrip("0"),
                        fmt=lambda v: "%d" % round(v))
check("a chart of days names them as dates, not seconds",
      "1794355200" not in days and ">%s</text>" % time.strftime(
          "%d %b", time.gmtime(1794355200)).lstrip("0") in days, days[:200])
check("and says counts as counts, not percents", "%" not in days and ": 12</title>" in days)

negative = charts.bar_chart([2020, 2021], [-50.0, 80.0])
check("a loss year is drawn in the loss colour", "var(--loss)" in negative)
check("one year alone draws nothing", charts.bar_chart([2020], [10.0]) == "")

# A title attribute is inserted into markup, so it has to be escaped.
nasty = charts.bar_chart([2020, 2021], [1.0, 2.0], title='"><script>x</script>')
check("a hostile title cannot break out of the markup",
      "<script>" not in nasty, nasty[:120])
check("and the chart is still valid XML", _valid(nasty))

print()
print("=" * 72)
print("ERROR REPORTING")
print("=" * 72)

check("it is off unless a DSN is set", monitoring.status()["enabled"] is False)

event = {
    "request": {
        "headers": {"Cookie": "session=abc", "X-Api-Key": "sk-secret",
                    "Authorization": "Bearer t", "Accept": "*/*"},
        "cookies": {"session": "abc"},
        "data": {"entry": 100, "stop": 95},
        "query_string": "symbol=AAPL&token=secret",
    },
    "user": {"id": 7, "email": "someone@example.com", "username": "someone",
             "ip_address": "1.2.3.4"},
    "exception": {"values": [{"stacktrace": {"frames": [
        {"vars": {"client_secret": "GOCSPX-x", "password": "hunter2",
                  "symbol": "AAPL"}}]}}]},
}
scrubbed = monitoring._scrub(event)
request = scrubbed["request"]
check("cookies are removed entirely", "cookies" not in request)
check("submitted form data is removed", "data" not in request)
check("a cookie header is masked", request["headers"]["Cookie"] == "[removed]")
check("an api key header is masked", request["headers"]["X-Api-Key"] == "[removed]")
check("an authorization header is masked",
      request["headers"]["Authorization"] == "[removed]")
check("a harmless header survives", request["headers"]["Accept"] == "*/*")
check("a query string holding a token is masked",
      request["query_string"] == "[removed]")
check("the email address is removed", "email" not in scrubbed["user"])
check("the username is removed", "username" not in scrubbed["user"])
check("the ip address is removed", "ip_address" not in scrubbed["user"])
check("an opaque user id is kept", scrubbed["user"]["id"] == 7)

frame = scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
check("a secret local variable is masked", frame["client_secret"] == "[removed]")
check("a password local variable is masked", frame["password"] == "[removed]")
check("a harmless local variable is kept", frame["symbol"] == "AAPL")


# The calls made just before the fault travel with the report. Their paths and
# queries can hold a phone's push address, a customer's number or a key.
crumbed = monitoring._before_send({"request": {}, "breadcrumbs": {"values": [
    {"category": "httplib", "data": {
        "http.method": "POST", "url": "https://push.example.com/send/DEVICE-abc",
        "http.query": "", "http.fragment": "", "status_code": 201}},
    {"category": "httplib", "data": {
        "http.method": "GET", "url": "https://prices.example.com/v8/AAPL",
        "http.query": "interval=1d&crumb=CRUMB-xyz", "http.fragment": "frag"}},
    {"category": "bot.push", "message":
        "gone: https://push.example.com/send/DEVICE-abc?k=CRUMB-xyz after 2"},
    {"category": "query", "message": "SELECT 1", "data": None}]}}, None)
shown = repr(crumbed)
check("a report's list of calls keeps no path or query of any of them",
      not any(s in shown for s in ("DEVICE-abc", "CRUMB-xyz", "AAPL", "frag'")), shown)
first_crumb = crumbed["breadcrumbs"]["values"][0]["data"]
check("but still says which site was called, how, and what it answered",
      first_crumb["url"] == "https://push.example.com" and
      first_crumb["http.method"] == "POST" and first_crumb["status_code"] == 201,
      first_crumb)
check("a call named in a log line is cut to its site too",
      crumbed["breadcrumbs"]["values"][2]["message"]
      == "gone: https://push.example.com after 2", shown)


class Expected(Exception):
    pass


Expected.__name__ = "DataError"
check("an expected failure is not reported",
      monitoring._before_send({}, {"exc_info": (Expected, Expected(), None)})
      is None)


class Real(Exception):
    pass


check("a genuine failure is reported",
      monitoring._before_send({"request": {}},
                              {"exc_info": (Real, Real(), None)}) is not None)
check("an event with no hint is still scrubbed and sent",
      monitoring._before_send({"request": {}}, None) is not None)

print()
print("=" * 72)
print("BACKUPS")
print("=" * 72)

import backup as backup_mod

folder = tempfile.mkdtemp(prefix="stockbot-backup-test-")
_os.environ["STOCKBOT_DB"] = DB

conn = db.connect(DB)
db.open_trade(conn, "AAPL", "1d", 1, entry=100, stop=95, target1=120,
              quantity=10, user=1)
conn.close()

path = backup_mod.take(folder, with_csv=True)
check("a backup file is written", _os.path.exists(path))
check("it verifies", backup_mod.verify(path))
check("the csv exports are written alongside",
      any(f.endswith(".csv") for f in _os.listdir(folder)))

# The exports hold everybody's rows. Without whose each row is, they could not
# be handed back to anyone.
import csv as _csv  # noqa: E402
from bot import export as export_mod  # noqa: E402

conn = db.connect(DB)
other = A.upsert_email_user(conn, "second@example.com")
theirs = db.open_trade(conn, "MSFT", "1d", 1, entry=50, stop=45, target1=60,
                       quantity=1, user=other.id)
db.close_trade(conn, theirs, exit_price=55, exit_reason="target")
owned = tempfile.mkdtemp(prefix="stockbot-backup-owners-")
backup_mod.take(owned, with_csv=True)


def _table(dataset):
    name = [f for f in _os.listdir(owned) if f.startswith("stockbot-%s-" % dataset)][0]
    with open(_os.path.join(owned, name), encoding="utf-8", newline="") as fh:
        return list(_csv.reader(fh))


books = {name: _table(name) for name in export_mod.DATASETS}
check("every backup export but the shared record says whose each row is",
      all((rows[0][0] == "user_id") == (name != "strategies")
          for name, rows in books.items()), {k: v[0][:2] for k, v in books.items()})
trade_rows = books["trades"]
at = trade_rows[0].index("symbol")
check("with that person's own account number",
      {r[at]: r[0] for r in trade_rows[1:]} == {"AAPL": "1", "MSFT": str(other.id)},
      trade_rows)
check("on the exits as well",
      [r[0] for r in books["exits"][1:]] == [str(other.id)], books["exits"])
for fmt in ("csv", "json"):
    body = export_mod.build(conn, "trades", fmt, user=other.id)["body"]
    check("a person's own %s download has no such column" % fmt,
          "MSFT" in body and "user_id" not in body, body[:200])
A.delete_user(conn, other.id)
conn.close()

restored = db.connect(path)
try:
    rows = db.list_trades(restored, user=1)
    check("the backup contains the trade", len(rows) == 1 and rows[0]["symbol"] == "AAPL")
finally:
    restored.close()

corrupt = _os.path.join(folder, "corrupt.db")
with open(corrupt, "wb") as fh:
    fh.write(b"this is definitely not a database" * 40)
check("a corrupt file fails verification", backup_mod.verify(corrupt) is False)
check("a missing file fails verification",
      backup_mod.verify(_os.path.join(folder, "nope.db")) is False)

# The scheduler calls this, so it must raise something catchable rather than
# taking the process down.
_os.environ["STOCKBOT_DB"] = _os.path.join(folder, "does-not-exist.db")
try:
    backup_mod.take(folder)
    check("a missing database raises", False, "it did not raise")
except SystemExit:
    check("a missing database raises something catchable", False,
          "SystemExit would kill a scheduled run")
except FileNotFoundError:
    check("a missing database raises something catchable", True)
_os.environ["STOCKBOT_DB"] = DB

for index in range(4):
    backup_mod.take(folder)
kept_before = len([f for f in _os.listdir(folder) if f.endswith(".db")])
backup_mod.prune(folder, 2)
kept_after = len([f for f in _os.listdir(folder)
                  if f.endswith(".db") and f.startswith("stockbot-")])
print("   %d backups before pruning, %d after" % (kept_before, kept_after))
check("pruning keeps only the newest few", kept_after == 2, kept_after)

# The CSVs hold the same people's data as the copy. They used to be kept for
# ever, slowly filling the disk the database is on.
import sqlite3 as _sqlite3
import time as _time

sets = tempfile.mkdtemp(prefix="stockbot-backup-sets-")
taken = []
for index in range(3):
    taken.append(_os.path.basename(backup_mod.take(sets, with_csv=True)))
    _time.sleep(0.05)
per_copy = len([f for f in _os.listdir(sets) if f.endswith(".csv")]) // 3
backup_mod.prune(sets, 1)
left = sorted(_os.listdir(sets))
newest = taken[-1][len("stockbot-"):-len(".db")]
print("   left after pruning to one: %s" % left)
check("each backup writes its own CSVs", per_copy >= 4, per_copy)
check("pruning removes a backup's CSVs with its database copy",
      len([f for f in left if f.endswith(".csv")]) == per_copy, left)
check("what is left all belongs to the newest backup",
      all(newest in f for f in left), left)

age = backup_mod.newest_age(sets)
check("the newest backup's age is known", age is not None and age < 60, age)
check("with a recent backup the next one waits until it is due",
      backup_mod.first_run_in(sets, 3600.0) > 3000)
check("with none, the first runs soon after start",
      backup_mod.first_run_in(tempfile.mkdtemp(), 3600.0) == 300.0)
check("with only an old one, the first runs soon after start",
      backup_mod.first_run_in(sets, 30.0) == 300.0)

# A copy cut off part-way used to be left as an empty file that counted as the
# newest backup, so the next good one waited twelve hours.
cut = tempfile.mkdtemp(prefix="stockbot-backup-cut-")
real_replace = backup_mod.os.replace
during = []


def stopped_here(part, final):
    during.append((backup_mod._copies(cut), _os.listdir(cut)))
    raise KeyboardInterrupt()


backup_mod.os.replace = stopped_here
try:
    backup_mod.take(cut)
except KeyboardInterrupt:
    pass
finally:
    backup_mod.os.replace = real_replace
check("until a copy is whole it is not counted as one",
      len(during) == 1 and during[0][0] == [] and len(during[0][1]) == 1, during)
check("and a copy that could not be finished leaves nothing behind",
      _os.listdir(cut) == [], _os.listdir(cut))
broken_source = _os.path.join(folder, "broken-source.db")
with open(broken_source, "wb") as fh:
    fh.write(b"this is definitely not a database" * 400)
try:
    backup_mod.take(cut, source=broken_source)
    check("a database that cannot be read is an error", False)
except _sqlite3.DatabaseError:
    check("a database that cannot be read is an error, and leaves nothing",
          _os.listdir(cut) == [], _os.listdir(cut))
whole = _os.path.basename(backup_mod.take(cut))
old = _time.time() - 2 * 3600
for name, when in (("stockbot-2026-01-02-030405.db.part", old),
                   ("stockbot-2026-01-02-030405.db.part-journal", old),
                   ("stockbot-2026-01-02-030406.db.part", _time.time()),
                   ("stockbot-2026-01-02-030407.db-journal", old),
                   ("notes.db.part", old)):
    with open(_os.path.join(cut, name), "w") as fh:
        fh.write("x")
    _os.utime(_os.path.join(cut, name), (when, when))
backup_mod.prune(cut, 5)
check("what a killed server left behind is cleared away, but never a copy that "
      "may still be being written", sorted(_os.listdir(cut)) == sorted([
          whole, "stockbot-2026-01-02-030406.db.part", "notes.db.part"]),
      _os.listdir(cut))

# By hand on the server, a backup goes where the server keeps its own: beside
# the code is inside the container, and gone at the next deploy.
by_hand = {}
real_take, real_verify = backup_mod.take, backup_mod.verify
real_prune = backup_mod.prune
kept_env = {k: _os.environ.get(k) for k in ("STOCKBOT_BACKUP_DIR", "STOCKBOT_DB",
                                            "STOCKBOT_BACKUP_KEEP")}
backup_mod.take = lambda out, with_csv=False: by_hand.update(out=out) or out
backup_mod.verify = lambda path: True
backup_mod.prune = lambda out, keep: by_hand.update(pruned=out, keep=keep)


def _by_hand(*argv, **settings):
    for key in kept_env:
        _os.environ.pop(key, None)
    _os.environ.update(settings)
    by_hand.clear()
    backup_mod.main(list(argv))
    return dict(by_hand)


try:
    beside = _os.path.join(_os.path.dirname(_os.path.abspath(backup_mod.__file__)),
                           "backups")
    on_disk = _os.path.join(cut, "data")
    got = _by_hand(STOCKBOT_DB=_os.path.join(on_disk, "stockbot.db"))
    check("a backup by hand goes beside the server's database",
          got["out"] == got["pruned"] == _os.path.join(on_disk, "backups"), got)
    got = _by_hand(STOCKBOT_DB=_os.path.join(on_disk, "stockbot.db"),
                   STOCKBOT_BACKUP_DIR=cut)
    check("or in the folder the settings name", got["out"] == cut, got)
    check("or, with nothing set, beside the code", _by_hand()["out"] == beside)
    check("and where it is told to go, if it is told",
          _by_hand("--out", on_disk, STOCKBOT_BACKUP_DIR=cut)["out"] == on_disk)
    check("it never deletes a timed backup the server means to keep",
          [_by_hand(STOCKBOT_BACKUP_KEEP=k)["keep"]
           for k in ("30", "8", "0", "eight", "nan", "")] == [30, 14, 0, 14, 14, 14])
finally:
    backup_mod.take, backup_mod.verify, backup_mod.prune = (
        real_take, real_verify, real_prune)
    for key, value in kept_env.items():
        if value is None:
            _os.environ.pop(key, None)
        else:
            _os.environ[key] = value
if _os.name == "posix":
    was_mask = _os.umask(0o077)
    try:
        private = backup_mod.take(tempfile.mkdtemp(), with_csv=True)
        check("only the server's own user can read a backup",
              all(_os.stat(_os.path.join(_os.path.dirname(private), f)).st_mode & 0o077 == 0
                  for f in _os.listdir(_os.path.dirname(private))))
    finally:
        _os.umask(was_mask)
masks = []
real_umask = _os.umask
_os.umask = lambda m: masks.append(m) or 0o022
try:
    backup_mod.main(["--verify", _os.path.join(folder, "nope.db")])
finally:
    _os.umask = real_umask
check("what the backup command writes is for its own user alone", masks == [0o077], masks)

real_free = backup_mod._free_bytes
backup_mod._free_bytes = lambda path: 1024
try:
    before = sorted(_os.listdir(sets))
    backup_mod.take(sets, with_csv=True)
    check("a backup that would fill the disk is refused", False, "it was taken")
except OSError as exc:
    check("a backup that would fill the disk is refused", True)
    check("and says so plainly", "disk space" in str(exc), exc)
    check("and writes nothing", sorted(_os.listdir(sets)) == before)
finally:
    backup_mod._free_bytes = real_free

print()
print("=" * 72)
print("THE RUNNER OF THESE SUITES")
print("=" * 72)

import subprocess as _subprocess  # noqa: E402
import run_all  # noqa: E402

real_run = _subprocess.run


def _ran(code, out):
    return lambda *a, **kw: _subprocess.CompletedProcess(a, code, out, "")


def _stalled(*a, **kw):
    raise _subprocess.TimeoutExpired(a, 900, output=b"so far", stderr=None)


got = {}
try:
    for label, stand_in in (("pass", _ran(0, "all fine\n")), ("fail", _ran(1, "SKIP\n")),
                            ("skip", _ran(0, "SKIP  no browser here\n")),
                            ("stalled", _stalled)):
        run_all.subprocess.run = stand_in
        got[label] = run_all.run_suite("test_templates.py")
finally:
    run_all.subprocess.run = real_run
check("a suite that passes or fails is listed so",
      got["pass"][0] == "PASS" and got["fail"][0] == "FAIL", got)
check("one that could not run is listed as skipped, not as passed",
      got["skip"][0] == "SKIP", got["skip"])
check("one that never ends fails, with why, and the rest still run",
      got["stalled"][0] == "FAIL" and "so far" in got["stalled"][1]
      and "timed out" in got["stalled"][2], got["stalled"])

_AS_SUITE = '''
import sys
sys.argv[0] = %r
sys.path.insert(0, %r)
import harness, socket, tempfile
said = []
if harness.OFFLINE:
    for host in ("stock-prices.invalid", "192.0.2.1"):
        try:
            socket.create_connection((host, 80), timeout=1)
            said.append("reached")
        except OSError as exc:
            said.append("offline suite" in str(exc))
    here = socket.create_server(("127.0.0.1", 0))
    socket.create_connection(here.getsockname(), timeout=5).close()
    said.append("itself")
print([harness.OFFLINE, said])
print(tempfile.mkdtemp(prefix="stockbot-test-left-"))
'''
said_as = {}
for suite in ("test_lock.py", "test_web.py"):
    ran = _subprocess.run([_sys.executable, "-c", _AS_SUITE % (
        suite, _os.path.dirname(_os.path.abspath(__file__)))],
        capture_output=True, text=True, timeout=60)
    said_as[suite] = (ran.stdout.splitlines() + [ran.stderr, ""])[:2]
check("a suite listed as needing no network is refused any other machine, not its own",
      said_as["test_lock.py"][0] == "[True, [True, True, 'itself']]", said_as)
check("one that needs it is not", said_as["test_web.py"][0] == "[False, []]", said_as)
check("and a folder a suite makes goes when it ends",
      all(s[1] and "stockbot-test-left-" in s[1] and not _os.path.exists(s[1])
          for s in said_as.values()), said_as)

print()
print("=" * 72)
print("THE CHECK OF A DEPLOYMENT READS THE HAND-OFF TO GOOGLE")
print("=" * 72)

import preflight  # noqa: E402

real_fetch = preflight.fetch


def _hand_off(status, location):
    """The verdicts preflight gives when /auth/google answers this."""
    preflight.fetch = lambda url, **kw: {"status": status, "body": b"", "seconds": 0.0,
                                         "headers": {"location": location}, "url": url}
    report = preflight.Report()
    try:
        preflight.google_hand_off("https://example.com", report)
    finally:
        preflight.fetch = real_fetch
    return [v for v, _, _ in report.rows]


GOOGLE = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"
check("PKCE, a state and no secret pass",
      _hand_off(302, GOOGLE + "&code_challenge=abc&state=s") == ["PASS", "PASS"])
check("a missing challenge fails",
      _hand_off(302, GOOGLE + "&state=s") == ["FAIL", "PASS"])
check("a missing state fails",
      _hand_off(302, GOOGLE + "&code_challenge=abc") == ["FAIL", "PASS"])
check("the secret in the address fails",
      _hand_off(302, GOOGLE + "&code_challenge=abc&state=s&client_secret=y")
      == ["PASS", "FAIL"])
check("with Google sign-in off nothing is claimed to have been checked",
      _hand_off(503, "") == ["WARN"])


def _way_in(door):
    """What preflight says of a site whose account page asks for sign-in."""
    def answer(url, **kw):
        path = url.split("example.com", 1)[1]
        out = {"status": 302, "body": b"", "seconds": 0.0, "url": url,
               "headers": {"Location": "/signin?next=%2Faccount"}}
        if path == "/signin":
            out.update(status=200, body=door.encode("utf-8"), headers={})
        return out

    preflight.fetch = answer
    report = preflight.Report()
    try:
        preflight.run("https://example.com", report)
    finally:
        preflight.fetch = real_fetch
    return [v for v, what, _ in report.rows if what == "there is a way to sign in"]


check("a site nobody can sign in to fails the check",
      _way_in("<span>Signing in is off for now</span>") == ["FAIL"])
check("one that shows a way in passes it", _way_in("Continue with Google") == ["PASS"])

print()
print("=" * 72)
print("THE HEALTH CHECK")
print("=" * 72)

import shutil as _shutil
import sqlite3 as _sqlite3
import types as _types

healthy = c.get("/healthz")
check("a healthy server answers 200", healthy.status_code == 200)
check("and says so, and nothing else",
      healthy.get_json() == {"ok": True, "service": "stockbot"}, healthy.get_json())

real_connect = web.db_mod.connect


def unreadable(path=None):
    # Every read fails, as it does when the disk is gone. Finding out who is
    # asking used to fail first, so the answer was a 500 and a page.
    raise _sqlite3.OperationalError("database is locked: /data/stockbot.db")


web.db_mod.connect = unreadable
try:
    broken = c.get("/healthz")
finally:
    web.db_mod.connect = real_connect
check("a database it cannot read is a 503", broken.status_code == 503)
check("which says only that it is not ok", broken.get_json() == {"ok": False},
      broken.get_json())
check("never the error itself", b"locked" not in broken.data
      and b"/data" not in broken.data)

real_usage = _shutil.disk_usage
_shutil.disk_usage = lambda path: _types.SimpleNamespace(
    total=1 << 30, used=(1 << 30) - (1 << 20), free=1 << 20)
try:
    short = c.get("/healthz")
finally:
    _shutil.disk_usage = real_usage
check("a nearly full disk is reported",
      "disk" in short.get_json().get("detail", ""), short.get_json())
check("without failing the check: a restart frees no space",
      short.status_code == 200 and short.get_json().get("ok") is True)

print()
print("=" * 72)
print("FILES THAT ARE THE SAME FOR EVERYBODY")
print("=" * 72)

conn = db.connect(DB)
reader = A.upsert_email_user(conn, "reader@example.com")
reader_token = A.start_session(conn, reader.id)
conn.close()
signed_in = web.app.test_client()
with signed_in.session_transaction() as sess:
    sess.permanent = True
    sess[web.SESSION_KEY] = reader_token
opened = []


def counted(path=None):
    opened.append(path)
    return real_connect(path)


web.db_mod.connect = counted
try:
    plain_file = signed_in.get("/static/style.css")
    stamped = signed_in.get("/static/style.css?v=" + web.ASSET_VERSION)
    again = signed_in.get("/static/style.css?v=" + web.ASSET_VERSION,
                          headers={"If-None-Match": stamped.headers["ETag"]})
    other_release = signed_in.get("/static/style.css?v=0123456789ab")
    missing = signed_in.get("/static/nothing.css?v=" + web.ASSET_VERSION)
    from_files = list(opened)
finally:
    web.db_mod.connect = real_connect
check("a file is sent without opening the database, whoever asks",
      plain_file.status_code == stamped.status_code == 200 and from_files == [], from_files)
FOR_GOOD = "public, max-age=31536000, immutable"
check("one asked for by this release's version is kept for good, when asked again too",
      again.status_code == 304
      and stamped.headers["Cache-Control"] == again.headers["Cache-Control"] == FOR_GOOD,
      (stamped.headers.get("Cache-Control"), again.status_code))
check("asked for plain, by another release's, or not there, it is checked each time",
      not any("immutable" in (r.headers.get("Cache-Control") or "")
              for r in (plain_file, other_release, missing))
      and missing.status_code == 404)
for path in ("/static/style.css", "/favicon.ico", "/record.csv", "/account"):
    response = signed_in.get(path)
    check("%s does not send the sign-in cookie again" % path,
          response.status_code == 200 and "Set-Cookie" not in response.headers,
          (response.status_code, response.headers.get("Set-Cookie")))
check("and the person is still signed in",
      b"reader@example.com" in signed_in.get("/account").data)

print()
print("=" * 72)
print("THE USAGE VIEW")
print("=" * 72)

conn = db.connect(DB)
counts = usage.overview(conn, 30)
check("it reads without error", isinstance(counts, dict))
check("it counts the trade that was logged", counts["trades"] >= 1)
check("small numbers are called out",
      any("Too little use" in n or "second day" in n
          for n in usage.read(conn, 30)["notes"]))
conn.close()

check("the operator can see it", c.get("/usage").status_code == 200)

# How the timed backups are going, which only the host's log said before.
backups_here = tempfile.mkdtemp(prefix="stockbot-backup-usage-")
_os.environ["STOCKBOT_BACKUP_DIR"] = backups_here


def _backups_said(hours):
    web.app.config["BACKUP_HOURS"] = hours
    page = c.get("/usage").data.decode("utf-8", "replace")
    return " ".join(page.split("Where the data lives", 1)[1]
                    .split("Copies kept elsewhere", 1)[0].split())


try:
    said_off = _backups_said(0)
    said_none = _backups_said(12.0)
    backup_mod.take(backups_here)
    said_fresh = _backups_said(12.0)
    for f in _os.listdir(backups_here):
        stale = _time.time() - 30 * 3600
        _os.utime(_os.path.join(backups_here, f), (stale, stale))
    said_late = _backups_said(12.0)
    said_stopped = _backups_said(0)
finally:
    _os.environ.pop("STOCKBOT_BACKUP_DIR", None)
    web.app.config.pop("BACKUP_HOURS", None)
check("it says when no backups are taken",
      "No backups are being taken" in said_off and "Last backup" not in said_off, said_off)
check("when none has been taken yet", "No backup has been taken yet" in said_none, said_none)
check("when the last one was, and how many are kept",
      "Last backup: just now, 1 kept. One is taken every 12 hours." in said_fresh
      and "overdue" not in said_fresh, said_fresh)
check("and in red that one is overdue",
      "Last backup: 1d ago" in said_late and "It is overdue." in said_late
      and 'prose loss">Last backup' in said_late, said_late)
check("with the timer off, what there is is still said",
      "No backups are being taken" in said_stopped and "1 kept" in said_stopped, said_stopped)

_os.environ["GOOGLE_CLIENT_ID"] = "x.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "y"
web.app.config["AUTH_MODE"] = None
try:
    conn = db.connect(DB)
    ordinary = A.upsert_google_user(conn, {
        "email": "ordinary@example.com", "sub": "ord", "name": "Ordinary",
        "email_verified": True})
    token = A.start_session(conn, ordinary.id)
    conn.close()

    stranger = web.app.test_client()
    with stranger.session_transaction() as sess:
        sess[web.SESSION_KEY] = token

    response = stranger.get("/usage")
    check("an ordinary account is refused", response.status_code == 403,
          response.status_code)
    check("and told why", "runs this installation"
          in response.data.decode("utf-8", "replace"))

    _os.environ["STOCKBOT_ADMIN_EMAIL"] = "ordinary@example.com"
    check("naming them as operator lets them in",
          stranger.get("/usage").status_code == 200)

    _os.environ["STOCKBOT_ADMIN_EMAIL"] = "someone.else@example.com"
    check("a different operator email refuses them again",
          stranger.get("/usage").status_code == 403)

    _os.environ["STOCKBOT_ADMIN_EMAIL"] = "A@Example.COM ,ordinary@EXAMPLE.com"
    check("the operator list is case and space tolerant",
          stranger.get("/usage").status_code == 200)
finally:
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "STOCKBOT_ADMIN_EMAIL"):
        _os.environ.pop(key, None)
    web.app.config["AUTH_MODE"] = None


print()
print("=" * 72)
print("CHECKING THE MODEL'S ANSWER AGAINST THE EVIDENCE")
print("=" * 72)

from bot import reasoning as R

evidence = ("Revenue 416161000000 for 2025, up from 383285000000. "
            "Net margin 0.269. Operating cash flow 111500000000. "
            "Piotroski 8 of 9. Altman Z 12.48. Free cash flow yield 2.26%.")

grounded = R.Reasoning(
    True,
    summary="Revenue reached 416.2B, up from 383.3B, at a 26.9% net margin.",
    strongest_bull="Operating cash flow of 111.5B covers the dividend easily.",
    strongest_bear="An Altman of 12.48 says nothing about whether it is cheap.")
R.verify(grounded, evidence)
print("   grounded answer: %d figures checked, %d unverified"
      % (grounded.checked_figures, len(grounded.unverified)))
check("a grounded answer passes", grounded.grounded, grounded.unverified)
check("and it actually checked something", grounded.checked_figures >= 4)

invented = R.Reasoning(
    True,
    summary="Revenue reached 416.2B but margins fell from 31.4% to 26.9%.",
    strongest_bull="Free cash flow grew 47% last year.",
    strongest_bear="Debt of 88.3B is a concern.")
R.verify(invented, evidence)
print("   invented figures caught: %s" % invented.unverified)
check("invented figures are caught", not invented.grounded)
check("the fabricated margin is named", "31.4%" in invented.unverified)
check("the fabricated growth rate is named", "47%" in invented.unverified)
check("the fabricated debt figure is named", "88.3B" in invented.unverified)
check("the real figures are not flagged",
      not any(f.startswith("416") or f == "26.9%" for f in invented.unverified),
      invented.unverified)

# Rounding is not invention. The model is told to write plainly, so it will
# round, and flagging that would bury a real fabrication in noise.
rounded = R.Reasoning(True, summary="Margin of about 27% on revenue near 416B.")
R.verify(rounded, evidence)
check("sensible rounding is not called an invention", rounded.grounded,
      rounded.unverified)

# Prose numbers and years must not be flagged either.
prose = R.Reasoning(
    True, summary="Over the last 3 years, 2 of the 9 measures worsened.",
    strongest_bull="Since 2023 the trend has held.")
R.verify(prose, evidence)
check("small bare integers are left alone", prose.grounded, prose.unverified)

years = R.Reasoning(True, summary="Between 2019 and 2025 the picture changed.")
R.verify(years, evidence)
check("years are not treated as measurements", years.grounded, years.unverified)

empty = R.Reasoning(True, summary="The evidence is too thin to say much.")
R.verify(empty, evidence)
check("an answer with no figures is grounded", empty.grounded)
check("and reports that it checked none", empty.checked_figures == 0)

check("no evidence means no false accusations",
      R.verify(R.Reasoning(True, summary="Revenue was 999B."), "").grounded)

# The percentage form is the one most likely to trip a naive comparison: the
# evidence holds 0.269 and the model writes 26.9%.
scaled = R.Reasoning(True, summary="A net margin of 26.9%.")
R.verify(scaled, "Net margin 0.269")
check("a fraction in the evidence matches a percentage in the prose",
      scaled.grounded, scaled.unverified)


print()
print("=" * 72)
print("CHARTS DO NOT MIX CURRENCIES")
print("=" * 72)

from bot import sec as _sec

mixed = {"available": True, "mixed_currency": True, "currencies": ["JPY", "USD"],
         "table": {
             "revenue": [{"fy": 2023, "value": 3.0e13, "unit": "JPY"},
                         {"fy": 2024, "value": 3.1e13, "unit": "JPY"}],
             "net_income": [{"fy": 2023, "value": 3.4e9, "unit": "USD"},
                            {"fy": 2024, "value": 4.0e9, "unit": "USD"}]},
         "years": [2023, 2024], "derived": {}}

years, values = charts.margin_series(mixed)
check("no margin is computed across currencies", years == [] and values == [])

drawn = charts.charts_for(mixed)
check("each single line item is still drawn", "revenue" in drawn)
check("and carries the unit it was filed in", drawn.get("revenue_unit") == "JPY")
check("net income carries its own unit", drawn.get("net_income_unit") == "USD")
check("no margin chart is produced", "net_margin" not in drawn)

same = dict(mixed)
same["mixed_currency"] = False
same["currencies"] = ["USD"]
same["table"] = {
    "revenue": [{"fy": 2023, "value": 1000.0, "unit": "USD"},
                {"fy": 2024, "value": 1100.0, "unit": "USD"}],
    "net_income": [{"fy": 2023, "value": 100.0, "unit": "USD"},
                   {"fy": 2024, "value": 200.0, "unit": "USD"}]}
years, values = charts.margin_series(same)
check("one currency still produces a margin", len(years) == 2, years)
check("and the margin is right",
      abs(values[-1] - 200.0 / 1100.0) < 1e-9, values)

# Against the real filer that caused this.
toyota = _sec.financial_history("TM", years=8)
if toyota.get("available") and toyota.get("mixed_currency"):
    margins = [v.get("net_margin") for v in (toyota.get("derived") or {}).values()
               if v.get("net_margin") is not None]
    check("Toyota gets no cross-currency margin", margins == [], margins[:3])
    check("but its line items are still charted",
          bool(charts.charts_for(toyota).get("revenue")))


print()
print("=" * 72)
print("PLAIN ENGLISH")
print("=" * 72)

from bot import plain


class FakePlan:
    action = "WAIT"
    entry, stop, target1 = 100.0, 95.0, 120.0
    probability, prob_samples, prob_reliable = 0.33, 103, True
    breakeven_rate = 0.40
    # A real Plan carries what a win and a loss were actually worth; a stand-in
    # that does not is testing a shape the code never receives.
    avg_win_r, avg_loss_r = 1.50, -0.45
    position = {"quantity": 10, "risk_amount": 50.0,
                "gap_loss": 160.0, "gap_multiple": 3.2}


class FakeBars:
    currency = "USD"


said = plain.explain_plan(FakePlan(), FakeBars(), "about 2 weeks")
print("   %s" % said["headline"])
for line in said["points"]:
    print("      - %s" % line[:78])
print("   sure: %s" % said["sure"][:90])
print("   risk: %s" % said["risk"][:90])

check("a WAIT is explained as do not buy yet", "not buy yet" in said["headline"])
check("the buy price is named", "100" in " ".join(said["points"]))
check("the get-out price is named", "95" in " ".join(said["points"]))

# The whole point of the plain version: turn a rate into counted events.
check("the odds are given as counts, not a percentage",
      "103 times" in said["sure"] and "worked 34 of them" in said["sure"], said["sure"])
check("and it says the bar was not cleared",
      "not been shown to make money" in said["sure"], said["sure"])
check("gap risk is spelled out in cash", "160" in said["risk"], said["risk"])

# No jargon may leak into the plain version. This is the rule the box exists
# for, so it is asserted rather than trusted.
everything = " ".join([said["headline"], said["sure"], said["risk"], said["hold"]]
                      + said["points"]).lower()
for word in ("expectancy", "r-multiple", "confidence interval", "atr",
             "wilson", "piotroski", "altman", "beneish", "basis point",
             "sharpe", "drawdown"):
    check("the word %r never appears" % word, word not in everything)

avoid = FakePlan()
avoid.action = "AVOID"
said = plain.explain_plan(avoid, FakeBars())
check("an AVOID says leave it alone", "alone" in said["headline"])
check("and reassures that this is normal",
      "normal answer" in " ".join(said["points"]))

shorting = FakePlan()
shorting.action = "SHORT"
said = plain.explain_plan(shorting, FakeBars())
check("a SHORT warns a beginner off",
      "beginners should skip" in " ".join(said["points"]))

# Nothing may crash on missing figures, because that is the common case.
class Bare:
    action = "AVOID"
    entry = stop = target1 = probability = breakeven_rate = None
    prob_samples = 0
    prob_reliable = False
    position = {}


bare = plain.explain_plan(Bare(), FakeBars())
check("a plan with no numbers still explains itself", bool(bare["headline"]))
check("and offers no figures it does not have", bare["sure"] == "" and bare["risk"] == "")


class FakeResearch:
    sec_history = {"available": False}
    fundamentals = None
    scores = None
    valuation = None


told = plain.explain_company(FakeResearch())
check("a non-filer is explained, not blamed", told["available"] is False)
check("and the reason is plain", "crypto" in told["why"])
check("and it says the price side still works", "price side" in told["why"])

for symbol in ("AAPL", "DOGE-USD"):
    for page in ("analyse", "research"):
        body = c.get("/%s?symbol=%s&interval=1d" % (page, symbol))             .data.decode("utf-8", "replace")
        check("%s %s shows the plain box" % (symbol, page),
              'class="plainbox"' in body)

print()
print("=" * 72)
print("RESEARCH IS KEPT PER SET OF SETTINGS")
print("=" * 72)

# The thesis and the reasoning quote the plan's own stop and target, so
# research cached by ticker alone showed one person's plan to the next.
from bot import config as _config

_real_research = web.engine.research
built = []


def _counting(symbol, price, cfg, **kw):
    built.append((cfg.get("stop_atr_multiple"), kw.get("with_options")))
    if not hasattr(_counting, "found"):
        _counting.found = _real_research(symbol, price, cfg, **kw)
    return _counting.found


_kept_cfg = _config.load(harness.CFG_PATH)
web.engine.research = _counting
web._RESEARCH_CACHE.clear()
try:
    c.get("/research?symbol=AAPL&interval=1d")
    c.get("/research?symbol=AAPL&interval=1d")
    check("opening it twice with the same settings builds it once", len(built) == 1,
          built)
    _wider = dict(_kept_cfg, stop_atr_multiple=_kept_cfg["stop_atr_multiple"] * 2)
    _config.save(_wider, harness.CFG_PATH)
    c.get("/research?symbol=AAPL&interval=1d")
    check("other settings get research built from their own plan",
          len(built) == 2 and built[-1][0] == _wider["stop_atr_multiple"], built)
    c.get("/research?symbol=AAPL&interval=1d&options=0")
    check("and asking without options is not handed the copy with them",
          len(built) == 3 and built[-1][1] is False, built)
finally:
    web.engine.research = _real_research
    _config.save(_kept_cfg, harness.CFG_PATH)

print()
print("%d failure(s)" % len(fails))
print("OPS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
