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
    check("and dating each by its session",
          any(x[1] == "SPY" and x[2] == "2026-11-24" for x in rows))
    check("with nothing about any person in it", "@" not in body and "user" not in body)
    page = text("/record")
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
    check("a stored test carries its spread and separate trades",
          len(stored["x"]) == 5 and stored["x"][:3] == [64, near.wins, round(near.sum_r, 3)])
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
    engine.resolve = lambda s: {"symbol": s, "name": s}
    engine.fetch_bars = lambda s, iv: live
    catalysts.fetch = _no_calendar
    snap = web._public_snapshot("NVDA")
    check("the check stores each strategy's spread, and buying every day",
          snap["baseline"] and len(snap["baseline"]) == 5
          and snap["tests"] and all(len(v) == 5 for v in snap["tests"].values() if v[0] >= 2))
    check("and whether the market was open, and the session of the price",
          snap["market_open"] is False
          and time.strftime("%a %d %b", time.gmtime(snap["price_day"] * DAY)) == "Fri 06 Nov")
    conn.execute("DELETE FROM calls WHERE symbol='NVDA'")
    conn.commit()
    check("the call after the close is written down", public.record_call(conn, snap))
    then = int(stamps[-40]) + 7 * 3600                    # an older call, after its close
    public.record_call(conn, dict(snap, at=then, price=float(close[-40]),
                                  stop=float(close[-40]) * 0.95,
                                  target=float(close[-40]) * 1.08, price_day=None))
    public.mark_calls(conn, "NVDA", live)
    check("and an older one is scored on the real bars",
          public.settle_calls(conn, "NVDA", live) == 1
          and row(conn, "NVDA", "outcome")[0] is None, row(conn, "NVDA", "outcome")[:])
    dated = conn.execute("SELECT price_day FROM calls WHERE symbol='NVDA' AND at=?",
                         (then,)).fetchone()[0]
    check("dated by the session of the bar its price came from",
          dated == int((stamps[-40] - 18000) // DAY), dated)
finally:
    conn.close()

print()
print("%d failure(s)" % len(fails))
print("RECORD OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
