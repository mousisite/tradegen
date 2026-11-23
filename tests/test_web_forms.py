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
from markupsafe import escape as _markup_escape

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

    # The bar size chosen on the page counts; only a request that names none
    # there is falls back on the picture's own, whatever kind of value that is.
    for sent, seen, want in (("1d", "5m", "1d"), ("zzz", "5m", "5m"), (None, "1h", "1h"),
                             (None, 5, "1d")):
        web.read_chart = lambda png, seen=seen: ChartRead(symbol="AAPL", timeframe=seen)
        shot = io.BytesIO()
        Image.new("RGB", (400, 200), (200, 30, 30)).save(shot, format="PNG")
        shot.seek(0)
        fields = {"chart": (shot, "c.png")}
        if sent:
            fields["interval"] = sent
        r = c.post("/analyse/image", data=fields, content_type="multipart/form-data")
        check("bar size %s with a picture saying %r opens %s bars" % (sent, seen, want),
              r.status_code == 302 and "interval=%s&" % want in r.headers.get("Location", "")
              + "&", str(r.headers.get("Location")))
    web.read_chart = fake_read
    page = c.get("/").data.decode("utf-8", "replace")
    check("the page says which bar size a picture is analysed on",
          "bar size is the one chosen" in " ".join(page.split()))
    check("and holds no image without a picture",
          all(" src=" in tag for tag in re.findall(r"<img\b[^>]*>", page)),
          str(re.findall(r"<img\b[^>]*>", page)))

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

# A target on the losing side, or no price at all, read as "At target" the
# moment the trade was logged.
for side, entry, stop, target, words in (
        ("long", "100", "95", "90", "the target must sit above the entry"),
        ("long", "100", "95", "100", "the target must sit above the entry"),
        ("long", "100", "95", "-5", "must be a positive price"),
        ("long", "100", "95", "0", "must be a positive price"),
        ("short", "100", "105", "110", "the target must sit below the entry")):
    r = c.post("/trades/open", data={"symbol": "TSLA", "direction": side, "entry": entry,
                                     "stop": stop, "target": target})
    check("a %s from %s with a target of %s is refused in words" % (side, entry, target),
          r.status_code == 400 and words in r.data.decode("utf-8", "replace"),
          "status %d" % r.status_code)
for extra, words in (({"direction": "sideways"}, "long or short"),
                     ({"interval": "zzz"}, "Pick an interval"),
                     ({"symbol": "A" * 300}, "too long for a ticker")):
    r = c.post("/trades/open", data=dict({"symbol": "TSLA", "direction": "long",
                                          "entry": "100", "stop": "95"}, **extra))
    check("a made-up %s is refused in words" % list(extra)[0], r.status_code == 400
          and words in r.data.decode("utf-8", "replace"), "status %d" % r.status_code)

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
check("in words, since the first press of Delete may have removed it",
      "It may have been deleted already." in r.data.decode("utf-8", "replace"))

# A quote in what was typed as the ticker ended the text of the Delete button's
# question, and what followed it ran as script.
r = c.post("/trades/open", data={"symbol": "X'+Y+'", "direction": "long",
                                 "entry": "100", "stop": "95"})
body = c.get("/trades").data.decode("utf-8", "replace")
asked = re.findall(r"onsubmit='return confirm\((.*?)\);'", body)
check("a quote in a ticker stays inside the Delete question",
      r.status_code == 302 and len(asked) == 1 and "X'" not in asked[0]
      and __import__("json").loads(asked[0])
      == "Delete the X'+Y+' trade? This cannot be undone.", str(asked))
conn = db.connect(DB)
try:
    for row in db.list_trades(conn):
        db.delete_trade(conn, row["id"])
finally:
    conn.close()

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
# The default interval did nothing on the Analyse box, which said daily always.
body = c.get("/").data.decode("utf-8", "replace")
check("daily is preselected on the Analyse box", '<option value="1d" selected>' in body
      and "Daily bars are preselected because" in body
      and "trading costs take the least from them" in " ".join(body.split()))
check("and no claim is made that other bar sizes never pay",
      "the only interval" not in body)
