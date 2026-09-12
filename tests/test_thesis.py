"""Closing out a thesis, scoring the record, and the alerts a plan suggests.

The point of writing a thesis down is finding out later whether it was right.
These cover the half that does the finding out.
"""
import os as _os
import re
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import database as db
from bot import thesis as T

c, DB = harness.isolated_web("thesis")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


print("=" * 72)
print("SCOREBOARD")
print("=" * 72)

board = T.scoreboard([
    {"outcome": "right"}, {"outcome": "right"}, {"outcome": "wrong"},
    {"outcome": "luck"}, {"outcome": "luck"}, {"outcome": "luck"},
    {"outcome": None},
])
print("   %s" % board)
check("open theses counted separately", board["open"] == 1)
check("closed theses counted", board["closed"] == 6)
check("undecided excluded from the denominator", board["decided"] == 6)
check("hit rate counts only the genuine rights",
      abs(board["hit_rate"] - 2 / 6) < 1e-9)
check("luck outnumbering skill is called out", "wrong reason" in board["note"])

thin = T.scoreboard([{"outcome": "right"}, {"outcome": "wrong"}])
check("a thin record refuses to draw a conclusion", "Too few" in thin["note"])

empty = T.scoreboard([])
check("an empty record is not an error", empty["closed"] == 0
      and empty["hit_rate"] is None)

# undecided must never count as a win, however many there are
odd = T.scoreboard([{"outcome": "undecided"}] * 9)
check("all-undecided leaves no hit rate", odd["hit_rate"] is None)
check("all-undecided has nothing decided", odd["decided"] == 0)

print()
print("=" * 72)
print("THE PROPOSED OUTCOME")
print("=" * 72)

import time
now = int(time.time())
long_ago = now - 90 * 86400

# A bullish thesis whose instrument then rose is right; the same thesis on a
# fall is wrong; a flat price decides nothing.
cases = [
    ({"price": 100.0, "balance": 0.6, "created": long_ago}, 130.0, "right"),
    ({"price": 100.0, "balance": 0.6, "created": long_ago}, 70.0, "wrong"),
    ({"price": 100.0, "balance": 0.6, "created": long_ago}, 100.5, "undecided"),
    ({"price": 100.0, "balance": -0.6, "created": long_ago}, 70.0, "right"),
    ({"price": 100.0, "balance": -0.6, "created": long_ago}, 130.0, "wrong"),
    ({"price": 100.0, "balance": 0.0, "created": long_ago}, 130.0, "undecided"),
]
for stored, price, want in cases:
    got = T.suggested_outcome(stored, price)
    check("balance %+.1f then %+.0f%% proposes %s"
          % (stored["balance"], (price / stored["price"] - 1) * 100, want),
          got == want, "got %s" % got)

check("every proposal is a recordable outcome",
      all(T.suggested_outcome(s, p) in T.OUTCOMES for s, p, _ in cases))

print()
print("=" * 72)
print("CLOSING ONE OUT")
print("=" * 72)

for sym in ("AAPL", "NVDA"):
    r = c.post("/thesis/save", data={"symbol": sym, "interval": "1d",
                                     "note": "closing test"})
    check("saved %s" % sym, r.status_code == 302, "got %d" % r.status_code)

conn = db.connect(DB)
ids = [row["id"] for row in db.thesis_list(conn)]
conn.close()
print("   saved ids %s" % ids)

body = c.get("/monitor").data.decode("utf-8", "replace")
check("the record is scored on the page", "Right for the stated reason" in body)
check("closing is offered", "Close it out" in body)
check("the four outcomes are explained", "Right for the wrong reason" in body)

block = re.search(r'<select[^>]*name="outcome".*?</select>', body, re.S)
check("an outcome select is rendered", block is not None)
if block:
    opts = re.findall(r'<option value="(\w+)"\s*(selected)?', block.group(0))
    check("all four outcomes offered", len(opts) == len(T.OUTCOMES))
    check("exactly one is proposed", sum(1 for _, sel in opts if sel) == 1)

r = c.post("/thesis/close", data={"thesis_id": ids[0], "outcome": "luck"})
check("closing accepted", r.status_code == 302, "got %d" % r.status_code)

conn = db.connect(DB)
row = db.thesis_get(conn, ids[0])
conn.close()
print("   closed at %s as %r" % (row["closed_price"], row["outcome"]))
check("outcome recorded", row["outcome"] == "luck")
check("closing price taken from the market",
      row["closed_price"] and row["closed_price"] > 0)
