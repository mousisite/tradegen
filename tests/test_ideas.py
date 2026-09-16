"""The idea finder: what it shows, and more importantly what it refuses to."""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

from bot import ideas

c, DB = harness.isolated_web("ideas")

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def row(**kw):
    base = {"symbol": "TEST", "action": "BUY", "expectancy": 0.1,
            "samples": 100, "conviction": 0.5, "probability": 0.4}
    base.update(kw)
    return base


print("=" * 72)
print("THE BAR")
print("=" * 72)

check("a firing setup with a measured edge passes", ideas._worth_showing(row()))
check("an AVOID never passes", not ideas._worth_showing(row(action="AVOID")))
check("a negative edge never passes",
      not ideas._worth_showing(row(expectancy=-0.05)))
check("a zero edge never passes", not ideas._worth_showing(row(expectancy=0.0)))
check("an unmeasured edge never passes",
      not ideas._worth_showing(row(expectancy=None)))
check("too little history never passes", not ideas._worth_showing(row(samples=12)))
check("no history at all never passes", not ideas._worth_showing(row(samples=0)))
check("a WAIT can pass", ideas._worth_showing(row(action="WAIT")))
check("a SHORT can pass", ideas._worth_showing(row(action="SHORT")))

print()
print("=" * 72)
print("WHAT GETS SCANNED")
print("=" * 72)

crypto = ideas._universe("crypto", 10)
check("crypto returns coins", all(s.endswith("-USD") for s in crypto), crypto[:3])
check("and respects the limit", len(crypto) == 10, len(crypto))

both = ideas._universe("both", 12)
check("both mixes coins and shares",
      any(s.endswith("-USD") for s in both)
      and any(not s.endswith("-USD") for s in both), both)
check("and still respects the limit", len(both) <= 12, len(both))

stocks = ideas._universe("stocks", 10)
check("stocks returns no coins", not any(s.endswith("-USD") for s in stocks), stocks)
check("and no foreign or warrant tickers",
      all(s.isalpha() and len(s) <= 5 for s in stocks), stocks)

print()
print("=" * 72)
print("A REAL SCAN")
print("=" * 72)

found = ideas.find("both", "medium", limit=12)
print("   %s" % found["summary"][:170])
print("   scanned %d, ready %d, watching %d, avoided %d, in %.0fs"
      % (found["scanned"], len(found["passed"]), len(found["watch"]),
         found["avoided"], found["seconds"]))

check("something was actually looked at", found["scanned"] > 0)
check("every ready row really cleared the bar",
      all(ideas._worth_showing(r) for r in found["passed"]))
check("ready rows are ordered best first",
      all(found["passed"][i]["expectancy"] >= found["passed"][i + 1]["expectancy"]
          for i in range(len(found["passed"]) - 1)))
check("nothing appears in both lists",
      not ({r["symbol"] for r in found["passed"]}
           & {r["symbol"] for r in found["watch"]}))
check("every watch row has a measured edge",
      all((r["expectancy"] or 0) > 0 and r["samples"] >= 30
          for r in found["watch"]))
check("an empty ready list is explained rather than padded",
      bool(found["passed"]) or "Nothing to act on" in found["summary"],
      found["summary"][:80])
check("the summary never promises a return",
      not any(w in found["summary"].lower()
              for w in ("guarantee", "will make", "profit you", "sure thing")))

short = ideas.find("crypto", "short", limit=6)
check("a short horizon uses intraday bars", short["interval"] == "1h",
      short["interval"])
check("and warns about trading costs", "costs" in (short["warning"] or "").lower())
check("a medium horizon uses daily bars",
      ideas.HORIZONS["medium"]["interval"] == "1d")

junk = ideas.find("nonsense", "nonsense", limit=4)
check("an unknown market falls back rather than failing",
      junk["market"] == "stocks" and junk["horizon"] == "medium")

print()
print("=" * 72)
print("THE PAGE")
print("=" * 72)

check("the form loads with no scan", c.get("/ideas").status_code == 200)
body = c.get("/ideas?market=crypto&horizon=medium").data.decode("utf-8", "replace")
check("a scan renders", "What should I look at" in body)
check("it says plainly what it found", 'class="plainbox"' in body)
check("it admits how many it skipped", "had no setup" in body or "no setup at all" in body)
check("it says an empty list is the app working",
      "not failing" in body)

import sys as _exit_sys
print()
print("%d failure(s)" % len(fails))
print("IDEAS OK" if not fails else "FAILURES: %s" % fails)
_exit_sys.exit(1 if fails else 0)
