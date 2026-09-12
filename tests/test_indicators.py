"""Correctness checks for the indicator library."""
import sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import numpy as np
from bot import indicators as ind
from bot.market import fetch_bars

fails = []
def check(name, cond, detail=""):
    if cond:
        print("  OK   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        fails.append(name)

print("--- synthetic sanity ---")
# A strictly rising series: RSI must be 100, Williams %R must be 0, ADX high.
rise = np.arange(1, 121, dtype=float)
r = ind.rsi(rise)
check("RSI of monotonic rise == 100", abs(r[-1] - 100.0) < 1e-6, "got %.4f" % r[-1])
fall = rise[::-1].copy()
check("RSI of monotonic fall == 0", abs(ind.rsi(fall)[-1]) < 1e-6, "got %.4f" % ind.rsi(fall)[-1])

# Rolling helpers against numpy ground truth.
x = np.random.RandomState(0).randn(200).cumsum() + 100
for n in (5, 20, 50):
    rm, rn, rs = ind.rolling_max(x, n), ind.rolling_min(x, n), ind.rolling_std(x, n)
    ok = all(abs(rm[i] - x[i-n+1:i+1].max()) < 1e-9 and
             abs(rn[i] - x[i-n+1:i+1].min()) < 1e-9 and
             abs(rs[i] - x[i-n+1:i+1].std()) < 1e-9 for i in range(n-1, len(x)))
    check("rolling max/min/std n=%d match numpy" % n, ok)

# SMA against numpy
s = ind.sma(x, 20)
check("SMA matches numpy", all(abs(s[i] - x[i-19:i+1].mean()) < 1e-9 for i in range(19, len(x))))

# linreg on a perfect line: r2 == 1, slope sign correct
line = np.linspace(100, 200, 100)
sl, r2 = ind.linreg(line, 20)
check("linreg r2==1 on a straight line", abs(r2[-1] - 1.0) < 1e-9, "got %.9f" % r2[-1])
check("linreg slope positive on a rising line", sl[-1] > 0, "got %.4f" % sl[-1])

# ichimoku shift: senkou_a[i] must equal the raw value from `shift` bars ago
h = x + 1; l = x - 1
tenkan, kijun, sa, sb = ind.ichimoku(h, l)
raw_a = (ind.rolling_max(h, 9) + ind.rolling_min(l, 9)) / 2.0
raw_a = (raw_a + (ind.rolling_max(h, 26) + ind.rolling_min(l, 26)) / 2.0) / 2.0
check("ichimoku senkou_a is shifted forward 26",
      abs(sa[120] - raw_a[120 - 26]) < 1e-9, "got %.6f vs %.6f" % (sa[120], raw_a[94]))

print("\n--- real data, all instruments ---")
for sym in ["AAPL", "BTC-USD"]:
    b = fetch_bars(sym, "5m")
    o, hi, lo, c, v = b.open, b.high, b.low, b.close, b.volume
    n = len(b)
    t0 = time.time()
    series = {
        "sma20": ind.sma(c, 20), "ema21": ind.ema(c, 21),
        "rsi": ind.rsi(c), "atr": ind.atr(hi, lo, c),
        "cci": ind.cci(hi, lo, c), "roc": ind.roc(c), "cmo": ind.cmo(c),
        "tsi": ind.tsi(c), "zscore": ind.zscore(c),
        "willr": ind.williams_r(hi, lo, c),
        "hv": ind.historical_volatility(c),
        "obv": ind.obv(c, v), "mfi": ind.money_flow_index(hi, lo, c, v),
        "ad": ind.accum_dist(hi, lo, c, v), "rvol": ind.relative_volume(v),
    }
    k, d = ind.stochastic(hi, lo, c); series["stoch_k"] = k; series["stoch_d"] = d
    a, pdi, mdi = ind.adx(hi, lo, c); series["adx"] = a
    au, ad_ = ind.aroon(hi, lo); series["aroon_up"] = au; series["aroon_dn"] = ad_
    bu, bm, bl, pb, bw = ind.bollinger(c); series["pctb"] = pb; series["bw"] = bw
    ku, km, kl = ind.keltner(hi, lo, c); series["kelt_u"] = ku
    du, dm, dl = ind.donchian(hi, lo); series["donch_u"] = du
    st, stdir = ind.supertrend(hi, lo, c); series["supertrend"] = st
    sar, sardir = ind.parabolic_sar(hi, lo); series["sar"] = sar
    tk, kj, sa2, sb2 = ind.ichimoku(hi, lo); series["kijun"] = kj; series["senkou_a"] = sa2
    slp, r2v = ind.linreg(c); series["linreg_slope"] = slp
    vw, vu, vl = ind.vwap_bands(hi, lo, c, v, b.session_id); series["vwap_up"] = vu
    elapsed = time.time() - t0

    bad_len = [k2 for k2, arr in series.items() if len(arr) != n]
    bad_tail = [k2 for k2, arr in series.items() if not np.isfinite(arr[-1])]
    print("  %s (%d bars, %.2fs): %d series" % (sym, n, elapsed, len(series)))
    check("   %s all lengths aligned" % sym, not bad_len, str(bad_len))
    check("   %s no NaN at final bar" % sym, not bad_tail, str(bad_tail))

    # Bounded oscillators must stay in range.
    bounds = {"rsi": (0, 100), "stoch_k": (0, 100), "stoch_d": (0, 100),
              "mfi": (0, 100), "willr": (-100, 0), "aroon_up": (0, 100),
              "aroon_dn": (0, 100), "cmo": (-100, 100)}
    for key, (lo_b, hi_b) in bounds.items():
        arr = series[key]
        fin = arr[np.isfinite(arr)]
        check("   %s %s within [%g,%g]" % (sym, key, lo_b, hi_b),
              len(fin) > 0 and fin.min() >= lo_b - 1e-6 and fin.max() <= hi_b + 1e-6,
              "range [%.3f,%.3f]" % (fin.min(), fin.max()) if len(fin) else "empty")

    check("   %s supertrend direction is +-1" % sym,
          set(np.unique(stdir[stdir != 0]).tolist()) <= {-1.0, 1.0})
    check("   %s SAR direction is +-1" % sym,
          set(np.unique(sardir[sardir != 0]).tolist()) <= {-1.0, 1.0})
    check("   %s keltner upper above lower" % sym, bool(np.all(ku[np.isfinite(ku)] >= kl[np.isfinite(ku)])))
    check("   %s donchian upper above lower" % sym, bool(np.all(du[np.isfinite(du)] >= dl[np.isfinite(du)])))
    check("   %s vwap bands bracket vwap" % sym,
          bool(np.all(vu[np.isfinite(vu)] >= vw[np.isfinite(vu)])))

print("\n%d failures" % len(fails))
print("INDICATORS OK" if not fails else "INDICATORS HAVE FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
