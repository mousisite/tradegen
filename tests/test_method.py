"""How calls, odds and back-tests are worked out, on prices made up for the test.

No network: market data is synthetic, and where a real download would happen
the payload is built by hand. Each section pins one way the method used to
mislead, and says how.
"""
import os as _os
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
head("A COMPANY'S FIGURES STAY IN ONE CURRENCY")
# ---------------------------------------------------------------------------
# A US-listed foreign company reports in its own currency and is priced in
# dollars. Dividing one by the other gave a business worth 100 a share a "fair
# value" of 3,484 and a free cash flow yield of 192%.

from bot import fundamentals as F  # noqa: E402
from bot import options as O  # noqa: E402
from bot import quality as Q  # noqa: E402
from bot import risk as R  # noqa: E402
from bot import sec, upstream, yahoo  # noqa: E402
from bot import valuation as V  # noqa: E402


def company(kept="USD", priced="USD", rate=1.0, shares=1e9, cap=1e11):
    """What Yahoo sends for one company, its accounts at `rate` to the dollar."""
    return {
        "price": {"longName": "Test Corp", "currency": priced},
        "assetProfile": {"sector": "Technology"},
        "summaryDetail": {"marketCap": cap, "trailingPE": 20.0, "currency": priced},
        "defaultKeyStatistics": {"sharesOutstanding": shares, "beta": 1.0,
                                 "priceToBook": 4.0, "forwardPE": 18.0,
                                 "enterpriseToEbitda": 0.4, "enterpriseValue": 9e10},
        "financialData": {"financialCurrency": kept, "currentPrice": 100.0,
                          "totalRevenue": 3e10 * rate, "freeCashflow": 6e9 * rate,
                          "totalCash": 1e10 * rate, "totalDebt": 5e9 * rate,
                          "revenueGrowth": 0.08, "profitMargins": 0.2}}


def loaded(**kw):
    real_summary = yahoo.quote_summary
    yahoo.quote_summary = lambda symbol, modules: company(**kw)
    try:
        return F.load("TEST", with_sec=False)
    finally:
        yahoo.quote_summary = real_summary


def valued(f):
    return [V.discounted_cash_flow(f, 100.0), V.reverse_dcf(f, 100.0),
            V.multiples(f, 100.0)]


home, away = loaded(), loaded(kept="TWD", rate=32.0)
h_dcf, h_rev, h_mult = valued(home)
a_dcf, a_rev, a_mult = valued(away)
check("accounts and price in dollars are valued as before",
      h_dcf.usable and h_rev.usable and 50 < h_dcf.fair_value < 300
      and abs(home.get("fcf_yield") - 0.06) < 1e-9
      and not home.currency_problem() and home.show("revenue") == "30.00B",
      (h_dcf.fair_value, home.get("fcf_yield"), home.show("revenue")))
check("accounts in another currency are not divided by a dollar price",
      not a_dcf.usable and not a_rev.usable and a_dcf.fair_value is None
      and "TWD" in a_dcf.reason and "USD" in a_dcf.reason and "TWD" in a_rev.reason,
      (a_dcf.fair_value, a_dcf.reason))
check("nor made into a yield or a ratio that crosses the two",
      away.get("fcf_yield") is None and away.get("pb") is None
      and away.get("ev_ebitda") is None and away.get("enterprise_value") is None
      and away.get("pe") == 20.0)
check("the money rows say what they are in",
      away.show("revenue") == "960.00B TWD" and away.show("market_cap") == "100.00B USD",
      (away.show("revenue"), away.show("market_cap")))
check("and the page says why, beside the figures",
      any("TWD" in n and "USD" in n for n in away.notes), away.notes)
check("with no model left, the reason is given rather than 'no data'",
      "TWD" in V.combine([a_dcf, a_rev], 100.0)["reason"])
from bot import thesis  # noqa: E402
said_both = thesis.build("TEST", 100.0, fundamentals=away,
                         valuation=V.combine([a_dcf, a_rev, a_mult], 100.0))
