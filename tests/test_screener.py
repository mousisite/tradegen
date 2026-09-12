"""Verify screener and portfolio modules."""
import sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness
from bot import screener as S, portfolio as P, database as db

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

print("=" * 72)
print("SCREENER: universes")
print("=" * 72)
syms = S.universe_symbols("most_actives", 20)
print("   most_actives -> %d symbols: %s" % (len(syms), syms[:8]))
check("universe returns symbols", len(syms) >= 5)

print()
print("=" * 72)
print("SCREENER: explicit list with shallow filters")
print("=" * 72)
t0 = time.time()
r = S.run(symbols=["AAPL","MSFT","NVDA","KO","T","F","PFE","XOM","JNJ","WMT"],
          filters={"market_cap_min": 5e10, "pe_max": 30}, limit=20)
print("   scanned %d, passed %d, rejected %d in %.1fs" % (
    r.scanned, len(r.passed), r.rejected, time.time()-t0))
for f in r.filters:
    print("      filter: %-32s %s" % (f["label"], f["value"]))
for c in r.passed:
    print("      %-6s %-28s cap=%s pe=%s" % (
        c.symbol, c.name[:28],
        "%.0fB" % (c.market_cap/1e9) if c.market_cap else "—",
        "%.1f" % c.pe if c.pe else "—"))
check("screen returned candidates", len(r.passed) > 0)
check("every survivor meets the cap filter",
      all(c.market_cap and c.market_cap >= 5e10 for c in r.passed))
check("every survivor meets the PE filter",
      all(c.pe and c.pe <= 30 for c in r.passed))

print()
print("   --- impossible filter should return nothing, not crash ---")
r2 = S.run(symbols=["AAPL","MSFT"], filters={"pe_max": 0.1}, limit=10)
check("impossible filter yields none", len(r2.passed) == 0)
print("      note: %s" % (r2.notes[0][:90] if r2.notes else "—"))

print()
print("   --- unknown data excluded by default ---")
r3 = S.run(symbols=["BTC-USD","AAPL"], filters={"pe_max": 100}, limit=10)
got = [c.symbol for c in r3.passed]
print("      passed: %s" % got)
check("crypto without a PE is excluded", "BTC-USD" not in got)

print()
print("=" * 72)
print("SCREENER: deep filters (fundamentals)")
print("=" * 72)
t0 = time.time()
r4 = S.run(symbols=["AAPL","MSFT","NVDA","KO","XOM","JNJ","WMT","PG"],
           filters={"roe_min": 0.15, "margin_min": 0.10}, limit=20)
print("   scanned %d, passed %d in %.1fs (deep=%s)" % (
    r4.scanned, len(r4.passed), time.time()-t0, r4.deep))
for c in r4.passed:
    print("      %-6s roe=%s margin=%s growth=%s quality=%s" % (
        c.symbol,
        "%.0f%%" % (c.roe*100) if c.roe else "—",
        "%.0f%%" % (c.net_margin*100) if c.net_margin else "—",
        "%.0f%%" % (c.revenue_growth*100) if c.revenue_growth is not None else "—",
        "%.2f" % c.quality if c.quality else "—"))
check("deep screen ran", r4.deep)
check("deep survivors meet ROE", all(c.roe and c.roe >= 0.15 for c in r4.passed))
check("deep survivors meet margin", all(c.net_margin and c.net_margin >= 0.10 for c in r4.passed))

print()
print("=" * 72)
print("SCREENER: presets are all well-formed")
print("=" * 72)
for key, preset in S.PRESETS.items():
    bad = [f for f in preset["filters"] if f not in S.FILTERS]
    check("preset %s uses real filters" % key, not bad, str(bad))
    check("preset %s has a rationale" % key, len(preset.get("why", "")) > 30)
print("   %d presets checked" % len(S.PRESETS))

print()
print("=" * 72)
print("PORTFOLIO")
print("=" * 72)
import os
# A private database, so this suite neither depends on nor destroys
# whatever is in the real one.
DB = harness.isolate("screener")[0]
conn = db.connect(DB)
db.open_trade(conn, "AAPL", "1d", 1, entry=300, stop=285, target1=340, quantity=30)
db.open_trade(conn, "MSFT", "1d", 1, entry=480, stop=455, target1=540, quantity=12)
db.open_trade(conn, "NVDA", "1d", 1, entry=210, stop=198, target1=240, quantity=40)
db.open_trade(conn, "KO",   "1d", 1, entry=85,  stop=80,  target1=95,  quantity=60)
rows = db.list_trades(conn, status="open")
conn.close()

t0 = time.time()
v = P.build(rows, account=100000)
print("   built in %.1fs" % (time.time()-t0))
print("   invested %s  cash %s  open P&L %s" % (
    "{:,.0f}".format(v.invested), "{:,.0f}".format(v.cash), "{:+,.0f}".format(v.open_pnl)))
print("   gross exposure %s  net %s  total risk %s" % (
    "{:,.0f}".format(v.gross_exposure), "{:,.0f}".format(v.net_exposure),
    "{:,.0f}".format(v.total_risk)))
print("   concentration %.3f (%.1f effective positions)" % (
    v.concentration or 0, 1/v.concentration if v.concentration else 0))
print("   portfolio beta %s" % ("%.2f" % v.portfolio_beta if v.portfolio_beta else "—"))
print("   by sector: %s" % {k: "%.0f%%" % (val/v.gross_exposure*100)
                            for k, val in v.by_sector.items()})
check("all positions priced", len(v.failed) == 0, str(v.failed))
check("invested is positive", v.invested > 0)
check("concentration between 0 and 1", 0 < (v.concentration or 0) <= 1)
check("cash plus invested is about the account",
      abs((v.cash + v.invested) - 100000) < 1 or v.invested > 100000)
check("correlation computed", bool(v.correlation.get("usable")))
if v.correlation.get("usable"):
    print("   average correlation %.3f" % v.correlation["average"])
print("   observations:")
for o in v.observations:
    print("      - %s" % o[:110])
print("   warnings:")
for w in v.warnings:
    print("      ! %s" % w[:110])

print()
print("   --- what if the market drops 10%% ---")
wi = P.what_if(v, -0.10)
print("      total change %s (%.1f%% of account)" % (
    "{:+,.0f}".format(wi["total_change"]), wi["pct_of_account"]))
for row in wi["rows"][:3]:
    print("      %-6s beta %.2f -> %s" % (row["symbol"], row["beta"],
                                          "{:+,.0f}".format(row["change"])))
check("shock produces a loss for a long book", wi["total_change"] < 0)

print()
print("   --- empty portfolio ---")
ev = P.build([], account=50000)
check("empty portfolio handled", ev.cash == 50000 and not ev.warnings)
print("      %s" % ev.observations[0])

print()
print("%d failure(s)" % len(fails))
print("SCREENER AND PORTFOLIO OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
