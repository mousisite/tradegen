"""The Setup lab: the course's setups traded on made-up intraday prices.

(a) Two runs on the same bars write the same records, byte for byte.
(b) On a pure random walk the lab finds no edge: fewer than one cell in ten
    keeps money by more than its margin, and at most one beats its random
    entries by more than its margin.
(c) A planted edge is found: after every sweep, break and retest the price
    rises, and the lab says the four-step model kept money and beat its
    random entries.
(d) Nothing peeks: a trade's entry, stop and targets are the same when the
    bars are cut at its entry; its random entries are on other days in the
    same half hour.
(e) Costs: charged as entry x cost / risk; a huge cost sinks every setup.
(f) The lab's walk follows calibrate.simulate_trade's rules exactly.
(g) A failing download is recorded and the other instruments are written; a
    refusal from the source stops the downloads.
(h) Every record written passes labstore.validate_result.
(i) A full run over eight instruments and two intervals stays under two
    minutes of CPU.
(j) due(): when the nightly run goes, on summer and winter dates.
No network.
"""
import json
import os as _os
import sqlite3
import sys as _sys
import time
from datetime import date

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

harness.isolate("setuplab")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import calibrate, clock, config, labstore, liquidity, market, setuplab, setups  # noqa: E402
from bot.strategies import Context  # noqa: E402

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


KIND = {"ES=F": "future", "NQ=F": "future", "BTC-USD": "coin", "ETH-USD": "coin"}
STEP = {"5m": 300, "15m": 900, "1h": 3600}


def random_walk(ts, seed, kind="future", interval="5m", price=6000.0, vol=0.0008,
                symbol=None):
    """A walk with no drift and no memory: nothing in it can be predicted."""
    rng = np.random.RandomState(seed)
    n = len(ts)
    close = price * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    open_ = np.concatenate([[price], close[:-1]])
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.5, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.5, n)))
    return sc.make(open_, high, low, close, ts, kind=kind, interval=interval, symbol=symbol)


def fake_bars(symbol, interval, days=None):
    """What fetch_bars might give: 60 days of 5- or 15-minute bars, or about
    two years of hourly ones, for each kind of instrument in the lab."""
    kind = KIND.get(symbol, "stock")
    step = STEP[interval]
    hourly = interval == "1h"
    days = days or (500 if hourly else 60)
    if kind == "future":
        start = date(2024, 11, 10) if hourly else date(2026, 10, 4)
        ts = sc.futures_times(int(days * 23 * 3600 / step), start=start, step=step)
    elif kind == "coin":
        ts = sc.coin_times(int(days * 86400 / step), step=step)
    elif hourly:
        ts = sc.stock_times(days * 7, step=3600, start=date(2024, 11, 1))
    else:
        ts = sc.stock_times(days * int(6.5 * 3600 / step), step=step)
    price = {"future": 6000.0, "coin": 90000.0}.get(kind, 200.0)
    seed = sum(map(ord, symbol + interval))
    return sc.walk(ts, seed=seed, kind=kind, interval=interval, price=price,
                   vol=0.0008 * (step / 300.0) ** 0.5, symbol=symbol)


def fake_fetch(fail=(), refuse=()):
    calls = []

    def fetch(symbol, interval):
        calls.append((symbol, interval))
        if symbol in refuse:
            raise market.DataThrottled("Yahoo Finance asked us to wait", 60)
        if symbol in fail:
            raise market.DataError("could not fetch %s: no data returned" % symbol)
        return fake_bars(symbol, interval)
    fetch.calls = calls
    return fetch


def memory():
    return sqlite3.connect(":memory:")


