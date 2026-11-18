"""Trailing stops, partial exits, and closing a trade after a split.

No network: every price is a stand-in. Four places read a position's stop,
the portfolio, the journal, paper.py and the background stop notice, and they
must agree, trail and split included, on the trade's own bars, and so must
the front page: a crossing any of them saw stays one. A position closed in
parts must count right everywhere: its R, the journal's statistics, the
portfolio's size and risk, the front page, the export, deleting it, and an
older version reading the same file.
"""
import csv
import html
import io
import os as _os
import re
import sqlite3
import sys as _sys
import time
import types
from types import SimpleNamespace as NS

import numpy as np

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for _key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SMTP_HOST", "STRIPE_SECRET_KEY",
             "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(_key, None)

c, DB = harness.isolated_web("journalexits")

import web  # noqa: E402
import paper  # noqa: E402
from rich.console import Console  # noqa: E402

from bot import accounts as A  # noqa: E402
from bot import alerts  # noqa: E402
from bot import database as db  # noqa: E402
from bot import export  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot import portfolio as P  # noqa: E402
from bot.portfolio import Trail, read_stop, stop_touched, trail_problem  # noqa: E402

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


def flat(html):
    return " ".join(html.split())


DAY = 86400
T0 = 1_760_000_000 - 1_760_000_000 % DAY          # a day's start
OPENED = T0 + 3600                                  # bought an hour into it


def bars_of(highs, lows, live, start=None, splits=(), closes=None, symbol="X"):
    """Daily stand-in bars, one a day from the day after OPENED unless start says."""
    start = T0 + DAY if start is None else start
    n = len(highs)
    hi, lo = np.asarray(highs, dtype=float), np.asarray(lows, dtype=float)
    cl = np.asarray(closes, dtype=float) if closes is not None else (hi + lo) / 2
    ts = np.array([start + k * DAY for k in range(n)], dtype=np.int64)
    return NS(symbol=symbol, name=symbol, interval="1d", timestamp=ts, open=cl,
              high=hi, low=lo, close=cl, volume=np.ones(n), last_price=float(live),
              splits=list(splits), asset_class="equity", currency="USD",
              meta={"regularMarketTime": int(ts[-1]) + 3600 if n else T0},
              market_open=False, last_bar_time="", gmt_offset=0,
              prev_close=float(cl[-1]) if n else float(live))


def bars_at(rows, live, interval="1h", symbol="X"):
    """Stand-in bars that start at the times given, each row (start, high, low)."""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    hi = np.array([r[1] for r in rows], dtype=float)
    lo = np.array([r[2] for r in rows], dtype=float)
    cl = (hi + lo) / 2
    return NS(symbol=symbol, name=symbol, interval=interval, timestamp=ts, open=cl,
              high=hi, low=lo, close=cl, volume=np.ones(len(ts)), last_price=float(live),
              splits=[], asset_class="equity", currency="USD",
              meta={"regularMarketTime": int(ts[-1]) + 3600}, market_open=False,
              last_bar_time="", gmt_offset=0, prev_close=float(cl[-1]))


def first(bars, k):
    """The first k bars of a stand-in, as if read k days in."""
    return NS(**dict(vars(bars), timestamp=bars.timestamp[:k], high=bars.high[:k],
                     low=bars.low[:k], close=bars.close[:k], open=bars.open[:k],
                     last_price=float(bars.close[k - 1])))


# ---------------------------------------------------------------------------
head("THE STOP RULE: A TRAIL TIGHTENS, THEN IS HIT")
# ---------------------------------------------------------------------------
# A long from 100 with its stop at 95, trailing 5% behind the high.
five = Trail(pct=0.05, since=OPENED)
climb = bars_of(highs=[102, 106, 110, 108, 109], lows=[100, 103, 106, 104.6, 104.4], live=105)
stops = [read_stop(first(climb, k), 1, 100.0, 95.0, OPENED, trail=five).stop
         for k in range(1, 6)]
check("the stop follows the high, 5% behind it", close(stops[0], 102 * 0.95)
      and close(stops[1], 106 * 0.95) and close(stops[2], 110 * 0.95), stops)
check("and only ever tightens", all(b >= a for a, b in zip(stops, stops[1:])), stops)
read = read_stop(climb, 1, 100.0, 95.0, OPENED, trail=five)
check("max(initial stop, highest price since open minus the trail)",
      close(read.stop, max(95.0, 110 * 0.95)) and close(read.best, 110.0) and read.moved, read)
check("a low through the trailed stop is a hit, at the stop in force then",
      read.touched_at == int(climb.timestamp[4]) and close(read.touched, 104.4)
      and close(read.touched_stop, 104.5) and read.hit and not read.past, read)

# A bar is held to the stop in force when it started: its own high may have
# come after its low, so it cannot tighten the stop it is judged by.
same_bar = bars_of(highs=[102, 106, 110], lows=[100, 103, 101], live=108)
read = read_stop(same_bar, 1, 100.0, 95.0, OPENED, trail=five)
check("a bar's own new high does not tighten the stop it is judged by",
      read.touched_at is None and close(read.stop, 104.5) and not read.hit, read)

# The mirror for a short: from 100, stop 105, 5% above the low.
fall = bars_of(highs=[100, 97, 93, 94.6], lows=[98, 94, 90, 92], live=93)
read = read_stop(fall, -1, 100.0, 105.0, OPENED, trail=five)
check("a short's stop follows the low down, 5% above it",
      close(read.stop, 90 * 1.05) and close(read.best, 90.0), read)
check("and a high through it is a hit", read.touched_at == int(fall.timestamp[3])
      and close(read.touched, 94.6) and close(read.touched_stop, 94.5), read)

# An ATR trail is a fixed distance, kept from when it was set.
atr = Trail(distance=3.0, atr=1.5, since=OPENED)
read = read_stop(bars_of([104, 108], [101, 105.5], live=107), 1, 100.0, 95.0, OPENED, trail=atr)
check("an ATR trail sits its distance under the high",
      close(read.stop, 105.0) and not read.hit and read.trail.words().startswith("1.5 ATR"),
      read)

# Set after opening: the high since opening counts, the dips before it do not.
later = Trail(pct=0.05, since=T0 + int(2.5 * DAY))
read = read_stop(bars_of([110, 104, 106], [100, 99, 105], live=106), 1, 100.0, 95.0,
                 OPENED, trail=later)
check("a trail set later trails from the high since opening",
      close(read.stop, 104.5), read.stop)
check("but a dip from before it was set is not a hit", read.touched_at is None, read)
read = read_stop(bars_of([110, 104, 106], [100, 99, 104], live=106), 1, 100.0, 95.0,
                 OPENED, trail=later)
check("one after it is", read.touched_at == T0 + 3 * DAY, read)

# A best price kept from a longer look holds the stop up for bars that start
# after it, here bars that never saw the 120.
kept = Trail(pct=0.05, since=OPENED, best=120.0, best_at=T0 + int(1.5 * DAY))
short_look = bars_of([115, 114], [114.5, 113.9], live=114.5, start=T0 + 2 * DAY)
read = read_stop(short_look, 1, 100.0, 95.0, OPENED, trail=kept)
check("a kept best holds the stop up where the bars cannot see it",
      close(read.stop, 114.0) and close(read.best, 120.0) and read.touched_at == T0 + 3 * DAY,
      read)
