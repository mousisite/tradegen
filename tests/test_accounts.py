"""Accounts, sessions, and the separation between one person's data and another's.

The important test in this file is not that signing in works. It is that two
people signed into the same installation cannot see, change or delete each
other's trades. Adding a login without that would be worse than having no login
at all, because it looks like a boundary while not being one.
"""
import os as _os
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import accounts as A
from bot import auth as auth_mod
from bot import database as db

c, DB = harness.isolated_web("accounts")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def sign_in_as(client, user):
    """Put a real session cookie in the client, as the callback would."""
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token
    return token



def visible(client, path):
    """A page's content with its form hints removed.

    Every symbol input carries placeholder="AAPL" as a hint. Searching the raw
    HTML for a ticker finds that and reports a leak that is not there, so the
    placeholders come out before the page is examined.
    """
    import re as _re
    body = client.get(path).data.decode("utf-8", "replace")
    body = _re.sub(r'placeholder="[^"]*"', "", body)
    body = _re.sub(r'<!--.*?-->', "", body, flags=_re.S)
    return body


print("=" * 72)
print("THE LOCAL ACCOUNT")
print("=" * 72)

conn = db.connect(DB)
local = A.local_user(conn)
again = A.local_user(conn)
check("a local account exists", local.id > 0)
check("asking twice returns the same one", local.id == again.id)
check("it is marked as local", local.is_local)
check("only one local account is ever made",
      len([u for u in A.list_users(conn) if u.is_local]) == 1)
conn.close()

check("no credentials means no sign-in required", web.auth_required() is False)
for path in ("/", "/trades", "/monitor", "/settings", "/account"):
    check("GET %s works with no login" % path, c.get(path).status_code == 200)

print()
print("=" * 72)
print("TWO PEOPLE, ONE INSTALLATION")
print("=" * 72)

conn = db.connect(DB)
alice = A.upsert_google_user(conn, {"email": "alice@example.com", "sub": "g-alice",
                                    "name": "Alice", "email_verified": True})
bob = A.upsert_google_user(conn, {"email": "bob@example.com", "sub": "g-bob",
                                  "name": "Bob", "email_verified": True})
check("two distinct accounts", alice.id != bob.id)
check("signing in again does not duplicate",
      A.upsert_google_user(conn, {"email": "alice@example.com", "sub": "g-alice",
                                  "name": "Alice R", "email_verified": True}).id
      == alice.id)
check("a changed name is taken", A.get_user(conn, alice.id).name == "Alice R")

# The subject id, not the email, is the identity: an address can be reassigned.
moved = A.upsert_google_user(conn, {"email": "alice.r@example.com", "sub": "g-alice",
                                    "name": "Alice R", "email_verified": True})
check("a changed address stays the same account", moved.id == alice.id)
check("and the new address is stored",
      A.get_user(conn, alice.id).email == "alice.r@example.com")

# The other half of that: once the address is given to someone new, a
# different Google identity holding it is not the account's owner.
try:
    A.upsert_google_user(conn, {"email": "alice.r@example.com", "sub": "g-newcomer",
                                "name": "Newcomer", "email_verified": True})
    check("a different Google account with the same address is refused", False,
          "it was handed Alice's account")
except ValueError:
    check("a different Google account with the same address is refused", True)
check("and the account stays tied to Alice's own Google identity",
      conn.execute("SELECT provider_id FROM users WHERE id=?",
                   (alice.id,)).fetchone()[0] == "g-alice")
by_code = A.upsert_email_user(conn, "carol@example.com")
check("while an account made with an emailed code still joins Google",
      A.upsert_google_user(conn, {"email": "carol@example.com", "sub": "g-carol",
                                  "name": "Carol", "email_verified": True}).id
      == by_code.id)

try:
    A.upsert_google_user(conn, {"email": "x@example.com", "sub": "z",
                                "email_verified": False})
    check("an unverified email is refused", False, "it was accepted")
except ValueError:
    check("an unverified email is refused", True)
conn.close()

# Give each of them data, through the web layer, as themselves.
sign_in_as(c, alice)
c.post("/trades/open", data={"symbol": "AAPL", "direction": "long",
                             "entry": "100", "stop": "95", "target": "120",
                             "quantity": "10"})
c.post("/watch/add", data={"symbol": "NVDA", "interval": "1d"})
c.post("/monitor/alert/add", data={"symbol": "AAPL", "kind": "price_above",
                                   "threshold": "987654", "interval": "1d"})

sign_in_as(c, bob)
c.post("/trades/open", data={"symbol": "TSLA", "direction": "long",
                             "entry": "200", "stop": "190", "target": "230",
                             "quantity": "5"})
c.post("/watch/add", data={"symbol": "NVDA", "interval": "1d"})

# A thesis is the most personal thing in here: it is somebody's own reasoning
# about a position, not just a row of numbers. Written directly because saving
# one through the web layer needs a live analysis behind it.
conn = db.connect(DB)
for owner, symbol, note in ((alice, "AAPL", "ALICE PRIVATE REASONING"),
                            (bob, "TSLA", "bob reasoning")):
    db.thesis_save(conn, symbol=symbol, price=100.0, balance=0.5,
                   verdict="buy", bull=[], bear=[], falsifiers=["x"],
                   sources=[], note=note, user=owner.id)
