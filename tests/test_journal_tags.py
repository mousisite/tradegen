"""The journal's tags, review, breakdown, daily rules, export and trade picture.

No network: every price is a stand-in. A trade can carry the setup it was
taken from, how the person felt, the checklist ticked and the time it was
really entered; after it closes, a review. The breakdown splits closed trades
by those tags after costs, with a margin of error only from ten trades. The
daily rules stop a trade from being logged until the person says to log it
anyway, and then the trade is marked. Trades from before any of this keep
every number they had.
"""
import csv
import io
import json
import math
import os as _os
import re
import sqlite3
import sys as _sys
import time
import zlib
from datetime import timedelta

import numpy as np

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for _key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SMTP_HOST", "STRIPE_SECRET_KEY",
             "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(_key, None)

c, DB = harness.isolated_web("journaltags")

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import clock  # noqa: E402
from bot import database as db  # noqa: E402
from bot import export  # noqa: E402
from bot import journal  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot.market import Bars  # noqa: E402

web._example_state["busy"] = True
web.app.config["AUTH_MODE"] = "google"

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) < tol


def text(response):
    return response.data.decode("utf-8", "replace")


def flat(page):
    return " ".join(page.split())


# A number that is not one, as Python prints it.
NAN = re.compile(r"\bnan\b|\bNone\b")


def sign_in(client, user):
    k = db.connect(DB)
    try:
        token = A.start_session(k, user.id, "test")
    finally:
        k.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


NOW = int(time.time())
TODAY = clock.ny_clock(NOW)[0]


def ny_at(days_ago, hh, mm):
    """A New York wall-clock moment some days before today."""
    d = TODAY - timedelta(days=days_ago)
    return clock.ny(d.year, d.month, d.day, hh, mm)


# --- stand-in prices -------------------------------------------------------------