check("without it the same bars would trail from their own high",
      not read_stop(short_look, 1, 100.0, 95.0, OPENED, trail=five).hit)

# A ten-for-one split after opening: the trade is in the shares it opened
# with, the bars in today's.
read = read_stop(bars_of([105, 110, 108], [100, 104, 104.2], live=103), 1, 1000.0, 950.0,
                 OPENED, split=10.0, trail=five)
check("after a split the trail works in the shares the trade opened with",
      close(read.stop, 1045.0) and close(read.price, 1030.0) and read.past
      and close(read.touched, 1042.0), read)

read = read_stop(bars_of([102], [100], live=120), 1, 100.0, 95.0, OPENED, trail=five, now=999)
check("today's price is part of the best, and moves the stop now",
      close(read.stop, 114.0) and read.best_at == 999, read)

plain = NS(timestamp=[100, 200, 300], low=[8.0, 9.5, 8.9], high=[11, 11, 11], last_price=9.6)
check("a stop that does not trail reads as it always did",
      stop_touched(plain, 150, 9.0, 1) == (300, 8.9)
      and read_stop(plain, 1, 10.0, 9.0, 150).touched_at == 300)

before = read_stop(climb, 1, 100.0, 95.0, OPENED, trail=five)
check("no new trail for a position already past its stop",
      "already past its stop" in (trail_problem("X", 1, before, before) or ""))
calm = bars_of([102, 106, 110], [100, 103, 106], live=108)
before = read_stop(calm, 1, 100.0, 95.0, OPENED, trail=five)
wider = read_stop(calm, 1, 100.0, 95.0, OPENED, trail=Trail(pct=0.10, since=OPENED))
check("a wider trail that would loosen the stop is refused",
      "only tightens" in (trail_problem("X", 1, before, wider) or ""))
tight = read_stop(calm, 1, 100.0, 95.0, OPENED, trail=Trail(pct=0.01, since=OPENED))
check("one that would put the stop past today's price is refused",
      "past today's price" in (trail_problem("X", 1, before, tight) or ""))
narrower = read_stop(calm, 1, 100.0, 95.0, OPENED, trail=Trail(pct=0.03, since=OPENED))
check("and a narrower one is fine", trail_problem("X", 1, before, narrower) is None)


# ---------------------------------------------------------------------------
head("CLOSING PART OF A POSITION")
# ---------------------------------------------------------------------------
conn = db.connect(DB)
alice = A.upsert_email_user(conn, "alice@example.com")
bob = A.upsert_email_user(conn, "bob@example.com")

ta = db.open_trade(conn, "ZZA", "1d", 1, 100.0, 90.0, 130.0, quantity=60, user=alice.id)
first_part = db.exit_trade(conn, ta, 110.0, 20, "target", user=alice.id)
row = conn.execute("SELECT * FROM trades WHERE id=?", (ta,)).fetchone()
check("a part closes, and the rest stays open",
      not first_part["closed"] and close(first_part["left"], 40) and row["status"] == "open")
check("the trade's own row is untouched: its size, and no exit",
      row["quantity"] == 60 and row["exit_price"] is None and row["r_multiple"] is None)
check("the part's R and cash are its own", close(first_part["r_multiple"], 1.0)
      and close(first_part["pnl"], 200.0), first_part)
db.exit_trade(conn, ta, 120.0, 20, "target", user=alice.id)
try:
    db.exit_trade(conn, ta, 100.0, 25, user=alice.id)
    check("closing more than is open is refused", False)
except ValueError as exc:
    check("closing more than is open is refused, saying how much is", "Only 20" in str(exc), exc)
last = db.exit_trade(conn, ta, 95.0, None, "stop", user=alice.id)
row = conn.execute("SELECT * FROM trades WHERE id=?", (ta,)).fetchone()
parts = conn.execute("SELECT COUNT(*) FROM trade_exits WHERE trade_id=?", (ta,)).fetchone()[0]
check("the trade is closed when nothing is left", last["closed"] and row["status"] == "closed"
      and parts == 3, (dict(row), parts))
check("its row then holds the whole position: the average exit",
      close(row["exit_price"], (20 * 110 + 20 * 120 + 20 * 95) / 60.0), row["exit_price"])
check("the R of the whole position, each part weighted by its size",
      close(row["r_multiple"], (1.0 + 2.0 - 0.5) / 3), row["r_multiple"])
check("and the cash of all three", close(row["pnl"], 200 + 400 - 100), row["pnl"])
whole = db.trade_parts(row, db.exits_for(conn, [ta])[ta])
check("which is what the parts add up to", close(whole["r"], row["r_multiple"])
      and close(whole["pnl"], 500) and whole["parts"] == 3 and whole["left"] == 0, whole)

loose = db.open_trade(conn, "ZZU", "1d", 1, 100.0, 90.0, user=alice.id)
try:
    db.exit_trade(conn, loose, 105.0, 5, user=alice.id)
    check("a trade with no size cannot be closed in part", False)
except ValueError as exc:
    check("a trade with no size cannot be closed in part", "without a size" in str(exc))
check("but closes whole as before", close(db.close_trade(conn, loose, 105.0, user=alice.id)
                                          ["r_multiple"], 0.5))

twice = db.open_trade(conn, "ZZD", "1d", 1, 100.0, 90.0, quantity=10, user=alice.id)
db.exit_trade(conn, twice, 105.0, 4, user=alice.id, expect_left=10.0)
try:
    db.exit_trade(conn, twice, 105.0, 4, user=alice.id, expect_left=10.0)
    check("a form sent twice does not close a second part", False)
except db.TradeChanged:
    check("a form sent twice does not close a second part",
          conn.execute("SELECT COUNT(*) FROM trade_exits WHERE trade_id=?",
                       (twice,)).fetchone()[0] == 1)
db.exit_trade(conn, twice, 106.0, None, user=alice.id, expect_left=6.0, expect_open=True)
try:
    db.exit_trade(conn, twice, 106.0, None, user=alice.id, expect_left=6.0, expect_open=True)
    check("nor a second close of a closed trade", False)
except db.TradeChanged as exc:
    check("nor a second close of a closed trade", "already closed" in str(exc))
try:
    db.exit_trade(conn, ta, 100.0, 1, user=bob.id)
    check("nobody closes someone else's trade", False)
except ValueError:
    check("nobody closes someone else's trade", True)

coin = db.open_trade(conn, "ZZC-USD", "1d", 1, 100.0, 90.0, quantity=0.3, user=alice.id)
db.exit_trade(conn, coin, 101.0, 0.1, user=alice.id)
db.exit_trade(conn, coin, 102.0, 0.2, user=alice.id)
check("parts of a coin that add up in floating point close it",
      conn.execute("SELECT status FROM trades WHERE id=?", (coin,)).fetchone()[0] == "closed")