alice_thesis = db.thesis_list(conn, user=alice.id)[0]["id"]
conn.close()

conn = db.connect(DB)
a_trades = [r["symbol"] for r in db.list_trades(conn, user=alice.id)]
b_trades = [r["symbol"] for r in db.list_trades(conn, user=bob.id)]
print("   Alice holds %s, Bob holds %s" % (a_trades, b_trades))
check("each trade went to the right person",
      a_trades == ["AAPL"] and b_trades == ["TSLA"])
check("both can watch the same instrument",
      len(db.watchlist(conn, user=alice.id)) == 1
      and len(db.watchlist(conn, user=bob.id)) == 1)
alice_trade = db.list_trades(conn, user=alice.id)[0]["id"]
alice_alert = db.alert_list(conn, user=alice.id)[0]["id"]
conn.close()

print()
print("   Bob is signed in. Nothing of Alice's may reach him.")
page = visible(c, "/trades")
check("Bob's journal shows his own trade", "TSLA" in page)
check("Bob's journal does not show Alice's", "AAPL" not in page, "AAPL leaked")

page = visible(c, "/monitor")
check("Bob sees no alert of Alice's", "AAPL" not in page, "AAPL leaked")
check("nor its threshold", "987654" not in page, "threshold leaked")

page = visible(c, "/portfolio")
check("Bob's portfolio excludes Alice's position", "AAPL" not in page)

export = c.get("/export/trades.csv").data.decode("utf-8", "replace")
check("Bob's export contains his trade", "TSLA" in export)
check("Bob's export excludes Alice's", "AAPL" not in export, "AAPL leaked in export")

r = c.post("/trades/close", data={"trade_id": alice_trade, "price": "120"})
check("Bob cannot close Alice's trade", r.status_code == 400, r.status_code)
r = c.post("/trades/delete", data={"trade_id": alice_trade})
check("Bob cannot delete Alice's trade", r.status_code == 400, r.status_code)

# An analysis run is Alice's too: its symbol and levels are her plan.
_conn = db.connect(DB)
_run = _conn.execute(
    "INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, user_id) "
    "VALUES (?, 'AAPL', '1d', 'BUY', 100.0, 95.0, 110.0, ?)", (int(time.time()), alice.id)).lastrowid
_avoid = _conn.execute(
    "INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, user_id) "
    "VALUES (?, 'TSLA', '1d', 'AVOID', 100.0, 95.0, 110.0, ?)", (int(time.time()), bob.id)).lastrowid
_wait_short = _conn.execute(
    "INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, user_id) "
    "VALUES (?, 'TSLA', '1d', 'WAIT', 100.0, 105.0, 90.0, ?)", (int(time.time()), bob.id)).lastrowid
_wait_long = _conn.execute(
    "INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, user_id) "
    "VALUES (?, 'TSLA', '1d', 'WAIT', 95.0, 90.0, 105.0, ?)", (int(time.time()), bob.id)).lastrowid
_short = _conn.execute(
    "INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, user_id) "
    "VALUES (?, 'TSLA', '1d', 'SHORT', 100.0, 105.0, 90.0, ?)", (int(time.time()), bob.id)).lastrowid
_conn.commit()
_conn.close()
r = c.post("/trades/take", data={"run_id": _run})
check("Bob cannot open a paper trade from Alice's analysis", r.status_code == 400, r.status_code)
r = c.post("/trades/take", data={"run_id": _avoid})
check("an AVOID has no trade in it to simulate", r.status_code == 400, r.status_code)
# A WAIT names a price the market has not reached. Logging it opened a trade
# at that price straight away: a fill that never happened, counted as won or
# lost, and for a short WAIT an error about where a long's stop goes.
for _wait in (_wait_long, _wait_short):
    r = c.post("/trades/take", data={"run_id": _wait})
    _said = r.data.decode("utf-8", "replace")
    check("a WAIT is not logged as a filled trade",
          r.status_code == 400 and "Nothing has filled yet" in _said, r.status_code)
    check("and is told why, not about stops or mistyped tickers",
          "the stop must sit" not in _said and "ticker does not exist" not in _said)
c.post("/trades/take", data={"run_id": _short})
_conn = db.connect(DB)
_side = _conn.execute("SELECT direction FROM trades WHERE run_id=?", (_short,)).fetchone()
_waits = _conn.execute("SELECT COUNT(*) FROM trades WHERE run_id IN (?, ?)",
                       (_wait_long, _wait_short)).fetchone()[0]
_conn.close()
check("no trade was written for either WAIT", _waits == 0, _waits)
check("a SHORT, whose stop sits above the entry, opens a short", _side and _side[0] == -1, _side)
r = c.post("/monitor/alert/delete", data={"alert_id": alice_alert})
check("Bob cannot delete Alice's alert", r.status_code == 400, r.status_code)

conn = db.connect(DB)
check("Bob's theses are only his own",
      [r["user_note"] for r in db.thesis_list(conn, user=bob.id)]
      == ["bob reasoning"])
check("Bob cannot read Alice's thesis by its id",
      db.thesis_get(conn, alice_thesis, user=bob.id) is None,
      "Alice's reasoning leaked")
conn.close()

