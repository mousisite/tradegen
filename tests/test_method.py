"""How calls, odds and back-tests are worked out, on prices made up for the test.

No network: market data is synthetic, and where a real download would happen
the payload is built by hand. Each section pins one way the method used to
mislead, and says how.
"""
import os as _os
import re
import sys as _sys
import time
import types

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402,F401  (keeps any database this touches out of the real one)

harness.isolate("method")

import numpy as np  # noqa: E402

from bot import calibrate as cal  # noqa: E402
from bot import catalysts, config, decision, engine, market, plain, public  # noqa: E402
from bot import strategies as S  # noqa: E402
from bot.calibrate import Calibration, Outcome  # noqa: E402
from bot.market import Bars  # noqa: E402
from bot.sentiment import Sentiment  # noqa: E402

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


CFG = config.load()
DAY = 86400
FLAT = Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped")
JARGON = ("expectancy", "r-multiple", "confidence interval", "atr", "wilson",
          "basis point", "sharpe", "drawdown")


def make_bars(close, open_=None, high=None, low=None, volume=None, start=None,
              step=DAY, gmt=-18000, asset="equity", interval="1d", factor=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev.copy() if open_ is None else np.asarray(open_, dtype=float)
    high = np.maximum(open_, close) * 1.004 if high is None else np.asarray(high, float)
    low = np.minimum(open_, close) * 0.996 if low is None else np.asarray(low, float)
    volume = np.full(n, 1e6) if volume is None else np.asarray(volume, float)
    start = start if start is not None else 1_600_000_000 - (1_600_000_000 % DAY) + 52200
    ts = np.array([start + k * step for k in range(n)], dtype=np.int64)
    return Bars(symbol="TEST", name="Test", asset_class=asset, currency="USD",
                exchange="TEST", interval=interval, timestamp=ts, open=open_,
                high=high, low=low, close=close, volume=volume,
                session_id=((ts + gmt) // DAY).astype(np.int64), gmt_offset=gmt,
                prev_close=float(close[-2]), live_price=None, market_open=False,
                last_bar_time="", meta={}, dividend_factor=factor)


def walk(seed, n=1250, drift=0.0, vol=0.02):
    """A random walk: no signal can have an edge on it."""
    rng = np.random.RandomState(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    prev = np.concatenate([[100.0], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, vol * 0.25, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.4, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.4, n)))
    return make_bars(close, open_, high, low, np.exp(rng.normal(15.0, 0.4, n)))


def edge_series(seed, n=1250, leg=60, drift=0.005, vol=0.01, into=40, dip=4):
    """Rises and falls in turn, so buying on any day earns little, while buying
    when the trend signals lean up earns a lot: a real edge, carried by the
    signals rather than by drift. It ends on a short pullback and a turn back
    up inside a rise, so the price is neither stretched nor under a level."""
    rng = np.random.RandomState(seed)
    k = np.arange(n)
    sign = np.where(((k - (n - into)) // leg) % 2 == 0, 1.0, -1.0)
    rets = sign * drift + rng.normal(0.0, vol, n)
    rets[-dip - 1:-1] = -0.012 + rng.normal(0.0, vol * 0.2, dip)
    rets[-1] = 0.02
    close = 100.0 * np.exp(np.cumsum(rets))
    prev = np.concatenate([[100.0], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, vol * 0.2, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.3, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.3, n)))
    open_[-1] = low[-1] = close[-2] * 0.998
    high[-1] = close[-1] = max(close[-1], high[-2] * 1.002)
    return make_bars(close, open_, high, low, np.exp(rng.normal(15.0, 0.3, n)))


# The full pipeline, exactly as the web app runs it, fed synthetic bars.
_FEED = {}
engine.resolve = lambda s: {"symbol": s, "name": s}
engine.fetch_bars = lambda s, iv: _FEED["bars"]


def _no_calendar(symbol):
    raise RuntimeError("no calendar in tests")


catalysts.fetch = _no_calendar


def analyse(bars, cfg=None):
    _FEED["bars"] = bars
    return engine.analyse("TEST", cfg or CFG, interval="1d", with_news=False,
                          record=False)


# ---------------------------------------------------------------------------
head("A FINISHED SESSION IS READ, NOT DROPPED")
# ---------------------------------------------------------------------------
# Yahoo stamps a stock's daily bar at the session's open. Read as a 24-hour
# bar, the session that closed at 4pm counted as unfinished until the next
# morning, so every evening analysis ran on the day before, and the day's
# change was measured against the close two sessions back.

def payload(ts, closes, itype="EQUITY", gmt=-18000, regular=None, adj=None,
            price=None, volumes=None, when=None):
    n = len(closes)
    known = [x if x is not None else 100.0 for x in closes]
    quote = {"open": list(known), "high": [x * 1.01 for x in known],
             "low": [x * 0.99 for x in known], "close": list(closes),
             "volume": list(volumes) if volumes is not None else [1000.0] * n}
    meta = {"symbol": "TEST", "instrumentType": itype, "gmtoffset": gmt,
            "currency": "USD", "regularMarketPrice": price or closes[-1],
            "currentTradingPeriod": {"regular": regular} if regular else {}}
    if when is not None:
        meta["regularMarketTime"] = when
    ind = {"quote": [quote]}
    if adj is not None:
        ind["adjclose"] = [{"adjclose": list(adj)}]
    return {"chart": {"result": [{"meta": meta, "timestamp": list(ts),
                                  "indicators": ind}], "error": None}}


def fetch(pl, now, interval="1d"):
    real_get, real_time = market._get, market.time
    market._get = lambda url, params, tries=3: pl
    market.time = types.SimpleNamespace(time=lambda: now, strftime=time.strftime,
                                        gmtime=time.gmtime, sleep=lambda s: None)
    try:
        return market.fetch_bars("TEST", interval)
    finally:
        market._get, market.time = real_get, real_time


D0 = 1_700_000_000 - (1_700_000_000 % DAY)          # a midnight UTC
OPEN = 14 * 3600 + 1800                              # 09:30 New York, in UTC
days = [D0 + k * DAY for k in range(40)]
closes = [100.0 + k for k in range(40)]
last = days[-1]
today = {"start": last + OPEN, "end": last + OPEN + 6 * 3600 + 1800}

b = fetch(payload([d + OPEN for d in days], closes, regular=today),
          now=last + 23 * 3600 + 45 * 60)            # 18:45 New York, closed
check("after the close, the session that just closed is read",
      len(b) == 40 and b.close[-1] == closes[-1], (len(b), b.close[-1]))
check("and the day's change is against the session before it",
      b.prev_close == closes[-2], b.prev_close)

b = fetch(payload([d + OPEN for d in days], closes, regular=today),
          now=last + OPEN + 3 * 3600)               # mid-session
check("while the session trades, its bar is the live price",
      len(b) == 39 and b.live_price == closes[-1], (len(b), b.live_price))
check("and the previous close is yesterday's, not two sessions back",
      b.prev_close == closes[-2], (b.prev_close, closes[-2], closes[-3]))

tomorrow = {"start": today["start"] + DAY, "end": today["end"] + DAY}
b = fetch(payload([d + OPEN for d in days], closes, regular=tomorrow),
          now=last + DAY + 12 * 3600)               # next morning, before the open
check("the next morning, before the open, yesterday is complete",
      len(b) == 40 and b.prev_close == closes[-2], (len(b), b.prev_close))

stale = {"start": today["start"] - DAY, "end": today["end"] - DAY}
b = fetch(payload([d + OPEN for d in days], closes, regular=stale),
          now=last + OPEN + 3600)
check("a bar newer than the session described is still treated as forming",
      len(b) == 39, len(b))

# After the close Yahoo has sent the finished session's bar with no close, its
# close being the quote it reports for that session. Dropped, the session went
# with it, and the day's change ran against the close two sessions back.
blank = closes[:-1] + [None]
closed_at = today["end"]
b = fetch(payload([d + OPEN for d in days], blank, regular=today, price=closes[-1],
                  when=closed_at), now=last + 23 * 3600 + 45 * 60)
check("a finished session sent without its close is filled from its own quote",
      len(b) == 40 and b.close[-1] == closes[-1], (len(b), b.close[-1]))
check("and its change is against the session before",
      b.prev_close == closes[-2], b.prev_close)
b = fetch(payload([d + OPEN for d in days], blank, regular=today, price=closes[-1],
                  when=today["start"] + 3600), now=today["start"] + 3600)
check("a session still trading is never filled in",
      len(b) == 39 and b.prev_close == closes[-2], (len(b), b.prev_close))
b = fetch(payload([d + OPEN for d in days[:-1]], closes[:-1], regular=today,
                  price=closes[-1], when=today["start"] + 3600),
          now=today["start"] + 3600)
check("before the session's bar exists, the change is still against yesterday",
      len(b) == 39 and b.prev_close == closes[-2], (len(b), b.prev_close))

# Intraday keeps its own rule: a bar lasts its interval.
five = 300
sess = [D0 + k * DAY + OPEN for k in range(3)]
ts5 = [s + j * five for s in sess for j in range(78)]
c5 = [200.0 + 0.01 * j for j in range(len(ts5))]
ts5.append(sess[-1] + DAY)                          # first bar of a fourth session
c5.append(250.0)
b = fetch(payload(ts5, c5, regular={"start": sess[-1] + DAY,
                                    "end": sess[-1] + DAY + 23400}),
          now=sess[-1] + DAY + 60, interval="5m")
check("intraday: the newest five-minute bar, still forming, is the live price",
      b.live_price == 250.0 and len(b) == len(ts5) - 1)
check("and at a session's first bar the previous close is the last session's",
      b.prev_close == c5[-2], (b.prev_close, c5[-2]))
b = fetch(payload(ts5[:-1], c5[:-1], regular={"start": sess[-1], "end": sess[-1] + 23400}),
          now=sess[-1] + 77 * five + 120, interval="5m")
check("mid-session the previous close is still the prior session's last",
      b.prev_close == c5[2 * 78 - 1], (b.prev_close, c5[2 * 78 - 1]))

# Crypto's day is a full UTC day, so its rule is unchanged too.
cdays = [D0 + k * DAY for k in range(40)]
b = fetch(payload(cdays, closes, itype="CRYPTOCURRENCY", gmt=0,
                  regular={"start": cdays[-1], "end": cdays[-1] + DAY - 1}),
          now=cdays[-1] + 12 * 3600)
check("crypto: today's bar is still forming at noon",
      len(b) == 39 and b.live_price == closes[-1])
check("and the day's change is against yesterday's close",
      b.prev_close == closes[-2], (b.prev_close, closes[-2]))

# Dividends: the factor is carried, the traded prices are not rewritten.
adj = [c * (0.98 if k < 30 else 1.0) for k, c in enumerate(closes)]
b = fetch(payload([d + OPEN for d in days], closes, regular=today, adj=adj),
          now=last + 23 * 3600)
check("daily bars carry each bar's dividend factor",
      b.dividend_factor is not None and abs(b.dividend_factor[0] - 0.98) < 1e-9
      and b.dividend_factor[-1] == 1.0)
check("and keep the prices as traded", list(b.close) == closes)


# ---------------------------------------------------------------------------
head("THE ODDS ARE THE CALL'S OWN SIDE")
# ---------------------------------------------------------------------------
# Long and short trades were pooled into one bucket by score strength, so a
# bearish call on NVDA was quoted +0.14R over 77 setups, 64 of them longs that
# made money; its 13 shorts had lost.

ctx = S.build_context(walk(1, n=700))
real_backtest = cal.run_backtest
longs = [Outcome(0.3, k % 2 == 0, 1.5 if k % 2 == 0 else -0.5, 5, 220 + 6 * k, 0.0, 1)
         for k in range(60)]
shorts = [Outcome(0.3, False, -1.0, 5, 223 + 6 * k, 0.0, -1) for k in range(60)]
cal.run_backtest = lambda *a, **k: longs + shorts
try:
    up = cal.calibrate(ctx, 0.3, 1.5, 2.25, 10, direction=1)
    down = cal.calibrate(ctx, -0.3, 1.5, 2.25, 10)
    check("a long call's odds come only from long trades",
          up.direction == 1 and up.bucket_samples == 60
          and abs(up.expectancy_r - 0.5) < 1e-9, (up.bucket_samples, up.expectancy_r))
    check("a bearish score's odds come only from short trades",
          down.direction == -1 and down.bucket_samples == 60
          and abs(down.expectancy_r + 1.0) < 1e-9, (down.bucket_samples, down.expectancy_r))
    cal.run_backtest = lambda *a, **k: longs + shorts[:10]
    few = cal.calibrate(ctx, -0.3, 1.5, 2.25, 10, min_bucket=25)
    check("too few trades on the call's side is not called reliable",
          not few.reliable and few.bucket_samples == 10, few.bucket_samples)
    check("and never shows an edge", not few.demonstrated)
finally:
    cal.run_backtest = real_backtest

every = cal.run_backtest(ctx, 1.5, 2.25, 10, 3, CFG["strategy_weights"])
check("every simulated trade records its side",
      every and all(o.direction in (1, -1) for o in every))
real = cal.calibrate(ctx, 0.25, 1.5, 2.25, 10, 3, CFG["strategy_weights"],
                     direction=1)
mine = [o for o in every if o.direction == 1
        and real.bucket_low <= o.score <= real.bucket_high]
check("on a real replay the bucket holds that side's trades and nothing else",
      len(mine) == real.bucket_samples
      and abs(np.mean([o.r_multiple for o in mine]) - real.expectancy_r) < 1e-9,
      (len(mine), real.bucket_samples))


# ---------------------------------------------------------------------------
head("A STOP THE PRICE OPENED THROUGH")
# ---------------------------------------------------------------------------
# Every stop used to fill exactly at the stop. An earnings gap opens past it,
# and the loss is the open: on several real names that flipped the record's
# sign.

def one_trade(next_open, next_low, next_high, direction=1, factor=None, flat=None):
    n = 40
    close = np.full(n, 100.0)
    open_ = np.full(n, 100.0)
    high = np.full(n, 100.5)
    low = np.full(n, 99.5)
    open_[21], low[21], high[21] = next_open, next_low, next_high
    close[21] = (next_low + next_high) / 2
    if flat is not None:
        close[21:] = open_[21:] = flat
        high[21:], low[21:] = flat + 0.2, flat - 0.2
    bars = make_bars(close, open_, high, low, factor=factor)
    ctx1 = S.Context(bars, {"atr": np.full(n, 2.0)}, warmup=0)
    return cal.simulate_trade(ctx1, 20, direction, 1.5, 2.25, 10, 0.0)


won, r, held, _ = one_trade(94.0, 93.0, 96.0)
check("a long whose stop was gapped through loses at the open",
      not won and abs(r - (-2.0)) < 1e-9, r)
won, r, _, _ = one_trade(99.0, 96.5, 99.5)
check("a stop touched without a gap still costs one risk unit",
      not won and abs(r - (-1.0)) < 1e-9, r)
won, r, _, _ = one_trade(106.0, 105.0, 107.0, direction=-1)
check("a short gapped through its stop loses at the open too",
      not won and abs(r - (-2.0)) < 1e-9, r)
won, r, _, _ = one_trade(105.0, 96.0, 106.0)
check("a day that opened past the target reached it first",
      won and abs(r - 1.5) < 1e-9, r)
factor = np.concatenate([np.full(21, 0.99), np.ones(19)])
won, r, _, _ = one_trade(99.0, 98.8, 99.2, factor=factor, flat=99.0)
won0, r0, _, _ = one_trade(99.0, 98.8, 99.2, flat=99.0)
check("an ex-dividend drop is not charged to a long as a loss",
      abs(r) < 1e-9 and r0 < -0.3, (r, r0))

walked = public._walk(types.SimpleNamespace(open=[94.0], high=[99.0], low=[93.0],
                                            close=[95.0]), 0, 100.0, 97.0, 104.5, 10)
check("the public record's pretend buy also sells a gap at the open",
      walked == ("stop", 94.0), walked)
check("and without opens it falls back to the stop",
      public._walk(types.SimpleNamespace(high=[99.0], low=[93.0], close=[95.0]),
                   0, 100.0, 97.0, 104.5, 10) == ("stop", 97.0))


# ---------------------------------------------------------------------------
head("OVERLAPPING TRADES ARE NOT SEPARATE TESTS")
# ---------------------------------------------------------------------------
# A trade every three bars, each held up to ten: neighbours share most of
# their bars. Counted as independent, the range held the true rate about nine
# times in ten instead of 19 in 20.

# Wins and losses in runs of eight: every trade a window overlaps settled the
# same way as most of its neighbours, which is what shared bars do. (Runs of
# four alternate within one window, so their overlaps cancel out.)
runs = [Outcome(0.3, (k // 8) % 2 == 0, 1.0, 8, 3 * k, 0.0, 1) for k in range(40)]
apart = [Outcome(0.3, (k // 8) % 2 == 0, 1.0, 8, 30 * k, 0.0, 1) for k in range(40)]
check("trades that overlap and win or lose together widen the range",
      cal.design_effect(runs, 10) > 1.5, cal.design_effect(runs, 10))
check("trades that never overlap count in full",
      cal.design_effect(apart, 10) == 1.0, cal.design_effect(apart, 10))

cal.run_backtest = lambda *a, **k: runs
try:
    wide = cal.calibrate(ctx, 0.3, 1.5, 2.25, 10, direction=1)
finally:
    cal.run_backtest = real_backtest
naive = cal.wilson_interval(sum(o.won for o in runs), len(runs))
check("so the odds' 95% range is wider than the raw count gives",
      (wide.ci_high - wide.ci_low) > (naive[1] - naive[0]) * 1.15
      and wide.effective_samples < len(runs),
      ((wide.ci_low, wide.ci_high), naive, wide.effective_samples))

T0 = 1_767_628_800                                    # 5 Jan 2026
rows = [{"symbol": "NVDA", "at": T0 + k * DAY, "horizon": 10, "outcome": "right"}
        for k in range(10)]
check("ten daily calls on one stock are one separate test",
      public._separate_tests(rows) == 1, public._separate_tests(rows))
spread = [dict(r, symbol="S%d" % k) for k, r in enumerate(rows)]
check("calls on ten stocks are ten", public._separate_tests(spread) == 10)
check("and a call after the last one's days have run out counts again",
      public._separate_tests(rows + [dict(rows[0], at=T0 + 15 * DAY)]) == 2)
one = public._stats([dict(r, how="target", exit_price=1.0, price=1.0, stop=0.9)
                     for r in rows], {})
many = public._stats([dict(r, how="target", exit_price=1.0, price=1.0, stop=0.9)
                      for r in spread], {})
check("so the record's range for one stock's run is the wider one",
      one["lo"] < many["lo"] and one["separate"] == 1 and many["separate"] == 10,
      (one["lo"], many["lo"]))


# ---------------------------------------------------------------------------
head("A BUY NEEDS A DEMONSTRATED EDGE")
# ---------------------------------------------------------------------------
# A positive average over enough cases used to be promoted to BUY. On random
# prices that called about one in fourteen a buy, most under a verdict saying
# no edge was proven.

base_bars = walk(7, n=400, vol=0.01)
dctx = S.build_context(base_bars)
calm = dict(CFG, max_extension_atr=99.0)        # this section tests the record, not the stretch


def calib(rate=0.30, lo=0.20, hi=0.41, be=0.28, exp=0.05, n=120, side=1,
          reliable=True, stable=True, baseline=0.0):
    return Calibration(samples=400, bucket_low=0.1, bucket_high=0.5,
                       bucket_samples=n, hit_rate=rate, ci_low=lo, ci_high=hi,
                       breakeven=be, expectancy_r=exp, horizon=10,
                       reward_risk=1.5, baseline_rate=0.3, reliable=reliable,
                       note="", avg_win_r=1.48, avg_loss_r=-0.55,
                       holdout_samples=40, stable=stable, direction=side,
                       effective_samples=n * 0.7, baseline_expectancy=baseline,
                       score=0.3)


def call(c, composite=0.3, cfg=calm):
    reg = S.classify_regime(dctx, len(base_bars) - 1)
    return decision.decide(dctx, base_bars, [], composite, reg, c, FLAT, [], cfg)


thin = call(calib())
check("a thin positive record stays WAIT", thin.action == "WAIT", thin.action)
check("and says why", "not clear of chance" in thin.entry_rationale,
      thin.entry_rationale)
clear = call(calib(rate=0.45, lo=0.36, hi=0.54, exp=0.30))
check("a record whose range clears break-even is a BUY", clear.action == "BUY",
      clear.action)
drift = call(calib(rate=0.45, lo=0.36, hi=0.54, exp=0.30, baseline=0.40))
check("but not when buying on any day did at least as well",
      drift.action == "WAIT" and "same side" in drift.entry_rationale,
      (drift.action, drift.entry_rationale))
decay = call(calib(rate=0.45, lo=0.36, hi=0.54, exp=0.30, stable=False))
check("nor when the most recent third disagrees", decay.action == "WAIT",
      decay.action)
quiet = call(calib(), composite=0.05)
check("a weak lean with a thin record is not heard out",
      quiet.action == "AVOID" and quiet.avoid_reason == "no_signal",
      (quiet.action, quiet.avoid_reason))
other = call(calib(rate=0.45, lo=0.36, hi=0.54, exp=0.30, side=-1))
check("a record from the other side never promotes a call",
      other.action != "BUY", other.action)
losing = call(calib(rate=0.2, lo=0.12, hi=0.3, exp=-0.2))
check("a losing record is an AVOID, and says that is why",
      losing.action == "AVOID" and losing.avoid_reason == "negative_record",
      (losing.action, losing.avoid_reason))
bear = call(calib(side=-1), composite=-0.4)
check("a bearish lean with shorts off is an AVOID that says so",
      bear.action == "AVOID" and bear.avoid_reason == "bearish_no_shorts"
      and bear.odds_direction == -1, (bear.action, bear.avoid_reason))
check("and whose verdict says the odds are for a fall",
      "falling" in bear.edge_verdict, bear.edge_verdict)
check("the measured break-even is labelled as the tested trade's",
      "That trade, entered at the close" in clear.edge_verdict, clear.edge_verdict)
check("and a buy says what buying on any day made",
      "Buying on every day" in clear.edge_verdict, clear.edge_verdict)
check("daily bars name the 20-bar VWAP, not a session VWAP",
      any("20-bar VWAP" in c for c in clear.confirmations)
      and not any("session VWAP" in c for c in clear.confirmations),
      clear.confirmations)


# ---------------------------------------------------------------------------
head("THE APP CAN STILL SAY BUY ON A REAL EDGE")
# ---------------------------------------------------------------------------
# The stricter test must not make BUY unreachable. On prices where the signals
# carry a real edge, and buying on any day earns little, the full pipeline
# with the house settings still says buy.

edge_calls = [analyse(edge_series(seed)) for seed in range(4)]
buys = [r for r in edge_calls if r.plan.action == "BUY"]
print("   %d of %d edge series called BUY" % (len(buys), len(edge_calls)))
check("a real edge is still called BUY", len(buys) >= 2,
      [r.plan.action for r in edge_calls])
check("each of those rests on a demonstrated record",
      all(r.calibration.demonstrated for r in buys))
check("which beat buying on any day",
      all(r.calibration.expectancy_r > r.calibration.baseline_expectancy + 0.2
          for r in buys))

noise = [analyse(walk(500 + seed)) for seed in range(30)]
false_buys = [r for r in noise if r.plan.action in ("BUY", "SHORT")]
print("   %d of %d random walks called BUY" % (len(false_buys), len(noise)))
check("on prices with no edge it rarely says buy", len(false_buys) <= 2,
      len(false_buys))
check("every market entry on them still rests on a demonstrated record",
      all(r.calibration.demonstrated for r in false_buys))


# ---------------------------------------------------------------------------
head("PLAIN WORDS FOLLOW THE SIDE")
# ---------------------------------------------------------------------------

class P:
    action = "WAIT"
    direction = 1
    entry, stop, target1 = 100.0, 95.0, 110.0
    probability, prob_samples, prob_reliable = 0.36, 78, True
    prob_low = 0.30
    breakeven_rate = 0.29
    avg_win_r, avg_loss_r = 1.48, -0.52
    reward_risk, tested_reward_risk = 1.5, 1.5
    avoid_reason = ""
    odds_direction = 1
    position = {}


class Bars_:
    currency = "USD"


def said(**kw):
    plan = P()
    for k, v in kw.items():
        setattr(plan, k, v)
    out = plain.explain_plan(plan, Bars_())
    text = " ".join([out["headline"], out["sure"]] + out["points"])
    return out, text


out, text = said(direction=-1, entry=105.0, stop=110.0, target1=95.0,
                 odds_direction=-1)
check("a short WAIT is not explained as a buy",
      "short yet" in out["headline"] and "buy price" not in text, out["headline"])
check("it says to wait for a rise, and to get out if it keeps rising",
      "rise to $105.00" in text and "rising to $110.00" in text, out["points"])
out, text = said(action="SHORT", direction=-1, entry=105.0, stop=110.0,
                 target1=95.0, odds_direction=-1)
check("a SHORT's stop is above it, and said so",
      "rising to $110.00" in text and "fell to" not in text, out["points"])

out, text = said(action="AVOID", direction=0, entry=None, stop=None, target1=None,
                 avoid_reason="bearish_no_shorts", odds_direction=-1)
check("a bearish AVOID says the signals point to a fall",
      "fall" in out["headline"] and "do not buy" in text, out["headline"])
check("and that its record is for a fall, not a reason to buy",
      "record for a fall" in out["sure"] and "cleared that bar" not in out["sure"],
      out["sure"])
check("the counts still open the explanation, as the public page expects",
      out["sure"].startswith("In tests on past prices, this same setup came up 78 "
                             "times and worked 28 of them, reaching its target "
                             "before its stop."), out["sure"])
out, text = said(action="AVOID", direction=0, entry=None, stop=None, target1=None,
                 avoid_reason="no_signal", odds_direction=-1)
check("any AVOID whose odds lean down says what worked means",
      "bet on the price falling" in out["sure"], out["sure"])
out, text = said(action="AVOID", direction=0, entry=None, stop=None, target1=None,
                 avoid_reason="negative_record")
check("an AVOID on a losing record says it lost money",
      "lost money" in text and "normal answer" in text)
out, text = said(action="AVOID", direction=0, entry=None, stop=None, target1=None)
check("an AVOID with nothing to go on keeps its reassurance",
      "normal answer" in text)
out, text = said(reward_risk=1.1)
check("a plan aiming nearer than the tested target says a win would be smaller",
      "nearer" in out["sure"] and "smaller" in out["sure"], out["sure"])
out, text = said(prob_low=0.25)
check("clearing break-even by less than luck moves is said plainly",
      "rule out luck" in out["sure"], out["sure"])
everything = []
for kw in ({"direction": -1, "entry": 105.0, "stop": 110.0, "target1": 95.0},
           {"action": "AVOID", "avoid_reason": "bearish_no_shorts", "odds_direction": -1},
           {"reward_risk": 1.1}, {"prob_low": 0.25}):
    everything.append(said(**kw)[1].lower())
leaked = [w for w in JARGON for t in everything if w in t]
check("none of it uses a word that needs a glossary", not leaked, leaked)


# ---------------------------------------------------------------------------
head("VOLUME LINES DO NOT DEPEND ON WHERE THE HISTORY STARTS")
# ---------------------------------------------------------------------------
# OBV and the A/D line are running totals from the first bar downloaded, and
# two strategies divided by their level, so the same day read differently
# when the five-year window slid by a day.

full = walk(42, n=600)


def cut(bars, k):
    return make_bars(bars.close[k:], bars.open[k:], bars.high[k:], bars.low[k:],
                     bars.volume[k:])


fctx = S.build_context(full)
moved = []
for k in (1, 5, 21, 63):
    sctx = S.build_context(cut(full, k))
    for i in range(300, 600, 7):
        for name in ("obv_divergence", "accumulation_trend"):
            a_ = S.BY_NAME[name].fn(fctx, i).score
            b_ = S.BY_NAME[name].fn(sctx, i - k).score
            if abs(a_ - b_) > 1e-9:
                moved.append((name, k, i, a_, b_))
check("OBV divergence and A/D trend read the same bar the same way", not moved,
      moved[:3])


# ---------------------------------------------------------------------------
head("EVERY STRATEGY COUNT COMES FROM THE REGISTRY")
# ---------------------------------------------------------------------------

daily = walk(3, n=900, vol=0.025)
wctx = S.build_context(daily)
fired = set()
for i in range(wctx.warmup, len(daily)):
    for st in S.REGISTRY:
        sig = st.fn(wctx, i)
        if sig.weight > 0 and abs(sig.score) > 0.05:
            fired.add(st.name)
silent = [n for n in S.daily_strategy_names() if n not in fired]
check("every strategy outside INTRADAY_ONLY fires on daily bars", not silent, silent)
check("and none inside it does",
      not [n for n in S.INTRADAY_ONLY if n in fired], S.INTRADAY_ONLY)

from bot import reasoning, thesis  # noqa: E402

bull, bear_case = thesis.Case("bull"), thesis.Case("bear")
thesis._technical_points({"composite": 0.4, "regime": "trending", "expectancy": 0.1,
                          "hit_rate": 0.4, "samples": 120}, bull, bear_case, "today")
# Only the strategies that vote make the composite: all of them today.
check("the case for a stock counts the strategies it ran",
      bull.points and ("across %d strategies" % len(S.voting_names()))
      in bull.points[0].detail, bull.points[0].detail if bull.points else "")
brief = reasoning.build_payload("TEST", 100.0, plan=clear)
check("and so does the brief given to the reasoning step",
      ("Technical plan (%d-strategy engine)" % len(S.voting_names())) in brief, list(brief))

# The README is the repository's front page. It said 34 for days after 44 voted.
readme = open(_os.path.join(harness.ROOT, "README.md"), encoding="utf-8").read()
said = set(re.findall(r"\b(\d+)(?:\*\*)? (?:trading )?strateg(?:y|ies)\b", readme)) 
check("the README gives the registry's number of strategies",
      str(len(S.REGISTRY)) in said and said <= {str(len(S.REGISTRY))}, sorted(said))
wrong = [(label, n) for label, fam in
         (("Trend", S.TREND), ("Mean reversion", S.REVERSION), ("Breakout", S.BREAKOUT),
          ("Volume", S.VOLUME), ("Structure", S.STRUCTURE), ("Pattern", S.PATTERN),
          ("Liquidity", S.LIQUIDITY))
         for n in [sum(1 for s in S.REGISTRY if s.family == fam)]
         if "**%s (%d)**" % (label, n) not in readme]
check("and each family's own", not wrong, wrong)

# A bearish call's odds are the short side's: a record of betting on a fall
# that paid argues the bear case, and must not be filed as "this has paid".
bull, bear_case = thesis.Case("bull"), thesis.Case("bear")
thesis._technical_points({"composite": -0.4, "regime": "trending", "expectancy": 0.2,
                          "hit_rate": 0.4, "samples": 150}, bull, bear_case, "today")
check("a paying record for a fall is put in the bear case",
      any("fall have paid" in p.headline for p in bear_case.points)
      and not any("has paid" in p.headline for p in bull.points),
      ([p.headline for p in bull.points], [p.headline for p in bear_case.points]))


# ---------------------------------------------------------------------------
head("THE ODDS PAGE SAYS WHAT THE ODDS ARE FOR")
# ---------------------------------------------------------------------------

import web  # noqa: E402
from flask import render_template  # noqa: E402

shown = analyse(edge_series(0))
with web.app.test_request_context("/analyse?symbol=TEST&interval=1d"):
    web.app.preprocess_request()
    page = render_template(
        "analysis.html", r=shown, cfg=CFG, families=web._family_rollup(shown),
        ranked=sorted(shown.signals, key=lambda s: -abs(s.contribution)),
        hold_text="2 weeks", plan=shown.plan, calib=shown.calibration,
        suggestions=[], calendar_rows=[], overlap={"warnings": [], "pairs": []},
        plain=plain.explain_plan(shown.plan, shown.bars), popular_url="")
best = shown.strategy_report.best(minimum=40, limit=5)
check("the weights shown are the ones the call used",
      best and all(("%.2f" % shown.learned[s.name]) in page for s in best))
check("the odds say which side they are for", "long trades only" in page)
check("and what buying on any day made", "Buying any day" in page)

# A deeper pullback turns the signals down; the first such series is used.
bear_r = None
for seed in range(6):
    bear_r = analyse(edge_series(seed, dip=6))
    if bear_r.plan.avoid_reason == "bearish_no_shorts":
        break
if bear_r.plan.avoid_reason == "bearish_no_shorts":
    with web.app.test_request_context("/analyse?symbol=TEST&interval=1d"):
        web.app.preprocess_request()
        page = render_template(
            "analysis.html", r=bear_r, cfg=CFG, families=web._family_rollup(bear_r),
            ranked=bear_r.signals, hold_text="2 weeks", plan=bear_r.plan,
            calib=bear_r.calibration, suggestions=[], calendar_rows=[],
            overlap={"warnings": [], "pairs": []},
            plain=plain.explain_plan(bear_r.plan, bear_r.bars), popular_url="")
    check("a bearish AVOID's page says its odds are for the price falling",
          "These odds are for the price falling" in page)
else:
    check("the bearish example is still bearish", False, bear_r.plan.avoid_reason)


# ---------------------------------------------------------------------------
head("THE SAME DAYS ARE TESTED WHATEVER THE DOWNLOAD STARTED ON")
# ---------------------------------------------------------------------------
# Every third bar was counted from the first one downloaded, so each day, as
# one old bar left the five-year window, a different third of history was
# tested: the same setup read 19 of 47, then 33 of 68, then 19 of 47 again.

import dataclasses  # noqa: E402

MONDAY = 1_600_646_400                                # 21 September 2020, midnight UTC


def stamped(bars, ts):
    ts = np.asarray(ts, dtype=np.int64)
    return dataclasses.replace(bars, timestamp=ts,
                               session_id=((ts - 18000) // DAY).astype(np.int64))


def weekdays(n):
    return [MONDAY + 52200 + (k // 5 * 7 + k % 5) * DAY for k in range(n)]


def later(bars, k):
    """The same bars with the first k gone, as a window that slid on."""
    return dataclasses.replace(bars, **{f: getattr(bars, f)[k:] for f in (
        "timestamp", "open", "high", "low", "close", "volume", "session_id")})


hours = [d - 0 + h * 3600 for d in weekdays(180) for h in range(7)]
fives = [d + j * 300 for d in weekdays(20) for j in range(78)]
nights = [MONDAY + j * 3600 for j in range(1500)]
shapes = {
    "daily": stamped(walk(71, n=1250), weekdays(1250)),
    "hourly": dataclasses.replace(stamped(walk(72, n=len(hours)), hours), interval="1h"),
    "5-minute": dataclasses.replace(stamped(walk(73, n=len(fives)), fives), interval="5m"),
    "coin hourly": dataclasses.replace(stamped(walk(74, n=1500), nights), interval="1h",
                                       asset_class="crypto"),
}
for label, whole in shapes.items():
    sctx = S.Context(whole, {}, warmup=210)
    points = cal.sample_points(sctx, 10, 3)
    at = [int(whole.timestamp[i]) for i in points]
    same = True
    for k in (1, 2, 5):
        part = later(whole, k)
        there = [int(part.timestamp[i])
                 for i in cal.sample_points(S.Context(part, {}, warmup=210), 10, 3)]
        same = same and there == [t for t in at if t >= int(part.timestamp[210])]
    check("%s: a window that slid on tests the same bars" % label, same)
    check("%s: still a third of them" % label,
          abs(len(points) - (len(whole) - 221) / 3.0) <= 2, len(points))
    gaps = set(int(g) for g in np.diff(points))
    check("%s: still every third one" % label,
          gaps == {3} if label in ("daily", "coin hourly") else gaps <= {1, 2, 3, 4, 5}, gaps)
hourly = shapes["hourly"]
check("every hour of the day gets its turn",
      len({int(hourly.timestamp[i]) % DAY for i in
           cal.sample_points(S.Context(hourly, {}, warmup=210), 10, 3)}) == 7)
check("a stride of seven is not one weekday only",
      len({(int(shapes["daily"].timestamp[i]) // DAY) % 7 for i in
           cal.sample_points(S.Context(shapes["daily"], {}, warmup=210), 10, 7)}) == 5)
few = walk(75, n=40)
check("too little history tests nothing", cal.sample_points(S.Context(few, {}, warmup=210), 10) == [])

swings, words = [], []
for seed in (106, 107, 109):
    whole = stamped(walk(seed, n=1000), weekdays(1000))
    runs_ = [analyse(later(whole, k)) for k in (0, 1, 2)]
    rates = [r.calibration.hit_rate or 0.0 for r in runs_]
    swings.append(max(rates) - min(rates))
    words.append(len({r.plan.action for r in runs_}))
check("so a setup's record no longer moves as one old bar leaves", max(swings) < 0.03, swings)
check("and neither does the call", max(words) == 1, words)
from bot import learning  # noqa: E402

lctx = S.build_context(shapes["daily"])
check("the strategies are measured on the very same bars as the odds",
      learning.measure_strategies(lctx, 1.5, 2.25, 10, 3).samples
      == len(cal.sample_points(lctx, 10, 3, 1500)))


# ---------------------------------------------------------------------------
head("ODDS ARE NOT BORROWED FROM THE OTHER SIDE")
# ---------------------------------------------------------------------------
# The learned weights or the news can put the call on the other side from the
# score the replay uses. The page then quoted buys that had scored +0.16 as
# "this same setup" on a day whose own score was -0.16.

cross = cal.calibrate(ctx, -0.16, 1.5, 2.25, 10, direction=1)
check("a long call on a score pointing down has no comparable setups",
      cross.hit_rate is None and cross.bucket_samples == 0 and not cross.reliable
      and not cross.demonstrated and "leans down" in cross.note, cross.note)
crossed = call(cross)
check("its plan says the two readings disagree, and quotes no odds",
      crossed.probability is None and crossed.action == "WAIT"
      and "leans down today" in crossed.edge_verdict, crossed.edge_verdict)
check("a score of nothing still has no side to disagree with",
      cal.calibrate(ctx, 0.0, 1.5, 2.25, 10, direction=1).hit_rate is not None)


# ---------------------------------------------------------------------------
head("A PRICE FAR BELOW A CENT KEEPS ITS LEVELS")
# ---------------------------------------------------------------------------
# Cut to six decimals, a coin at 0.0000024 had an entry of 0.000002, a stop of
# 0.000002 and a target of 0.000003, and its words said to watch for $0.0000.

def shrunk(bars, scale, **kw):
    return dataclasses.replace(bars, open=bars.open * scale, high=bars.high * scale,
                               low=bars.low * scale, close=bars.close * scale, **kw)


def plan_on(bars, composite=0.3, cfg=calm, c=None):
    dc = S.build_context(bars)
    return decision.decide(dc, bars, [], composite, S.classify_regime(dc, len(bars) - 1),
                           c or calib(), FLAT, [], cfg)


for scale in (1e-8, 2.4e-8, 1e-11):
    tiny = shrunk(base_bars, scale, asset_class="crypto")
    p_tiny = plan_on(tiny)
    risk_ = abs(p_tiny.entry - p_tiny.stop)
    true_ = 1.5 * float(S.build_context(tiny).atr[-1])
    check("at %.3g the stop, entry and target stay apart" % tiny.last_price,
          p_tiny.stop < p_tiny.entry < p_tiny.target1
          and abs(risk_ / true_ - 1.0) < 0.05, (p_tiny.entry, p_tiny.stop, true_))
check("and its plain words give a price", plain._money(0.0000123) == "$0.0000123"
      and plain._money(0.0123) == "$0.0123" and plain._money(0.0) == "$0.0000",
      plain._money(0.0000123))
slow = shrunk(base_bars, 1.0)
slow = dataclasses.replace(slow, **{f: 1.08 + (getattr(base_bars, f) - 100.0) * 1e-5
                                    for f in ("open", "high", "low", "close")})
p_slow = plan_on(slow)
check("a price that moves less than the usual tick keeps its levels apart too",
      p_slow.stop < p_slow.entry < p_slow.target1 and "error" not in p_slow.position,
      (p_slow.entry, p_slow.stop, p_slow.position))
check("ordinary prices are rounded as they were",
      decision._round_price(101.2749, 101.0, 2.3) == 101.27
      and decision._round_price(1543.26, 1500.0, 30.0) == 1543.3
      and decision._round_price(12.34567, 12.0, 0.3) == 12.346
      and decision._round_price(0.1234567, 0.12, 0.01) == 0.123457)
from bot import reasoning as _reasoning  # noqa: E402

check("the brief keeps a tiny price", _reasoning.build_payload("T", 0.0000024)
      ["Instrument"]["price"] == "0.0000024"
      and _reasoning.build_payload("T", 182.5)["Instrument"]["price"] == 182.5)


# ---------------------------------------------------------------------------
head("A SIZE IS FOR THE PRICES SHOWN, IN ITS OWN MONEY")
# ---------------------------------------------------------------------------

sized = plan_on(base_bars, c=calib(rate=0.45, lo=0.36, hi=0.54, exp=0.30))
check("the money at risk is that of the rounded prices the review page uses",
      sized.action == "BUY" and abs(sized.position["risk_amount"] - sized.position["quantity"]
                                    * abs(sized.entry - sized.stop)) < 1e-9
      and abs(sized.reward_risk - abs(sized.target1 - sized.entry)
              / abs(sized.entry - sized.stop)) < 1e-12, sized.position)
check("and whole shares are written whole",
      sized.position["quantity_text"] == "%d" % sized.position["quantity"]
      and sized.position["unit_text"] == "units", sized.position["quantity_text"])
check("a dollar price says nothing about currency", not any("priced in" in r for r in sized.risks)
      and not sized.position["account_in"])
yen = dataclasses.replace(base_bars, currency="JPY")
p_yen = plan_on(yen)
check("a price in another currency says what the size assumes",
      p_yen.position["account_in"] == "JPY"
      and any("priced in JPY" in r and "convert it" in r for r in p_yen.risks), p_yen.risks)
dear = decision.size_position(90000.0, 80000.0, yen, CFG)
check("as does a refusal", "counting the account as JPY" in dear["error"], dear)
pence = dataclasses.replace(base_bars, currency="GBp")
check("and pence are called pence", plan_on(pence).position["account_in"] == "pence (GBp)")

es = dataclasses.replace(base_bars, symbol="ES=F", asset_class="other")
rich = dict(CFG, account_size=1_000_000.0, risk_per_trade_pct=1.0)
f_ = decision.size_position(5646.75, 5596.75, es, rich)
check("a futures contract is sized at its dollars a point",
      f_["quantity"] == 4.0 and f_["risk_amount"] == 10000.0
      and f_["notional"] == 4 * 50 * 5646.75 and f_["unit_text"] == "ES contracts", f_)
check("and its gap loss too", f_["gap_loss"] >= f_["risk_amount"])
small = decision.size_position(5646.75, 5596.75, es, CFG)
check("an account one contract is too big for is told so, and of the micro",
      "Not even one ES contract fits" in small["error"] and "MES" in small["error"], small)
odd = decision.size_position(110.0, 109.0, dataclasses.replace(es, symbol="ZB=F"), rich)
check("a contract whose size is not known is not guessed", "size calculator" in odd["error"], odd)
check("and the plain words count contracts", "Buying 4 ES contracts would put" in plain.explain_plan(
    dataclasses.replace(sized, position=f_), es)["risk"])
check("a size that was refused is not said to be at risk", plain.explain_plan(
    dataclasses.replace(sized, position=dear), yen)["risk"] == "")


# ---------------------------------------------------------------------------
head("A PRICE THAT HAS NOT MOVED SAYS NOTHING")
# ---------------------------------------------------------------------------
# Twenty flat bars leave a spread of about 1e-14 from rounding. Divided by, it
# read as "+60 standard deviations", CCI 4000 and "overbought on heavy flow",
# and a fund at a constant price was called strongly bearish.

from bot import indicators as ind  # noqa: E402

moved_then = walk(81, n=600)
for f in ("open", "high", "low", "close"):
    getattr(moved_then, f)[-25:] = 97.3
stillc = S.build_context(moved_then)
whole_flat = S.build_context(make_bars(np.full(300, 10.1), np.full(300, 10.1),
                                       np.full(300, 10.1), np.full(300, 10.1)))
for label, fc in (("a price that stopped moving", stillc), ("one that never moved", whole_flat)):
    last_ = len(fc.bars) - 1
    check("%s: no extreme readings" % label,
          fc.at("zscore", last_) == 0.0 and fc.at("cci", last_) == 0.0
          and fc.at("pct_b", last_) == 0.5 and fc.at("mfi", last_) == 50.0,
          [fc.at(k, last_) for k in ("zscore", "cci", "pct_b", "mfi")])
    sigs_ = {s.name: s for s in S.evaluate(fc, last_)[0]}
    check("%s: none of the four calls it overbought" % label,
          all(abs(sigs_[n].score) < 1e-9 for n in (
              "zscore_reversion", "cci_reversion", "mfi_extremes", "bollinger_fade")),
          [(n, sigs_[n].score) for n in ("zscore_reversion", "cci_reversion",
                                         "mfi_extremes", "bollinger_fade")])
check("a price that never moved has an RSI of 50 and no trend",
      whole_flat.at("rsi", 299) == 50.0 and whole_flat.at("adx", 299) == 0.0)
check("one tick in a dear price is still a movement",
      abs(ind.zscore(np.concatenate([np.full(30, 98000.0), [98000.01]]))[-1]) > 4)

check("an average over a missing value is missing, not low",
      np.isnan(ind.sma(np.array([np.nan, 1.0, 1.0, 1.0]), 3)[2])
      and ind.sma(np.array([np.nan, 1.0, 1.0, 1.0]), 3)[3] == 1.0)
young = walk(82, n=60)
k_, d_ = ind.stochastic(young.high, young.low, young.close, 14, 3)
check("so %D waits for three readings of %K", np.isnan(d_[14]) and np.isfinite(d_[15])
      and abs(d_[15] - k_[13:16].mean()) < 1e-9)
check("and a distance from the mean waits for the mean",
      np.isnan(ind.zscore(young.close)[18]) and np.isfinite(ind.zscore(young.close)[19]))
long_ = walk(83, n=400)
far = ind.adx(long_.high, long_.low, long_.close)[0][-1]
near = ind.adx(long_.high[-60:], long_.low[-60:], long_.close[-60:])[0]
check("ADX starts from its first real readings",
      np.isnan(near[25]) and np.isfinite(near[26]) and np.isfinite(far))
ratios = []
for seed in range(20):
    w_ = walk(900 + seed, n=400)
    ratios.append(ind.adx(w_.high[-30:], w_.low[-30:], w_.close[-30:])[0][-1]
                  / ind.adx(w_.high, w_.low, w_.close)[0][-1])
check("and no longer reads low on a young listing", 0.9 < np.mean(ratios) < 1.3, np.mean(ratios))


# ---------------------------------------------------------------------------
head("THE OPENING RANGE ON LONGER BARS")
# ---------------------------------------------------------------------------
# It needed three bars inside the first half hour, so on 15- and 30-minute
# bars it said "not yet complete" all day.

def opening(step, interval, per_day):
    ts_ = [d + j * step for d in weekdays(30) for j in range(per_day)]
    b_ = dataclasses.replace(stamped(walk(91, n=len(ts_)), ts_), interval=interval)
    c_ = S.build_context(b_)
    return [(i % per_day, S.opening_range_break(c_, i)) for i in range(per_day * 5, len(ts_))]


for step_, interval_, per_ in ((300, "5m", 78), (900, "15m", 26), (1800, "30m", 13)):
    got = opening(step_, interval_, per_)
    inside = 1800 // step_
    check("on %s bars it fires once the half hour is over" % interval_,
          any(abs(s_.score) > 0.05 for pos, s_ in got if pos >= inside)
          and all(s_.weight == 0 for pos, s_ in got if pos < inside)
          and all(s_.weight > 0 for pos, s_ in got if pos >= inside))
got = opening(3600, "1h", 7)
check("on hourly bars it says what it needs", all(
    s_.weight == 0 and "30 minutes or shorter" in s_.reason for _, s_ in got))


# ---------------------------------------------------------------------------
head("WHAT A RUN WRITES DOWN")
# ---------------------------------------------------------------------------
# The strategy record is one table everyone reads. Every analysis rewrote it
# under whoever's settings it ran with, so one person's odd stop put nonsense
# on the Strategies page for all.

import logging  # noqa: E402

from bot import database as db  # noqa: E402

HOUSE = config.load()


def recorded(bars, cfg, interval="1d"):
    _FEED["bars"] = bars
    return engine.analyse("TEST", cfg, interval=interval, with_news=False, record=True,
                          db_path=harness.DB_PATH)


def stored():
    conn_ = db.connect(harness.DB_PATH)
    try:
        return ([tuple(r) for r in conn_.execute(
                    "SELECT strategy, regime, n, wins, sum_r, sum_r_gross FROM strategy_stats "
                    "WHERE symbol='TEST' ORDER BY strategy, regime")], db.stats(conn_))
    finally:
        conn_.close()


recorded(walk(61, n=700), dict(HOUSE))
first, counts = stored()
check("a run on the house settings writes the shared record", len(first) > 20, len(first))
for what, odd_ in (("stop", {"stop_atr_multiple": 0.05}), ("target", {"target_atr_multiple": 40.0}),
                   ("costs", {"cost_bps_equity": 300.0}), ("holding window", {"horizon_bars": 40}),
                   ("weights", {"strategy_weights": {"trend_stack": 0.0}})):
    check("one with its own %s does not count as the house's" % what,
          not engine.house_method(dict(HOUSE, **odd_), "TEST", "equity", "1d"))
r_ = recorded(walk(61, n=700), dict(HOUSE, stop_atr_multiple=0.05))
check("it is recorded, and leaves the shared record alone",
      r_.run_id is not None and stored()[0] == first)
check("settings the measuring does not use still count as the house's", engine.house_method(
    dict(HOUSE, account_size=77.0, horizon_bars=10, cost_bps_crypto=99.0, allow_shorts=True),
    "TEST", "equity", "1d"))
check("and a coin's costs matter to a coin", not engine.house_method(
    dict(HOUSE, cost_bps_crypto=99.0), "BTC-USD", "crypto", "1d"))
check("a run stores neither its scores nor its headlines, which nothing read",
      counts["runs"] >= 1 and counts["run_signals"] == 0 and counts["run_news"] == 0, counts)

conn = db.connect(harness.DB_PATH)
conn.execute("INSERT INTO strategy_stats (symbol, interval, strategy, family, regime, n, wins, sum_r, "
             "sum_r_gross, sum_bars, cost_r, updated_ts) VALUES ('TEST','1d','old_name','Trend','all',"
             "500,400,900,900,1,0,1)")
conn.execute("INSERT INTO strategy_stats (symbol, interval, strategy, family, regime, n, wins, sum_r, "
             "sum_r_gross, sum_bars, cost_r, updated_ts) VALUES ('OTHER','1d','old_name','Trend','all',"
             "500,400,900,900,1,0,1)")
conn.commit()
check("a measurement with nothing in it replaces nothing",
      db.save_strategy_stats(conn, "TEST", "1d", learning.StrategyReport()) == 0
      and len(stored()[0]) == len(first) + 1)
conn.close()
recorded(walk(61, n=700), dict(HOUSE))
conn = db.connect(harness.DB_PATH)
check("a strategy that is gone does not keep its old figures, on that instrument only",
      stored()[0] == first and conn.execute(
          "SELECT COUNT(*) FROM strategy_stats WHERE symbol='OTHER'").fetchone()[0] == 1)
conn.close()

heard = []


class Ear(logging.Handler):
    def emit(self, record_):
        heard.append(record_.getMessage())


ear, real_record = Ear(), db.record_run
engine.log.addHandler(ear)


def full_disk(*a, **k):
    raise RuntimeError("database or disk is full")


db.record_run = full_disk
try:
    lost_run = recorded(walk(61, n=700), dict(HOUSE))
finally:
    db.record_run = real_record
    engine.log.removeHandler(ear)
check("a run that could not be written keeps its analysis, and is in the log",
      lost_run.run_id is None and lost_run.plan is not None
      and any("Could not record the analysis of TEST" in h for h in heard), heard)

# What a call could not rest on is said on its page.
short_r = analyse(walk(62, n=120))
check("a short history is said to be one",
      any("Only 120 bars of history" in r for r in short_r.plan.risks), short_r.plan.risks)
long_r = r_
check("a long one is not", not any("bars of history" in r for r in long_r.plan.risks)
      and not any("could not be read" in r for r in long_r.plan.risks))
from bot import liquidity  # noqa: E402

real_series, real_level = liquidity.series, S.log.level
S.log.setLevel(logging.CRITICAL + 1)


def unreadable(bars, k=2):
    raise ValueError("made to fail")


liquidity.series = unreadable
try:
    blind = analyse(walk(62, n=700))
finally:
    liquidity.series = real_series
    S.log.setLevel(real_level)
check("price structure that could not be read is said on the page",
      any("price structure could not be read" in r for r in blind.plan.risks), blind.plan.risks)


# ---------------------------------------------------------------------------
head("THE WRITTEN REASONING'S FIGURES")
# ---------------------------------------------------------------------------
# The check read a range's hyphen as a minus sign, so a correct answer was
# shown with a warning about figures nobody had made up.

FACTS = ("- Net margin by year: FY2021 25.9%, FY2024 24.0%\n- price: 182.50\n- high: 231.00\n"
         "- Revenue: FY2022 383.3B, FY2024 391.0B\n- worst fall: -32.0%\n")


def flagged(words):
    return _reasoning.verify(_reasoning.Reasoning(True, summary=words), FACTS).unverified


check("a range is not a minus sign", flagged(
    "Margins of 25.9%-24.0% over FY2021-2024, a price of $182.50-$231.00, sales of "
    "383.3B-391.0B, in line with the S&P 500.") == [], flagged(
        "Margins of 25.9%-24.0% over FY2021-2024, a price of $182.50-$231.00, sales of "
        "383.3B-391.0B, in line with the S&P 500."))
check("a fall may be said without its sign", flagged("It fell 32.0% at worst, -32.0% in all.") == [])
check("a figure that is nowhere in the evidence is still flagged",
      flagged("Margins of 25.9%-61.0%, and 47.5% growth.") == ["61.0%", "47.5%"],
      flagged("Margins of 25.9%-61.0%, and 47.5% growth."))


def replied(text, stop):
    block = types.SimpleNamespace(type="text", text=text)
    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **k: types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(content=[block], stop_reason=stop)))
    had, was, key = _sys.modules.get("anthropic"), _reasoning.available, _os.environ.get(
        "ANTHROPIC_API_KEY")
    level_ = _reasoning.log.level
    _reasoning.log.setLevel(logging.CRITICAL + 1)
    _sys.modules["anthropic"], _reasoning.available = fake, lambda: True
    _os.environ["ANTHROPIC_API_KEY"] = "made up for this test"
    try:
        return _reasoning.analyse({"Instrument": {"symbol": "T", "price": 10.0}})
    finally:
        _reasoning.available = was
        _reasoning.log.setLevel(level_)
        _sys.modules.pop("anthropic")
        if had is not None:
            _sys.modules["anthropic"] = had
        _os.environ.pop("ANTHROPIC_API_KEY")
        if key is not None:
            _os.environ["ANTHROPIC_API_KEY"] = key


cut_off = replied('{"summary": "half an ans', "max_tokens")
check("an answer cut off at the limit is known for what it is",
      not cut_off.available and "cut off" in cut_off.detail
      and "could not be produced just now" in cut_off.reason_unavailable, cut_off.detail)
check("as is a refusal", "declined" in replied("No.", "refusal").detail)
check("and a whole answer is read", replied('{"summary": "Fine."}', "end_turn").summary == "Fine.")


# ---------------------------------------------------------------------------
head("A FUTURES PLAN IN THE JOURNAL")
# ---------------------------------------------------------------------------

import web  # noqa: E402

es_row = {"symbol": "ES=F", "entry": 5646.75, "stop": 5596.75, "target1": 5746.75,
          "target2": None, "action": "BUY", "asset_class": "other"}
paper = web._paper_size(es_row, rich)
check("the review sizes the same contracts, each counted as its dollars a point",
      paper["quantity"] == 200.0 and paper["risk"] == 10000.0
      and paper["contract_words"] == "4 ES contracts" and paper["reward_risk"] == 2.0, paper)
try:
    web._paper_size(es_row, CFG)
    check("and refuses what one contract is too big for", False)
except web._NoTrade as exc:
    check("and refuses what one contract is too big for", "Not even one ES contract" in str(exc))


print()
print("%d failure(s)" % len(fails))
print("METHOD OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