# ---------------------------------------------------------------------------
head("THE JOURNAL'S STATISTICS WITH PARTS")
# ---------------------------------------------------------------------------
# Bob's book. A: two units, +2R on the first at t=100, -2R on the second at
# t=300, so 0R in all. B: -1R in one go at t=200. Walked trade by trade in
# the order they closed, the worst drawdown is 1R; walked exit by exit, as
# the account really moved, it is 2R.
tA = db.open_trade(conn, "ZZB1", "1d", 1, 100.0, 90.0, quantity=2, user=bob.id)
db.exit_trade(conn, tA, 120.0, 1, user=bob.id)
db.exit_trade(conn, tA, 80.0, None, user=bob.id)
tB = db.open_trade(conn, "ZZB2", "1d", 1, 100.0, 90.0, quantity=1, user=bob.id)
db.close_trade(conn, tB, 90.0, user=bob.id)
ids = [r[0] for r in conn.execute("SELECT id FROM trade_exits WHERE trade_id=? ORDER BY id",
                                  (tA,))]
conn.execute("UPDATE trade_exits SET ts=100 WHERE id=?", (ids[0],))
conn.execute("UPDATE trade_exits SET ts=300 WHERE id=?", (ids[1],))
conn.execute("UPDATE trades SET ts_closed=300 WHERE id=?", (tA,))
conn.execute("UPDATE trades SET ts_closed=200 WHERE id=?", (tB,))
conn.commit()
s = db.journal_summary(conn, user=bob.id)
check("a trade closed in parts counts once, at the R of the whole position",
      s["trades"] == 2 and (s["wins"], s["losses"], s["scratches"]) == (0, 1, 1)
      and close(s["expectancy_r"], -0.5) and close(s["total_r"], -1.0), s)
check("the drawdown walks each exit when it was taken", close(s["max_drawdown_r"], 2.0),
      s["max_drawdown_r"])
check("and the cash counts every part", close(s["pnl"], 20 - 20 - 10), s["pnl"])
net = db.journal_summary(conn, user=bob.id, costs={"equity": 0.001})
check("costs are paid once per trade, on the whole position",
      close(net["expectancy_r"], -0.5 - 0.01) and close(net["pnl"], -10 - (0.2 + 0.1))
      and net["losses"] == 2, net)
# From the peak after A's first part: B's loss, then A's second part.
check("and the drawdown is after them too",
      close(net["max_drawdown_r"], 1.01 + 0.5 * (2 + 0.01)), net)

tC = db.open_trade(conn, "ZZB3", "1d", 1, 50.0, 45.0, quantity=4, user=bob.id)
db.exit_trade(conn, tC, 55.0, 1, user=bob.id)
s = db.journal_summary(conn, user=bob.id, costs={"equity": 0.001})
check("cash taken from a position still open is kept apart, after costs",
      s["open_in_part"] == 1 and close(s["open_realised"], 5.0 - 50 * 1 * 0.001)
      and s["trades"] == 2, s)


# ---------------------------------------------------------------------------
head("WHAT AN OLDER VERSION READS AND WRITES")
# ---------------------------------------------------------------------------
# The previous version's own code for these, as it was: it knows the trades
# table and nothing else.


def old_close(conn, trade_id, exit_price):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    direction, entry, stop = int(row["direction"]), float(row["entry"]), float(row["stop"])
    move = (exit_price - entry) * direction
    conn.execute("UPDATE trades SET status='closed', ts_closed=?, exit_price=?, exit_reason=?, "
                 "r_multiple=?, pnl=? WHERE id=?",
                 (int(time.time()), exit_price, "manual", move / abs(entry - stop),
                  move * float(row["quantity"] or 0.0), trade_id))
    conn.commit()


def old_figures(conn, trade_id):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return row["quantity"], row["exit_price"], row["r_multiple"], row["pnl"]


qty, price, r, pnl = old_figures(conn, ta)
check("an older version reads a trade closed in parts as one trade at the average price",
      qty == 60 and close(price, 108.3333333333, 1e-6) and close(r, 0.8333333333, 1e-6)
      and close(pnl, 500), (qty, price, r, pnl))
check("and an open one closed in part at its full size, as if no part were closed",
      old_figures(conn, tC)[0] == 4)

tD = db.open_trade(conn, "ZZB4", "1d", 1, 100.0, 90.0, quantity=10, user=bob.id)
db.exit_trade(conn, tD, 110.0, 4, user=bob.id)
old_close(conn, tD, 120.0)              # closed by an older version after a rollback
row = conn.execute("SELECT * FROM trades WHERE id=?", (tD,)).fetchone()
back = db.trade_parts(row, db.exits_for(conn, [tD])[tD])
check("a position an older version closed after a part is read right again: "
      "the rest went at its price", close(back["r"], (4 * 1.0 + 6 * 2.0) / 10)
      and close(back["pnl"], 4 * 10 + 6 * 20) and len(back["exits"]) == 2, back)
s = db.journal_summary(conn, user=bob.id)
check("and the statistics count it so", close(s["total_r"], -1.0 + 1.6), s["total_r"])

conn.execute("DELETE FROM trades WHERE id=?", (tA,))    # an older version deleting it
conn.commit()
check("an older version can still delete a trade closed in parts, and its parts go with it",
      conn.execute("SELECT COUNT(*) FROM trade_exits WHERE trade_id=?", (tA,)).fetchone()[0] == 0)
gone = db.delete_trade(conn, tD, user=bob.id)
check("and this one deletes the parts first",
      gone["id"] == tD and not db.exits_for(conn, [tD]))


# ---------------------------------------------------------------------------
head("A FILE FROM BEFORE PARTS AND TRAILS")
# ---------------------------------------------------------------------------
old = _os.path.join(_os.path.dirname(DB), "before.db")
db.connect(old).close()
raw = sqlite3.connect(old)
raw.execute("DROP TABLE trade_exits")
for column in ("trail_pct", "trail_dist", "trail_atr", "trail_ts", "trail_best",
               "trail_best_ts"):
    raw.execute("ALTER TABLE trades DROP COLUMN %s" % column)
raw.execute("INSERT INTO trades (account, symbol, interval, direction, status, ts_opened, "
            "entry, stop, quantity, user_id) VALUES ('paper','OLD','1d',1,'open',1,10,9,5,1)")
raw.commit()
raw.close()
db.migrate(old)
db.migrate(old)
moved = db.connect(old)
cols = {r[1] for r in moved.execute("PRAGMA table_info(trades)")}
check("bringing it up to date adds the parts table and the trail columns, twice safely",
      {"trail_pct", "trail_best", "trail_best_ts", "trail_hit_ts"} <= cols
      and moved.execute("SELECT COUNT(*) FROM trade_exits").fetchone()[0] == 0)
check("and its trades read as before", db.open_positions(moved)[0]["quantity"] == 5)
moved.close()
bare = sqlite3.connect(old)
bare.row_factory = sqlite3.Row
bare.execute("DROP TABLE trade_exits")
check("a copy read without being brought up to date (a backup's export) has no parts",
      db.exits_for(bare, [1]) == {} and len(export.exits(bare)) == 0)
bare.close()


# ---------------------------------------------------------------------------
head("DELETING AN ACCOUNT, AND THE EXPORT")
# ---------------------------------------------------------------------------
carol = A.upsert_email_user(conn, "carol@example.com")
tE = db.open_trade(conn, "ZZE", "1d", 1, 100.0, 90.0, quantity=3, user=carol.id)
db.exit_trade(conn, tE, 104.0, 1, user=carol.id)
removed = A.delete_user(conn, carol.id)
check("deleting an account takes the parts of its trades, before the trades",
      removed.get("trade_exits") == 1 and removed.get("trades") == 1
      and not db.exits_for(conn, [tE]), removed)