r = c.post("/thesis/delete", data={"thesis_id": alice_thesis})
check("Bob cannot delete Alice's thesis", r.status_code == 400, r.status_code)
r = c.post("/thesis/close", data={"thesis_id": alice_thesis,
                                  "outcome": "right", "price": "150"})
check("Bob cannot close Alice's thesis", r.status_code == 400, r.status_code)

conn = db.connect(DB)
check("Alice's trade survived all of that",
      len(db.list_trades(conn, user=alice.id)) == 1)
check("Alice's alert survived too",
      len(db.alert_list(conn, user=alice.id)) == 1)
kept = db.thesis_get(conn, alice_thesis, user=alice.id)
check("Alice's thesis survived, and is still open",
      kept is not None and kept["outcome"] in (None, ""),
      dict(kept) if kept else "gone")
conn.close()

# And the same in reverse, so the test is not accidentally one-directional.
sign_in_as(c, alice)
page = visible(c, "/trades")
check("Alice sees her own trade", "AAPL" in page)
check("Alice does not see Bob's", "TSLA" not in page, "TSLA leaked")

print()
print("=" * 72)
print("ROUTINES ACT FOR WHOEVER RUNS THEM")
print("=" * 72)

# No network: prices and the analysis are stand-ins. Carol and Dave hold no
# trades, so the position check has nothing to price.
from types import SimpleNamespace as _NS
from bot import alerts as _alerts
from bot import workflows as _workflows


def _bars(symbol, interval="1d", lookback=None):
    return _NS(symbol=symbol, interval=interval, last_price=101.0, close=[101.0] * 60,
               volume=[1.0] * 60, prev_close=100.0, market_open=False,
               last_bar_time="", meta={})


_recorded_for = []


def _analyse(symbol, cfg, interval=None, with_news=True, with_learning=True,
             record=True, db_path=None, user=1, progress=None, source="engine"):
    run_id = None
    if record:
        _cn = db.connect(DB)
        try:
            run_id = _cn.execute(
                "INSERT INTO runs (ts, symbol, interval, action, user_id, source) "
                "VALUES (?, ?, ?, 'BUY', ?, ?)",
                (int(time.time()), symbol.upper(), interval, user, source)).lastrowid
            _cn.commit()
        finally:
            _cn.close()
    _recorded_for.append(user)
    return _NS(bars=_NS(symbol=symbol.upper(), interval=interval), run_id=run_id,
               plan=_NS(action="BUY", headline="A stand-in call."))


_real = (_alerts.fetch_bars, _workflows.fetch_bars, _workflows.engine.analyse)
_alerts.fetch_bars = _workflows.fetch_bars = _bars
_workflows.engine.analyse = _analyse
try:
    conn = db.connect(DB)
    carol = A.upsert_google_user(conn, {"email": "carol@example.com", "sub": "g-carol",
                                        "name": "Carol", "email_verified": True})
    dave = A.upsert_google_user(conn, {"email": "dave@example.com", "sub": "g-dave",
                                       "name": "Dave", "email_verified": True})
    dave_alert = db.alert_add(conn, "ZZDAVE", "price_above", 1.0, user=dave.id)
    carol_alert = db.alert_add(conn, "ZZCAROL", "price_above", 1.0, user=carol.id)
    db.watch_add(conn, "ZZCAROL", "1d", user=carol.id)
    conn.close()

    sign_in_as(c, carol)
    check("Carol's morning review runs", c.post("/workflow/morning").status_code == 302)
    conn = db.connect(DB)
    row = conn.execute("SELECT active, triggered_ts FROM alerts WHERE id=?",
                       (dave_alert,)).fetchone()
    check("it leaves Dave's alert alone, still on and never fired",
          row["active"] == 1 and row["triggered_ts"] is None, dict(row))
    check("and checks Carol's",
          conn.execute("SELECT triggered_ts FROM alerts WHERE id=?",
                       (carol_alert,)).fetchone()[0] is not None)
    check("nothing of it lands in Dave's history", not db.event_list(conn, dave.id))
    conn.close()
    page = visible(c, "/monitor")
    check("Carol sees her report", "ZZCAROL is 101" in page)
    fires = c.get("/api/alerts/status").get_json()["new_fires"]
    check("and the bar on her pages is told of the fire, like one from the loop",
          [f["symbol"] for f in fires] == ["ZZCAROL"], fires)

    sign_in_as(c, dave)
    page = visible(c, "/monitor")
    check("Dave does not see Carol's report", "ZZCAROL" not in page and "needs you" not in page)
    check("nor does her fire reach Dave's bar",
          "ZZCAROL" not in str(c.get("/api/alerts/status").get_json()))

    # The watchlist routine: Carol's last own look at ZZCAROL said WAIT.
    conn = db.connect(DB)
    conn.execute("INSERT INTO runs (ts, symbol, interval, action, user_id) "
                 "VALUES (?, 'ZZCAROL', '1d', 'WAIT', ?)", (int(time.time()) - 60, carol.id))
    conn.commit()
    conn.close()
    sign_in_as(c, carol)
    c.post("/workflow/watchlist")
    page = visible(c, "/monitor")
    check("the watchlist routine reports Carol's change of call",
          "went from WAIT to BUY" in page)
    check("and keeps the new analysis as Carol's",
          _recorded_for and set(_recorded_for) == {carol.id}, _recorded_for)
    c.post("/workflow/watchlist")
    page = visible(c, "/monitor")
    check("run again, the call is unchanged rather than the same change twice",
          "BUY, unchanged" in page and "went from WAIT to BUY" not in page)
