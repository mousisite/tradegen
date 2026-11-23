"""The public track record and the public strategy tests, on made-up data.

No network: bars are built by hand. Each section pins one way the record or
the strategy pages used to mislead, and says how.
"""
import calendar
import csv
import io
import math
import os as _os
import sqlite3
import sys as _sys
import tempfile
import time
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

import numpy as np  # noqa: E402

c, DB = harness.isolated_web("record")

import web  # noqa: E402
from bot import calibrate, config, public  # noqa: E402
from bot import database as db  # noqa: E402
from bot.learning import StrategyStat, measure_strategies  # noqa: E402
from bot.market import Bars  # noqa: E402
from bot.strategies import build_context  # noqa: E402

# Sign-in required, so every page here is fetched as a visitor would see it.
web.app.config["AUTH_MODE"] = "google"
fails = []
DAY = 86400


def text(url):
    """A page's HTML with its line breaks and indents folded to single spaces."""
    return " ".join(c.get(url).data.decode("utf-8", "replace").split())


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


def utc(y, m, d, hh=0, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0, 0, 0, 0))


def call(symbol, action, price, atr, at, price_day=None, market_open=False):
    """A snapshot as the background check makes it, with the house levels."""
    return {"symbol": symbol, "action": action, "price": price,
            "stop": price - 1.5 * atr, "target": price + 2.25 * atr,
            "horizon": 10, "at": at, "price_day": price_day,
            "market_open": market_open}


def bars_at(stamps, closes, swing=1.0, opens=None, gmt=-18000, splits=None):
    out = SimpleNamespace(timestamp=list(stamps), close=list(closes),
                          high=[x + swing for x in closes],
                          low=[x - swing for x in closes], gmt_offset=gmt)
    if opens is not None:
        out.open = list(opens)
    if splits is not None:
        out.splits = splits
    return out


def row(conn, symbol, *cols):
    return conn.execute("SELECT %s FROM calls WHERE symbol=? ORDER BY at DESC"
                        % ", ".join(cols), (symbol,)).fetchone()