rows = {r["id"]: r for r in csv.DictReader(io.StringIO(
    export.build(conn, "trades", "csv", user=alice.id)["body"]))}
mine = rows[str(ta)]
check("the trades export gives a trade closed in parts its whole figures",
      close(mine["quantity"], 60) and close(mine["quantity_open"], 0) and mine["exits"] == "3"
      and close(mine["r_multiple"], 0.8333333333, 1e-6) and close(mine["pnl"], 500)
      and close(mine["exit_price"], 108.3333333333, 1e-6), mine)
lines = list(csv.DictReader(io.StringIO(export.build(conn, "exits", "csv",
                                                     user=alice.id)["body"])))
of_a = [x for x in lines if x["trade_id"] == str(ta)]
check("the exits export lists each part with its own price",
      [float(x["price"]) for x in of_a] == [110.0, 120.0, 95.0]
      and all(x["part"] == "yes" for x in of_a), of_a)
check("an exit written without its split leaves the shares of its day unsaid",
      all(x.get("split") == "" and x.get("quantity_traded") == ""
          and x.get("price_traded") == "" for x in of_a), of_a)
check("and a trade closed in one go as one exit",
      [x["part"] for x in lines if x["trade_id"] == str(loose)] == ["no"])
check("nobody else's exits are in it", all(x["symbol"].startswith("ZZ") for x in lines)
      and not any(x["symbol"] in ("ZZB1", "ZZB3") for x in lines))


# ---------------------------------------------------------------------------
head("THE PORTFOLIO HOLDS WHAT IS LEFT, AT THE STOP IN FORCE")
# ---------------------------------------------------------------------------
BARS = {}
SPLITS = {}


def fake_bars(symbol, interval="1d", lookback=None):
    found = BARS.get(symbol)
    if isinstance(found, dict):                 # bars for each bar size
        found = found.get(interval)
    if found is None:
        raise market_mod.DataError("no instrument found for %r" % symbol)
    return found(lookback) if callable(found) else found


def offline(*_a, **_k):
    raise RuntimeError("offline")


real = (market_mod.fetch_bars, market_mod.split_history, P.fetch_bars, P.fund.load,
        P.risk_mod.profile, P.risk_mod.correlation_matrix, alerts.fetch_bars,
        paper.fetch_bars)
