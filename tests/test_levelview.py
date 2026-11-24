"""The analysis page's "What price is doing" section, on made-up prices.

levelview.for_analysis on a future's round-the-clock 5-minute bars, a stock's
day-session 5-minute bars and daily bars: the documented keys, yesterday's high
and low as the bars have them, the Asia and London rows only where the data
has those sessions, the distances, when a level was taken (in New York time),
the clock strip at fixed times, the day's lean with a fetch that fails and
with a stored record. Then the analysis page itself, through the test client
with the price source stubbed: the section is there, says no None, nan or
undefined, every "?" it uses has words, a daily analysis shows only structure
and levels, and inside the iPhone app the video is named but not linked. No
network.
"""
import dataclasses
import json
import logging
import math
import os as _os
import re
import sys as _sys
from types import SimpleNamespace as NS

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for key in ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET", "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(key, None)

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
import web  # noqa: E402
from bot import bias as bias_mod  # noqa: E402
from bot import catalysts, clock, engine, labstore, levelview, liquidity, plain  # noqa: E402
from bot import database as db  # noqa: E402
from bot import market as market_mod  # noqa: E402
from bot import sentiment as sentiment_mod  # noqa: E402

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


def flat(html):
    return " ".join(html.split())


def ending_at(bars, hh, mm):
    """The bars cut after the last bar that starts before hh:mm New York time
    on the last whole day they reach, so a session is known to be over."""
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    days = clock.day_table(bars)
    for d in reversed(days[:-1]):
        for i in range(d.last, d.first - 1, -1):
            if clock.ny_clock(int(ts[i]))[1] < hh * 60 + mm:
                return clock.head(bars, i + 1)
    raise AssertionError("no such bar")


c, DB = harness.isolated_web("levelview")
web.app.config["AUTH_MODE"] = None
web._example_state["busy"] = True

FUTURE = ending_at(sc.futures_series(5200, roll_day=None), 10, 42)
STOCK = ending_at(sc.stock_series(3000), 11, 2)
DAILY = sc.daily_series(600)
HOURLY = sc.hourly_path(sc.BIAS_UP)
NQ = sc.walk(FUTURE.timestamp, seed=21, kind="future", vol=0.0008, price=21000.0,
             symbol="NQ=F")


def hourly_fetch(symbol, interval):
    return HOURLY if interval == "1h" else NQ


def broken_fetch(symbol, interval):
    raise market_mod.DataError("the made-up source broke on purpose")


def analysis(bars):
    return NS(bars=bars, context=None)


KEYS = {"kind", "intraday", "symbol", "interval", "words", "close", "clock", "structure",
        "levels", "bias", "chart", "calm", "notes", "pair", "video"}
LEVEL_KEYS = {"name", "price", "side", "pct", "atr", "taken", "taken_word", "taken_at",
              "untaken", "source"}
STRUCTURE_KEYS = {"trend", "since", "flip_level", "last_flip", "last_take", "gaps", "eq",
                  "points"}

head("THE DICT HAS THE DOCUMENTED KEYS")
at_955 = clock.ny(2026, 11, 18, 9, 55)
levelview._FETCHED.clear()
fut = levelview.for_analysis(analysis(FUTURE), {}, fetch=hourly_fetch, now=at_955)
stock = levelview.for_analysis(analysis(STOCK), {}, fetch=hourly_fetch, now=at_955)
daily = levelview.for_analysis(analysis(DAILY), {}, fetch=hourly_fetch, now=at_955)
for name, out in (("future", fut), ("stock", stock), ("daily", daily)):
    check("%s: every top-level key" % name, set(out) == KEYS, sorted(set(out) ^ KEYS))
    check("%s: the structure's keys" % name, set(out["structure"] or {}) == STRUCTURE_KEYS,
          sorted(set(out["structure"] or {}) ^ STRUCTURE_KEYS))
    check("%s: each level's keys" % name,
          out["levels"] and all(set(r) == LEVEL_KEYS for r in out["levels"]))
    check("%s: points in words, each with a help key that has words" % name,
          out["structure"]["points"] and all(
              p["text"] and (not p["help"] or p["help"] in plain.HELP)
              for p in out["structure"]["points"]))
