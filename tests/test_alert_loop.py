"""The background alert loop: calls that change, crossed stops, history,
email, and nobody seeing anybody else's alerts. Also what reaches the page,
alerts that keep watching, values and tickers that cannot work, the 52-week
and volume readings, the free plan's limit and the loop's own bookkeeping.
No network: prices are stubbed."""
import json
import os as _os
import re
import sys as _sys
import threading
import time
from types import SimpleNamespace

import numpy as np

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
# Symbols that need a whole series rather than one price, built per request.
SERIES = {}
# Every (symbol, bar size) asked for, to see which bars a check used.
ASKED = []


def bars_for(symbol, interval="1d"):
    ASKED.append((symbol, interval))
    if symbol in SERIES:
        return SERIES[symbol](interval)
    if symbol not in PRICE:
        raise alerts.DataError("no such symbol")
    p = PRICE[symbol]
    return SimpleNamespace(symbol=symbol, last_price=p, close=[p] * 60, volume=[1] * 60,
                           prev_close=p, market_open=False, last_bar_time="2026-11-11 16:00",
                           meta={"regularMarketTime": 1762894800})


DAY = 86400
# 11 Nov 2025 14:30 UTC: the open of a US session, where Yahoo stamps a daily bar.
LAST = 1762871400


def daily(closes, highs=None, lows=None, volumes=None, live=None, interval="1d"):
    """A daily series ending on LAST, one bar a day, shaped like market.Bars."""
    n = len(closes)
    close = np.asarray(closes, dtype=float)
    return SimpleNamespace(
        symbol="X", interval=interval, gmt_offset=0, market_open=False,
        timestamp=np.array([LAST - (n - 1 - i) * DAY for i in range(n)], dtype=np.int64),
        close=close,
        high=np.asarray(highs if highs is not None else closes, dtype=float),
        low=np.asarray(lows if lows is not None else closes, dtype=float),
        volume=np.asarray(volumes if volumes is not None else [100.0] * n, dtype=float),
        last_price=float(live if live is not None else close[-1]),
        prev_close=float(close[-2]), last_bar_time="",
        meta={"regularMarketTime": LAST + 6 * 3600})


alerts.fetch_bars = bars_for
CALL = {"AAA": "WAIT", "BBB": "WAIT"}


def call_of(symbol, interval="1d"):
    return CALL.get(symbol)


# "Check them all now" works a call out through the web app's own helper,
# which would run a real analysis. The stand-in answers from CALL instead.
web._call_now = call_of


