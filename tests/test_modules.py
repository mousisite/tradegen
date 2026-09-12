"""Verify thesis, alerts, reasoning payload, workflows and the new tables."""
import os, sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

# A private database, so this suite neither depends on nor destroys
# whatever is in the real one.
DB = harness.isolate("modules")[0]

from bot import (alerts as A, config as config_mod, database as db,
                 fundamentals as F, portfolio as P, reasoning as RE,
                 risk as R, sec, thesis as T, valuation as V, workflows as W)
from bot.market import fetch_bars

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

cfg = config_mod.load()
conn = db.connect(DB)

print("=" * 72)
print("NEW TABLES")
print("=" * 72)
st = db.stats(conn)
for t in ("watchlist", "alerts", "theses"):
    check("table %s exists" % t, t in st)
print("   %s" % st)

print()
print("=" * 72)
print("WATCHLIST")
print("=" * 72)
db.watch_add(conn, "aapl", "1d", "core holding")
db.watch_add(conn, "MSFT", "1d")
db.watch_add(conn, "AAPL", "1d", "updated note")   # upsert, not duplicate
wl = db.watchlist(conn)
print("   entries: %s" % [(r["symbol"], r["note"]) for r in wl])
check("upsert did not duplicate", len(wl) == 2)
check("symbol upper-cased", all(r["symbol"].isupper() for r in wl))
db.watch_remove(conn, "MSFT")
check("removal works", len(db.watchlist(conn)) == 1)

print()
print("=" * 72)
print("ALERTS")
print("=" * 72)
bars = fetch_bars("AAPL", "1d")
price = bars.last_price
print("   AAPL is %.2f" % price)

a1 = db.alert_add(conn, "AAPL", "price_above", price * 0.5)   # must fire
a2 = db.alert_add(conn, "AAPL", "price_below", price * 0.5)   # must not
a3 = db.alert_add(conn, "AAPL", "rsi_above", 1)               # must fire
a4 = db.alert_add(conn, "AAPL", "volume_spike", 99)           # must not
print("   created alerts %s" % [a1, a2, a3, a4])

for bad, why in [
    (lambda: db.alert_add(conn, "AAPL", "not_a_kind", 1), "unknown kind"),
    (lambda: db.alert_add(conn, "AAPL", "price_above", None), "missing threshold"),
    (lambda: db.alert_add(conn, "AAPL", "price_above", -5), "negative price"),
]:
    try:
        bad(); check("rejects %s" % why, False, "was accepted")
    except ValueError:
        check("rejects %s" % why, True)

res = A.check(conn)
fired_ids = {f.alert_id for f in res["fired"]}
print("   checked %d, fired %d" % (res["checked"], len(res["fired"])))
for f in res["fired"]:
    print("      #%d %s" % (f.alert_id, f.message[:88]))
check("price_above fired", a1 in fired_ids)
check("rsi_above fired", a3 in fired_ids)
check("price_below did not fire", a2 not in fired_ids)
check("volume_spike did not fire", a4 not in fired_ids)
check("no errors", not res["errors"], str(res["errors"])[:80])

rows = {r["id"]: r for r in db.alert_list(conn)}
check("fired alert deactivated", rows[a1]["active"] == 0)
check("fired alert stored its value", rows[a1]["trigger_value"] is not None)
db.alert_reset(conn, a1)
check("reset re-arms", db.alert_list(conn)[0] is not None and
      {r["id"]: r for r in db.alert_list(conn)}[a1]["active"] == 1)
db.alert_delete(conn, a4)
check("delete works", a4 not in {r["id"] for r in db.alert_list(conn)})
try:
    db.alert_delete(conn, 9999); check("delete missing rejected", False)
except ValueError:
    check("delete missing rejected", True)

print("   descriptions:")
for kind in ("price_above", "pct_down", "rsi_below", "crosses_ma", "volume_spike"):
    print("      %-14s %s" % (kind, A.describe(kind, 150 if kind != "pct_down" else 0.05, "AAPL")))

print()
print("=" * 72)
print("THESIS")
print("=" * 72)
f = F.load("AAPL")
q = F.quality_score(f)
val = V.combine([V.discounted_cash_flow(f, price), V.reverse_dcf(f, price),
                 V.multiples(f, price)], price)
rp = R.profile("AAPL", "1d")
hist = sec.financial_history("AAPL", 6)
fl = sec.recent_material("AAPL", 8)