check("the case names it among what could not be measured",
      any("TWD" in u for u in said_both.unknowns), said_both.unknowns)
check("and gives its cash flow in its own currency",
      any("192.00B TWD" in p.detail for p in said_both.bull.points),
      [p.detail for p in said_both.bull.points])
pence = loaded(kept="GBP", priced="GBp")
check("a price in pence is not set against accounts in pounds",
      not V.discounted_cash_flow(pence, 7200.0).usable
      and "pence" in pence.currency_problem())
check("while its market value, which is in pounds, still is",
      pence.get("fcf_yield") is not None and pence.show("market_cap") == "100.00B GBP",
      pence.show("market_cap"))


class Filed:
    """Filed cash flows, in whichever currency they were filed."""

    def __init__(self, unit, scale):
        self.unit, self.scale = unit, scale

    def series(self, concept, years):
        value = {"operating_cashflow": 8e9, "capex": 2e9}[concept] * self.scale
        return [{"fy": y, "value": value, "unit": self.unit} for y in (2023, 2024, 2025)]


home.sec_facts = Filed("JPY", 150.0)
check("filed cash flow in another currency does not set the starting figure",
      V.normalised_fcf(home)["value"] == 6e9, V.normalised_fcf(home)["value"])
home.sec_facts = Filed("USD", 1.0)
check("filed cash flow in the same one does",
      "filed" in V.normalised_fcf(home)["source"])
home.sec_facts = None

# Yahoo's share count is the listed class alone; the cash flow covers all.
classes = loaded(shares=5e8)
check("every share class counts, in both models",
      abs(V.discounted_cash_flow(classes, 100.0).fair_value - h_dcf.fair_value) < 1e-6
      and abs(V.reverse_dcf(classes, 100.0).implied_growth - h_rev.implied_growth) < 1e-6,
      (V.discounted_cash_flow(classes, 100.0).fair_value, h_dcf.fair_value))
check("and where the count came from is said",
      any("every share class" in a_.source
          for a_ in V.discounted_cash_flow(classes, 100.0).assumptions))


def told(table, **kw):
    history = {"available": True, "table": table, "years": [2024, 2025],
               "currency": "USD"}
    out = plain.explain_company(types.SimpleNamespace(
        sec_history=dict(history, **kw), fundamentals=None, scores=None, valuation=None))
    return " ".join(out["points"])


def rows_of(unit, **by_year):
    return [{"fy": int(y[1:]), "value": v, "unit": unit} for y, v in by_year.items()]


words = told({"revenue": rows_of("USD", y2024=9e9, y2025=1e10),
              "net_income": rows_of("USD", y2024=1e9, y2025=2e9)})
check("plain words: dollars, and cents of every dollar",
      "In its 2025 financial year it sold $10.0 billion" in words
      and "about 20 cents of every dollar" in words, words)
words = told({"revenue": rows_of("JPY", y2025=4.5e13),
              "net_income": rows_of("JPY", y2025=4.5e12)}, currency="JPY")
check("plain words: yen are called yen, and the share is of what it sold",
      "45.0 trillion Japanese yen" in words and "about 10% of what it sold" in words
      and "$" not in words and "cents" not in words, words)
words = told({"revenue": rows_of("JPY", y2025=4.5e13),
              "net_income": rows_of("USD", y2025=3e10)}, currency="", mixed_currency=True)
check("plain words: no share is worked out across two currencies",
      "Japanese yen" in words and "kept" not in words, words)
words = told({"revenue": rows_of("USD", y2016=1.8e9, y2017=2e9),
              "net_income": rows_of("USD", y2017=1e8, y2025=1.5e9)})
check("plain words: the profit and the sales are of one year, and it is named",
      "In its 2017 financial year" in words and "about 5 cents" in words
      and "Last year" not in words, words)
words = told({"revenue": rows_of("USD", y2017=2e9),
              "net_income": rows_of("USD", y2025=1.5e9)})
check("plain words: with no year shared nothing is said", words == "", words)


# ---------------------------------------------------------------------------
head("THE SCORES JUDGE WHAT THEY COULD MEASURE")
# ---------------------------------------------------------------------------