# ---------------------------------------------------------------------------
head("(f) THE WALK FOLLOWS calibrate.simulate_trade, TRADE FOR TRADE")
# ---------------------------------------------------------------------------
b = sc.walk(sc.futures_times(3000), seed=5, kind="future", price=6000.0, vol=0.0008)
s = liquidity.series(b)
ctx = Context(b, {"atr": s["st_atr"]}, 20)
rng = np.random.RandomState(9)
agree = 0
worst = ""
gapped = 0
for _ in range(200):
    i = int(rng.randint(30, 2900))
    d = 1 if rng.rand() < 0.5 else -1
    stop_atr = float(rng.uniform(0.3, 2.5))
    target_atr = float(rng.uniform(0.3, 4.0))
    horizon = int(rng.randint(3, 60))
    bps = float(rng.choice([0.0, 1.0, 5.0]))
    want = calibrate.simulate_trade(ctx, i, d, stop_atr, target_atr, horizon, bps)
    atr = float(s["st_atr"][i])
    entry = float(b.close[i])
    risk = stop_atr * atr
    stop = entry - d * risk
    target = entry + d * target_atr * atr
    px, hit, at = setuplab.walk(b.open, b.high, b.low, b.close, i, i + horizon, d, entry,
                                stop, [target])[0]
    cost_r = entry * bps / 10000.0 / risk
    r = d * (px - entry) / risk - cost_r
    same = (want is not None and want[0] == hit and abs(want[1] - r) < 1e-9
            and want[2] == at - i)
    agree += same
    if px not in (stop, target) and at < i + horizon:
        gapped += 1
    if not same and not worst:
        worst = (i, d, want, (hit, r, at - i))
check("200 random trades end the same way, at the same bar, for the same result",
      agree == 200, worst)
check("including trades whose stop was jumped at the open", gapped > 0, gapped)

# ---------------------------------------------------------------------------
head("(e) COSTS ARE CHARGED IN UNITS OF RISK")
# ---------------------------------------------------------------------------
es = fake_bars("ES=F", "5m", days=20)
res, trades = setuplab.measure(es, "ES=F", "5m", cfg=config.DEFAULTS)
check("a future is charged its own cost (1 basis point)", res["cost_bps"] == 1.0, res["cost_bps"])
sample = [t for rules in trades.values() for t in rules["2r"]][:300]
check("every trade's cost is entry x cost / 10,000 / risk",
      sample and all(abs(t.cost_r - t.entry * 1.0 / 10000.0 / t.risk) < 1e-12 for t in sample))
check("and what it kept is before costs minus that",
      all(abs(t.r - (t.r_gross - t.cost_r)) < 1e-12 for t in sample))
dear = dict(config.DEFAULTS)
dear["cost_bps_futures"] = 2000.0
res_dear, _ = setuplab.measure(es, "ES=F", "5m", cfg=dear)
cells = [(n, r) for n, c in res_dear["setups"].items() for r, rec in c["rules"].items()
         if rec["n"]]
check("a huge cost leaves every setup and target losing money",
      cells and all(res_dear["setups"][n]["rules"][r]["sum_r"] < 0 for n, r in cells),
      [(n, r) for n, r in cells if res_dear["setups"][n]["rules"][r]["sum_r"] >= 0])
check("the same trades are counted either way",
      all(res_dear["setups"][n]["rules"][r]["n"] == res["setups"][n]["rules"][r]["n"]
          for n, r in cells))