check("closing time recorded", bool(row["closed_ts"]))

check("a closed thesis keeps its original price", row["price"] > 0)

r = c.post("/thesis/close", data={"thesis_id": ids[1], "outcome": "nonsense"})
check("an outcome outside the vocabulary is refused", r.status_code == 400,
      "got %d" % r.status_code)
r = c.post("/thesis/close", data={"thesis_id": 999999, "outcome": "right"})
check("closing a thesis that does not exist is refused", r.status_code == 400,
      "got %d" % r.status_code)

body = c.get("/monitor").data.decode("utf-8", "replace")
check("the closed one shows its outcome", "Reopen" in body)

r = c.post("/thesis/reopen", data={"thesis_id": ids[0]})
check("reopening accepted", r.status_code == 302)
conn = db.connect(DB)
reopened = db.thesis_get(conn, ids[0])
conn.close()
check("reopening clears the outcome", reopened["outcome"] is None)
check("reopening clears the closing price", reopened["closed_price"] is None)
check("reopening clears the closing time", reopened["closed_ts"] is None)

r = c.post("/thesis/reopen", data={"thesis_id": 999999})
check("reopening a missing thesis is refused", r.status_code == 400)

print()
print("=" * 72)
print("ALERTS THE PLAN SUGGESTS")
print("=" * 72)

page = c.get("/analyse?symbol=AAPL&interval=1d").data.decode("utf-8", "replace")
check("suggestions are offered", "Worth being told about" in page)
check("no literal doubled percent reaches the page", "5%%" not in page)

pairs = re.findall(
    r'name="kind" value="(\w+)">\s*<input type="hidden" name="threshold" value="([^"]+)"',
    page)
print("   suggested: %s" % pairs)
check("more than one suggestion", len(pairs) >= 3)
check("every suggestion names a real alert kind",
      all(k in web.alerts_mod.KINDS for k, _ in pairs))
check("every suggestion carries a number",
      all(v not in ("", "None") for _, v in pairs))

kind, threshold = pairs[0]
r = c.post("/monitor/alert/add",
           data={"symbol": "AAPL", "kind": kind, "threshold": threshold,
                 "interval": "1d", "note": "from the plan",
                 "back": "/analyse?symbol=AAPL&interval=1d"})
location = r.headers.get("Location") or ""
check("setting one is accepted", r.status_code == 302, "got %d" % r.status_code)
check("it returns to the analysis", "/analyse" in location, location)
check("it says that it worked", "alert_set=1" in location, location)

conn = db.connect(DB)
stored = db.alert_list(conn)[0]
conn.close()
check("the suggested value is stored unchanged",
      abs(stored["threshold"] - float(threshold)) < 1e-9,
      "%s vs %s" % (stored["threshold"], threshold))

confirmed = c.get("/analyse?symbol=AAPL&interval=1d&alert_set=1")
check("the confirmation renders",
      "Alert set." in confirmed.data.decode("utf-8", "replace"))

# A volume multiple of 3 must not be read as three percent.
c.post("/monitor/alert/add", data={"symbol": "AAPL", "kind": "volume_spike",
                                   "threshold": "3"})
conn = db.connect(DB)
spike = [a for a in db.alert_list(conn) if a["kind"] == "volume_spike"][0]
conn.close()
check("a multiple is not converted like a percentage",
      abs(spike["threshold"] - 3.0) < 1e-9, "%s" % spike["threshold"])

print()
print("=" * 72)
print("A FORM FIELD CANNOT REDIRECT OFF THIS SITE")
print("=" * 72)

for hostile in ("//evil.example.com", "https://evil.example.com",
                "http://evil.example.com/x", "///evil.example.com"):
    r = c.post("/monitor/alert/add",
               data={"symbol": "AAPL", "kind": "price_above", "threshold": "1",
                     "back": hostile})
    location = r.headers.get("Location") or ""
    check("refuses %s" % hostile,
          "evil" not in location and not location.startswith("http"), location)

r = c.post("/thesis/delete", data={"thesis_id": ids[1],
                                   "back": "//evil.example.com"})
location = r.headers.get("Location") or ""
check("the same guard covers thesis delete", "evil" not in location, location)

print()
print("%d failure(s)" % len(fails))
print("THESIS AND SUGGESTIONS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