check("the kinds are read from the bars", (fut["kind"], stock["kind"], daily["kind"])
      == ("future", "stock", "daily"))
check("a daily analysis gets structure and levels, and no clock, lean or pair",
      daily["clock"] is None and daily["bias"] is None and daily["pair"] is None
      and daily["structure"] and daily["levels"])
check("an intraday one gets the clock and the lean",
      fut["clock"] and fut["bias"] and stock["clock"] and stock["bias"])
check("the chart is drawn wide and narrow", fut["chart"].get("wide", "").startswith("<svg")
      and fut["chart"].get("narrow", "").startswith("<svg"))
check("calm colours follow the setting",
      levelview.for_analysis(analysis(DAILY), {"calm_candles": True})["calm"] is True
      and daily["calm"] is False)

head("YESTERDAY'S HIGH AND LOW ARE THE BARS'")
for name, bars, out in (("future", FUTURE, fut), ("stock", STOCK, stock)):
    days = clock.day_table(bars)
    prev = [d for d in days[:-1] if d.complete][-1]
    rows = {r["name"]: r for r in out["levels"]}
    check("%s: yesterday's high is the last whole day's" % name,
          abs(rows["Yesterday's high"]["price"] - prev.high) < 1e-9,
          (rows.get("Yesterday's high"), prev.high))
    check("%s: and its low" % name, abs(rows["Yesterday's low"]["price"] - prev.low) < 1e-9)
    check("%s: and its close" % name, abs(rows["Yesterday's close"]["price"] - prev.close) < 1e-9)
rows = {r["name"]: r for r in daily["levels"]}
check("daily bars: yesterday is the bar before",
      abs(rows["Yesterday's high"]["price"] - float(DAILY.high[-2])) < 1e-9
      and abs(rows["Yesterday's low"]["price"] - float(DAILY.low[-2])) < 1e-9)

head("SESSIONS WHERE THE DATA HAS THEM")
names = {r["name"] for r in fut["levels"]}
check("the future has the Asia and London ranges",
      {"Asia session high", "Asia session low", "London session high",
       "London session low"} <= names, sorted(names))
today = clock.day_table(FUTURE)[-1]
rows = {r["name"]: r for r in fut["levels"]}
check("and they are today's sessions'",
      abs(rows["Asia session high"]["price"] - today.sessions["asia"].high) < 1e-9
      and abs(rows["London session low"]["price"] - today.sessions["london"].low) < 1e-9)
check("with the before-the-open range, and the 9:30 to 10:00 range once it is over",
      "New York before the open high" in names and "Opening range high (9:30 to 10:00)" in names)
snames = {r["name"] for r in stock["levels"]}
check("the stock has none of them", not any(n.startswith(("Asia", "London", "New York before"))
                                            for n in snames), sorted(snames))
check("and says why, where the levels are",
      any("no Asia or London ranges" in note for note in stock["notes"]), stock["notes"])

head("DISTANCES")
s = liquidity.series(FUTURE)
close = float(FUTURE.close[-1])
atr = float(s["st_atr"][-1])
check("each row's percent is from the last close",
      all(abs(r["pct"] - (r["price"] - close) / close * 100.0) < 1e-9 for r in fut["levels"]))
check("and its typical bars are the distance over the 14-bar average range",
      all(abs(r["atr"] - abs(r["price"] - close) / atr) < 1e-9 for r in fut["levels"]))
check("above and below are said against it",
      all((r["side"] == "above") == (r["price"] > close) for r in fut["levels"]))
check("the rows run from the highest price down",
      [r["price"] for r in fut["levels"]] == sorted((r["price"] for r in fut["levels"]),
                                                    reverse=True))
