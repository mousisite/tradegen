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
# And how much history each ask wanted, as (symbol, bar size, lookback).
LOOKED = []


def bars_for(symbol, interval="1d", lookback=None):
    ASKED.append((symbol, interval))
    LOOKED.append((symbol, interval, lookback))
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
# Put back when the suite ends: the split checks stand a history in for it,
# and a position they leave open must never send the suite to the network.
REAL_HISTORY = web.market_mod.split_history

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
    row = conn.execute("SELECT last_error, last_error_ts, last_checked FROM alerts WHERE id=?",
                       (gone,)).fetchone()
    check("a symbol with no price leaves its reason on the alert",
          row["last_error"] and "No price" in row["last_error"] and row["last_error_ts"],
          row["last_error"])
    check("and does not say it was looked at, which it was not", row["last_checked"] is None,
          row["last_checked"])
    LIMITED = ("The market data source limits how often this site may ask for prices, and "
               "turned this request away. Try again in about 5 minutes. Nothing you did "
               "caused this, and your alerts are safe in the meantime, every one of them.")
    check("a long reason is kept to a whole sentence",
          alerts._whole(LIMITED) == LIMITED[:LIMITED.index(" Nothing")], alerts._whole(LIMITED))
    check("and one long sentence to a whole word",
          alerts._whole("alpha " * 60).endswith("alpha...")
          and len(alerts._whole("alpha " * 60)) <= 190, alerts._whole("alpha " * 60))
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
    print("A TEN-FOR-ONE SPLIT")
    print("=" * 72)
    # Before it, about 1,000 a share; after, about 100. Yahoo rescales the
    # whole history, so every price stored before the split is ten times
    # today's quote.
    conn = db.connect(DB)
    split_at = LAST - 3 * DAY
    # The symbol's whole split history, as the source would list it. Stand-in
    # bars that do not reach back to a level's day send the look-up there.
    web.market_mod.split_history = (lambda symbol: [(split_at, 10.0)]
                                    if symbol in ("SPL", "SP2") else [])

    def split_bars(interval="1d"):
        b = daily([100.0] * 30, live=101.0, interval=interval)
        b.splits = [(split_at, 10.0)]
        return b

    SERIES["SPL"] = split_bars
    before = split_at - 30 * DAY
    below = db.alert_add(conn, "SPL", "price_below", threshold=900.0, user=alice.id)
    above = db.alert_add(conn, "SPL", "price_above", threshold=1100.0, user=alice.id)
    conn.execute("UPDATE alerts SET created_ts=? WHERE id IN (?, ?)", (before, below, above))
    conn.commit()
    out = alerts.check(conn, call_of=call_of)
    check("a 'falls below 900' set before a ten-for-one does not fire at 101",
          not any(f.alert_id in (below, above) for f in out["fired"]),
          [f.message for f in out["fired"]])
    level = {r["id"]: r["threshold"] for r in
             conn.execute("SELECT id, threshold FROM alerts WHERE id IN (?, ?)", (below, above))}
    check("both levels are moved into today's shares",
          abs(level[below] - 90.0) < 1e-9 and abs(level[above] - 110.0) < 1e-9, level)
    check("and the owner is told in the history",
          any(e["kind"] == "split" and e["alert_id"] == below and "10-for-1" in e["message"]
              for e in db.event_list(conn, alice.id, limit=50)))
    alerts.check(conn, call_of=call_of)
    check("a second check does not move them again",
          conn.execute("SELECT threshold FROM alerts WHERE id=?", (below,)).fetchone()[0]
          == level[below])
    after = db.alert_add(conn, "SPL", "price_below", threshold=95.0, user=alice.id)
    alerts.check(conn, call_of=call_of)
    check("an alert set after the split is left as set",
          conn.execute("SELECT threshold FROM alerts WHERE id=?", (after,)).fetchone()[0] == 95.0)
    held = db.open_trade(conn, "SPL", "1d", 1, entry=1000.0, stop=950.0, target1=1200.0,
                         quantity=2, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (before, held))
    conn.commit()
    out = alerts.check_stops(conn)
    check("a long opened at 1,000 before the split is not past its stop at 101",
          not any(f.trade_id == held for f in out["fired"]),
          [f.message for f in out["fired"]])
    real_fetch = web.market_mod.fetch_bars
    web.market_mod.fetch_bars = bars_for
    try:
        mark = web._mark_one(conn.execute("SELECT * FROM trades WHERE id=?", (held,)).fetchone())
    finally:
        web.market_mod.fetch_bars = real_fetch
    check("the journal marks it in the shares it was opened with",
          abs(mark["price"] - 1010.0) < 1e-9 and abs(mark["r"] - 0.2) < 1e-9
          and mark["status"] == "open" and mark["split_words"] == "10-for-1", mark)
    check("a thesis saved before the split is priced in its own shares",
          web._thesis_price({"symbol": "SPL", "created_ts": before}, split_bars())
          == (1010.0, 10.0))

    # A level typed after the split is in today's shares already, and is
    # never divided again: paused through the split, edited, resumed.
    edited = db.alert_add(conn, "SPL", "price_below", threshold=900.0, user=alice.id)
    conn.execute("UPDATE alerts SET created_ts=? WHERE id=?", (before, edited))
    conn.commit()
    db.alert_set_active(conn, edited, False, user=alice.id)
    alerts.check(conn, call_of=call_of)
    db.alert_edit(conn, edited, 95.0, "", user=alice.id)
    db.alert_set_active(conn, edited, True, user=alice.id)
    out = alerts.check(conn, call_of=call_of)
    check("a level typed after the split is left as typed",
          conn.execute("SELECT threshold FROM alerts WHERE id=?", (edited,)).fetchone()[0] == 95.0
          and edited not in fired_ids(out))
    conn.execute("UPDATE alerts SET last_error='Moved from X' WHERE id=?", (edited,))
    conn.commit()
    db.alert_set_active(conn, edited, False, user=alice.id)
    db.alert_set_active(conn, edited, True, user=alice.id)
    check("resuming an alert clears what was kept from before the pause",
          conn.execute("SELECT last_error FROM alerts WHERE id=?", (edited,)).fetchone()[0] is None)

    # A 15-minute alert reads days of bars, which start after the split: it
    # comes from the symbol's whole history.
    SERIES["SP2"] = lambda interval="15m": daily([100.0] * 2, live=101.0, interval=interval)
    short = db.alert_add(conn, "SP2", "price_below", threshold=900.0, interval="15m",
                         user=alice.id)
    conn.execute("UPDATE alerts SET created_ts=? WHERE id=?", (before, short))
    conn.commit()
    out = alerts.check(conn, call_of=call_of)
    check("an alert whose bars do not reach the split still has it applied",
          short not in fired_ids(out) and abs(conn.execute(
              "SELECT threshold FROM alerts WHERE id=?", (short,)).fetchone()[0] - 90.0) < 1e-9)

    # A history kept from before today's split must not hide the split the
    # bars themselves list.
    web.market_mod.split_history = lambda symbol: []
    fresh = daily([100.0] * 30, live=101.0)
    fresh.splits = [(split_at, 10.0)]
    check("a split in the bars counts even when a kept history predates it",
          web.market_mod.split_factor_since("SPL", before, fresh) == 10.0)

    # With no history to hand, nothing that acts on prices from before the
    # bars goes ahead: it waits for the next round.
    def no_answer(symbol):
        raise web.market_mod.DataUnavailable("the source did not answer")

    web.market_mod.split_history = no_answer
    SERIES["OUT"] = lambda interval="1d": daily([100.0] * 30, live=101.0, interval=interval)
    waits = db.open_trade(conn, "OUT", "1d", 1, entry=1000.0, stop=950.0, target1=1200.0,
                          quantity=1, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (before, waits))
    conn.commit()
    res = alerts.check_stops(conn)
    check("with no split history to hand, a position from before its bars is not told",
          not any(f.trade_id == waits for f in res["fired"])
          and conn.execute("SELECT breach_noticed FROM trades WHERE id=?",
                           (waits,)).fetchone()[0] is None)
    held_back = db.alert_add(conn, "OUT", "price_below", threshold=900.0, user=alice.id)
    conn.execute("UPDATE alerts SET created_ts=? WHERE id=?", (before, held_back))
    conn.commit()
    res = alerts.check(conn, call_of=call_of)
    kept = conn.execute("SELECT active, last_error FROM alerts WHERE id=?",
                        (held_back,)).fetchone()
    check("and a price alert from before them waits, saying why",
          held_back not in fired_ids(res) and kept["active"] == 1
          and "split history" in (kept["last_error"] or ""), dict(kept))
    check("and the check reports it, so a routine does not say all is well",
          any("split history" in e["error"] for e in res["errors"]), res["errors"])
    db.alert_delete(conn, held_back, user=alice.id)
    conn.execute("UPDATE trades SET breach_noticed=1 WHERE id=?", (waits,))
    conn.commit()
    web.market_mod.split_history = (lambda symbol: [(split_at, 10.0)]
                                    if symbol in ("SPL", "SP2") else [])

    # A stop that traded and came back: the journal counts it as hit, so the
    # owner is told, once.
    def dipped(interval="1d"):
        lows = [100.0] * 30
        lows[25] = 94.0
        return daily([100.0] * 30, lows=lows, live=99.0, interval=interval)

    SERIES["TCH"] = dipped
    dip = db.open_trade(conn, "TCH", "1d", 1, entry=100.0, stop=95.0, target1=110.0,
                        quantity=1, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (LAST - 20 * DAY, dip))
    conn.commit()
    out = alerts.check_stops(conn)
    said = [f.message for f in out["fired"] if f.trade_id == dip]
    check("a stop that traded and came back is told, as the journal counts it",
          len(said) == 1 and "traded at 94" in said[0], said)

    # Two checks at once both read the trade before either marks it.
    SERIES["OVL"] = lambda interval="1d": daily([100.0] * 30, live=90.0, interval=interval)
    ovl = db.open_trade(conn, "OVL", "1d", 1, entry=100.0, stop=95.0, target1=110.0,
                        quantity=1, user=alice.id)
    real_fetch_bars = alerts.fetch_bars

    def slow(symbol, interval="1d", lookback=None):
        time.sleep(0.3)
        return bars_for(symbol, interval, lookback)

    alerts.fetch_bars = slow
    told = []

    def run_check():
        k = db.connect(DB)
        try:
            told.append(alerts.check_stops(k))
        finally:
            k.close()

    import threading as _threading
    pair = [_threading.Thread(target=run_check) for _ in range(2)]
    for th in pair:
        th.start()
    for th in pair:
        th.join()
    alerts.fetch_bars = real_fetch_bars
    fired_ovl = sum(1 for res in told for f in res["fired"] if f.trade_id == ovl)
    stored_ovl = conn.execute("SELECT COUNT(*) FROM alert_events WHERE trade_id=?",
                              (ovl,)).fetchone()[0]
    check("two checks at once tell a crossed stop once",
          fired_ovl == 1 and stored_ovl == 1, (fired_ovl, stored_ovl))

    # Pro is promised as many alerts as it likes; anyone else keeps 200.
    many = person("many@example.com")
    for k in range(db.MAX_ALERTS):
        db.alert_add(conn, "AAA", "price_above", threshold=1000.0 + k, user=many.id)
    try:
        db.alert_add(conn, "AAA", "price_above", threshold=5000.0, user=many.id)
        refused = False
    except ValueError:
        refused = True
    check("an account not on Pro keeps at most 200 alerts", refused)
    real_pro = web._pro
    web._pro = lambda: True
    sign_in(many)
    try:
        r = c.post("/monitor/alert/add", data={"symbol": "AAA", "kind": "price_above",
                                               "threshold": "6000"})
    finally:
        web._pro = real_pro
    check("but a Pro account is let past it",
          r.status_code == 302 and conn.execute(
              "SELECT COUNT(*) FROM alerts WHERE user_id=?", (many.id,)).fetchone()[0]
          == db.MAX_ALERTS + 1, r.status_code)
    conn.execute("DELETE FROM alerts WHERE user_id=?", (many.id,))
    conn.commit()
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
    check("a failed send is on record too, in words of its own", out["failed"] == 1
          and row["emailed"] is None and row["email_error"] == alerts.NOT_SENT
          and "Google" not in alerts.NOT_SENT, row["email_error"])
    long = alerts.Fired(1, "BTC-USD", "sweeps_prior_high", None, 96900.0,
                        "BTC-USD took yesterday's high of 97,250 and closed back below it, at "
                        "96,900, in the bar that opened at 10:00 New York time on 19 Nov.", "",
                        alice.id, "")
    del sent[:]
    alerts.deliver(conn, [long], send, web._alert_address(conn), site="https://example.com/")
    check("a long subject ends on a whole word",
          sent[0][1] == "Orenth: BTC-USD took yesterday's high of 97,250 and closed back...", sent[0][1])
    check("and the body says where the Monitor page is",
          "https://example.com/monitor\n" in sent[0][2], sent[0][2])
    slow = alerts.Fired(1, "AAA", "price_above", 90.0, 100.0, "AAA is above 90 once more.", "",
                        alice.id, "")
    slow.event_id = db.event_add(conn, alice.id, slow.kind, slow.symbol, slow.message)
    real_mail = (web.codes_mod.email_ready, web.codes_mod.send_mail)
    web.codes_mod.email_ready = lambda: True
    web.codes_mod.send_mail = lambda *a, **k: (time.sleep(1.5), send(*a, **k))
    del sent[:]
    try:
        started = time.time()
        web._tell([slow])
        waited = time.time() - started
    finally:
        web.codes_mod.email_ready, web.codes_mod.send_mail = real_mail
    check("a slow mail server holds up neither a check nor a page", waited < 1.0, waited)
    check("which is still sent, and on record", alerts.mail_idle(10)
          and [s[0] for s in sent] == ["alice@example.com"]
          and conn.execute("SELECT emailed FROM alert_events WHERE id=?",
                           (slow.event_id,)).fetchone()[0], sent)
    conn.close()

    print()
    print("=" * 72)
    print("A ROUND THAT BREAKS OFF STILL TELLS WHAT FIRED")
    print("=" * 72)
    conn = db.connect(DB)
    lost = person("lost@example.com")
    level = db.alert_add(conn, "AAA", "price_above", threshold=90, user=lost.id)
    told, real = [], (alerts.check_stops, web._tell)

    def busy(*args, **kwargs):
        raise RuntimeError("database is locked")

    alerts.check_stops, web._tell = busy, lambda fired: told.extend(fired)
    try:
        try:
            web._check_and_tell(conn, user=lost.id)
            raised = False
        except RuntimeError:
            raised = True
    finally:
        alerts.check_stops = real[0]
    check("what fired before the stops failed is told", [f.alert_id for f in told] == [level], told)
    check("and the failure still reaches the caller", raised)
    del told[:]
    second = db.alert_add(conn, "BBB", "price_above", threshold=40, user=lost.id)
    real_add = db.event_add

    def no_event(*args, **kwargs):
        raise RuntimeError("database is locked")

    db.event_add = no_event
    try:
        web._check_and_tell(conn, user=lost.id)
    except RuntimeError:
        pass
    finally:
        db.event_add = real_add
    check("so is one whose place in the history could not be written",
          [f.alert_id for f in told] == [second], told)
    del told[:]
    web._check_and_tell(conn, user=lost.id)
    web._tell = real[1]
    check("and the next round tells neither again", told == [], told)
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
        odd = {q: c.get("/api/alerts/status?" + q).status_code for q in (
            "since=nan", "since=inf", "since=-inf", "since=1e300", "since=abc",
            "after=" + "9" * 30, "after=-" + "9" * 30, "after=" + "9" * 6000)}
        check("a marker that cannot be one is read as none", set(odd.values()) == {200}, odd)
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
    check("including whether the email went",
          "sent" in page and "failed: it could not be sent just now" in page)
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
    db.alert_edit(conn, keep, 48.0, "a note alone", user=carol.id)
    row = conn.execute("SELECT note, state FROM alerts WHERE id=?", (keep,)).fetchone()
    check("a note alone is not: it is kept, and nothing is told again",
          row["note"] == "a note alone" and row["state"] == "met"
          and fired_ids(alerts.check(conn, user=carol.id)) == [], tuple(row))
    sign_in(carol)
    for typed, shown in (("7", "7"), ("29", "29"), ("2.5", "2.5")):
        c.post("/monitor/alert/add", data={"symbol": "EEE", "kind": "near_52w_high",
                                           "threshold": typed, "repeat": "1"})
        pid = conn.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
        page = c.get("/monitor").data.decode("utf-8", "replace")
        box = re.search(r'id="e-th-%d"[^>]*?value="([^"]*)"' % pid, page, re.S).group(1)
        conn.execute("UPDATE alerts SET state='met' WHERE id=?", (pid,))
        conn.commit()
        c.post("/monitor/alert/edit", data={"alert_id": pid, "threshold": box, "note": "n"})
        row = conn.execute("SELECT threshold, state FROM alerts WHERE id=?", (pid,)).fetchone()
        check("%s%% is edited as %s, and saving its note alone keeps what it has told"
              % (typed, shown), box == shown and row["state"] == "met"
              and abs(row["threshold"] - float(typed) / 100) < 1e-12, (box, tuple(row)))
        db.alert_delete(conn, pid, user=carol.id)
    conn.close()
    sign_in(carol)
    page = c.get("/monitor").data.decode("utf-8", "replace")
    check("the page shows a repeating alert as watching, with no Re-arm",
          "tells you every time it happens" in page and page.count(">Re-arm<") == 1,
          page.count(">Re-arm<"))

    print()
    print("=" * 72)
    print("WHAT HAPPENED BETWEEN TWO CHECKS")
    print("=" * 72)
    # Five-minute bars ending on T0, 14:30 UTC. The check's own clock is given,
    # so bars are before or after it as they would be live.
    erin = person("erin@example.com")
    conn = db.connect(DB)
    T0 = LAST

    def five(closes, end=T0, live=None, highs=None, lows=None, volumes=None):
        b = daily(closes, highs, lows, volumes, live=live, interval="5m")
        n = len(closes)
        b.timestamp = np.array([end - (n - 1 - i) * 300 for i in range(n)], dtype=np.int64)
        b.meta = {"regularMarketTime": end + 300}
        return b

    def feed(symbol, bars, now):
        SERIES[symbol] = lambda interval="5m": bars
        return alerts.check(conn, user=erin.id, now=now)

    def said(out, aid):
        return next((f.message for f in out["fired"] if f.alert_id == aid), None)

    def state(aid):
        return conn.execute("SELECT state FROM alerts WHERE id=?", (aid,)).fetchone()[0]

    up = db.alert_add(conn, "SPK", "price_above", threshold=200.0, interval="5m", repeat=True,
                      user=erin.id)
    flat = [199.0] * 60
    # The last bar before the first look traded at 202, before anyone was watching.
    out = feed("SPK", five(flat, highs=[199.0] * 59 + [202.0]), T0 + 400)
    check("a bar from before the alert was watched is not news", said(out, up) is None
          and state(up) == "clear:%d" % (T0 + 400), (said(out, up), state(up)))
    ran = five(flat + [199.2, 199.4, 199.4], end=T0 + 900, live=199.5,
               highs=[199.0] * 60 + [199.2, 201.0, 199.4])
    out = feed("SPK", ran, T0 + 1300)
    check("a level traded and left between two checks is told, with the price and the bar",
          said(out, up) == "SPK traded at 201 in the 14:40 UTC bar on 11 Nov, above the 200 you "
                           "set, and was last at 199.5.", said(out, up))
    out = feed("SPK", ran, T0 + 1310)
    check("once", said(out, up) is None)
    out = feed("SPK", five(flat + [201.0], end=T0 + 1500, live=201.5,
                           highs=[199.0] * 60 + [201.5]), T0 + 1900)
    check("the price itself past the level is told as it always was",
          said(out, up) == "SPK is 201.5, above the 200 you set.", said(out, up))
    out = feed("SPK", five(flat + [201.0, 199.5], end=T0 + 1800, live=199.5,
                           highs=[199.0] * 60 + [201.5, 201.2]), T0 + 2200)
    check("and the bars it was told in are not told again when it comes back",
          said(out, up) is None and state(up) == "clear:%d" % (T0 + 2200), (said(out, up), state(up)))
    db.alert_set_active(conn, up, False, user=erin.id)
    db.alert_set_active(conn, up, True, user=erin.id)
    out = feed("SPK", five(flat + [199.0], end=T0 + 2700, live=199.0,
                           highs=[199.0] * 60 + [203.0]), T0 + 3100)
    check("nor what it did while the alert was paused", said(out, up) is None, said(out, up))
    conn.execute("UPDATE alerts SET state='clear' WHERE id=?", (up,))
    conn.commit()
    out = feed("SPK", ran, T0 + 3200)
    check("an alert from before this release tells nothing at its first check after it",
          said(out, up) is None and state(up) == "clear:%d" % (T0 + 3200), (said(out, up), state(up)))
    conn.execute("UPDATE alerts SET state=? WHERE id=?", ("clear:%d" % (T0 - 60 * DAY), up))
    conn.commit()
    out = feed("SPK", ran, T0 + 20 * DAY)
    check("one left unchecked for weeks does not tell what it missed long ago",
          said(out, up) is None, said(out, up))
    down = db.alert_add(conn, "SPD", "price_below", threshold=90.0, user=erin.id)
    feed("SPD", daily([100.0] * 40), LAST - DAY + 7 * 3600)
    out = feed("SPD", daily([100.0] * 40, lows=[100.0] * 39 + [88.0]), LAST + 7 * 3600)
    check("on daily bars it is told once the day is over, a one-off switching off",
          said(out, down) == "SPD traded at 88 on 11 Nov, below the 90 you set, and was last at 100."
          and conn.execute("SELECT active FROM alerts WHERE id=?", (down,)).fetchone()[0] == 0,
          said(out, down))

    cross = db.alert_add(conn, "MAV", "crosses_ma", interval="5m", repeat=True, user=erin.id)
    under = [100.0] * 70 + [99.0, 99.0, 99.0]
    out = feed("MAV", five(under, end=T0), T0 + 400)
    check("below its average, quiet", said(out, cross) is None and state(cross) == "bar:%d" % T0,
          state(cross))
    over = five(under + [101.0, 101.2], end=T0 + 600)
    out = feed("MAV", over, T0 + 1000)
    check("a cross on a bar that finished between two checks is told",
          (said(out, cross) or "").startswith("MAV closed at 101 in the 14:35 UTC bar on 11 Nov, "
                                              "back above its 50-bar average"), said(out, cross))
    out = feed("MAV", over, T0 + 1010)
    check("once", said(out, cross) is None)
    db.alert_reset(conn, cross, user=erin.id)
    out = feed("MAV", over, T0 + 1020)
    check("and re-arming does not tell it again", said(out, cross) is None, said(out, cross))
    for kept, want in (("met", False), ("clear", True)):
        newest = five(under + [101.0], end=T0 + 300)
        conn.execute("UPDATE alerts SET state=? WHERE id=?", (kept, cross))
        conn.commit()
        out = feed("MAV", newest, T0 + 700)
        check("kept as %r by the release before, the newest bar's cross is %s"
              % (kept, "told" if want else "not told again"),
              bool(said(out, cross)) == want and state(cross) == "bar:%d" % (T0 + 300),
              (said(out, cross), state(cross)))
    vol = db.alert_add(conn, "VOL", "volume_spike", threshold=3.0, interval="5m", repeat=True,
                       user=erin.id)
    out = feed("VOL", five([10.0] * 40, end=T0), T0 + 400)
    busy = five([10.0] * 43, end=T0 + 900, volumes=[100.0] * 40 + [500.0, 600.0, 100.0])
    out = feed("VOL", busy, T0 + 1300)
    check("volume spikes between two checks too, the latest of them",
          (said(out, vol) or "").startswith("VOL traded 5.0 times its normal volume in the 14:40 UTC")
          and said(out, vol).endswith("It was the latest of 2 since the last look."), said(out, vol))

    # A level that moves every session: Monday's move says nothing of Tuesday's.
    def day_of(base, live, day):
        b = daily([100.0] * 58 + [base], live=live)
        b.prev_close = base
        b.meta = {"regularMarketTime": LAST + day * DAY + 3600}
        return b

    rise = db.alert_add(conn, "GAP", "pct_up", threshold=0.05, repeat=True, user=erin.id)
    check("up 6% is told", said(feed("GAP", day_of(100.0, 106.0, 0), T0), rise) is not None)
    check("and not again that day, its base corrected or not",
          said(feed("GAP", day_of(100.0, 106.5, 0), T0), rise) is None
          and said(feed("GAP", day_of(99.9, 106.5, 0), T0), rise) is None)
    check("nor on another date measured from the same close, as a Sunday's futures are",
          said(feed("GAP", day_of(100.0, 106.5, 1), T0), rise) is None)
    tuesday = said(feed("GAP", day_of(106.0, 112.5, 1), T0), rise)
    check("up 6% again the next session, from the new close, is told again",
          tuesday is not None and "6.13%" in tuesday, tuesday)
    check("once", said(feed("GAP", day_of(106.0, 113.0, 1), T0), rise) is None)
    check("and old data from the day before tells nothing",
          said(feed("GAP", day_of(100.0, 106.0, 0), T0), rise) is None)
    conn.execute("UPDATE alerts SET state='met' WHERE id=?", (rise,))
    conn.commit()
    check("one already met before this release is not told at its first check after it",
          said(feed("GAP", day_of(106.0, 113.0, 1), T0), rise) is None
          and state(rise).startswith("met:"), state(rise))
    check("but is the next session",
          said(feed("GAP", day_of(113.0, 120.0, 2), T0), rise) is not None)

    # A check that has read an alert must not undo what happened to it since.
    db.alert_checked(conn, rise, state="clear", was="met:1:1.0")
    check("a check keeps its state only over the one it read",
          state(rise).startswith("met:"), state(rise))
    conn.execute("DELETE FROM alerts WHERE user_id=?", (erin.id,))
    conn.commit()
    conn.close()

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
    alerts.fetch_bars = lambda s, i="1d", lookback=None: (time.sleep(0.4),
                                                          bars_for(s, i, lookback))[1]
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
    plan = SimpleNamespace(action="WAIT", direction=1, entry=89.7, stop=85.0)
    check("a wait suggests an alert at its entry, and at its stop",
          [(s["kind"], s["threshold"]) for s in alerts.suggest_for("EEE", plan, None)]
          == [("price_below", 89.7), ("price_below", 85.0)])
    for action in ("BUY", "SHORT", "AVOID"):
        plan = SimpleNamespace(action=action, direction=1, entry=89.7, stop=85.0)
        check("a %s waits for no entry, so only its stop is suggested" % action,
              [s["threshold"] for s in alerts.suggest_for("EEE", plan, None)] == [85.0])
    check("the call words keep their capitals",
          alerts.describe("call_buy", None, "NVDA") == "NVDA call turns BUY"
          and "WAIT to BUY" in alerts.describe("call_changes", None, "NVDA"))
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
    # The ceiling is for accounts not on Pro: the pricing page promises Pro
    # as many alerts as it likes. While billing is off everyone has Pro.
    ceiling, db.MAX_ALERTS = db.MAX_ALERTS, held + 1
    conn = db.connect(DB)
    try:
        db.alert_add(conn, "AAPL", "price_above", threshold=1.0, user=carol.id)
        try:
            db.alert_add(conn, "AAPL", "price_above", threshold=2.0, user=carol.id)
            stopped = False
        except ValueError as exc:
            stopped = "An account not on Pro can keep" in str(exc)
        check("an account not on Pro keeps alerts up to the ceiling, and not past it",
              stopped)
        check("Pro, as everyone is while billing is off, has no ceiling",
              add().status_code == 302)
    finally:
        db.MAX_ALERTS = ceiling
        conn.close()

    before = newest(carol)["id"]
    r = add(interval="banana")
    check("a bar size that is not one is refused, in words",
          r.status_code == 400 and "bar sizes" in r.data.decode("utf-8", "replace")
          and newest(carol)["id"] == before, r.status_code)
    r = c.post("/watch/add", data={"symbol": "AMD", "interval": "x" * 19000})
    conn = db.connect(DB)
    held = [w["symbol"] for w in db.watchlist(conn, user=carol.id)]
    check("for the watchlist too", r.status_code == 400 and "AMD" not in held, r.status_code)
    ceiling, db.MAX_WATCHED = db.MAX_WATCHED, len(held)
    try:
        r = c.post("/watch/add", data={"symbol": "AMD", "interval": "1d"})
        check("a full watchlist takes no more, and says so", r.status_code == 400
              and "Remove one first" in r.data.decode("utf-8", "replace"), r.status_code)
        check("but what is on it can still be changed",
              c.post("/watch/add", data={"symbol": "nvda", "interval": "1h"}).status_code == 302)
    finally:
        db.MAX_WATCHED = ceiling
    conn.close()

    print()
    print("=" * 72)
    print("SAVED THESES: JUDGED AT THE CLOSE, AND ALL OF THEM COUNTED")
    print("=" * 72)
    frank = person("frank@example.com")
    conn = db.connect(DB)
    PRICE["THE"] = 90.0
    for k in range(25):
        tid = db.thesis_save(conn, "THE", 100.0, 0.6, "x", [], [], [], [], user=frank.id)
        conn.execute("UPDATE theses SET created_ts=? WHERE id=?",
                     (int(time.time()) - (40 - k) * DAY, tid))
        if k < 6:
            db.thesis_close(conn, tid, 120.0, "right", user=frank.id)
        if k == 24:
            db.thesis_close(conn, tid, 120.0, "right", user=frank.id)
    conn.commit()
    conn.close()
    sign_in(frank)
    real_fetch = web.market_mod.fetch_bars
    web.market_mod.fetch_bars = bars_for
    try:
        page = re.sub(r"\s+", " ", c.get("/monitor").data.decode("utf-8", "replace"))
        older = re.sub(r"\s+", " ", c.get("/monitor?theses=20").data.decode("utf-8", "replace"))
        odd = [c.get("/monitor?theses=%s" % v).status_code for v in ("-3", "x", "99999")]
    finally:
        web.market_mod.fetch_bars = real_fetch
    figures = re.findall(r'<div class="k">([^<]+)</div> <div class="v fig[^"]*">([^<]+)</div>', page)
    check("the record counts every thesis, not the newest 20",
          dict(figures).get("Closed out") == "7" and dict(figures).get("Still open") == "18", figures)
    check("and so does the heading", "1 to 20 of 25, newest first" in page
          and 'href="/monitor?theses=20#theses"' in page)
    check("the older ones are a page away, where they can be closed",
          "21 to 25 of 25" in older and "Newer theses" in older and "Reopen" in older)
    check("an odd page number is a page, not an error", odd == [200, 200, 200], odd)
    closed = page[page.index("<b>THE</b>"):]
    closed = closed[:closed.index("Reopen")]
    check("a closed thesis moves from the price then to the price it was closed at",
          ">120" in closed.replace("> 120", ">120") and "20.0%" in closed and "-10.0%" not in closed,
          re.findall(r"-?[0-9.]+%", closed))
    gone = person("gone@example.com")
    conn = db.connect(DB)
    for sym in ("ZZG1", "ZZG2", "ZZG3"):
        db.thesis_save(conn, sym, 100.0, -0.6, "x", [], [], [], [], user=gone.id)
    conn.close()
    sign_in(gone)
    at_once, most = [0], [0]
    gate = threading.Lock()

    def slow_gone(symbol, interval="1d", lookback=None):
        with gate:
            at_once[0] += 1
            most[0] = max(most[0], at_once[0])
        time.sleep(0.2)
        with gate:
            at_once[0] -= 1
        raise alerts.DataError("no such symbol")

    web.market_mod.fetch_bars = slow_gone
    try:
        page = c.get("/monitor").data.decode("utf-8", "replace")
    finally:
        web.market_mod.fetch_bars = real_fetch
    check("several instruments are priced at once", most[0] == 3, most[0])
    picked = re.findall(r'<option value="(\w+)"\s+selected>', page[page.index('name="outcome"'):])
    check("with no price, the outcome offered is undecided", "Could not price ZZG1" in page
          and picked == ["undecided"] * 3, picked)
    sign_in(carol)

    print()
    print("=" * 72)
    print("THE COUNTS THE PAGE GIVES")
    print("=" * 72)
    gail = person("gail@example.com")
    sign_in(gail)
    conn = db.connect(DB)
    for k in range(17):
        db.event_add(conn, gail.id, "price_above", "AAA", "Told %d." % k, 100.0, "")
    for k in range(2):
        db.open_trade(conn, "AAA", "1d", 1, entry=100.0, stop=90.0, target1=120.0,
                      quantity=1, user=gail.id)
    conn.close()
    r = c.post("/monitor/alerts/check", follow_redirects=True)
    page = re.sub(r"\s+", " ", r.data.decode("utf-8", "replace"))
    check("open positions are not counted as alerts",
          "Checked 0 alerts and the stops of 2 open positions. 0 triggered." in page,
          re.findall(r"Checked [^<]*", page))
    check("the history's count is every time it told, not the rows shown",
          re.search(r'What it has told you</h3> <span class="count">17</span> '
                    r'<span class="tail">the latest 15 are shown', page) is not None)
    one = re.sub(r"\s+", " ", c.get("/monitor?checked=1&stops=1").data.decode("utf-8", "replace"))
    check("and one of each is one",
          "Checked 1 alert and the stop of 1 open position. 0 triggered." in one)
    real_loop, web.ALERTS = web.ALERTS, scheduler.Scheduler(lambda: {}, interval_seconds=60,
                                                            name="test-minute")
    web.ALERTS.start()
    try:
        page = re.sub(r"\s+", " ", c.get("/monitor").data.decode("utf-8", "replace"))
    finally:
        web.ALERTS.stop()
        web.ALERTS = real_loop
    check("checks a minute apart are every minute", "checks your alerts every minute, and" in page)
    check("a call alert says whose settings it is worked out with",
          "Orenth's standard settings" in page.replace("&#39;", "'"))

    print()
    print("=" * 72)
    print("A NOTE ON WHAT IS WATCHED")
    print("=" * 72)
    check("the watch form takes one", re.search(r'id="w-note" name="note"', page) is not None)
    c.post("/watch/add", data={"symbol": "NVDA", "interval": "1d", "note": "after earnings"})
    c.post("/watch/add", data={"symbol": "NVDA", "interval": "1h", "note": ""})
    conn = db.connect(DB)
    row = conn.execute("SELECT interval, note FROM watchlist WHERE user_id=?", (gail.id,)).fetchone()
    check("added again without one, it keeps the note it has",
          tuple(row) == ("1h", "after earnings"), tuple(row))
    c.post("/watch/add", data={"symbol": "NVDA", "interval": "1h", "note": "new note"})
    c.post("/watch/add", data={"symbol": "AMD", "interval": "1d"})
    held = {w["symbol"]: w["note"] for w in db.watchlist(conn, user=gail.id)}
    check("a new note replaces it, and one never given is empty",
          held == {"NVDA": "new note", "AMD": ""}, held)
    conn.close()
    sign_in(carol)

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

    # What a "1y" download holds: trading days from a year ago to yesterday.
    weekdays = [d for d in range(LAST - 364 * DAY, LAST + 1, DAY)
                if time.gmtime(d).tm_wday < 5]
    year = daily([50.0] * len(weekdays), [51.0] * len(weekdays))
    year.timestamp = np.array(weekdays, dtype=np.int64)
    year_alert = db.alert_add(conn, "YYY", "near_52w_high", threshold=0.05, user=carol.id)
    SERIES["YYY"] = lambda interval: year
    msg = {f.alert_id: f.message for f in alerts.check(conn, user=carol.id)["fired"]}.get(
        year_alert, "")
    check("a year of daily bars, all the check now downloads, is read as a whole year",
          "its 52-week high of 51" in msg, msg)
    conn.close()

    print()
    print("=" * 72)
    print("EACH CHECK DOWNLOADS ONLY THE HISTORY IT READS")
    print("=" * 72)
    # Five years of daily bars, or sixty days of five-minute ones, every five
    # minutes, used to be fetched for readings that never look past a year.
    from bot import market
    conn = db.connect(DB)
    short = db.alert_add(conn, "EEE", "rsi_above", threshold=99, interval="5m", user=carol.id)
    del LOOKED[:]
    alerts.check(conn, user=carol.id)
    asked = {(s, i): lb for s, i, lb in LOOKED}
    check("a daily alert asks for a year of bars", asked.get(("EEE", "1d")) == "1y", asked)
    check("a five-minute one for a few days of them", asked.get(("EEE", "5m")) == "5d", asked)
    del LOOKED[:]
    PRICE["SSS"] = 10.0
    db.open_trade(conn, "SSS", "1d", 1, entry=12.0, stop=11.0, target1=15.0,
                  quantity=1, user=carol.id)
    alerts.check_stops(conn, user=carol.id)
    check("and a crossed-stop check the same year a daily alert asks for, so the two share "
          "one download", ("SSS", "1d", "1y") in LOOKED, LOOKED)
    check("every bar size has its lookback, within what the source serves for it",
          set(alerts.LOOKBACK) == set(market._MAX_RANGE)
          and all(alerts.LOOKBACK[i] in ("5d", "1mo", "1y") for i in alerts.LOOKBACK)
          and alerts.LOOKBACK["1m"] == "5d", alerts.LOOKBACK)
    db.alert_delete(conn, short, user=carol.id)
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
    going, rounds = threading.Event(), []

    def long_round():
        rounds.append(1)
        going.wait(5)
        return {}

    mid = scheduler.Scheduler(long_round, interval_seconds=3600, name="test-mid")
    mid.start()
    mid.check_now()
    while not rounds:
        time.sleep(0.01)
    mid.stop(timeout=0.1)
    check("started again while still finishing a round, it carries on", mid.start() is True)
    going.set()
    time.sleep(0.3)
    check("and is still running once the round is over, waiting as before",
          mid.running and len(rounds) == 1, (mid.running, rounds))
    check("where starting again is refused", mid.start() is False)
    mid.check_now()
    time.sleep(0.3)
    check("and it checks", len(rounds) == 2, rounds)
    mid.stop()
    check("and stops", not mid.running)
    began = []
    steady = scheduler.Scheduler(lambda: (began.append(time.monotonic()), time.sleep(0.3), {})[2],
                                 interval_seconds=30, name="test-steady")
    steady._interval = 0.6
    steady.start()
    time.sleep(3.4)
    steady.stop()
    gaps = [b - a for a, b in zip(began, began[1:])]
    check("rounds start an interval apart, whatever a round takes",
          len(gaps) >= 3 and sum(gaps) / len(gaps) < 0.75, gaps)
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
    check("notifications on this device can be switched on",
          'data-notify-act="on"' in page)
finally:
    web.market_mod.split_history = REAL_HISTORY
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("ALERT LOOP OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
