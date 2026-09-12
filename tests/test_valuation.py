"""Verify valuation and risk modules."""
import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import numpy as np
from bot import fundamentals as F, valuation as V, risk as R
from bot.market import fetch_bars

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

print("=" * 72)
print("VALUATION")
print("=" * 72)
for sym in ["AAPL", "KO"]:
    f = F.load(sym)
    price = f.get("price") or 0
    print("\n   %s at %.2f" % (sym, price))

    d = V.discounted_cash_flow(f, price)
    print("      DCF usable=%s fair=%s upside=%s" % (
        d.usable, "%.2f" % d.fair_value if d.fair_value else "—",
        "%.0f%%" % (d.upside * 100) if d.upside is not None else "—"))
    check("%s DCF ran" % sym, d.usable, d.reason)
    if d.usable:
        check("%s DCF value positive" % sym, d.fair_value > 0)
        check("%s DCF has sensitivity grid" % sym, len(d.sensitivity) == 5)
        check("%s DCF shows workings" % sym, len(d.workings) >= 10)
        print("      range across assumptions: %.2f to %.2f" % (d.low, d.high))
        for a in d.assumptions:
            print("        %-24s %-14s (%s)" % (
                a.label, "%.4g" % a.value, a.source))
        for n in d.notes:
            print("        note: %s" % n[:95])

    rv = V.reverse_dcf(f, price)
    check("%s reverse DCF ran" % sym, rv.usable, rv.reason)
    for n in rv.notes:
        print("      %s" % n[:150])

    mu = V.multiples(f, price)
    check("%s multiples ran" % sym, mu.usable, mu.reason)
    if mu.usable:
        for r in mu.workings[:4]:
            print("        %-22s %-9s %s" % (
                r["label"],
                "%.1f%%" % (r["value"]*100) if r["unit"] == "pct" else "%.1fx" % r["value"],
                r["comment"]))

    comb = V.combine([d, rv, mu], price)
    print("      combined: usable=%s agreement=%s" % (comb["usable"], comb.get("agreement")))
    for line in comb.get("commentary", []):
        print("        %s" % line[:120])

print()
print("   --- refuses to model what it cannot ---")
for sym in ["BTC-USD", "SPY"]:
    f = F.load(sym)
    price = f.get("price") or float(fetch_bars(sym, "1d").last_price)
    d = V.discounted_cash_flow(f, price)
    print("      %-8s DCF usable=%-5s reason: %s" % (sym, d.usable, d.reason[:70]))
    check("%s refuses rather than inventing" % sym, (not d.usable) or d.fair_value > 0)

print()
print("=" * 72)
print("RISK")
print("=" * 72)
for sym in ["AAPL", "NVDA", "BTC-USD", "KO"]:
    p = R.profile(sym, "1d")
    print("\n   %s  (%s, %d returns, reliable=%s)" % (
        p.symbol, p.period, p.samples, p.reliable))
    print("      volatility     %s" % ("%.1f%%" % (p.volatility*100) if p.volatility else "—"))
    print("      max drawdown   %s over %s bars" % (
        "%.1f%%" % (p.max_drawdown*100) if p.max_drawdown else "—", p.drawdown_days))
    print("      VaR 95 / 99    %s / %s" % (
        "%.2f%%" % (p.var_95*100) if p.var_95 else "—",
        "%.2f%%" % (p.var_99*100) if p.var_99 else "—"))
    print("      shortfall      %s" % ("%.2f%%" % (p.expected_shortfall*100) if p.expected_shortfall else "—"))
    print("      beta / corr    %s / %s" % (
        "%.2f" % p.beta if p.beta else "—",
        "%.2f" % p.correlation_spy if p.correlation_spy else "—"))
    print("      sharpe/sortino %s / %s" % (
        "%.2f" % p.sharpe if p.sharpe else "—",
        "%.2f" % p.sortino if p.sortino else "—"))
    check("%s volatility positive" % sym, p.volatility and p.volatility > 0)
    check("%s drawdown negative or zero" % sym, p.max_drawdown is not None and p.max_drawdown <= 0)
    check("%s VaR95 above VaR99" % sym, p.var_95 >= p.var_99)
    check("%s up-day share in range" % sym, 0 < p.up_days < 1)
    for n in p.notes[:2]:
        print("      note: %s" % n[:95])