check("the old swing highs are the untaken ones above",
      sorted(r["price"] for r in fut["levels"] if r["source"] in ("swing", "equal")
             and r["side"] == "above")
      == sorted(float(s[k][-1]) for k in ("st_above1", "st_above2", "st_above3")
                if math.isfinite(s[k][-1])))

head("A TAKEN LEVEL SAYS WHEN, IN NEW YORK TIME")
ts = np.asarray(FUTURE.timestamp, dtype=np.int64)
start = levelview._today_start(s, len(ts) - 1)
found = 0
for r in fut["levels"]:
    if r["source"] != "prior_day" or r["name"] == "Yesterday's close":
        continue
    up = r["name"].endswith("high")
    hits = np.flatnonzero(np.asarray(FUTURE.high[start:] if up else FUTURE.low[start:])
                          > r["price"] if up else
                          np.asarray(FUTURE.low[start:]) < r["price"])
    if hits.size:
        found += 1
        day, _, hm = clock.label_ny(int(ts[start + hits[0]])).rpartition(" ")
        check("%s: taken at the first bar past it" % r["name"],
              r["taken"] and r["taken_at"] == "%s at %s" % (day, hm), (r, day, hm))
    else:
        check("%s: not taken, and says no time" % r["name"],
              not r["taken"] and r["taken_at"] == "")
check("the made-up future took at least one of yesterday's levels", found >= 1)
# A level set by hand: the first bar today above it is the one named.
probe = float(np.max(FUTURE.high[start:start + 40])) - 1e-6
j = start + int(np.flatnonzero(np.asarray(FUTURE.high[start:]) > probe)[0])
got = levelview._first_past(FUTURE, start, len(ts) - 1, probe, 1)
check("the first bar past a level is found exactly", got == j, (got, j))
day, _, hm = clock.label_ny(int(ts[j])).rpartition(" ")
check("and said as day and New York time",
      levelview._when(FUTURE, s, j, False) == "%s at %s" % (day, hm))
check("New York time, not UTC: 9:30 there is 14:30 UTC in November",
      clock.label_ny(clock.utc(2026, 11, 18, 14, 30)) == "18 Nov 09:30")

check("a close is reached, not taken or traded at",
      {r["taken_word"] for r in fut["levels"] if r["name"] == "Yesterday's close"} == {"Reached"})

head("WHILE THE MARKET TRADES, YESTERDAY IS THE LAST FINISHED DAY")


def live(bars, at, price):
    return dataclasses.replace(bars, live_price=price,
                               meta=dict(bars.meta, regularMarketTime=int(at)))


def prior_rows(out):
    return {r["name"]: r for r in out["levels"]}


prior = liquidity.SOURCES.index("prior_day")
taken = {"lq_take": [-1, 1, -1], "lq_take_src": [prior] * 3, "ck_day": [5, 6, 6]}
check("a high taken on an earlier day is that day's yesterday, not the reader's",
      [liquidity.level_name(taken, i) for i in range(3)]
      == ["the previous day's high", "yesterday's low", "yesterday's high"],
      [liquidity.level_name(taken, i) for i in range(3)])