def filings(**series):
    years = sorted({y for values in series.values() for y in values})
    return {"available": True, "years": years, "currency": "USD", "table": {
        key: [{"fy": y, "value": v, "unit": "USD"} for y, v in sorted(values.items())]
        for key, values in series.items()}}


# Six measurable tests: profitable, cash generative and backed by cash pass;
# returns, margins and asset turn fail. Judged out of nine, this was "weak".
half = filings(net_income={2024: 110.0, 2025: 100.0},
               assets={2024: 1000.0, 2025: 1000.0},
               operating_cashflow={2024: 150.0, 2025: 150.0},
               revenue={2024: 1000.0, 2025: 950.0},
               gross_profit={2024: 400.0, 2025: 350.0})
score = Q.piotroski(half)
check("three passed of six is not called weak",
      score.value == 3.0 and score.measured == 6 and "Mixed" in score.verdict
      and Q.strength_band(score) == "mixed", (score.value, score.verdict))
check("and its scale is the tests that could be measured", "0 to 6" in score.scale,
      score.scale)
judged = Q.assess(half)
check("nor listed as a concern out of nine", not judged["concerns"], judged["concerns"])
good = Q.assess(filings(net_income={2024: 100.0, 2025: 110.0},
                        assets={2024: 1000.0, 2025: 1000.0},
                        operating_cashflow={2024: 150.0, 2025: 160.0},
                        revenue={2024: 1000.0, 2025: 1100.0},
                        gross_profit={2024: 400.0, 2025: 460.0}))
check("six passed of six is a strength, and worded as it was measured",
      any("6 of the 6 measures that could be checked" in s for s in good["strengths"]),
      good["strengths"])
bank = Q.assess(half, 2000.0, "Financial Services")
check("a bank is refused all four, each with its reason",
      all(not s.usable and "financial services" in s.verdict
          for s in bank["scores"].values()),
      [(k, s.value, s.verdict) for k, s in bank["scores"].items()])
check("and the headline does not say they ran",
      "None of these models could be used" in bank["headline"], bank["headline"])

from bot import ideas  # noqa: E402

real_hist, real_load = sec.financial_history, F.load
try:
    sec.financial_history = lambda symbol, years=6: half
    F.load = lambda symbol, with_sec=True: types.SimpleNamespace(
        notes=[], sector="Technology", get=lambda key: None)
    kept = ideas._accounts_hold_up("HALF")
    F.load = lambda symbol, with_sec=True: types.SimpleNamespace(
        notes=[], sector="Financial Services", get=lambda key: None)
    lender = ideas._accounts_hold_up("BANK")
finally:
    sec.financial_history, F.load = real_hist, real_load
check("Find does not drop the half-measured company", kept["ok"] is True, kept)
check("and calls a bank's accounts unjudged, not sound and not bad",
      lender["ok"] is None and lender.get("unjudged") and not lender["reasons"], lender)
real_checked = ideas._check_accounts
ideas._check_accounts = lambda symbols, *_a, **_k: {"BANK": lender}
try:
    listed = ideas._shortlist(
        {"market": "stocks", "interval": "1d", "asked_for": 1, "cut_short": False,
         "seconds": 1.0, "scanned_at": time.time(),
         "rows": [{"symbol": "BANK", "name": "Bank", "action": "BUY", "price": 100.0,
                   "asset_class": "equity", "expectancy": 0.2, "conviction": 0.5,
                   "entry": 100.0, "stop": 95.0, "target1": 110.0, "probability": 0.5,
                   "samples": 100, "reliable": True, "demonstrated": True,
                   "gap_multiple": 1.2, "direction": 1, "edge_margin": 0.1,
                   "breakeven": 0.3, "risks": [], "volume_ok": True}]},
        "long", "balanced")["passed"]
finally:
    ideas._check_accounts = real_checked
check("and keeps it in the list under that label",
      [r.get("accounts") for r in listed] == ["not judged"], listed)

