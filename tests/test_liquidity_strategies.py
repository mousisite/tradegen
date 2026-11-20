"""The course's price-action setups, on trial in the strategy library.

Ten strategies from TJR's video are scored on every analysis and measured by
every back-test, but they do not vote: the composite, the odds and every call
must be exactly what they are without them. This suite pins that, then that
each setup fires on a scene built to show it (with the right sign and a reason
naming the price), keeps quiet where there is nothing, never reads a bar after
the one it scores, is measured by the daily back-test, sits out cleanly when
the price structure cannot be read, and costs little time.

No network: every price here is made up (tests/scenarios.py).
"""
import os as _os
import random
import sys as _sys
import time
import warnings

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

harness.isolate("liquidity_strategies")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import calibrate as cal  # noqa: E402
from bot import catalysts, clock, config, engine, learning, liquidity  # noqa: E402
from bot import strategies as S  # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)   # indicators on very short scenes

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


VOTING = [s for s in S.REGISTRY if s.votes]
NEW = [s for s in S.REGISTRY if not s.votes]


class only_voting:
    """The registry without the strategies on trial, for the length of a block."""

    def __enter__(self):
        self.real = S.REGISTRY
        S.REGISTRY = list(VOTING)

    def __exit__(self, *exc):
        S.REGISTRY = self.real


DAILY = sc.daily_series(1250)
STOCK = sc.stock_series(2500)
FUTURE = sc.futures_series(4000, roll_day=None)

# ---------------------------------------------------------------------------
head("THE STRATEGIES ON TRIAL")
# ---------------------------------------------------------------------------
check("ten setups on trial, in the liquidity family",
      len(NEW) == 10 and all(s.family == S.LIQUIDITY for s in NEW)
      and S.LIQUIDITY in S.FAMILIES, [s.name for s in NEW])
check("each a strategy the setup catalog names",
      set(S.TRIAL) == {s.name for s in NEW}
      and all(n in __import__("bot.setups", fromlist=["x"]).REGISTRY_NAMES for n in S.TRIAL))
check("thirty-four strategies still vote",
      len(S.voting_names()) == 34 and len(S.voting_names()) + len(S.TRIAL) == len(S.REGISTRY))
check("the session sweep needs intraday bars",
      "session_sweep" in S.INTRADAY_ONLY and "session_sweep" not in S.daily_strategy_names())
check("each weighs 0.7 at the start",
      all(abs(s.base_weight - 0.7) < 1e-12 for s in NEW))

# ---------------------------------------------------------------------------
head("THE CALL IS THE SAME WITH OR WITHOUT THEM")
# ---------------------------------------------------------------------------
rng = random.Random(7)
worst = 0.0
fired_trial = 0
for name, bars in (("daily", DAILY), ("stock", STOCK), ("future", FUTURE)):
    ctx = S.build_context(bars)
    picks = sorted(rng.sample(range(ctx.warmup, len(bars)), 60))
    learned = {s.name: 0.4 + 0.02 * k for k, s in enumerate(S.REGISTRY)}
    for i in picks:
        sig_all, comp_all, reg_all = S.evaluate(ctx, i)
        sig_l, comp_l, _ = S.evaluate(ctx, i, learned=learned)
        fired_trial += sum(1 for s in sig_all if s.name in S.TRIAL and s.score)
        with only_voting():
            sig_v, comp_v, reg_v = S.evaluate(ctx, i)
            _, comp_vl, _ = S.evaluate(ctx, i, learned=learned)
        worst = max(worst, abs(comp_all - comp_v), abs(comp_l - comp_vl))
        same = {s.name: (s.score, s.weight) for s in sig_v} == {
            s.name: (s.score, s.weight) for s in sig_all if s.name not in S.TRIAL}
        if not same or reg_all.trend != reg_v.trend:
            worst = max(worst, 1.0)