finally:
    _alerts.fetch_bars, _workflows.fetch_bars, _workflows.engine.analyse = _real

print()
print("=" * 72)
print("SETTINGS ARE EACH PERSON'S OWN")
print("=" * 72)

# Settings used to be one shared file on a deployment, so whoever signed in
# changed everyone's account size, costs and thresholds.
from bot import config as _cfgmod
_shared_before = _cfgmod.load(web.app.config.get("CFG_PATH"))["account_size"]

sign_in_as(c, alice)
r = c.post("/settings", data={"account_size": "123456", "risk_per_trade_pct": "2"})
check("Alice can save her settings", r.status_code == 302, r.status_code)
check("and sees them", "123456" in visible(c, "/settings"))

sign_in_as(c, bob)
check("Bob does not get Alice's account size",
      "123456" not in visible(c, "/settings"))
check("and the shared file was not touched",
      _cfgmod.load(web.app.config.get("CFG_PATH"))["account_size"]
      == _shared_before)

page = visible(c, "/settings")
check("Bob is not offered the server-wide alert schedule",
      "Background checking" not in page)
check("and is told his settings are his own",
      "changes nothing for anyone else" in page)
# The page was written for the desktop install, where nothing leaves the
# machine. Online, settings live with the account on Orenth's server.
check("and where they are kept", "Saved to your account on Orenth" in page)
for desktop in ("never leaves this machine", "nothing phoned home", "config.json",
                "STOCKBOT_DB", "everything you have looked"):
    check("and is not told the desktop's '%s'" % desktop, desktop not in page)
check("Restoring defaults asks first",
      "return confirm(" in page[page.index('name="reset"') - 200:
                                page.index("Restore defaults")])

sign_in_as(c, alice)
c.get("/settings?reset=1")
check("opening the old reset address leaves Alice's settings alone",
      "123456" in visible(c, "/settings"))
c.post("/settings", data={"reset": "1"})
check("the reset button restores her defaults",
      "123456" not in visible(c, "/settings"))

# The analysis cache is shared between people, so it has to be keyed on the
# settings: a different account size is a different position size.
check("different settings give different analysis cache keys",
      web._settings_key({"account_size": 1000}) != web._settings_key({"account_size": 9000}))
check("the same settings give the same key",
      web._settings_key({"account_size": 1000}) == web._settings_key({"account_size": 1000}))

conn = db.connect(DB)
A.save_settings(conn, bob.id, {"account_size": 777, "not_a_setting": 1})
check("only personal settings are stored",
      A.settings_for(conn, bob.id) == {"account_size": 777})
conn.close()

print()
print("=" * 72)
print("ONE SHARED ANALYSIS, A RUN FOR EACH PERSON")
print("=" * 72)

# The analysis cache is shared, and the run recorded by whoever asked first
# used to be handed to everyone after them: missing from their history, and
# refused by "take this trade", which only opens your own runs.
from types import SimpleNamespace as _NS
from bot import engine as _engine


def _fake_analyse(symbol, cfg, interval=None, with_news=True, source="",
                  db_path=None, user=1, record=True, **_kw):
    bars = _NS(symbol="SHRD", name="Shared Co", asset_class="equity",
               interval="1d", last_price=100.0, market_open=False)
    plan = _NS(conviction=0.5, action="BUY", headline="Buy", entry=100.0,
               stop=95.0, target1=110.0, target2=None, reward_risk=2.0,
               breakeven_rate=0.4, probability=0.5, prob_samples=60,
               expectancy_r=0.1)
    regime = _NS(trend="up", volatility="normal", direction=1, adx=20.0)
    calib, mood = _NS(cost_r=0.01), _NS(score=0.0, items=[])
    run_id = None
    if record:
        rconn = db.connect(DB)
        run_id = db.record_run(rconn, bars, plan, [], 0.3, regime, calib, mood,
                               source=source, user=user)
        rconn.close()
    return _engine.Analysis(bars=bars, context=None, signals=[], composite=0.3,
                            regime=regime, levels=[], calibration=calib,
                            sentiment=mood, plan=plan, run_id=run_id)


_real_analyse = web.engine.analyse
web.engine.analyse = _fake_analyse
_shared_cfg = _cfgmod.load(web.app.config.get("CFG_PATH"))
runs_of = {}
try:
    for person in (alice, bob, alice):
        conn = db.connect(DB)
        with web.app.test_request_context("/"):
            web.g.user, web.g.demo = A.get_user(conn, person.id), False
            got = web.cached_analysis("SHRD", "1d", _shared_cfg, False)
        conn.close()
        runs_of.setdefault(person.id, []).append(got.run_id)
    conn = db.connect(DB)
    owner = {r["id"]: r["user_id"] for r in conn.execute("SELECT id, user_id FROM runs")}
    conn.close()
    check("the second person gets a run of their own",
          runs_of[bob.id][0] and runs_of[bob.id][0] != runs_of[alice.id][0], runs_of)
    check("recorded against them", owner.get(runs_of[bob.id][0]) == bob.id)
    check("while the first keeps theirs, without a new one each refresh",
          runs_of[alice.id][0] == runs_of[alice.id][1]
          and owner.get(runs_of[alice.id][0]) == alice.id, runs_of)
    sign_in_as(c, bob)
    r = c.post("/trades/take", data={"run_id": runs_of[bob.id][0]})
    check("so 'take this trade' works for the second person too",
          r.status_code == 302, r.status_code)