# Cash a fifth ahead of profit, receivables far faster than sales.
flagged = dict(revenue={2024: 1000.0, 2025: 1600.0}, receivables={2024: 100.0, 2025: 300.0},
               gross_profit={2024: 400.0, 2025: 500.0},
               current_assets={2024: 300.0, 2025: 300.0}, ppe_net={2024: 500.0, 2025: 500.0},
               assets={2024: 1000.0, 2025: 1000.0}, depreciation={2024: 50.0, 2025: 50.0},
               sga_expense={2024: 200.0, 2025: 200.0},
               current_liabilities={2024: 200.0, 2025: 200.0},
               long_term_debt={2024: 100.0, 2025: 100.0},
               net_income={2024: 100.0, 2025: 100.0},
               operating_cashflow={2024: 100.0, 2025: 110.0})
m = Q.beneish_m(filings(**flagged))
check("the ratios named are the ones that pull the score up",
      m.value > -1.78 and "receivables against sales" in m.verdict
      and "accruals" not in m.verdict, (m.value, m.verdict))
flagged["operating_cashflow"] = {2024: 100.0, 2025: -300.0}
m = Q.beneish_m(filings(**flagged))
check("and accruals when they are one", "accruals" in m.verdict, m.verdict)


def cash_story(profit, cash):
    flat_assets = {2024: 1000.0, 2025: 1000.0}
    return Q.assess(filings(net_income={2024: 1.0, 2025: profit}, assets=flat_assets,
                            operating_cashflow={2024: 1.0, 2025: cash}))


for profit, cash, words, strong in (
        (-200.0, -150.0, "cash going out was smaller than the reported loss", False),
        (-100.0, -150.0, "cash going out was larger than the reported loss", False),
        (-200.0, 50.0, "still brought in more cash", True),
        (100.0, -20.0, "used up cash", False),
        (100.0, 150.0, "good direction", True)):
    out = cash_story(profit, cash)
    said_ = out["scores"]["accruals"].verdict
    check("a result of %+.0f with %+.0f of cash is told straight" % (profit, cash),
          words in said_ and ("good direction" in said_) == (profit > 0 and cash > profit)
          and bool(out["strengths"]) == strong, (said_, out["strengths"]))
out = cash_story(-50.0, -300.0)
check("a loss with more cash going out is not 'profit ahead of cash'",
      out["concerns"] and "profit" not in out["concerns"][0], out["concerns"])


# ---------------------------------------------------------------------------
head("RISK OVER THE DAYS A THING TRADES, WITH ITS DIVIDENDS")
# ---------------------------------------------------------------------------

def risk_of(bars, **kw):
    real_fetch = R.fetch_bars
    R.fetch_bars = lambda symbol, interval: bars
    try:
        return R.profile("TEST", bars.interval, benchmark="", **kw)
    finally:
        R.fetch_bars = real_fetch


rng = np.random.RandomState(5)
moves = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, 900)))
stock, coin = risk_of(make_bars(moves)), risk_of(make_bars(moves, asset="crypto"))
daily_dev = float(np.std(np.diff(moves) / moves[:-1], ddof=1))
check("a stock's year is 252 days",
      abs(stock.volatility - daily_dev * 252 ** 0.5) < 1e-9, stock.volatility)
check("a coin's is all 365", abs(coin.volatility - daily_dev * 365 ** 0.5) < 1e-9,
      (coin.volatility, daily_dev * 365 ** 0.5))
check("so is the return its Sharpe ratio sets against it",
      abs(coin.sharpe - (np.mean(np.diff(moves) / moves[:-1]) * 365 - 0.042)
          / coin.volatility) < 1e-9, coin.sharpe)
hourly = risk_of(make_bars(moves, asset="crypto", step=3600, interval="1h"))
check("and its hours are all 24",
      abs(hourly.volatility - daily_dev * (365 * 24) ** 0.5) < 1e-9, hourly.volatility)
check("a typical day's move is the same from either",
      abs(R.position_risk(100, 94, 1, 1000, coin)["stop_in_daily_moves"]
          - R.position_risk(100, 94, 1, 1000, stock)["stop_in_daily_moves"]) < 1e-9)

