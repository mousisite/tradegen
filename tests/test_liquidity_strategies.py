"""The course's price-action setups, which vote like every other strategy.

Ten strategies from TJR's video have had a say in the composite, the odds and
every call since 19 November 2026. This suite pins that they do, by the rule
every strategy follows (one voice in full from a family, later ones less, idle
ones not at all), and that votes=False still keeps one out. Then that each
setup fires on a scene built to show it (with the right sign and a reason
naming the price), keeps quiet where there is nothing, never reads a bar after
the one it scores, is measured by the daily back-test, sits out cleanly (and
takes nothing else with it) when the price structure cannot be read, and costs
little time.

No network: every price here is made up (tests/scenarios.py).
"""
import os as _os
import random
import sys as _sys
import time
import warnings

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import dataclasses  # noqa: E402

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


NEW = [s for s in S.REGISTRY if s.family == S.LIQUIDITY]
TEN = {s.name for s in NEW}
OTHERS = [s for s in S.REGISTRY if s.family != S.LIQUIDITY]


class registry:
    """Another registry for the length of a block: without the ten, or with
    some of them put back on trial."""

    def __init__(self, strategies):
        self.strategies = list(strategies)

    def __enter__(self):
        self.real = S.REGISTRY
        S.REGISTRY = self.strategies

    def __exit__(self, *exc):
        S.REGISTRY = self.real


DAILY = sc.daily_series(1250)
STOCK = sc.stock_series(2500)
FUTURE = sc.futures_series(4000, roll_day=None)

# ---------------------------------------------------------------------------
head("THE TEN SETUPS")
# ---------------------------------------------------------------------------
check("ten setups, in the liquidity family",
      len(NEW) == 10 and S.LIQUIDITY in S.FAMILIES, [s.name for s in NEW])
check("each a strategy the setup catalog names",
      len(NEW) == 10
      and all(n in __import__("bot.setups", fromlist=["x"]).REGISTRY_NAMES for n in (s.name for s in NEW)))
check("all forty-four strategies vote (the owner switched the setups on, 19 Nov 2026)",
      len(S.voting_names()) == 44 and S.voting_names() == S.strategy_names()
      and all(s.votes for s in NEW) and S.TRIAL == () and len(OTHERS) == 34)
check("the session sweep needs intraday bars",
      "session_sweep" in S.INTRADAY_ONLY and "session_sweep" not in S.daily_strategy_names())
check("each weighs 0.7 at the start",
      all(abs(s.base_weight - 0.7) < 1e-12 for s in NEW))

# ---------------------------------------------------------------------------
head("THEY HAVE A SAY IN THE CALL, BY THE RULE EVERY FAMILY FOLLOWS")
# ---------------------------------------------------------------------------


def by_hand(signals, damped=True):
    """The composite worked out here: in each family the loudest voice counts
    in full, the next ones 1/1.45, 1/1.9, 1/2.35 and so on; idle ones nothing.
    damped=False gives every liquidity voice its full say instead."""
    top = bottom = 0.0
    for family in S.FAMILIES:
        voices = sorted((s for s in signals if s.family == family
                         and s.weight > 0 and abs(s.score) > 0.05),
                        key=lambda s: -abs(s.score * s.weight))
        for rank, s in enumerate(voices):
            share = 1.0 / (1.0 + 0.45 * rank)
            if family == S.LIQUIDITY and not damped:
                share = 1.0
            top += s.score * s.weight * share
            bottom += s.weight * share
    return max(-1.0, min(1.0, top / bottom)) if bottom else 0.0


rng = random.Random(7)
bars_seen = spoke = moved_bars = crowded = 0
wrong_sum, wrong_move, wrong_others, undamped = [], [], [], 0
for name, bars in (("daily", DAILY), ("stock", STOCK), ("future", FUTURE)):
    ctx = S.build_context(bars)
    picks = sorted(rng.sample(range(ctx.warmup, len(bars)), 60))
    learned = {s.name: 0.4 + 0.02 * k for k, s in enumerate(S.REGISTRY)}
    for i in picks:
        bars_seen += 1
        sig_all, comp_all, reg_all = S.evaluate(ctx, i)
        sig_l, comp_l, _ = S.evaluate(ctx, i, learned=learned)
        with registry(OTHERS):
            sig_v, comp_v, reg_v = S.evaluate(ctx, i)
        for sigs, comp in ((sig_all, comp_all), (sig_l, comp_l), (sig_v, comp_v)):
            if abs(by_hand(sigs) - comp) > 1e-12:
                wrong_sum.append((name, i, by_hand(sigs), comp))
        voices = [s for s in sig_all if s.name in TEN and s.weight > 0 and abs(s.score) > 0.05]
        spoke += bool(voices)
        crowded += len(voices) > 1
        moved_bars += abs(comp_all - comp_v) > 1e-12
        # A family with nothing to say leaves the number alone, and one with a
        # voice moves it (unless that voice says exactly what the rest say).
        if bool(voices) != (abs(comp_all - comp_v) > 1e-12):
            wrong_move.append((name, i, len(voices), comp_all, comp_v))
        if {s.name: (s.score, s.weight) for s in sig_v} != {
                s.name: (s.score, s.weight) for s in sig_all if s.name not in TEN} \
                or reg_all.trend != reg_v.trend:
            wrong_others.append((name, i))
        if len(voices) > 1:
            # With a full say for each of its voices, the number would differ.
            undamped += abs(by_hand(sig_all, damped=False) - comp_all) > 1e-9
