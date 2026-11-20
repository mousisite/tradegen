"""Price structure as the course draws it, checked bar by bar.

The swings, the trend and its breaks, sweeps, equal highs, gaps, inverse gaps,
the 50% line and the course's whole sweep-break-retest chain, each on a scene
built so the answer is known, and asserted at the exact bar. Then the rule
every back-test depends on: no value at bar i may use a bar after i, for every
key of liquidity.series, on three made-up series (daily, a stock's 5-minute
day session, a future's round-the-clock 5-minute bars with pauses, weekends,
a clock change and a contract roll). No network.
"""
import math
import os as _os
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402,F401

harness.isolate("structure")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import clock, levels, liquidity, structure  # noqa: E402

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


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
head("SWING POINTS ARE KNOWN k BARS AFTER THEY FORM")
# ---------------------------------------------------------------------------
b = sc.swing_scene()
s = structure.series(b)
check("the swing high is not known on the bar after it", math.isnan(s["st_hi"][3]))
check("it is known exactly k = 2 bars later, at its own price and bar",
      s["st_hi"][4] == 10.9 and s["st_hi_at"][4] == 2, (s["st_hi"][4], s["st_hi_at"][4]))
rng = np.random.RandomState(3)
hi = 100 + np.cumsum(rng.normal(0, 1, 800))
lo = hi - np.abs(rng.normal(0, 1, 800))
hi = np.round(hi, 1)        # ties between neighbours happen
lo = np.round(lo, 1)
check("pivots follow levels.swing_pivots' rule, ties and all",
      structure.pivots(hi, lo, 2) == tuple(levels.swing_pivots(hi, lo, 2)))
check("of two equal highs side by side, the first is the swing",
      structure.pivots(np.array([9.0, 9.5, 10.5, 10.5, 10.0, 9.0, 8.0]),
                       np.array([8.0] * 7), 2)[0] == [2])

# ---------------------------------------------------------------------------
head("THE TREND CHANGES ONLY ON A CLOSE PAST THE LAST SWING")
# ---------------------------------------------------------------------------
b = sc.trend_scene()
s = structure.series(b)
check("a close above the first swing high sets the trend up, and is not a flip",
      s["st_trend"][10] == 0 and s["st_trend"][11] == 1 and s["st_flip"][11] == 0
      and s["st_trend_at"][11] == 11, (s["st_trend"][10:12], s["st_flip"][11]))
check("the reference low is then the latest swing low, 100.4", s["st_ref_lo"][11] == 100.4,
      s["st_ref_lo"][11])
check("a newer swing low (101.5) becomes the reference when it is confirmed",
      s["st_ref_lo"][17] == 100.4 and s["st_ref_lo"][18] == 101.5,
      (s["st_ref_lo"][17], s["st_ref_lo"][18]))
check("a wick below it that closes above is a sweep that came back",
      s["st_sweep"][19] == 1 and s["st_sweep_back"][19] == 1 and s["st_sweep_lvl"][19] == 101.5
      and s["st_sweep_n"][19] == 1, (s["st_sweep"][19], s["st_sweep_back"][19],
                                     s["st_sweep_lvl"][19]))
check("and is not a flip", s["st_flip"][19] == 0 and s["st_trend"][19] == 1)
check("a close exactly on the swing low is not a flip",
      b.close[20] == 101.5 and s["st_flip"][20] == 0 and s["st_trend"][20] == 1)
check("a close below it flips the trend down", s["st_flip"][21] == -1 and s["st_trend"][21] == -1
      and s["st_trend_at"][21] == 21)
check("and the next flip is measured from the latest swing high, 103.0, not 104.0",
      s["st_ref_hi"][21] == 103.0, s["st_ref_hi"][21])
check("a wick below 100.4 that closes back is another sweep that came back",
      s["st_sweep"][23] == 1 and s["st_sweep_back"][23] == 1 and s["st_sweep_lvl"][23] == 100.4)
check("a close of 103.1 above 103.0 flips it up again",
      s["st_flip"][24] == 1 and s["st_trend"][24] == 1, (s["st_flip"][24], s["st_trend"][24]))
check("the same bar took the swing high it closed above (a sweep that did not come back)",
      s["st_sweep"][24] == -1 and s["st_sweep_back"][24] == 0 and s["st_sweep_lvl"][24] == 103.0)
check("the reference low after the flip is the latest swing low, 101.5",
      s["st_ref_lo"][24] == 101.5, s["st_ref_lo"][24])
check("a lower swing low confirmed in the up trend replaces it (100.2)",
      s["st_ref_lo"][25] == 100.2 and s["st_lo_at"][25] == 23, (s["st_ref_lo"][25], s["st_lo_at"][25]))
