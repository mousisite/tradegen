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
import scenarios as sc  # noqa: E402
from bot import catalysts, clock, config, decision, engine, levels  # noqa: E402
from bot import market, plain, public  # noqa: E402
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

# The plan's "prior session" levels follow the same rule: while the market
# trades, the session before the live price's is the last finished bar.
b = fetch(payload([d + OPEN for d in days], closes, regular=today, price=closes[-1],
                  when=today["start"] + 3600), now=today["start"] + 3600)
prior = levels.prior_session(b)
check("mid-session the plan's prior session is the one that closed last",
      prior == {"high": float(b.high[-1]), "low": float(b.low[-1]),
                "close": float(b.close[-1])} and prior["close"] == closes[-2], prior)
b = fetch(payload([d + OPEN for d in days], closes, regular=today, price=closes[-1],
                  when=today["end"]), now=today["end"] + 3600)
check("and after the close the one before today's",
      levels.prior_session(b)["close"] == closes[-2], levels.prior_session(b))
b = fetch(payload(ts5, c5, regular={"start": sess[-1] + DAY, "end": sess[-1] + DAY + 23400},
                  when=sess[-1] + DAY + 60), now=sess[-1] + DAY + 60, interval="5m")
check("at an intraday session's first bar it is yesterday's, with no opening range yet",
      levels.prior_session(b)["close"] == c5[-2] and levels.opening_range(b) is None,
      levels.prior_session(b))
orb_days = sc.stock_series(78 * 3)
for bars_in, wanted in ((3, False), (6, False), (7, True)):
    part = clock.head(orb_days, 78 * 2 + bars_in)
    named = [lv for lv in levels.build_levels(part, 0.0001) if "opening range" in lv.source]
    check("%d bars into a session the plan has %s opening range"
          % (bars_in, "an" if wanted else "no"), bool(named) == wanted, named)
fut = sc.futures_series(276 * 4 + 200, roll_day=None)
table = clock.day_table(fut)
check("a future's prior session is its whole trading day, as the key levels have it",
      levels.prior_session(fut) == {"high": table[-2].high, "low": table[-2].low,
                                    "close": table[-2].close})
check("and it has no opening range at midnight", levels.opening_range(fut) is None)

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

sized = {"quantity": 7, "risk_amount": 87.73, "gap_loss": 188.32, "gap_multiple": 2.1}
for act in ("WAIT", "SHORT"):
    out, text = said(action=act, direction=-1, entry=105.0, stop=110.0, target1=95.0,
                     odds_direction=-1, position=sized)
    check("a short %s's risk is a sale, and its danger a jump up" % act,
          out["risk"] == "Shorting 7 of these would put $87.73 at risk. But if the price "
          "jumped up overnight, which happens, you could lose about $188.32 instead.",
          out["risk"])
out, text = said(position=sized)
check("a buy's is still a purchase, and a jump down",
      out["risk"].startswith("Buying 7 of these") and "jumped down" in out["risk"], out["risk"])
for miss, want, never in ((-0.52, "lost about 52% of what you put at risk, not all of it", "trading costs"),
                          (-1.0, "about 100% of what you put at risk: trading costs", "not all"),
                          (-2.3, "about 230% of what you put at risk: trading costs", "not all"),
                          (0.12, "still made about 12% of what you put at risk", "lost about")):
    out, text = said(avg_loss_r=miss)
    check("a usual miss of %+.2f times the risk is said as that" % miss,
          want in out["sure"] and never not in out["sure"], out["sure"])
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
check("one past example is said as one, won or lost",
      plain.counted(1, 1) == "In tests on past prices, this same setup came up once and "
      "worked that time, reaching its target before its stop."
      and plain.counted(1, 0, fall=True) == "In tests on past prices, this same setup came "
      "up once and a bet on the price falling did not work that time: it did not reach its "
      "target before its stop.", (plain.counted(1, 1), plain.counted(1, 0, fall=True)))
out, text = said(prob_samples=1, probability=1.0, prob_reliable=False)
check("and needs no count of one time to come out even",
      "1 times" not in out["sure"] and "too few times" in out["sure"], out["sure"])
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


def yahoo_like(price):
    """The same company, priced at this much a share."""
    f = loaded(cap=price * 1e9)
    f.metrics["price"].value = price
    return f


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

# Worked by hand. 6bn of free cash flow that grows 2.5% a year for ever, at a
# discount rate of 4.2% + 1.0 x 5% = 9.2%, is worth 6 x 1.025 / 0.067 = 91.79bn;
# with 10bn of cash and 5bn of debt, 96.79 for each of a billion shares.
steady = V.discounted_cash_flow(home, 100.0, growth=0.025)
check("a cash flow that grows steadily is worth what the sum says",
      abs(steady.fair_value - (6 * 1.025 / 0.067 + 5)) < 1e-6
      and abs(steady.upside - (steady.fair_value / 100.0 - 1)) < 1e-9, steady.fair_value)
check("and the growth in that price is the growth that was put in",
      abs(V.reverse_dcf(yahoo_like(steady.fair_value), steady.fair_value).implied_growth
          - 0.025) < 1e-6)
check("a price of 20 times earnings of 5 is 20 times earnings of 5",
      h_mult.fair_value == 100.0 and h_mult.upside == 0.0, h_mult.fair_value)
both = V.combine([steady, h_rev, h_mult], 100.0)
check("two values are put together as their middle, and the price is not a third",
      abs(both["midpoint"] - (steady.fair_value + 100.0) / 2) < 1e-9
      and both["low"] == steady.fair_value and both["high"] == 100.0
      and both["agreement"] == "tight", both)

# Yahoo's share count is the listed class alone; the cash flow covers all.
classes = loaded(shares=5e8)
check("every share class counts, in both models",
      abs(V.discounted_cash_flow(classes, 100.0).fair_value - h_dcf.fair_value) < 1e-6
      and abs(V.reverse_dcf(classes, 100.0).implied_growth - h_rev.implied_growth) < 1e-6,
      (V.discounted_cash_flow(classes, 100.0).fair_value, h_dcf.fair_value))
check("and where the count came from is said",
      any("every share class" in a_.source
          for a_ in V.discounted_cash_flow(classes, 100.0).assumptions))


# A model that comes to less than nothing has no price to give.
def yahoo_says(**changed):
    sent = company()
    for path, value in changed.items():
        module, key = path.split("__")
        sent[module][key] = value
    real_summary = yahoo.quote_summary
    yahoo.quote_summary = lambda symbol, modules: sent
    try:
        return F.load("TEST", with_sec=False)
    finally:
        yahoo.quote_summary = real_summary