print("   (one or more spoke at %d of %d bars, two or more at %d, and the composite moved at %d)"
      % (spoke, bars_seen, crowded, moved_bars))
check("the composite is the family-damped mean of all 44, worked by hand, "
      "learned weights or not", bars_seen == 180 and not wrong_sum, wrong_sum[:2])
check("it moves where a setup speaks, and nowhere else",
      spoke > 20 and not wrong_move, wrong_move[:3])
check("a crowded liquidity family is damped, as any other is",
      crowded > 3 and undamped == crowded, (crowded, undamped))
check("and the other 34 score and weigh the same beside them", not wrong_others, wrong_others[:3])

# Put back on trial, one by one or together, they are scored and left out.
ctx = S.build_context(DAILY)
on_trial = [dataclasses.replace(s, votes=s.family != S.LIQUIDITY) for s in S.REGISTRY]
leaks = []
for i in range(ctx.warmup, len(DAILY), 41):
    with registry(OTHERS):
        comp_v = S.evaluate(ctx, i)[1]
    with registry(on_trial):
        sig_t, comp_t, _ = S.evaluate(ctx, i)
    if comp_t != comp_v or len(sig_t) != 44:
        leaks.append((i, comp_t, comp_v))
check("votes=False still keeps a strategy out of the composite", not leaks, leaks[:3])

score = S.evaluate(ctx, len(DAILY) - 1)[1]
calib_all = cal.calibrate(ctx, score, 1.5, 2.25, 10, stride=3, cost_bps=2.0)
with registry(OTHERS):
    calib_v = cal.calibrate(ctx, score, 1.5, 2.25, 10, stride=3, cost_bps=2.0)
check("the odds are measured with them voting too",
      calib_all.samples > 0 and repr(calib_all.__dict__) != repr(calib_v.__dict__),
      (calib_all.hit_rate, calib_v.hit_rate))

CFG = config.load()
engine.resolve = lambda s: {"symbol": s, "name": s}
engine.fetch_bars = lambda s, iv: DAILY
catalysts.fetch = lambda s: (_ for _ in ()).throw(RuntimeError("no calendar in tests"))
analysis = engine.analyse("TEST", CFG, interval="1d", with_news=False, record=False)
check("a whole analysis takes its composite from all 44",
      len(analysis.signals) == 44 and TEN <= {s.name for s in analysis.signals}
      and abs(by_hand(analysis.signals) - analysis.composite) < 1e-9,
      (by_hand(analysis.signals), analysis.composite))
measured = sorted(n for n in TEN if n in analysis.strategy_report.overall)
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
counts = {n: (report.overall[n].n if n in report.overall else 0) for n in [s.name for s in NEW]}
print("   trades taken: %s" % counts)
daily_new = [n for n in [s.name for s in NEW] if n not in S.INTRADAY_ONLY]
check("every daily setup takes trades on five years of daily bars",
      all(counts[n] > 0 for n in daily_new), counts)
check("and the session sweep takes none there", counts["session_sweep"] == 0, counts)

# ---------------------------------------------------------------------------
head("THE PAGES COUNT THEM WITH THE REST")
# ---------------------------------------------------------------------------
for key in ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET", "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(key, None)
import web  # noqa: E402
from bot import database as db  # noqa: E402
from bot import public  # noqa: E402

client, DB = harness.isolated_web("liquidity_pages")
web.app.config["AUTH_MODE"] = None
page = " ".join(client.get("/backtest/liquidity-model").data.decode("utf-8", "replace").split())
check("/backtest/liquidity-model draws, with nothing about a trial",
      "Does the sweep, break and retest model work?" in page
      and "on trial" not in page and "Measured here, not used" not in page, page[:200])