def fired_ids(result):
    return [f.alert_id for f in result["fired"]]


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
    # The page is told from each person's alert history in the database, so
    # that is where these go; the loop below holds the same two in memory.
    conn = db.connect(DB)
    mine = db.event_add(conn, alice.id, "price_above", "AAA", "Alice's private level",
                        100.0, "11 Nov 21:00 UTC", alert_id=1)
    db.event_add(conn, bob.id, "price_above", "BBB", "Bob's private level", 50.0,
                 "11 Nov 21:00 UTC", alert_id=2)
    conn.close()
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
        conn = db.connect(DB)
        owners = {conn.execute("SELECT user_id FROM alert_events WHERE id=?",
                               (f["id"],)).fetchone()[0] for f in got["new_fires"]}
        conn.close()
        check("the status feed shows a person only their own fires",
              owners == {alice.id} and "Bob's private level" not in json.dumps(got)
              and got["new_fires"][0]["message"] == "Alice's private level", got["new_fires"])
        check("newest first, each with when it fired and when the price was set",
              got["new_fires"][0]["id"] == mine and got["new_fires"][0]["at"]
              and got["new_fires"][0]["as_of"] == "11 Nov 21:00 UTC")
        check("and not the loop's list of everyone's",
              "recent_fires" not in got and "history" not in got, list(got))
        check("only the operator can run everybody's checks early",
              c.post("/api/alerts/check-now").status_code == 403)
        seen = got["cursor"]
        check("a page that has seen them is told nothing more",
              c.get("/api/alerts/status?after=%d" % seen).get_json()["new_fires"] == [])
        sign_in(bob)
        got = c.get("/api/alerts/status?since=0").get_json()
        check("Bob sees Bob's", got["new_fires"][0]["message"] == "Bob's private level"
              and "Alice's private level" not in json.dumps(got), got["new_fires"])
        check("and his marker is his own",
              got["cursor"] != seen and c.get("/api/alerts/status?after=%d" % got["cursor"])
              .get_json()["new_fires"] == [])
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

    print()
    print("=" * 72)
    print("WHAT REACHES THE PAGE")
    print("=" * 72)
    sign_in(alice)
    real_loop, web.ALERTS = web.ALERTS, None
    try:
        got = c.get("/api/alerts/status").get_json()
        check("with no loop in memory, as after a restart, the fires are still there",
              got["enabled"] is False and mine in [f["id"] for f in got["new_fires"]], got)
        conn = db.connect(DB)
        old = db.event_add(conn, alice.id, "price_above", "AAA", "Last week's news", 100.0, "")
        conn.execute("UPDATE alert_events SET ts=? WHERE id=?", (int(time.time()) - 5 * DAY, old))
        conn.commit()
        conn.close()
        got = c.get("/api/alerts/status").get_json()
        check("a browser with no marker is not told last week's fire as news",
              old not in [f["id"] for f in got["new_fires"]])
        seen = got["cursor"]
        c.post("/monitor/alert/add", data={"symbol": "ccc", "kind": "price_below",
                                           "threshold": "25"})
        c.post("/monitor/alerts/check")
        got = c.get("/api/alerts/status?after=%d" % seen).get_json()
        check("a fire from 'Check them all now' reaches the page too",
              [f["symbol"] for f in got["new_fires"]] == ["CCC"], got["new_fires"])
        check("once", c.get("/api/alerts/status?after=%d" % got["cursor"])
              .get_json()["new_fires"] == [])
    finally:
        web.ALERTS = real_loop

    print()
    print("=" * 72)
    print("AN ALERT THAT KEEPS WATCHING")
    print("=" * 72)
    carol = person("carol@example.com")
    PRICE["EEE"] = 50.0
    conn = db.connect(DB)
    keep = db.alert_add(conn, "EEE", "price_above", threshold=45, repeat=True, user=carol.id)
    once = db.alert_add(conn, "EEE", "price_below", threshold=60, user=carol.id)
    first = alerts.check(conn, user=carol.id)
    second = alerts.check(conn, user=carol.id)
    check("a repeating alert fires when its condition becomes true", keep in fired_ids(first))
    check("not again on the next check while it stays true", keep not in fired_ids(second))
    PRICE["EEE"] = 40.0
    alerts.check(conn, user=carol.id)
    PRICE["EEE"] = 50.0
    third = alerts.check(conn, user=carol.id)
    check("it speaks again once the condition has cleared and come back",
          keep in fired_ids(third))
    check("and stays switched on",
          conn.execute("SELECT active FROM alerts WHERE id=?", (keep,)).fetchone()[0] == 1)
    check("two fires are two entries in the history, not one per check",
          len([e for e in db.event_list(conn, carol.id, 50) if e["alert_id"] == keep]) == 2)
    check("a one-off fires once and switches itself off",
          once in fired_ids(first)
          and conn.execute("SELECT active FROM alerts WHERE id=?", (once,)).fetchone()[0] == 0)
    db.alert_reset(conn, once, user=carol.id)
    check("re-armed while still true, a one-off says so at the next check",
          fired_ids(alerts.check(conn, user=carol.id)) == [once])
    db.alert_edit(conn, keep, 48.0, "", user=carol.id)
    check("a new level is a new question: already met, it fires",
          fired_ids(alerts.check(conn, user=carol.id)) == [keep])
    conn.close()
    sign_in(carol)
    page = c.get("/monitor").data.decode("utf-8", "replace")
    check("the page shows a repeating alert as watching, with no Re-arm",
          "tells you every time it happens" in page and page.count(">Re-arm<") == 1,
          page.count(">Re-arm<"))

    print()
    print("=" * 72)
    print("TWO CHECKS AT ONCE")
    print("=" * 72)
    dave = person("dave@example.com")
    PRICE["FFF"] = 20.0
    conn = db.connect(DB)
    both = db.alert_add(conn, "FFF", "price_above", threshold=10, user=dave.id)
    check("the database records one fire once, however often it is asked",
          db.alert_fire(conn, both, 20.0, "x", state="met", was=None) is True
          and db.alert_fire(conn, both, 20.0, "x", state="met", was=None) is False)
    db.alert_reset(conn, both, user=dave.id)
    conn.close()
    results = []

    def one_check():
        cn = db.connect(DB)
        try:
            results.append(alerts.check(cn, user=dave.id))
        finally:
            cn.close()

    # Slow prices, so both checks have read the alert before either fires it.
    alerts.fetch_bars = lambda s, i="1d": (time.sleep(0.4), bars_for(s, i))[1]
    try:
        runners = [threading.Thread(target=one_check) for _ in range(2)]
        for t in runners:
            t.start()
        for t in runners:
            t.join()
    finally:
        alerts.fetch_bars = bars_for
    told = [f for r in results for f in r["fired"] if f.alert_id == both]
    conn = db.connect(DB)
    check("two checks at once tell one fire once",
          len(results) == 2 and len(told) == 1
          and len([e for e in db.event_list(conn, dave.id) if e["alert_id"] == both]) == 1,
          len(told))
    conn.close()

    print()
    print("=" * 72)
    print("VALUES THAT CANNOT WORK")
    print("=" * 72)
    sign_in(carol)

    def add(**form):
        data = {"symbol": "EEE", "interval": "1d", "kind": "price_above", "threshold": "1"}
        data.update(form)
        return c.post("/monitor/alert/add", data=data)

    def newest(user):
        cn = db.connect(DB)
        try:
            return cn.execute("SELECT * FROM alerts WHERE user_id=? ORDER BY id DESC LIMIT 1",
                              (user.id,)).fetchone()
        finally:
            cn.close()

    for kind, value, why in (("pct_down", "-5", "a negative percent"),
                             ("pct_down", "100", "a fall of 100%"),
                             ("pct_up", "nan", "a percent that is not a number"),
                             ("pct_up", "inf", "an endless percent"),
                             ("near_52w_high", "0", "a gap of nothing"),
                             ("rsi_above", "150", "an RSI above 100"),
                             ("rsi_below", "0", "an RSI of 0"),
                             ("volume_spike", "1", "normal volume as a spike"),
                             ("volume_spike", "0", "no volume as a spike"),
                             ("price_above", "-1", "a negative price"),
                             ("price_above", "nan", "a price that is not a number")):
        r = add(kind=kind, threshold=value)
        check("refuses %s" % why, r.status_code == 400, r.status_code)
    add(kind="pct_down", threshold="0.5")
    row = newest(carol)
    check("0.5 typed is half a percent, not fifty",
          row["kind"] == "pct_down" and abs(row["threshold"] - 0.005) < 1e-12, dict(row))
    check("and is described that way",
          "0.5%" in alerts.describe("pct_down", row["threshold"], "EEE"),
          alerts.describe("pct_down", row["threshold"], "EEE"))
    add(kind="pct_down", threshold="5")
    check("5 typed is five percent", abs(newest(carol)["threshold"] - 0.05) < 1e-12)
    suggested = {s["kind"]: s for s in alerts.suggest_for("EEE", None, object())}
    check("a suggestion on an analysis posts its percent as the whole number",
          suggested["pct_down"]["form_value"] == 5.0
          and suggested["near_52w_high"]["form_value"] == 2.0
          and suggested["volume_spike"]["form_value"] == 3.0, suggested)
    add(kind="near_52w_high", threshold=str(suggested["near_52w_high"]["form_value"]))
    check("which is stored as the percent it means",
          abs(newest(carol)["threshold"] - 0.02) < 1e-12)

    conn = db.connect(DB)
    legacy = conn.execute(
        "INSERT INTO alerts (symbol, kind, threshold, interval, note, active, created_ts, "
        "repeat, user_id) VALUES ('GGG', 'pct_down', -5.0, '1d', '', 1, ?, 0, ?)",
        (int(time.time()), dave.id)).lastrowid
    conn.commit()
    SERIES["GGG"] = lambda interval: daily([100.0] * 59 + [101.0])       # up 1%
    out = alerts.check(conn, user=dave.id)
    row = conn.execute("SELECT triggered_ts, last_error FROM alerts WHERE id=?",
                       (legacy,)).fetchone()
    check("an old alert with a negative percent no longer fires on a rise",
          legacy not in fired_ids(out) and row["triggered_ts"] is None)
    check("it says what is wrong with it instead",
          "cannot work" in (row["last_error"] or ""), row["last_error"])
    conn.close()

    print()
    print("=" * 72)
    print("TICKERS AS AN ANALYSIS READS THEM, AND LIMITS ON SIZE")
    print("=" * 72)
    for typed, stored in (("btc", "BTC-USD"), ("BTCUSD", "BTC-USD"), (" aapl", "AAPL"),
                          ("eth/usdt", "ETH-USD"), ("NASDAQ:msft", "MSFT")):
        add(symbol=typed)
        check("an alert typed as %r watches %s" % (typed, stored),
              newest(carol)["symbol"] == stored, newest(carol)["symbol"])
    check("an empty ticker is refused", add(symbol="   ").status_code == 400)
    check("so is a paragraph", add(symbol="A" * 40).status_code == 400)
    for typed, stored in (("eth", "ETH-USD"), (" nvda ", "NVDA")):
        c.post("/watch/add", data={"symbol": typed, "interval": "1d"})
        conn = db.connect(DB)
        check("watching %r watches %s" % (typed, stored),
              stored in [w["symbol"] for w in db.watchlist(conn, user=carol.id)])
        conn.close()
    check("watching nothing is refused",
          c.post("/watch/add", data={"symbol": "  "}).status_code == 400)
    add(note="x" * 5000)
    check("a long note is cut to 200 characters", len(newest(carol)["note"]) == 200)
    conn = db.connect(DB)
    held = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id=?", (carol.id,)).fetchone()[0]
    conn.close()
    ceiling, db.MAX_ALERTS = db.MAX_ALERTS, held + 1
    try:
        check("one account can keep alerts up to the ceiling", add().status_code == 302)
        r = add()
        check("and not past it, on any plan",
              r.status_code == 400 and b"the most one account can keep" in r.data, r.status_code)
    finally:
        db.MAX_ALERTS = ceiling

    print()
    print("=" * 72)
    print("52-WEEK HIGHS AND LOWS, FROM A YEAR OF DAILY BARS")
    print("=" * 72)
    # 600 days: an old high of 200 more than a year back, then a year whose
    # highs top out at 101. Asked for five-minute bars, a stand-in says 100.6.
    highs = [200.0] * 100 + [101.0] * 500
    lows = [140.0] * 100 + [99.0] * 500
    closes = [150.0] * 100 + [100.0] * 500

    def year_of(live):
        return lambda interval: (daily(closes, highs, lows, live=live) if interval == "1d"
                                 else daily([100.5] * 300, [100.6] * 300, interval=interval))

    SERIES["HHH"] = year_of(100.5)
    add(symbol="hhh", kind="near_52w_high", threshold="2", interval="5m")
    hi = newest(carol)
    check("a 52-week alert is kept on daily bars whatever the form said",
          hi["interval"] == "1d", hi["interval"])
    conn = db.connect(DB)
    # As one saved before that was enforced.
    conn.execute("UPDATE alerts SET interval='5m' WHERE id=?", (hi["id"],))
    conn.commit()
    del ASKED[:]
    out = alerts.check(conn, user=carol.id)
    told = {f.alert_id: f.message for f in out["fired"]}
    check("and is read from daily bars even when saved on five-minute ones",
          ("HHH", "1d") in ASKED and ("HHH", "5m") not in ASKED, ASKED)
    check("against the last year's high, not an older one or three days of bars",
          "within 0.5% of its 52-week high of 101" in told.get(hi["id"], ""), told)
    db.alert_reset(conn, hi["id"], user=carol.id)
    SERIES["HHH"] = year_of(105.0)
    msg = {f.alert_id: f.message for f in alerts.check(conn, user=carol.id)["fired"]}.get(hi["id"], "")
    check("a price past the high is a new high, not 'within -4%'",
          "above its 52-week high of 101: a new high" in msg and "within -" not in msg, msg)
    lo = db.alert_add(conn, "HHH", "near_52w_low", threshold=0.02, user=carol.id)
    SERIES["HHH"] = year_of(80.0)
    msg = {f.alert_id: f.message for f in alerts.check(conn, user=carol.id)["fired"]}.get(lo, "")
    check("and a price under the low is a new low",
          "below its 52-week low of 99: a new low" in msg, msg)
    young = db.alert_add(conn, "JJJ", "near_52w_high", threshold=0.05, user=carol.id)
    SERIES["JJJ"] = lambda interval: daily([10.0] * 199 + [9.9], [10.2] * 200)
    msg = {f.alert_id: f.message for f in alerts.check(conn, user=carol.id)["fired"]}.get(young, "")
    check("with under a year of history it says how far back it looked",
          msg and "52-week" not in msg and "the data goes back no further" in msg, msg)
    conn.close()

    print()
    print("=" * 72)
    print("VOLUME AND RSI SAY WHICH BAR THEY READ")
    print("=" * 72)
    conn = db.connect(DB)
    spike = db.alert_add(conn, "KKK", "volume_spike", threshold=3.0, user=dave.id)
    SERIES["KKK"] = lambda interval: daily([20.0] * 30, volumes=[100.0] * 29 + [300.0])
    out = alerts.check(conn, user=dave.id)
    msg = {f.alert_id: f.message for f in out["fired"]}.get(spike, "")
    check("three times the 20 sessions before counts as three, not 2.7 with itself averaged in",
          spike in fired_ids(out), [f.message for f in out["fired"]])
    check("and the message names the session",
          "on 11 Nov" in msg and "20 sessions before" in msg, msg)
    level_rsi = db.alert_add(conn, "LLL", "rsi_above", threshold=60, user=dave.id)
    walk, price = [], 10.0
    for i in range(60):
        price += 0.3 if i % 2 == 0 else -0.1
        walk.append(price)
    SERIES["LLL"] = lambda interval: daily(walk)
    msg = {f.alert_id: f.message for f in alerts.check(conn, user=dave.id)["fired"]}.get(level_rsi, "")
    check("an RSI reading says which close it is from", "at the 11 Nov close" in msg, msg)
    conn.close()

    print()
    print("=" * 72)
    print("THE FREE PLAN'S LIMIT, EVERY WAY AN ALERT IS SWITCHED ON")
    print("=" * 72)
    from bot import billing
    STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
              "STRIPE_WEBHOOK_SECRET": "whsec_test"}
    real_stripe = billing._stripe

    def offline(method, path, data=None):
        raise billing.BillingError("no network in tests")

    billing._stripe = offline
    _os.environ.update(STRIPE)
    try:
        free = person("free@example.com")
        sign_in(free)
        PRICE["MMM"] = 30.0
        r = c.post("/monitor/alert/add", data={"symbol": "MMM", "kind": "price_above",
                                               "threshold": "10"})
        check("a free account can set its one alert", r.status_code == 302, r.status_code)
        c.post("/monitor/alerts/check")           # it fires, and a one-off switches off
        conn = db.connect(DB)
        fired_first = conn.execute("SELECT id FROM alerts WHERE user_id=? ORDER BY id LIMIT 1",
                                   (free.id,)).fetchone()[0]
        conn.close()
        check("and set another once that one has fired",
              c.post("/monitor/alert/add", data={"symbol": "MMM", "kind": "price_above",
                                                 "threshold": "50"}).status_code == 302)
        check("but not a third while one runs",
              c.post("/monitor/alert/add", data={"symbol": "MMM", "kind": "price_above",
                                                 "threshold": "60"}).status_code == 402)
        r = c.post("/monitor/alert/reset", data={"alert_id": fired_first})
        check("re-arming the fired one is refused too", r.status_code == 402, r.status_code)
        conn = db.connect(DB)
        running = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1",
                               (free.id,)).fetchone()[0]
        check("so one runs, as the plan says", running == 1, running)
        # A paid plan that lapsed with three running.
        extra = [db.alert_add(conn, "MMM", "price_above", threshold=t, user=free.id)
                 for t in (70, 80)]
        out = alerts.check(conn, user=free.id, limit_of=web._alert_limits(conn))
        check("after a paid plan lapses, only the oldest is checked", out["checked"] == 1,
              out["checked"])
        notes = [conn.execute("SELECT last_error FROM alerts WHERE id=?", (i,)).fetchone()[0]
                 for i in extra]
        check("and each of the others says why",
              all(n and "free plan checks only your oldest alert" in n for n in notes), notes)
        conn.close()
    finally:
        billing._stripe = real_stripe
        for key in STRIPE:
            _os.environ.pop(key, None)

    print()
    print("=" * 72)
    print("SWITCHING BACKGROUND CHECKS OFF AND ON AGAIN")
    print("=" * 72)
    idle = scheduler.Scheduler(lambda: {}, interval_seconds=3600, name="test-idle")
    idle.start()
    idle.stop()
    idle.set_interval(120)
    check("a stopped loop is not restarted by a new interval alone", not idle.running)
    idle.start()
    check("start brings it back", idle.running)
    idle.stop()
    _os.environ["STOCKBOT_ADMIN_EMAIL"] = "alice@example.com"
    real_loop, web.ALERTS = web.ALERTS, None
    try:
        sign_in(alice)
        c.post("/settings", data={"alert_check_minutes": "0"})
        check("switched off, nothing checks", web.ALERTS is None)
        c.post("/settings", data={"alert_check_minutes": "5"})
        check("switched on from off, the loop starts",
              web.ALERTS is not None and web.ALERTS.running)
        c.post("/settings", data={"alert_check_minutes": "0"})
        check("switched off again, it stops", not web.ALERTS.running)
        c.post("/settings", data={"alert_check_minutes": "7"})
        check("and on again, at the new interval",
              web.ALERTS.running and web.ALERTS.status()["interval"] == 420.0,
              web.ALERTS.status())
    finally:
        if web.ALERTS is not None:
            web.ALERTS.stop()
        web.ALERTS = real_loop
        _os.environ.pop("STOCKBOT_ADMIN_EMAIL", None)

    print()
    print("=" * 72)
    print("THE LOOP'S LISTS, READ WHILE THEY ARE WRITTEN")
    print("=" * 72)
    busy = scheduler.Scheduler(lambda: {"fired": [{"symbol": "X", "user_id": 0}] * 25},
                               interval_seconds=3600, name="test-busy")
    until = time.time() + 1.5
    raised = []

    def keep_writing():
        while time.time() < until:
            busy._run_once()

    writer = threading.Thread(target=keep_writing)
    writer.start()
    try:
        while time.time() < until:
            try:
                busy.unseen_fires(0)
                busy.status()
            except RuntimeError as exc:
                raised.append(str(exc))
                break
    finally:
        writer.join()
    check("reading them while the loop writes never raises", not raised, raised[:1])

    print()
    print("=" * 72)
    print("THE PAGE")
    print("=" * 72)
    sign_in(alice)
    page = c.get("/monitor").data.decode("utf-8", "replace")
    check("the fired-alert bar sits under the nav, not after the footer",
          0 < page.find('id="firebar"') < page.find('class="wrap"'),
          (page.find('id="firebar"'), page.find('class="wrap"')))
    menu = re.search(r'<select class="field" id="a-kind".*?</select>', page, re.S).group(0)
    labels = [re.sub(r"\s+", " ", t).strip()
              for t in re.findall(r"<option[^>]*>(.*?)</option>", menu, re.S)]
    check("every kind of alert reads differently in the list",
          len(labels) == len(set(labels)), labels)
    check("the 52-week ones say which is which",
          "Comes within … of its 52-week high" in labels
          and "Comes within … of its 52-week low" in labels, labels)
    check("the form says how a percent is typed", "5 means 5%" in page)
    check("a desktop notice can be asked for", 'id="notifyask"' in page)
finally:
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("ALERT LOOP OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