# ---------------------------------------------------------------------------
head("(d) NOTHING PEEKS: CUT THE BARS AT THE ENTRY AND THE PLAN IS THE SAME")
# ---------------------------------------------------------------------------
p = setuplab.prepare(es, liquidity.series(es), 1.0, "ES=F", "5m")
full = liquidity.series(es)
checked = same = 0
bad = []
for name in setuplab.SETUP_NAMES:
    lst = trades[name]["2r"] if name in trades else []
    lvl = {t.i: t for t in (trades[name]["level"] if name in trades else [])}
    picks = lst[::max(1, len(lst) // 4)][:4]
    for t in picks:
        cut = clock.head(es, t.i + 1)
        s_cut = liquidity.series(cut)
        p_cut = setuplab.prepare(cut, s_cut, 1.0, "ES=F", "5m")
        plan = setuplab.plan_for(name, s_cut, p_cut, t.i, t.direction)
        checked += 1
        ok = (not isinstance(plan, str) and abs(plan[0] - t.entry) < 1e-9
              and abs(plan[1] - t.stop) < 1e-9 and abs(plan[3]["2r"][0] - t.target) < 1e-9
              and abs(plan[3]["level"][0] - lvl[t.i].target) < 1e-9)
        same += ok
        if not ok:
            bad.append((name, t.i))
        # And the event itself is there on the cut bars.
        idx, dirs = setuplab.events(name, s_cut, cut)
        if not (idx.size and idx[-1] == t.i and dirs[-1] == t.direction):
            bad.append((name, t.i, "event"))
check("entry, stop and both targets read only bars up to the entry (%d trades, every setup)"
      % checked, checked >= 30 and same == checked and not bad, bad[:5])
ok_ctrl = n_ctrl = 0
for name in ("liquidity_model", "sweep_reclaim", "fvg_retest"):
    for t in trades[name]["2r"][:40]:
        for j in t.controls:
            n_ctrl += 1
            ok_ctrl += (p.day[j] != p.day[t.i] and p.minute[j] // 30 == p.minute[t.i] // 30
                        and p.good[j])
check("random entries come from other trading days, in the same New York half hour",
      n_ctrl > 50 and ok_ctrl == n_ctrl, (ok_ctrl, n_ctrl))
check("and are the same on every run",
      setuplab.controls(p, "sweep_reclaim", trades["sweep_reclaim"]["2r"][3].i)
      == list(trades["sweep_reclaim"]["2r"][3].controls))
check("no trade is entered on a bar that ends at 16:00 (no time left)",
      all(not (clock.fmt_ny(int(es.timestamp[t.i]) + 300) == "16:00")
          for rules in trades.values() for t in rules["2r"]))
late = []
for i in range(0, len(es.close) - 1, 7):
    end = int(es.timestamp[i]) + 300
    day_, minute, _ = clock.ny_clock(end)
    if minute > 16 * 60:
        day_ = date.fromordinal(day_.toordinal() + 1)
    x = clock.ny(day_.year, day_.month, day_.day, 16, 0)
    j = int(p.exit_i[i])
    if not (int(es.timestamp[j]) + 300 <= x
            and (j + 1 >= len(es.close) or int(es.timestamp[j + 1]) + 300 > x)):
        late.append(i)
check("each bar's last bar is the last one ending by the next 16:00 New York time",
      not late, late[:5])
check("and every trade ends by then",
      all(t.exit_i <= p.exit_i[t.i] for rules in trades.values() for t in rules["2r"]))

# ---------------------------------------------------------------------------
head("(a) TWO RUNS ON THE SAME BARS WRITE THE SAME RECORDS")
# ---------------------------------------------------------------------------
c1, c2 = memory(), memory()
setuplab.run(c1, fetch=fake_fetch(), symbols=["ES=F", "AAPL"], intervals=["15m"], pause=0,
             now=1795000000)
setuplab.run(c2, fetch=fake_fetch(), symbols=["ES=F", "AAPL"], intervals=["15m"], pause=0,
             now=1795000000)
rows1 = c1.execute("SELECT symbol, interval, data FROM lab_results ORDER BY symbol").fetchall()
rows2 = c2.execute("SELECT symbol, interval, data FROM lab_results ORDER BY symbol").fetchall()
check("identical JSON, byte for byte", rows1 and rows1 == rows2, len(rows1))
check("a partial run leaves the claims alone",
      c1.execute("SELECT COUNT(*) FROM lab_claims").fetchone()[0] == 0)
aapl = labstore.result(c1, "AAPL", "15m")
check("a stock is not tested for the session sweep, and says why",
      "session_sweep" not in aapl["setups"]
      and "day session" in aapl.get("not_tested", {}).get("session_sweep", ""))

# ---------------------------------------------------------------------------
head("(b) A PURE RANDOM WALK HAS NO EDGE, AND THE LAB SAYS SO")
# ---------------------------------------------------------------------------
walk = random_walk(sc.futures_times(60 * 276, start=date(2026, 10, 4)), seed=21,
                   symbol="ES=F")
res_rw, _ = setuplab.measure(walk, "ES=F", "5m")
plus = beat = total = 0
for name, cell in res_rw["setups"].items():
    for rule, rec in cell["rules"].items():
        total += 1
        mean = rec["sum_r"] / rec["n"] if rec["n"] else None
        if labstore.verdict(mean, rec["se"], rec["n"], days=rec["days"]) == 1:
            plus += 1
        if rec["diff"] is not None and rec["diff_se"] is not None \
                and rec["diff"] - 2 * rec["diff_se"] > 0:
            beat += 1
print("   (%d cells; %d kept money beyond their margin; %d beat random entries beyond it)"
      % (total, plus, beat))
check("fewer than one cell in ten keeps money by more than its margin", plus < 0.1 * total,
      plus)
check("at most one beats its random entries by more than its margin", beat <= 1, beat)

# ---------------------------------------------------------------------------
head("(c) A PLANTED EDGE IS FOUND")
# ---------------------------------------------------------------------------
# Each trading day: flat until the course's whole pattern plays out (the
# chain_scene: a fall, the lows taken, a break up, a pullback into the gap),
# then price rises 6 points (more than three times the usual bar's range,
# and three times the risk) before it ever falls back, and stays flat. The
# pattern starts at a different half hour each day, so the random entries at
# the same time of day on other days mostly find flat prices.
scene = sc.chain_scene()
table = [tuple(r) for r in zip(scene.open, scene.high, scene.low, scene.close)][:31]
days_n = 40
per = 276
ts_all = sc.futures_times(days_n * per, start=date(2026, 10, 4))
o, h, l_, c = [], [], [], []
level = 105.0
for k in range(days_n):
    day_ts = ts_all[k * per:(k + 1) * per]
    start_min = 7 * 60 + 30 * (k % 6)
    first = next(j for j, t in enumerate(day_ts) if clock.ny_clock(int(t))[1] == start_min)
    off = level - 105.0
    rows = [(level, level, level, level)] * first
    rows += [(a + off, b_ + off, c_ + off, d_ + off) for a, b_, c_, d_ in table]
    top = table[-1][3] + off
    for m in range(10):
        lo_ = top + 0.6 * m
        rows.append((lo_, lo_ + 0.65, lo_ - 0.05, lo_ + 0.6))
    level = rows[-1][3]
    rows += [(level, level, level, level)] * (per - len(rows))
    for r in rows:
        o.append(r[0])
        h.append(r[1])
        l_.append(r[2])
        c.append(r[3])
planted = sc.make(o, h, l_, c, ts_all, kind="future", symbol="ES=F")
s_pl = liquidity.series(planted)
chains = int(np.count_nonzero(s_pl["lq_chain"] == 1))
check("the pattern is found once a day", chains == days_n, chains)
res_pl, tr_pl = setuplab.measure(planted, "ES=F", "5m")
lm = res_pl["setups"]["liquidity_model"]["rules"]["2r"]
mean = lm["sum_r"] / lm["n"] if lm["n"] else None
v = labstore.verdict(mean, lm["se"], lm["n"], days=lm["days"])
check("the four-step model kept money by more than its margin", v == 1,
      (lm["n"], mean, lm["se"], lm["days"]))
check("and beat its random entries by more than its margin",
      lm["diff"] is not None and lm["diff_se"] is not None
      and lm["diff"] - 2 * lm["diff_se"] > 0, (lm["diff"], lm["diff_se"]))
check("its random entries did not find the same edge",
      lm["control"]["n"] and lm["control"]["sum_r"] / lm["control"]["n"] < 0.5 * mean,
      lm["control"])
check("an example trade is stored for the Learn page to draw",
      res_pl["setups"]["liquidity_model"].get("example", {}).get("direction") == 1)

# ---------------------------------------------------------------------------
head("(i) A FULL RUN, AND (h) EVERY RECORD IT WRITES IS VALID")
# ---------------------------------------------------------------------------
conn = memory()
cpu = time.process_time()
summary = setuplab.run(conn, fetch=fake_fetch(), pause=0, now=1795000000)
took = time.process_time() - cpu
print("   (8 instruments x 2 intervals, the claims and the bias study: %.1f s of CPU)" % took)
check("under 120 seconds of CPU with no pauses", took < 120, took)
check("every instrument and interval was measured", summary["checked"] == 16
      and not summary["errors"], summary["errors"])
stored = labstore.results(conn)
valid = 0
for rec in stored:
    try:
        labstore.validate_result(rec)
        valid += 1
    except ValueError as exc:
        print("      ", exc)
check("all 16 records are stored and pass validate_result", len(stored) == 16 and valid == 16,
      (len(stored), valid))
check("in the lab's order", [r["symbol"] for r in stored[::2]]
      == [s_[0] for s_ in setups.LAB_SYMBOLS])
check("every claim measured on a full run is stored",
      set(labstore.claims(conn)) == set(c_.id for c_ in setups.CLAIMS if c_.must),
      sorted(set(c_.id for c_ in setups.CLAIMS if c_.must) - set(labstore.claims(conn))))
check("and the bias study, per instrument and pooled",
      labstore.bias(conn, labstore.POOL_ALL) is not None
      and all(labstore.bias(conn, s_[0]) for s_ in setups.LAB_SYMBOLS))
last = labstore.last_run(conn)
check("the run is recorded", last and last["summary"]["checked"] == 16
      and "results" not in last["summary"])
text = setuplab.table(stored)
check("the printed table has no blanks, None or nan",
      "None" not in text and "nan" not in text and "Sweep, break and retest" in text)

# ---------------------------------------------------------------------------
head("(g) A FAILING DOWNLOAD IS RECORDED AND THE REST ARE WRITTEN")
# ---------------------------------------------------------------------------
conn = memory()
fetch = fake_fetch(fail=("SPY",))
summary = setuplab.run(conn, fetch=fetch, symbols=["ES=F", "SPY", "BTC-USD"],
                       intervals=["15m"], pause=1.5, sleep=lambda s_: None, now=1795000000)
check("the failure is in the summary, by name", any("SPY" in e for e in summary["errors"]),
      summary["errors"])
check("the other two are written", sorted(r["symbol"] for r in labstore.results(conn))
      == ["BTC-USD", "ES=F"])
waits = []
summary = setuplab.run(memory(), fetch=fake_fetch(refuse=("ES=F",)),
                       symbols=["AAPL", "ES=F", "SPY"], intervals=["15m"], pause=3.0,
                       sleep=waits.append, now=1795000000)
check("it waits between downloads", waits == [3.0], waits)
check("a refusal stops the downloads, and says so",
      summary["checked"] == 1 and any("asked to wait" in e for e in summary["errors"]),
      summary["errors"])

# ---------------------------------------------------------------------------
head("(j) WHEN THE NIGHTLY RUN GOES")
# ---------------------------------------------------------------------------
for label, y, mo, d in (("summer", 2026, 7, 15), ("winter", 2026, 12, 9)):
    conn = memory()
    at_1659 = clock.ny(y, mo, d, 16, 59)
    at_1706 = clock.ny(y, mo, d, 17, 6)
    check("%s: never ran, so it goes" % label, setuplab.due(conn, at_1659))
    labstore.save_run(conn, at_1706 - 19 * 3600, at_1706 - 19 * 3600 + 60, {"checked": 16})
    check("%s: ran 19 hours ago, so not yet" % label, not setuplab.due(conn, at_1706))
    conn = memory()
    labstore.save_run(conn, at_1659 - 21 * 3600, at_1659 - 21 * 3600 + 60, {"checked": 16})
    check("%s: ran 21 hours ago but it is 16:59 in New York: not yet" % label,
          not setuplab.due(conn, at_1659))
    conn = memory()
    labstore.save_run(conn, at_1706 - 21 * 3600, at_1706 - 21 * 3600 + 60, {"checked": 16})
    check("%s: ran 21 hours ago and it is 17:06 in New York: it goes" % label,
          setuplab.due(conn, at_1706))
conn = memory()
noon = clock.ny(2026, 7, 15, 12, 0)
labstore.save_run(conn, noon - 3 * 3600, noon - 3 * 3600 + 5, {"checked": 0})
check("a run that stored nothing is tried again after two hours", setuplab.due(conn, noon))
labstore.save_run(conn, noon - 3600, noon - 3600 + 5, {"checked": 0})
check("but not sooner", not setuplab.due(conn, noon))

# ---------------------------------------------------------------------------
head("THE SERVER'S NIGHTLY JOB MEASURES ONLY WHEN DUE")
# ---------------------------------------------------------------------------
import web  # noqa: E402
from bot import database as db_mod  # noqa: E402

harness.isolate("setuplab-web")
conn = db_mod.connect(harness.DB_PATH)
labstore.save_run(conn, int(time.time()) - 3600, int(time.time()) - 3000, {"checked": 16})
conn.close()
calls = []
real_run = setuplab.run
setuplab.run = lambda conn_, **kw: calls.append(kw) or {"checked": 16, "errors": []}
try:
    out = web._lab_job()
    check("an hour after a run it does nothing", out == {"checked": 0, "skipped": 1}
          and not calls, out)
    harness.isolate("setuplab-web-new")
    out = web._lab_job()
    check("on a server where it never ran it measures, on the house settings",
          out == {"checked": 16, "errors": []} and len(calls) == 1
          and calls[0]["cfg"]["cost_bps_futures"] == config.DEFAULTS["cost_bps_futures"],
          (out, len(calls)))
finally:
    setuplab.run = real_run

print()
print("%d failure(s)" % len(fails))
print("SETUP LAB OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