owing = yahoo_says(financialData__totalDebt=5e11)
o_dcf, o_rev, o_mult = valued(owing)
check("debt above the whole value is a reason, not a fair value below nothing",
      not o_dcf.usable and o_dcf.fair_value is None and "debt is larger" in o_dcf.reason,
      (o_dcf.fair_value, o_dcf.reason))
alone = V.combine([o_dcf, o_rev, o_mult], 100.0)
check("20 times earnings is never the value on its own",
      o_mult.usable and not alone["usable"] and "debt is larger" in alone["reason"], alone)
check("models far apart are never said to agree",
      V.combine([h_dcf, V.Valuation("Other", fair_value=h_dcf.fair_value * 5, usable=True)],
                100.0)["agreement_words"] == "the models disagree widely"
      and V.combine(valued(home), 100.0)["agreement_words"].startswith("the models"))
said_alone = thesis.build("TEST", 100.0, fundamentals=owing, valuation=alone)
check("nor a point in the case, which says why there is no value",
      not any("on the model" in p.headline
              for p in said_alone.bull.points + said_alone.bear.points)
      and any("debt is larger" in u for u in said_alone.unknowns), said_alone.unknowns)
check("while what the price assumes is still said",
      o_rev.usable and any("already assumes a lot" in p.headline
                           for p in said_alone.bear.points),
      (o_rev.implied_average, [p.headline for p in said_alone.bear.points]))
fund = yahoo_says(financialData__freeCashflow=None)
check("a fund is not told that it burns cash",
      "none is reported for this instrument" in V.discounted_cash_flow(fund, 100.0).reason
      and "burn" not in V.discounted_cash_flow(fund, 100.0).reason)
lone = yahoo_says(summaryDetail__trailingPE=None)
only = V.combine(valued(lone), 100.0)
check("one model alone is not said to agree",
      only["usable"] and only["agreement"] == "single"
      and "Only one model" in only["commentary"][0]
      and not any("agree" in line for line in only["commentary"]), only["commentary"])
check("two are judged as before", V.combine(valued(home), 100.0)["agreement"]
      in ("tight", "loose", "wide"))
near = yahoo_says(financialData__totalDebt=1.05e11)
n_dcf = V.discounted_cash_flow(near, 100.0)
check("the grid shows no value below nothing, and says what its dashes are",
      n_dcf.usable and all(v is None or v > 0 for row in n_dcf.sensitivity
                           for v in row["values"])
      and any(v is None for row in n_dcf.sensitivity for v in row["values"])
      and any("dash" in n for n in n_dcf.notes), n_dcf.sensitivity)

slow = yahoo_says(financialData__revenueGrowth=0.01)
grid = V.discounted_cash_flow(slow, 100.0).sensitivity
wrong = [row["growth"] for row in grid if abs(
    row["values"][2] - V.discounted_cash_flow(slow, 100.0, growth=row["growth"]).fair_value)
    > 1e-6]
check("every row of the grid is worked out at the growth it names",
      not wrong and len({row["values"][2] for row in grid}) == 5, (wrong, grid))

path = [h_rev.implied_growth + (0.025 - h_rev.implied_growth) * k / 9 for k in range(10)]
came_to = float(np.prod([1 + g for g in path])) ** 0.1 - 1
check("what the price assumes is the average of a path that fades",
      abs(h_rev.implied_average - came_to) < 1e-9
      and "on average" in h_rev.notes[0] and "%.1f%% a year on average" % (came_to * 100)
      in h_rev.notes[0] and "a year for 10 years" not in h_rev.notes[0], h_rev.notes)
fast = V.reverse_dcf(yahoo_says(summaryDetail__marketCap=3.2e11,
                                defaultKeyStatistics__sharesOutstanding=3.2e9), 100.0)
said_fast = thesis.build("TEST", 100.0, valuation=V.combine([h_dcf, fast, h_mult], 100.0))
check("and the case asks for that average, not the first year's rate",
      fast.implied_growth > 0.25 > fast.implied_average > 0.12
      and not any("assumes a lot" in p.headline for p in said_fast.bear.points),
      (fast.implied_growth, fast.implied_average))
bargain = V.reverse_dcf(yahoo_says(summaryDetail__marketCap=1e10,
                                   financialData__totalCash=0.0,
                                   financialData__totalDebt=0.0,
                                   defaultKeyStatistics__sharesOutstanding=1e8), 100.0)
check("a price below the search says it is the limit",
      bargain.usable and bargain.implied_at_limit and "lowest it tries" in bargain.notes[0],
      (bargain.implied_growth, bargain.notes))
small = yahoo_says(financialData__freeCashflow=5.1e6)
small.sec_facts = Filed("USD", 3.4e6 / 6e9)
check("a small company's cash flow is not written in billions",
      "trailing figure of 5.1M" in V.normalised_fcf(small)["note"]
      and "average of 3.4M" in V.normalised_fcf(small)["note"]
      and "B" not in V.normalised_fcf(small)["note"], V.normalised_fcf(small)["note"])
check("amounts are written on the scale they need",
      [F.big_number(v) for v in (1.2e12, 3.4e9, 999.96e6, 28e6, 5100, 999.96, 12, 0, -2.4e6)]
      == ["1.2T", "3.4B", "1.0B", "28.0M", "5.1K", "1.0K", "12.00", "0.00", "-2.4M"]
      and F.big_number(48e6, 2) == "48.00M",
      [F.big_number(v) for v in (1.2e12, 3.4e9, 999.96e6, 28e6, 5100, 999.96, 12, 0, -2.4e6)])
brief_ = reasoning_mod = __import__("bot.reasoning", fromlist=["x"]).build_payload(
    "TEST", 100.0, valuation=only)
check("the reasoning step is told the same",
      "on average over ten years" in str(brief_) and "only one model" in str(brief_), brief_)

losing = yahoo_says(defaultKeyStatistics__forwardPE=-8.0,
                    defaultKeyStatistics__priceToBook=-3.5,
                    defaultKeyStatistics__enterpriseToEbitda=-12.0)
ratios = V.multiples(losing, 100.0)
below = [r for r in ratios.workings if r["value"] < 0]
check("a ratio below nothing is not cheap",
      len(below) == 3 and not any(r["cheap"] or r["comparable"] for r in below)
      and all("negative" in r["comment"] for r in below), below)