finally:
    web.engine.analyse = _real_analyse
    web._CACHE.clear()

print()
print("=" * 72)
print("SESSIONS")
print("=" * 72)

conn = db.connect(DB)
token = A.start_session(conn, alice.id, "probe")
check("a session resolves to its user",
      A.user_for_session(conn, token).id == alice.id)
check("a made-up token resolves to nobody",
      A.user_for_session(conn, "not-a-real-token") is None)
check("an empty token resolves to nobody", A.user_for_session(conn, "") is None)

A.end_session(conn, token)
check("ending a session invalidates it", A.user_for_session(conn, token) is None)

expired = A.start_session(conn, alice.id, "old")
conn.execute("UPDATE sessions SET expires_ts=? WHERE token=?",
             (int(time.time()) - 10, expired))
conn.commit()
check("an expired session is refused", A.user_for_session(conn, expired) is None)
check("purging removes it", A.purge_expired(conn) >= 1)

A.end_all_sessions(conn, alice.id)          # start from a known state
t1 = A.start_session(conn, alice.id)
t2 = A.start_session(conn, alice.id)
check("signing out everywhere ends every one of them",
      A.end_all_sessions(conn, alice.id) == 2)
check("both are then dead",
      A.user_for_session(conn, t1) is None and A.user_for_session(conn, t2) is None)
conn.close()

sign_in_as(c, bob)
check("signed in, the journal loads", c.get("/trades").status_code == 200)
r = c.post("/signout")
check("signing out redirects", r.status_code == 302)
# Local mode is still on, so after signing out the local account takes over
# rather than the app becoming unusable.
check("the app still works after signing out", c.get("/trades").status_code == 200)

print()
print("=" * 72)
print("DELETING AN ACCOUNT")
print("=" * 72)

conn = db.connect(DB)
shared_before = conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
counts = A.owned_counts(conn, bob.id)
print("   Bob owns %s" % counts)
check("Bob owns something to delete", counts["trades"] >= 1)

removed = A.delete_user(conn, bob.id)
print("   removed %s" % removed)
check("his trades are gone", A.owned_counts(conn, bob.id)["trades"] == 0)
check("and so are his settings", A.settings_for(conn, bob.id) == {})
check("his account is gone", A.get_user(conn, bob.id) is None)
check("Alice is untouched", len(db.list_trades(conn, user=alice.id)) == 1)
check("the shared strategy record is untouched",
      conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
      == shared_before)

try:
    A.delete_user(conn, local.id)
    check("the local account cannot be deleted", False, "it was deleted")
except ValueError:
    check("the local account cannot be deleted", True)
try:
    A.delete_user(conn, 999999)
    check("deleting a missing account is refused", False)
except ValueError:
    check("deleting a missing account is refused", True)
conn.close()

print()
print("=" * 72)
print("THE GOOGLE FLOW")
print("=" * 72)

check("with no credentials it is not configured", not auth_mod.configured())
check("and it says which one is missing", "GOOGLE_CLIENT_ID" in auth_mod.why_not())

_os.environ["GOOGLE_CLIENT_ID"] = "test-id.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret-value"
web.app.config["AUTH_MODE"] = None
try:
    check("credentials switch it on", auth_mod.configured())
    check("and sign-in becomes required", web.auth_required() is True)

    verifier = auth_mod.new_verifier()
    state = auth_mod.new_state()
    check("a verifier is long enough to be a verifier", 43 <= len(verifier) <= 128)
    check("two verifiers differ", verifier != auth_mod.new_verifier())
    check("two states differ", state != auth_mod.new_state())

    url = auth_mod.authorization_url("http://localhost:8000/auth/google/callback",
                                     state, verifier)
    for piece in ("accounts.google.com", "code_challenge_method=S256",
                  "response_type=code", "scope=openid", "state=" + state):
        check("the authorize url has %s" % piece, piece in url)
    check("the secret is not in the url", "test-secret-value" not in url)
    check("the verifier is not in the url", verifier not in url)

    # A fresh client, so no session exists yet.
    anon = web.app.test_client()
    r = anon.get("/")
    check("an anonymous visitor gets the landing page, not a redirect",
          r.status_code == 200, r.status_code)
    check("and it carries no one's journal",
          "Open positions" not in r.data.decode("utf-8", "replace"))
    r = anon.get("/account")
    check("the account page still sends them to sign in",
          r.status_code == 302 and "/signin" in r.headers.get("Location", ""))
    r = anon.get("/trades")
    check("the journal shows them the example account instead",
          r.status_code == 200
          and "looking around an example account" in r.data.decode("utf-8", "replace"))
    r = anon.get("/api/alerts/status")
    check("the api answers 401 rather than redirecting", r.status_code == 401)
    check("the sign-in page is reachable", anon.get("/signin").status_code == 200)
    check("static files stay reachable",
          anon.get("/static/style.css").status_code == 200)

    r = anon.get("/auth/google/callback?code=abc&state=forged")
    check("a forged state is refused", r.status_code == 400)
    r = anon.get("/auth/google/callback?state=x")
    check("a callback with no code is refused", r.status_code == 400)

    _os.environ["GOOGLE_ALLOWED_DOMAINS"] = "example.com"
    check("a domain restriction is read", auth_mod.allowed_domains() == ("example.com",))
    auth_mod.check_domain("someone@example.com")
    check("an allowed domain passes", True)
    try:
        auth_mod.check_domain("someone@elsewhere.com")
        check("a disallowed domain is refused", False, "it was allowed")
    except auth_mod.AuthError:
        check("a disallowed domain is refused", True)
    del _os.environ["GOOGLE_ALLOWED_DOMAINS"]
    auth_mod.check_domain("anyone@anywhere.com")
    check("with no restriction anyone passes", True)
finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

check("removing the credentials restores local mode", web.auth_required() is False)

print()
print("   A public deployment whose sign-in keys went missing:")
# Every visitor used to be signed in as the local account, which is the
# operator and can see every account.
_os.environ["STOCKBOT_PUBLIC_URL"] = "https://orenth.app"
try:
    check("still asks people to sign in", web.auth_required() is True)
    nobody = web.app.test_client()
    r = nobody.get("/account")
    check("so a visitor is not handed the operator's account",
          r.status_code == 302 and "/signin" in r.headers.get("Location", ""),
          r.status_code)
    check("nor the page about everyone's use", nobody.get("/usage").status_code == 302)
    check("and the sign-in page says what is missing",
          "GOOGLE_CLIENT_ID" in nobody.get("/signin").data.decode("utf-8", "replace"))
    web.app.config["AUTH_MODE"] = "off"
    check("unless sign-in is switched off on purpose", web.auth_required() is False)
finally:
    web.app.config["AUTH_MODE"] = None
    del _os.environ["STOCKBOT_PUBLIC_URL"]
_os.environ["STOCKBOT_BEHIND_PROXY"] = "1"
try:
    check("behind the host's proxy it asks too", web.auth_required() is True)
finally:
    del _os.environ["STOCKBOT_BEHIND_PROXY"]
check("while on your own machine it still needs no sign-in",
      web.auth_required() is False and c.get("/trades").status_code == 200)

print()
print("=" * 72)
print("THE COOKIE KEY")
print("=" * 72)

import tempfile
key_path = _os.path.join(tempfile.mkdtemp(), "secret.key")
first = auth_mod.session_secret(key_path)
second = auth_mod.session_secret(key_path)
check("a key is generated", len(first) >= 32)
check("it is stable across calls", first == second)

# A key is random bytes, and roughly one in twenty begins or ends with a byte
# that happens to be whitespace. Reading one back must return exactly what was
# written, so this forces the cases that would otherwise show up as an
# occasional mysterious sign-out.
# 0x20 space, 0x09 tab, 0x0a newline, 0x0d return, 0x0b and 0x0c the two
# vertical forms. Built numerically so the bytes are unmistakable.
for edge in (bytes([n]) for n in (0x20, 0x09, 0x0A, 0x0D, 0x0B, 0x0C)):
    import secrets as _secrets
    path = _os.path.join(tempfile.mkdtemp(), "secret.key")
    written = edge + _secrets.token_bytes(46) + edge
    with open(path, "wb") as fh:
        fh.write(written)
    check("a key wrapped in %r survives being read back" % edge,
          auth_mod.session_secret(path) == written,
          "%d bytes written, %d read" % (len(written),
                                         len(auth_mod.session_secret(path))))
check("it is not a fixed constant",
      auth_mod.session_secret(_os.path.join(tempfile.mkdtemp(), "k")) != first)

_os.environ["STOCKBOT_SECRET_KEY"] = "a-key-from-the-environment-that-is-long"
check("the environment wins",
      auth_mod.session_secret(key_path).decode() ==
      "a-key-from-the-environment-that-is-long")
del _os.environ["STOCKBOT_SECRET_KEY"]


print()
print("=" * 72)
print("READY TO BE PUT ON THE INTERNET")
print("=" * 72)