print("   (the setups on trial fired %d times at those bars)" % fired_trial)
check("the composite is identical at 180 bars on three series, learned weights or not",
      worst <= 1e-12 and fired_trial > 20, (worst, fired_trial))

ctx = S.build_context(DAILY)
score = S.evaluate(ctx, len(DAILY) - 1)[1]
calib_all = cal.calibrate(ctx, score, 1.5, 2.25, 10, stride=3, cost_bps=2.0)
with only_voting():
    calib_v = cal.calibrate(ctx, score, 1.5, 2.25, 10, stride=3, cost_bps=2.0)
check("the odds are measured the same way",
      repr(calib_all.__dict__) == repr(calib_v.__dict__)
      and calib_all.samples > 0, (calib_all.hit_rate, calib_v.hit_rate))

CFG = config.load()
engine.resolve = lambda s: {"symbol": s, "name": s}
engine.fetch_bars = lambda s, iv: DAILY
catalysts.fetch = lambda s: (_ for _ in ()).throw(RuntimeError("no calendar in tests"))


def call():
    r = engine.analyse("TEST", CFG, interval="1d", with_news=False, record=False)
    p = r.plan
    return (p.action, p.direction, p.entry, p.stop, p.target1, p.target2, p.probability,
            p.prob_low, p.prob_high, p.prob_samples, p.expectancy_r, round(r.composite, 15),
            r.calibration.hit_rate, r.calibration.samples), r


full, analysis = call()
with only_voting():
    bare, _ = call()
check("and a whole analysis gives the same call, entry, stop, target and odds",
      repr(full) == repr(bare), (full, bare))
check("while the analysis still shows the setups on trial",
      len(analysis.signals) == len(S.REGISTRY)
      and {s.name for s in analysis.signals} >= set(S.TRIAL))
measured = [n for n in S.TRIAL if n in analysis.strategy_report.overall]
check("and measures them on its history", len(measured) >= 5, measured)

# ---------------------------------------------------------------------------
head("EACH FIRES ON ITS SCENE, AND ONLY THERE")
# ---------------------------------------------------------------------------


def edited(bars, at, row, keep=None):
    """A scene with bar `at` replaced by (open, high, low, close), cut after keep."""
    o, h, l, c = (np.array(x, dtype=float) for x in (bars.open, bars.high, bars.low,
                                                       bars.close))
    o[at], h[at], l[at], c[at] = row
    n = len(c) if keep is None else keep
    return sc.make(o[:n], h[:n], l[:n], c[:n], bars.timestamp[:n], interval=bars.interval)


def daily_take(back=True):
    """Twenty-five quiet days; the last one trades below the day before's low
    (99.4) and closes back above it, at 100.0 (or below it, at 99.2)."""
    table = [(100.0, 100.6, 99.4, 100.1) if k % 2 else (100.1, 100.5, 99.5, 100.0)
             for k in range(24)]
    table.append((99.8, 100.3, 98.9, 100.0 if back else 99.2))
    return sc.rows(table, ts=sc.daily_times(len(table)), interval="1d")


def london_take(back=True, symbol="ES=F"):
    """A future's day from the Sunday 18:00 reopen: quiet at 100 (99.9 to 100.1),
    London (03:00 to 08:30) reaching 101.0 and 99.5, then at 09:00 New York a
    bar trading to 101.3 that closes back below 101.0 (at 100.8), or above it."""
    ts = sc.futures_times(200)
    n = len(ts)
    o = np.full(n, 100.0)
    c = np.full(n, 100.0)
    h = np.full(n, 100.1)
    l_ = np.full(n, 99.9)
    times = [clock.fmt_ny(int(t)) for t in ts]
    h[times.index("05:00")] = 101.0
    l_[times.index("06:00")] = 99.5
    at = times.index("09:00")
    o[at], h[at], l_[at], c[at] = 100.5, 101.3, 100.4, (100.8 if back else 101.2)
    return sc.make(o[:at + 3], h[:at + 3], l_[:at + 3], c[:at + 3], ts[:at + 3],
                   kind="future", symbol=symbol), at