check("and is left out of the count",
      any(n.startswith("2 of 2 ratios look cheap") for n in ratios.notes), ratios.notes)

check("debt to equity carries its percent sign",
      yahoo_says(financialData__debtToEquity=154.49).show("debt_to_equity") == "154%")
notes = " ".join(yahoo_says(financialData__profitMargins=-0.2,
                            financialData__returnOnEquity=-0.1,
                            summaryDetail__trailingPE=None).notes)
check("a company that lost money is told that is why it has no P/E",
      "because it lost money" in notes and "Not reported" not in notes, notes)
notes = " ".join(yahoo_says(financialData__profitMargins=None,
                            financialData__totalRevenue=None,
                            summaryDetail__trailingPE=None).notes)
check("figures that are missing go by their names",
      "revenue, net margin, return on equity, price to earnings" in notes
      and "net_margin" not in notes and "coins" in notes, notes)


def yahoo_down(symbol, modules):
    raise yahoo.YahooError("refused")


real_summary, yahoo.quote_summary = yahoo.quote_summary, yahoo_down
F.log.disabled = True
try:
    notes = F.load("TEST", with_sec=False).notes
finally:
    yahoo.quote_summary, F.log.disabled = real_summary, False
check("figures that were not fetched are not called unpublished",
      len(notes) == 1 and "unavailable" in notes[0], notes)


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
check("what could not be measured is named in words",
      "because 3 of them could not be measured (debt, liquidity, share count)"
      in score.verdict and "was not reported" not in score.verdict, score.verdict)
check("the score says which years it compares, and 'the year before' is that",
      "fiscal 2025 against 2024" in score.scale
      and any("the year before" in t.detail for t in score.tests), score.scale)
gap = filings(**{key: {2021: values[2024], 2025: values[2025]} for key, values in (
    ("net_income", {2024: 110.0, 2025: 100.0}), ("assets", {2024: 1000.0, 2025: 1000.0}),
    ("operating_cashflow", {2024: 150.0, 2025: 150.0}),
    ("revenue", {2024: 1000.0, 2025: 950.0}),
    ("gross_profit", {2024: 400.0, 2025: 350.0}))})
score = Q.piotroski(gap)
check("four years back is not 'the year before'",
      "fiscal 2025 against 2021" in score.scale
      and any("in 2021" in t.detail for t in score.tests)
      and not any("year before" in t.detail or "last year" in t.detail for t in score.tests),
      [t.detail for t in score.tests])
judged = Q.assess(half)
check("the earnings score names its year too",
      "fiscal 2025" in judged["scores"]["accruals"].scale,
      judged["scores"]["accruals"].scale)
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
check("the manipulation screen names the years it compares",
      "fiscal 2025 against 2024" in m.scale, m.scale)
sound = Q.altman_z(filings(retained_earnings={2024: 300.0, 2025: 400.0},
                           operating_income={2024: 90.0, 2025: 100.0},
                           liabilities={2024: 400.0, 2025: 400.0},
                           **{k: flagged[k] for k in ("assets", "current_assets",
                                                      "current_liabilities", "revenue")}),
                   2000.0)
check("and the distress score its own", sound.usable and "fiscal 2025" in sound.scale,
      (sound.value, sound.scale, sound.verdict))
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
fell = risk_of(make_bars(np.concatenate([[100.0, 50.0], np.full(60, 50.0)])))
check("a fall from the very first close counts",
      abs(fell.max_drawdown + 0.5) < 1e-9 and abs(fell.current_drawdown + 0.5) < 1e-9
      and fell.drawdown_days == 1,
      (fell.max_drawdown, fell.current_drawdown, fell.drawdown_days))
grew = risk_of(make_bars(np.linspace(100.0, 150.0, 60)))
check("and a price that only rose has none", grew.max_drawdown == 0.0, grew.max_drawdown)


check("the period is said in words", grew.period == "60 daily bars"
      and risk_of(make_bars(np.linspace(100.0, 150.0, 1250))).period == "1,250 daily bars",
      grew.period)


def beta_words(beta):
    """What the risk notes and the case say of this beta."""
    p = R.RiskProfile(symbol="T", samples=500, period="", beta=beta, reliable=True)
    R._add_notes(p)
    for_, against = thesis.Case("bull"), thesis.Case("bear")
    thesis._risk_points(p, against, for_, "today")
    return " ".join(p.notes), [q.headline for q in for_.points]


for beta, word, counted in ((-2.9, "against the market", False), (-0.6, "against the", False),
                            (-0.2, "independently", True), (0.2, "independently", True),
                            (1.0, "", False), (2.0, "amplify", False)):
    words, points = beta_words(beta)
    check("a beta of %+.1f is %s" % (beta, word or "not remarked on"),
          (word in words if word else words == "")
          and ("independently" in words) == (word == "independently")
          and (points == ["Moves independently"]) == counted, (words, points))


def matched(**series):
    real_fetch = R.fetch_bars
    R.fetch_bars = lambda symbol, interval: make_bars(series[symbol])
    try:
        return R.correlation_matrix(sorted(series))
    finally:
        R.fetch_bars = real_fetch


ups, other = moves[:200], 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
flat_too = matched(FLAT=np.full(200, 10.0), WALK=ups, ZED=other)
check("a price that never moves is left out of the overlap, not shown as nan",
      flat_too["usable"] and flat_too["symbols"] == ["WALK", "ZED"]
      and flat_too["failed"] == ["FLAT"] and len(flat_too["matrix"]) == 2
      and "nan" not in str(flat_too), flat_too)
check("with one thing left that moves there is nothing to compare",
      not matched(FLAT=np.full(200, 10.0), WALK=ups)["usable"])

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


def quoted(state, *rows):
    """A chain of calls with these (bid, ask, last), in a market in this state."""
    now = O.clock.ny(2026, 11, 12, 10, 0)
    stamp = _calendar.timegm((2026, 11, 20, 0, 0, 0))
    quote = {"regularMarketPrice": 100.0}
    if state:
        quote["marketState"] = state
    sent = {"quote": quote, "expirationDates": [stamp], "options": [{
        "expirationDate": stamp, "puts": [], "calls": [
            {"strike": 95.0 + k, "bid": bid, "ask": ask, "lastPrice": last,
             "openInterest": 100} for k, (bid, ask, last) in enumerate(rows)]}]}
    real_o = (O.yahoo.options, O.risk_free_rate, O.time)
    O.yahoo.options = lambda symbol, expiry=None: sent
    O.risk_free_rate = lambda default=0.042: 0.042
    O.time = types.SimpleNamespace(time=lambda: now, gmtime=time.gmtime,
                                   strftime=time.strftime)
    try:
        return O.load_chain("TEST")
    finally:
        O.yahoo.options, O.risk_free_rate, O.time = real_o