def make_bars(symbol, interval, start, step, n, base=100.0):
    rng = np.random.RandomState(zlib.crc32((symbol + interval).encode()) % 10000)
    ts = np.array([start + k * step for k in range(n)], dtype=np.int64)
    close_ = base * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    open_ = np.concatenate([[close_[0]], close_[:-1]])
    high = np.maximum(open_, close_) * 1.001
    low = np.minimum(open_, close_) * 0.999
    return Bars(symbol=symbol, name=symbol, asset_class="equity", currency="USD",
                exchange="NMS", interval=interval, timestamp=ts, open=open_, high=high,
                low=low, close=close_, volume=np.ones(n) * 1000,
                session_id=(ts // 86400).astype(np.int64), gmt_offset=-18000,
                prev_close=float(close_[-2]), live_price=None, market_open=False,
                last_bar_time="", meta={"regularMarketTime": int(ts[-1])})


FETCHED = []


def fake_bars(symbol, interval="1d", lookback=None):
    FETCHED.append((symbol, interval))
    if symbol == "OLDT" and interval == "5m":
        # Five-minute bars that start well after the trade was entered.
        return make_bars(symbol, "5m", NOW - 3 * 86400, 300, 600)
    if interval == "1h":
        return make_bars(symbol, "1h", NOW - 40 * 86400, 3600, 40 * 24)
    if interval in ("5m", "15m"):
        return make_bars(symbol, interval, NOW - 50 * 86400, 900, 50 * 96)
    return make_bars(symbol, "1d", NOW - 400 * 86400, 86400, 400)


REAL = (market_mod.fetch_bars, market_mod.split_history)
market_mod.fetch_bars = fake_bars
market_mod.split_history = lambda symbol: []

try:
    # -----------------------------------------------------------------------
    head("AN OLDER DATABASE GAINS THE COLUMNS AND KEEPS ITS TRADES")
    # -----------------------------------------------------------------------
    old_path = _os.path.join(_os.path.dirname(DB), "old.db")
    raw = sqlite3.connect(old_path)
    raw.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
            account TEXT NOT NULL DEFAULT 'paper', symbol TEXT NOT NULL, interval TEXT,
            direction INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            ts_opened INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL,
            target1 REAL, target2 REAL, quantity REAL, risk_amount REAL, ts_closed INTEGER,
            exit_price REAL, exit_reason TEXT, r_multiple REAL, pnl REAL,
            strategy_note TEXT, notes TEXT);
        INSERT INTO trades (symbol, interval, direction, status, ts_opened, entry, stop,
            ts_closed, exit_price, exit_reason, r_multiple, pnl)
        VALUES ('KEEP', '1d', 1, 'closed', 1700000000, 100, 95, 1700086400, 110,
                'target', 2.0, 0);
    """)
    raw.commit()
    raw.close()
    old = db.connect(old_path)
    columns = {r[1] for r in old.execute("PRAGMA table_info(trades)")}
    wanted = {"setup", "emotion", "followed_plan", "checklist", "lesson", "broke_rule",
              "reviewed_ts"}
    check("the new columns are added", wanted <= columns, wanted - columns)
    kept = old.execute("SELECT * FROM trades WHERE symbol='KEEP'").fetchone()
    check("the trade from before is kept, untouched", kept is not None
          and kept["r_multiple"] == 2.0 and kept["setup"] is None
          and kept["checklist"] is None)
    summary = db.journal_summary(old, costs={"equity": 0.0002, "crypto": 0.002})
    check("and its R after costs is as it was", close(summary["expectancy_r"],
                                                      2.0 - 100 * 0.0002 / 5), summary)
    check("it counts as not tagged in the breakdown",
          journal.breakdown(old, None, {}, "setup")[0]["label"] == "Not tagged")
    old.close()

    # -----------------------------------------------------------------------
    head("TAGS ARE KEPT, AND ONLY KNOWN ONES")
    # -----------------------------------------------------------------------
    conn = db.connect(DB)
    alice = A.upsert_email_user(conn, "alice@example.com")
    bob = A.upsert_email_user(conn, "bob@example.com")
    t1 = db.open_trade(conn, "ZZA", "5m", 1, 100.0, 99.0, user=alice.id,
                       setup="liquidity_model", emotion="calm",
                       checklist=["stop", "bias", "stop"], opened_ts=ny_at(2, 9, 55))
    row = conn.execute("SELECT * FROM trades WHERE id=?", (t1,)).fetchone()
    check("setup and feeling are stored", row["setup"] == "liquidity_model"
          and row["emotion"] == "calm")
    check("the checklist is stored as its keys, once each",
          json.loads(row["checklist"]) == ["bias", "stop"], row["checklist"])
    check("the entry time typed is the trade's", row["ts_opened"] == ny_at(2, 9, 55))
    check("its session and window come from the New York entry time",
          journal.session_of_trade(row) == "ny_am" and journal.window_of_trade(row) == "macro",
          (journal.session_of_trade(row), journal.window_of_trade(row)))
    late = db.open_trade(conn, "ZZA", "5m", 1, 100.0, 99.0, user=alice.id,
                         opened_ts=ny_at(1, 20, 15))
    late_row = conn.execute("SELECT * FROM trades WHERE id=?", (late,)).fetchone()
    check("an evening entry is in the Asia session and outside the morning windows",
          journal.session_of_trade(late_row) == "asia"
          and journal.window_of_trade(late_row) == "outside")
    db.delete_trade(conn, late)

    for kwargs, why in (({"setup": "made_up"}, "setup"), ({"emotion": "angry"}, "feeling"),
                        ({"checklist": ["bias", "lucky"]}, "checklist item")):
        try:
            db.open_trade(conn, "ZZA", "1d", 1, 100.0, 99.0, user=alice.id, **kwargs)
            check("an unknown %s is refused" % why, False)
        except ValueError as exc:
            check("an unknown %s is refused, in words" % why, "journal" in str(exc), exc)
    try:
        db.open_trade(conn, "ZZA", "1d", 1, 100.0, 99.0, user=alice.id,
                      opened_ts=NOW + 3600)
        check("an entry time in the future is refused", False)
    except ValueError as exc:
        check("an entry time in the future is refused", "future" in str(exc), exc)
    try:
        db.open_trade(conn, "ZZA", "1d", 1, 100.0, 99.0, user=alice.id,
                      opened_ts=NOW - 31 * 86400)
        check("one over 30 days back is refused", False)
    except ValueError as exc:
        check("one over 30 days back is refused", "30 days" in str(exc), exc)
    ok = db.open_trade(conn, "ZZA", "1d", 1, 100.0, 99.0, user=alice.id,
                       opened_ts=NOW - 29 * 86400)
    check("29 days back is fine", conn.execute("SELECT ts_opened FROM trades WHERE id=?",
                                               (ok,)).fetchone()[0] == NOW - 29 * 86400)
    slack = db.open_trade(conn, "ZZA", "1d", 1, 100.0, 99.0, user=alice.id,
                          opened_ts=int(time.time()) + 60)
    check("a minute ahead, a phone's clock, is read as now",
          conn.execute("SELECT ts_opened FROM trades WHERE id=?", (slack,)).fetchone()[0]
          <= int(time.time()))
    for tid in (ok, slack):
        db.delete_trade(conn, tid)
    plain = db.open_trade(conn, "ZZP", "1d", 1, 100.0, 99.0, user=alice.id)
    prow = conn.execute("SELECT * FROM trades WHERE id=?", (plain,)).fetchone()
    check("a trade with no tags has none, and no checklist",
          prow["setup"] is None and prow["checklist"] is None and prow["broke_rule"] is None)
    db.delete_trade(conn, plain)

    check("the entry field reads back the time it shows",
          journal.parse_entry(journal.entry_field(ny_at(3, 10, 2))) == ny_at(3, 10, 2))
    try:
        journal.parse_entry("tomorrow at ten")
        check("a time that is not one is refused", False)
    except ValueError as exc:
        check("a time that is not one is refused, in words", "New York" in str(exc))

    # -----------------------------------------------------------------------
    head("A REVIEW AFTER IT CLOSES")
    # -----------------------------------------------------------------------
    try:
        db.review_trade(conn, t1, "yes", "too early", user=alice.id)
        check("an open trade is not reviewed yet", False)
    except ValueError as exc:
        check("an open trade is not reviewed yet", "closed" in str(exc))
    db.close_trade(conn, t1, 102.0, user=alice.id)
    sign_in(c, alice)
    r = c.post("/trades/review", data={"trade_id": str(t1), "followed_plan": "partly",
                                       "lesson": "Moved my stop. Next time leave it.",
                                       "setup": "liquidity_model", "emotion": "fomo"})
    row = conn.execute("SELECT * FROM trades WHERE id=?", (t1,)).fetchone()
    check("the review is saved", r.status_code == 302 and row["followed_plan"] == "partly"
          and row["lesson"] == "Moved my stop. Next time leave it."
          and row["reviewed_ts"] and row["emotion"] == "fomo", (r.status_code, dict(row)))
    page = flat(text(c.get("/trades")))
    check("and shown in the journal", "Moved my stop. Next time leave it." in page
          and "Followed the plan: partly." in page and "Sweep, break and retest" in page
          and "Afraid of missing out" in page, page[page.find("Closed"):][:800])
    check("with a way to see the trade", "/trades/%d/picture" % t1 in page)
    check("and nothing unfinished on the page", ">None<" not in page and not NAN.search(page)
          and "undefined" not in page)
    check("saying once that it is kept", "Your review is kept with the trade." in page
          and "Your review is kept" not in text(c.get("/trades?reviewed=1")))
    r = c.post("/trades/review", data={"trade_id": str(t1), "followed_plan": "maybe"})
    check("a made-up answer is refused", r.status_code == 400)
    sign_in(c, bob)
    r = c.post("/trades/review", data={"trade_id": str(t1), "followed_plan": "no"})
    check("and nothing thanks them for it", "Your review is kept" not in text(c.get("/trades")))
    check("nobody reviews someone else's trade", r.status_code == 400
          and conn.execute("SELECT followed_plan FROM trades WHERE id=?",
                           (t1,)).fetchone()[0] == "partly")
    db.delete_trade(conn, t1)

    # The form always sends the setup and the feeling, so a press of Save on an
    # untouched form was kept, and announced, as a review.
    sign_in(c, alice)
    blank = db.open_trade(conn, "ZZA", "5m", 1, 100.0, 99.0, user=alice.id, setup="own")
    db.close_trade(conn, blank, 101.0, user=alice.id)
    r = c.post("/trades/review", data={"trade_id": str(blank), "followed_plan": "",
                                       "lesson": " ", "setup": "own", "emotion": ""})
    row = conn.execute("SELECT * FROM trades WHERE id=?", (blank,)).fetchone()
    check("a review with nothing in it is refused, and nothing is kept",
          r.status_code == 400 and "Say whether you followed your plan" in text(r)
          and row["reviewed_ts"] is None, r.status_code)
    r = c.post("/trades/review", data={"trade_id": str(blank), "followed_plan": "",
                                       "lesson": "", "setup": "own", "emotion": "calm"})
    check("but a feeling set on it is kept", r.status_code == 302 and conn.execute(
        "SELECT emotion FROM trades WHERE id=?", (blank,)).fetchone()[0] == "calm")
    for answer in ("yes", ""):
        r = c.post("/trades/review", data={"trade_id": str(blank), "followed_plan": answer,
                                           "lesson": "", "setup": "own", "emotion": "calm"})
    check("and an answer given by mistake can be taken back", r.status_code == 302
          and conn.execute("SELECT followed_plan FROM trades WHERE id=?",
                           (blank,)).fetchone()[0] is None)
    db.delete_trade(conn, blank)

    # -----------------------------------------------------------------------
    head("THE BREAKDOWN, ON 40 SCRIPTED TRADES")
    # -----------------------------------------------------------------------
    # Entry 100, stop 99: one R is one dollar, and a 2bp round trip is 0.02R.
    COSTS = {"equity": 0.0002, "crypto": 0.002, "futures": 0.0001}
    COST_R = 100 * 0.0002 / 1.0
    plan = []
    # 12 Sweep-break-retest trades, entered 9:55 (the 9:50-10:10 window).
    for k, exit_ in enumerate([102, 99, 102, 99, 103, 99, 102, 99, 101, 99, 102, 98.5]):
        plan.append(dict(days=1 + k % 6, hh=9, mm=55, exit=exit_, setup="liquidity_model",
                         emotion="calm", followed="yes",
                         checklist=[k for k, _ in journal.setups.CHECKLIST]))
    # 5 breakouts, entered 10:40, afraid of missing out, not all ticked.
    for k, exit_ in enumerate([99, 99, 101, 99, 99]):
        plan.append(dict(days=1 + k, hh=10, mm=40, exit=exit_, setup="crowd_breakout",
                         emotion="fomo", followed="no", checklist=["bias"]))
    # 23 untagged, entered 14:00.
    for k in range(23):
        plan.append(dict(days=1 + k % 7, hh=14, mm=0, exit=101 if k % 3 else 99))
    ids = []
    for p in plan:
        tid = db.open_trade(conn, "ZZB", "5m", 1, 100.0, 99.0, user=alice.id,
                            setup=p.get("setup"), emotion=p.get("emotion"),
                            checklist=p.get("checklist"),
                            opened_ts=ny_at(p["days"], p["hh"], p["mm"]))
        db.close_trade(conn, tid, p["exit"], user=alice.id)
        if p.get("followed"):
            db.review_trade(conn, tid, p["followed"], "", user=alice.id)
        # Closed an hour after it was entered.
        conn.execute("UPDATE trades SET ts_closed=? WHERE id=?",
                     (ny_at(p["days"], p["hh"], p["mm"]) + 3600, tid))
        p["net"] = (p["exit"] - 100.0) - COST_R
        ids.append(tid)
    conn.commit()

    found = journal.review(conn, alice.id, COSTS)
    summary = db.journal_summary(conn, user=alice.id, costs=COSTS)
    check("40 closed trades", found["trades"] == 40 == summary["trades"], found["trades"])
    check("whose average after costs is the journal's own",
          close(found["overall"]["avg_r"], summary["expectancy_r"]),
          (found["overall"]["avg_r"], summary["expectancy_r"]))
    groups = {g["key"]: g for g in journal.split(found["items"], "setup")}
    sbr = [p["net"] for p in plan if p.get("setup") == "liquidity_model"]
    mean = sum(sbr) / len(sbr)
    sd = math.sqrt(sum((x - mean) ** 2 for x in sbr) / (len(sbr) - 1))
    g = groups["liquidity_model"]
    check("a group's count, wins and average after costs are exact",
          g["trades"] == 12 and g["wins"] == sum(1 for x in sbr if x > 0)
          and close(g["avg_r"], mean) and close(g["total_r"], sum(sbr)), g)
    check("from 10 trades it has a margin of two standard errors",
          close(g["margin"], 2 * sd / math.sqrt(12)), (g["margin"], 2 * sd / math.sqrt(12)))
    b = groups["crowd_breakout"]
    check("below 10 it has none, and says too few to tell",
          b["trades"] == 5 and b["margin"] is None and b["verdict"] == "too few to tell", b)
    check("untagged trades are their own group, last",
          list(groups)[-1] == "" and groups[""]["label"] == "Not tagged"
          and groups[""]["trades"] == 23)
    windows = {g["key"]: g["trades"] for g in journal.split(found["items"], "window")}
    check("windows come from the New York entry time",
          windows == {"macro": 12, "after_cutoff": 28}, windows)
    sessions = {g["key"]: g["trades"] for g in journal.split(found["items"], "session")}
    check("and so do sessions", sessions == {"ny_am": 17, "ny_pm": 23}, sessions)
    followed = {g["key"]: g["trades"] for g in journal.split(found["items"], "followed_plan")}
    check("followed the plan, by the review", followed == {"yes": 12, "no": 5, "": 23},
          followed)
    ticked = {g["key"]: g["trades"] for g in journal.split(found["items"], "checklist")}
    check("the checklist: all ticked, some, and not used",
          ticked == {"all": 12, "some": 5, "": 23}, ticked)
    days = {g["key"]: g["trades"] for g in journal.split(found["items"], "weekday")}
    check("weekdays add up", sum(days.values()) == 40)

    by_day = {}
    for p in plan:
        d = TODAY - timedelta(days=p["days"])
        by_day[d] = by_day.get(d, 0.0) + p["net"]
    green = found["green"]
    check("green days are exact", green["days"] == len(by_day)
          and green["green"] == sum(1 for v in by_day.values() if v > 0)
          and green["red"] == sum(1 for v in by_day.values() if v < 0), (green, by_day))
    wins = [p["net"] for p in plan if p["net"] > 0]
    losses = [-p["net"] for p in plan if p["net"] < 0]
    w, l = sum(wins) / len(wins), sum(losses) / len(losses)
    be = found["breakeven"]
    check("the break-even hit rate is L / (W + L) of your own average win and loss",
          close(be["needed"], l / (w + l)) and close(be["reached"],
                                                      len(wins) / (len(wins) + len(losses))),
          be)
    best = found["best_day"]
    total = sum(by_day.values())
    if total > 0:
        check("the best day's share is its R over the total",
              best is not None and close(best["share"], max(by_day.values()) / total), best)
    else:
        check("with no profit there is no best day's share", best is None, best)

    sign_in(c, alice)
    page = flat(text(c.get("/trades/breakdown")))
    check("the breakdown page opens", "What your journal says" in page
          and "Sweep, break and retest" in page and "By the time of the morning" in page
          and "9:50 to 10:10" in page)
    check("with margins where there are 10 trades, and too few to tell below",
          "give or take" in page and "Too few to tell" in page)
    check("it says 40 trades is not under the 30 minimum",
          "not enough to separate skill from luck" not in page)
    check("and nothing unfinished", ">None<" not in page and "nan%" not in page
          and "undefined" not in page)
    limited = flat(text(c.get("/trades/breakdown?limit=1.5&target=3")))
    check("the loss-limit check answers", "A fixed limit, 1.5R below where you started" in
          limited and "A profit target of 3R" in limited)
    for typed in ("-5", "2000"):
        page = flat(text(c.get("/trades/breakdown?limit=5&target=" + typed)))
        check("a profit target of %sR is refused by its rule, and not then reported on" % typed,
              "must be more than 0 and at most 1000R" in page
              and "A profit target of" not in page)
    check("no help sits inside a heading", not re.search(
        r"<h2[^>]*>(?:(?!</h2>).)*<details", limited))
    # 5 November 2026 at 20:30 in New York is the 6th in London.
    evening = clock.ny(2026, 11, 5, 20, 30)
    check("a journal date is the New York one", web._day_ny(evening) == "5 Nov 2026"
          and web._day(evening) == "6 Nov 2026" and web._day_ny(None) == "-")
    late = A.upsert_email_user(conn, "late@example.com")
    tid = db.open_trade(conn, "ZZL", "1h", 1, 100.0, 98.0, quantity=2, user=late.id)
    db.exit_trade(conn, tid, 98.0, 1, "stop", user=late.id)
    db.exit_trade(conn, tid, 97.0, 1, "stop", user=late.id)
    evening = ny_at(2, 23, 30)
    conn.execute("UPDATE trades SET ts_closed=? WHERE id=?", (evening, tid))
    conn.execute("UPDATE trade_exits SET ts=? WHERE trade_id=?", (evening, tid))
    conn.commit()
    sign_in(c, late)
    there = (TODAY - timedelta(days=2)).strftime("%d %b %Y").lstrip("0")
    page = flat(text(c.get("/trades/breakdown?limit=1")))
    check("the breakdown dates a limit hit in New York", "<b>hit</b> on " + there in page
          and "<b>hit</b> on " + web._day(evening) not in page, page[-1500:])
    page = flat(text(c.get("/trades")))
    check("and the journal each exit", page.count("stop, " + there) == 2, page[-3000:])
    db.delete_trade(conn, tid)
    sign_in(c, alice)
    midnight = NOW - NOW % 86400 - 3 * 86400            # 00:00 UTC, the evening before there
    coin = make_bars("ZZC-USD", "1d", midnight - 9 * 86400, 86400, 12)
    check("a daily coin bar keeps its own day, and any other moment is New York's",
          web._touched_day({"interval": "1d"}, coin, midnight) == web._day(midnight)
          and web._touched_day({"interval": "1h"}, coin, midnight) == web._day_ny(midnight)
          and web._day_ny(midnight) != web._day(midnight)
          and web._touched_day({"interval": "1d"}, coin, midnight + 90 * 60)
          == web._day_ny(midnight)
          and web._touched_day({"interval": "1d"}, coin, None) == "")
    check("a price is filled in without a feed's noise, and no coarser than it matters",
          [web._plain_price(x) for x in (85.90824904048138, 101.0, 63012.341796875, 1.07345,
                                         0.0000084213, 5000, None, float("nan"))]
          == ["85.9082", "101", "63012.34", "1.07345", "0.0000084213", "5000", "", ""])

    # -----------------------------------------------------------------------
    head("FEWER THAN 30, AND NONE")
    # -----------------------------------------------------------------------
    carol = A.upsert_email_user(conn, "carol@example.com")
    sign_in(c, carol)
    page = flat(text(c.get("/trades/breakdown")))
    check("no closed trades says so", "No closed trades yet" in page)
    few = db.open_trade(conn, "ZZC", "1d", 1, 100.0, 99.0, user=carol.id)
    db.close_trade(conn, few, 101.0, user=carol.id)
    page = flat(text(c.get("/trades/breakdown")))
    check("below 30 it says nothing separates skill from luck",
          "1 closed trade is not enough to separate skill from luck" in page)

    # -----------------------------------------------------------------------
    head("DAILY RULES")
    # -----------------------------------------------------------------------
    cfg = dict(web.config_mod.DEFAULTS, daily_loss_limit_r=2.0)
    then = ny_at(2, 15, 0)
    dan = A.upsert_email_user(conn, "dan@example.com")

    def closed_at(user, exit_, opened, shut):
        tid = db.open_trade(conn, "ZZR", "5m", 1, 100.0, 99.0, user=user.id,
                            opened_ts=opened)
        db.close_trade(conn, tid, exit_, user=user.id)
        conn.execute("UPDATE trades SET ts_closed=? WHERE id=?", (shut, tid))
        conn.commit()
        return tid

    closed_at(dan, 95.0, ny_at(3, 10, 0), ny_at(3, 11, 0))      # the day before: not today
    closed_at(dan, 98.5, ny_at(2, 9, 40), ny_at(2, 10, 0))      # -1.52R after costs
    closed_at(dan, 99.4, ny_at(2, 10, 30), ny_at(2, 11, 0))     # -0.62R after costs
    closed_at(dan, 90.0, ny_at(2, 10, 45), ny_at(2, 16, 0))     # closed after 15:00
    broken = journal.rules_check(conn, dan.id, cfg, then, costs=COSTS)
    check("the loss limit counts today's closed trades, after costs",
          [b["rule"] for b in broken] == ["loss"]
          and "you have lost 2.1R today (limit 2R)" in broken[0]["words"], broken)
    check("and says so in the rule's words", journal.rules_words(broken) ==
          "Your rule says stop for today: you have lost 2.1R today (limit 2R).")
    check("one less loss and it holds", not journal.rules_check(
        conn, dan.id, dict(cfg, daily_loss_limit_r=2.5), then, costs=COSTS))
    timed = dict(web.config_mod.DEFAULTS, last_entry_time="10:30")
    check("the late-entry rule at 10:31 with 10:30 set",
          [b["rule"] for b in journal.rules_check(conn, dan.id, timed, ny_at(1, 10, 31))]
          == ["time"])
    check("but not at 10:30", not journal.rules_check(conn, dan.id, timed, ny_at(1, 10, 30)))
    check("and the time is said", "it is 10:31 New York time, after your last entry time "
          "of 10:30" in journal.rules_check(conn, dan.id, timed, ny_at(1, 10, 31))[0]["words"])
    check("rules switched off say nothing",
          not journal.rules_check(conn, dan.id, web.config_mod.DEFAULTS, then, costs=COSTS))

    # A trade typed in later is told about its own day, not about today.
    late = journal.rules_check(conn, dan.id, dict(timed, max_trades_per_day=1), ny_at(1, 10, 31),
                               present=ny_at(0, 12, 0))
    check("a trade from an earlier day is told when it was entered",
          journal.rules_words(late) == "Your rule said stop that day: you entered at 10:31 New "
          "York time, after your last entry time of 10:30.", journal.rules_words(late))
    late = journal.rules_check(conn, dan.id, dict(cfg, max_trades_per_day=1), then, costs=COSTS,
                               present=NOW)
    check("and so for the loss and the count", journal.rules_words(late) ==
          "Your rules said stop that day: you had lost 2.1R that day (limit 2R) and you had "
          "taken 3 trades that day (most 1).", journal.rules_words(late))
    late = journal.rules_check(conn, dan.id, timed, ny_at(1, 10, 31), present=ny_at(1, 13, 0))
    check("one from earlier today is still today's", journal.rules_words(late) ==
          "Your rule says stop for today: you entered at 10:31 New York time, after your last "
          "entry time of 10:30.", journal.rules_words(late))

    erin = A.upsert_email_user(conn, "erin@example.com")
    A.save_settings(conn, erin.id, {"max_trades_per_day": 2})
    sign_in(c, erin)
    form = {"symbol": "ZZM", "direction": "long", "entry": "100", "stop": "99",
            "interval": "1d", "setup": "own", "checklist": ["bias", "stop"]}
    for _ in range(2):
        r = c.post("/trades/open", data=form)
        check("a trade inside the rules is logged", r.status_code == 302, r.status_code)
    r = c.post("/trades/open", data=form)
    page = flat(text(r))
    count = conn.execute("SELECT COUNT(*) FROM trades WHERE user_id=?", (erin.id,)).fetchone()[0]
    check("the third, with a most of 2, shows the stop page and logs nothing",
          r.status_code == 200 and "Your rule says stop for today: you have taken 2 trades "
          "today (most 2)." in page and count == 2, (r.status_code, count))
    check("which offers not to log it, or to log it marked",
          "Don't log it" in page and "Log it anyway and mark that I broke my rule" in page
          and 'name="override" value="1"' in page)
    check("and carries every field, the checklist's boxes included",
          page.count('name="checklist"') == 2 and 'value="ZZM"' in page)
    r = c.post("/trades/open", data=dict(form, override="1"))
    row = conn.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC",
                       (erin.id,)).fetchone()
    check("override logs it and stores the rule it broke", r.status_code == 302
          and row["broke_rule"] == "trades" and row["setup"] == "own"
          and json.loads(row["checklist"]) == ["bias", "stop"], dict(row))
    page = flat(text(c.get("/trades")))
    check("the journal marks it", "broke a rule" in page)
    db.close_trade(conn, row["id"], 101.0, user=erin.id)
    tagged = {g["key"]: g["trades"] for g in journal.breakdown(conn, erin.id, COSTS,
                                                               "broke_rule")}
    check("broken rules show up in the breakdown", tagged == {"yes": 1}, tagged)

    # One typed in for yesterday is held to yesterday, and the page says so.
    A.save_settings(conn, erin.id, {"max_trades_per_day": 2, "last_entry_time": "00:01"})
    page = flat(text(c.post("/trades/open", data=dict(
        form, opened=journal.entry_field(ny_at(1, 9, 45)),
        opened_shown=journal.entry_field(NOW)))))
    check("the stop page for an earlier day's trade never says today",
          "Your rule said stop that day: you entered at 9:45 New York time" in page
          and "Log it anyway?" in page and "Stop for today?" not in page, page[:1500])

    # The settings page keeps the rules.
    r = c.post("/settings", data={"daily_loss_limit_r": "2", "max_trades_per_day": "3",
                                  "last_entry_time": "10:30", "cost_bps_futures": "0.8",
                                  "calm_candles": "1"})
    saved = A.settings_for(conn, erin.id)
    check("the settings page saves the daily rules", r.status_code == 302
          and saved.get("daily_loss_limit_r") == 2.0 and saved.get("max_trades_per_day") == 3
          and saved.get("last_entry_time") == "10:30" and saved.get("cost_bps_futures") == 0.8
          and saved.get("calm_candles") is True, saved)
    r = c.post("/settings", data={"last_entry_time": "half ten"})
    check("and refuses a time that is not one, in words", r.status_code == 200
          and "a time like 10:30" in text(r))
    r = c.post("/settings", data={"max_trades_per_day": "2.5"})
    check("and a part of a trade", "whole number" in text(r))
    r = c.post("/settings", data={"last_entry_time": ""})
    check("an empty time switches the rule off",
          A.settings_for(conn, erin.id).get("last_entry_time") == "")
    page = flat(text(c.get("/settings")))
    check("the settings page shows the rules", "Your trading rules" in page
          and "Most trades a day" in page and "Futures, round trip" in page)

    # A paper trade from a plan is held to the same rules.
    run = conn.execute("INSERT INTO runs (ts, symbol, interval, price, action, entry, stop, "
                       "target1, asset_class, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (int(time.time()), "ZZM", "1d", 100.0, "BUY", 100.0, 95.0, 110.0,
                        "equity", erin.id)).lastrowid
    conn.commit()
    page = flat(text(c.get("/trades/simulate?run_id=%d" % run)))
    check("the review page offers the tags, set to From an Orenth call",
          'value="orenth_call" selected' in page and 'form="take"' in page
          and "How you felt" in page)
    r = c.post("/trades/take", data={"run_id": str(run), "setup": "orenth_call",
                                     "emotion": "calm"})
    check("a trade from a plan is stopped by the rules too", r.status_code == 200
          and "Your rule says stop for today" in text(r))
    r = c.post("/trades/take", data={"run_id": str(run), "setup": "orenth_call",
                                     "emotion": "calm", "override": "1"})
    row = conn.execute("SELECT * FROM trades WHERE run_id=?", (run,)).fetchone()
    check("and logged with its tags when the person says so", r.status_code == 302
          and row["setup"] == "orenth_call" and row["emotion"] == "calm"
          and row["broke_rule"] == "trades", r.status_code)

    # A hand-entered trade at an earlier time.
    A.save_settings(conn, erin.id, {})
    typed = journal.entry_field(ny_at(4, 9, 45))
    r = c.post("/trades/open", data=dict(form, opened=typed,
                                         opened_shown=journal.entry_field(NOW)))
    row = conn.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC",
                       (erin.id,)).fetchone()
    check("a trade typed in for an earlier time keeps that time", r.status_code == 302
          and row["ts_opened"] == ny_at(4, 9, 45), (r.status_code, row["ts_opened"]))
    shown = journal.entry_field(NOW - 600)
    r = c.post("/trades/open", data=dict(form, opened=shown, opened_shown=shown))
    row = conn.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC",
                       (erin.id,)).fetchone()
    check("one left as the form filled it in is logged when it is sent",
          r.status_code == 302 and row["ts_opened"] >= NOW, row["ts_opened"])
    r = c.post("/trades/open", data=dict(form, opened=journal.entry_field(NOW - 40 * 86400),
                                         opened_shown=shown))
    check("40 days back is refused in words", r.status_code == 400
          and "30 days" in text(r))

    # -----------------------------------------------------------------------
    head("EVERY TRADE CAN BE REACHED, AND THE COUNTS AGREE")
    # -----------------------------------------------------------------------
    hal = A.upsert_email_user(conn, "hal@example.com")
    sign_in(c, hal)
    put = ("INSERT INTO trades (account, symbol, interval, direction, status, ts_opened, entry, "
           "stop, quantity, ts_closed, exit_price, exit_reason, r_multiple, pnl, user_id) "
           "VALUES (?,?,'1d',1,?,?,100,99,?,?,?,?,?,?,?)")
    for k in range(60):
        conn.execute(put, ("paper", "OPN%d" % k, "open", NOW - 20 * 86400 + k, 1, None, None,
                           None, None, None, hal.id))
    # Opened in one order and closed in the other.
    for k in range(130):
        conn.execute(put, ("paper", "CLS%d" % k, "closed", NOW - 40 * 86400 + k, None,
                           NOW - 1000 - k, 100.0, "manual", 0.0, 0.0, hal.id))
    conn.execute(put, ("live", "LIVE1", "open", NOW, 1, None, None, None, None, None, hal.id))
    conn.execute(put, ("live", "LIVE2", "closed", NOW, 1, NOW, 101.0, "manual", 1.0, 1.0,
                       hal.id))
    conn.commit()
    page = text(c.get("/trades"))
    check("every open position has its Close form",
          page.count('action="/trades/close"') == 60 and "60 positions" in page
          and "<b>OPN0</b>" in page, page.count('action="/trades/close"'))
    check("the closed ones are a page at a time, and the heading gives the real number",
          "130 trades, 1 to 100 shown" in flat(page) and "Page 1 of 2" in page
          and page.count('action="/trades/review"') == 100, flat(page)[-600:])
    check("the latest closed first", page.index("<b>CLS0</b>") < page.index("<b>CLS99</b>")
          and "<b>CLS100</b>" not in page)
    older = text(c.get("/trades?page=2"))
    check("and the rest on the next page", "130 trades, 101 to 130 shown" in flat(older)
          and "<b>CLS129</b>" in older and older.count('action="/trades/review"') == 30
          and "?page=1#closed" in older and "?page=3" not in older)
    check("a page that does not exist is the last one, and nonsense the first",
          "<b>CLS129</b>" in text(c.get("/trades?page=99"))
          and "<b>CLS0</b>" in text(c.get("/trades?page=x"))
          and "<b>CLS0</b>" in text(c.get("/trades?page=-4")))
    check("a journal of another name, which the figures leave out, is not listed",
          "LIVE1" not in page and "LIVE2" not in page
          and "LIVE1" not in text(c.get("/portfolio")))
    check("an even trade is not coloured as a loss, and one with no size has no cash",
          'class="r fig loss" data-label="R"' not in page
          and not re.search(r'data-label="Cash">\s*0\.00', page))
    check("the page says which figures are after costs and which before",
          "These figures are after trading costs." in flat(page)
          and "R and cash before costs" in flat(page))
    last = conn.execute("SELECT id FROM trades WHERE symbol='CLS129'").fetchone()[0]
    r = c.post("/trades/review", data={"trade_id": str(last), "followed_plan": "yes",
                                       "lesson": "", "page": "2"})
    check("a review on the second page comes back to it",
          "page=2" in r.headers["Location"] and r.headers["Location"].endswith("#t%d" % last))
    r = c.post("/trades/delete", data={"trade_id": str(last), "page": "2"})
    check("and so does a delete", r.headers["Location"].endswith("/trades?page=2"))
    front = flat(text(c.get("/")))
    check("the front page shows 20 and says how many more", front.count("<td><b>OPN") == 20
          and "The newest 20. 40 more in" in front)
    # The oldest of them goes through its stop: it is not among the 20 listed.
    real_mark = web._mark_to_market
    web._mark_to_market = lambda trades: {
        t["id"]: {"price": 98.0 if t["symbol"] == "OPN0" else 100.0, "r": 0.0, "as_of": "now",
                  "status": "past stop" if t["symbol"] == "OPN0" else "open"} for t in trades}
    try:
        front = flat(text(c.get("/")))
    finally:
        web._mark_to_market = real_mark
    check("and tells of a stop crossed on one it does not list",
          "<b>OPN0</b> long:" in front
          and "None of your open positions is past its stop" not in front, front[:1500])

    # Every open position's price is read by the background check each round.
    for k in range(60, db.MAX_OPEN_TRADES - 1):        # and the one of the other name
        conn.execute(put, ("paper", "OPN%d" % k, "open", NOW - 86400, 1, None, None, None, None,
                           None, hal.id))
    conn.commit()
    one = {"symbol": "ZZM", "direction": "long", "entry": "100", "stop": "99"}
    r = c.post("/trades/open", data=one)
    check("a journal holds so many open positions and no more, said in words",
          r.status_code == 400 and "can hold 200 open positions at once" in text(r), text(r)[:400])
    db.close_trade(conn, conn.execute("SELECT id FROM trades WHERE symbol='OPN0'").fetchone()[0],
                   101.0)
    check("and takes another once one is closed",
          c.post("/trades/open", data=one).status_code == 302)
    web._RATE.clear()
    sign_in(c, carol)
    limit, web.TRADES_PER_HOUR = web.TRADES_PER_HOUR, 2
    try:
        got = [c.post("/trades/open", data=one).status_code for _ in range(3)]
    finally:
        web.TRADES_PER_HOUR = limit
        web._RATE.clear()
    check("logging trades has an hourly budget", got == [302, 302, 429], got)

    # -----------------------------------------------------------------------
    head("A FORM SENT TWICE, AND NUMBERS NOTHING HAS")
    # -----------------------------------------------------------------------
    ivy = A.upsert_email_user(conn, "ivy@example.com")
    sign_in(c, ivy)

    def held():
        return conn.execute("SELECT COUNT(*) FROM trades WHERE user_id=?",
                            (ivy.id,)).fetchone()[0]

    names = [re.search(r'name="form_id" value="([^"]+)"', text(c.get("/trades"))).group(1)
             for _ in range(2)]
    check("each journal page names its form", names[0] != names[1] and len(names[0]) > 10)
    twice = dict(one, form_id=names[0], quantity="3", target="104")
    got = [c.post("/trades/open", data=twice).status_code for _ in range(2)]
    check("Add to journal pressed twice logs one trade", got == [302, 302] and held() == 1,
          (got, held()))
    c.post("/trades/open", data=dict(twice, entry="101"))
    check("the same form sent again with another price is another trade", held() == 2, held())
    c.post("/trades/open", data=dict(twice, form_id=names[1]))
    check("and so is the same trade from a fresh form", held() == 3, held())
    for _ in range(2):
        c.post("/trades/open", data=one)
    check("a form with no name is logged as it always was", held() == 5, held())
    # Two presses at once: both get past the first look.
    import threading
    gate = threading.Barrier(2)
    real_trail = web._trail_asked

    def slow_trail(symbol, interval):
        gate.wait(10)
        return real_trail(symbol, interval)

    answers = []

    def press():
        own = web.app.test_client()
        sign_in(own, ivy)
        answers.append(own.post("/trades/open",
                                data=dict(twice, form_id="pressed-twice")).status_code)

    web._trail_asked = slow_trail
    try:
        presses = [threading.Thread(target=press) for _ in range(2)]
        for p in presses:
            p.start()
        for p in presses:
            p.join()
    finally:
        web._trail_asked = real_trail
    check("two presses at once log one", answers == [302, 302] and held() == 6,
          (answers, held()))

    huge = "99999999999999999999"
    for path, form in (("/trades/close", {"price": "100"}),
                       ("/trades/trail", {"trail": "5", "trail_kind": "pct"}),
                       ("/trades/review", {"followed_plan": "yes"}), ("/trades/delete", {})):
        r = c.post(path, data=dict(form, trade_id=huge))
        check("%s with a number nothing has says there is no such trade" % path,
              r.status_code == 400 and ("no such trade in your journal" in text(r)
                                        or "not in your journal" in text(r)), r.status_code)
        for bad in ({}, {"trade_id": "x"}):
            r = c.post(path, data=dict(form, **bad))
            check("and without one, that the form was incomplete", r.status_code == 400
                  and "That form was incomplete. Go back and try again." in text(r),
                  r.status_code)
    r = c.post("/trades/take", data={"run_id": huge})
    check("a plan nothing has is refused in words", r.status_code == 400
          and "Something broke" not in text(r), r.status_code)
    r = c.post("/trades/take", data={})
    check("and one not named at all", r.status_code == 400
          and "That form was incomplete." in text(r), r.status_code)
    check("the review page and the picture are not found",
          c.get("/trades/simulate?run_id=" + huge).status_code == 404
          and c.get("/trades/%s/picture" % huge).status_code == 404)

    # -----------------------------------------------------------------------
    head("THE EXPORT")
    # -----------------------------------------------------------------------
    rows = export.trades(conn, user=alice.id)
    check("the export has the new columns",
          {"setup", "emotion", "checklist", "broke_rule", "followed_plan", "lesson",
           "reviewed", "session"} <= set(export.TRADE_COLUMNS))
    first = next(r for r in rows if r["setup"] == "Sweep, break and retest")
    check("in words", first["setup"] == "Sweep, break and retest" and first["emotion"] == "Calm"
          and first["checklist"].startswith("8 of 8 ticked") and first["followed_plan"] == "Yes"
          and first["session"].startswith("New York morning"), first)
    body = export.build(conn, "trades", "csv", user=alice.id)["body"]
    header = next(csv.reader(io.StringIO(body)))
    check("and the CSV carries them", header == export.TRADE_COLUMNS)

    # -----------------------------------------------------------------------
    head("SEE THE TRADE")
    # -----------------------------------------------------------------------
    old_trade = db.open_trade(conn, "OLDT", "5m", 1, 100.0, 99.0, 103.0, user=alice.id,
                              opened_ts=NOW - 10 * 86400)
    db.close_trade(conn, old_trade, 101.0, user=alice.id)
    sign_in(c, alice)
    FETCHED.clear()
    r = c.get("/trades/%d/picture" % old_trade)
    page = flat(text(r))
    check("an older trade is drawn on hourly bars, and says so", r.status_code == 200
          and "Drawn on hourly bars: 5-minute bars from when it was entered are no longer "
          "available." in page and "<svg" in page, page[:1500])
    check("having tried its own bars first", FETCHED[:2] == [("OLDT", "5m"), ("OLDT", "1h")],
          FETCHED)
    check("with entry, stop, target and exit drawn",
          all(w in page for w in ("Entry ", "Stop ", "Target ", "Exit ")))
    check("in New York time", "New York time" in page)
    check("and nothing unfinished", ">None<" not in page and not NAN.search(page))
    recent = db.open_trade(conn, "ZZN", "15m", -1, 100.0, 101.0, user=alice.id,
                           opened_ts=NOW - 2 * 86400)
    page = flat(text(c.get("/trades/%d/picture" % recent)))
    check("a recent trade uses its own bars", "Drawn on" not in page and "<svg" in page
          and "still open" in page)
    sign_in(c, bob)
    check("someone else's trade is not found", c.get("/trades/%d/picture"
                                                     % old_trade).status_code == 404)
    visitor = web.app.test_client()
    check("nor is a member's trade for a visitor",
          visitor.get("/trades/%d/picture" % old_trade).status_code == 404)
    page = flat(text(visitor.get("/trades")))
    links = re.findall(r'href="(/trades/\d+/picture)"', page)
    check("a visitor sees the example's trades, each with its picture", links, page[:400])
    if links:
        r = visitor.get(links[0])
        check("which opens", r.status_code == 200 and "See the trade" in text(r))
    check("and the example's breakdown opens",
          visitor.get("/trades/breakdown").status_code == 200)
    conn.close()
finally:
    market_mod.fetch_bars, market_mod.split_history = REAL

print()
print("%d failure(s)" % len(fails))
if fails:
    for f in fails:
        print("   - " + f)
    _sys.exit(1)
print("JOURNAL TAGS OK")
