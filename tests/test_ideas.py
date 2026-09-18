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


print()
print("=" * 72)
print("IT MUST NOT RECOMMEND SOMETHING NOBODY ASKED FOR")
print("=" * 72)

# The resolver falls back to a fuzzy search for a dead ticker, which is right
# when a person mistypes and wrong here: a scan that silently substitutes is
# recommending an instrument nothing vetted. APT-USD is dead on the source and
# used to come back as STAPT-USD.
from bot import config as config_mod

_cfg = config_mod.load()
check("a dead ticker returns nothing rather than a substitute",
      ideas._look_at("APT-USD", "1d", _cfg) is None)
check("a nonsense ticker returns nothing",
      ideas._look_at("ZZQQ99XX", "1d", _cfg) is None)

real = ideas._look_at("AAPL", "1d", _cfg)
check("a real ticker still works", real is not None and real["symbol"] == "AAPL")

everything = ideas._universe("both", 60)
check("no dead tickers are carried in the list", "APT-USD" not in everything)

scan = ideas.find("crypto", "medium", _cfg, limit=10)
asked = set(ideas._universe("crypto", 10))
returned = {r["symbol"] for r in scan["passed"] + scan["watch"]}
check("every name shown was a name asked about",
      returned <= asked, sorted(returned - asked))

print()
print("=" * 72)
print("MONTHS MUST NOT BE A COPY OF WEEKS")
print("=" * 72)

check("the long horizon is marked as needing sound accounts",
      ideas.HORIZONS["long"].get("needs_sound_accounts") is True)
check("the medium horizon is not",
      not ideas.HORIZONS["medium"].get("needs_sound_accounts"))

long_scan = ideas.find("stocks", "long", _cfg, limit=16)
print("   long: ready %d, left out %d"
      % (len(long_scan["passed"]), len(long_scan.get("dropped") or [])))
check("the long scan reports what it left out", "dropped" in long_scan)
check("everything kept carries an accounts verdict",
      all(r.get("accounts") in ("sound", "not filed") for r in long_scan["passed"]),
      [r.get("accounts") for r in long_scan["passed"]])
check("everything left out carries a reason",
      all(r.get("dropped_because") for r in (long_scan.get("dropped") or [])))
check("a coin is never kept for a months-long hold on its accounts",
      not any(r.get("asset_class") == "crypto" and r.get("accounts") == "sound"
              for r in long_scan["passed"]))

print()
print("=" * 72)
print("RISK APPETITE")
print("=" * 72)

# An appetite narrows what cleared the bar. It must never lower the bar, and
# "high risk, high reward" must never become "show me the ones that lost".
spec_wild = ideas.RISKS["wild"]
spec_steady = ideas.RISKS["steady"]

check("three appetites are offered",
      set(ideas.RISKS) == {"steady", "balanced", "wild"}, list(ideas.RISKS))

losing = row(symbol="LOSER", expectancy=-0.5, gap_multiple=9.0, probability=0.1)
check("a losing setup is refused however hungry for risk you are",
      not ideas._worth_showing(losing))

# Reward-to-risk is not an axis: every plan is built to the same ratio from the
# settings, so filtering on it would sort by the user's own configuration.
check("no appetite filters on reward-to-risk",
      not any("reward_risk" in str(v) for v in ideas.RISKS.values()))

check("high risk demands measured gap risk, not a blank",
      not ideas._matches_risk(row(gap_multiple=None), spec_wild))
check("and refuses a calm instrument",
      not ideas._matches_risk(row(gap_multiple=1.2), spec_wild))
check("but accepts a violent one",
      ideas._matches_risk(row(gap_multiple=4.0), spec_wild))

check("steady refuses a violent instrument",
      not ideas._matches_risk(row(gap_multiple=4.0, probability=0.9),
                              spec_steady))
check("steady refuses a setup that rarely works",
      not ideas._matches_risk(row(gap_multiple=1.2, probability=0.05),
                              spec_steady))
check("steady accepts a calm one that usually works",
      ideas._matches_risk(row(gap_multiple=1.2, probability=0.45), spec_steady))

# An unknown figure is not evidence of safety either, but it is not counted
# against an instrument the way a measured bad figure is.
check("an unmeasured gap does not disqualify a steady pick",
      ideas._matches_risk(row(gap_multiple=None, probability=0.45),
                          spec_steady))

scans = {name: ideas.find(market="stocks", horizon="medium", risk=name)
         for name in ("steady", "balanced", "wild")}

for name, scan in scans.items():
    shown = scan["passed"]
    check("%s ranks its list 1..N with no gaps" % name,
          [r["rank"] for r in shown] == list(range(1, len(shown) + 1)),
          [r.get("rank") for r in shown])
    check("%s shows nothing that lost money after costs" % name,
          all((r.get("expectancy") or 0) > 0 for r in shown))
    check("%s shows nothing measured on too little history" % name,
          all((r.get("samples") or 0) >= 30 for r in shown))

wild_gaps = [r["gap_multiple"] for r in scans["wild"]["passed"]
             if r.get("gap_multiple")]
steady_gaps = [r["gap_multiple"] for r in scans["steady"]["passed"]
               if r.get("gap_multiple")]
if wild_gaps and steady_gaps:
    check("the risky list really is riskier than the calm one",
          min(wild_gaps) > max(steady_gaps),
          "wild %s vs steady %s" % (min(wild_gaps), max(steady_gaps)))

check("an appetite never shows more than balanced does",
      len(scans["wild"]["passed"]) <= len(scans["balanced"]["passed"])
      and len(scans["steady"]["passed"]) <= len(scans["balanced"]["passed"]))
check("and the page is told how many it set aside",
      scans["wild"]["wrong_shape"] >= 0
      and scans["balanced"]["wrong_shape"] == 0)

check("an unknown appetite falls back rather than failing",
      ideas.find(market="stocks", horizon="medium",
                 risk="nonsense")["risk"] == "balanced")

import sys as _exit_sys
print()
print("%d failure(s)" % len(fails))
print("IDEAS OK" if not fails else "FAILURES: %s" % fails)
_exit_sys.exit(1 if fails else 0)