thin = [(2.5, 2.6, 2.5)] + [(0.0, 3.0, 2.0)] * 4
open_now = quoted("REGULAR", *thin)
check("with the market open, thin strikes do not close it",
      not any("closed" in n for n in open_now.notes)
      and [c_.quality for c_ in open_now.calls[1:]] == ["no-bid"] * 4,
      (open_now.notes, [c_.quality for c_ in open_now.calls]))
check("with it closed the last prices still stand, and it is said",
      any("market is closed" in n for n in quoted("CLOSED", *thin).notes)
      and quoted("CLOSED", *thin).calls[1].quality == "stale")
just_open = quoted("REGULAR", *[(0.0, 0.0, 2.0)] * 5)
check("open without one bid yet, the last prices are used and called that",
      just_open.calls[0].quality == "stale"
      and any("No live quotes" in n for n in just_open.notes)
      and not any("closed" in n for n in just_open.notes), just_open.notes)
check("a market state that was not sent is read as before",
      quoted("", *thin).calls[1].quality == "stale")
check("bid and ask at one price is the best spread there is, not the worst",
      quoted("REGULAR", (2.5, 2.5, 2.5)).calls[0].tradable)


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
check("growth says which years it runs between, five of them at most",
      moved.cagr_years("revenue") == (2020, 2025))
three = facts_from(980006, {LATER: annual({2023: 100.0, 2024: 110.0, 2025: 121.0})})
check("or the two it has", three.cagr_years("revenue") == (2023, 2025)
      and abs(three.cagr("revenue") - 0.10) < 0.001, three.cagr_years("revenue"))
check("and none where there is no growth to give",
      facts_from(980007, {LATER: annual({2024: 1.0, 2025: 2.0})})
      .cagr_years("revenue") is None)
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


def history_from(cik, taxonomies, symbol="TEST"):
    real_sec = (sec._get, sec.cik_for)
    sec._get = lambda url, params=None, tries=2: {"entityName": "Test Co",
                                                  "facts": taxonomies}
    sec.cik_for = lambda symbol: cik
    try:
        return sec.financial_history(symbol)
    finally:
        sec._get, sec.cik_for = real_sec
        sec._FACTS_CACHE.pop(cik, None)


canada = annual({2024: 8.0, 2025: 9.0})
for row in canada["units"]["USD"]:
    row["form"] = "40-F"
check("a Canadian company's 40-F is an annual report",
      history_from(980011, {"us-gaap": {LATER: canada}}).get("years") == [2024, 2025])
ifrs = history_from(980012, {"ifrs-full": {"Revenue": annual({2024: 8.0, 2025: 9.0})}})
check("a company under international rules is told that, not that it re-registered",
      not ifrs["available"] and "international rules (IFRS)" in ifrs["reason"]
      and "re-registered" not in ifrs["reason"], ifrs["reason"])
words = plain.explain_company(types.SimpleNamespace(
    sec_history=ifrs, fundamentals=None, scores=None, valuation=None))
check("and the plain words do not call it a company that does not file",
      not words["available"] and "IFRS" in words["why"]
      and "not a company that files" not in words["why"], words)
bare_history = history_from(980013, {"us-gaap": {LATER: annual({2025: 9.0})}})
check("one year filed is said without a 'CIK'",
      "only one year" in bare_history["reason"] and "CIK" not in bare_history["reason"],
      bare_history["reason"])


# The ticker list in memory ages too, and an old one beats none.
import json  # noqa: E402
import tempfile  # noqa: E402