# Google fetches the privacy policy itself, anonymously, before it will let an
# external OAuth client out of testing. If these need a login, registration
# cannot be completed.
_os.environ["GOOGLE_CLIENT_ID"] = "deploy-check.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "deploy-check-secret"
web.app.config["AUTH_MODE"] = None
try:
    stranger = web.app.test_client()
    # "/" is on this list deliberately: it is the address everyone shares, so it
    # has to answer a stranger rather than bouncing them to a login form.
    for path in ("/", "/privacy", "/terms", "/healthz", "/signin",
                 "/static/style.css"):
        check("%s is readable without signing in" % path,
              stranger.get(path).status_code == 200)
    # Pages a visitor may look at are served as the example account; these
    # hold somebody's own data or act on it, and still need signing in.
    for path in ("/account", "/export/trades.csv", "/usage"):
        r = stranger.get(path)
        check("%s still needs an account" % path,
              r.status_code == 302 and "/signin" in r.headers.get("Location", ""),
              r.status_code)

    print()
    print("   A stranger looking around")
    from bot import demo as _demo
    for path in ("/trades", "/portfolio", "/monitor", "/settings",
                 "/strategies", "/analyse?symbol=NVDA&interval=1d"):
        got = stranger.get(path)
        check("%s shows the example to a stranger" % path,
              got.status_code == 200
              and "looking around an example account"
              in got.data.decode("utf-8", "replace"), got.status_code)

    # Real people's data never appears in the example.
    seen = stranger.get("/trades").data.decode("utf-8", "replace")
    check("Alice's trade is not in the example journal", "987654" not in seen)
    seen = stranger.get("/monitor").data.decode("utf-8", "replace")
    check("nor anyone's routine report in the example's Monitor page",
          "ZZCAROL" not in seen and "needs you" not in seen)

    # The strategy record is shared, but which instruments it covers says what
    # people here have looked up. A visitor's list holds only popular names.
    conn = db.connect(DB)
    for _sym in ("ZZPRIVATE", "NVDA"):
        conn.execute("INSERT OR REPLACE INTO strategy_stats (symbol, interval, strategy, "
                     "family, regime, n, wins, sum_r, sum_r_gross, sum_bars, updated_ts) "
                     "VALUES (?, '1d', 'rsi_reversion', 'mean_reversion', 'all', 50, 25, "
                     "2.0, 3.0, 400, ?)", (_sym, int(time.time())))
    conn.commit()
    conn.close()
    seen = stranger.get("/strategies").data.decode("utf-8", "replace")
    check("a visitor's instrument list leaves out what people looked up",
          "ZZPRIVATE" not in seen and '<option value="NVDA"' in seen)
    seen = stranger.get("/strategies?symbol=ZZPRIVATE").data.decode("utf-8", "replace")
    check("and asking for one by name shows nothing about it", "ZZPRIVATE" not in seen)
    seen = stranger.get("/strategies").data.decode("utf-8", "replace")
    check("the strategies page says the record is everyone's",
          "anyone on Orenth has analysed" in seen
          and "you have run" not in seen and "you have analysed" not in seen)

    seen = stranger.get("/settings").data.decode("utf-8", "replace")
    check("the example's settings do not claim to be the visitor's own",
          "changes nothing for anyone else" not in seen
          and "never leaves this machine" not in seen and "config.json" not in seen)

    # Nothing a visitor posts is written: every form sends them to sign up,
    # and back to the page they were on rather than to a URL that only POSTs.
    for path, form in (("/trades/open", {"symbol": "GME", "direction": "long",
                                         "entry": "10", "stop": "9"}),
                       ("/settings", {"account_size": "1"}),
                       ("/watch/add", {"symbol": "GME"}),
                       ("/monitor/alert/add", {"symbol": "GME", "kind": "price_above",
                                               "threshold": "1"})):
        got = stranger.post(path, data=form,
                            headers={"Referer": "http://localhost/trades"})
        check("a visitor posting to %s is sent to sign up" % path,
              got.status_code == 302 and "/signin" in got.headers.get("Location", ""),
              got.status_code)
    check("and is brought back to where they were",
          "next=/trades" in stranger.post(
              "/trades/open", data={},
              headers={"Referer": "http://localhost/trades"}).headers.get("Location", ""))

    conn = db.connect(DB)
    demo_user = _demo.user(conn)
    check("the example account gained nothing from any of that",
          conn.execute("SELECT COUNT(*) FROM trades WHERE user_id=? AND symbol='GME'",
                       (demo_user.id,)).fetchone()[0] == 0
          and conn.execute("SELECT COUNT(*) FROM user_settings WHERE user_id=?",
                           (demo_user.id,)).fetchone()[0] == 0)
    check("and recorded no analyses for the example",
          conn.execute("SELECT COUNT(*) FROM runs WHERE user_id=?",
                       (demo_user.id,)).fetchone()[0] == 0)
    # An example book priced weeks ago drifts past its stops. It is refilled
    # at today's prices once a week, and only its own rows are touched.
    _others = conn.execute("SELECT COUNT(*) FROM trades WHERE user_id<>?",
                           (demo_user.id,)).fetchone()[0]
    conn.execute("UPDATE trades SET ts_opened=ts_opened-? WHERE user_id=?",
                 (_demo.RESEED_AFTER + 86400, demo_user.id))
    conn.commit()
    _demo.user(conn)
    _newest = conn.execute("SELECT MAX(ts_opened) FROM trades WHERE user_id=?",
                           (demo_user.id,)).fetchone()[0]
    check("a stale example account is refilled at today's prices",
          _newest is not None and time.time() - _newest < 600)
    check("and nobody else's trades are touched",
          conn.execute("SELECT COUNT(*) FROM trades WHERE user_id<>?",
                       (demo_user.id,)).fetchone()[0] == _others)
    conn.close()

    # Each of these would make the server analyse or scan something it has
    # not pre-computed, so a visitor is offered an account instead.
    for path, says in (("/analyse?symbol=GME&interval=1d", "look up GME"),
                       ("/analyse?symbol=NVDA&interval=1h", "change the timeframe"),
                       ("/research?symbol=GME", "look up GME"),
                       ("/screener?go=1&pe_max=20", "run your own screen"),
                       ("/ideas?market=stocks&horizon=short", "short holds")):
        got = stranger.get(path).data.decode("utf-8", "replace")
        check("%s offers an account instead" % path, says in got)

    body = stranger.get("/privacy").data.decode("utf-8", "replace")
    for claim in ("openid", "profile", "No tracking scripts",
                  "No selling, renting or sharing"):
        check("the privacy page states: %s" % claim[:34], claim in body)
    terms_body = stranger.get("/terms").data.decode("utf-8", "replace")
    check("the terms say it is not advice",
          "not financial advice" in terms_body)
    check("and that most day trading loses money",
          "loses money for the large majority" in terms_body)

    health = stranger.get("/healthz").get_json()
    check("the health check reports ok", health.get("ok") is True)
    check("and leaks nothing about anyone",
          set(health) <= {"ok", "service", "detail"}, health)