# Flat but for a 1.25% drop each quarter, which is the dividend being paid.
steps = np.ones(756)
steps[62::63] = 1 - 0.0125
payer = 100.0 * np.cumprod(steps * np.exp(rng.normal(0.0, 0.004, 756)))
factor = np.array([np.prod(steps[k + 1:]) for k in range(756)])
bare, whole = risk_of(make_bars(payer)), risk_of(make_bars(payer, factor=factor))
check("an ex-dividend drop is not counted as a loss",
      whole.sharpe > bare.sharpe + 0.4 and whole.worst_day > bare.worst_day,
      (bare.sharpe, whole.sharpe))
paid = make_bars(payer, factor=factor)
check("nor when two instruments are compared day by day",
      np.allclose(R.aligned_returns([paid, paid])[0], R.daily_returns(paid)))


# ---------------------------------------------------------------------------
head("OPTIONS RUN TO THE CLOSE OF THEIR LAST DAY")
# ---------------------------------------------------------------------------

import calendar as _calendar  # noqa: E402


def chain_at(now, expiry, vol=0.30):
    """A chain whose every price is worth exactly `vol`, read at `now`."""
    years = (O.clock.ny(*expiry, 16, 0) - now) / (365.0 * DAY)
    stamp = _calendar.timegm((*expiry, 0, 0, 0))

    def row(strike, call):
        worth = O.black_scholes(100.0, strike, years, 0.042, vol, call)["price"]
        return {"strike": strike, "bid": worth, "ask": worth, "openInterest": 100}

    sent = {"quote": {"regularMarketPrice": 100.0}, "expirationDates": [stamp],
            "options": [{"expirationDate": stamp,
                         "calls": [row(s, True) for s in (95.0, 100.0, 105.0)],
                         "puts": [row(s, False) for s in (95.0, 100.0, 105.0)]}]}
    real_o = (O.yahoo.options, O.risk_free_rate, O.time)
    O.yahoo.options = lambda symbol, expiry=None: sent
    O.risk_free_rate = lambda default=0.042: 0.042
    O.time = types.SimpleNamespace(time=lambda: now, gmtime=time.gmtime,
                                   strftime=time.strftime)
    try:
        return O.load_chain("TEST")
    finally:
        O.yahoo.options, O.risk_free_rate, O.time = real_o


for when, expiry, out_ in (((2026, 11, 12, 10, 0), (2026, 11, 20), 8),
                           ((2026, 11, 18, 10, 0), (2026, 11, 20), 2),
                           ((2026, 11, 20, 10, 0), (2026, 11, 20), 0),
                           ((2026, 11, 11, 21, 0), (2026, 11, 20), 9),
                           ((2026, 7, 8, 15, 0), (2026, 7, 17), 9)):
    chain = chain_at(O.clock.ny(*when), expiry)
    iv = O.analyse(chain)["atm_iv"]
    check("priced at 30%%, %d days out, it reads 30%%" % out_,
          iv is not None and abs(iv - 0.30) < 0.001 and chain.days == out_
          and chain.calls[1].delta > 0, (iv, chain.days))
check("after the close of the last day there is no time left to price",
      chain_at(O.clock.ny(2026, 11, 20, 15, 59), (2026, 11, 20)).days == 0)


# ---------------------------------------------------------------------------
head("EDGAR: ONE DOWNLOAD, THE NEWEST TAG, AND NO LONG WAITS")
# ---------------------------------------------------------------------------

def annual(tag_years, unit="USD"):
    return {"units": {unit: [
        {"start": "%d-01-01" % y, "end": "%d-12-31" % y, "val": v, "form": "10-K",
         "fp": "FY", "filed": "%d-02-15" % (y + 1), "accn": "x"}
        for y, v in tag_years.items()]}}


def quarter(value):
    return {"units": {"USD": [{"start": "2025-01-01", "end": "2025-03-31", "val": value,
                               "form": "10-Q", "fp": "Q1", "filed": "2025-05-01"}]}}


