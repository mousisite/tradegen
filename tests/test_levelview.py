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
import json
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
              "source"}
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
check("and the lab's test of the window, in the claim's words",
      labstore.claim_words(0) in measured, measured)

head("THE PAIR CHECK")
check("ES=F is compared with NQ=F", fut["pair"] and fut["pair"]["partner"] == "NQ=F")
check("in words about their last swing highs and lows",
      fut["pair"] and all(re.match(r"At their last two swing (high|low)s, ES=F made an? "
                                   r"(higher|lower|equal) (high|low) and NQ=F made", row)
                          for row in fut["pair"]["rows"]), fut["pair"])
check("a stock with no partner gets none", stock["pair"] is None)

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
