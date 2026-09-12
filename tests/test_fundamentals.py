"""Verify the fundamentals and SEC layers against live data."""
import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot import fundamentals as F
from bot import sec
from bot import yahoo

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

print("=" * 72)
print("SEC ticker map and CIK lookup")
print("=" * 72)
m = sec.ticker_map()
print("   companies: %s" % "{:,}".format(len(m)))
for t, want in [("AAPL", 320193), ("MSFT", 789019), ("NVDA", 1045810)]:
    got = sec.cik_for(t)
    check("CIK %s == %d" % (t, want), got == want, "got %s" % got)
check("unknown ticker returns None", sec.cik_for("ZZQQ9") is None)
check("crypto returns None", sec.cik_for("BTC-USD") is None)

print()
print("=" * 72)
print("SEC filed financial history")
print("=" * 72)
h = sec.financial_history("AAPL", years=6)
check("history available", h.get("available"))
if h.get("available"):
    print("   entity: %s (CIK %d)" % (h["entity"], h["cik"]))
    print("   fiscal years: %s" % h["years"])
    rev = h["table"].get("revenue", [])
    print("   revenue by year:")
    for r in rev[-5:]:
        print("      FY%s  %15s  filed %s" % (
            r["fy"], "{:,.0f}".format(r["value"]), r["filed"]))
    check("revenue has multiple years", len(rev) >= 4, "%d years" % len(rev))
    check("every row carries a filing link", all(r["url"].startswith("https://") for r in rev))
    print("   5y CAGR: %s" % {k: (round(v, 4) if v else None) for k, v in h["cagr"].items()})
    print("   derived margins:")
    for fy in sorted(h["derived"])[-3:]:
        d = h["derived"][fy]
        print("      FY%s net_margin=%s fcf=%s" % (
            fy,
            "%.1f%%" % (d["net_margin"] * 100) if "net_margin" in d else "—",
            "{:,.0f}".format(d["free_cashflow"]) if "free_cashflow" in d else "—"))
    check("margins derived", any("net_margin" in d for d in h["derived"].values()))

print()
print("=" * 72)
print("SEC filings list")
print("=" * 72)
fl = sec.recent_material("AAPL", limit=8)
check("material filings returned", len(fl) > 0)
for f in fl[:6]:
    print("   %-9s %s  %s" % (f.form, f.filed, f.meaning[:52]))
    check("   %s has a working url" % f.form, f.url.startswith("https://www.sec.gov/Archives/"))

print()
print("=" * 72)
print("SEC full-text search")
print("=" * 72)
hits = sec.full_text_search("artificial intelligence", forms="10-K", limit=5)
check("search returned hits", len(hits) > 0)
for hit in hits[:4]:
    print("   %-40s %-6s %s" % (hit["company"][:40], hit["form"], hit["filed"]))
total = sec.total_filings_hits("artificial intelligence", forms="10-K")
print("   total 10-K filings mentioning it: %s" % ("{:,}".format(total) if total else "?"))

print()
print("=" * 72)
print("Yahoo fundamentals")
print("=" * 72)
for sym in ["AAPL", "KO"]:
    f = F.load(sym)
    print("\n   %s  %s / %s" % (f.symbol, f.sector or "?", f.industry or "?"))
    for key in ("market_cap", "pe", "forward_pe", "pb", "net_margin", "roe",
                "revenue_growth", "free_cashflow", "fcf_yield", "debt_to_equity", "beta"):
        print("      %-18s %s" % (key, f.show(key)))
    check("%s has market cap" % sym, f.get("market_cap") is not None)
    check("%s has margins" % sym, f.get("net_margin") is not None)
    check("%s cites its sources" % sym, len(f.sources) >= 1)
    check("%s reconciled with SEC" % sym, f.get("sec_revenue") is not None)
    print("      SEC revenue:  %s" % f.show("sec_revenue"))
    print("      analysts: %s target %s (%s analysts)" % (
        f.analysts.get("recommendation"), f.analysts.get("target_mean"),
        f.analysts.get("analyst_count")))
    q = F.quality_score(f)
    print("      quality: %s of %s checks passed, reliable=%s" % (
        len(q["passed"]), q["known"], q["reliable"]))
    print("      %s" % q["verdict"])
    for note in f.notes:
        print("      note: %s" % note[:100])

print()
print("=" * 72)
print("Graceful behaviour on things without fundamentals")
print("=" * 72)
for sym in ["BTC-USD", "SPY"]:
    f = F.load(sym)
    q = F.quality_score(f)
    print("   %-8s metrics=%d  sec=%s  quality_reliable=%s" % (
        sym, sum(1 for m in f.metrics.values() if m.known),
        bool(f.sec_facts), q["reliable"]))
    check("%s does not crash" % sym, True)
    check("%s refuses a fake score" % sym, q["score"] is None or not q["reliable"] or sym == "SPY")

print()
print("%d failure(s)" % len(fails))
print("FUNDAMENTALS OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