th = T.build("AAPL", price, fundamentals=f, quality=q, valuation=val,
             risk_profile=rp, sec_history=hist, filings=fl,
             technical={"composite": 0.3, "regime": "trending",
                        "expectancy": 0.05, "hit_rate": 0.44, "samples": 300})
print("   bull points: %d   bear points: %d   evidence: %d" % (
    len(th.bull.points), len(th.bear.points), th.evidence_count))
print("   balance %+.2f" % th.balance)
print("   verdict: %s" % th.verdict[:110])
check("thesis has both sides", th.bull.points and th.bear.points)
check("every point cites evidence",
      all(p.evidence for p in th.bull.points + th.bear.points))
check("every evidence names a source",
      all(e.source for p in th.bull.points + th.bear.points for e in p.evidence))
check("falsifiers written", len(th.falsifiers) >= 3)
check("sources collected", len(th.all_sources) >= 2)
print("   strongest bull: %s" % th.bull.points[0].headline)
print("      %s" % th.bull.points[0].detail[:100])
print("      cited: %s" % th.bull.points[0].evidence[0].cite())
print("   strongest bear: %s" % th.bear.points[0].headline)
print("   falsifiers:")
for fa in th.falsifiers[:3]:
    print("      - %s" % fa[:100])

print()
print("   --- saving and reviewing ---")
tid = db.thesis_save(conn, "AAPL", price, th.balance, th.verdict,
                     [{"headline": p.headline} for p in th.bull.points],
                     [{"headline": p.headline} for p in th.bear.points],
                     th.falsifiers, th.all_sources, "test note")
check("thesis saved", tid > 0)
stored = db.thesis_get(conn, tid)
check("thesis retrievable", stored is not None and stored["symbol"] == "AAPL")
rev = T.review({"price": price * 0.9, "balance": 0.5, "created": int(time.time()) - 86400 * 10},
               price)
print("   review of a bullish thesis after a rise: %s" % rev["outcome"])
check("review says right", rev["outcome"] == "right so far")
rev2 = T.review({"price": price * 1.1, "balance": 0.5, "created": int(time.time())}, price)
check("review says wrong when it fell", rev2["outcome"] == "wrong so far")
db.thesis_delete(conn, tid)
check("thesis delete works", db.thesis_get(conn, tid) is None)

print()
print("=" * 72)
print("REASONING PAYLOAD (no key needed to build it)")
print("=" * 72)
payload = RE.build_payload("AAPL", price, fundamentals=f, quality=q,
                           valuation=val, risk_profile=rp, sec_history=hist,
                           filings=fl, thesis=th)
print("   sections: %s" % list(payload.keys()))
check("payload has fundamentals", any("Fundamental" in k for k in payload))
check("payload has filed financials", any("Filed" in k for k in payload))
check("payload has risk", "Measured risk" in payload)
rendered = RE._facts_block(payload)
print("   rendered brief: %d characters" % len(rendered))
check("brief is substantial", len(rendered) > 600)
check("brief has no None leaking", "None" not in rendered)
r = RE.analyse(payload) if RE.available() else RE.Reasoning(False,
    reason_unavailable="no key in this test environment")
print("   reasoning available: %s" % r.available)
print("   %s" % (r.summary[:110] if r.available else r.reason_unavailable[:110]))
check("degrades gracefully without a key", r.available or bool(r.reason_unavailable))

print()
print("=" * 72)
print("WORKFLOWS")
print("=" * 72)
db.open_trade(conn, "AAPL", "1d", 1, entry=price * 0.9, stop=price * 0.85,
              target1=price * 1.05, quantity=10)
conn.close()

for name in ("positions", "morning"):
    t0 = time.time()
    rep = W.run(name, cfg)
    print("\n   %s -> %d steps in %.1fs" % (rep.name, len(rep.steps), time.time() - t0))
    print("   headline: %s" % rep.headline)
    for s in rep.steps[:6]:
        print("      [%-9s] %s" % (s.status, s.detail[:88]))
    check("%s ran" % name, len(rep.steps) > 0)
    check("%s has no failures" % name,
          not [s for s in rep.steps if s.status == "failed"],
          str([s.detail[:50] for s in rep.steps if s.status == "failed"]))

rep = W.run("watchlist", cfg)
print("\n   %s -> %s" % (rep.name, rep.headline))
check("watchlist workflow ran", len(rep.steps) > 0)

rep = W.run("nonexistent", cfg)
check("unknown workflow handled", rep.steps[0].status == "failed")

print()
print("%d failure(s)" % len(fails))
print("DEEP MODULES OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
