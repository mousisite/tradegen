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
print("%d failure(s)" % len(fails))
print("OPS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