def ticker_reads(age_days, answers, reads=1, in_memory=True):
    """ticker_map with a list this old: (its last answer, downloads asked for)."""
    asked_ = []

    def get(url, params=None, tries=2):
        asked_.append(url)
        if isinstance(answers, Exception):
            raise answers
        return answers

    with tempfile.TemporaryDirectory() as folder:
        path = _os.path.join(folder, "sec_tickers.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"OLD": 1}, fh)
        made = time.time() - age_days * DAY
        _os.utime(path, (made, made))
        real_t = (sec._get, sec._cache_dir, sec._TICKER_CACHE, sec._TICKER_AT[0],
                  sec._TICKER_ASKED[0])
        sec._get, sec._cache_dir = get, lambda: folder
        sec._TICKER_CACHE = {"OLD": 1} if in_memory else None
        sec._TICKER_AT[0], sec._TICKER_ASKED[0] = made, 0.0
        try:
            for _ in range(reads):
                got_ = sec.ticker_map()
            return got_, len(asked_)
        finally:
            (sec._get, sec._cache_dir, sec._TICKER_CACHE, sec._TICKER_AT[0],
             sec._TICKER_ASKED[0]) = real_t


listed_new = {"0": {"ticker": "NEW", "cik_str": 2}}
check("a list in memory past its age is asked for again",
      ticker_reads(15, listed_new, reads=3) == ({"NEW": 2}, 1),
      ticker_reads(15, listed_new, reads=3))
check("a younger one is not", ticker_reads(3, listed_new) == ({"OLD": 1}, 0))
check("when none comes the old one stays, and it is not asked for at every page",
      ticker_reads(15, sec.SECError("down"), reads=3) == ({"OLD": 1}, 1))
check("so does one left on disk when the server starts",
      ticker_reads(15, sec.SECError("down"), in_memory=False) == ({"OLD": 1}, 1))


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
for _ in range(3):
    edgar(None, None)
got, calls, _ = edgar(200)
check("three with no answer at all leave EDGAR be for a while, said as that",
      isinstance(got, sec.SECError) and not calls and "not answering" in str(got), got)
upstream._breaker(sec._HOST)["open_until"] = time.time() - 1
got, calls, _ = edgar(None, 404)
check("then one request, with its retry, finds out, and having nothing is an answer",
      got is None and len(calls) == 2 and not upstream.refusing(sec._HOST), (got, len(calls)))
upstream.reset()

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

grown = history_from(980014, {"us-gaap": {LATER: annual(
    {2023: 100.0, 2024: 150.0, 2025: 225.0})}})
said_grown = thesis.build("TEST", 100.0, sec_history=grown)
check("the case names the years its growth is over, and the years it read",
      any("a year from 2023 to 2025" in p.detail for p in said_grown.bull.points)
      and any("fiscal years 2023 to 2025" in s["detail"] for s in said_grown.all_sources),
      ([p.detail for p in said_grown.bull.points], said_grown.all_sources))
check("so does the brief given to the reasoning step",
      "Revenue growth a year, FY2023 to FY2025" in str(
          __import__("bot.reasoning", fromlist=["x"]).build_payload(
              "TEST", 100.0, sec_history=grown)))
said_few = thesis.build("TEST", 100.0, fundamentals=home)
check("several points support or argue; one supports or argues",
      "%d points support owning this" % len(said_few.bull.points) in said_few.bull.summary
      and said_grown.bull.summary.startswith("1 point supports owning this, on strong")
      and said_grown.bear.summary == "Nothing in the available evidence argues against it."
      and thesis._summarise(said_few.bull, "argues", "argue", "against it")
      .startswith("%d points argue against it" % len(said_few.bull.points)),
      (said_few.bull.summary, said_grown.bull.summary))
check("a case too thin for a view is not judged right or wrong later",
      thesis.no_view(said_grown.verdict)
      and not thesis.no_view("The evidence leans bullish.") and not thesis.no_view(None)
      and all(thesis.review({"price": 100.0, "balance": 1.0, "created": 0,
                             "verdict": said_grown.verdict}, now_)["outcome"] == "no call"
              and thesis.suggested_outcome(
                  {"price": 100.0, "balance": 1.0, "created": 0,
                   "verdict": said_grown.verdict}, now_) == "undecided"
              for now_ in (80.0, 130.0)), said_grown.verdict)
check("while one with a view still is",
      thesis.review({"price": 100.0, "balance": 1.0, "created": 0,
                     "verdict": "The evidence leans bullish."}, 80.0)["outcome"]
      == "wrong so far")

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


coin_case = thesis.build("BTC-USD", 0.00001234, technical={
    "composite": 0.4, "regime": "trending", "expectancy": 0.1, "hit_rate": 0.4,
    "samples": 50})
told_wrong = " ".join(coin_case.falsifiers)
check("a coin is not told about revenue, margins or chief executives",
      coin_case.balance > 0 and len(coin_case.falsifiers) == 1
      and not any(w in told_wrong.lower() for w in ("revenue", "margin", "executive",
                                                    "8-k", "cash")), told_wrong)
check("and its price keeps the digits it needs", "0.000009872" in told_wrong, told_wrong)
check("a case that leans neither way has nothing to prove wrong",
      thesis.build("TEST", 100.0).falsifiers == [])
deep = yahoo_says(financialData__profitMargins=-0.1, financialData__freeCashflow=-1e9)
told_wrong = " ".join(thesis.build("TEST", 100.0, fundamentals=deep).falsifiers)
check("a company already losing money is not told its margin could fall below zero",
      "below 0.0%" not in told_wrong and "better profit margins" in told_wrong
      and "cash-burn" in told_wrong and "chief executive" not in told_wrong, told_wrong)
check("a company that files is told which filings matter",
      any("chief executive" in x for x in full_case.falsifiers)
      and any("Net margin falling below 15.0%" in x for x in full_case.falsifiers),
      full_case.falsifiers)


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
real_wait, engine.RESEARCH_WAIT = engine.RESEARCH_WAIT, 0.3
release = threading.Event()
real_parts = (F.load, sec.financial_history, sec.recent_material, R.profile)
F.load = lambda symbol, with_sec=True: home
sec.financial_history = lambda symbol, years=6: release.wait(10)
sec.recent_material = lambda symbol, limit=12: []
R.profile = lambda symbol, interval="1d", benchmark="SPY": None
began = time.time()
try:
    late = engine.research("TEST", 100.0, CFG, with_options=False, with_reasoning=False)
finally:
    release.set()
    engine.RESEARCH_WAIT = real_wait
    F.load, sec.financial_history, sec.recent_material, R.profile = real_parts
check("a source that never answers costs the page that piece, not the page",
      time.time() - began < 5 and late.unavailable == ["Filed accounts"]
      and late.fundamentals is home and late.valuation["usable"],
      (time.time() - began, late.unavailable))
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
    risk=types.SimpleNamespace(
        period="1,250 daily bars", reliable=True, volatility=0.2, max_drawdown=-0.1,
        var_95=-0.02, var_99=-0.03, beta=-0.001, sharpe=-0.004, notes=[]),
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
check("and filed amounts in another currency are scaled and named",
      "960.0B" in page and "900.0B" in page)
check("the plain words name the currency", "Taiwan dollars" in page)
check("and nothing unfinished is on it",
      not any(w in page for w in (">None<", "None%", ">nan<", "currency_problem")))


def drawn(r, **kw):
    with web.app.test_request_context("/research?symbol=TEST&interval=1d"):
        web.app.preprocess_request()
        return " ".join(render_template(
            "research.html", r=r, a=shown, cfg=CFG, saved=[], watched=False,
            price_svg="", price_svg_narrow="", filed_charts={}, plain=None, **kw).split())


check("a Sharpe or beta that rounds to nothing is not 'minus nothing'",
      "-0.00" not in page and "1,250 daily bars" in page)
little = yahoo_says(financialData__freeCashflow=3.4e6, summaryDetail__marketCap=4.8e9,
                    defaultKeyStatistics__sharesOutstanding=4.8e7,
                    financialData__totalCash=0.0, financialData__totalDebt=0.0)
tiny = filings(net_income={2024: 2.1e6, 2025: 2.5e6}, assets={2024: 3e7, 2025: 3.2e7},
               operating_cashflow={2024: 2.6e6, 2025: 3.1e6},
               revenue={2024: 2.8e7, 2025: 3e7})
few_dear = V.combine([V.discounted_cash_flow(little, 100.0), V.reverse_dcf(
    yahoo_says(summaryDetail__marketCap=7.05e13, financialData__currentPrice=705000.0,
               financialData__freeCashflow=3e12,
               defaultKeyStatistics__sharesOutstanding=1e8), 705000.0)], 100.0)
page = drawn(engine.Research(
    symbol="TEST", price=100.0, fundamentals=little, quality=F.quality_score(little),
    valuation=few_dear, scores=Q.assess(tiny), thesis=said_few,
    sec_history=dict(tiny, entity="Test Co", source_url="https://sec.gov/x",
                     derived={2024: {"free_cashflow": 3.1e6}, 2025: {"free_cashflow": -2.4e6}},
                     cagr={"revenue": 0.071}, cagr_years={"revenue": (2024, 2025)})))
check("a small company's figures are in the scale they need, not 0.0B",
      not __import__("re").search(r"(?<![0-9])0[.]0+B", page)
      and "3.1M" in page and "-2.4M" in page
      and "28.0M" in page and "3.40M" in page and "48.00M" in page)
check("the earnings score's amounts are readable", "31.0M" in page and "31000000" not in page)
check("the price in the model's table is written as prices are",
      "705,000" in page and "e+05" not in page)
check("growth is headed by what it is, with its years under it",
      "Growth a year" in page and "5y growth" not in page
      and "2024 to 2025</div>" in page)
check("a test's mark is said in words as well",
      '<span class="sr">passed</span>' in page and '<span class="sr">not reported</span>' in page)
page = drawn(engine.Research(symbol="TEST", price=100.0, fundamentals=owing,
                             quality=F.quality_score(owing), valuation=alone,
                             thesis=said_alone))
check("with no value the ratios are still shown, and no fair value is",
      "No model could put a value on it" in page and "Price to earnings" in page
      and "Fair value" not in page and "Modelled" not in page.split("What it might")[-1])
page = drawn(engine.Research(symbol="TEST", price=100.0, fundamentals=lone,
                             quality=F.quality_score(lone), valuation=only,
                             thesis=thesis.build("TEST", 100.0, fundamentals=lone,
                                                 valuation=only)))
check("one model is called one model",
      "from one model" in page and "Modelled value" in page and "Midpoint" not in page
      and "single agreement" not in page)
check("the key figures are there once each", page.count("<td>Market capitalisation</td>") == 1
      and "The business" in page)
for mid, low, high, said in ((17.346, 15.2, 1234567.8, ("17.35", "15.20–1,234,567.80")),
                             (0.0041, 0.0032, 0.0051, ("0.0041", "0.0032–0.0051"))):
    for agreement, words in (("single", 'Modelled value</div> <div class="v fig">%s<' % said[0]),
                             ("wide", 'Modelled range</div> <div class="v fig">%s<' % said[1])):
        check("a share worth about %s is said to be, %s" % (
            said[0], "from one model" if agreement == "single" else "from several"),
            words in drawn(engine.Research(
                symbol="TEST", price=mid, fundamentals=lone, quality=F.quality_score(lone),
                valuation=dict(only, midpoint=mid, low=low, high=high, agreement=agreement),
                thesis=thesis.build("TEST", 100.0, fundamentals=lone, valuation=only))))
nothing = F.Fundamentals(symbol="BTC-USD")
page = drawn(engine.Research(symbol="BTC-USD", price=1.0, fundamentals=nothing,
                             quality=F.quality_score(nothing), thesis=coin_case,
                             valuation=V.combine(valued(nothing), 1.0)))
check("a coin has no business section, nor a table of nothing",
      "The business" not in page and "checks could be judged" not in page
      and "Key figures" not in page and "What is reported" not in page)
check("a case that leans neither way says there is nothing to prove wrong",
      "no case here to prove wrong" in drawn(engine.Research(
          symbol="TEST", price=1.0, thesis=thesis.build("TEST", 1.0))))


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
# 1.084 and 1.084: kept apart by the plan, and read as one price on the page.
pair_ = dataclasses.replace(p_slow, entry=1.08412, stop=1.08386, target1=1.0849,
                            action="BUY")
levels_ = (pair_.entry, pair_.stop, pair_.target1)
check("they read apart on the page too, alike in how many decimals they may have",
      [market.format_price(v, beside=levels_) for v in levels_]
      == ["1.0841", "1.0839", "1.0849"],
      [market.format_price(v, beside=levels_) for v in levels_])
said_ = " ".join(plain.explain_plan(pair_, slow)["points"])
check("and in its plain words", "buy price is $1.0841." in said_
      and "fell to $1.0839," in said_ and "around $1.0849." in said_, said_)
check("at any size of price", [market.format_price(a, beside=(b,)) + " " +
                               market.format_price(b, beside=(a,)) for a, b in
                               ((151.2312, 151.2287), (100.031, 100.0295), (0.91, 0.909999),
                                (2e-11, 1.5e-11), (94731.004, 94731.001))]
      == ["151.231 151.229", "100.031 100.0295", "0.91 0.909999",
          "0.00000000002 0.000000000015", "94,731.004 94,731.001"],
      [market.format_price(a, beside=(b,)) for a, b in ((151.2312, 151.2287),)])
check("ordinary prices, and one price said twice, read as ever beside each other",
      all(market.format_price(a, beside=(b, None, float("nan"))) == market.format_price(a)
          and plain._money(a, "USD", (b, None)) == plain._money(a, "USD")
          for a, b in ((101.27, 99.4), (12.346, 12.31), (5.5, 5.5), (105628.0, 97177.76),
                       (0.1234567, 0.12), (0.00001234, 0.00001377), (0.0, 1.0),
                       (1.084, 1.084 + 1e-13))))
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
from bot.levels import Level as _Level  # noqa: E402

_ctx = S.build_context(base_bars)
_atr = float(_ctx.atr[-1])
for nudge in (0.0004, 0.0006):             # rounded down, and up, onto its own side
    near_at = float(np.floor((base_bars.last_price - 0.31 * _atr) * 1000) / 1000) + nudge
    at_level = decision.decide(
        _ctx, base_bars, [], 0.3, S.classify_regime(_ctx, len(base_bars) - 1), calib(), FLAT,
        [_Level(near_at, "support", 2, "swing low", 0.5),
         _Level(near_at - 1.5 * _atr, "support", 5, "swing low", 0.9)], calm)
    check("an entry at a level (%+.4f) keeps its stop behind the next level below" % nudge,
          at_level.entry == round(near_at, 3) and "5 touches" in at_level.stop_rationale,
          (at_level.entry, near_at, at_level.stop_rationale))
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
check("a size in the millions is written out", "Buying 416,666,666.5 of these" in plain.explain_plan(
    dataclasses.replace(sized, position=dict(sized.position, quantity=416666666.5,
                                             **decision.size_words(416666666.5, True))),
    base_bars)["risk"])
refused = plain.explain_plan(dataclasses.replace(
    sized, action="AVOID", direction=-1, avoid_reason="negative_record",
    stop=sized.entry + 5, target1=sized.entry - 7), base_bars)
check("a short refused on its record is not told as a buy",
      refused["risk"].startswith("Shorting ") and any("kept rising" in x for x in refused["points"])
      and not any("If you bought" in x for x in refused["points"]), refused)
unmeasured = plain.explain_plan(decision.Plan(
    action="AVOID", direction=0, headline="", conviction=-0.3,
    avoid_reason="bearish_no_shorts", odds_direction=-1), base_bars)
check("and a fall with no record is not said to have one below",
      not any("past record" in x for x in unmeasured["points"])
      and "do not buy" in unmeasured["points"][0], unmeasured)
check("a company's small sales name their currency once",
      plain._big(450000, "EUR") == "450,000 euros" and plain._big(450000) == "$450,000")


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
      and not any("structure could not be read" in r for r in long_r.plan.risks),
      long_r.plan.risks)
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
penny = web._paper_size(dict(es_row, symbol="TINY-USD", asset_class="crypto", entry=1.667e-6,
                             stop=1.544e-6, target1=1.8e-6), dict(CFG, account_size=10000.0))
