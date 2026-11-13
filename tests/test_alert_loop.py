"""The background alert loop: calls that change, crossed stops, history,
email, and nobody seeing anybody else's alerts. No network: prices are stubbed."""
import os as _os
import sys as _sys
import time
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


for key in ("STOCKBOT_ADMIN_EMAIL", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"):
    _os.environ.pop(key, None)
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import alerts  # noqa: E402
from bot import database as db  # noqa: E402
from bot import scheduler  # noqa: E402

c, DB = harness.isolated_web("alertloop")
web.app.config["AUTH_MODE"] = None

PRICE = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0}


def bars_for(symbol, interval="1d"):
    if symbol not in PRICE:
        raise alerts.DataError("no such symbol")
    p = PRICE[symbol]
    return SimpleNamespace(symbol=symbol, last_price=p, close=[p] * 60, volume=[1] * 60,
                           prev_close=p, market_open=False, last_bar_time="2026-11-11 16:00",
                           meta={"regularMarketTime": 1762894800})


alerts.fetch_bars = bars_for
CALL = {"AAA": "WAIT", "BBB": "WAIT"}


def call_of(symbol, interval="1d"):
    return CALL.get(symbol)


def person(email):
    conn = db.connect(DB)
    try:
        return A.upsert_email_user(conn, email)
    finally:
        conn.close()