market_mod.fetch_bars = P.fetch_bars = alerts.fetch_bars = fake_bars
paper.fetch_bars = lambda symbol, interval="1d": fake_bars(symbol, interval)
market_mod.split_history = lambda symbol: list(SPLITS.get(symbol, []))
P.fund.load = P.risk_mod.profile = offline
P.risk_mod.correlation_matrix = lambda *_a, **_k: {}
try:
    # Bob's ZZB3: four units from 50, stop 45, one closed; trailing 5%.
    db.set_trail(conn, tC, pct=0.05, user=bob.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=? WHERE id=?", (OPENED, OPENED, tC))
    conn.commit()
    BARS["ZZB3"] = bars_of([52, 56, 60], [50, 53, 58], live=58)
    view = P.build(db.open_positions(conn, user=bob.id), account=10000,
                   with_correlation=False, cost_bps={"equity": 0.0})
    pos = [q for q in view.positions if q.symbol == "ZZB3"][0]
    check("only what is left is held: its size, value and open P&L",
          close(pos.quantity, 3) and close(pos.notional, 58 * 3) and close(pos.open_pnl, 8 * 3),
          (pos.quantity, pos.notional, pos.open_pnl))
    check("still at risk is from today's price to the trailed stop",
          close(pos.stop, 57.0) and close(pos.risk_remaining, (58 - 57.0) * 3), pos)
    check("while the planned risk and R stay measured from the stop it opened with",
          close(pos.planned_risk, 5 * 3) and close(pos.open_r, 8 / 5.0), pos)


    # -----------------------------------------------------------------------
    head("THE FOUR READERS AGREE")
    # -----------------------------------------------------------------------
    NOW = int(time.time())
    start = NOW - 9 * DAY

    def four(symbol):
        """The stop and whether it was hit, as each of the four reads one trade."""
        row = conn.execute("SELECT * FROM trades WHERE symbol=?", (symbol,)).fetchone()
        mark = web._mark_one(row)
        pos = P.build([dict(row)], account=10000, with_correlation=False).positions[0]
        out = io.StringIO()
        paper.console = Console(file=out, width=220, color_system=None)
        paper.cmd_check(NS(account="paper"), conn)
        said = [line for line in out.getvalue().splitlines() if symbol in line.split()]
        told = alerts.check_stops(conn)
        told = [f for f in told["fired"] if f.trade_id == row["id"]]
        return mark, pos, (said[0] if said else ""), told

    tf = db.open_trade(conn, "ZZF", "1d", 1, 100.0, 95.0, quantity=10, trail_pct=0.05,
                       user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, tf))
    tg = db.open_trade(conn, "ZZG", "1d", 1, 100.0, 95.0, quantity=10, trail_pct=0.05,
                       user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, tg))
    conn.commit()
    BARS["ZZF"] = bars_of([102, 106, 110, 108, 109], [100, 103, 106, 104.6, 104.4], live=105,
                          start=start)
    BARS["ZZG"] = bars_of([102, 106, 110, 108, 109], [100, 103, 106, 104.6, 104.7], live=105,
                          start=start)
    # ZZF first: the notice checks every open position each time, and tells
    # a crossing once.
    mark, pos, said, told = four("ZZF")
    check("a trail hit: all four say so, at the trailed stop",
          mark["status"] == "past stop" and close(mark["touched_stop"], 104.5)
          and pos.breached and close(pos.touched_stop, 104.5) and "STOP HIT" in said
          and len(told) == 1 and "the trailing stop of 104.5" in told[0].message,
          (mark.get("status"), pos.breached, said, [f.message for f in told]))
    from bot import workflows  # noqa: E402
    routine = workflows.RunReport(name="positions", started=0, finished=0)
    workflows._check_positions(conn, web.config_mod.load(harness.CFG_PATH), routine,
                               lambda _label: None, user=alice.id)
    named = [s.detail for s in routine.steps if s.label == "Position: ZZF"]
    check("and a routine names the trailing stop at the level it was crossed",
          named and "has crossed its trailing stop of 104.5" in named[0], named)
    mark, pos, said, told = four("ZZG")
    check("a trail not hit: the journal, the portfolio, paper.py and the notice agree",
          close(mark["stop"], 104.5) and mark["status"] == "open" and close(pos.stop, 104.5)
          and not pos.breached and "104.5" in said and "STOP" not in said and not told,
          (mark.get("stop"), mark.get("status"), pos.stop, said, told))

    # The background check reads a few days of bars. A best kept by a longer
    # look holds its stop where the journal has it.
    th = db.open_trade(conn, "ZZH", "1d", 1, 100.0, 95.0, quantity=10, trail_pct=0.05,
                       user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, th))
    conn.commit()
    full = bars_of([102, 106, 110, 108, 109], [100, 103, 106, 104.6, 104.4], live=105,
                   start=start)
    recent = bars_of([108, 109], [104.6, 104.4], live=105, start=start + 3 * DAY)
    BARS["ZZH"] = lambda lookback: recent if lookback else full
    mark = web._mark_one(conn.execute("SELECT * FROM trades WHERE id=?", (th,)).fetchone())
    web._keep_trail_bests(conn, [conn.execute("SELECT * FROM trades WHERE id=?",
                                              (th,)).fetchone()], {th: mark})
    kept = conn.execute("SELECT trail_best, trail_best_ts FROM trades WHERE id=?",
                        (th,)).fetchone()
    check("the journal keeps the best price it read", close(kept[0], 110.0)
          and kept[1] == start + 2 * DAY, tuple(kept))
    told = [f for f in alerts.check_stops(conn)["fired"] if f.trade_id == th]
    check("so the notice, whose bars never saw that high, still trails from it",
          len(told) == 1 and "104.5" in told[0].message, [f.message for f in told])

    # Seen live: today's own high trails the stop up to 114, then the price
    # falls through it before the day is out. Once today's bar is finished,
    # its high and low do not say which came first, so it is held to the
    # stop from before it, 95, and shows no crossing. The one a reader saw
    # is kept on the trade, so the stop stays hit for all four.
    today = NOW - 6 * 3600                      # today's bar started six hours ago
    days = bars_of([99, 99, 99, 99, 99, 101], [97, 97, 97, 97, 97, 99], live=120,
                   start=today - 6 * DAY)
    done = bars_of([99, 99, 99, 99, 99, 101, 120], [97, 97, 97, 97, 97, 99, 104],
                   live=116, start=today - 6 * DAY)

    def opened_yesterday(symbol):
        tid = db.open_trade(conn, symbol, "1d", 1, 100.0, 90.0, quantity=60,
                            trail_pct=0.05, user=alice.id)
        conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                     (today - DAY + 3600, today - DAY + 3600, today - DAY + 3600, tid))
        conn.commit()
        return tid

    tj = opened_yesterday("ZZJ")
    BARS["ZZJ"] = days
    mark, pos, said, told = four("ZZJ")
    check("a new high, live, trails the stop up at once",
          close(mark["stop"], 114.0) and mark["status"] == "open" and not pos.breached
          and not told, (mark.get("stop"), mark.get("status"), said))
    BARS["ZZJ"] = NS(**dict(vars(days), last_price=113.0))
    mark, pos, said, told = four("ZZJ")
    check("and a fall through it, live, is a hit for all four",
          mark["status"] == "past stop" and pos.breached and "PAST STOP" in said
          and len(told) == 1 and "is at 113, past the trailing stop of 114" in told[0].message,
          (mark.get("status"), pos.breached, said, [f.message for f in told]))
    BARS["ZZJ"] = done
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tj,)).fetchone()
    check("today's bar, finished, would not show that crossing on its own",
          read_stop(done, 1, 100.0, 90.0, int(row["ts_opened"]),
                    trail=Trail(pct=0.05, since=int(row["trail_ts"]), best=120.0,
                                best_at=int(row["trail_best_ts"]))).touched_at is None)
    mark, pos, said, told = four("ZZJ")
    check("but the crossing seen is kept: all four still count the stop as hit",
          mark["status"] == "past stop" and close(mark["touched"], 113.0)
          and close(mark["touched_stop"], 114.0) and pos.breached
          and close(pos.touched_stop, 114.0) and "STOP HIT" in said and not told,
          (mark.get("status"), mark.get("touched"), pos.breached, said))
    _trail, before, after = P.trail_from(row, done, 1.0, pct=0.04)
    check("and no new trail is set on it",
          "already past its stop" in (trail_problem("ZZJ", 1, before, after) or ""))

    # The journal keeps a crossing it saw too: the notice, which had not
    # looked at that moment, tells it once the day is done.
    tj2 = opened_yesterday("ZZJ2")
    for live in (120.0, 113.0):
        BARS["ZZJ2"] = NS(**dict(vars(days), last_price=live))
        row = conn.execute("SELECT * FROM trades WHERE id=?", (tj2,)).fetchone()
        web._keep_trail_bests(conn, [row], {tj2: web._mark_one(row)})
    BARS["ZZJ2"] = done
    told = [f for f in alerts.check_stops(conn)["fired"] if f.trade_id == tj2]
    check("a crossing the journal saw is told by the notice once the day is done",
          len(told) == 1 and "traded at 113 on" in told[0].message
          and "past the trailing stop of 114" in told[0].message
          and "is at 116 now" in told[0].message, [f.message for f in told])

    # An hourly trade is read on its own hourly bars by all four. Yesterday's
    # high at 10:30 trailed the stop up to 106.7, and the 14:30 bar's low of
    # 106 crossed it before the price came back to 109. On daily bars the
    # whole day is held to the stop from before it, 97, and shows nothing.
    hour = 3600
    d2 = NOW - 2 * hour - DAY                   # yesterday's session
    d1 = d2 - DAY
    tk = db.open_trade(conn, "ZZK", "1h", 1, 100.0, 95.0, quantity=10, trail_pct=0.03,
                       user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (d1 + 1800, d1 + 1800, d1 + 1800, tk))
    conn.commit()
    climb = [(105, 100), (110, 104), (109, 107.5), (109, 107), (108.5, 107), (108, 106),
             (109.5, 107.8)]
    BARS["ZZK"] = {
        "1h": bars_at([(d1 + k * hour, 99.8, 98.5) for k in range(7)]
                      + [(d2 + k * hour, hi, lo) for k, (hi, lo) in enumerate(climb)]
                      + [(d2 + DAY, 109.6, 108.8)], live=109, interval="1h"),
        "1d": bars_of([99, 99, 99, 99, 99, 99.8, 110], [97, 97, 97, 97, 97, 98.5, 100],
                      live=109, start=d1 - 5 * DAY)}
    mark, pos, said, told = four("ZZK")
    check("an hourly trade: all four read its own bars, and agree its trail was crossed",
          mark["status"] == "past stop" and close(mark["touched"], 106.0)
          and close(mark["touched_stop"], 106.7) and pos.breached
          and close(pos.touched, 106.0) and close(pos.touched_stop, 106.7)
          and "STOP HIT" in said and len(told) == 1
          and "traded at 106 on" in told[0].message
          and "past the trailing stop of 106.7" in told[0].message,
          (mark.get("status"), pos.breached, pos.touched, said, [f.message for f in told]))
    routine = workflows.RunReport(name="positions", started=0, finished=0)
    workflows._check_positions(conn, web.config_mod.load(harness.CFG_PATH), routine,
                               lambda _label: None, user=alice.id)
    named = [s.detail for s in routine.steps if s.label == "Position: ZZK"]
    check("and the positions routine names it at the level it was crossed",
          named and "has crossed its trailing stop of 106.7" in named[0], named)

    # One logged with no bar size is read on daily bars by all four.
    tn = db.open_trade(conn, "ZZN", None, 1, 100.0, 95.0, quantity=10, trail_pct=0.05,
                       user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, tn))
    conn.commit()
    BARS["ZZN"] = {"1d": bars_of([102, 106, 110, 108, 109], [100, 103, 106, 104.6, 104.4],
                                 live=105, start=start)}
    mark, pos, said, told = four("ZZN")
    check("and one logged with no bar size is read on daily bars by all four",
          mark["status"] == "past stop" and close(mark["touched_stop"], 104.5)
          and pos.breached and "STOP HIT" in said and len(told) == 1,
          (mark.get("status"), pos.breached, said, [f.message for f in told]))

    # A fixed stop on a five-minute trade, traded through once and come back.
    # Five-minute bars reach back sixty days, so in time the touch drops out
    # of every reader's bars. The first reader to see it keeps it, as it does
    # a trailing stop's, so it stays a crossing for all four after that.
    five = 300
    dip = NOW - 2 * DAY
    tq5 = db.open_trade(conn, "ZZQ5", "5m", 1, 100.0, 95.0, quantity=10, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (dip - 3 * five, tq5))
    conn.commit()
    BARS["ZZQ5"] = {"5m": bars_at([(dip - 2 * five, 101.0, 99.0), (dip - five, 101.0, 99.0),
                                   (dip, 99.5, 94.0), (dip + five, 102.0, 99.0)],
                                  live=101, interval="5m")}
    mark, pos, said, told = four("ZZQ5")
    check("a fixed stop crossed inside a five-minute trade: all four say so",
          mark["status"] == "past stop" and close(mark["touched"], 94.0) and pos.breached
          and "STOP HIT" in said and len(told) == 1 and "traded at 94 on" in told[0].message,
          (mark.get("status"), pos.breached, said, [f.message for f in told]))
    kept = conn.execute("SELECT trail_hit_ts, trail_hit_price, trail_hit_stop FROM trades "
                        "WHERE id=?", (tq5,)).fetchone()
    check("and its crossing is kept, as a trailing stop's is",
          tuple(kept) == (dip, 94.0, 95.0), tuple(kept))
    BARS["ZZQ5"] = {"5m": bars_at([(NOW - DAY + k * five, 102.0, 99.0) for k in range(4)],
                                  live=101, interval="5m")}
    mark, pos, said, told = four("ZZQ5")
    check("once the touch is gone from its bars, all four still count the stop as hit",
          mark["status"] == "past stop" and close(mark["touched"], 94.0)
          and close(mark["touched_stop"], 95.0) and pos.breached and close(pos.touched, 94.0)
          and close(pos.touched_stop, 95.0) and "STOP HIT" in said and not told,
          (mark.get("status"), mark.get("touched"), pos.breached, pos.touched, said))
    routine = workflows.RunReport(name="positions", started=0, finished=0)
    workflows._check_positions(conn, web.config_mod.load(harness.CFG_PATH), routine,
                               lambda _label: None, user=alice.id)
    named = [s.detail for s in routine.steps if s.label == "Position: ZZQ5"]
    check("and so does the positions routine",
          named and "has crossed its stop of 95" in named[0], named)
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tq5,)).fetchone()
    _trail, before, after = P.trail_from(row, BARS["ZZQ5"]["5m"], 1.0, pct=0.03)
    check("and no trail is set on it",
          "already past its stop" in (trail_problem("ZZQ5", 1, before, after) or ""))


    # -----------------------------------------------------------------------
    head("PAPER.PY: PARTS, TRAILS AND TODAY'S SHARES")
    # -----------------------------------------------------------------------
    def cli(*argv):
        out = io.StringIO()
        paper.console = Console(file=out, width=200, color_system=None)
        code = paper.main(["--db", DB] + [str(a) for a in argv])
        return code, " ".join(out.getvalue().split())

    BARS["ZZP"] = bars_of([104], [101], live=105, start=NOW - 2 * DAY)
    code, said = cli("open", "ZZP", "--entry", 100, "--stop", 90, "--qty", 10,
                     "--interval", "1d", "--trail", 5)
    tp = conn.execute("SELECT * FROM trades WHERE symbol='ZZP'").fetchone()
    check("paper.py opens a trade that trails", code == 0 and close(tp["trail_pct"], 0.05)
          and "trails 5% behind the best price" in said, said)
    code, said = cli("close", tp["id"], "--price", 110, "--qty", 4)
    check("and closes part of it", code == 0 and "4 closed at 110, 6 still open" in said, said)
    code, said = cli("list")
    check("its list shows what is left", "6 of 10" in said, said)
    code, said = cli("trail", tp["id"], "--pct", 20)
    check("a wider trail is refused there too", code == 2 and "only tightens" in said, said)
    code, said = cli("trail", tp["id"], "--pct", 3)
    check("a narrower one is set", code == 0 and close(conn.execute(
        "SELECT trail_pct FROM trades WHERE id=?", (tp["id"],)).fetchone()[0], 0.03), said)
    code, said = cli("stats")
    check("its statistics keep the cash taken from an open position apart",
          "taken by closing part of" in said, said)

    tz = db.open_trade(conn, "ZZQ2", "1d", 1, 1000.0, 950.0, quantity=2)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (NOW - 30 * DAY, tz))
    conn.commit()
    SPLITS["ZZQ2"] = [(NOW - 10 * DAY, 10.0)]
    BARS["ZZQ2"] = bars_of([102] * 15, [99] * 15, live=101, start=NOW - 20 * DAY,
                           splits=[(NOW - 10 * DAY, 10.0)])
    code, said = cli("close", tz, "--price", 101, "--qty", 10, "--today")
    part = db.exits_for(conn, [tz])[tz]
    check("close --today turns today's shares into the ones opened with",
          code == 0 and close(part[0]["price"], 1010.0) and close(part[0]["quantity"], 1.0)
          and "101 in today's shares is 1,010 in the shares you opened with" in said, said)
    code, said = cli("close", tz, "--price", 1020)
    check("and the last part closes it, saying so", code == 0
          and "closed in 2 parts at 1,015 on average" in said, said)
    sold = [x for x in csv.DictReader(io.StringIO(export.build(conn, "exits", "csv",
                                                               user=1)["body"]))
            if x["symbol"] == "ZZQ2"]
    check("paper.py keeps the split with each exit, so the export gives both in today's shares",
          [(x.get("split"), x.get("quantity_traded"), x.get("price_traded")) for x in sold]
          == [("10.0", "10.0", "101.0"), ("10.0", "10.0", "102.0")], sold)

    # -----------------------------------------------------------------------
    head("THE JOURNAL PAGE: CLOSING IN PART, TRAILING, A SPLIT")
    # -----------------------------------------------------------------------
    def sign_in(client, user):
        k = db.connect(DB)
        try:
            token = A.start_session(k, user.id, "test")
        finally:
            k.close()
        with client.session_transaction() as sess:
            sess[web.SESSION_KEY] = token

    sign_in(c, alice)
    r = c.post("/trades/open", data={"symbol": "ZZT", "direction": "long", "entry": "100",
                                     "stop": "95", "quantity": "60", "interval": "1d",
                                     "trail": "5", "trail_kind": "pct"})
    tt = conn.execute("SELECT * FROM trades WHERE symbol='ZZT'").fetchone()
    check("a trade logged by hand can trail from the start", r.status_code == 302
          and close(tt["trail_pct"], 0.05) and tt["trail_ts"] == tt["ts_opened"]
          and close(tt["trail_best"], 100.0), dict(tt) if tt else r.status_code)
    conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                 (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, tt["id"]))
    conn.commit()
    BARS["ZZT"] = bars_of([102, 106, 110], [100, 103, 106], live=108, start=start)
    body = text(c.get("/trades"))
    cell = flat(body[body.index("<b>ZZT</b>"):])
    cell = cell[:cell.index("</tr>")]
    check("the journal shows the stop in force, and how it moved",
          "104.5" in cell and "5% under 110" in cell and "up from 95" in cell, cell[:900])
    check("its close form takes a size, empty for all of it, and says what it saw open",
          'name="quantity"' in cell and 'placeholder="all"' in cell
          and 'name="left" value="60.0"' in cell)
    check("and a position not past its stop can have its trail tightened",
          "Tighten the trail" in cell and 'action="/trades/trail"' in cell)
    check("the best price it read is kept",
          close(conn.execute("SELECT trail_best FROM trades WHERE id=?",
                             (tt["id"],)).fetchone()[0], 110.0))

    r = c.post("/trades/close", data={"trade_id": tt["id"], "price": "108", "quantity": "20",
                                      "reason": "target", "left": "60.0"})
    check("closing 20 of 60 goes back to the journal", r.status_code == 302
          and r.headers["Location"].endswith("/trades"), r.headers.get("Location"))
    body = flat(text(c.get(r.headers["Location"])))
    check("which says what was recorded",
          "Recorded: 20 of ZZT closed at 108. 40 of it is still open." in body)
    check("once", "Recorded:" not in text(c.get("/trades")))
    check("and only to the person who closed it: a link cannot put it there",
          "Recorded:" not in text(c.get("/trades?exited=%d&said=Recorded" % tt["id"])))
    check("and shows what is left", "40 of 60 still open" in body
          and "20 closed at 108, +160.00" in body, body[body.index("<b>ZZT</b>"):][:600])
    r = c.post("/trades/close", data={"trade_id": tt["id"], "price": "108", "quantity": "20",
                                      "reason": "target", "left": "60.0"})
    check("pressing Close twice closes one part, and says why nothing more was",
          "changed=%d" % tt["id"] in r.headers.get("Location", "")
          and len(db.exits_for(conn, [tt["id"]])[tt["id"]]) == 1
          and "had changed since the page was drawn" in flat(text(c.get(r.headers["Location"]))))
    r = c.post("/trades/close", data={"trade_id": tt["id"], "price": "abc", "left": "40.0"})
    page = text(r)
    check("a price that is not a number is refused in words",
          r.status_code == 400 and "The exit price must be a number." in page
          and "could not convert" not in page)

    r = c.post("/trades/trail", data={"trade_id": tt["id"], "trail": "10", "trail_kind": "pct"})
    check("a wider trail is refused: the stop only tightens",
          r.status_code == 400 and "only tightens" in text(r))
    r = c.post("/trades/trail", data={"trade_id": tt["id"], "trail": "1", "trail_kind": "pct"})
    check("one that would stop it at once is refused",
          r.status_code == 400 and "past today&#39;s price" in text(r))
    r = c.post("/trades/trail", data={"trade_id": tt["id"], "trail": "3", "trail_kind": "pct"})
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tt["id"],)).fetchone()
    check("a narrower one is set", r.status_code == 302 and close(row["trail_pct"], 0.03)
          and row["trail_ts"] >= NOW, dict(row))
    sign_in(c, bob)
    r = c.post("/trades/trail", data={"trade_id": tt["id"], "trail": "2", "trail_kind": "pct"})
    check("nobody trails someone else's trade", r.status_code == 400
          and "no such trade in your journal" in text(r))
    sign_in(c, alice)

    # Measured on the trade's own bars: a range of 2 every day is an ATR of 2.
    ta2 = db.open_trade(conn, "ZZV", "1d", 1, 100.0, 90.0, quantity=5, user=alice.id)
    BARS["ZZV"] = bars_of([101] * 40, [99] * 40, live=100, start=NOW - 41 * DAY,
                          closes=[100] * 40)
    r = c.post("/trades/trail", data={"trade_id": ta2, "trail": "2", "trail_kind": "atr"})
    row = conn.execute("SELECT * FROM trades WHERE id=?", (ta2,)).fetchone()
    check("an ATR trail is measured on the trade's own bars and kept as a distance",
          r.status_code == 302 and close(row["trail_dist"], 4.0, 1e-6)
          and close(row["trail_atr"], 2.0) and row["trail_pct"] is None, dict(row))

    # A ten-for-one split after opening. Bought 2 at 1,000; today that is 20
    # shares quoted at 101.
    split_at = NOW - 10 * DAY
    ts_ = db.open_trade(conn, "ZZS", "1d", 1, 1000.0, 950.0, quantity=2, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (NOW - 30 * DAY, ts_))
    conn.commit()
    SPLITS["ZZS"] = [(split_at, 10.0)]
    BARS["ZZS"] = bars_of([102] * 15, [99] * 15, live=101, start=NOW - 20 * DAY,
                          splits=[(split_at, 10.0)])
    body = text(c.get("/trades"))
    cell = flat(body[body.index("<b>ZZS</b>"):])
    cell = cell[:cell.index("</tr>")]
    check("after a split the close form is filled in today's shares, and says so",
          'name="price" step="any" required placeholder="price" value="101.0"' in cell
          and '<option value="today" selected>today\'s shares</option>' in cell
          and '<option value="opened">shares opened with</option>' in cell, cell[:1500])
    check("with the split shown beside the price", "quoted 101 today, after a 10-for-1 split"
          in cell)
    check("and the page says how a typed price is read",
          "say which shares the price you type is in" in flat(body))
    r = c.post("/trades/close", data={"trade_id": ts_, "price": "101", "quantity": "10",
                                      "shares": "today", "reason": "manual", "left": "2.0"})
    part = db.exits_for(conn, [ts_])[ts_]
    check("10 of today's shares at 101 are 1 of the shares opened with, at 1,010",
          r.status_code == 302 and len(part) == 1 and close(part[0]["price"], 1010.0)
          and close(part[0]["quantity"], 1.0), [dict(x) for x in part])
    check("and the journal says so, both ways, so the conversion can be checked",
          "Recorded: 10 of today's ZZS shares at 101, which is 1 at 1,010 in the shares "
          "you opened with. 1 of those is still open."
          in html.unescape(flat(text(c.get("/trades")))))
    r = c.post("/trades/close", data={"trade_id": ts_, "price": "1020", "shares": "opened",
                                      "reason": "target", "left": "1.0"})
    row = conn.execute("SELECT * FROM trades WHERE id=?", (ts_,)).fetchone()
    check("and the rest, typed in the shares opened with, is taken as typed",
          row["status"] == "closed" and close(row["exit_price"], 1015.0)
          and close(row["r_multiple"], (10 + 20) / 2 / 50.0) and close(row["pnl"], 30.0),
          dict(row))
    check("the journal then says it closed in two parts",
          "Closed ZZS in 2 parts at 1,015 on average in the shares you opened with: "
          "+0.30R on the whole position." in flat(text(c.get(r.headers["Location"]))))

    # A split that cannot be checked just now: nothing is written.
    def no_answer(symbol):
        raise market_mod.DataUnavailable("the source did not answer")

    tq = db.open_trade(conn, "ZZQ", "1d", 1, 1000.0, 950.0, quantity=2, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (NOW - 30 * DAY, tq))
    conn.commit()
    BARS["ZZQ"] = bars_of([102] * 15, [99] * 15, live=101, start=NOW - 20 * DAY)
    market_mod.split_history = no_answer
    r = c.post("/trades/close", data={"trade_id": tq, "price": "101", "shares": "today",
                                      "left": "2.0"})
    page = text(r)
    market_mod.split_history = lambda symbol: list(SPLITS.get(symbol, []))
    check("when the split cannot be checked, a price in today's shares waits, in words",
          r.status_code == 503 and r.headers.get("Retry-After")
          and "Could not check ZZQ for a share split just now" in page
          and "did not answer" not in page and not db.exits_for(conn, [tq]), r.status_code)

    # The review before a paper trade offers the trail too, as part of Confirm.
    run = conn.execute("INSERT INTO runs (ts, symbol, name, asset_class, interval, price, "
                       "action, entry, stop, target1, user_id, source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (NOW, "ZZR", "ZZR Corp", "equity", "1d", 100.0, "BUY", 100.0, 95.0,
                        110.0, alice.id, "web")).lastrowid
    conn.commit()
    body = text(c.get("/trades/simulate?run_id=%d" % run))
    check("the review offers a trailing stop, in the Confirm form",
          'form="take"' in body and 'name="trail"' in body and 'id="take"' in body)
    c.post("/trades/take", data={"run_id": run, "trail": "4", "trail_kind": "pct"})
    row = conn.execute("SELECT * FROM trades WHERE run_id=?", (run,)).fetchone()
    check("and a trade confirmed with one trails from the start",
          row is not None and close(row["trail_pct"], 0.04), dict(row) if row else None)

    # The front page names a trailed stop that was crossed.
    BARS["ZZF"] = bars_of([102, 106, 110, 108, 109], [100, 103, 106, 104.6, 104.4], live=105,
                          start=start)
    body = flat(text(c.get("/")))
    check("the front page names a crossed trailing stop at the level it was crossed",
          "<b>ZZF</b> long: traded through its trailing stop of 104.5 at 104.4" in body,
          body[body.find("ZZF") - 100:][:400])
    check("and so does it for an hourly trade, and for a crossing kept from when it was seen",
          "<b>ZZK</b> long: traded through its trailing stop of 106.7 at 106" in body
          and "<b>ZZJ</b> long: traded through its trailing stop of 114 at 113" in body,
          [body[body.find(s) - 20:][:200] for s in ("<b>ZZK</b>", "<b>ZZJ</b>")])
    page = flat(text(c.get("/portfolio")))
    check("as does the portfolio page",
          "Stop breached on ZZK: it traded through its trailing stop of 106.7" in page
          and "Stop breached on ZZJ: it traded through its trailing stop of 114" in page)
    check("both of them a fixed stop crossed in a five-minute trade, from what was kept",
          "<b>ZZQ5</b> long: traded through its stop of 95 at 94" in body
          and "Stop breached on ZZQ5: it traded through its stop of 95" in page,
          [body[body.find("<b>ZZQ5</b>") - 20:][:200]])

    # Its table of open positions shows the stop in force, which a trail
    # moves, and what that stop risks from the entry on what is still held.
    def front_row(symbol):
        """The cells of a position's row in the front page's Open positions table."""
        page = flat(text(c.get("/")))
        at = page.rindex("<b>%s</b>" % symbol)
        row = page[page.rindex("<tr", 0, at):page.index("</tr>", at)]
        return [re.sub("<[^>]+>", "", cell).strip()
                for cell in re.findall(r"<td[^>]*>(.*?)</td>", row)]

    def trailed_then_part_closed(symbol, stop, risk, highs, lows, live):
        tid = db.open_trade(conn, symbol, "1d", 1, 100.0, stop, quantity=60,
                            risk_amount=risk, trail_pct=0.05, user=alice.id)
        conn.execute("UPDATE trades SET ts_opened=?, trail_ts=?, trail_best_ts=? WHERE id=?",
                     (start - DAY + 3600, start - DAY + 3600, start - DAY + 3600, tid))
        conn.commit()
        BARS[symbol] = bars_of(highs, lows, live=live, start=start)
        db.exit_trade(conn, tid, live, 40, user=alice.id)

    trailed_then_part_closed("ZZM", 90.0, 600.0, [102, 104], [100, 101], live=103)
    cells = front_row("ZZM")
    check("the front page's open positions show the trailed stop, and the risk on what is left",
          cells[:4] == ["ZZM", "Long", "100", "98.8"] and cells[5] == "24.00", cells)
    trailed_then_part_closed("ZZM2", 95.0, 300.0, [110, 120, 130, 129], [104, 114, 124, 126],
                             live=128)
    cells = front_row("ZZM2")
    check("and nothing at risk once the trail has carried the stop past the entry",
          cells[3] == "123.5" and cells[5] == "0.00", cells)
    page = text(c.get("/portfolio"))
    check("and so does the portfolio", "traded through its trailing stop of" in flat(page)
          and "ZZF" in page)
    r = c.get("/export/exits.csv")
    check("the exits can be downloaded", r.status_code == 200 and "ZZS" in text(r)
          and "1010" in text(r))

    # The journal keeps a trade in the shares it was opened with, and a
    # broker's record of a sale after a split is in that day's shares. The
    # split is kept with each exit, so the export gives both.
    t3 = db.open_trade(conn, "ZZS3", "1d", 1, 1000.0, 900.0, quantity=10, user=alice.id)
    conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (NOW - 30 * DAY, t3))
    conn.commit()
    SPLITS["ZZS3"] = [(split_at, 10.0)]
    BARS["ZZS3"] = bars_of([122] * 15, [118] * 15, live=120, start=NOW - 20 * DAY,
                           splits=[(split_at, 10.0)])
    c.post("/trades/close", data={"trade_id": t3, "price": "120", "quantity": "100",
                                  "shares": "today", "reason": "manual", "left": "10.0"})
    sold = {}
    for x in csv.DictReader(io.StringIO(text(c.get("/export/exits.csv")))):
        sold.setdefault(x["symbol"], []).append(
            (x["quantity"], x["price"], x.get("split"), x.get("quantity_traded"),
             x.get("price_traded")))
    check("after a split each sale is also given in the shares of its day, as a broker "
          "lists it: typed in today's shares, or in the shares opened with",
          sold.get("ZZS") == [("1.0", "1010.0", "10.0", "10.0", "101.0"),
                              ("1.0", "1020.0", "10.0", "10.0", "102.0")], sold.get("ZZS"))
    check("and a trade closed in one go after a split as well",
          sold.get("ZZS3") == [("10.0", "1200.0", "10.0", "100.0", "120.0")], sold.get("ZZS3"))
    check("one with no split since opening is the same in both",
          sold.get("ZZT") == [("20.0", "108.0", "1.0", "20.0", "108.0")], sold.get("ZZT"))
finally:
    (market_mod.fetch_bars, market_mod.split_history, P.fetch_bars, P.fund.load,
     P.risk_mod.profile, P.risk_mod.correlation_matrix, alerts.fetch_bars,
     paper.fetch_bars) = real
    conn.close()

print()
print("%d failure(s)" % len(fails))
print("JOURNAL EXITS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
