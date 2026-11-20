"""The video's claims, each measured on prices scripted so the answer is known.

Each must-have claim in setups.CLAIMS on a series built for it: days where
London always reaches the other side of Asia's range (and a band the same
size, centred on 03:00, never does), New York and London's range likewise,
yesterday's levels, the open's spikes, bar sizes before and after 10:30,
gaps that are always retested, the 18:00 and weekend gaps filled at known
times, the lab's trades by window and setup, the daily lean on a market that
rises every day. Then: incomplete and roll days are left out and counted, a
claim with under 30 trials has no verdict, every record passes
labstore.validate_claim, and claims.run stores them. No network.
"""
import os as _os
import sqlite3
import sys as _sys
from datetime import date

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

harness.isolate("claims")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import claims, clock, labstore, setups, structure  # noqa: E402
from bot.setuplab import Trade  # noqa: E402

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


def since(hh, mm=0):
    """Minutes since the futures' 18:00 reopen."""
    return (hh * 60 + mm - 18 * 60) % (24 * 60)


def futures_days(paths, start=date(2026, 10, 4), step=300, wick=0.05, spikes=None,
                 symbol="ES=F", interval=None, drop_day=None):
    """A future's bars, one trading day per path.

    A path is [(minutes since 18:00, price), ...]; each bar closes at the
    path's price at its end and opens at the close before (the day's first
    bar opens at the path's starting price, so a path can open with a gap).
    spikes maps (day, minutes since 18:00 of a bar's start) to a high (above
    the price) or low (below). drop_day takes most bars out of one day.
    """
    per = int(23 * 3600 / step)
    ts = sc.futures_times(len(paths) * per, start=start, step=step)
    o, h, l_, c, keep = [], [], [], [], []
    for k, path in enumerate(paths):
        xs = [p[0] for p in path]
        ys = [p[1] for p in path]
        for j in range(per):
            t = int(ts[k * per + j])
            _, minute, _ = clock.ny_clock(t)
            start_min = (minute - 18 * 60) % (24 * 60)
            close = float(np.interp(start_min + step / 60.0, xs, ys))
            open_ = float(np.interp(0, xs, ys)) if j == 0 else c[-1]
            hi, lo = max(open_, close) + wick, min(open_, close) - wick
            spike = (spikes or {}).get((k, start_min))
            if spike is not None:
                if spike > close:
                    hi = max(hi, spike)
                else:
                    lo = min(lo, spike)
            o.append(open_)
            h.append(hi)
            l_.append(lo)
            c.append(close)
            keep.append(not (drop_day == k and j % 3 != 0))
    keep = np.array(keep)
    arr = [np.array(x)[keep] for x in (o, h, l_, c)]
    return sc.make(arr[0], arr[1], arr[2], arr[3], ts[keep], kind="future",
                   interval=interval or ("1h" if step == 3600 else "5m"), symbol=symbol)


DAYS = 40


# ---------------------------------------------------------------------------
head("LONDON TAKES ONE SIDE OF ASIA, THEN REACHES THE OTHER")
# ---------------------------------------------------------------------------
# Asia: 99 to 101. 03:00 opens at 100.5, so a band as wide as Asia centred
# there runs 99.45 to 101.55. London goes to 101.4 (past Asia's high, not the
# band's), then to 98.8 by 07:00 (past both lows): London reached Asia's other
# side; the band's other side was never reached.
LONDON = [(0, 100), (since(1), 101), (since(2, 30), 99), (since(3), 100.5),
          (since(5), 101.4), (since(7), 98.8), (since(12), 98.8), (since(17), 100)]
b = futures_days([LONDON] * DAYS)
rec = claims.london_takes_asia([b])
p = rec["pooled"]
check("every day London took Asia's high and then reached its low",
      p["trials"] == DAYS and p["hits"] == DAYS, p)