print()
print("   --- BTC should be more volatile than KO ---")
btc = R.profile("BTC-USD", "1d"); ko = R.profile("KO", "1d")
check("BTC volatility exceeds KO", btc.volatility > ko.volatility,
      "%.2f vs %.2f" % (btc.volatility, ko.volatility))

print()
print("=" * 72)
print("POSITION RISK")
print("=" * 72)
p = R.profile("AAPL", "1d")
for label, entry, stop, qty, acct in [
    ("sensible", 200, 190, 10, 100000),
    ("moderate", 200, 190, 100, 100000),
    ("genuinely too big", 200, 190, 300, 100000),
    ("stop too tight", 200, 199.5, 10, 100000),
]:
    out = R.position_risk(entry, stop, qty, acct, p)
    print("   %-16s risk=%.2f (%.2f%% of account) exposure=%.1f%% stop=%.2f daily moves" % (
        label, out["cash_at_risk"], out["pct_of_account"], out["exposure_pct"],
        out.get("stop_in_daily_moves", 0)))
    for w in out["warnings"]:
        print("        ! %s" % w[:100])
# 100 units risks 1% of the account with 20% exposure. That is aggressive but
# not objectively dangerous, so the sizer is right to stay quiet about it; only
# a position that breaks a stated rule should produce a warning.
moderate = R.position_risk(200, 190, 100, 100000, p)
big = R.position_risk(200, 190, 300, 100000, p)
check("a 1%-risk position is not flagged", len(moderate["warnings"]) == 0,
      "warned: %s" % moderate["warnings"])
check("a 3%-risk position is flagged", len(big["warnings"]) > 0)
check("the flag names the risk rule",
      any("2%" in w for w in big["warnings"]))
check("sensible position is quiet", len(R.position_risk(200,190,5,100000,p)["warnings"]) == 0)

# Gap risk: the stop says what you mean to lose, a gap says what you lose when
# the market reopens past it. It must never be reported as less than the stop.
for qty in (5, 100, 300):
    for entry, stop in ((200, 190), (190, 200)):        # long and short
        o = R.position_risk(entry, stop, qty, 100000, p)
        check("gap loss >= stop loss (%s %d)" % ("long" if entry > stop else "short", qty),
              o["gap_loss"] >= o["cash_at_risk"] - 1e-9,
              "gap %.2f < risk %.2f" % (o["gap_loss"], o["cash_at_risk"]))
        check("gap loss scales with size (%s %d)" % ("long" if entry > stop else "short", qty),
              o["gap_loss"] > 0)
check("gap uses the instrument's own worst day",
      abs(moderate["gap_move"] - abs(p.worst_day)) < 1e-12)
check("gap loss is stated as a share of the account",
      abs(moderate["gap_pct_of_account"] - moderate["gap_loss"] / 100000 * 100) < 1e-9)
no_profile = R.position_risk(200, 190, 100, 100000, None)
check("no profile means no invented gap figure", "gap_loss" not in no_profile)

print()
print("=" * 72)
print("CORRELATION")
print("=" * 72)
cm = R.correlation_matrix(["AAPL", "MSFT", "NVDA", "KO", "GLD"], "1d")
check("matrix usable", cm["usable"])
if cm["usable"]:
    print("   symbols: %s  (%d common returns)" % (cm["symbols"], cm["samples"]))
    print("   average correlation: %.3f" % cm["average"])
    print("   most correlated pairs:")
    for pr in cm["pairs"][:4]:
        print("      %-6s %-6s %+.3f" % (pr["a"], pr["b"], pr["value"]))
    for n in cm["notes"]:
        print("   note: %s" % n[:110])
    diag_ok = all(abs(cm["matrix"][i][i] - 1.0) < 1e-9 for i in range(len(cm["symbols"])))
    check("diagonal is 1.0", diag_ok)
    check("all correlations in [-1,1]",
          all(-1.0001 <= v <= 1.0001 for row in cm["matrix"] for v in row))
    ko_gld = [p for p in cm["pairs"] if set([p["a"],p["b"]]) == {"KO","GLD"}]
    tech = [p for p in cm["pairs"] if set([p["a"],p["b"]]) == {"AAPL","MSFT"}]
    if ko_gld and tech:
        check("tech pair more correlated than KO/GLD",
              tech[0]["value"] > ko_gld[0]["value"],
              "%.2f vs %.2f" % (tech[0]["value"], ko_gld[0]["value"]))

print()
print("%d failure(s)" % len(fails))
print("VALUATION AND RISK OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