page = " ".join(client.get("/backtest/session-sweep").data.decode("utf-8", "replace").split())
check("the session sweep's page says a call still uses it, and where its test will be",
      "Not in the daily test" in page and "An analysis on shorter bars still uses it." in page
      and "5- and 15-minute prices is shown on this page once it has run" in page)
page = " ".join(client.get("/backtest/opening-range-break").data.decode("utf-8", "replace").split())
check("the other intraday strategy promises no such test", "Not in the daily test" in page
      and "5- and 15-minute prices" not in page)
rollup = {r["name"]: r for r in web._family_rollup(analysis)}
check("the analysis page's families have a liquidity row of ten",
      rollup.get("liquidity", {}).get("total") == 10, rollup.get("liquidity"))


def listed(trades):
    """Store a test of the model on NVDA with this many separate trades, and
    read the strategy tests page."""
    conn = db.connect(DB)
    try:
        public.save_verdict(conn, {"symbol": "NVDA", "name": "NVIDIA", "action": "WAIT",
                                   "at": int(time.time()),
                                   "tests": {"liquidity_model": [trades, trades // 2, 3.0],
                                             "rsi_reversion": [60, 30, 2.0]}})
    finally:
        conn.close()
    return client.get("/backtests").data.decode("utf-8", "replace")


page = listed(39)
check("the strategy tests leave it off with fewer than 40 separate trades",
      "Sweep, break and retest" not in page and "RSI reversion" in page)
page = listed(40)
row_at = page.find(">Sweep, break and retest</a>")
check("and list it once it has 40, with no mark on it",
      row_at > 0 and "chip" not in page[row_at:row_at + 200])
check("saying all 44 decide the call",
      "44 strategies decide Orenth's calls." in " ".join(page.split()) and "on trial" not in page)

# ---------------------------------------------------------------------------
head("WHEN THE STRUCTURE CANNOT BE READ")
# ---------------------------------------------------------------------------
real_series = liquidity.series


def broken(bars, k=2):
    raise ValueError("made to fail")


S._STRUCTURE_FAILED[STOCK.symbol] = time.time()        # keep the log quiet for this test
liquidity.series = broken
try:
    lost = S.build_context(STOCK)
finally:
    liquidity.series = real_series
kept = S.build_context(STOCK)
diff, not_idle, changed = [], [], 0
for i in range(kept.warmup, len(STOCK), 97):
    a_sig, a_comp, _ = S.evaluate(kept, i)
    a = {s.name: (s.score, s.weight, s.reason) for s in a_sig}
    b_sig, b_comp, _ = S.evaluate(lost, i)
    b = {s.name: (s.score, s.weight, s.reason) for s in b_sig}
    diff += [(s.name, i) for s in OTHERS if a[s.name] != b[s.name]]
    with registry(OTHERS):
        others_only = S.evaluate(kept, i)[1]
    if b_comp != others_only:
        diff.append(("composite", i, b_comp, others_only))
    changed += abs(a_comp - b_comp) > 1e-12
    not_idle += [(n, i) for n in TEN
                 if b[n][0] != 0 or b[n][1] != 0
                 or b[n][2] != "price structure could not be read"]
check("the other 34 are untouched, and the call is the one they make alone",
      not diff and changed > 0, (changed, diff[:3]))
check("and the ten sit out, saying why", not not_idle, not_idle[:3])

# ---------------------------------------------------------------------------
head("THEY COST LITTLE TIME")
# ---------------------------------------------------------------------------
BIG = sc.futures_series(17000)
t0 = time.time()
big = S.build_context(BIG)
print("   context on %d five-minute bars: %.2fs" % (len(BIG), time.time() - t0))


def timed():
    t = time.time()
    learning.measure_strategies(big, 1.5, 2.25, 24, 3, cost_bps=1.0)
    return time.time() - t


# Turn about, and the best of three: a busy machine slows both alike.
with_ten = without = 1e9
for _ in range(3):
    with_ten = min(with_ten, timed())
    with registry(OTHERS):
        without = min(without, timed())
print("   measuring every strategy: %.2fs with the ten, %.2fs without "
      "(%.2fx)" % (with_ten, without, with_ten / without))
check("measuring with them takes at most 1.35 times as long",
      with_ten <= 1.35 * without, (with_ten, without))

print()
print("%d failed" % len(fails) if fails else "ALL PASSED")
_sys.exit(1 if fails else 0)