check("the band centred on 03:00 was taken on one side and never reached on the other",
      p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("so it happens more often than usual", rec["verdict"] == 1, rec["verdict"])
check("with the rate and its range", p["rate"] == 1.0 and 0.9 < p["low"] < 1.0, p)
few = claims.london_takes_asia([futures_days([LONDON] * 10)])
check("ten days are too few for a verdict", few["verdict"] is None and few["pooled"]["trials"] == 10)

# ---------------------------------------------------------------------------
head("NEW YORK TAKES ONE SIDE OF LONDON, THEN REACHES THE OTHER")
# ---------------------------------------------------------------------------
NY = [(0, 100), (since(3), 100), (since(5), 101), (since(7), 99), (since(8, 25), 100.6),
      (since(9, 30), 101.5), (since(11), 98.5), (since(16), 98.5), (since(17), 100)]
rec = claims.ny_takes_london([futures_days([NY] * DAYS)])
p = rec["pooled"]
check("every day New York took London's high and then its low by 16:00",
      p["trials"] == DAYS and p["hits"] == DAYS, p)
check("the band centred on 08:30 never had its other side reached",
      p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("verdict: more often than usual", rec["verdict"] == 1)

# ---------------------------------------------------------------------------
head("YESTERDAY'S HIGH TAKEN, THEN YESTERDAY'S LOW")
# ---------------------------------------------------------------------------
paths = [[(0, 100), (since(6), 100 + 3 + 0.02 * k), (since(12), 100 - 3 - 0.02 * k),
          (since(17), 100)] for k in range(DAYS)]
rec = claims.pd_sweep_reverse([futures_days(paths)])
p = rec["pooled"]
check("each day after the first took yesterday's high, then reached its low",
      p["trials"] == DAYS - 1 and p["hits"] == DAYS - 1, p)
check("a band as wide as yesterday's, centred on today's open, is yesterday's range here, "
      "so it did the same", p["base_trials"] == DAYS - 1 and p["base_hits"] == DAYS - 1, p)
check("and the verdict is that the two cannot be told apart", rec["verdict"] == 0)

# ---------------------------------------------------------------------------
head("9:30 TO 9:50 TAKES A LEVEL AND CLOSES BACK; 10:30 DOES NOT")
# ---------------------------------------------------------------------------
OPEN = [(0, 100), (since(3), 100), (since(5), 100.5), (since(7), 99.5), (since(8, 30), 100),
        (since(17), 100)]
spikes = {(k, since(9, 35)): 100.8 for k in range(DAYS)}
rec = claims.open_manipulation([futures_days([OPEN] * DAYS, spikes=spikes)])
p = rec["pooled"]
check("every day the 9:35 bar went past the levels and closed back inside",
      p["trials"] == DAYS and p["hits"] == DAYS, p)
check("from 10:30 to 10:55 nothing was taken", p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("verdict: more often than usual", rec["verdict"] == 1)

# ---------------------------------------------------------------------------
head("THE MARKET MOVES LESS AFTER 10:30")
# ---------------------------------------------------------------------------
ts = sc.stock_times(DAYS * 78)
mins = np.array([clock.ny_clock(int(t))[1] for t in ts])
size = np.where(mins < 10 * 60 + 30, 1.0, 0.2)
stock = sc.make(np.full(len(ts), 100.0), 100 + size / 2, 100 - size / 2, np.full(len(ts), 100.0),
                ts, kind="stock", symbol="SPY")
rec = claims.after_1030_slower([stock])
p = rec["pooled"]
check("every day the bars after 10:30 were smaller", p["trials"] == DAYS and p["hits"] == DAYS, p)
check("measured as an average, days counted once",
      rec["measure"] == "mean" and p["diff"] > 0 and p["mean"] < p["base_mean"], p)
check("verdict: it holds", rec["verdict"] == 1)

# ---------------------------------------------------------------------------
head("A FRESH GAP IS COME BACK TO")
# ---------------------------------------------------------------------------
# A six-bar cycle: a big up candle leaves a gap (100.3 to 100.6), the next
# bar dips into it and closes inside, then price settles back.
CYCLE = [(100.0, 100.3, 99.9, 100.2), (100.2, 101.2, 100.2, 101.1),
         (101.1, 101.6, 100.6, 101.5), (101.5, 101.55, 100.25, 100.5),
         (100.5, 100.6, 99.95, 100.0), (100.0, 100.3, 99.95, 100.0)]
rows = CYCLE * (3000 // 6)
gaps = sc.rows(rows, ts=sc.futures_times(len(rows), start=date(2026, 10, 4)), kind="future",
               symbol="ES=F")
s = structure.series(gaps)
made = int(np.count_nonzero(s["st_fvg_new"] == 1))
check("the cycle makes a gap up every six bars and no gap down",
      made >= 400 and not np.any(s["st_fvg_new"] == -1), made)
rec = claims.fvg_gets_retested([gaps])
p = rec["pooled"]
check("every gap was traded back into within 20 bars", p["trials"] > 400 and p["hits"] == p["trials"], p)
check("and the random zones were measured beside them", p["base_trials"] > 300, p)

# ---------------------------------------------------------------------------
head("A CLOSE PAST A SWING AGAINST A WICK: COUNTED AS A PLAIN LOOP WOULD")
# ---------------------------------------------------------------------------
walk = sc.walk(sc.futures_times(8000, start=date(2026, 10, 4)), seed=4, kind="future",
               price=6000.0, vol=0.0008)
rec = claims.close_beats_wick([walk])
s = structure.series(walk)
v = claims._View(walk)
want = {"trials": 0, "hits": 0, "base_trials": 0, "base_hits": 0}
H, L, C, O = walk.high, walk.low, walk.close, walk.open
for i in np.flatnonzero(s["st_sweep"] != 0):
    if i + 20 >= len(C) or not v.usable_bar[i] or v.rolls_in(i, i + 20):
        continue
    a = s["st_atr"][i]
    if not a > 0:
        continue
    way = -int(s["st_sweep"][i])
    far, back = C[i] + way * a, C[i] - way * a
    hit = False
    for k in range(i + 1, i + 21):
        went_back = (L[k] <= back) if way > 0 else (H[k] >= back)
        went_on = (H[k] >= far) if way > 0 else (L[k] <= far)
        if went_back:
            break
        if went_on:
            hit = True
            break
    key = "base_" if s["st_sweep_back"][i] else ""
    want[key + "trials"] += 1
    want[key + "hits"] += hit
got = {k: rec["pooled"][k] for k in want}
check("closes past and wicks past old swings, and how often each carried on",
      got == want and want["trials"] > 100 and want["base_trials"] > 100, (got, want))

# ---------------------------------------------------------------------------
head("THE 18:00 GAP: FILLED IN AN HOUR, IN FOUR, OR AFTER 9:30")
# ---------------------------------------------------------------------------
paths, kinds = [], []
for k in range(DAYS):
    kind = k % 3
    if kind == 0:
        path = [(0, 100.5), (30, 100), (since(17), 100)]
    elif kind == 1:
        path = [(0, 100.5), (since(20), 100.5), (since(21), 100), (since(17), 100)]
    else:
        path = [(0, 100.5), (since(16, 40), 100.5), (since(17), 100)]
    paths.append(path)
    kinds.append(kind)
b = futures_days(paths)
days = clock.day_table(b)
counted = [kinds[k] for k, d in enumerate(days) if k > 0 and d.weekday != 0]
rec = claims.ndog_fills([b])
p = rec["pooled"]
check("Monday's gap (the weekend's) and the first day are left out",
      p["trials"] == len(counted), (p["trials"], len(counted)))
check("filled within the hour, within four hours, and by 9:30 as scripted",
      p["hit_1h"] == counted.count(0) and p["hit_4h"] == counted.count(0) + counted.count(1)
      and p["hits"] == counted.count(0) + counted.count(1), p)
check("set against one in two", p["base_rate"] == 0.5 and rec["verdict"] in (1, 0), rec["verdict"])

# ---------------------------------------------------------------------------
head("THE WEEKEND GAP: FILLED MONDAY, LATER IN THE WEEK, OR NOT")
# ---------------------------------------------------------------------------
# Week w opens a point above the Friday close before it (F), then: kind 0
# trades back to F on Monday; kind 1 on Wednesday; kind 2 never. Each Friday
# rises to close 5 points higher, and that is the next week's F.
paths, plan = [], []
for w in range(DAYS // 5):
    f = 100.0 + 5 * w
    kind = w % 3
    plan.append(kind)
    for weekday in range(5):
        if weekday == 0 and kind == 0:
            path = [(0, f + 1), (since(2), f - 0.5), (since(17), f + 1)]
        elif weekday == 2 and kind == 1:
            path = [(0, f + 1), (since(10), f - 0.5), (since(17), f + 1)]
        elif weekday == 4:
            path = [(0, f + 1), (since(17), f + 5)]
        else:
            path = [(0, f + 1), (since(17), f + 1)]
        paths.append(path)
hourly = futures_days(paths, step=3600)
rec = claims.nwog_fills([hourly])
p = rec["pooled"]
weeks = plan[1:]
check("each week after the first is counted", p["trials"] == len(weeks), (p, weeks))
check("filled on Monday, and by Friday, as scripted",
      p["hit_monday"] == weeks.count(0) and p["hits"] == weeks.count(0) + weeks.count(1), p)

# ---------------------------------------------------------------------------
head("THE LAB'S OWN TRADES: BY TIME OF DAY, AND BY SETUP")
# ---------------------------------------------------------------------------


def trade(setup, r, day, window="after_cutoff", control=None):
    return Trade(setup=setup, rule="2r", i=0, exit_i=1, day=day, direction=1, entry=100.0,
                 stop=99.0, target=102.0, risk=1.0, r=r, r_gross=r, cost_r=0.0, bars=1,
                 hit=r > 0, window=window, target_r=2.0, control_r=control)


macro = [trade("liquidity_model", 1.0, 700000 + k, "macro") for k in range(40)]
other = [trade("liquidity_model", -0.5, 700000 + k, "midday") for k in range(60)]
rec = claims.macro_window({"ES=F": {"liquidity_model": {"2r": macro + other}}})
p = rec["pooled"]
check("trades from 9:50 to 10:10 against all the others",
      p["trials"] == 40 and p["base_trials"] == 60 and p["hits"] == 40, p)
check("verdict: they did better", rec["verdict"] == 1 and abs(p["diff"] - 1.5) < 1e-9)
crowd = [trade("crowd_breakout", -1.0, 700000 + k // 2, control=-0.1) for k in range(40)]
rec = claims.crowd_breakout_loses({"ES=F": {"crowd_breakout": {"2r": crowd}}})
check("breakout trades that all lost: the claim that it loses holds",
      rec["verdict"] == 1 and rec["pooled"]["mean"] == -1.0
      and abs(rec["pooled"]["diff"] + 0.9) < 1e-9, rec["pooled"])
won = [trade("crowd_breakout", 0.5 + 0.01 * (k % 3), 700000 + k) for k in range(40)]
rec = claims.crowd_breakout_loses({"ES=F": {"crowd_breakout": {"2r": won}}})
check("breakout trades that all won: the claim fails", rec["verdict"] == -1)
sweeps = [trade("sweep_reclaim", -0.5 + 0.01 * (k % 4), 700000 + k) for k in range(40)]
chain = [trade("liquidity_model", 0.5 + 0.01 * (k % 4), 700000 + k) for k in range(40)]
rec = claims.sweep_alone_loses({"ES=F": {"sweep_reclaim": {"2r": sweeps},
                                         "liquidity_model": {"2r": chain}}})
check("sweeps alone lost and the four steps did better: it holds", rec["verdict"] == 1,
      rec["pooled"])
pattern = [1.0, 1.0, -1.0, -1.0, -1.0, 1.0, -0.5]
days_ = [trade("liquidity_model", r, 700000 + k) for k, r in enumerate(pattern)]
rec = claims.not_every_day_green({"ES=F": {"liquidity_model": {"2r": days_}}})
check("green days and the longest losing run are described, with no verdict",
      rec["pooled"]["hits"] == 3 and rec["pooled"]["trials"] == 7
      and rec["pooled"]["longest_red"] == 3 and rec["verdict"] is None
      and rec["measure"] == "descriptive", rec["pooled"])

# ---------------------------------------------------------------------------
head("THE DAILY LEAN, ON A MARKET THAT RISES EVERY DAY")
# ---------------------------------------------------------------------------
# Every trading day: a dip from 18:00 to 08:00, then a rise to 17:00 that
# ends higher than the day before.
paths = []
for k in range(60):
    base = 6000.0 + 3.0 * k
    paths.append([(0, base), (since(8), base - 3.0), (since(9), base - 2.6),
                  (since(17), base + 3.0)])
up = futures_days(paths, step=3600, wick=0.2, start=date(2026, 9, 27))
study, skipped = claims.bias_study([up])
rec = study.get("ES=F")
check("the lean was read on most days", rec is not None and rec["called"] >= 40, rec)
check("it was right every day it leaned (the market rose every day)",
      rec is not None and rec["right"] == rec["called"] and rec["up_days"] == rec["days"], rec)
check("and a pooled record is kept", labstore.POOL_ALL in study
      and study[labstore.POOL_ALL]["called"] == rec["called"])
work = claims.bias_works(study, skipped)
check("set against always saying up, which was right as often: cannot be told apart",
      work["verdict"] == 0 and work["pooled"]["base_hits"] == work["pooled"]["hits"],
      work["pooled"])
check("the days without enough history before them are counted",
      skipped.get("too_little_history", 0) >= 1, skipped)
try:
    labstore.validate_bias({k: rec[k] for k in ("days", "called", "right", "up_days", "unclear",
                                                 "target_reached", "from", "to")})
    ok = True
except ValueError:
    ok = False
check("the study's record passes validate_bias", ok)

# ---------------------------------------------------------------------------
head("WHEN TWO RELATED MARKETS AGREE, THERE IS NOTHING TO COUNT")
# ---------------------------------------------------------------------------
w = sc.walk(sc.futures_times(6000, start=date(2026, 10, 4)), seed=8, kind="future",
            price=6000.0, vol=0.0008, symbol="ES=F")
twin = sc.make(w.open, w.high, w.low, w.close, w.timestamp, kind="future", symbol="NQ=F")
rec = claims.smt_at_sweep({"ES=F": w, "NQ=F": twin})
check("identical prices never disagree at a high or a low", rec["pooled"]["trials"] == 0,
      rec["pooled"])
check("but their shared new highs and lows past old ones are counted as the control",
      rec["pooled"]["base_trials"] > 50, rec["pooled"])
unrelated = sc.walk(sc.futures_times(6000, start=date(2026, 10, 4)), seed=9, kind="future",
                    price=21000.0, vol=0.001, symbol="NQ=F")
rec = claims.smt_at_sweep({"ES=F": w, "NQ=F": unrelated})
check("two unrelated markets disagree often", rec["pooled"]["trials"] > 50, rec["pooled"])

# ---------------------------------------------------------------------------
head("BAD DAYS ARE LEFT OUT AND COUNTED")
# ---------------------------------------------------------------------------
rolled = sc.futures_series(276 * 40, roll_day=12)
table_ = clock.day_table(rolled)
rolls = sum(1 for d in table_ if d.roll)
rec = claims.pd_sweep_reverse([rolled])
check("a contract roll is left out and counted", rolls >= 1 and rec["skipped"].get("roll", 0) >= rolls,
      (rolls, rec["skipped"]))
holed = futures_days([LONDON] * 12, drop_day=5)
rec = claims.london_takes_asia([holed])
check("a day with a third of its bars is left out and counted",
      rec["skipped"].get("incomplete") == 1 and rec["pooled"]["trials"] == 11, rec)

# ---------------------------------------------------------------------------
head("claims.run STORES EVERY CLAIM IT COULD MEASURE, EACH VALID")
# ---------------------------------------------------------------------------
conn = sqlite3.connect(":memory:")
five = futures_days([LONDON] * DAYS)
fetched = []


def fetch(symbol, interval):
    fetched.append((symbol, interval))
    return futures_days(paths, step=3600, wick=0.2, start=date(2026, 9, 27))


out = claims.run(conn, {("ES=F", "5m"): five}, fetch=fetch,
                 trades={"ES=F": {"liquidity_model": {"2r": macro + other}}}, pause=0)
stored = labstore.claims(conn)
check("hourly prices are fetched for the lean and the weekly gaps", fetched == [("ES=F", "1h")])
check("no errors", not out["errors"], out["errors"])
check("what was measured is stored", "london_takes_asia" in stored and "macro_window" in stored
      and "bias_works" in stored and labstore.bias(conn, "ES=F") is not None, sorted(stored))
check("a claim with nothing to measure is not written (smt needs two markets)",
      "smt_at_sweep" not in stored)
bad = []
for cid, data in stored.items():
    try:
        labstore.validate_claim(data)
    except ValueError as exc:
        bad.append((cid, str(exc)))
check("every stored record passes validate_claim", not bad, bad)
words = " ".join(" ".join(r["notes"]) for r in stored.values()).lower()
check("the notes use no jargon",
      not any(j in words for j in ("expectancy", "r-multiple", "confidence interval", " atr",
                                   "wilson", "basis point", "sharpe", "drawdown")), words[:200])

print()
print("%d failure(s)" % len(fails))
print("CLAIMS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
