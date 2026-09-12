"""End-to-end exercise of the new research, screener, portfolio and monitor pages."""
import os, re, sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

import web
from bot import database as db

# A private database and config, so this suite neither depends on nor destroys
# whatever is in the real one, and does not care what ran before it.
c, DB = harness.isolated_web("web")

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)


print("=" * 72)
print("PAGES")
print("=" * 72)
for path in ["/", "/screener", "/portfolio", "/monitor", "/settings",
             "/trades", "/strategies"]:
    r = c.get(path)
    check("GET %s" % path, r.status_code == 200, "got %d" % r.status_code)

print()
print("=" * 72)
print("RESEARCH")
print("=" * 72)
t0 = time.time()
r = c.get("/research?symbol=AAPL&interval=1d")
print("   /research AAPL -> %d in %.1fs (%d bytes)" % (
    r.status_code, time.time() - t0, len(r.data)))
check("research renders", r.status_code == 200)
body = r.data.decode("utf-8", "replace")
for probe in ["The case, both ways", "The business", "As filed with the SEC",
              "What it might be worth", "How much this can hurt",
              "What options are pricing", "Recent filings",
              "Where all of this came from", "What would prove this wrong",
              "Key figures", "sec.gov"]:
    check("research shows '%s'" % probe, probe in body)

t0 = time.time()
r2 = c.get("/research?symbol=AAPL&interval=1d")
check("research is cached", (time.time() - t0) < 1.0, "%.1fs" % (time.time() - t0))

r = c.get("/research?symbol=BTC-USD&interval=1d")
check("research handles crypto", r.status_code == 200, "got %d" % r.status_code)
cb = r.data.decode("utf-8", "replace")
check("crypto says what is unavailable",
      "Not available" in cb or "could not" in cb.lower() or "No valuation" in cb)

r = c.get("/research?symbol=ZZQQ99&interval=1d")
check("bad ticker in research is handled", r.status_code == 404)

print()
print("=" * 72)
print("SCREENER")
print("=" * 72)
r = c.get("/screener?go=1&symbols=AAPL+MSFT+NVDA+KO+T&market_cap_min=50000000000")
check("custom screen runs", r.status_code == 200)
sb = r.data.decode("utf-8", "replace")
rows = re.findall(r"<td><b>([A-Z.\-]+)</b>", sb)
print("   passed: %s" % rows)
check("screen returned rows", len(rows) >= 2)
check("screen shows the filters used", "Market cap at least" in sb)

t0 = time.time()
r = c.get("/screener?preset=quality_value")
print("   preset quality_value -> %d in %.1fs" % (r.status_code, time.time() - t0))
check("preset screen runs", r.status_code == 200)
pb = r.data.decode("utf-8", "replace")
check("preset explains itself", "Profitable and not expensive" in pb)

r = c.get("/screener?go=1&symbols=AAPL&pe_max=0.01")
check("impossible filter handled", r.status_code == 200 and
      "Nothing passed" in r.data.decode("utf-8", "replace"))

print()
print("=" * 72)
print("WATCHLIST AND ALERTS")
print("=" * 72)
r = c.post("/watch/add", data={"symbol": "aapl", "interval": "1d"})
check("watch add", r.status_code == 302)
conn = db.connect(DB)
check("watch stored upper-case", db.watchlist(conn)[0]["symbol"] == "AAPL")
conn.close()

r = c.post("/monitor/alert/add", data={"symbol": "AAPL", "kind": "price_above",
                                       "threshold": "1", "interval": "1d"})
check("alert add", r.status_code == 302)
r = c.post("/monitor/alert/add", data={"symbol": "AAPL", "kind": "pct_down",
                                       "threshold": "5", "interval": "1d"})
check("percent alert add", r.status_code == 302)
conn = db.connect(DB)
rows = db.alert_list(conn)
pct_alert = [a for a in rows if a["kind"] == "pct_down"][0]
print("   5 typed for percent stored as %.3f" % pct_alert["threshold"])
check("percent converted from whole number", abs(pct_alert["threshold"] - 0.05) < 1e-9)
conn.close()