stack = sc.stack_scene()
half = sc.half_scene()
LONDON, LONDON_AT = london_take()
SCENES = {
    # name: (bars, bar, sign, a price the reason names)
    "sweep_reclaim": (sc.trend_scene(), 19, 1, "101.5"),
    "prior_day_sweep": (daily_take(), 24, 1, "99.4"),
    "equal_levels_sweep": (sc.equal_scene(0.05), 28, -1, "100.95"),
    "session_sweep": (LONDON, LONDON_AT, -1, "101"),
    "structure_flip": (sc.trend_scene(), 21, -1, "101.5"),
    "sweep_then_flip": (sc.chain_scene(), 26, 1, "99.8"),
    # The stack scene with bar 10 dipping into the upper gap (10.6 to 10.9)
    # and closing up, in the rise that began at bar 7.
    "fvg_retest": (edited(stack, 10, (11.0, 11.3, 10.7, 11.2), keep=11), 10, 1, "10.6"),
    "ifvg_flip": (stack, 11, -1, "10.2"),
    # The 50% scene with bar 10 dipping to the line (10.5) and closing up.
    "premium_discount": (edited(half, 10, (10.6, 10.7, 10.4, 10.65)), 10, 1, "10.5"),
    "liquidity_model": (sc.chain_scene(), 30, 1, "99.8"),
}
QUIET = {
    "sweep_reclaim": sc.swing_scene(),
    "prior_day_sweep": daily_take(back=False),
    "equal_levels_sweep": sc.equal_scene(0.2),
    "session_sweep": london_take(back=False)[0],
    "structure_flip": sc.swing_scene(),
    "sweep_then_flip": sc.gap_scene(),
    "fvg_retest": stack,
    "ifvg_flip": sc.gap_scene(),
    "premium_discount": half,
    "liquidity_model": sc.chain_scene(broken=True),
}

for strat in NEW:
    bars, at, sign, price = SCENES[strat.name]
    ctx = S.build_context(bars)
    sig = strat.fn(ctx, at)
    check("%s fires %s at its bar, naming the price"
          % (strat.name, "up" if sign > 0 else "down"),
          sig.score * sign > 0 and sig.weight > 0 and price in sig.reason
          and sig.family == S.LIQUIDITY, (sig.score, sig.reason))
    quiet = QUIET[strat.name]
    qctx = S.build_context(quiet)
    loud = [(i, s.reason) for i in range(len(quiet))
            for s in [strat.fn(qctx, i)] if s.score]
    check("%s keeps quiet where its event is missing" % strat.name, not loud, loud[:2])

dctx = S.build_context(daily_take())
idle = S.session_sweep(dctx, 24)
check("the session sweep sits out on daily bars, and says why",
      idle.score == 0 and idle.weight == 0 and "shorter than a day" in idle.reason, idle)
sctx = S.build_context(sc.stock_series(300))
idle = S.session_sweep(sctx, 250)
check("and on a stock, which trades in the day session only",
      idle.score == 0 and idle.weight == 0 and "London" in idle.reason, idle)
check("its reason names which session's level was taken",
      "London session's high" in S.session_sweep(S.build_context(LONDON), LONDON_AT).reason)

# ---------------------------------------------------------------------------
head("SCORES ARE SANE AND NEVER READ A LATER BAR")
# ---------------------------------------------------------------------------
bad = []
for bars in (DAILY, STOCK, FUTURE):
    ctx = S.build_context(bars)
    for i in range(0, len(bars), 7):
        for strat in NEW:
            sig = strat.fn(ctx, i)
            if not (-1.0 <= sig.score <= 1.0) or sig.score != sig.score \
                    or not np.isfinite(sig.weight) or sig.weight < 0 \
                    or "nan" in sig.reason.lower() or "none" in sig.reason.lower():
                bad.append((strat.name, i, sig.score, sig.weight, sig.reason))
