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

print("\n--- screenshot upload safety ---")
import struct
import threading
import time
import zlib
from bot.vision import ChartRead


def claimed_png(width, height):
    """A file of a few bytes whose header claims a vast image."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return io.BytesIO(b"\x89PNG\r\n\x1a\n"
                      + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


def upload(client, image, fmt="PNG"):
    data = io.BytesIO()
    image.save(data, format=fmt)
    data.seek(0)
    return client.post("/analyse/image", data={"chart": (data, "c." + fmt.lower())},
                       content_type="multipart/form-data")


received = []


def fake_read(png):
    shown = Image.open(png)
    received.append((isinstance(png, str), shown.size))
    red, _green, blue = shown.convert("RGB").getpixel((0, 0))
    time.sleep(0.3)       # long enough for a second upload to land meanwhile
    return ChartRead(symbol="REDCO" if red > blue else "BLUECO")


real_read = web.read_chart
web.read_chart = fake_read
try:
    for width, height, label in ((6000, 5000, "30 million"), (20000, 20000, "400 million")):
        started = time.time()
        r = c.post("/analyse/image", data={"chart": (claimed_png(width, height), "b.png")},
                   content_type="multipart/form-data")
        check("a file claiming %s pixels is refused before it is decoded" % label,
              r.status_code == 413 and "too large" in r.data.decode("utf-8", "replace")
              and time.time() - started < 3, "status %d" % r.status_code)
    check("and it never reaches the vision model", not received)

    r = upload(c, Image.new("RGB", (3000, 2000), (200, 30, 30)))
    check("a large real screenshot is read", r.status_code == 302
          and "symbol=REDCO" in r.headers.get("Location", ""), "status %d" % r.status_code)
    check("from memory, not from a file on disk", received and not received[-1][0])
    check("shrunk to what the vision model uses",
          received and max(received[-1][1]) <= web.MAX_EDGE, str(received[-1:]))
    r = upload(c, Image.new("P", (2400, 1200), 3), "GIF")
    check("a palette image is read too", r.status_code == 302,
          "status %d" % r.status_code)
    for fmt in ("JPEG", "WEBP", "BMP"):
        r = upload(c, Image.new("RGB", (640, 360), (200, 30, 30)), fmt)
        check("a %s screenshot is read" % fmt, r.status_code == 302,
              "status %d" % r.status_code)

    # Every upload used to go through one shared file, so two at once could
    # read, or delete, each other's picture.
    landed = {}

    def send(colour):
        r = upload(web.app.test_client(), Image.new("RGB", (800, 400), colour))
        landed[colour] = r.headers.get("Location", "")

    first = threading.Thread(target=send, args=((220, 20, 20),))
    second = threading.Thread(target=send, args=((20, 20, 220),))
    first.start()
    time.sleep(0.05)
    second.start()
    first.join()
    second.join()
    check("two uploads at once are each read from their own picture",
          "REDCO" in landed.get((220, 20, 20), "")
          and "BLUECO" in landed.get((20, 20, 220), ""), str(landed))
finally:
    web.read_chart = real_read

from bot import vision  # noqa: E402

try:
    vision._load_image_bytes(claimed_png(6000, 5000))
    check("the vision reader refuses an oversized image on its own too", False)
except vision.VisionError as exc:
    check("the vision reader refuses an oversized image on its own too",
          "too large" in str(exc), str(exc))
shot = io.BytesIO()
Image.new("RGB", (2000, 1000), (20, 20, 220)).save(shot, format="PNG")
shot.seek(0)
data, kind = vision._load_image_bytes(shot)
check("and reads a picture held in memory, shrunk for the model",
      kind == "image/png" and max(Image.open(io.BytesIO(data)).size) <= vision.MAX_EDGE)

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

# Every typed field is a ticker, a number or a short note. Half a megabyte a
# field let a few hundred posts fill the data disk.
r = c.post("/trades/open", data={"symbol": "AAPL", "direction": "long",
                                 "entry": "200", "stop": "195", "notes": "x" * 30000})
body = r.data.decode("utf-8", "replace")
check("a padded note is refused", r.status_code == 413, "status %d" % r.status_code)
check("with a message about text, not about an image",
      "more text than this form takes" in body and "12 MB" not in body)
conn = db.connect(DB)
try:
    check("and nothing is stored", len(db.list_trades(conn)) == 0)
finally:
    conn.close()

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
# Restoring defaults used to be a plain link, /settings?reset=1, so anyone
# who opened it, or any crawler that followed it, wiped the settings.
c.get("/settings?reset=1")
check("opening the old reset address no longer resets anything",
      cfgmod.load(harness.CFG_PATH)["account_size"] == 5000.0)
r = c.post("/settings", data={"reset": "1"})
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