c.post("/settings", data={"interval": "1h"})
body = c.get("/").data.decode("utf-8", "replace")
check("the default interval chosen in Settings is the one preselected there",
      '<option value="1h" selected>' in body and '<option value="1d" selected>' not in body
      and "The bar size you chose in Settings" in body)
check("and the one the quick picks and the screenshot box use",
      "symbol=AAPL&amp;interval=1h" in body and "Daily bars are preselected because" not in body)
c.post("/settings", data={"interval": "1wk"})
body = c.get("/").data.decode("utf-8", "replace")
check("one the box does not offer leaves daily preselected",
      '<option value="1d" selected>' in body)
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

print("\n--- landmarks and names on every page ---")
for path in ["/", "/trades", "/strategies", "/settings"]:
    body = c.get(path).data.decode("utf-8", "replace")
    check("%s has one main landmark" % path, body.count('<main id="main">') == 1)
    check("%s starts with a link that skips to it" % path,
          0 <= body.find('href="#main"') < body.find("<header"))
    check("%s has one h1" % path, body.count("<h1") == 1, body.count("<h1"))
body = c.get("/trades").data.decode("utf-8", "replace")
check("the journal is called the journal in its title and the nav",
      "<title>Journal · Orenth</title>" in body and ">Journal</a>" in body
      and ">Trades</a>" not in body)

print("\n--- the analysis page, on made-up prices (no network) ---")
import numpy as np
from bot import accounts as accounts_mod
from bot import catalysts as catalysts_mod
from bot import config as config_mod
from bot import engine as engine_mod
from bot import market as market_mod
from bot import public as public_mod