# The scripted days end on a Tuesday; cut them so a week ends on the last bar too.
DAYS = [int(x) for x in clock.series(DAILY)["ck_day"]]
friday = max(i for i in range(len(DAYS) - 1) if (DAYS[i] - 1) % 7 == 4)
midweek = max(i for i in range(len(DAYS) - 1) if (DAYS[i] - 1) % 7 == 1)
for label, i in (("a Wednesday", midweek), ("a Monday", friday)):
    cut = clock.head(DAILY, i + 1)
    top = float(cut.high[-1])
    out = levelview.for_analysis(analysis(live(cut, int(DAILY.timestamp[i + 1]) + 5400,
                                               top + 1.0)), {})
    got = prior_rows(out)
    check("daily, %s: yesterday is the last finished bar" % label,
          (got["Yesterday's high"]["price"], got["Yesterday's low"]["price"],
           got["Yesterday's close"]["price"]) == (top, float(cut.low[-1]), float(cut.close[-1])),
          got["Yesterday's high"])
    check("daily, %s: taken is judged by the live price, and says so" % label,
          got["Yesterday's high"]["taken"]
          and got["Yesterday's high"]["taken_at"] == "by the latest price"
          and not got["Yesterday's low"]["taken"]
          and got["Yesterday's low"]["untaken"] == "not at the latest price")
    check("daily, %s: the chart's line is the same" % label,
          "Yest. high" in out["chart"]["wide"]
          and market_mod.format_price(top) in out["chart"]["wide"])
    check("daily, %s: nothing is said about today's opening gap" % label,
          not any(p["help"] == "opening_gap" for p in out["structure"]["points"]))
    begun = next(k for k in range(i, -1, -1) if (DAYS[k] - 1) // 7 != (DAYS[i] - 1) // 7) + 1
    if label == "a Monday":
        check("and last week is the week of that bar",
              got["Last week's high"]["price"] == float(np.max(cut.high[begun:]))
              and got["Last week's low"]["price"] == float(np.min(cut.low[begun:])))
    else:
        check("and last week is the one before that bar's",
              got["Last week's high"]["price"]
              == float(clock.series(cut)["ck_pwh"][-1]))
same = prior_rows(levelview.for_analysis(analysis(live(
    DAILY, int(DAILY.timestamp[-1]) + 23400, float(DAILY.close[-1]))), {}))
check("after the close, when today's bar is finished, it is the bar before again",
      same["Yesterday's high"]["price"] == float(DAILY.high[-2])
      and same["Yesterday's high"]["untaken"] == "no")
for name, bars, opened in (
        ("a stock's hourly", sc.walk(sc.stock_times(7 * 30, step=3600), seed=4, interval="1h",
                                     vol=0.004), int(sc.stock_times(7 * 31, step=3600)[-7])),
        ("a future's 5-minute", sc.walk(sc.futures_times(276 * 4), seed=4, kind="future",
                                        vol=0.0008, price=6000.0),
         int(sc.futures_times(276 * 4 + 1)[-1]))):
    table = clock.day_table(bars)
    out = levelview.for_analysis(analysis(live(bars, opened + 60, float(bars.close[-1]))), {},
                                 fetch=hourly_fetch, now=opened + 60)
    got = prior_rows(out)
    check("%s bars in a day's first bar: yesterday is the day that just ended" % name,
          got["Yesterday's high"]["price"] == table[-1].high
          and got["Yesterday's low"]["price"] == table[-1].low)
    check("%s: no level of that day is called today's" % name,
          not any(r["name"].startswith(("Today's", "Asia", "London", "New York before",
                                        "Opening range")) for r in out["levels"]),
          sorted(got))
    check("%s: nor its opening gap" % name,
          not any(p["help"] == "opening_gap" for p in out["structure"]["points"]))

head("A STOCK ON AN EXCHANGE ABROAD")
gmt = 9 * 3600
tts = np.array([clock.utc(2026, 11, 9) - gmt + d * 86400 + m * 60 for d in range(12)
                if d % 7 < 5 for m in list(range(540, 690, 5)) + list(range(750, 930, 5))])
TOKYO = dataclasses.replace(
    sc.walk(tts, seed=5, vol=0.001, price=3000.0, symbol="7203.T"), gmt_offset=gmt,
    session_id=(tts + gmt) // 86400,
    meta={"instrumentType": "EQUITY", "exchangeTimezoneName": "Asia/Tokyo"})
out = levelview.for_analysis(analysis(TOKYO), {}, fetch=broken_fetch,
                             now=clock.ny(2026, 11, 18, 9, 35))
got = prior_rows(out)
before = TOKYO.session_id == np.unique(TOKYO.session_id)[-2]
check("yesterday's levels are its own last session's",
      got["Yesterday's high"]["price"] == float(TOKYO.high[before].max())
      and got["Yesterday's low"]["price"] == float(TOKYO.low[before].min()))
said = " ".join(out["clock"]["words"] + out["notes"])
check("nothing is said of the US market, a US stock or today's New York window",
      "US stock" not in said and "does not trade New York's hours" in said
      and "This is 9:30" not in said, said)
check("and no New York opening range is listed",
      not any(r["source"] == "opening_range" for r in out["levels"]))

head("NOTES THAT BLAME NOBODY")
for kind, times in (("future", sc.futures_times(400, step=3600)),
                    ("coin", sc.coin_times(400, step=3600))):
    noon = [j for j in range(len(times)) if clock.fmt_ny(int(times[j])) == "12:00"][-1]
    hb = sc.walk(times[:noon + 1], seed=3, kind=kind, interval="1h", price=6000.0, vol=0.002)
    out = levelview.for_analysis(analysis(hb), {}, fetch=hourly_fetch, now=at_955)
    check("%s, hourly: no bar that long fits in 8:30 to 9:30, and none is said missing" % kind,
          not any("before-the-open" in note for note in out["notes"]), out["notes"])
gone = np.array([k for k in range(len(FUTURE.close))
                 if not 180 <= clock.ny_clock(int(FUTURE.timestamp[k]))[1] < 420])
out = levelview.for_analysis(analysis(sc.make(
    FUTURE.open[gone], FUTURE.high[gone], FUTURE.low[gone], FUTURE.close[gone],
    FUTURE.timestamp[gone], kind="future")), {}, fetch=hourly_fetch, now=at_955)
check("a session whose bars are missing is still said to be",
      any("London session's range is left out" in note for note in out["notes"]), out["notes"])

head("THE DAY'S LEAN")
levelview._FETCHED.clear()
bad = levelview.for_analysis(analysis(FUTURE), {}, fetch=broken_fetch, now=at_955)
check("a fetch that fails gives one plain sentence",
      bad["bias"] == {"unavailable": levelview.UNAVAILABLE}, bad["bias"])
check("which says what is missing and never an error's text",
      "could not be fetched" in levelview.UNAVAILABLE and "broke" not in json.dumps(bad["bias"]))
check("and the rest of the section is still there", bad["structure"] and bad["levels"])
check("the pair check stays silent when its bars cannot be had", bad["pair"] is None)
b = fut["bias"]
check("with hourly bars, a lean in words", b.get("word") in ("Leans up", "Leans down", "Unclear")
      and b.get("reasons"), b)
check("the scripted rise leans up", b.get("lean") == 1, b.get("lean"))
check("with no database, it says the read is not measured, not tested",
      "not been measured" in " ".join(b["record_words"]))
against = levelview._bias(sc.hourly_path(sc.BIAS_TAKEN_THEN_GAP), None, None)
check("a lean against the hourly trend is not said to turn that trend",
      against["lean"] == 1 and "would go against this lean" in against["flip_words"]
      and "turn the hourly trend" not in against["flip_words"], against["flip_words"])
check("a lean with it is", "would turn the hourly trend down" in b["flip_words"], b["flip_words"])
two_years = sc.walk(sc.coin_times(6000, step=3600), seed=6, kind="coin", interval="1h",
                    vol=0.004, price=90000.0)
reads = []
real_read = bias_mod.read
bias_mod.read = lambda h, *a, **k: (reads.append(len(h.close)), real_read(h, *a, **k))[1]
try:
    short = levelview._bias(two_years, None, None)
finally:
    bias_mod.read = real_read
whole_read = bias_mod.read(two_years)
check("an hourly analysis's lean is read from its last 1,500 bars", reads == [1500], reads)
check("and is the read of all of them",
      (short["lean"], short["reasons"], short["flip_level"])
      == (whole_read.lean, whole_read.reasons, whole_read.flip_level))


class Kept(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


kept = Kept()
levelview.log.addHandler(kept)
levelview.log.propagate = False
bias_mod.read = lambda *a, **k: 1 / 0
try:
    odd = levelview._bias(HOURLY, None, None)
finally:
    bias_mod.read = real_read
    levelview.log.removeHandler(kept)
    levelview.log.propagate = True
check("a fault in the read is not called a failed download, and is logged",
      odd == {"unavailable": levelview.NOT_READ} and "fetched" not in levelview.NOT_READ
      and len(kept.lines) == 1, (odd, kept.lines))
conn = db.connect(DB)
labstore.save_bias(conn, "ES=F", labstore.sample_bias())
said = levelview.bias_record(conn, "ES=F")
words = " ".join(said["record_words"])
check("a stored record says its counts", "312 past trading days" in words
      and "leaned one way on 190" in words and "right on 101 (53%" in words, words)
check("with its 95% range", re.search(r"95% range of \d+% to \d+%", words) is not None, words)
check("beside always guessing up", "Always guessing up was right on 162 of the 312 days (52%)"
      in words, words)
check("and a coin flip", "a coin flip is right on half" in words)
check("and when those reads were made", "once a day, at 9:00 New York time (9:30 for a stock)"
      in words and "later in the day was not measured" in words, words)
check("and a conclusion from the one rule", said["record"]["verdict"] == labstore.rate_verdict(
    101, 190, 162, 312) and "cannot be told apart from always guessing up" in words, words)
pooled = levelview.bias_record(conn, "NQ=F")
check("an instrument not studied gets nothing until the pooled record exists",
      pooled["record"] is None and not pooled["pooled"])
labstore.save_bias(conn, labstore.POOL_ALL, labstore.sample_bias())
pooled = levelview.bias_record(conn, "NQ=F")
check("then the pooled record, said to be pooled", pooled["pooled"]
      and "Across every instrument studied (this one was not)" in pooled["record_words"][0])
conn.close()

head("THE NEW YORK CLOCK")
strip = levelview.clock_strip(FUTURE, "future", clock.ny(2026, 11, 18, 9, 55))
text = " ".join(strip["words"])
check("at 9:55 it names the morning session", "the New York morning" in text, text)
check("and the 9:50 to 10:10 window", "9:50 to 10:10" in text and strip["window"] == "macro")
strip = levelview.clock_strip(FUTURE, "future", clock.ny(2026, 11, 18, 10, 40))
text = " ".join(strip["words"])
check("at 10:40 it says the course's 10:30 cut-off has passed",
      "10:30 cut-off has passed" in text, text)
strip = levelview.clock_strip(FUTURE, "future", clock.ny(2026, 11, 21, 12, 0))
check("on a Saturday the futures are shut", "shut for the weekend" in " ".join(strip["words"]))
for label, bars, kind, at in (
        ("a future on a Saturday", FUTURE, "future", clock.ny(2026, 11, 21, 9, 55)),
        ("a stock on a Saturday", STOCK, "stock", clock.ny(2026, 11, 21, 9, 55)),
        ("a stock on Thanksgiving", STOCK, "stock", clock.ny(2026, 11, 26, 9, 40))):
    text = " ".join(levelview.clock_strip(bars, kind, at)["words"])
    check("%s: shut, and no window is said to be running" % label,
          "This is 9:" not in text and "The course watches 9:30 to 9:50" in text, text)
check("hours are written one way in sentences", not any(
    re.search(r"\b0\d:", w) for w in levelview._LAB_WHEN.values()))
strip = levelview.clock_strip(STOCK, "stock", clock.ny(2026, 11, 18, 7, 0))
check("before 9:30 a stock's market is shut, and the course's times are said",
      "shut now" in " ".join(strip["words"]) and "9:50 to 10:10" in " ".join(strip["words"]))
strip = levelview.clock_strip(FUTURE, "future", clock.ny(2026, 7, 14, 4, 0))
check("in summer too, New York time: 4:00 is the London session",
      "the London session" in " ".join(strip["words"]), strip["words"])
conn = db.connect(DB)
labstore.save_result(conn, labstore.sample_result("ES=F", "5m"))
labstore.save_claim(conn, labstore.sample_claim("macro_window"))
strip = levelview.clock_strip(FUTURE, "future", clock.ny(2026, 11, 18, 9, 55), conn)
conn.close()
measured = " ".join(strip["measured"])
check("with the lab's trades entered in this window, as a count", "5 sweep, break and retest "
      "trades on ES=F's 5-minute bars were entered from 9:50 to 10:10" in measured
      and "too few to say anything" in measured, measured)
conn = db.connect(DB)
many = labstore.sample_result("ES=F", "5m")
many["setups"]["liquidity_model"]["rules"]["2r"]["by_window"]["macro"] = [40, 17, -3.0]
labstore.save_result(conn, many)
often = " ".join(levelview.clock_strip(FUTURE, "future", clock.ny(2026, 11, 18, 9, 55),
                                       conn)["measured"])
labstore.save_result(conn, labstore.sample_result("ES=F", "5m"))
conn.close()
check("the trades that kept money are called that, not trades that reached their target",
      "17 kept money after costs" in often and "reached their target" not in often, often)
check("and the lab's test of the window, in the claim's words",
      labstore.claim_words(0) in measured, measured)

head("THE PAIR CHECK")
check("ES=F is compared with NQ=F", fut["pair"] and fut["pair"]["partner"] == "NQ=F")
check("in words about their last swing highs and lows",
      fut["pair"] and all(re.match(r"At their last two swing (high|low)s, ES=F made an? "
                                   r"(higher|lower|equal) (high|low) and NQ=F made", row)
                          for row in fut["pair"]["rows"]), fut["pair"])
check("a stock with no partner gets none", stock["pair"] is None)
asked = []
real_fetch = market_mod.fetch_bars
market_mod.fetch_bars = lambda s_, iv, lookback=None: (asked.append((iv, lookback)), NQ)[1]
try:
    for iv in ("5m", "15m", "30m", "1h"):
        levelview._FETCHED.clear()
        levelview._fetched("QQQ", iv, None)
finally:
    market_mod.fetch_bars = real_fetch
    levelview._FETCHED.clear()
check("a partner's bars are asked for no further back than the check compares",
      asked == [("5m", "5d"), ("15m", "1mo"), ("30m", None), ("1h", "60d")], asked)

head("NOTHING PRINTS AS NONE OR NAN")
blob = json.dumps({k: v for k, v in fut.items() if k != "chart"}, default=str)
check("no None, nan or inf in any sentence",
      not re.search(r"\bNone\b|\bnan\b|\binf\b", " ".join(
          p["text"] for p in fut["structure"]["points"]) + blob.replace("null", "")), blob[:400])

head("THE PAGE")
real = {"resolve": engine.resolve, "bars": engine.fetch_bars, "market": market_mod.fetch_bars,
        "calendar": catalysts.fetch, "news": sentiment_mod.gather_news,
        "social": sentiment_mod.gather_social}
SOURCE = {"ES=F": FUTURE, "TEST": DAILY}
engine.resolve = lambda s_: {"symbol": s_, "name": "Made-up " + s_}
engine.fetch_bars = lambda s_, iv: SOURCE[s_]
market_mod.fetch_bars = lambda s_, iv, lookback=None: HOURLY if iv == "1h" else NQ


def no_calendar(symbol):
    raise RuntimeError("no calendar in tests")


catalysts.fetch = no_calendar
sentiment_mod.gather_news = lambda symbol, name="": []
sentiment_mod.gather_social = lambda symbol: []
APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 OrenthApp/1")
try:
    levelview._FETCHED.clear()
    r = c.get("/analyse?symbol=ES=F&interval=5m&news=0")
    page = r.data.decode("utf-8", "replace")
    check("an intraday future's analysis draws", r.status_code == 200, r.status_code)
    at = page.find('<section class="pricedoing"')
    section = page[at:page.find("</section>", at)]
    check("with the section, after the chart and before the odds",
          0 < page.find('class="tradechart"') < at < page.find("<h2>The odds, measured</h2>"))
    for want in ("What price is doing", "New York clock", "The day's lean, read the "
                 "course's way", "All key levels (", "Show key levels on the chart",
                 "ES=F beside NQ=F", "How this read did before."):
        check("it shows %s" % want, want in section)
    check("the price chart says its times are New York time",
          "Times are New York time." in page and "Times are UTC" not in page)
    check("and names its candles in New York time",
          clock.label_ny(int(FUTURE.timestamp[-1])) + "  open" in page)
    check("no None, nan or undefined",
          not re.search(r">\s*None\s*<|\bnan\b|undefined|\bNone\b", section),
          re.findall(r".{60}(?:None|nan|undefined).{60}", section)[:2])
    template = open(_os.path.join(harness.ROOT, "templates", "analysis.html"),
                    encoding="utf-8").read()
    used = set(re.findall(r"\bhelp\(\s*'(\w+)'\s*\)", template))
    for group in re.findall(r"\bhelp_list\(\s*\[([^\]]*)\]", template):
        used |= set(re.findall(r"'(\w+)'", group))
    new_keys = ("structure", "flip_level", "sweep", "fvg", "ifvg", "half_line",
                "opening_gap", "draw", "prior_day", "typical_bar", "session_ny",
                "window_ny", "bias", "pair_check")
    check("every new help key is used by the page and has words",
          set(new_keys) <= used and set(new_keys) <= set(plain.HELP),
          sorted(set(new_keys) - used))
    shown = section.replace("&#39;", "'")
    for key in ("structure", "flip_level", "sweep", "draw", "prior_day", "typical_bar",
                "session_ny", "window_ny", "bias", "pair_check"):
        check("the %s help's words are on the page" % key,
              plain.HELP[key]["text"][:40] in shown, key)
    toggles = re.findall(r'<details class="help(?: list)?">', section)
    check("a good number of question marks in the section", len(toggles) >= 8, len(toggles))
    check("the video is named and linked outside the app",
          'href="https://youtu.be/yiuFUp0kFz8"' in section)

    app_page = c.get("/analyse?symbol=ES=F&interval=5m&news=0",
                     headers={"User-Agent": APP_UA}).data.decode("utf-8", "replace")
    at = app_page.find('<section class="pricedoing"')
    app_section = app_page[at:app_page.find("</section>", at)]
    check("inside the iPhone app it is named and not linked",
          "How To Start Day Trading In 2026" in app_section and "youtu" not in app_section
          and not re.search(r'href="https?://', app_section))

    r = c.get("/analyse?symbol=TEST&interval=1d&news=0")
    page = r.data.decode("utf-8", "replace")
    at = page.find('<section class="pricedoing"')
    section = page[at:page.find("</section>", at)]
    check("a daily analysis draws", r.status_code == 200)
    check("with structure and levels", at > 0 and "All key levels (" in section
          and "Structure" in section)
    check("and no clock or lean", "New York clock" not in section
          and "The day&#39;s lean" not in section)
    check("its price chart keeps dates, with no time zone to state",
          "Times are New York time." not in page[:page.find('class="pricedoing"')])
finally:
    engine.resolve, engine.fetch_bars = real["resolve"], real["bars"]
    market_mod.fetch_bars = real["market"]
    catalysts.fetch = real["calendar"]
    sentiment_mod.gather_news, sentiment_mod.gather_social = real["news"], real["social"]

print()
print("%d failure(s)" % len(fails))
print("LEVELVIEW OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