conn = db.connect(DB)
try:
    # -----------------------------------------------------------------------
    head("ONE CALL A SESSION, MADE AFTER THE CLOSE")
    # -----------------------------------------------------------------------
    # Calls were written at whatever check came first each UTC day. On a
    # weekday that was before the open, but a check during the session took a
    # price still moving, and the scoring then skipped the rest of the day.
    s = call("NVDA", "BUY", 100.0, 2.0, utc(2026, 3, 3, 16), price_day=30000,
             market_open=True)
    check("a stock's check while its market is open is not written down",
          not public.record_call(conn, s))
    s.update(market_open=False, at=utc(2026, 3, 3, 21, 40))
    check("the first check after the close is", public.record_call(conn, s))
    check("and only once for that session",
          not public.record_call(conn, dict(s, at=s["at"] + 6 * 3600, action="AVOID")))
    coin = call("BTC-USD", "AVOID", 90000.0, 2000.0, utc(2026, 3, 3, 3), price_day=30000,
                market_open=True)
    check("a coin, whose market never closes, is written down while it trades",
          public.record_call(conn, coin))
    try:
        conn.execute("INSERT INTO calls (symbol, day, action, price, stop, target, "
                     "horizon, at, price_day) VALUES ('NVDA', '2099-01-01', 'BUY', "
                     "1, 0.5, 2, 10, 1, 30000)")
        refused = False
    except sqlite3.IntegrityError:
        refused = True
    check("the database itself refuses a second call for one session", refused)
    a = call("KO", "WAIT", 60.0, 1.0, utc(2026, 3, 2, 21, 30), price_day=30001)
    b = dict(a, at=utc(2026, 3, 3, 21, 30), price_day=30002)
    check("a new session that closes at the old session's price is a new call",
          public.record_call(conn, a) and public.record_call(conn, b))

    # A database where two calls already share a session: the index cannot be
    # built over them, and they are never changed to make room.
    tmp = tempfile.mkdtemp(prefix="stockbot-test-dupes-")
    other = sqlite3.connect(_os.path.join(tmp, "x.db"))
    other.row_factory = sqlite3.Row
    other.executescript(public._SCHEMA)
    for day in ("2026-03-02", "2026-03-03"):
        other.execute("INSERT INTO calls (symbol, day, action, price, stop, target, "
                      "horizon, at, price_day) VALUES ('F', ?, 'WAIT', 10, 9, 11, 10, 1, 7)",
                      (day,))
    other.commit()
    try:
        public.ensure_schema(other)
        survived = True
    except sqlite3.Error as exc:
        survived = exc
    check("a database with a session written twice still opens",
          survived is True, survived)
    check("and still refuses a third call for that session",
          not public.record_call(other, call("F", "WAIT", 10.0, 0.5, 2, price_day=7)))
    check("without touching the two already there",
          other.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2)
    other.close()

    # -----------------------------------------------------------------------
    head("REPEATS ARE FOUND BY THE BARS, HOLIDAYS INCLUDED")
    # -----------------------------------------------------------------------
    # Thanksgiving 2026 is Thursday 26 November. Under the old rule the first
    # check of each UTC day, before the open, wrote down the last close.
    sessions = [utc(2026, 11, d, 14, 30) for d in (24, 25, 27)] + [utc(2026, 11, 30, 14, 30)]
    holiday = bars_at(sessions, [500.0, 501.0, 502.0, 503.0])
    for d in (25, 26, 27, 28, 29, 30):
        conn.execute("INSERT INTO calls (symbol, day, action, price, stop, target, "
                     "horizon, at) VALUES ('SPY', ?, 'AVOID', 500.0, 490.0, 515.0, 10, ?)",
                     ("2026-11-%d" % d, utc(2026, 11, d, 3)))
    conn.commit()
    check("the day after a holiday and the weekend repeat the call before them",
          public.mark_calls(conn, "SPY", holiday) == 3)
    kept = [time.strftime("%d", time.gmtime(r[0])) for r in conn.execute(
        "SELECT at FROM calls WHERE symbol='SPY' AND excluded IS NULL ORDER BY at")]
    check("the first call of each session is kept", kept == ["25", "26", "28"], kept)
    sess = [time.strftime("%a %d", time.gmtime(r[0] * DAY)) for r in conn.execute(
        "SELECT price_day FROM calls WHERE symbol='SPY' AND excluded IS NULL ORDER BY at")]
    check("each dated by the session its price came from",
          sess == ["Tue 24", "Wed 25", "Fri 27"], sess)
    check("running it again marks nothing new", public.mark_calls(conn, "SPY", holiday) == 0)

    # A coin's forming day is held back from the bars, so a call made today
    # has no next bar yet. Two such calls from different days are not one.
    days = [utc(2026, 3, d) for d in range(1, 6)]
    for d in (4, 5):
        conn.execute("INSERT INTO calls (symbol, day, action, price, stop, target, "
                     "horizon, at) VALUES ('ETH-USD', ?, 'WAIT', 3000, 2900, 3150, 10, ?)",
                     ("2026-03-0%d" % d, utc(2026, 3, d, 3)))
    conn.commit()
    check("calls whose next bar has not happened are left alone",
          public.mark_calls(conn, "ETH-USD", bars_at(days[:4], [3000.0] * 4, gmt=0)) == 0)
    later = [utc(2026, 3, d) for d in range(1, 9)]
    check("and are not taken for repeats once it has",
          public.mark_calls(conn, "ETH-USD", bars_at(later, [3000.0] * 8, gmt=0)) == 0
          and conn.execute("SELECT COUNT(*) FROM calls WHERE symbol='ETH-USD' "
                           "AND excluded IS NULL").fetchone()[0] == 2)

    # -----------------------------------------------------------------------
    head("A STOCK SPLIT INSIDE A CALL'S WINDOW")
    # -----------------------------------------------------------------------
    # Yahoo rescales the prices before a split. A call written down at 1120
    # was walked against history reading 112, "fell to the stop" on its first
    # day, and was scored wrong for good.
    at = utc(2026, 2, 26, 22)                                  # Thursday, after the close
    stamps = [utc(2026, 2, 26, 14, 30), utc(2026, 2, 27, 14, 30), utc(2026, 3, 2, 14, 30),
              utc(2026, 3, 3, 14, 30), utc(2026, 3, 4, 14, 30)]
    adjusted = [112.0, 112.5, 113.5, 118.0, 118.5]             # every bar in post-split prices
    split = [(utc(2026, 3, 2, 14, 30), 10.0)]                  # ten-for-one, from Monday
    after = bars_at(stamps, adjusted, swing=1.0, opens=adjusted, splits=split)
    public.record_call(conn, call("NFLX", "BUY", 1120.0, 28.0, at))
    public.settle_calls(conn, "NFLX", after)
    got = row(conn, "NFLX", "outcome", "how", "exit_price")
    check("a call made before a split is scored in prices on the same footing",
          got[:2] == ("right", "target") and abs(got[2] - 1183.0) < 1e-6, got[:])
    check("and its exit is kept in its own prices", 1000 < got[2] < 1300, got[2])
    check("a correctly scored call is not marked afterwards",
          public.mark_calls(conn, "NFLX", after) == 0
          and row(conn, "NFLX", "excluded")[0] is None)
    unadjusted = bars_at(stamps, adjusted, swing=1.0, opens=adjusted)   # no split events
    public.record_call(conn, call("AVGO", "BUY", 1120.0, 28.0, at))
    public.settle_calls(conn, "AVGO", unadjusted)
    check("without split events the old scoring is unchanged",
          row(conn, "AVGO", "outcome", "how", "exit_price")[:] == ("wrong", "stop", 112.5))
    check("once the split is known, a call scored across it the old way is marked",
          public.mark_calls(conn, "AVGO", after) == 1)
    got = row(conn, "AVGO", "outcome", "excluded")
    check("with the reason, and its stored result left as it was",
          got[0] == "wrong" and "split on 2 Mar 2026" in got[1]
          and "rose to the target" in got[1], got[:])

    # One-for-ten: the history before is multiplied up, and the old scoring
    # read the first day as reaching the target.
    small = [50.0, 49.0, 47.0, 46.0, 45.0]
    rev = [(utc(2026, 3, 2, 14, 30), 0.1)]
    public.record_call(conn, call("RIVN", "BUY", 5.0, 0.2, at))
    public.settle_calls(conn, "RIVN", bars_at(stamps, small, swing=0.5, opens=small))
    check("a reverse split scored the old way read as a win",
          row(conn, "RIVN", "how")[0] == "target")
    public.mark_calls(conn, "RIVN", bars_at(stamps, small, swing=0.5, opens=small,
                                            splits=rev))
    check("and is marked, saying the price fell to the stop",
          "fell to the stop" in (row(conn, "RIVN", "excluded")[0] or ""))
    public.record_call(conn, call("SMCI", "BUY", 5.0, 0.2, at))
    public.settle_calls(conn, "SMCI", bars_at(stamps, small, swing=0.5, opens=small,
                                              splits=rev))
    got = row(conn, "SMCI", "how", "exit_price")
    check("while one scored with it falls to the stop in its own prices",
          got[0] == "stop" and abs(got[1] - 4.7) < 1e-9, got[:])

    # Written down after the close before the split, once Yahoo had already
    # rescaled its prices: 113.5 is in new shares, and divided again it was
    # 11.35 beside prices near 113, a target "reached" on the first day.
    eve = utc(2026, 2, 28, 1)                                  # Friday's close, at night
    falls = [112.0, 112.5, 105.0, 104.0, 103.0]
    eve_bars = bars_at(stamps, falls, swing=1.0, opens=falls, splits=split)
    public.record_call(conn, call("LLY", "AVOID", 112.5, 2.0, eve))
    public.settle_calls(conn, "LLY", eve_bars)
    got = row(conn, "LLY", "outcome", "how", "exit_price")
    check("a call already in the new shares is not divided by the split again",
          got[:] == ("right", "stop", 105.0), got[:])
    check("and is not marked", public.mark_calls(conn, "LLY", eve_bars) == 0)
    public.record_call(conn, call("COST", "AVOID", 1125.0, 20.0, eve))
    public.settle_calls(conn, "COST", eve_bars)
    got = row(conn, "COST", "outcome", "how", "exit_price")
    check("one written the same night in the old shares still is",
          got[:] == ("right", "stop", 1050.0), got[:])
    public.record_call(conn, call("WMT", "AVOID", 112.5, 2.0, eve))
    conn.execute("UPDATE calls SET outcome='wrong', how='target', exit_price=157.5, "
                 "settled=? WHERE symbol='WMT'", (stamps[-1],))
    check("one scored that way before this is marked, with what happened",
          public.mark_calls(conn, "WMT", eve_bars) == 1
          and "its price already allowed for it. Counted once, the price fell to the stop."
          in row(conn, "WMT", "excluded")[0], row(conn, "WMT", "excluded")[0])
    two = split + [(utc(2026, 3, 4, 14, 30), 2.0)]             # and a second, two days on
    halved = [x / 2 for x in falls]
    check("a later split still counts, beside one its price allowed for",
          public._split_factor(bars_at(stamps, halved, splits=two), eve, 112.5) == 2.0
          and public._split_factor(bars_at(stamps, halved, splits=two), eve, 1125.0) == 20.0)

    # -----------------------------------------------------------------------
    head("ONLY CALLS OLD ENOUGH TO HAVE SETTLED ARE SCORED")
    # -----------------------------------------------------------------------
    # While the record is young, the calls already settled are mostly quick
    # stop-outs, because the stop is nearer than the target. Rates over them
    # favour every call that said not to buy.
    now = utc(2026, 6, 30, 12)
    young = db.connect(_os.path.join(tempfile.mkdtemp(prefix="stockbot-test-young-"),
                                     "data", "young.db"))
    try:
        old_day, new_day = now - 40 * DAY, now - 5 * DAY
        for k, sym in enumerate(("AAPL", "MSFT", "AMZN")):
            public.record_call(young, call(sym, "AVOID", 100.0, 2.0, old_day + k))
            public.settle_calls(young, sym, bars_at(
                [old_day + DAY * (j + 1) for j in range(10)], [100.0] * 9 + [104.0],
                swing=0.5))
        for k, sym in enumerate(("META", "TSLA")):
            public.record_call(young, call(sym, "AVOID", 100.0, 2.0, new_day + k))
            public.settle_calls(young, sym, bars_at([new_day + DAY], [96.0], swing=0.5))
        public.record_call(young, call("NVDA", "AVOID", 100.0, 2.0, new_day))
        rec = public.record(young, now=now)
        kinds = {x["action"]: x for x in rec["classes"]}
        check("the scores count only calls old enough",
              rec["scored"] == 3 and kinds["AVOID"]["decided"] == 3, rec["scored"])
        check("the newer settled ones are counted apart",
              rec["early"] == 2 and kinds["AVOID"]["waiting"] == 3,
              (rec["early"], kinds["AVOID"]["waiting"]))
        check("so three slow wrong calls are not hidden behind two quick right ones",
              kinds["AVOID"]["rate"] == 0.0, kinds["AVOID"]["rate"])
        check("and the list of results still shows every settled call",
              len(rec["latest"]) == 5)
        rec = public.record(young, now=old_day + 10 * DAY)
        check("before any call is old enough, nothing is scored and the start is dated",
              rec["scored"] == 0 and rec["counts_from"] == old_day + public.MATURE_DAYS * DAY)
    finally:
        young.close()

    # -----------------------------------------------------------------------
    head("CALLS THAT ARE ONE TEST ARE COUNTED AS ONE")
    # -----------------------------------------------------------------------
    t0 = utc(2026, 1, 5, 21, 30)
    pair = [{"symbol": "SPY", "at": t0, "horizon": 10},
            {"symbol": "VOO", "at": t0 + DAY, "horizon": 10}]
    check("SPY and VOO, which hold the same companies, are one test",
          public._separate_tests(pair) == 1)
    check("QQQ is another", public._separate_tests(
        pair + [{"symbol": "QQQ", "at": t0, "horizon": 10}]) == 2)

    # -----------------------------------------------------------------------
    head("EVERY CALL, AS A FILE ANYONE CAN CHECK")
    # -----------------------------------------------------------------------
    r = c.get("/record.csv")
    body = r.data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(body)))
    total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    check("a visitor can download it", r.status_code == 200 and r.mimetype == "text/csv"
          and "attachment" in r.headers.get("Content-Disposition", ""), r.status_code)
    check("with a header and one row for every call, left-out ones included",
          tuple(rows[0]) == public.CSV_COLUMNS and len(rows) == total + 1,
          (len(rows), total))
    check("saying why a call is left out", any("no market session" in x[-1] for x in rows)
          and any("split on 2 Mar 2026" in x[-1] for x in rows))
    named = [dict(zip(rows[0], x)) for x in rows[1:]]
    check("and dating each by its session",
          any(x["symbol"] == "SPY" and x["session"] == "2026-11-24" for x in named))
    check("with nothing about any person in it", "@" not in body and "user" not in body)
    page = text("/record")
    worked_out = []
    real_record, real_csv = public.record, public.calls_csv
    public.record = lambda *a, **k: (worked_out.append("page"), real_record(*a, **k))[1]
    public.calls_csv = lambda *a, **k: (worked_out.append("file"), real_csv(*a, **k))[1]
    try:
        again = text("/record")
        c.get("/record.csv")
        check("seen again with nothing new, the record is not worked out again",
              worked_out == [] and again == page, worked_out)
        public.record_call(conn, call("KO", "BUY", 70.0, 1.0, utc(2026, 4, 1, 21),
                                      price_day=39999))
        fresh_file = c.get("/record.csv").data.decode("utf-8")
        text("/record")
        check("a new call shows at once, on the page and in the file",
              worked_out == ["file", "page"] and "2026-04-01 21:00:00,BUY,70.0" in fresh_file,
              worked_out)
        conn.execute("UPDATE calls SET excluded='left out' WHERE symbol='KO' AND price_day=39999")
        conn.commit()
        check("and so does one marked as left out",
              "left out" in c.get("/record.csv").data.decode("utf-8").split(",KO,")[0]
              and worked_out[-1] == "file" and len(worked_out) == 3, worked_out)
        del worked_out[:]
        kept, web.RECORD_KEPT = web.RECORD_KEPT, 0.0
        text("/record")
        web.RECORD_KEPT = kept
        check("and it is worked out again after a while, since the clock moves what counts",
              worked_out == ["page"], worked_out)
    finally:
        public.record, public.calls_csv = real_record, real_csv
    check("the record page links to it", 'href="/record.csv"' in page)
    check("and says how a call is made and scored", "first check after its market closes"
          in page and "scored when the price reaches its stop or its target" in page
          and "trading days later" not in page)
    check("and that the levels are fixed, not a BUY plan's own",
          "fixed levels" in page and "below support" in page)
    import dataclasses
    has_splits = "splits" in {f.name for f in dataclasses.fields(Bars)}
    check("it says splits are allowed for only when the market data carries them",
          ("prices divided by the split" in page) == has_splits)

    @dataclasses.dataclass
    class WithSplits(Bars):
        splits: list = dataclasses.field(default_factory=list)

    real_bars = web.market_mod.Bars
    web.market_mod.Bars = WithSplits
    try:
        check("and does once it does", "prices divided by the split" in text("/record"))
    finally:
        web.market_mod.Bars = real_bars

    # -----------------------------------------------------------------------
    head("A STOCK PAGE SAYS WHICH CALL IS SCORED")
    # -----------------------------------------------------------------------
    page = text("/stock/nflx")
    check("the call on record is named, with its price and when it was written",
          "The call on record for 26 Feb 2026" in page and "BUY" in page
          and "1,120" in page and "22:00 UTC on 26 Feb 2026" in page)
    check("and the page says only the call after the close is scored",
          "only the call after the close is" in page)
    page = text("/stock/btc-usd")
    check("a coin's says how a coin's call is made", "first check after midnight" in page)

    # -----------------------------------------------------------------------
    head("A STRATEGY'S RESULT CARRIES ITS MARGIN OF ERROR")
    # -----------------------------------------------------------------------
    # A trade every third day, held up to ten, shares most of its days with
    # the next. The pages ranked 33 strategies by figures about one standard
    # error from zero, and called every sampled day a "signal".
    near, far = StrategyStat("x", "f", "all"), StrategyStat("y", "f", "all")
    for k in range(64):
        r_ = 1.5 if (k // 8) % 2 == 0 else -1.0
        for st, step in ((near, 3), (far, 30)):
            st.n += 1
            st.sum_r += r_
            st.note(k * step, r_, 10)
    var_near, sep_near = near.spread()
    var_far, sep_far = far.spread()
    check("trades that overlap and move together count as fewer separate ones",
          sep_near < 0.6 * near.n and var_near > var_far, (sep_near, var_near, var_far))
    check("trades that never overlap count in full", abs(sep_far - far.n) < 1e-9, sep_far)

    report = SimpleNamespace(overall={"x": near, "y": far})
    stored = public.tests(report)
    spread = public.spreads(report)
    # Three figures, the shape the version before this one reads, so rolling
    # back does not break the strategy pages; the spread is kept beside them.
    check("a stored test keeps the three figures the version before reads",
          stored["x"] == [64, near.wins, round(near.sum_r, 3)], stored["x"])
    check("and its spread and separate trades beside them",
          spread["x"] == [round(var_near, 3), round(sep_near, 1)]
          and public._figures(stored["x"], spread["x"])["separate"] == round(sep_near, 1),
          spread.get("x"))
    old = public._figures([50, 10, -1.5])
    check("a test stored before that shows no range, and counts every trade",
          old["se"] is None and old["separate"] == 50 and old["verdict"] is None)

    rows_ = [{"symbol": "NVDA", "name": "NVIDIA",
              "tests": {"x": [60, 30, 6.0, 400.0, 30.0], "y": [60, 30, 6.0, 60.0, 55.0]},
              "baseline": [370, 100, 18.5, 900.0, 160.0]}]
    check("a strategy with too few separate trades is left out, however many in all",
          not public.strategy_results(rows_, "x") and public.strategy_results(rows_, "y"))
    two = [{"symbol": "AMD", "name": "AMD", "tests": {"y": [100, 40, 20.0, 100.0, 90.0]},
            "baseline": [370, 100, -3.7, 900.0, 160.0]}] + rows_
    summary = public.strategy_summary(public.strategy_results(two, "y"))
    check("pooled over stocks, its margin adds up theirs",
          abs(summary["se"] - math.sqrt(160.0) / 160) < 1e-9
          and abs(summary["kept"] - 26.0 / 160) < 1e-9, summary)
    check("and is only called a gain when the whole margin is above zero",
          summary["verdict"] == 1 and public._verdict(0.02, 0.03) == 0
          and public._verdict(-0.2, 0.05) == -1)
    check("beside buying every day on the same stocks, weighted the same way",
          abs(summary["base"] - (100 * -0.01 + 60 * 0.05) / 160) < 1e-9, summary["base"])

    # Buying on every tested day takes the same days the strategies were
    # tested on.
    rng = np.random.RandomState(3)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n)))
    prev = np.concatenate([[100.0], close[:-1]])
    ts = np.array([1_600_000_000 + k * DAY for k in range(n)], dtype=np.int64)
    walk = Bars(symbol="T", name="T", asset_class="equity", currency="USD", exchange="T",
                interval="1d", timestamp=ts, open=prev,
                high=np.maximum(prev, close) * 1.01, low=np.minimum(prev, close) * 0.99,
                close=close, volume=np.full(n, 1e6), session_id=ts // DAY,
                market_open=False)
    ctx = build_context(walk)
    cfg = config.load()
    rep = measure_strategies(ctx, 1.5, 2.25, 10, 3, 2.0, weights=cfg["strategy_weights"])
    base = public.buy_every_day(ctx, 1.5, 2.25, 10, 3, 2.0)
    check("buying every day is tested on the same days as the strategies",
          base[0] == rep.samples == len(calibrate.sample_points(ctx, 10, 3, 1500)),
          (base[0], rep.samples))
    check("and carries a margin of its own", len(base) == 5 and base[3] > 0)

    for sym, kept, var, base_kept in (("NVDA", 12.0, 150.0, 40.0), ("AMD", -2.0, 150.0, -5.0)):
        snap = {"symbol": sym, "name": public.NAMES[sym], "action": "WAIT", "at": int(time.time()),
                "tests": {"rsi_reversion": [120, 40, kept, var, 60.0],
                          "trend_stack": [300, 90, 3.0, 400.0, 120.0]},
                "baseline": [370, 110, base_kept, 900.0, 160.0]}
        public.save_verdict(conn, snap)
    page = text("/backtests")
    check("the strategy list shows buying on every tested day among them",
          "Buying on every tested day" in page and "no strategy" in page)
    check("with each result's margin, and days tested rather than signals",
          "±" in page and "Days tested" in page and "Signals" not in page)
    check("and warns that one or two can clear it by luck",
          "one or two can look this good by luck alone" in page)
    check("its count of strategies leaves the shaded row out",
          "2 strategies, back-tested every day" in page)
    page = text("/backtest/rsi-reversion")
    check("a strategy's page sets each stock beside buying every day there",
          "Buying every day" in page and "+0.11R" in page)
    check("and says plainly when the result cannot be told from luck",
          "cannot be told apart from luck" in page)
    check("and how many separate trades its days are worth",
          "worth about 120 separate trades" in page)
    check("the method says every third day, and what the margin means",
          "every third trading day" in page and "margin of error" in page
          and "separate trades on a stock is" in page)

    # -----------------------------------------------------------------------
    head("SHORTING EVERY DAY, AND HOLDING THE INDEX")
    # -----------------------------------------------------------------------
    # A strategy that mostly shorted was set beside buying every day alone,
    # which a rising market makes hard for a short to beat, and nothing said
    # what simply holding the index over the same days kept.
    lift = 100 * np.exp(np.cumsum(np.random.RandomState(8).normal(0.004, 0.01, 900)))
    lift_prev = np.concatenate([[100.0], lift[:-1]])
    rising = Bars(symbol="T", name="T", asset_class="equity", currency="USD", exchange="T",
                  interval="1d", timestamp=ts, open=lift_prev,
                  high=np.maximum(lift_prev, lift) * 1.01,
                  low=np.minimum(lift_prev, lift) * 0.99, close=lift,
                  volume=np.full(900, 1e6), session_id=ts // DAY, market_open=False)
    rctx = build_context(rising)
    buys = public.buy_every_day(rctx, 1.5, 2.25, 10, 3, 2.0)
    shorts = public.short_every_day(rctx, 1.5, 2.25, 10, 3, 2.0)
    check("shorting every day takes the same days as buying every day",
          shorts[0] == buys[0] == len(calibrate.sample_points(rctx, 10, 3, 1500))
          and len(shorts) == 5, (shorts[0], buys[0]))
    check("and over a rising price loses where buying keeps",
          buys[2] > 0 > shorts[2], (buys[2], shorts[2]))

    # Each strategy's buys are counted as measure_strategies takes them.
    from bot.learning import MIN_OPINION
    from bot.strategies import evaluate
    sides = public.sides(rep)
    busiest = max(rep.overall, key=lambda k: rep.overall[k].n)
    by_hand = 0
    for i in calibrate.sample_points(ctx, 10, 3, 1500):
        for sig in evaluate(ctx, i, cfg["strategy_weights"])[0]:
            if (sig.name == busiest and sig.weight > 0 and sig.score >= MIN_OPINION
                    and calibrate.simulate_trade(ctx, i, 1, 1.5, 2.25, 10, 2.0)):
                by_hand += 1
    check("each strategy's buys are counted, the rest of its trades being shorts",
          sides[busiest] == by_hand and set(sides) == set(rep.overall)
          and any(0 < sides[k] < rep.overall[k].n for k in sides),
          (busiest, sides.get(busiest), by_hand))

    # An index that rises exactly 0.1% a day: every ten-day window gains
    # 1.001**10 - 1, less a 2bp round trip, and in R of a trade risking 1.5
    # ATR of the stock's own price that is the gain times price / (1.5 ATR).
    points = calibrate.sample_points(ctx, 10, 3, 1500)
    idx_close = 400.0 * 1.001 ** np.arange(900)
    spy = Bars(symbol="SPY", name="SPY", asset_class="equity", currency="USD", exchange="P",
               interval="1d", timestamp=ts, open=idx_close, high=idx_close, low=idx_close,
               close=idx_close, volume=np.full(900, 1e6), session_id=ts // DAY,
               market_open=False)

    def by_hand_hold(gain):
        return sum((gain - 1 - 0.0002) * float(walk.close[i]) / (1.5 * float(ctx.atr[i]))
                   for i in points)

    held = public.hold_index(ctx, spy, 1.5, 10, 3, 2.0)
    check("holding the index puts the same money in it over each tested window",
          held[0] == len(points) and abs(held[2] - by_hand_hold(1.001 ** 10)) < 1e-3
          and len(held) == 5, (held[:3], by_hand_hold(1.001 ** 10)))
    spy.dividend_factor = 1.0005 ** (np.arange(900) - 899.0)
    check("and counts its dividends, as a buy is counted",
          abs(public.hold_index(ctx, spy, 1.5, 10, 3, 2.0)[2]
              - by_hand_hold(1.001 ** 10 * 1.0005 ** 10)) < 1e-3)
    spy.dividend_factor[points[5]] = 0.0            # a hole in the source's data
    check("a window starting on a hole in the dividends is left out, not an error",
          public.hold_index(ctx, spy, 1.5, 10, 3, 2.0)[0] == len(points) - 1)
    spy.dividend_factor = None
    gone = points[50] + 10                      # the end of one window, the start of none
    keep = np.arange(900) != gone
    spy_gap = Bars(symbol="SPY", name="SPY", asset_class="equity", currency="USD",
                   exchange="P", interval="1d", timestamp=ts[keep], open=idx_close[keep],
                   high=idx_close[keep], low=idx_close[keep], close=idx_close[keep],
                   volume=np.full(899, 1e6), session_id=ts[keep] // DAY, market_open=False)
    check("a window the index has no price for at one end is left out",
          public.hold_index(ctx, spy_gap, 1.5, 10, 3, 2.0)[0] == len(points) - 1)
    check("a coin is set beside Bitcoin and a stock beside SPY",
          public.index_symbol("ETH-USD") == "BTC-USD" and public.index_symbol("KO") == "SPY")

    # The pages. rsi_reversion bought on 30 of its 120 trades on each stock,
    # trend_stack on 250 of 300.
    for sym, short_kept, index_kept in (("NVDA", -74.0, 74.0), ("AMD", -37.0, 37.0)):
        snap = public.verdict(conn, sym)
        snap.update(baseline_short=[370, 90, short_kept, 900.0, 160.0],
                    sides={"rsi_reversion": 30, "trend_stack": 250},
                    index={"symbol": "SPY", "hold": [370, 200, index_kept, 900.0, 160.0]})
        public.save_verdict(conn, snap)
    results = public.strategy_results(public.verdicts(conn), "rsi_reversion")
    summary = public.strategy_summary(results)
    # Each stock's figure per trade, weighted by the strategy's 120 trades on each.
    check("a strategy's summary sets it beside both sides and the index, weighted alike",
          abs(summary["short_base"] - (-0.2 - 0.1) / 2) < 1e-9
          and abs(summary["index"] - (0.2 + 0.1) / 2) < 1e-9
          and summary["long_share"] == 0.25 and summary["side"] == "short"
          and summary["indexes"] == ["SPY"], summary)
    page = text("/backtests")
    check("the strategy list shows shorting on every tested day too",
          "Shorting on every tested day" in page and "Buying on every tested day" in page)
    check("and marks a strategy that mostly shorts", "mostly shorts" in page
          and page.count("mostly shorts") == 1)
    page = text("/backtest/rsi-reversion")
    check("a mostly-short strategy's page sets it beside shorting every day, in bold",
          "It shorted on 75% of its trades, so set it beside <b>shorting on every "
          "tested day</b> of the same stocks, weighted the same way, which kept "
          "<b>-0.15R</b>. Buying on every one kept +0.05R." in page)
    check("and each stock beside both", "Shorting every day" in page and "-0.20R" in page)
    check("and beside holding the index, saying which",
          "Holding the index instead, the same money for 10 trading days" in page
          and "+0.15R a trade" in page and "SPY, a fund holding the S&amp;P 500." in page)
    check("the method says what each yardstick is",
          "shorting on every tested day</b> for one that mostly shorted" in page
          and "<b>holding the index</b>: the same money put in an index instead"
          in page)
    page = text("/backtest/trend-stack")
    check("a mostly-buying strategy's page sets it beside buying every day",
          "It bought on 83% of its trades, so set it beside <b>buying on every "
          "tested day</b>" in page)
    btc = dict(public.verdict(conn, "NVDA"), symbol="BTC-USD", name="Bitcoin",
               index={"symbol": "BTC-USD", "hold": [370, 200, 37.0, 900.0, 160.0]})
    public.save_verdict(conn, btc)
    check("with stocks and coins, it names both indexes",
          "SPY, a fund holding the S&amp;P 500, for the stocks, and Bitcoin for the coins"
          in text("/backtest/rsi-reversion"))
    old = public.verdict(conn, "AMD")
    for key in ("baseline_short", "sides", "index"):
        old.pop(key)
    public.save_verdict(conn, old)
    page = text("/backtest/rsi-reversion")
    check("an instrument checked before these were measured leaves them out, not wrong",
          "Buying on every tested day of the same stocks, weighted the same way, kept"
          in page and "It shorted on" not in page and "Holding the index" not in page
          .split("How we tested")[0])
    conn.execute("DELETE FROM public_verdicts WHERE symbol='BTC-USD'")
    conn.commit()

    # -----------------------------------------------------------------------
    head("THE BACKGROUND CHECK, END TO END")
    # -----------------------------------------------------------------------
    # The real engine on made-up bars: no network, no calendar.
    from bot import catalysts, engine

    def _no_calendar(symbol):
        raise RuntimeError("no calendar in tests")

    rng = np.random.RandomState(5)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n)))
    prev = np.concatenate([[100.0], close[:-1]])
    end = utc(2026, 11, 6, 14, 30)                         # a Friday's session
    stamps = np.array([end - (n - 1 - k) * DAY for k in range(n)], dtype=np.int64)
    live = Bars(symbol="NVDA", name="NVIDIA Corp", asset_class="equity", currency="USD",
                exchange="NMS", interval="1d", timestamp=stamps, open=prev,
                high=np.maximum(prev, close) * 1.01, low=np.minimum(prev, close) * 0.99,
                close=close, volume=np.full(n, 1e6), session_id=(stamps - 18000) // DAY,
                gmt_offset=-18000, market_open=False,
                meta={"regularMarketTime": int(end) + 6 * 3600 + 1800})
    looked_up = []
    engine.resolve = lambda s: (looked_up.append(s), {"symbol": s, "name": s})[1]
    engine.fetch_bars = lambda s, iv: live

    def calendar_read(s):
        looked_up.append("calendar of " + s)
        return _no_calendar(s)

    catalysts.fetch = calendar_read
    snap = web._public_snapshot("NVDA")
    check("a popular check asks for no ticker search and no earnings calendar, used by neither",
          looked_up == [], looked_up)
    web._public_snapshot("ZZTYPED")
    check("a typed watchlist entry is still looked up", looked_up == ["ZZTYPED"], looked_up)
    check("the check stores each strategy's spread, and buying every day",
          snap["baseline"] and len(snap["baseline"]) == 5
          and snap["tests"] and all(len(v) == 3 for v in snap["tests"].values())
          and all(len(snap["spread"].get(k, ())) == 2
                  for k, v in snap["tests"].items() if v[0] >= 2))
    check("and whether the market was open, and the session of the price",
          snap["market_open"] is False
          and time.strftime("%a %d %b", time.gmtime(snap["price_day"] * DAY)) == "Fri 06 Nov")
    check("without the index's prices, it leaves holding the index out", snap["index"] is None)
    both = web._public_snapshot("NVDA", {"SPY": live})
    check("and stores shorting every day, each strategy's buys, and holding SPY",
          len(both["baseline_short"]) == 5
          and both["baseline_short"][0] == both["baseline"][0]
          and set(both["sides"]) == set(both["tests"])
          and all(0 <= both["sides"][k] <= both["tests"][k][0] for k in both["sides"])
          and both["index"]["symbol"] == "SPY" and len(both["index"]["hold"]) == 5,
          (both.get("index"), both.get("baseline_short")))
    check("each beside the figures the version before reads, which are as they were",
          both["tests"] == snap["tests"] and both["baseline"] == snap["baseline"]
          and both["spread"] == snap["spread"])
    web.app.logger.disabled = True              # the failure is logged, as it should be
    try:
        broken = web._public_snapshot("NVDA", {"SPY": SimpleNamespace()})
    finally:
        web.app.logger.disabled = False
    check("index prices that cannot be read leave the comparison out, never the call",
          broken["index"] is None and broken["action"] == snap["action"]
          and broken["baseline_short"] == both["baseline_short"])
    # The run before stored its comparison. This run's fetch of SPY was
    # refused, which _index_bars hands on as None for the rest of the run.
    public.save_verdict(conn, both)
    kept = public.verdict(conn, "NVDA")["index"]
    refused = web._public_snapshot("NVDA", {"SPY": None})
    check("a run that could not fetch the index keeps the comparison stored before",
          kept and kept["symbol"] == "SPY" and refused["index"] == kept
          and refused["action"] == snap["action"], (kept, refused.get("index")))
    check("and one never checked before has none to keep",
          web._public_snapshot("ZZNEVER", {"SPY": None})["index"] is None)
    asked = []

    def fetch_index(symbol, interval):
        asked.append(symbol)
        if symbol == "BTC-USD":
            raise RuntimeError("the source refused")
        return live

    real_fetch = web.market_mod.fetch_bars
    web.market_mod.fetch_bars = fetch_index
    web.app.logger.disabled = True              # the refusal is logged, as it should be
    try:
        fetched = {}
        for sym in ("NVDA", "AMD", "BTC-USD", "ETH-USD", "KO"):
            web._index_bars(sym, fetched)
    finally:
        web.market_mod.fetch_bars = real_fetch
        web.app.logger.disabled = False
    check("a background run fetches each index once, and a refusal leaves only it out",
          asked == ["SPY", "BTC-USD"] and fetched["SPY"] is live
          and fetched["BTC-USD"] is None, asked)
    # As another stock: NVDA has calls from above, and a call is never removed.
    snap = dict(snap, symbol="ORCL")
    check("the call after the close is written down", public.record_call(conn, snap))
    then = int(stamps[-40]) + 7 * 3600                    # an older call, after its close
    public.record_call(conn, dict(snap, at=then, price=float(close[-40]),
                                  stop=float(close[-40]) * 0.95,
                                  target=float(close[-40]) * 1.08, price_day=None))
    public.mark_calls(conn, "ORCL", live)
    check("and an older one is scored on the real bars",
          public.settle_calls(conn, "ORCL", live) == 1
          and row(conn, "ORCL", "outcome")[0] is None, row(conn, "ORCL", "outcome")[:])
    dated = conn.execute("SELECT price_day FROM calls WHERE symbol='ORCL' AND at=?",
                         (then,)).fetchone()[0]
    check("dated by the session of the bar its price came from",
          dated == int((stamps[-40] - 18000) // DAY), dated)
finally:
    conn.close()

# ---------------------------------------------------------------------------
head("A COIN DAY THE SOURCE LEFT BLANK")
# ---------------------------------------------------------------------------
# market.py rebuilds a blank coin day between two known ones: its open and
# close are exact, its range is only what those two span. A stop or target
# the real day touched can be missing from the estimate.
conn = db.connect(DB)
try:
    today = int(time.time()) // DAY * DAY
    stamps = [today - (29 - k) * DAY for k in range(30)]
    flat = [100.0] * 30

    def coin_bars(closes, blank):
        out = bars_at(stamps, closes, swing=0.5, opens=closes, gmt=0)
        out.rebuilt = [stamps[k] for k in blank]
        return out

    # A call 11 days ago: its ten days run to yesterday, and the day before
    # yesterday is blank. The flat prices touch neither level.
    public.record_call(conn, call("AAA-USD", "BUY", 100.0, 2.0, stamps[18] + 3600,
                                  price_day=1, market_open=True))
    waited = public.settle_calls(conn, "AAA-USD", coin_bars(flat, [27]))
    check("a call whose window holds a blank day from this week waits for the source",
          waited == 0 and row(conn, "AAA-USD", "outcome")[0] is None, waited)
    check("and is scored once the source fills the day in",
          public.settle_calls(conn, "AAA-USD", coin_bars(flat, [])) == 1
          and row(conn, "AAA-USD", "outcome", "how", "excluded")[:]
          == ("flat", "flat", None),
          row(conn, "AAA-USD", "outcome", "how", "excluded")[:])

    # A stop touched before the blank day is a fact, whatever the blank holds.
    falls = list(flat)
    falls[20] = 90.0
    public.record_call(conn, call("BBB-USD", "BUY", 100.0, 2.0, stamps[18] + 3600,
                                  price_day=1, market_open=True))
    check("a stop touched before the blank day is scored at once",
          public.settle_calls(conn, "BBB-USD", coin_bars(falls, [27])) == 1
          and row(conn, "BBB-USD", "outcome", "how", "excluded")[:]
          == ("wrong", "stop", None),
          row(conn, "BBB-USD", "outcome", "how", "excluded")[:])

    # A blank day the source never filled: scored on the estimate and kept
    # out of the counts, with the reason on the page.
    public.record_call(conn, call("CCC-USD", "AVOID", 100.0, 2.0, stamps[12] + 3600,
                                  price_day=1, market_open=True))
    check("a blank day older than the wait is scored but does not count",
          public.settle_calls(conn, "CCC-USD", coin_bars(flat, [16])) == 1
          and row(conn, "CCC-USD", "outcome")[0] is not None
          and "never filled in" in (row(conn, "CCC-USD", "excluded")[0] or ""),
          row(conn, "CCC-USD", "outcome", "excluded")[:])

    # The call itself waits while the newest finished day is blank.
    check("the newest finished day blank is noticed",
          public._blank_yesterday(coin_bars(flat, [28]))
          and not public._blank_yesterday(coin_bars(flat, [20]))
          and not public._blank_yesterday(coin_bars(flat, [])))
    # Production bars end at the newest finished day: fetch_bars splits the
    # forming day off into the live price.
    finished = SimpleNamespace(timestamp=stamps[:-1], rebuilt=[stamps[-2]])
    older = SimpleNamespace(timestamp=stamps[:-1], rebuilt=[stamps[-3]])
    check("bars that end yesterday: a blank yesterday holds the call back",
          public._blank_yesterday(finished))
    check("but a blank day before it does not, once yesterday is in",
          not public._blank_yesterday(older))

    # Whether a call counts must not depend on how it did. A call an early
    # stop settled, before the blank day, is marked like the rest once the
    # source has had its three days to fill the day in.
    falls2 = list(flat)
    falls2[7] = 90.0
    public.record_call(conn, call("EEE-USD", "AVOID", 100.0, 2.0, stamps[5] + 3600,
                                  price_day=1, market_open=True))
    settled = public.settle_calls(conn, "EEE-USD", coin_bars(falls2, []))
    marked = public.mark_calls(conn, "EEE-USD", coin_bars(falls2, [10]))
    check("a call an early stop settled is marked too, once a day in its window stays blank",
          settled == 1 and marked == 1
          and "never filled in" in (row(conn, "EEE-USD", "excluded")[0] or ""),
          row(conn, "EEE-USD", "outcome", "how", "excluded")[:])
    public.record_call(conn, call("FFF-USD", "AVOID", 100.0, 2.0, stamps[15] + 3600,
                                  price_day=1, market_open=True))
    check("and a call whose window misses the blank day is not",
          public.mark_calls(conn, "FFF-USD", coin_bars(flat, [10])) == 0)

    snap_ = {"samples": 312, "worked": 150, "separate": 104}
    check("the picture card keeps the counts to the one line it has room for",
          "overlap" in public.counts_line(snap_)
          and "overlap" not in public.counts_line(snap_, short=True))

    real_due = public.due
    public.due = lambda conn, now=None: ["DDD-USD"]
    snap = call("DDD-USD", "BUY", 100.0, 2.0, int(time.time()), price_day=2,
                market_open=True)
    try:
        public.refresh(conn, lambda sym: dict(snap), pause=0,
                       bars=lambda sym: coin_bars(flat, [28]))
        skipped = conn.execute("SELECT COUNT(*) FROM calls WHERE symbol='DDD-USD'"
                               ).fetchone()[0]
        public.refresh(conn, lambda sym: dict(snap), pause=0,
                       bars=lambda sym: coin_bars(flat, []))
        written = conn.execute("SELECT COUNT(*) FROM calls WHERE symbol='DDD-USD'"
                               ).fetchone()[0]
    finally:
        public.due = real_due
    check("a check while yesterday is blank writes no call, and the next one does",
          (skipped, written) == (0, 1), (skipped, written))
finally:
    conn.close()


def on_page(path, db_path):
    """A page as a visitor sees it, read from another database."""
    was = web.app.config["DB_PATH"]
    web.app.config["DB_PATH"] = db_path
    try:
        return text(path)
    finally:
        web.app.config["DB_PATH"] = was


def outcomes(paths, atr):
    """What a buy did over each path of log moves from its price: the
    record's pretend trade, stop 1.5 ATR below, target 2.25 ATR above, the
    stop first when both are touched, and half the stop's distance at the
    end to count as up or down."""
    stop, target, h = -1.5 * atr, 2.25 * atr, paths.shape[-1]
    hit_stop, hit_target = paths <= stop, paths >= target
    first_stop = np.where(hit_stop.any(-1), hit_stop.argmax(-1), h)
    first_target = np.where(hit_target.any(-1), hit_target.argmax(-1), h)
    r = paths[..., -1] / (1.5 * atr)
    how = np.where(r >= 0.5, "up", np.where(r <= -0.5, "down", "flat")).astype(object)
    how[first_target < first_stop] = "target"
    how[(first_stop <= first_target) & (first_stop < h)] = "stop"
    return how


SIM_SIGMA, SIM_ATR = 0.02, 0.028


def avoid_calls(rng, stocks, days, together, start_day=20000):
    """An AVOID every trading day on each of `stocks` stocks for `days` days,
    scored as the record scores them. Each day's moves share a market move
    with weight `together`: 0 is stocks moving apart."""
    m = rng.normal(0, 1, days + 10)
    e = rng.normal(0, 1, (stocks, days + 10))
    moves = SIM_SIGMA * (math.sqrt(together) * m + math.sqrt(1 - together) * e)
    cum = np.concatenate([np.zeros((stocks, 1)), np.cumsum(moves, axis=1)], axis=1)
    paths = np.stack([cum[:, d + 1:d + 11] - cum[:, d:d + 1] for d in range(days)], axis=1)
    how = outcomes(paths, SIM_ATR)
    rows = []
    for d in range(days):
        day = start_day + d + 2 * (d // 5)              # weekdays only
        for s in range(stocks):
            h = how[s, d]
            rows.append({"symbol": "S%02d" % s, "at": day * DAY + 21 * 3600,
                         "session_at": day * DAY, "horizon": 10, "action": "AVOID",
                         "how": h, "outcome": ("right" if h in ("stop", "down") else
                                               "flat" if h == "flat" else "wrong"),
                         "price": 100.0, "stop": 100.0 * (1 - 1.5 * SIM_ATR),
                         "exit_price": 100.0 * math.exp(paths[s, d, -1])})
    return rows


# ---------------------------------------------------------------------------
head("A RANGE THAT ALLOWS FOR THE WHOLE MARKET MOVING")
# ---------------------------------------------------------------------------
# The record counted calls on different stocks as separate tests, though on
# a day the whole market moves they settle alike, so its 95% ranges were
# too narrow to mean 95%.
check("the t multiplier: 12.706 at one degree of freedom, 2.042 at 30, 1.96 at very many",
      public.t_95(1) == 12.706 and public.t_95(2) == 4.303 and public.t_95(30) == 2.042
      and abs(public.t_95(31) - 2.0395) < 1e-3 and abs(public.t_95(10 ** 6) - 1.95996) < 1e-4)


def hand(symbol, day, right):
    return {"symbol": symbol, "at": (20000 + day) * DAY + 3600, "horizon": 10,
            "session_at": (20000 + day) * DAY, "outcome": "right" if right else "wrong"}


cases = [hand("BTC-USD", 0, 1), hand("AAA", 0, 1), hand("BBB", 0, 1),
         hand("BTC-USD", 12, 1), hand("AAA", 12, 1),
         hand("AAA", 30, 0), hand("BBB", 30, 0),
         hand("BTC-USD", 44, 0), hand("AAA", 44, 0), hand("BBB", 44, 0)]
ones = [1.0 if r["outcome"] == "right" else 0.0 for r in cases]
var, df = public._together(cases, ones)
# By hand. The average is 0.5. Each call's distance from it, summed by
# market day and window (a coin's is 10 days, a stock's 10 trading days, 14
# calendar days):
#   day 0: coin +0.5, stocks +1.0     day 12: coin +0.5, stock +0.5
#   day 30: stocks -1.0               day 44: coin -0.5, stocks -1.0
# Squares: 0.25 + 1 + 0.25 + 0.25 + 1 + 0.25 + 1 = 4.0. Overlapping pairs,
# twice each: day 0's coin and stocks 1.0; day 0's stocks with day 12's coin
# and stock, 12 days apart, inside the stocks' 14: 1.0 + 1.0; day 12's two,
# 0.5; day 44's two, 1.0. Day 0's coin and day 12's calls are 12 days apart,
# past the coin's 10, and days 30 and 44 are 14 apart: none of those count.
# Total 8.5, above the calls' own 10 x 0.25 = 2.5. The 45 days hold 45 / 14
# = 3.21 windows of market, but counted where calls are (from day 0, then
# from day 30, then from day 44) only 3. So it is scaled by 3 / 2 and divided
# by 10 squared, with 3 - 1 = 2 degrees of freedom.
expect = 8.5 * 3.0 / 2.0 / 100.0
check("calls whose days overlap count as moving together, on one stock or two",
      abs(var - expect) < 1e-12 and df == 2, (var, df, expect))
check("calls that cover too few weeks of market give no such range",
      public._together(cases[:5], ones[:5]) is None)
bunched = [hand(sym, day, (day + k) % 2) for k, sym in enumerate(("AAA", "BBB"))
           for day in (0, 1, 2, 60, 61, 62, 120, 121, 122)]
check("calls bunched into three weeks are three spells of market, not the months between",
      public._together(bunched, [1.0 if r["outcome"] == "right" else 0.0
                                 for r in bunched])[1] == 2)
scored = [dict(r, how="stop" if r["outcome"] == "right" else "target", price=1.0,
               stop=0.9, exit_price=0.95 if r["outcome"] == "right" else 1.135)
          for r in cases]
young = public._stats(scored[:5], {})
wide = public._stats(scored, {})
apart = calibrate.wilson_interval(0.5 * wide["separate"], wide["separate"])
check("a kind of call too young for it says so, keeping the range per stock",
      young["together"] is False and young["lo"] is not None and young["move_lo"] is None)
check("once old enough, the range is the wider of the two at each end",
      wide["together"] is True and wide["lo"] <= apart[0] and wide["hi"] >= apart[1]
      and wide["move_lo"] < wide["mean_move"] < wide["move_hi"], (wide["lo"], apart))

# Coverage. Stocks that share a daily market move, and calls with no skill
# in them: the true right rate is the share of buys that lost, the same
# whatever the stocks share. Over a hundred and fifty made-up records of
# thirty stocks and sixty trading days, how often does each range hold it?
rng = np.random.RandomState(21)
lone = outcomes(np.cumsum(rng.normal(0, SIM_SIGMA, (200000, 10)), axis=1), SIM_ATR)
decided = lone[lone != "flat"]
truth = float(np.mean((decided == "stop") | (decided == "down")))


def coverage(together, runs=150):
    old_held = new_held = 0
    for _ in range(runs):
        st = public._stats(avoid_calls(rng, 30, 60, together), {})
        n_eff = max(1, min(st["separate"], st["decided"]))
        lo, hi = calibrate.wilson_interval(st["rate"] * n_eff, n_eff)
        old_held += lo <= truth <= hi
        new_held += st["together"] and st["lo"] <= truth <= st["hi"]
    return old_held / float(runs), new_held / float(runs)


old_cov, new_cov = coverage(0.4)
check("when stocks share a market move, counting them apart held the truth far less than 95%",
      old_cov < 0.7, old_cov)
check("and the range that allows for it holds it close to 95% of the time",
      new_cov >= 0.88 and new_cov - old_cov >= 0.25, (old_cov, new_cov))
old_cov, new_cov = coverage(0.0, runs=100)
check("when they move apart, it still holds it at least as often as before",
      new_cov >= 0.94 and new_cov >= old_cov - 0.02, (old_cov, new_cov))

# The page, on a record old enough, and on one too young.
now = int(time.time())
grown_path = _os.path.join(tempfile.mkdtemp(prefix="stockbot-test-grown-"), "data", "g.db")
grown = db.connect(grown_path)
try:
    first = now // DAY - 110
    for r in avoid_calls(np.random.RandomState(4), 12, 60, 0.4, start_day=first):
        grown.execute(
            "INSERT INTO calls (symbol, day, action, price, stop, target, horizon, at, "
            "outcome, how, exit_price, settled, price_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["symbol"], time.strftime("%Y-%m-%d", time.gmtime(r["at"])), "AVOID",
             r["price"], r["stop"], 100.0 * (1 + 2.25 * SIM_ATR), 10, r["at"],
             r["outcome"], r["how"], r["exit_price"], r["at"] + 15 * DAY,
             r["session_at"] // DAY))
    grown.commit()
    rec = public.record(grown)
    avoid = {c["action"]: c for c in rec["classes"]}["AVOID"]
    check("an old enough record's ranges allow for the market",
          avoid["together"] and avoid["move_lo"] is not None, avoid["together"])
finally:
    grown.close()
page = on_page("/record", grown_path)
check("and the page gives them, with what they allow for",
      "(95% range " + "%.0f%%" % (avoid["lo"] * 100) in page
      and "allow for calls on different stocks settling alike, as they do on a day "
          "the whole market moves" in page
      and "(95% range " in page.split("on average")[1][:40], page[:0])
check("and says how long that takes", "spread over about 6 weeks of the market" in page)
young_path = _os.path.join(tempfile.mkdtemp(prefix="stockbot-test-young2-"), "data", "y.db")
young_db = db.connect(young_path)
try:
    for r in avoid_calls(np.random.RandomState(5), 12, 12, 0.4, start_day=now // DAY - 40):
        young_db.execute(
            "INSERT INTO calls (symbol, day, action, price, stop, target, horizon, at, "
            "outcome, how, exit_price, settled, price_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["symbol"], time.strftime("%Y-%m-%d", time.gmtime(r["at"])), "AVOID",
             r["price"], r["stop"], 100.0 * (1 + 2.25 * SIM_ATR), 10, r["at"],
             r["outcome"], r["how"], r["exit_price"], r["at"] + 15 * DAY,
             r["session_at"] // DAY))
    young_db.commit()
finally:
    young_db.close()
page = on_page("/record", young_path)
check("a record too young for that range gives none, and says why",
      "(too early for a 95% range)" in page and "95% range " not in page.split("How it is scored")[0]
      and "Not yet distinguishable from chance: these calls cover too few weeks" in page)

# ---------------------------------------------------------------------------
head("A FINGERPRINT OF EACH DAY'S CALLS")
# ---------------------------------------------------------------------------
# A record that can be edited is a story. Each finished day's calls get a
# SHA-256 of the parts that never change, kept for good and published, so
# anyone who saves one can check later that no call was edited.
import hashlib  # noqa: E402
import re  # noqa: E402

prints_path = _os.path.join(tempfile.mkdtemp(prefix="stockbot-test-prints-"), "data", "p.db")
fp = db.connect(prints_path)
try:
    mon, tue = utc(2026, 3, 9), utc(2026, 3, 10)
    public.record_call(fp, call("BTC-USD", "AVOID", 69000.5, 1500.0, mon + 1800,
                                price_day=mon // DAY, market_open=True))
    public.record_call(fp, call("NVDA", "BUY", 201.25, 4.5, mon + 21 * 3600 + 2400,
                                price_day=mon // DAY))
    public.record_call(fp, call("KO", "WAIT", 70.1, 0.9, mon + 21 * 3600 + 2460))
    public.record_call(fp, call("BTC-USD", "WAIT", 70100.0, 1450.0, tue + 1800,
                                price_day=tue // DAY, market_open=True))
    check("a day is not fingerprinted before it is over",
          public.fingerprint_days(fp, now=mon + 23 * 3600) == 0)
    check("nor in the hour after, while its last calls may still be written",
          public.fingerprint_days(fp, now=tue + 1800) == 0)
    check("but is then, once", public.fingerprint_days(fp, now=tue + 3600) == 1
          and public.fingerprint_days(fp, now=tue + 7200) == 0)
    stored = public.fingerprints(fp)
    monday = fp.execute("SELECT * FROM calls WHERE at >= ? AND at < ? ORDER BY at, symbol",
                        (mon, mon + DAY)).fetchall()
    text_mon = public.fingerprint_text(monday)
    check("its fingerprint is the SHA-256 of the day's calls, one line each",
          len(stored) == 1 and stored[0]["day"] == "2026-03-09" and stored[0]["count"] == 3
          and stored[0]["sha256"] == hashlib.sha256(text_mon.encode()).hexdigest()
          and text_mon.count("\n") == 3
          and text_mon.splitlines()[0] == "BTC-USD,2026-03-09 00:30:00,AVOID,69000.5,"
                                           "66750.5,72375.5,10", text_mon)
    check("the same calls always give the same fingerprint",
          public.fingerprint_text([dict(r) for r in monday]) == text_mon)
    changed = []
    for field, value in (("symbol", "KO.L"), ("at", monday[2]["at"] + 1),
                         ("action", "AVOID"), ("price", 70.1 + 1e-9),
                         ("stop", monday[2]["stop"] - 0.01), ("target", 71.0),
                         ("horizon", 11)):
        edited = [dict(r) for r in monday]
        edited[2][field] = value
        changed.append(public.fingerprint_text(edited) != text_mon)
    check("changing any part written down, by any amount, changes it", all(changed), changed)
    check("and so does adding or removing a call",
          public.fingerprint_text(monday[:2]) != text_mon
          and public.fingerprint_text(list(monday) + [monday[0]]) != text_mon)
    # Scoring, marking and dating a call later touch other columns only.
    after = SimpleNamespace(timestamp=[tue + 14 * 3600 + k * DAY for k in range(12)],
                            close=[202.0] * 12, high=[220.0] * 12, low=[199.0] * 12,
                            gmt_offset=-18000)
    public.settle_calls(fp, "NVDA", after)
    fp.execute("UPDATE calls SET excluded='a reason' WHERE symbol='KO'")
    fp.execute("UPDATE calls SET price_day=20521 WHERE symbol='KO'")
    fp.commit()
    rescored = fp.execute("SELECT * FROM calls WHERE at >= ? AND at < ? ORDER BY at, symbol",
                          (mon, mon + DAY)).fetchall()
    check("scoring a call, leaving it out, or dating it later does not change it",
          rescored[1]["outcome"] == "right" and rescored[2]["excluded"] == "a reason"
          and public.fingerprint_text(rescored) == text_mon)
    allowed = []
    for sql in ["UPDATE calls SET %s WHERE symbol='KO'" % part for part in (
            "symbol='KO.L'", "at=at+1", "action='AVOID'", "price=price+1e-9",
            "stop=stop-0.01", "target=71.0", "horizon=11")] + [
            "DELETE FROM calls WHERE symbol='KO'"]:
        try:
            fp.execute(sql)
            allowed.append(sql)
        except sqlite3.DatabaseError:
            pass
    fp.commit()
    check("the database refuses to change what a call said, or to remove one",
          not allowed, allowed)
    try:
        fp.execute("UPDATE record_fingerprints SET sha256='0' WHERE day='2026-03-09'")
        edited = True
    except sqlite3.DatabaseError:
        edited = False
    try:
        fp.execute("DELETE FROM record_fingerprints")
        removed = True
    except sqlite3.DatabaseError:
        removed = False
    fp.execute("INSERT OR REPLACE INTO record_fingerprints (day, count, sha256, created_ts) "
               "VALUES ('2026-03-09', 9, 'x', 1)")
    fp.commit()
    check("the database refuses to change or remove a fingerprint, or to replace one",
          not edited and not removed
          and public.fingerprints(fp)[0]["sha256"] == stored[0]["sha256"]
          and public.fingerprints(fp)[0]["count"] == 3)
    # Tuesday's is taken three days late: it says so.
    public.fingerprint_days(fp, now=tue + 4 * DAY)
    prints = public.fingerprints(fp)
    check("one taken more than a day late is marked as after the fact",
          [p["day"] for p in prints] == ["2026-03-10", "2026-03-09"]
          and prints[0]["late"] and not prints[1]["late"], prints)
    # A day written before fingerprints existed gets one on the first run.
    public.record_call(fp, call("KO", "AVOID", 69.0, 0.9, mon - 6 * DAY + 21 * 3600))
    real_due = public.due
    public.due = lambda conn, now=None: []
    try:
        public.refresh(fp, lambda sym: {}, pause=0)
    finally:
        public.due = real_due
    late = [p for p in public.fingerprints(fp) if p["day"] == "2026-03-03"]
    check("a background run fingerprints the days it has not, dated then",
          late and late[0]["count"] == 1 and late[0]["late"]
          and late[0]["created_ts"] >= int(time.time()) - 60, late)
finally:
    fp.close()

body = None
was = web.app.config["DB_PATH"]
web.app.config["DB_PATH"] = prints_path
try:
    r = c.get("/record.csv")
    body = r.data.decode("utf-8")
    prints_file = c.get("/record/fingerprints.csv")
finally:
    web.app.config["DB_PATH"] = was
rows_ = list(csv.reader(io.StringIO(body)))
check("the record file starts with the columns that never change",
      tuple(rows_[0][:7]) == public.FINGERPRINTED
      and tuple(rows_[0]) == public.CSV_COLUMNS)
mine = [x for x in rows_[1:] if x[1].startswith("2026-03-09")]
again = hashlib.sha256("".join(",".join(x[:7]) + "\n" for x in mine).encode()).hexdigest()
check("and anyone can work out a day's fingerprint from it", again == stored[0]["sha256"])
# What the page tells people to run: grep '^[^,]*,DAY ' | cut -d, -f1-7 | shasum -a 256
lines = [ln for ln in body.split("\n") if re.match(r"^[^,]*,2026-03-09 ", ln)]
recipe = hashlib.sha256("".join(",".join(ln.split(",")[:7]) + "\n" for ln in lines)
                        .encode()).hexdigest()
check("and the shell recipe the page gives works out the same", recipe == stored[0]["sha256"])
got = prints_file.data.decode("utf-8")
check("every day's fingerprint is a file anyone can download",
      prints_file.status_code == 200 and prints_file.mimetype == "text/csv"
      and "attachment" in prints_file.headers.get("Content-Disposition", "")
      and got.splitlines()[0] == ",".join(public.FINGERPRINT_COLUMNS)
      and ("2026-03-09,3,%s," % stored[0]["sha256"]) in got
      and got.splitlines()[-1].endswith(",yes"), got[:300])
page = on_page("/record", prints_path)
check("the record page shows each day's fingerprint",
      stored[0]["sha256"] in page and "A fingerprint of each day's calls" in page
      and 'href="/record/fingerprints.csv"' in page)
check("says which were taken after the fact, and when",
      "fingerprinted on %s, after the fact" % time.strftime(
          "%d %b %Y", time.gmtime(time.time())).lstrip("0") in page
      and "fingerprinted on 14 Mar 2026, after the fact" in page
      and page.count("after the fact</span>") == 2
      and "its fingerprint vouches for its calls only from the day it was taken"
      in page)
check("and how to check one, with the newest day's command",
      "How to check one yourself" in page
      and "grep '^[^,]*,2026-03-10 ' orenth-record.csv | cut -d, -f1-7 | shasum -a 256"
      in page)

print()
print("%d failure(s)" % len(fails))
print("RECORD OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