check("a coin's size in the millions is written as the analysis page writes it",
      penny["quantity_text"] == decision.size_words(penny["quantity"], True)["quantity_text"]
      and "," in penny["quantity_text"], penny["quantity_text"])
try:
    web._paper_size(es_row, CFG)
    check("and refuses what one contract is too big for", False)
except web._NoTrade as exc:
    check("and refuses what one contract is too big for", "Not even one ES contract" in str(exc))


# ---------------------------------------------------------------------------
head("WHAT WAS NOT READ IS NOT SAID TO BE NOTHING")
# ---------------------------------------------------------------------------

from bot import screener  # noqa: E402


def no_list():
    raise sec.SECError("EDGAR request failed: HTTP 403")


real_map = sec.ticker_map
sec.ticker_map = no_list
try:
    try:
        said_ = sec.financial_history("AAPL")
    except sec.SECError as exc:
        said_ = exc
    check("without EDGAR's list, a company is not told it files nothing",
          isinstance(said_, sec.SECError), said_)
    unread_ = ideas._accounts_hold_up("AAPL")
    check("and Find calls its filings unread, and asks again soon",
          unread_ and unread_["ok"] is None and not unread_.get("unjudged"), unread_)
    check("a coin still has none", sec.financial_history("BTC-USD")["available"] is False)