check("only one side of the reference is shown at a time",
      math.isnan(s["st_ref_hi"][25]) and math.isnan(s["st_ref_lo"][21]))
check("no flip anywhere else in the scene",
      sorted(np.flatnonzero(s["st_flip"]).tolist()) == [21, 24],
      np.flatnonzero(s["st_flip"]).tolist())

# ---------------------------------------------------------------------------
head("GAPS: THREE CANDLES, A MOVING MIDDLE ONE, CLOSED THROUGH OR USED UP")
# ---------------------------------------------------------------------------
s = structure.series(sc.gap_scene())
check("a gap up forms on the third candle, from the first's high to the third's low",
      s["st_fvg_new"][2] == 1 and s["st_fvg_up_top"][2] == 10.4 and s["st_fvg_up_bot"][2] == 10.2
      and s["st_fvg_up_at"][2] == 2, (s["st_fvg_new"][2], s["st_fvg_up_top"][2]))
check("a wick through it that closes inside is its first touch, and it stays",
      s["st_fvg_touch"][3] == 1 and s["st_fvg_up_top"][3] == 10.4)
check("a close below its bottom ends it", math.isnan(s["st_fvg_up_top"][4])
      and s["st_fvg_touch"][4] == 0)
s = structure.series(sc.gap_scene(middle="down"))
check("with a down candle in the middle there is no gap",
      s["st_fvg_new"][2] == 0 and math.isnan(s["st_fvg_up_top"][2]))
s = structure.series(sc.gap_scene(touching=True))
check("wicks that only touch make no gap", s["st_fvg_new"][2] == 0)
s = structure.series(sc.gap_used_scene())
check("a close above a swing high confirmed after the gap ends it (used up)",
      s["st_fvg_up_top"][6] == 10.4 and math.isnan(s["st_fvg_up_top"][7]),
      (s["st_fvg_up_top"][6], s["st_fvg_up_top"][7]))

s = structure.series(sc.stack_scene())
check("two gaps with no down candle between their middles are one stack",
      s["st_fvg_up_top"][8] == 10.9 and s["st_fvg_up_bot"][8] == 10.6
      and s["st_fvg_up_stack_bot"][8] == 10.2, (s["st_fvg_up_top"][8], s["st_fvg_up_stack_bot"][8]))
check("closing below the upper gap only is not an inverse gap",
      s["st_ifvg"][10] == 0 and s["st_fvg_up_top"][10] == 10.4)
check("closing below the bottom of the stack is one",
      s["st_ifvg"][11] == -1 and s["st_ifvg_lvl"][11] == 10.2)
check("and the trend stays up: only a flip changes it",
      s["st_trend"][11] == 1 and s["st_flip"][11] == 0)
check("no other inverse gap in the scene", np.flatnonzero(s["st_ifvg"]).tolist() == [11])

# ---------------------------------------------------------------------------
head("THE 50% LINE")
# ---------------------------------------------------------------------------
s = structure.series(sc.half_scene())
check("the leg runs from the swing low 9.6", s["st_leg_lo"][7] == 9.6 and s["st_leg_hi"][7] == 11.0)
check("a down candle that makes the high does not fix the line",
      s["st_leg_hi"][8] == 11.4 and math.isnan(s["st_eq"][8]))
check("the first down candle after the high fixes it at the halfway point",
      near(s["st_eq"][9], 10.5), s["st_eq"][9])
check("the first bar back to it is a touch", s["st_eq_touch"][10] == 1)
check("the next one on the same leg is not", s["st_eq_touch"][11] == 0)
check("a newer swing low becomes the leg's low, and the line waits for a new lock",
      s["st_leg_lo"][13] == 10.3 and s["st_leg_hi"][13] == 11.3 and math.isnan(s["st_eq"][13]),
      (s["st_leg_lo"][13], s["st_leg_hi"][13], s["st_eq"][13]))
check("which the next down candle gives", near(s["st_eq"][14], 10.8), s["st_eq"][14])

# ---------------------------------------------------------------------------
head("EQUAL HIGHS, AND A LEVEL TAKEN ONLY ONCE")
# ---------------------------------------------------------------------------
b = sc.equal_scene(0.05)
s = structure.series(b)
check("ATR(14) is exactly 1.0 in the scene", near(s["st_atr"][27], 1.0), s["st_atr"][27])
check("two highs 0.05 ATR apart are equal highs, priced at the higher",
      math.isnan(s["st_eqh"][26]) and s["st_eqh"][27] == 101.0, (s["st_eqh"][26], s["st_eqh"][27]))
check("the bar that trades past both takes both at once, as equal highs",
      s["st_sweep"][28] == -1 and s["st_sweep_n"][28] == 2 and s["st_sweep_eq"][28] == 1
      and near(s["st_sweep_lvl"][28], 100.95) and s["st_sweep_far"][28] == 101.0,
      (s["st_sweep"][28], s["st_sweep_n"][28], s["st_sweep_lvl"][28]))