def facts_from(cik, tags, get=None):
    real_sec = (sec._get, sec.cik_for)
    sec._get = get or (lambda url, params=None, tries=2: {
        "entityName": "Test Co", "facts": {"us-gaap": tags}})
    sec.cik_for = lambda symbol: cik
    sec._FACTS_CACHE.pop(cik, None)
    try:
        return sec.company_facts("TEST")
    finally:
        sec._get, sec.cik_for = real_sec
        sec._FACTS_CACHE.pop(cik, None)


FIRST, LATER = sec._CONCEPTS["revenue"][0], sec._CONCEPTS["revenue"][2]
moved = facts_from(980001, {FIRST: annual({2018: 0.9e9, 2019: 1.0e9}),
                            LATER: annual({y: 2e9 + y for y in range(2019, 2026)})})
series = moved.series("revenue", 10)
check("a company that moved to another tag keeps its recent years",
      [r["fy"] for r in series] == list(range(2018, 2026))
      and moved.latest("revenue")["value"] == 2e9 + 2025, series)
check("and neighbouring years come from the tag in use",
      series[1]["value"] == 2e9 + 2019 and series[0]["value"] == 0.9e9, series[:2])
both_tags = facts_from(980002, {FIRST: annual({2024: 1.0, 2025: 2.0}),
                                LATER: annual({2023: 7.0, 2024: 8.0, 2025: 9.0})})
check("with both in use the preferred one still wins",
      [r["value"] for r in both_tags.series("revenue")] == [7.0, 1.0, 2.0],
      both_tags.series("revenue"))
check("a tag with only quarters no longer hides another's years",
      facts_from(980003, {FIRST: quarter(5.0), LATER: annual({2024: 8.0, 2025: 9.0})})
      .latest("revenue")["value"] == 9.0)
crossed = facts_from(980004, {FIRST: annual({2018: 3e11, 2019: 3e11}, unit="JPY"),
                              LATER: annual({2024: 8.0, 2025: 9.0})})
check("and years in another currency are never joined on",
      crossed.currencies(["revenue"]) == {"USD"}
      and [r["fy"] for r in crossed.series("revenue")] == [2024, 2025],
      crossed.series("revenue"))

import threading  # noqa: E402

asked, together = [], threading.Barrier(2)


def slow_get(url, params=None, tries=2):
    asked.append(url)
    time.sleep(0.3)
    return {"entityName": "Test Co", "facts": {"us-gaap": {LATER: annual({2024: 8.0, 2025: 9.0})}}}


def refusing_get(url, params=None, tries=2):
    asked.append(url)
    time.sleep(0.3)
    raise sec.SECError("EDGAR request failed: HTTP 503")