def made_up_bars(symbol="FAKE", n=1250, leg=60, into=40, dip=4):
    """Rises and falls in turn, ending on a turn up inside a rise: prices with
    a real edge for the signals, so the analysis has a plan to draw. On random
    prices it had one only until the strategies that vote were changed."""
    rng = np.random.RandomState(0)
    k = np.arange(n)
    sign = np.where(((k - (n - into)) // leg) % 2 == 0, 1.0, -1.0)
    rets = sign * 0.005 + rng.normal(0.0, 0.01, n)
    rets[-dip - 1:-1] = -0.012 + rng.normal(0.0, 0.002, dip)
    rets[-1] = 0.02
    close = 100.0 * np.exp(np.cumsum(rets))
    prev = np.concatenate([[100.0], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, 0.002, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, 0.003, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, 0.003, n)))
    open_[-1] = low[-1] = close[-2] * 0.998
    high[-1] = close[-1] = max(close[-1], high[-2] * 1.002)
    start = 1_600_000_000 - (1_600_000_000 % 86400) + 14 * 3600 + 1800
    ts = np.array([start + 86400 * i for i in range(n)], dtype=np.int64)
    return market_mod.Bars(symbol=symbol, name="Made Up Corp", asset_class="equity",
                           currency="USD", exchange="NMS", interval="1d",
                           timestamp=ts, open=open_, high=high, low=low, close=close,
                           volume=rng.randint(800_000, 1_600_000, n).astype(float),
                           session_id=ts // 86400, prev_close=float(close[-2]),
                           market_open=False, last_bar_time="2026-11-11")


def offline(*_a, **_k):
    raise RuntimeError("offline")


_real = (engine_mod.resolve, engine_mod.fetch_bars, catalysts_mod.fetch,
         web.cached_analysis, web.portfolio_mod.overlap)
_bars = made_up_bars()
engine_mod.resolve = lambda s: {"symbol": "FAKE", "name": "Made Up Corp"}
engine_mod.fetch_bars = lambda s, iv=None: _bars
catalysts_mod.fetch = offline
try:
    fake = engine_mod.analyse("FAKE", config_mod.load(harness.CFG_PATH), interval="1d",
                              with_news=False, record=False)
    web.cached_analysis = lambda *a, **k: fake
    web.portfolio_mod.overlap = lambda *a, **k: {"warnings": [], "pairs": []}
    fake.run_id = 4242
    check("the made-up stock gives a plan to draw",
          None not in (fake.plan.entry, fake.plan.stop, fake.plan.target1),
          fake.plan.action)
    # Only a call to act now is a trade. A WAIT's entry is a price the market
    # has not reached, and logging it put a fill that never happened in the
    # journal.
    for action, offered in (("BUY", True), ("SHORT", True), ("WAIT", False)):
        fake.plan.action = action
        body = c.get("/analyse?symbol=FAKE&interval=1d&news=0").data.decode("utf-8", "replace")
        check("a %s %s logged as a paper trade" % (action, "can be" if offered else "cannot be"),
              ('href="/trades/simulate?run_id=4242"' in body) == offered)
        # The button opens a review first; nothing is written from this page.
        check("and a %s page never logs one straight away" % action,
              'action="/trades/take"' not in body)
    check("and the WAIT says why", "A wait is not a trade yet" in body)
    check("the trade is drawn on its own candles", 'class="chart trade"' in body
          and "The plan on the chart" in body)
    check("with the plan's prices across them",
          "Short at " in body or "Buy at " in body)
    check("and when the price was set, in words and for a screen reader",
          'aria-label="Daily price, as of 2026-11-11, market closed"' in body
          and ">as of 2026-11-11, market closed</span>" in body
          and "Daily candles" in body)
    check("drawn twice, so a phone gets the narrow one",
          body.count('class="chart trade"') == 2 and ".tradechart .narrow" in body)
    # A finger cannot point at a candle: those words are for a mouse alone.
    with open(os.path.join(harness.ROOT, "static", "style.css"), encoding="utf-8") as fh:
        touch = fh.read().split("@media (hover: none) {", 1)[1].split("\n}", 1)[0]
    check("the words about pointing at a candle are hidden from a touch screen",
          '<span class="hover-only">Point at a candle' in body
          and ".hover-only { display: none; }" in touch)
    check("and are not in the iPhone app at all", "Point at a candle" not in c.get(
        "/analyse?symbol=FAKE&interval=1d&news=0",
        headers={"User-Agent": "Mozilla/5.0 (iPhone) OrenthApp/1"}).data.decode(
            "utf-8", "replace"))
    fake.plan.action = "AVOID"
    body = c.get("/analyse?symbol=FAKE&interval=1d&news=0").data.decode("utf-8", "replace")
    check("an AVOID draws the prices with no plan across them",
          'class="chart trade"' in body and "No plan is drawn" in body
          and "Stop " not in body.split("The price</h2>")[-1].split("</section>")[0])
    fake.plan.action = "WAIT"
    check("the symbol is the page's heading", '<h1 class="sym">FAKE</h1>' in body)
    check("the plain-words box links to how it is tested",
          "How the strategies are tested" in body)
    shown = sorted(fake.signals, key=lambda s: -abs(s.contribution))[:12]
    check("strategy names are the ones the public pages use",
          all(str(_markup_escape(public_mod.strategy_label(s.name))) in body for s in shown),
          repr([public_mod.strategy_label(s.name) for s in shown
                if str(_markup_escape(public_mod.strategy_label(s.name))) not in body]))
    garbled = [s.name.replace("_", " ").capitalize() for s in shown
               if s.name.replace("_", " ").capitalize() != public_mod.strategy_label(s.name)]
    check("with no 'Rsi' or 'Macd' left",
          garbled and not any(g in body for g in garbled), garbled)
finally:
    (engine_mod.resolve, engine_mod.fetch_bars, catalysts_mod.fetch,
     web.cached_analysis, web.portfolio_mod.overlap) = _real

print("\n--- the error page ---")


def no_such_ticker(*_a, **_k):
    raise web.DataError("No instrument found for 'ZZQQ'.")


web.cached_analysis = no_such_ticker
try:
    r = c.get("/analyse?symbol=ZZQQ&interval=1d")
    body = r.data.decode("utf-8", "replace")
    check("a ticker that is not there gets the advice about tickers",
          r.status_code == 404 and "ticker does not exist" in body
          and "Try another ticker" in body)
finally:
    web.cached_analysis = _real[3]


asked = []


def keeps_settings(symbol, interval, cfg, *_a, **_k):
    asked.append((cfg["account_size"], cfg["risk_per_trade_pct"]))
    raise web.DataError("No instrument found for 'ZZQQ'.")


# A number in the address that is none sized nonsense, or was an error page.
web.cached_analysis = keeps_settings
try:
    own = (cfgmod.load(harness.CFG_PATH)["account_size"],
           cfgmod.load(harness.CFG_PATH)["risk_per_trade_pct"])
    for query in ("account=nan", "account=inf", "account=1e999", "account=-5", "account=0",
                  "account=1e300", "risk=nan", "risk=-1", "risk=0", "risk=500", "risk=abc"):
        c.get("/analyse?symbol=ZZQQ&interval=1d&" + query)
        check("%s in the address is ignored" % query, asked and asked[-1] == own,
              str(asked[-1:]))
    c.get("/analyse?symbol=ZZQQ&interval=1d&account=25000&risk=2.5")
    check("a proper one is used", asked[-1] == (25000.0, 2.5), str(asked[-1:]))
finally:
    web.cached_analysis = _real[3]


def turned_away(*_a, **_k):
    raise web.market_mod.DataThrottled("The market data source limits how often "
                                       "this site may ask for prices.", 90)


# A rate limit at the source was a 404 that told people their ticker might not
# exist. It is a wait, and says how long: a 503 with Retry-After.
web.cached_analysis = turned_away
try:
    for url in ("/analyse?symbol=AAPL&interval=1d", "/research?symbol=AAPL&interval=1d"):
        r = c.get(url)
        body = r.data.decode("utf-8", "replace")
        check("a rate limit on %s is a 503 that says when to come back" % url.split("?")[0],
              r.status_code == 503 and r.headers.get("Retry-After") == "90"
              and "limits how often" in body, (r.status_code, r.headers.get("Retry-After")))
        check("and gives no advice about tickers that do not exist",
              "ticker does not exist" not in body)
    r = c.get("/api/analyse?symbol=AAPL")
    check("the API says the same in JSON",
          r.status_code == 503 and r.headers.get("Retry-After") == "90"
          and (r.get_json() or {}).get("retry_after") == 90,
          (r.status_code, r.data[:120]))
finally:
    web.cached_analysis = _real[3]


def timed_out(*_a, **_k):
    raise web.market_mod.DataUnavailable("could not fetch AAPL: The market data source "
                                         "could not be used: it took too long to answer.")


# A source that does not answer is a wait too, not a missing ticker.
web.cached_analysis = timed_out
try:
    r = c.get("/analyse?symbol=AAPL&interval=1d")
    body = r.data.decode("utf-8", "replace")
    check("a source that did not answer is a 503 with a time to come back",
          r.status_code == 503 and r.headers.get("Retry-After") == "60"
          and "took too long" in body, (r.status_code, r.headers.get("Retry-After")))
    check("with no advice about tickers that do not exist",
          "ticker does not exist" not in body)
    r = c.get("/api/analyse?symbol=AAPL")
    check("and the API says so in JSON",
          r.status_code == 503 and (r.get_json() or {}).get("retry_after") == 60,
          (r.status_code, r.data[:120]))
    # An account looping the API over a list of tickers is told to wait. Your
    # own machine, which the rest of this suite runs as, never is.
    real_limited, asked_of = web._rate_limited, []
    web._rate_limited = lambda action, most: (asked_of.append((action, most)), True)[1]
    real_search, web.market_mod.search = web.market_mod.search, lambda q: 1 / 0
    try:
        r = c.get("/api/analyse?symbol=AAPL")
        check("past its hourly budget, the API is a 429 in JSON with a time to come back",
              r.status_code == 429 and r.headers.get("Retry-After")
              and (r.get_json() or {}).get("error"), (r.status_code, r.data[:120]))
        r = c.get("/api/search?q=appl")
        check("and the search box suggests nothing, without asking the source",
              r.status_code == 200 and r.get_json() == [], (r.status_code, r.data[:120]))
        check("each on its own budget", asked_of == [("json", web.JSON_READS_PER_HOUR),
                                                      ("search", web.SEARCHES_PER_HOUR)],
              asked_of)
    finally:
        web._rate_limited, web.market_mod.search = real_limited, real_search
finally:
    web.cached_analysis = _real[3]

r = c.post("/trades/take", data={"run_id": "999999"},
           headers={"Referer": "http://localhost/analyse?symbol=FAKE&interval=1d"})
body = r.data.decode("utf-8", "replace")
check("any other failure gets no advice about tickers",
      r.status_code == 400 and "ticker does not exist" not in body)
check("and a way back to the page it came from",
      ">Go back</a>" in body and 'href="http://localhost/analyse?symbol=FAKE' in body)
r = c.post("/trades/take", data={"run_id": "999999"},
           headers={"Referer": "https://elsewhere.example/page"})
body = r.data.decode("utf-8", "replace")
check("but never back to another site",
      "elsewhere.example" not in body and ">Front page</a>" in body)

# The 500 page used to print the exception. Exceptions carry paths, queries
# and internal names; the visitor gets a sentence and the log gets the rest.
_propagate = web.app.config.get("PROPAGATE_EXCEPTIONS")
_board = web.db_mod.strategy_leaderboard


def broken(*_a, **_k):
    raise RuntimeError("OperationalError: no such table at C:/secret/stockbot.db")


web.app.config["PROPAGATE_EXCEPTIONS"] = False
web.app.logger.disabled = True
web.db_mod.strategy_leaderboard = broken
try:
    r = c.get("/strategies")
    body = r.data.decode("utf-8", "replace")
    check("something unexpected is a 500", r.status_code == 500, r.status_code)
    check("which never shows the exception's text",
          "secret" not in body and "OperationalError" not in body
          and "Internal Server Error" not in body)
    check("and says what happened plainly", "Something broke on our side" in body)
finally:
    web.db_mod.strategy_leaderboard = _board
    web.app.config["PROPAGATE_EXCEPTIONS"] = _propagate
    web.app.logger.disabled = False

print("\n--- the strategies page and recent analyses ---")
conn = db.connect(DB)
try:
    conn.execute("INSERT OR REPLACE INTO strategy_stats (symbol, interval, strategy, family, "
                 "regime, n, wins, sum_r, sum_r_gross, sum_bars, updated_ts) VALUES "
                 "('ZZLOCAL', '1d', 'macd_momentum', 'trend', 'all', 60, 30, 3.0, 6.0, "
                 "500, 1)")
    me = accounts_mod.local_user(conn).id
    conn.execute("INSERT INTO runs (ts, symbol, interval, action, entry, stop, target1, "
                 "expectancy_r, user_id) VALUES (?, 'ZZRUN', '1d', 'WAIT', 10, 9, 12, "
                 "0.123, ?)", (int(__import__('time').time()), me))
    conn.commit()
finally:
    conn.close()
body = c.get("/strategies?symbol=ZZLOCAL").data.decode("utf-8", "replace")
check("strategies use the shared name", "MACD momentum" in body and "Macd momentum" not in body)
check("both expectancy columns are in R", "+0.050R" in body and "+0.100R" in body)
check("it points to the daily strategy tests, and says whose record it is",
      'href="/backtests">Strategy tests</a>' in body[:body.index("</main>")]
      and "you have analysed" in body)
body = c.get("/").data.decode("utf-8", "replace")
check("recent analyses give expectancy in R, like the record above them", "+0.123R" in body)
_row = " ".join(body[body.index("ZZRUN"):].split())
check("and write the call as the rest of the page does",
      '<span class="chip hold"> WAIT</span>' in _row, _row[:600])
conn = db.connect(DB)
try:
    for symbol in ("ONE", "TWO"):
        db.close_trade(conn, db.open_trade(conn, symbol, "1d", 1, entry=100, stop=95,
                                           target1=110, quantity=1, user=me), 110.0, "target")
        body = " ".join(c.get("/").data.decode("utf-8", "replace").split())
        want = "1 closed trade cannot" if symbol == "ONE" else "2 closed trades cannot"
        check("the front page counts '%s'" % want, want in body)
finally:
    conn.close()
body = c.get("/settings").data.decode("utf-8", "replace")
check("on your own machine, settings say they stay on it",
      "never leaves this machine" in body and "config.json" in body)
check("and restoring defaults asks first", "return confirm(" in body)

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