finally:
    del _os.environ["GOOGLE_CLIENT_ID"]
    del _os.environ["GOOGLE_CLIENT_SECRET"]
    web.app.config["AUTH_MODE"] = None

# The callback address has to be predictable, because it must be typed into
# Google's console exactly and a mismatch is the usual cause of failure.
_os.environ["STOCKBOT_PUBLIC_URL"] = "https://stockbot.example.com"
try:
    with web.app.test_request_context("/"):
        uri = web._redirect_uri()
    check("the callback is built from the public address",
          uri == "https://stockbot.example.com/auth/google/callback", uri)
    _os.environ["GOOGLE_REDIRECT_URI"] = "https://override.example.com/cb"
    with web.app.test_request_context("/"):
        check("an explicit override wins",
              web._redirect_uri() == "https://override.example.com/cb")
    del _os.environ["GOOGLE_REDIRECT_URI"]
finally:
    del _os.environ["STOCKBOT_PUBLIC_URL"]

# Every dependency the container installs must be one that is actually here,
# and every third-party import must be one the container installs. The second
# half is the one that bites: a missing pin lets the image build and start, and
# fails only when the code path that needs it first runs.
import ast as _ast
import importlib.metadata as _md

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
req_path = _os.path.join(ROOT, "requirements.txt")
pinned = [l.split("#")[0].strip() for l in open(req_path, encoding="utf-8")
          if l.strip() and not l.startswith("#")]
pinned = [l for l in pinned if l]
check("requirements.txt lists something", len(pinned) >= 5)

# A requirement may name extras, as in sentry-sdk[flask]. The distribution is
# still called sentry-sdk; the bracket selects optional dependencies.
def _distribution(requirement):
    return requirement.split("[")[0].strip()


req_names = set()
for line in pinned:
    name, _, want = line.partition("==")
    name = _distribution(name)
    req_names.add(name.lower().replace("-", "_"))
    check("%s is pinned to a version" % name, bool(want), line)
    try:
        have = _md.version(name)
        check("%s pin matches what was tested" % name, have == want,
              "pinned %s, have %s" % (want, have))
    except _md.PackageNotFoundError:
        check("%s is actually installed" % name, False)

_STDLIB = set(_sys.stdlib_module_names)
# Modules that live in this repository rather than being installed.
_LOCAL = {"bot", "web", "analyze", "paper", "menu", "serve", "harness", "setup",
          "backup", "preflight"}
_ALIAS = {"pil": "pillow", "dotenv": "python_dotenv"}

# Anything that is a .py file or a package in this repository is local by
# definition. A hand-kept list meant every new script read as a missing
# third-party package until somebody remembered to add it here: the helper
# scripts in tools/ importing each other was the case that exposed it.
for _folder, _dirs, _files in _os.walk(ROOT):
    if any(skip in _folder for skip in (".runtime", "__pycache__", ".git")):
        continue
    _LOCAL.update(f[:-3] for f in _files if f.endswith(".py"))
    if "__init__.py" in _files:
        _LOCAL.add(_os.path.basename(_folder))

imported = {}
for folder, _dirs, files in _os.walk(ROOT):
    if any(skip in folder for skip in (".runtime", "__pycache__", ".git", "tests")):
        continue
    # The iPhone app's build tools (mobile/scripts: screenshots, icons) run on
    # a developer's machine, never in the server's container.
    if _os.path.relpath(folder, ROOT).split(_os.sep)[0] == "mobile":
        continue
    for fname in files:
        if not fname.endswith(".py"):
            continue
        full = _os.path.join(folder, fname)
        try:
            tree = _ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError:
            check("%s parses" % fname, False)
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    imported.setdefault(a.name.split(".")[0], set()).add(fname)
            elif isinstance(node, _ast.ImportFrom) and node.level == 0 and node.module:
                imported.setdefault(node.module.split(".")[0], set()).add(fname)

third_party = {n: w for n, w in imported.items()
               if n not in _STDLIB and n not in _LOCAL}
check("the scan found the imports at all", len(third_party) >= 5)
for name, where in sorted(third_party.items()):
    check("%s is in requirements.txt" % name,
          _ALIAS.get(name.lower(), name.lower()) in req_names,
          "imported by %s" % ", ".join(sorted(where)[:3]))

print()
print("%d failure(s)" % len(fails))
print("ACCOUNTS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