check("and closes back below them", s["st_sweep_back"][28] == 1)
check("a level taken once cannot be taken again", s["st_sweep"][29] == 0
      and b.high[29] > 101.0)
took = [e for e in liquidity.events(b, liquidity.series(b), limit=50) if e["kind"] == "take"
        and e["i"] == 28]
check("said as equal highs taken, the nearest named, closing back below them",
      took and took[0]["words"].startswith("Took equal highs (the nearest at 100.95)")
      and "closed back below them at 100" in took[0]["words"], took and took[0]["words"])
check("and no equal highs are left", math.isnan(s["st_eqh"][28]))
s = structure.series(sc.equal_scene(0.2))
check("two highs 0.2 ATR apart are not equal highs", math.isnan(s["st_eqh"][27]))
check("their sweep is not marked equal", s["st_sweep"][28] == -1 and s["st_sweep_eq"][28] == 0)
check("the nearest untaken highs above the close are listed nearest first",
      near(s["st_above1"][27], 100.8) and s["st_above2"][27] == 101.0
      and math.isnan(s["st_above3"][27]), (s["st_above1"][27], s["st_above2"][27]))

# ---------------------------------------------------------------------------
head("THE COURSE'S FULL CHAIN: TAKEN, BROKEN, RETESTED")
# ---------------------------------------------------------------------------
b = sc.chain_scene()
s = liquidity.series(b)
check("bar 24 takes the swing low 100.2 and closes back above it",
      s["lq_take"][24] == 1 and s["lq_take_lvl"][24] == 100.2 and s["lq_take_back"][24] == 1
      and liquidity.SOURCES[s["lq_take_src"][24]] == "swing")
check("bar 26 breaks the last swing high 101.4", s["st_flip"][26] == 1 and s["st_ref_lo"][26] == 99.8)
check("a sweep, then a break: lq_asf at bar 26 with the take's low as its stop",
      s["lq_asf"][26] == 1 and s["lq_asf_stop"][26] == 99.8 and s["lq_asf_sweep_at"][26] == 24)
check("the confirmation is recorded", s["lq_chain_conf"][26] == 1)
check("the chain fires once, at bar 30, through the gap",
      np.flatnonzero(s["lq_chain"]).tolist() == [30] and s["lq_chain"][30] == 1
      and s["lq_chain_via"][30] == 1 and liquidity.VIA[s["lq_chain_via"][30]] == "gap",
      np.flatnonzero(s["lq_chain"]).tolist())
check("with its stop at the take's low and the steps' bars",
      s["lq_chain_stop"][30] == 99.8 and s["lq_chain_sweep_at"][30] == 24
      and s["lq_chain_conf_at"][30] == 26)
check("not on the down candle in the gap before it", s["lq_chain"][29] == 0
      and s["st_fvg_touch"][29] == 1)
s = liquidity.series(sc.chain_scene(broken=True))
check("when the take's low is broken before the pullback it never fires",
      not np.any(s["lq_chain"][24:] == 1), np.flatnonzero(s["lq_chain"]).tolist())

ev = liquidity.events(b, liquidity.series(b), limit=50)
order = [(e["kind"], e["i"]) for e in ev]
check("events list the chain, the break and the take, newest first",
      order.index(("chain", 30)) < order.index(("sweep_flip", 26)) < order.index(("take", 24))
      and all(ev[k]["at"] >= ev[k + 1]["at"] for k in range(len(ev) - 1)), order)
check("bar 31's run past the swing high 102.3 is the newest event",
      order[0] == ("take", 31) and ev[0]["dir"] == -1 and ev[0]["price"] == 102.3, order[:2])
chain = next(e for e in ev if e["kind"] == "chain")
check("in words, with prices and New York time",
      "99.8" in chain["words"] and "New York time" in chain["words"]
      and "an earlier swing low" in chain["words"], chain["words"])
said = " ".join(e["words"] for e in ev).lower()
check("and no None, nan or forecast in them",
      not any(w in said for w in ("none", "nan", " will ", "guarantee", "smart money")))
check("events since a time keep only bars after it",
      all(e["at"] > int(b.timestamp[27]) for e in
          liquidity.events(b, liquidity.series(b), since=int(b.timestamp[27]))))

