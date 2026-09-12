"""End-to-end exercise of every web route, including the new ones."""
import io
import os
import re
import sys

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness
from PIL import Image, ImageDraw

import web
from bot import database as db

fails = []


def check(name, ok, detail=""):
    print("  %s  %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


c, DB = harness.isolated_web("forms")

print("--- pages render ---")
for path in ["/", "/trades", "/strategies", "/settings"]:
    r = c.get(path)
    check("GET %s" % path, r.status_code == 200, "got %d" % r.status_code)

print("\n--- error handling ---")
r = c.get("/nope")
check("unknown page is 404", r.status_code == 404)
r = c.get("/analyse?symbol=ZZQQ99X&interval=1d")
check("bad ticker is handled", r.status_code == 404)
r = c.get("/analyse")
check("empty symbol redirects", r.status_code == 302)

print("\n--- screenshot upload ---")
img = Image.new("RGB", (900, 500), (250, 249, 245))
d = ImageDraw.Draw(img)
d.text((30, 24), "NASDAQ:AAPL  1D", fill=(30, 28, 26))
buf = io.BytesIO()
img.save(buf, format="PNG")
buf.seek(0)

had_key = os.environ.pop("ANTHROPIC_API_KEY", None)
r = c.post("/analyse/image", data={"chart": (buf, "chart.png")},
           content_type="multipart/form-data")
body = r.data.decode("utf-8", "replace")
check("no API key gives a clear message", r.status_code == 400 and "API key" in body,
      "status %d" % r.status_code)
check("no-key page explains .env", "ANTHROPIC_API_KEY=" in body)
if had_key:
    os.environ["ANTHROPIC_API_KEY"] = had_key

r = c.post("/analyse/image", data={}, content_type="multipart/form-data")
check("missing file is rejected", r.status_code == 400)

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
r = c.post("/analyse/image",
           data={"chart": (io.BytesIO(b"this is not an image"), "x.png")},
           content_type="multipart/form-data")
check("non-image is rejected", r.status_code == 400 and "could not be read" in
      r.data.decode("utf-8", "replace"), "status %d" % r.status_code)
os.environ.pop("ANTHROPIC_API_KEY", None)
if had_key:
    os.environ["ANTHROPIC_API_KEY"] = had_key

print("\n--- interval mapping from a chart's own label ---")
for shown, want in [("5m", "5m"), ("15", "1d"), ("1h", "1h"), ("4H", "1h"),
                    ("1D", "1d"), ("Daily", "1d"), (None, "1d"), ("1w", "1d")]:
    got = web._interval_from(shown)
    check("timeframe %r -> %s" % (shown, want), got == want, "got %s" % got)

print("\n--- manual trade entry ---")
r = c.post("/trades/open", data={"symbol": "aapl", "direction": "long",
                                 "entry": "200", "stop": "195", "target": "210",
                                 "quantity": "10", "interval": "1d"})
check("valid long accepted", r.status_code == 302, "status %d" % r.status_code)

r = c.post("/trades/open", data={"symbol": "TSLA", "direction": "long",
                                 "entry": "200", "stop": "205"})
check("long with stop above entry rejected", r.status_code == 400)

r = c.post("/trades/open", data={"symbol": "TSLA", "direction": "short",
                                 "entry": "200", "stop": "195"})
check("short with stop below entry rejected", r.status_code == 400)

r = c.post("/trades/open", data={"symbol": "TSLA", "direction": "long",
                                 "entry": "abc", "stop": "195"})
check("non-numeric price rejected", r.status_code == 400)

r = c.post("/trades/open", data={"symbol": "TSLA"})
check("missing entry and stop rejected", r.status_code == 400)

conn = db.connect(DB)
try:
    rows = db.list_trades(conn, status="open")
    mine = [x for x in rows if x["symbol"] == "AAPL"]
    check("symbol was upper-cased", bool(mine))
    check("only the valid trade was stored", len(rows) == 1, "%d open" % len(rows))
    tid = mine[0]["id"] if mine else None
finally:
    conn.close()

print("\n--- close and delete ---")
if tid:
    r = c.post("/trades/close", data={"trade_id": tid, "price": "210",
                                      "reason": "target"})
    check("close works", r.status_code == 302)
    conn = db.connect(DB)
    try:
        closed = db.list_trades(conn, status="closed")
        got_r = float(closed[0]["r_multiple"]) if closed else None
        check("R computed correctly (+2.00)", got_r is not None and abs(got_r - 2.0) < 1e-6,
              "got %s" % got_r)
    finally:
        conn.close()

    r = c.post("/trades/delete", data={"trade_id": tid})
    check("delete works", r.status_code == 302)
    conn = db.connect(DB)
    try:
        check("trade is gone", len(db.list_trades(conn)) == 0)
    finally:
        conn.close()

r = c.post("/trades/delete", data={"trade_id": 99999})
check("deleting a missing trade is rejected", r.status_code == 400)

print("\n--- settings round trip ---")
r = c.post("/settings", data={"account_size": "5000", "risk_per_trade_pct": "2",
                              "max_position_pct": "30", "cost_bps_equity": "3",
                              "cost_bps_crypto": "15", "interval": "1d",
                              "stop_atr_multiple": "1.5", "target_atr_multiple": "2.25",
                              "horizon_bars": "24", "min_conviction": "0.2",
                              "strong_conviction": "0.45",
                              "require_positive_expectancy": "1",
                              "allow_shorts": "0", "use_llm_sentiment": "1"})
check("settings save", r.status_code == 302)
from bot import config as cfgmod
# Read back from the isolated config the server was told to write, not the real
# one: asserting against the real file also meant overwriting the real file.
check("value persisted", cfgmod.load(harness.CFG_PATH)["account_size"] == 5000.0)
r = c.get("/settings?reset=1")
check("reset works", r.status_code == 302
      and cfgmod.load(harness.CFG_PATH)["account_size"] == 10000.0)

print("\n--- a real analysis, all the way through ---")
r = c.get("/analyse?symbol=MSFT&interval=1d")
check("analysis renders", r.status_code == 200)
b = r.data.decode("utf-8", "replace")
check("a call was made", bool(re.search(r'class="word">(\w+)<', b)))
r = c.get("/api/analyse?symbol=MSFT&interval=1d")
check("json api works", r.status_code == 200 and r.mimetype == "application/json")

print("\n--- every link on every page resolves ---")
seen = set()
for path in ["/", "/trades", "/strategies", "/settings"]:
    body = c.get(path).data.decode("utf-8", "replace")
    for href in re.findall(r'href="(/[^"#?]*)', body):
        if href in seen:
            continue
        seen.add(href)
        code = c.get(href).status_code
        check("link %s" % href, code in (200, 302), "got %d" % code)

print("\n%d failure(s)" % len(fails))
print("WEB OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
