"""The video's claims, each measured on prices scripted so the answer is known.

Each must-have claim in setups.CLAIMS on a series built for it: days where
London always reaches the other side of Asia's range (and the range mirrored
around 03:00 never has its other side reached), New York and London's range
likewise, yesterday's levels, the open's spikes, bar sizes before and after
10:30, gaps that are always retested, the 18:00 and weekend gaps filled at
known times, the lab's trades by window and setup, the daily lean on a market
that rises every day. Then random walks with nothing in them, where no claim
may be found to hold. Then: incomplete and roll days are left out and
counted, a claim with under 30 trials has no verdict, every record passes
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
from bot.setuplab import CLAIM_SERIES, Trade  # noqa: E402

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
# Asia: 99 to 101. 03:00 opens at about 100.5, so Asia mirrored there runs
# from about 100 (as far below as Asia's high is above) to 102 (as far above as
# Asia's low is below). London goes to 101.4 (past Asia's high, not the
# mirror's), then to 98.8 by 07:00 (past both lows): London reached Asia's
# other side; the mirror's other side was never reached.
LONDON = [(0, 100), (since(1), 101), (since(2, 30), 99), (since(3), 100.5),
          (since(5), 101.4), (since(7), 98.8), (since(12), 98.8), (since(17), 100)]
b = futures_days([LONDON] * DAYS)
rec = claims.london_takes_asia([b])
p = rec["pooled"]
check("every day London took Asia's high and then reached its low",
      p["trials"] == DAYS and p["hits"] == DAYS, p)
check("Asia mirrored around 03:00 was taken on one side and never reached on the other",
      p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("so the claim holds", rec["verdict"] == 1, rec["verdict"])
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
check("London mirrored around 08:30 never had its other side reached",
      p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("verdict: it holds", rec["verdict"] == 1)

# ---------------------------------------------------------------------------
head("YESTERDAY'S HIGH TAKEN, THEN YESTERDAY'S LOW")
# ---------------------------------------------------------------------------
paths = [[(0, 100), (since(6), 100 + 3 + 0.02 * k), (since(12), 100 - 3 - 0.02 * k),
          (since(17), 100)] for k in range(DAYS)]
rec = claims.pd_sweep_reverse([futures_days(paths)])
p = rec["pooled"]
check("each day after the first took yesterday's high, then reached its low",
      p["trials"] == DAYS - 1 and p["hits"] == DAYS - 1, p)
check("yesterday's range mirrored around today's open is yesterday's range here, "
      "so it did the same", p["base_trials"] == DAYS - 1 and p["base_hits"] == DAYS - 1, p)
check("and the verdict is that the two cannot be told apart", rec["verdict"] == 0)

check("a range is mirrored: as far above as it was below, and the other way round",
      claims._mirrored(100.0, 101.0, 96.0) == (104.0, 99.0))

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
check("an hour later nothing was taken", p["base_trials"] == DAYS and p["base_hits"] == 0, p)
check("verdict: it holds", rec["verdict"] == 1)
late_spikes = {(k, since(9, 50)): 100.8 for k in range(DAYS)}
rec = claims.open_manipulation([futures_days([OPEN] * DAYS, spikes=late_spikes)])
check("a bar that starts at 9:50 is past the window, and is not counted",
      rec["pooled"]["trials"] == DAYS and rec["pooled"]["hits"] == 0, rec["pooled"])
hour_on = {(k, since(10, 35)): 100.8 for k in range(DAYS)}
rec = claims.open_manipulation([futures_days([OPEN] * DAYS, spikes=hour_on)])
check("the same spike an hour later is the comparison's, past a range that ended at 10:30",
      rec["pooled"]["hits"] == 0 and rec["pooled"]["base_hits"] == DAYS, rec["pooled"])

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
check("set against the level the same distance the other way, which was never reached",
      p["base_trials"] == len(counted) and p["base_hits"] == 0 and p["base_hit_1h"] == 0
      and p["base_hit_4h"] == 0 and rec["verdict"] == 1, p)
# A gap that fills only because price swings both ways past the open.
swings = [[(0, 100.5), (30, 99.9), (90, 101.2), (since(3), 100), (since(17), 100)]] * DAYS
rec = claims.ndog_fills([futures_days(swings)])
p = rec["pooled"]
check("gaps filled every day, and as far the other way every day, show no pull back",
      p["hits"] == p["trials"] == p["base_hits"] and p["trials"] >= 30
      and rec["verdict"] == 0, (p, rec["verdict"]))

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
week_paths = list(paths)
hourly = futures_days(paths, step=3600)
rec = claims.nwog_fills([hourly])
p = rec["pooled"]
weeks = plan[1:]
check("each week after the first is counted", p["trials"] == len(weeks), (p, weeks))
check("and as far the other way: the weeks that rose 1 point past the open by Friday 16:00",
      p["base_trials"] == len(weeks) and p["base_hit_monday"] == 0, p)
cut = futures_days(paths[:-2], step=3600)
rec = claims.nwog_fills([cut])
check("a week still under way is left out, filled or not",
      rec["pooled"]["trials"] == len(weeks) - 1 and rec["skipped"].get("week_not_over") == 1,
      (rec["pooled"], rec["skipped"]))
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

# Two instruments: a dear one whose trades all came at other times and lost
# heavily, and one where 9:50-10:10 did exactly as well as other times.
# Pooled, the window would look better; inside each instrument it is not.
mixed = {"BTC-USD": {"liquidity_model": {"2r": [
            trade("liquidity_model", -2.0 + 0.01 * (k % 5), 700000 + k, "asia")
            for k in range(60)]}},
         "ES=F": {"liquidity_model": {"2r": [
            trade("liquidity_model", 0.2 * ((k % 5) - 2), 700000 + k, "macro")
            for k in range(40)] + [
            trade("liquidity_model", 0.2 * ((k % 5) - 2), 700000 + k, "midday")
            for k in range(40)]}}}
rec = claims.macro_window(mixed)
p = rec["pooled"]
check("the window is compared inside each instrument, so a dear coin trading at night "
      "does not make it look better", p["mean"] - p["base_mean"] > 0.5
      and abs(p["diff"]) < 1e-9 and rec["verdict"] == 0, p)
check("each instrument's own averages are kept", rec["by_symbol"]["ES=F"]["mean"] == 0.0
      and rec["by_symbol"]["BTC-USD"]["base_mean"] < -1.9, rec["by_symbol"])
crowd = [trade("crowd_breakout", -1.0, 700000 + k // 2, control=-0.1) for k in range(40)]
sells = [Trade(**dict(t.__dict__, direction=-1, r=5.0)) for t in crowd[:20]]
rec = claims.crowd_breakout_loses({"ES=F": {"crowd_breakout": {"2r": crowd + sells}}})
check("breakout buys that all lost, and lost more than random entries: the claim holds",
      rec["verdict"] == 1 and rec["pooled"]["mean"] == -1.0
      and abs(rec["pooled"]["diff"] + 0.9) < 1e-9, rec["pooled"])
check("the sells the lab also measures are not counted in a claim about buying",
      rec["pooled"]["trials"] == 40, rec["pooled"])
costs = [trade("crowd_breakout", -0.3 + 0.1 * (k % 3), 700000 + k, control=-0.2)
         for k in range(42)]
rec = claims.crowd_breakout_loses({"ES=F": {"crowd_breakout": {"2r": costs}}})
check("buys that lost to costs as much as random entries did: it cannot be told either way",
      labstore.verdict(rec["pooled"]["mean"], rec["pooled"]["se"], 42) == -1
      and rec["verdict"] == 0, (rec["pooled"], rec["verdict"]))
won = [trade("crowd_breakout", 0.5 + 0.01 * (k % 3), 700000 + k, control=0.0)
       for k in range(40)]
rec = claims.crowd_breakout_loses({"ES=F": {"crowd_breakout": {"2r": won}}})
check("breakout trades that all won, and beat random entries: the claim fails",
      rec["verdict"] == -1)
sweeps = [trade("sweep_reclaim", -0.5 + 0.01 * (k % 4), 700000 + k, control=-0.2)
          for k in range(40)]
chain = [trade("liquidity_model", 0.5 + 0.01 * (k % 4), 700000 + k, control=-0.1)
         for k in range(40)]
rec = claims.sweep_alone_loses({"ES=F": {"sweep_reclaim": {"2r": sweeps},
                                         "liquidity_model": {"2r": chain}}})
check("sweeps alone lost, more than random entries, and the four steps did better: it holds",
      rec["verdict"] == 1, rec["pooled"])
check("set beside the sweeps' own random entries, with the four steps' figures in the notes",
      abs(rec["pooled"]["base_mean"] + 0.2) < 1e-9 and "+0.52R" in " ".join(rec["notes"]),
      (rec["pooled"], rec["notes"]))
# Nothing but costs: each setup does what its random entries do, and a close
# stop makes the sweeps' costs many more units of risk than the model's.
sweeps = [trade("sweep_reclaim", -2.0 + 0.2 * (k % 5 - 2), 700000 + k, control=-2.0)
          for k in range(40)]
chain = [trade("liquidity_model", -0.4 + 0.2 * (k % 5 - 2), 700000 + k, control=-0.4)
         for k in range(40)]
rec = claims.sweep_alone_loses({"ES=F": {"sweep_reclaim": {"2r": sweeps},
                                         "liquidity_model": {"2r": chain}}})
check("sweeps that lose only their costs are not called worse than the four steps",
      rec["verdict"] == 0 and abs(rec["pooled"]["diff"]) < 1e-9, (rec["pooled"], rec["verdict"]))
# Two instruments that had the same days are one market's evidence, not two.
days_ = [trade("liquidity_model", 0.3 if k % 2 else -0.2, 700000 + k) for k in range(40)]
one = claims._mean_se([("ES=F", t) for t in days_])
two = claims._mean_se([(sym, t) for sym in ("ES=F", "SPY") for t in days_])
check("the same days in two instruments leave the margin as wide as one's",
      abs(one[1] - two[1]) < 1e-12 and two[2] == 80, (one, two))
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
head("PRICES WITH NOTHING IN THEM: NO CLAIM IS FOUND TO HOLD")
# ---------------------------------------------------------------------------
# Random walks: no drift, no memory, no time of day. Each claim and its
# control must come out alike, or the test itself makes the pattern.


def nothing(seed, symbol, kind="future", days=60, jump=0.0, step=300):
    """A random walk; with jump, the price steps at each reopen and the walk
    goes on from there, with nothing pulling it back."""
    n = int(days * (23 if kind == "future" else 24) * 3600 / step)
    ts_ = (sc.futures_times(n, start=date(2026, 10, 4), step=step) if kind == "future"
           else sc.coin_times(n, step=step))
    rng = np.random.RandomState(seed)
    vol = 0.0008 * (step / 300.0) ** 0.5
    rets = rng.normal(0.0, vol, n)
    steps_ = np.where(np.diff(ts_, prepend=ts_[0]) > 1.5 * step,
                      rng.normal(0.0, vol * jump, n), 0.0) if jump else np.zeros(n)
    level = np.cumsum(rets + steps_)
    close, open_ = 6000.0 * np.exp(level), 6000.0 * np.exp(level - rets)
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.5, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.5, n)))
    return sc.make(open_, high, low, close, ts_, kind=kind,
                   interval="1h" if step == 3600 else "5m", symbol=symbol)


flat = [nothing(51, "ES=F"), nothing(52, "NQ=F"), nothing(53, "BTC-USD", "coin"),
        nothing(54, "ETH-USD", "coin")]
rec = claims.open_manipulation(flat)
p = rec["pooled"]
check("9:30 to 9:50 and the hour later take and lose their levels alike",
      p["trials"] > 200 and abs(p["rate"] - p["base_rate"]) < 0.12 and rec["verdict"] == 0,
      (p, rec["verdict"]))
tally = [0, 0, 0, 0]
for k in range(6):
    many = [nothing(100 + 10 * k + j, s_) for j, s_ in enumerate(("ES=F", "NQ=F"))] \
        + [nothing(105 + 10 * k + j, s_, "coin") for j, s_ in enumerate(("BTC-USD", "ETH-USD"))]
    p = claims.ny_takes_london(many)["pooled"]
    tally = [a + b_ for a, b_ in zip(tally, (p["hits"], p["trials"], p["base_hits"],
                                             p["base_trials"]))]
gap_ = (tally[0] / float(tally[1]) - tally[2] / float(tally[3])) * 100
check("London's range and its mirror are crossed as often as each other (%.1f points apart "
      "over %d days)" % (gap_, tally[1]), tally[1] > 1200 and abs(gap_) < 2.5
      and abs(tally[1] - tally[3]) < 0.04 * tally[1], tally)
rec = claims.ndog_fills([nothing(61, "ES=F", jump=2.0), nothing(62, "NQ=F", jump=2.0)])
p = rec["pooled"]
check("small opening gaps are nearly all filled, and nothing is made of it",
      p["trials"] > 80 and p["rate"] > 0.8 and rec["verdict"] == 0, (p, rec["verdict"]))
rec = claims.nwog_fills([nothing(s_, sym, days=500, jump=2.0, step=3600)
                         for s_, sym in ((63, "ES=F"), (64, "NQ=F"))])
p = rec["pooled"]
check("nor of the weekend's", p["trials"] > 150 and p["rate"] > 0.6 and rec["verdict"] == 0,
      (p, rec["verdict"]))

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
conn2 = sqlite3.connect(":memory:")


def fetch_half(symbol, interval):
    if symbol == "NQ=F":
        raise RuntimeError("no hourly prices")
    return fetch(symbol, interval)


out = claims.run(conn2, {("ES=F", "5m"): five, ("NQ=F", "5m"): five}, fetch=fetch_half,
                 pause=0)
check("when one instrument's hourly prices fail, its lean is not written and the pooled "
      "one is left as it was", labstore.bias(conn2, "ES=F") is not None
      and labstore.bias(conn2, labstore.POOL_ALL) is None
      and labstore.claim(conn2, "bias_works") is None
      and any("NQ=F" in e for e in out["errors"]), out)
def weekend(symbol, interval, missing=""):
    if symbol == missing:
        raise RuntimeError("no hourly prices")
    return futures_days(week_paths, step=3600, symbol=symbol)


pair = {("ES=F", "5m"): five, ("NQ=F", "5m"): futures_days([LONDON] * DAYS, symbol="NQ=F")}
claims.run(conn, pair, fetch=weekend, pause=0)
weekly = labstore.claim(conn, "nwog_fills")
check("the weekend gaps are measured when every future's hourly prices came",
      weekly is not None and sorted(weekly["by_symbol"]) == ["ES=F", "NQ=F"], weekly)
out = claims.run(conn, pair, fetch=lambda s_, iv: weekend(s_, iv, "NQ=F"), pause=0)
check("and left as they were when one future's did not", any("nwog_fills" in e for e in out["errors"])
      and "nwog_fills" not in out["claims"]
      and labstore.claim(conn, "nwog_fills")["pooled"] == weekly["pooled"], out)
out = claims.run(conn, {("ES=F", "5m"): five, ("SPY", "5m"): stock},
                 fetch=lambda s_, iv: weekend(s_, iv, "SPY"), pause=0)
check("a fund's missing hourly prices leave the futures' weekend gaps alone",
      "nwog_fills" in out["claims"], out)
whole = structure.series(walk)
kept = {key: whole[key] for key in CLAIM_SERIES}
real_series = structure.series
worked = []
structure.series = lambda bars_, *a, **k: worked.append(bars_) or real_series(bars_, *a, **k)
try:
    conn3 = sqlite3.connect(":memory:")
    claims.run(conn3, {("ES=F", "5m"): walk}, fetch=fetch, pause=0, series={("ES=F", "5m"): kept})
finally:
    structure.series = real_series
check("the structure the lab worked out is used, not worked out again, with the same result",
      not any(b_ is walk for b_ in worked) and labstore.claim(conn3, "close_beats_wick")["pooled"]
      == claims.close_beats_wick([walk])["pooled"], len(worked))
words = " ".join(" ".join(r["notes"]) for r in stored.values()).lower()
check("the notes use no jargon",
      not any(j in words for j in ("expectancy", "r-multiple", "confidence interval", " atr",
                                   "wilson", "basis point", "sharpe", "drawdown")), words[:200])

print()
print("%d failure(s)" % len(fails))
print("CLAIMS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