def sign_in(user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with c.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


alice, bob = person("alice@example.com"), person("bob@example.com")

try:
    print("=" * 72)
    print("A CHANGE OF CALL")
    print("=" * 72)
    conn = db.connect(DB)
    a_change = db.alert_add(conn, "AAA", "call_changes", repeat=True, user=alice.id)
    a_buy = db.alert_add(conn, "BBB", "call_buy", repeat=True, user=alice.id)
    out = alerts.check(conn, call_of=call_of)
    check("the first look only learns the call", not out["fired"])
    state = conn.execute("SELECT state FROM alerts WHERE id=?", (a_change,)).fetchone()[0]
    check("and remembers it", state == "WAIT", state)
    CALL.update(AAA="BUY", BBB="AVOID")
    out = alerts.check(conn, call_of=call_of)
    fired = {f.alert_id: f for f in out["fired"]}
    check("a change of call fires, saying from what to what",
          a_change in fired and "from WAIT to BUY" in fired[a_change].message,
          [f.message for f in out["fired"]])
    check("'turns BUY' does not fire for a change to AVOID", a_buy not in fired)
    check("it says when the price behind it was set",
          "UTC" in fired[a_change].as_of and "market closed" in fired[a_change].as_of,
          fired[a_change].as_of)
    check("and whose it is", fired[a_change].user_id == alice.id)
    CALL.update(BBB="BUY")
    out = alerts.check(conn, call_of=call_of)
    check("'turns BUY' fires when it does", [f.alert_id for f in out["fired"]] == [a_buy])
    out = alerts.check(conn, call_of=call_of)
    check("and not again while the call stays the same", not out["fired"])
    events = db.event_list(conn, alice.id)
    check("every fire is in the person's history",
          len(events) == 2 and {e["alert_id"] for e in events} == {a_change, a_buy})
    check("and nobody else's", not db.event_list(conn, bob.id))

    print()
    print("=" * 72)
    print("A CHECK THAT CANNOT BE DONE IS KEPT, WITH THE REASON")
    print("=" * 72)
    gone = db.alert_add(conn, "ZZZ", "price_above", threshold=5, user=bob.id)
    alerts.check(conn, call_of=call_of)
    row = conn.execute("SELECT last_error, last_checked FROM alerts WHERE id=?", (gone,)).fetchone()
    check("a symbol with no price leaves its reason on the alert",
          row["last_error"] and "No price" in row["last_error"] and row["last_checked"], row["last_error"])
    PRICE["ZZZ"] = 4.0
    alerts.check(conn, call_of=call_of)
    check("and a check that works clears it",
          conn.execute("SELECT last_error FROM alerts WHERE id=?", (gone,)).fetchone()[0] is None)

    print()
    print("=" * 72)
    print("A POSITION THAT CROSSES ITS STOP")
    print("=" * 72)
    long_id = db.open_trade(conn, "CCC", "1d", 1, entry=22.0, stop=21.0, target1=25.0,
                            quantity=10, user=alice.id)
    short_id = db.open_trade(conn, "AAA", "1d", -1, entry=95.0, stop=98.0, target1=90.0,
                             quantity=5, user=bob.id)
    fine_id = db.open_trade(conn, "BBB", "1d", 1, entry=45.0, stop=40.0, target1=60.0,
                            quantity=5, user=bob.id)
    out = alerts.check_stops(conn)
    told = {f.trade_id: f for f in out["fired"]}
    check("a long below its stop is reported to its owner",
          long_id in told and told[long_id].user_id == alice.id
          and "past the stop" in told[long_id].message)
    check("so is a short above its stop", short_id in told and told[short_id].user_id == bob.id)
    check("a position above its stop is not", fine_id not in told)
    out = alerts.check_stops(conn)
    check("each crossing is said once, not every five minutes", not out["fired"])
    check("and it is in the owner's history",
          any(e["kind"] == "stop_breach" and e["trade_id"] == long_id
              for e in db.event_list(conn, alice.id)))
    conn.close()

    print()
    print("=" * 72)
    print("EMAIL")
    print("=" * 72)
    conn = db.connect(DB)
    A.set_alert_email(conn, alice.id, True)
    sent = []

    def send(to, subject, body):
        sent.append((to, subject, body))

    hit = alerts.Fired(1, "AAA", "price_above", 90.0, 100.0, "AAA is above 90.", "",
                       alice.id, "11 Nov 21:00 UTC")
    hit.event_id = db.event_add(conn, alice.id, hit.kind, hit.symbol, hit.message, 100.0, hit.as_of)
    quiet = alerts.Fired(2, "BBB", "price_above", 40.0, 50.0, "BBB is above 40.", "",
                         bob.id, "11 Nov 21:00 UTC")
    quiet.event_id = db.event_add(conn, bob.id, quiet.kind, quiet.symbol, quiet.message, 50.0, quiet.as_of)
    out = alerts.deliver(conn, [hit, quiet], send, web._alert_address(conn))
    check("only people who asked are emailed", [s[0] for s in sent] == ["alice@example.com"], sent)
    check("with when the price was set", "price as of 11 Nov 21:00 UTC" in sent[0][2])
    check("and the send is on record",
          conn.execute("SELECT emailed FROM alert_events WHERE id=?", (hit.event_id,)).fetchone()[0])

    def broken(to, subject, body):
        raise RuntimeError("smtp down")

    again = alerts.Fired(1, "AAA", "price_above", 90.0, 100.0, "AAA is above 90 again.", "",
                         alice.id, "")
    again.event_id = db.event_add(conn, alice.id, again.kind, again.symbol, again.message)
    out = alerts.deliver(conn, [again], broken, web._alert_address(conn))
    row = conn.execute("SELECT emailed, email_error FROM alert_events WHERE id=?",
                       (again.event_id,)).fetchone()
    check("a failed send is on record too, with why", out["failed"] == 1
          and row["emailed"] is None and "smtp down" in row["email_error"])
    conn.close()

    print()
    print("=" * 72)
    print("NOBODY SEES ANYBODY ELSE'S ALERTS")
    print("=" * 72)
    loop = scheduler.Scheduler(lambda: {"fired": [
        alerts.Fired(1, "AAA", "price_above", 90.0, 100.0, "Alice's private level", "my note",
                     alice.id),
        alerts.Fired(2, "BBB", "price_above", 40.0, 50.0, "Bob's private level", "his note",
                     bob.id)]}, interval_seconds=3600, name="test-alerts")
    loop._run_once()
    real_loop, web.ALERTS = web.ALERTS, loop
    try:
        sign_in(alice)
        got = c.get("/api/alerts/status?since=0").get_json()
        check("the status feed shows a person only their own fires",
              [f["message"] for f in got["new_fires"]] == ["Alice's private level"], got["new_fires"])
        check("and not the loop's list of everyone's",
              "recent_fires" not in got and "history" not in got, list(got))
        check("only the operator can run everybody's checks early",
              c.post("/api/alerts/check-now").status_code == 403)
        sign_in(bob)
        got = c.get("/api/alerts/status?since=0").get_json()
        check("Bob sees Bob's", [f["message"] for f in got["new_fires"]] == ["Bob's private level"])
    finally:
        web.ALERTS = real_loop

    print()
    print("=" * 72)
    print("PAUSE, RESUME, EDIT, AND ONLY YOUR OWN")
    print("=" * 72)
    conn = db.connect(DB)
    level = db.alert_add(conn, "AAA", "price_above", threshold=150, user=alice.id)
    conn.close()
    sign_in(bob)
    r = c.post("/monitor/alert/pause", data={"alert_id": level})
    check("Bob cannot pause Alice's alert", r.status_code == 400)
    r = c.post("/monitor/alert/edit", data={"alert_id": level, "threshold": "1", "note": "x"})
    check("or edit it", r.status_code == 400)
    sign_in(alice)
    c.post("/monitor/alert/pause", data={"alert_id": level})
    conn = db.connect(DB)
    check("Alice can pause it",
          conn.execute("SELECT active FROM alerts WHERE id=?", (level,)).fetchone()[0] == 0)
    conn.close()
    c.post("/monitor/alert/edit", data={"alert_id": level, "threshold": "160", "note": "new high"})
    conn = db.connect(DB)
    row = conn.execute("SELECT threshold, note, active FROM alerts WHERE id=?", (level,)).fetchone()
    conn.close()
    check("and change its level and note", (row["threshold"], row["note"]) == (160.0, "new high"))
    r = c.post("/monitor/alert/resume", data={"alert_id": level})
    conn = db.connect(DB)
    check("and resume it", conn.execute("SELECT active FROM alerts WHERE id=?", (level,)).fetchone()[0] == 1)
    conn.close()
    page = c.get("/monitor").data.decode("utf-8", "replace")
    check("the Monitor page lists what it has told you",
          "What it has told you" in page and "from WAIT to BUY" in page and "Stop crossed" in page)
    check("including whether the email went", "sent" in page and "failed: smtp down" in page)
    check("the change-of-call alerts can be chosen", "Call turns BUY" in page)
finally:
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("ALERT LOOP OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