finally:
    sec.ticker_map = real_map

real_hist = sec.financial_history
sec.financial_history = lambda symbol, years=6: dict(ifrs)
try:
    check("Find does not say a company under international rules files nothing",
          (ideas._accounts_hold_up("TSM") or {}).get("unjudged") is True,
          ideas._accounts_hold_up("TSM"))
finally:
    sec.financial_history = real_hist

gone = F.Fundamentals("TEST", notes=["Yahoo fundamentals unavailable: Yahoo did not send "
                                     "this company's figures this time."])
gone_value = V.combine([V.discounted_cash_flow(gone, 100.0), V.reverse_dcf(gone, 100.0),
                        V.multiples(gone, 100.0)], 100.0)
check("figures Yahoo did not send are not called not reported",
      not gone_value["usable"] and "did not send" in gone_value["reason"]
      and not any("normal for" in m.reason or "not reported" in m.reason
                  for m in gone_value["models"]), [m.reason for m in gone_value["models"]])


class Filed:
    """Filed cash flows, by fiscal year."""

    def __init__(self, ocf, capex):
        self.rows = {"operating_cashflow": ocf, "capex": capex}

    def series(self, concept, years=10):
        return [{"fy": y, "value": v, "unit": "USD"}
                for y, v in sorted(self.rows[concept].items())][-years:]


def starting(ocf, capex):
    f_ = F.Fundamentals("TEST")
    F._put(f_.metrics, "free_cashflow", "Free cash flow", 70.0)
    f_.sec_facts = Filed(ocf, capex)
    return V.normalised_fcf(f_)


check("the average is of the last three filed years",
      starting({y: 100.0 for y in range(2020, 2026)},
               {y: 40.0 for y in range(2020, 2026)})["value"] == 60.0)
check("and never of three years long past",
      starting({y: 100.0 for y in range(2020, 2026)},
               {y: 40.0 for y in range(2017, 2023)})["value"] == 70.0)
check("nor of three with years missing between them",
      starting({y: 100.0 for y in range(2020, 2026)},
               {y: 40.0 for y in (2020, 2022, 2025)})["value"] == 70.0)


# ---------------------------------------------------------------------------
head("NO VIEW, NO TRADE THAT FITS IT")
# ---------------------------------------------------------------------------

def researched(facts_=None, history_=None):
    """engine.research on made-up parts: (the research, the view options were asked for)."""
    views = []
    chain_ = types.SimpleNamespace()
    real_parts = (F.load, sec.financial_history, sec.recent_material, R.profile,
                  O.load_chain, O.analyse, O.suggest)
    F.load = lambda symbol: facts_ or F.Fundamentals(symbol)
    sec.financial_history = lambda symbol, years=8: history_ or {"available": False, "reason": ""}
    sec.recent_material = lambda symbol, limit=10: []
    R.profile = lambda symbol, interval="1d": R.RiskProfile(
        symbol=symbol, samples=300, period="", reliable=True, beta=0.05)
    O.load_chain, O.analyse = lambda symbol: chain_, lambda chain: {}
    O.suggest = lambda chain, view, conviction=0.5: views.append(view) or []
    try:
        return engine.research("TEST", 100.0, dict(HOUSE), with_reasoning=False), views
    finally:
        (F.load, sec.financial_history, sec.recent_material, R.profile,
         O.load_chain, O.analyse, O.suggest) = real_parts


one_point, views_ = researched()
check("one point is not a view, and no option trade is offered to fit it",
      one_point.thesis.balance == 1.0 and thesis.no_view(one_point.thesis.verdict)
      and views_ == ["neutral"], (one_point.thesis.balance, views_))
bank_like = {"available": True, "years": [2024, 2025], "currency": "USD", "entity": "B",
             "table": {"revenue": [{"fy": y, "value": 1.0, "unit": "USD"} for y in (2024, 2025)]}}
check("a bank is not scored as a factory while what it is could not be read",
      researched(gone, bank_like)[0].scores["available"] is False)
check("any other company still is",
      researched(gone, dict(bank_like, table=dict(bank_like["table"], current_assets=[
          {"fy": 2025, "value": 1.0, "unit": "USD"}])))[0].scores["available"] is True)