check("every score in [-1, 1], every weight finite, no blank prices in a reason",
      not bad, bad[:3])

moved = []
for bars, count in ((DAILY, 14), (STOCK, 13), (FUTURE, 13)):
    full_ctx = S.build_context(bars)
    for i in sorted(rng.sample(range(30, len(bars)), count)):
        cut_ctx = S.build_context(clock.head(bars, i + 1))
        for strat in NEW:
            a, b = strat.fn(full_ctx, i), strat.fn(cut_ctx, i)
            if (a.score, a.weight, a.reason) != (b.score, b.weight, b.reason):
                moved.append((strat.name, i, a.score, b.score))
check("each score at a bar is the same with every later bar cut off (40 bars, "
      "three series)", not moved, moved[:3])

# ---------------------------------------------------------------------------
head("THE DAILY BACK-TEST MEASURES THEM")
# ---------------------------------------------------------------------------
ctx = S.build_context(DAILY)
report = learning.measure_strategies(ctx, 1.5, 2.25, 10, 3, cost_bps=2.0)
counts = {n: (report.overall[n].n if n in report.overall else 0) for n in S.TRIAL}
print("   trades taken: %s" % counts)
daily_new = [n for n in S.TRIAL if n not in S.INTRADAY_ONLY]
check("every daily setup on trial takes trades on five years of daily bars",
      all(counts[n] > 0 for n in daily_new), counts)
check("and the session sweep takes none there", counts["session_sweep"] == 0, counts)

# ---------------------------------------------------------------------------
head("WHEN THE STRUCTURE CANNOT BE READ")
# ---------------------------------------------------------------------------
real_series = liquidity.series


def broken(bars, k=2):
    raise ValueError("made to fail")


S._STRUCTURE_FAILED_LOGGED = True        # keep the log quiet for this test
liquidity.series = broken
try:
    lost = S.build_context(STOCK)
finally:
    liquidity.series = real_series
kept = S.build_context(STOCK)
diff, not_idle = [], []
for i in range(kept.warmup, len(STOCK), 97):
    a = {s.name: (s.score, s.weight, s.reason) for s in S.evaluate(kept, i)[0]}
    b_sig, b_comp, _ = S.evaluate(lost, i)
    b = {s.name: (s.score, s.weight, s.reason) for s in b_sig}
    diff += [(n, i) for n in S.voting_names() if a[n] != b[n]]
    if abs(S.evaluate(kept, i)[1] - b_comp) > 1e-12:
        diff.append(("composite", i))
    not_idle += [(n, i) for n in S.TRIAL
                 if b[n][0] != 0 or b[n][1] != 0
                 or b[n][2] != "price structure could not be read"]
check("the 34 voting strategies and the composite are untouched", not diff, diff[:3])
check("and the setups on trial sit out, saying why", not not_idle, not_idle[:3])

# ---------------------------------------------------------------------------
head("THEY COST LITTLE TIME")
# ---------------------------------------------------------------------------
BIG = sc.futures_series(17000)
t0 = time.time()
big = S.build_context(BIG)
print("   context on %d five-minute bars: %.2fs" % (len(BIG), time.time() - t0))


def timed(rounds=2):
    best = None
    for _ in range(rounds):
        t = time.time()
        learning.measure_strategies(big, 1.5, 2.25, 24, 3, cost_bps=1.0)
        best = min(best or 1e9, time.time() - t)
    return best


with_trial = timed()
with only_voting():
    without = timed()
print("   measuring every strategy: %.2fs with the setups on trial, %.2fs without "
      "(%.2fx)" % (with_trial, without, with_trial / without))
check("measuring with them takes at most 1.35 times as long",
      with_trial <= 1.35 * without, (with_trial, without))

print()
print("%d failed" % len(fails) if fails else "ALL PASSED")
_sys.exit(1 if fails else 0)