for bad, why in [
    ({"symbol": "AAPL", "kind": "nonsense", "threshold": "1"}, "unknown kind"),
    ({"symbol": "AAPL", "kind": "price_above", "threshold": ""}, "missing value"),
    ({"symbol": "AAPL", "kind": "price_above", "threshold": "abc"}, "non-numeric"),
]:
    r = c.post("/monitor/alert/add", data=bad)
    check("alert rejects %s" % why, r.status_code == 400, "got %d" % r.status_code)

r = c.post("/monitor/alerts/check")
check("alert check runs", r.status_code == 302)
conn = db.connect(DB)
fired = [a for a in db.alert_list(conn) if a["triggered_ts"]]
print("   fired: %d" % len(fired))
check("price_above 1 fired", len(fired) >= 1)
aid = fired[0]["id"]
conn.close()

r = c.post("/monitor/alert/reset", data={"alert_id": aid})
check("alert reset", r.status_code == 302)
r = c.post("/monitor/alert/delete", data={"alert_id": aid})
check("alert delete", r.status_code == 302)
r = c.post("/monitor/alert/delete", data={"alert_id": 99999})
check("deleting a missing alert rejected", r.status_code == 400)
r = c.post("/monitor/alert/bogus", data={"alert_id": 1})
check("unknown alert action rejected", r.status_code == 404)

print()
print("=" * 72)
print("THESIS")
print("=" * 72)
r = c.post("/thesis/save", data={"symbol": "AAPL", "interval": "1d",
                                 "note": "testing"})
check("thesis save", r.status_code == 302, "got %d" % r.status_code)
conn = db.connect(DB)
saved = db.thesis_list(conn, "AAPL")
print("   saved %d thesis rows" % len(saved))
check("thesis stored", len(saved) == 1)
if saved:
    import json
    bull = json.loads(saved[0]["bull_json"])
    check("thesis kept its bull points", len(bull) > 0)
    check("thesis points carry evidence",
          all(p.get("evidence") for p in bull))
    tid = saved[0]["id"]
conn.close()

r = c.get("/research?symbol=AAPL&interval=1d")
check("research shows the saved thesis",
      "Theses you saved" in r.data.decode("utf-8", "replace"))
r = c.post("/thesis/delete", data={"thesis_id": tid, "back": "/monitor"})
check("thesis delete", r.status_code == 302)

print()
print("=" * 72)
print("PORTFOLIO")
print("=" * 72)
conn = db.connect(DB)
db.open_trade(conn, "AAPL", "1d", 1, entry=300, stop=285, target1=340, quantity=30)
db.open_trade(conn, "NVDA", "1d", 1, entry=200, stop=190, target1=230, quantity=40)
conn.close()
t0 = time.time()
r = c.get("/portfolio")
print("   /portfolio -> %d in %.1fs" % (r.status_code, time.time() - t0))
check("portfolio renders with positions", r.status_code == 200)
pb = r.data.decode("utf-8", "replace")
for probe in ["Holdings", "Do these move together", "If the market fell",
              "What this adds up to"]:
    check("portfolio shows '%s'" % probe, probe in pb)

print()
print("=" * 72)
print("WORKFLOWS")
print("=" * 72)
for name in ("positions", "morning", "hunt"):
    t0 = time.time()
    r = c.post("/workflow/%s" % name)
    print("   %s -> %d in %.1fs" % (name, r.status_code, time.time() - t0))
    check("workflow %s runs" % name, r.status_code == 302)
r = c.post("/workflow/nonexistent")
check("unknown workflow rejected", r.status_code == 404)

r = c.get("/monitor")
mb = r.data.decode("utf-8", "replace")
check("monitor shows the last run", "Run now" in mb)

print()
print("=" * 72)
print("EVERY LINK RESOLVES")
print("=" * 72)
seen = set()
for path in ["/", "/screener", "/portfolio", "/monitor", "/settings", "/trades",
             "/strategies", "/research?symbol=AAPL&interval=1d"]:
    body = c.get(path).data.decode("utf-8", "replace")
    raw = re.findall(r'href="(/[^"#]*)"', body)
    for href in [h.replace("&amp;", "&") for h in raw]:
        if href in seen or href.startswith("/static"):
            continue
        seen.add(href)
        code = c.get(href).status_code
        check("link %s" % href[:56], code in (200, 302), "got %d" % code)

print()
print("%d failure(s)" % len(fails))
print("WEB DEEP OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