# ---------------------------------------------------------------------------
head("EVERY KEY, WITH ITS TYPE")
# ---------------------------------------------------------------------------
DOCUMENTED = {
    "int8": ("st_trend", "st_flip", "st_ext", "st_sweep", "st_sweep_both", "st_sweep_back",
             "st_sweep_eq", "st_fvg_new", "st_fvg_touch", "st_ifvg", "st_eq_touch",
             "ck_sess", "ck_pd_take", "ck_pd_both", "ck_pd_back", "ck_sess_take",
             "ck_sess_back", "ck_gap_filled", "ck_roll", "lq_take", "lq_take_back",
             "lq_take_src", "lq_asf", "lq_chain", "lq_chain_via", "lq_chain_conf"),
    "int16": ("st_sweep_n",),
    "int64": ("st_hi_at", "st_lo_at", "st_fvg_up_at", "st_fvg_dn_at", "ck_day", "ck_min",
              "lq_chain_sweep_at"),
    "float64": ("st_hi", "st_lo", "st_above1", "st_above2", "st_above3", "st_below1",
                "st_below2", "st_below3", "st_ref_hi", "st_ref_lo", "st_sweep_lvl",
                "st_sweep_far", "st_eqh", "st_eql", "st_fvg_up_top", "st_fvg_up_bot",
                "st_fvg_up_stack_bot", "st_fvg_dn_top", "st_fvg_dn_bot",
                "st_fvg_dn_stack_top", "st_eq", "st_leg_lo", "st_leg_hi", "ck_pdh", "ck_pdl",
                "ck_pdc", "ck_pwh", "ck_pwl", "ck_open", "ck_asia_hi", "ck_asia_lo",
                "ck_lon_hi", "ck_lon_lo", "ck_pre_hi", "ck_pre_lo", "ck_sess_lvl",
                "ck_gap_lo", "ck_gap_hi", "ck_wgap_lo", "ck_wgap_hi", "lq_take_lvl",
                "lq_asf_stop", "lq_chain_stop"),
}
full = liquidity.series(sc.stock_series(600))
wrong = [(k, str(full[k].dtype) if k in full else "missing") for dtype, keys in DOCUMENTED.items()
         for k in keys if k not in full or str(full[k].dtype) != dtype]
check("every documented key of structure, clock and liquidity is there with its type",
      not wrong, wrong)
check("every array is as long as the bars", all(len(v) == 600 for v in full.values()))
tiny = liquidity.series(sc.stock_series(3))
check("three bars are too few for anything, and nothing breaks",
      set(tiny) == set(full) and not np.any(tiny["st_trend"]))

# ---------------------------------------------------------------------------
head("NO PEEKING: THE VALUE AT BAR i IS THE SAME WHEN THE BARS STOP AT i")
# ---------------------------------------------------------------------------


def same(a, b):
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        return (math.isnan(a) and math.isnan(b)) or a == b
    return a == b


series_cases = (("daily", sc.daily_series(), 40), ("a stock's 5-minute day", sc.stock_series(), 40),
                ("a future's 5-minute round the clock", sc.futures_series(), 20))
for label, bars, count in series_cases:
    whole = liquidity.series(bars)
    rng = np.random.RandomState(7)
    n = len(bars.close)
    picks = sorted(rng.choice(np.arange(5, n), size=count, replace=False).tolist())
    # The scenes most likely to peek: a day's first and last bar, a roll.
    if label.startswith("a future"):
        days = clock.day_table(bars)
        rolled = next(d for d in days if d.roll)
        picks = sorted(set(picks + [rolled.roll_at - 1, rolled.roll_at, rolled.first,
                                    days[5].last, days[5].last + 1, days[12].first + 108]))
    bad = []
    for i in picks:
        cut = liquidity.series(clock.head(bars, i + 1))
        for key, arr in whole.items():
            if not same(arr[i], cut[key][i]):
                bad.append((key, i, arr[i], cut[key][i]))
    check("%s: %d bars, %d keys at %d bars each" % (label, n, len(whole), len(picks)),
          not bad, bad[:6])
fut = sc.futures_series()
fs = liquidity.series(fut)
check("the made-up future has every kind of event to check",
      all(np.any(fs[k] != 0) for k in ("lq_chain", "lq_asf", "ck_sess_take", "ck_roll",
                                       "st_ifvg", "st_eq_touch", "st_fvg_touch")),
      [k for k in ("lq_chain", "lq_asf", "ck_sess_take", "ck_roll", "st_ifvg") if not np.any(fs[k])])

# ---------------------------------------------------------------------------
head("SPEED")
# ---------------------------------------------------------------------------
fut = sc.futures_series(17000)
t = time.perf_counter()
structure.series(fut)
took_s = time.perf_counter() - t
t = time.perf_counter()
liquidity.series(fut)
took_l = time.perf_counter() - t
print("   structure.series on 17,000 bars: %.2fs; liquidity.series: %.2fs" % (took_s, took_l))
check("structure alone under 0.8 s", took_s < 0.8, took_s)
check("the whole liquidity model under 1.5 s", took_l < 1.5, took_l)


print()
print("%d failure(s)" % len(fails))
print("STRUCTURE OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