# ---------------------------------------------------------------------------
head("DOLLARS ARE COMPARED WITH DOLLARS")
# ---------------------------------------------------------------------------

def screened(filters, **figures):
    quote_ = {"symbol": "AAA", "regularMarketPrice": 2800.0, "marketCap": 4e13,
              "currency": "JPY"}
    quote_.update(figures.pop("quote", {}))

    def loaded(symbol, with_sec=False):
        f_ = F.Fundamentals(symbol, accounts_currency=figures.get("accounts", "JPY"))
        F._put(f_.metrics, "free_cashflow", "Free cash flow", 2e12)
        return f_

    real_s = (yahoo.quotes, F.load)
    yahoo.quotes, F.load = lambda symbols: [quote_], loaded
    try:
        return screener.run(symbols=["AAA"], filters=filters)
    finally:
        yahoo.quotes, F.load = real_s


yen = screened({"market_cap_min": 1e12})
check("forty trillion yen is not forty trillion dollars",
      not yen.passed and any("another currency" in n and "AAA" in n for n in yen.notes), yen.notes)
check("a size in yen says so", screener._from_quote(
    {"symbol": "X", "marketCap": 4e13, "currency": "JPY"}).size == "40.00T JPY")
check("and one of a share in pence is in pounds", screener._from_quote(
    {"symbol": "X", "marketCap": 1.5e10, "currency": "GBp"}).size == "15.0B GBP")
check("a dollar one needs no word", screener._from_quote(
    {"symbol": "X", "marketCap": 2e9, "currency": "USD"}).size == "2.0B")
check("more cash in than out is the same in any currency",
      [c.symbol for c in screened({"fcf_positive": 0}).passed] == ["AAA"])
check("an amount of it is not",
      not screened({"fcf_positive": 1e9}).passed
      and [c.symbol for c in screened({"fcf_positive": 1e9}, accounts="USD").passed] == ["AAA"])
yen_brief = str(_reasoning.build_payload("TEST", 100.0, sec_history={
    "available": True, "table": {"revenue": [{"fy": 2025, "value": 4.5e13, "unit": "JPY"}]}}))
check("the brief given to the reasoning step says which currency filed sales are in",
      "in JPY" in yen_brief and "45.0T" in yen_brief, yen_brief)


# ---------------------------------------------------------------------------
head("THE PAID REASONING IS ASKED FOR ONCE")
# ---------------------------------------------------------------------------

clients, requests_ = [], []


def counted_client(**k):
    clients.append(k)
    return types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: requests_.append(kw) or types.SimpleNamespace(
            content=[], stop_reason="end_turn")))


fake_sdk = types.ModuleType("anthropic")
fake_sdk.Anthropic = counted_client
had_sdk, was_available = _sys.modules.get("anthropic"), _reasoning.available
had_key = _os.environ.get("ANTHROPIC_API_KEY")
had_model = _os.environ.pop("CLAUDE_MODEL", None)
quiet = _reasoning.log.level
_reasoning.log.setLevel(logging.CRITICAL + 1)
_sys.modules["anthropic"], _reasoning.available = fake_sdk, lambda: True
_os.environ["ANTHROPIC_API_KEY"] = "made up for this test"
try:
    _reasoning.analyse({"Instrument": {"symbol": "T", "price": 10.0}})
    _reasoning.analyse({"Instrument": {"symbol": "T", "price": 10.0}}, model="another")
finally:
    _reasoning.available = was_available
    _reasoning.log.setLevel(quiet)
    _sys.modules.pop("anthropic")
    if had_sdk is not None:
        _sys.modules["anthropic"] = had_sdk
    _os.environ.pop("ANTHROPIC_API_KEY")
    if had_key is not None:
        _os.environ["ANTHROPIC_API_KEY"] = had_key
    if had_model is not None:
        _os.environ["CLAUDE_MODEL"] = had_model
check("a reply too slow for the page is not asked and paid for a second time",
      clients and all(c["max_retries"] == 0 and c["timeout"] <= 40 for c in clients), clients)
check("the default model is kept from thinking past the page's time",
      requests_[0].get("output_config") == {"effort": "medium"}
      and "output_config" not in requests_[1], requests_)


# ---------------------------------------------------------------------------
head("AN ANSWER THAT IS NOT DATA")
# ---------------------------------------------------------------------------

class Nothing:
    status_code, headers, text = 200, {}, "null"

    def json(self):
        return None


def rubbish(module, ask):
    real_parts = (module.requests.get, module.time, yahoo._ensure, yahoo._SESSION)
    module.requests.get = lambda *a, **k: Nothing()
    module.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)
    yahoo._ensure = lambda force=False, seen=None: None
    yahoo._SESSION = types.SimpleNamespace(get=lambda *a, **k: Nothing())
    upstream.reset()
    try:
        return ask()
    except Exception as exc:
        return exc
    finally:
        module.requests.get, module.time, yahoo._ensure, yahoo._SESSION = real_parts
        upstream.reset()


got = rubbish(market, lambda: market.fetch_bars("TEST", "1d"))
check("prices that are not data are the source's failure, said as that",
      isinstance(got, market.DataUnavailable), repr(got))
got = rubbish(yahoo, lambda: yahoo.quotes(["TEST"]))
check("as are figures", isinstance(got, yahoo.YahooError), repr(got))


# ---------------------------------------------------------------------------
head("EX-DIVIDEND TOMORROW, SEEN THE EVENING BEFORE")
# ---------------------------------------------------------------------------

ex_day = _calendar.timegm((2026, 11, 24, 0, 0, 0))


def ex_said(seconds_after):
    real_time = catalysts.time
    catalysts.time = types.SimpleNamespace(time=lambda: ex_day + seconds_after)
    try:
        cal_ = catalysts.Calendar("T", ex_dividend=ex_day, gmt_offset=-18000)
        return (catalysts.warnings_for(cal_, "1d", 10, -1),
                [r["due"] for r in catalysts.describe(cal_)])
    finally:
        catalysts.time = real_time


check("in the evening in New York the plan still warns, as the calendar does",
      ex_said(3600)[1] == ["tomorrow"] and any("tomorrow" in w for w in ex_said(3600)[0]),
      ex_said(3600))
check("on the day the price has dropped already",
      ex_said(15 * 3600) == ([], ["today"]), ex_said(15 * 3600))


print()
print("%d failure(s)" % len(fails))
print("METHOD OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