def two_at_once(get):
    real_sec = (sec._get, sec.cik_for)
    sec._get, sec.cik_for = get, lambda symbol: 980005
    sec._FACTS_CACHE.pop(980005, None)
    asked.clear()
    got = []

    def one():
        together.wait(5)
        try:
            got.append(sec.company_facts("TEST").entity)
        except sec.SECError:
            got.append("refused")

    threads = [threading.Thread(target=one) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        sec._get, sec.cik_for = real_sec
        sec._FACTS_CACHE.pop(980005, None)
    return got


check("one page's two readers share one download",
      two_at_once(slow_get) == ["Test Co", "Test Co"] and len(asked) == 1, asked)
check("a refusal is shared too, not asked again by each",
      two_at_once(refusing_get) == ["refused", "refused"] and len(asked) == 1, asked)
check("and nothing is left waiting", not sec._FACTS_FLIGHTS, sec._FACTS_FLIGHTS)


class Answer:
    def __init__(self, status):
        self.status_code, self.headers = status, {}

    def json(self):
        return {"ok": True}


def edgar(*statuses):
    """What _get does with these answers in turn: (result, calls, seconds slept)."""
    calls, slept = [], []
    left = list(statuses)

    def get(url, params=None, headers=None, timeout=None):
        calls.append((headers, timeout))
        status = left.pop(0)
        if status is None:
            raise sec.requests.RequestException("timed out")
        return Answer(status)

    real_r = (sec.requests.get, sec.time, sec._throttle)
    sec.requests.get, sec._throttle = get, lambda: None
    sec.time = types.SimpleNamespace(sleep=slept.append, time=time.time)
    try:
        return sec._get("https://example.invalid/x"), calls, slept
    except sec.SECError as exc:
        return exc, calls, slept
    finally:
        sec.requests.get, sec.time, sec._throttle = real_r


upstream.reset()
got, calls, slept = edgar(None, None, None)
longest = sum(sum(t) if isinstance(t, tuple) else t for _, t in calls) + sum(slept)
check("EDGAR not answering holds a page for half a minute at most",
      isinstance(got, sec.SECError) and longest <= 35, (longest, len(calls)))
got, calls, slept = edgar(503, 200)
check("a refusal is not asked again at once",
      isinstance(got, sec.SECError) and len(calls) == 1 and not slept, (got, len(calls)))
edgar(403)
edgar(429)
got, calls, _ = edgar(200)
check("and after three in a row nothing is sent for a while",
      isinstance(got, sec.SECError) and not calls, got)
upstream.reset()
got, calls, _ = edgar(None, 200)
check("one lost answer is asked for again", got == {"ok": True} and len(calls) == 2, got)

kept_contact = _os.environ.pop("STOCKBOT_CONTACT", None)
try:
    agent = sec._headers()["User-Agent"]
    check("without a contact set, EDGAR is told the project and its address",
          "Orenth" in agent and "https://orenth.app" in agent and "@" not in agent, agent)
    _os.environ["STOCKBOT_CONTACT"] = "hello@example.com"
    agent = sec._headers()["User-Agent"]
    check("with one, the site's own contact too",
          "Orenth" in agent and "hello@example.com" in agent, agent)
    _os.environ["STOCKBOT_CONTACT"] = "hello@example.com\nX-Other: 1"
    check("a setting that is not one plain line is left out of the header",
          "X-Other" not in sec._headers()["User-Agent"])
finally:
    _os.environ.pop("STOCKBOT_CONTACT", None)
    if kept_contact is not None:
        _os.environ["STOCKBOT_CONTACT"] = kept_contact
import inspect  # noqa: E402
check("and no mailbox is written into the code",
      not __import__("re").search(r"[\w.+-]+@[\w-]+\.\w+", inspect.getsource(sec)))


# ---------------------------------------------------------------------------
head("THE CASE FOR AND AGAINST COUNTS ONLY WHAT ARGUES")
# ---------------------------------------------------------------------------

report = types.SimpleNamespace(form="10-K", filed="2026-02-15", index_url="https://sec.gov/x")
straddle = {"expected_move": {"percent": 0.05}, "expiry_date": "2026-12-18",
            "atm_iv": 0.3, "put_call_oi": 1.0}
bare_case = thesis.build("TEST", 100.0, fundamentals=home)
full_case = thesis.build("TEST", 100.0, fundamentals=home, filings=[report],
                         options_stats=straddle)
check("an annual report and the options' expected move do not move the balance",
      full_case.balance == bare_case.balance and full_case.verdict == bare_case.verdict
      and full_case.bull.strength == bare_case.bull.strength,
      (bare_case.bull.strength, full_case.bull.strength))
check("and are kept, on neither side",
      len(full_case.neutral) == 2
      and not any("annual report" in p.headline or "size of the move" in p.headline
                  for p in full_case.bull.points + full_case.bear.points),
      [p.headline for p in full_case.neutral])


def researched(sec_answer, odds_side=0, composite=0.05):
    """engine.research on stand-ins, with EDGAR answering or not."""
    real_parts = (F.load, sec.financial_history, sec.recent_material, R.profile)

    def filed(symbol, years=6):
        if isinstance(sec_answer, Exception):
            raise sec_answer
        return sec_answer

    F.load = lambda symbol, with_sec=True: home
    sec.financial_history = filed
    sec.recent_material = lambda symbol, limit=12: []
    R.profile = lambda symbol, interval="1d", benchmark="SPY": None
    analysed = types.SimpleNamespace(
        composite=composite, regime=types.SimpleNamespace(trend="trending"), sentiment=None,
        plan=None, calibration=types.SimpleNamespace(
            expectancy_r=0.21, hit_rate=0.5, bucket_samples=180, direction=odds_side))
    try:
        return engine.research("TEST", 100.0, CFG, analysis=analysed, with_options=False,
                               with_reasoning=False)
    finally:
        F.load, sec.financial_history, sec.recent_material, R.profile = real_parts


down = researched(sec.SECError("EDGAR request failed: HTTP 503"))
none_filed = researched({"available": False, "reason": "does not file"})
check("the valuation runs when EDGAR does not answer",
      down.valuation and down.valuation["usable"]
      and down.valuation["midpoint"] == none_filed.valuation["midpoint"],
      down.valuation)
check("and only the filed accounts are named as missing",
      "Valuation" not in down.unavailable and "Filed accounts" in down.unavailable,
      down.unavailable)
short_odds = researched({"available": False}, odds_side=-1)
check("odds measured for a short argue the case against, whatever the signals lean",
      any("fall have paid" in p.headline for p in short_odds.thesis.bear.points)
      and not any("has paid" in p.headline for p in short_odds.thesis.bull.points),
      ([p.headline for p in short_odds.thesis.bull.points],
       [p.headline for p in short_odds.thesis.bear.points]))


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
# Only the strategies that vote make the composite; those on trial do not.
check("the case for a stock counts the strategies it ran",
      bull.points and ("across %d strategies" % len(S.voting_names()))
      in bull.points[0].detail, bull.points[0].detail if bull.points else "")
brief = reasoning.build_payload("TEST", 100.0, plan=clear)
check("and so does the brief given to the reasoning step",
      ("Technical plan (%d-strategy engine)" % len(S.voting_names())) in brief, list(brief))

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
head("THE RESEARCH PAGE NAMES THE CURRENCY AND WHAT IT LEFT OUT")
# ---------------------------------------------------------------------------

abroad = engine.Research(
    symbol="TEST", price=100.0, fundamentals=away, quality=F.quality_score(away),
    valuation=V.combine([a_dcf, a_rev, a_mult], 100.0), scores=bank,
    sec_history={"available": True, "entity": "Test Co", "source_url": "https://sec.gov/x",
                 "years": [2024, 2025], "currency": "TWD", "currencies": ["TWD"],
                 "table": {"revenue": rows_of("TWD", y2024=9e11, y2025=9.6e11),
                           "operating_cashflow": rows_of("TWD", y2024=2e11, y2025=2.1e11)},
                 "derived": {2025: {"free_cashflow": 1.9e11}}, "cagr": {}},
    thesis=full_case, options=dict(O.analyse(chain), days=1))
with web.app.test_request_context("/research?symbol=TEST&interval=1d"):
    web.app.preprocess_request()
    page = " ".join(render_template(
        "research.html", r=abroad, a=shown, cfg=CFG, saved=[], watched=False,
        price_svg="", price_svg_narrow="", filed_charts={},
        plain=plain.explain_company(abroad)).split())
check("the money rows carry their currency",
      "960.00B TWD" in page and "100.00B USD" in page
      and 'Revenue <span class="faint">in TWD</span>' in page
      and 'Free cash flow <span class="faint">in TWD</span>' in page)
check("the cash flow models say why they were not run",
      page.count("Not calculated: the accounts are in TWD") >= 2
      and "Discounted cash flow" in page)
check("what argues for neither side is shown apart",
      "counted for neither side" in page and "Recent annual report available" in page)
check("a bank's scores say why there are none",
      "Not applicable to a financial services company" in page)
check("one day is not 'days'", "1 day out" in page)
check("the plain words name the currency", "Taiwan dollars" in page)
check("and nothing unfinished is on it",
      not any(w in page for w in (">None<", "None%", ">nan<", "currency_problem")))


print()
print("%d failure(s)" % len(fails))
print("METHOD OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
