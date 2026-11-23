"""The idea finder: what it shows, and more importantly what it refuses to."""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

from bot import ideas
import time as _time

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
# "Both" is the stocks list and the coin list together, each to the limit,
# taken in turn so a scan that runs short still reaches the coins.
check("and takes each list to the limit, coins from the start",
      len(both) <= 24 and any(s.endswith("-USD") for s in both[:2]), both)

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
# Finished here, however long it takes, so what is checked below is a finished
# page. The page of a scan still running has stubbed checks of its own.
ideas.find("crypto", "medium", wait=ideas.SCAN_WAIT)
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
      all(r.get("accounts") in ("sound", "not filed", "unchecked", "unreadable")
          for r in long_scan["passed"]),
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

print()
print("=" * 72)
print("ONE SCAN, SHARED, INSIDE THE TIME A PAGE IS ALLOWED")
print("=" * 72)

# The slow part depends only on the market and the bar interval, so switching
# appetite must re-filter an existing scan rather than run another one. Before
# this, each of the three appetites ran its own thirty-to-forty-second scan.
ideas.find("stocks", "medium", risk="balanced")
_t = _time.time()
ideas.find("stocks", "medium", risk="wild")
_switch = _time.time() - _t
check("changing appetite re-filters the same scan instead of rerunning it",
      _switch < 2.0, "%.1fs" % _switch)

# Shared rows mean shared dicts, and the months view writes on them: which
# ones had sound accounts, which were dropped and why. None of that may turn
# up in somebody else's weeks view.
ideas.find("stocks", "long", risk="balanced")
_weeks = ideas.find("stocks", "medium", risk="balanced")
_leaked = [r["symbol"] for r in _weeks["passed"] + _weeks["watch"]
           if "dropped_because" in r or "accounts" in r]
check("a mark written for one view never appears in another",
      not _leaked, _leaked)

# Cloudflare cuts a request off at 100 seconds and shows its own error page.
# The scan stops short of that and says so rather than failing. The rows kept
# from the scans above would answer in no time at all, so they are cleared:
# this is about a scan that has real work left when the time runs out.
ideas._ROWS.clear()
_real_budget = ideas.SCAN_BUDGET
ideas.SCAN_BUDGET = 0.001
try:
    _rushed = ideas.find("stocks", "medium", limit=23)  # its own cache key
finally:
    ideas.SCAN_BUDGET = _real_budget
check("a scan that runs out of time says it was cut short",
      _rushed["cut_short"])
check("and admits how much it did not look at",
      _rushed["scanned"] < _rushed["asked_for"]
      and "stopped after" in _rushed["summary"], _rushed["summary"])

# Filings that did not answer in time are unknown, not missing. Calling them
# "no filings" would be a false statement about the company.
_real_hold = ideas._accounts_hold_up
_real_acc = ideas.ACCOUNTS_BUDGET


def _slow_hold(symbol):
    _time.sleep(2)
    return {"ok": True, "reasons": [], "strength": 7}


ideas._accounts_hold_up = _slow_hold
ideas.ACCOUNTS_BUDGET = 0.2
try:
    _started = _time.time()
    _got = ideas._check_accounts(["AAA", "BBB", "AAA"])
    _took = _time.time() - _started
finally:
    ideas._accounts_hold_up = _real_hold
    ideas.ACCOUNTS_BUDGET = _real_acc
check("filings that miss the budget are left out rather than guessed",
      _got == {}, _got)
check("and the page does not wait for them", _took < 1.0, "%.1fs" % _took)

_real_check = ideas._check_accounts
ideas._check_accounts = lambda symbols: {}
try:
    _fake = {"market": "stocks", "interval": "1d", "asked_for": 1,
             "cut_short": False, "seconds": 1.0, "scanned_at": _time.time(),
             "rows": [row(symbol="SLOW", asset_class="stock", gap_multiple=1.2)]}
    _listed = ideas._shortlist(_fake, "long", "balanced")
finally:
    ideas._check_accounts = _real_check
check("an unanswered filing is labelled unchecked, not dropped",
      [r.get("accounts") for r in _listed["passed"]] == ["unchecked"],
      [r.get("accounts") for r in _listed["passed"]])

print()
print("=" * 72)
print("A SCAN RUNS IN THE BACKGROUND, AND A PART-DONE ONE SAYS SO")
print("=" * 72)
import threading as _threading
from bot import upstream as _upstream

# The real scans above may have tripped the data source's breaker, and a scan
# stops early while it is open. What follows is stubbed and must not see it.
_upstream.reset()
_real_universe, _real_look = ideas._universe, ideas._look_at
_gate = _threading.Event()
_asked = []


def _fake_universe(market, limit):
    return ["S%02d" % i for i in range(10)]


def _fake_look(symbol, interval, cfg):
    _asked.append(symbol)
    if symbol >= "S04":
        _gate.wait(10)                       # the rest wait until released
    if symbol in ("S03", "S07"):
        return None                          # the data source gave nothing
    return row(symbol=symbol, asset_class="crypto", gap_multiple=1.2,
               expectancy=0.3 - int(symbol[1:]) * 0.01, price=1.0)


ideas._universe, ideas._look_at = _fake_universe, _fake_look
_cfg_bg = {"background-test": 1}
try:
    _part = ideas.find("crypto", "medium", _cfg_bg, "balanced", wait=1.0)
    check("a scan that is not finished comes back at once, marked running",
          _part["running"] and _part["partial"] and not _part["complete"], _part.get("summary"))
    check("it says how far it has got",
          _part["looked_at"] == 4 and _part["asked_for_now"] == 10
          and "Still checking: 4 of 10" in _part["summary"], _part["summary"])
    check("and ranks nothing, because part of a list is not the list",
          all(r["rank"] is None for r in _part["passed"]))
    _again = ideas.find("crypto", "medium", _cfg_bg, "wild", wait=0)
    check("a second person asking joins the same scan rather than starting another",
          _asked.count("S00") == 1 and _again["running"], _asked)
    _gate.set()
    _done = ideas.find("crypto", "medium", _cfg_bg, "balanced", wait=10)
    check("once finished it is complete and ranked",
          _done["complete"] and not _done["running"]
          and [r["rank"] for r in _done["passed"]] == list(range(1, len(_done["passed"]) + 1)))
    check("every instrument is accounted for: read, or could not be read",
          _done["scanned"] + len(_done["failed"]) == _done["asked_for"] == 10
          and _done["failed"] == ["S03", "S07"], (_done["scanned"], _done["failed"]))
    check("and the summary names how many could not be read",
          "2 could not be read" in _done["summary"], _done["summary"])
    _asked.clear()
    _gate.clear()
    _stale = ideas.find("crypto", "medium", _cfg_bg, "balanced", wait=0.5, ttl=0)
    check("an old scan is shown with its age while a fresh one runs",
          _stale["running"] and _stale.get("refreshing") and _stale["complete"]
          and len(_stale["passed"]) == len(_done["passed"]))
    _gate.set()
    ideas.find("crypto", "medium", _cfg_bg, "balanced", wait=10)

    def _boom(market, limit):
        raise RuntimeError("no list today")
    ideas._universe = _boom
    try:
        ideas.find("stocks", "medium", _cfg_bg, "balanced", wait=5)
        _raised = False
    except RuntimeError as exc:
        _raised = "no list today" in str(exc)
    check("a scan that fails says so instead of showing an empty list", _raised)

    ideas._universe = _fake_universe
    _gate.clear()
    import web as _web
    _web_cfg = _web._cfg
    _web._cfg = lambda: {"background-page": 1}
    _real_wait = ideas.PAGE_WAIT
    ideas.PAGE_WAIT = 0.5
    try:
        _page = c.get("/ideas?market=crypto&horizon=medium&risk=balanced").data.decode("utf-8", "replace")
    finally:
        _web._cfg = _web_cfg
        ideas.PAGE_WAIT = _real_wait
        _gate.set()
    check("the page shows progress and updates itself while the scan runs",
          "Still checking" in _page and 'http-equiv="refresh"' in _page
          and "not ranked" in _page.lower(), _page[:0])
finally:
    _gate.set()
    ideas._universe, ideas._look_at = _real_universe, _real_look

# Everything from here is stubbed: no request leaves the machine.
import web as _web
from bot import market as _market
from bot import screener as _screener
from bot import yahoo as _yahoo

_web_cfg = _web._cfg


def _names(rows):
    return {r["symbol"] for r in rows}


print()
print("=" * 72)
print("A SCAN THE DATA SOURCE REFUSED IS NEVER THE ANSWER")
print("=" * 72)


def _scan_of(n_rows, n_failed, asked_for=None, **extra):
    made = {"rows": [row(symbol="R%03d" % i) for i in range(n_rows)],
            "failed": ["F%03d" % i for i in range(n_failed)],
            "asked_for": n_rows + n_failed if asked_for is None else asked_for,
            "cut_short": False, "throttled": False}
    made.update(extra)
    return made


check("a scan missing a couple of names is still complete",
      ideas._is_complete(_scan_of(148, 2)))
check("one the source refused most of is not",
      not ideas._is_complete(_scan_of(40, 110)))
check("nor one that ran out of time",
      not ideas._is_complete(_scan_of(150, 0, cut_short=True)))
check("nor one stopped because the source started refusing",
      not ideas._is_complete(_scan_of(40, 1, asked_for=150, throttled=True)))
check("nor one that read nothing at all", not ideas._is_complete(_scan_of(0, 3)))

_upstream.reset()
_reads = []


def _refusing_look(symbol, interval, cfg):
    _reads.append(symbol)
    if len(_reads) > 20:
        # What the breaker does after a run of refusals: every request from
        # here on is refused before it is sent.
        _upstream._breakers["Yahoo Finance"] = {
            "failures": 0, "open_until": _time.time() + 60, "opened": 1}
        return None
    return row(symbol=symbol, asset_class="equity", gap_multiple=1.2, price=1.0)


ideas._universe = lambda market, limit: ["T%03d" % i for i in range(150)]
ideas._look_at = _refusing_look
try:
    _thr = ideas._scan("stocks", "1d", {"throttle-test": 1}, 150)
finally:
    ideas._universe, ideas._look_at = _real_universe, _real_look
    _upstream.reset()
check("a scan stops once the source starts refusing, rather than asking 130 more times",
      _thr["throttled"] and len(_reads) < 30, (len(_reads), _thr["throttled"]))
check("and is not complete", not _thr["complete"])
_thr_list = ideas._shortlist(_thr, "medium", "balanced")
check("so nothing in it is ranked",
      _thr_list["passed"] and all(r["rank"] is None for r in _thr_list["passed"]))
check("and it says the source started refusing, and how far it got",
      "started refusing" in _thr_list["summary"]
      and "of the 150" in _thr_list["summary"]
      and "nothing here is ranked" in _thr_list["summary"], _thr_list["summary"])

_state = {"refuse": False, "reads": 0}


def _flaky_look(symbol, interval, cfg):
    _state["reads"] += 1
    if _state["refuse"] and int(symbol[1:]) >= 2:
        return None
    return row(symbol=symbol, asset_class="crypto", gap_multiple=1.2, price=1.0,
               expectancy=0.3 - int(symbol[1:]) * 0.01)


ideas._universe = lambda market, limit: ["U%02d" % i for i in range(20)]
ideas._look_at = _flaky_look
_cfg_rf = {"refusal-test": 1}
try:
    _good = ideas.find("crypto", "medium", _cfg_rf, wait=10)
    _state["refuse"] = True
    _after = ideas.find("crypto", "medium", _cfg_rf, wait=10, ttl=0)
    _reads_then = _state["reads"]
    _again = ideas.find("crypto", "medium", _cfg_rf, wait=10, ttl=0)
    _web._cfg = lambda: _cfg_rf
    _page_rf = c.get("/ideas?market=crypto&horizon=medium&risk=balanced"
                     ).data.decode("utf-8", "replace")
    _reads_now = _state["reads"]
    ideas._universe = lambda market, limit: ["V%02d" % i for i in range(20)]
    _first = ideas.find("crypto", "medium", {"first-refused": 1}, wait=10)
    _web._cfg = lambda: {"first-refused": 1}
    _page_first = c.get("/ideas?market=crypto&horizon=medium&risk=balanced"
                        ).data.decode("utf-8", "replace")
finally:
    _web._cfg = _web_cfg
    ideas._universe, ideas._look_at = _real_universe, _real_look
check("a good scan is complete and ranked",
      _good["complete"] and [r["rank"] for r in _good["passed"]]
      == list(range(1, 21)))
check("a refresh the source mostly refused does not replace it",
      _after["complete"] and not _after["running"]
      and [r["rank"] for r in _after["passed"]] == list(range(1, 21)),
      (_after["complete"], len(_after["passed"])))
check("and says why the list was not refreshed",
      "could read only 2 of the 20" in (_after.get("refresh_note") or ""),
      _after.get("refresh_note"))
check("nor is the source asked again straight away",
      _reads_now == _reads_then and _again.get("refresh_note"),
      (_reads_now, _reads_then))
check("the page shows the last complete scan, ranked, and why",
      "Not refreshed." in _page_rf and "Ready now" in _page_rf, _page_rf[:0])
check("a first scan the source refused is shown as the part it is",
      not _first["complete"] and not _first["running"]
      and _first["passed"] and all(r["rank"] is None for r in _first["passed"]),
      (_first["complete"], [r["rank"] for r in _first["passed"]]))
check("and its page never calls that the app working",
      "not failing" not in _page_first and "could be read" in _page_first
      and "Ready now" not in _page_first, _page_first[:0])

print()
print("=" * 72)
print("THE WATCH LIST HOLDS ONLY THINGS WITH NOTHING TO BUY TODAY")
print("=" * 72)
_wscan = {"market": "stocks", "interval": "1d", "asked_for": 6,
          "cut_short": False, "seconds": 1.0, "scanned_at": _time.time(),
          "rows": [row(symbol="CALM", gap_multiple=1.2, asset_class="equity"),
                   row(symbol="WILDX", gap_multiple=4.0, asset_class="equity",
                       expectancy=0.35),
                   row(symbol="SICK", gap_multiple=1.5, asset_class="equity",
                       expectancy=0.3),
                   row(symbol="IDLE", action="AVOID", conviction=0.05,
                       asset_class="equity"),
                   row(symbol="DOWNX", action="AVOID", conviction=-0.4,
                       asset_class="equity"),
                   row(symbol="LOSS", action="AVOID", expectancy=-0.1,
                       asset_class="equity")]}
_steady = ideas._shortlist(_wscan, "medium", "steady")
check("steady sets the violent one aside as the wrong shape",
      _steady["wrong_shape"] == 1 and "WILDX" not in _names(_steady["passed"]))
check("and does not list it to watch as if nothing were firing",
      _names(_steady["watch"]) == {"IDLE", "DOWNX"}, _names(_steady["watch"]))

_real_check2 = ideas._check_accounts
ideas._check_accounts = lambda symbols, *_a, **_k: {
    "SICK": {"ok": False, "reasons": ["the balance sheet scores in the distress range"]},
    "CALM": {"ok": True, "reasons": []}, "WILDX": {"ok": True, "reasons": []}}
try:
    _months = ideas._shortlist(_wscan, "long", "balanced")
finally:
    ideas._check_accounts = _real_check2
check("a company left out for its accounts is not listed to watch either",
      "SICK" in _names(_months["dropped"]) and "SICK" not in _names(_months["watch"]))
check("the watch list never overlaps the other lists",
      not (_names(_months["watch"]) & (_names(_months["passed"])
                                       | _names(_months["dropped"])))
      and not (_names(_steady["watch"]) & {"WILDX"}))
check("and holds only stand-asides with a measured edge",
      all(r["action"] == "AVOID" and r["expectancy"] > 0
          for r in _months["watch"] + _steady["watch"]))
check("its count is the whole list, before anything is cut for display",
      _steady["watch_total"] == 2 and _steady["passed_total"] == len(_steady["passed"]))

# Coins file no accounts, so a months hold leaves every one out: that is not
# "nothing managed both", and not a fault in accounts they do not have.
_coins = ideas._shortlist(dict(_wscan, market="crypto", asked_for=2, rows=[
    row(symbol="AAA-USD", asset_class="crypto", gap_multiple=1.2),
    row(symbol="BBB-USD", asset_class="crypto", gap_multiple=1.2)]), "long", "balanced")
check("a months hold that leaves out all that cleared the bar says so",
      "nothing managed both" not in _coins["summary"]
      and "2 did clear the bar on the chart but were left out" in _coins["summary"],
      _coins["summary"])
check("and one that keeps some counts the rest",
      "left out of a months hold" in _months["summary"] and _months["passed"],
      _months["summary"])
check("with no Markdown in what people read",
      "*" not in ideas._verdict(10, [], 0, 0, "medium", 0, "balanced", 0))
check("what the risk filter set aside is said once, by the page",
      "payoff shape" not in _steady["summary"], _steady["summary"])
_only_wild = ideas._shortlist(dict(_wscan, rows=[
    row(symbol="WILDX", gap_multiple=4.0, asset_class="equity")]), "medium", "steady")
check("and with nothing left it is not called nothing that cleared the bar",
      "Nothing matching steady out of 1 looked at." in _only_wild["summary"]
      and "nothing managed both" not in _only_wild["summary"], _only_wild["summary"])
_short = dict(_steady, running=False,
              watch=[dict(r, price=50.0) for r in _steady["watch"]], passed=[
                  dict(_steady["passed"][0], action="SHORT", price=100.0, entry=100.0,
                       stop=106.0)])
_seen = []
_real_find = ideas.find
for _shown in (_short, dict(_only_wild, running=False)):
    ideas.find = lambda *_a, **_k: _shown
    try:
        _seen.append(" ".join(c.get("/ideas?market=stocks&horizon=medium&risk=steady"
                                    ).data.decode("utf-8", "replace").split()))
    finally:
        ideas.find = _real_find
check("the page says it beside how to see them, as more or as all there were",
      "1 more cleared the bar but did not match the risk" in _seen[0]
      and "1 cleared the bar but did not match the risk" in _seen[1])
check("a short's entry is not headed as a price to buy at",
      "Enter at" in _seen[0] and "Buy at" not in _seen[0])
check("each choice carries its own explanation, for the hint under the box",
      all('data-why="%s"' % spec["why"] in _seen[0].replace("&#39;", "'")
          for group in (ideas.MARKETS, ideas.HORIZONS, ideas.RISKS) for spec in group.values()))
check("and the watch list does not promise that routines run by themselves",
      "keep checking it" not in _seen[0] and "checks it again when you run it" in _seen[0])
ideas.find = lambda *_a, **_k: dict(_coins, running=False)
try:
    _page_coins = " ".join(c.get("/ideas?market=crypto&horizon=long&risk=balanced"
                                 ).data.decode("utf-8", "replace").split())
finally:
    ideas.find = _real_find
check("and its page does not blame accounts a coin does not have",
      "the accounts did not" not in _page_coins
      and "there are no accounts to check" in _page_coins
      and "Nothing worth buying today" not in _page_coins)

print()
print("=" * 72)
print("FILINGS THAT COULD NOT BE READ ARE NOT 'NO FILINGS'")
print("=" * 72)
from bot import fundamentals as _fund
from bot import sec as _sec

_real_hist, _real_load = _sec.financial_history, _fund.load


def _sec_down(symbol, years=6):
    raise _sec.SECError("EDGAR request failed: HTTP 503")


class _Refused:
    notes = ["Yahoo fundamentals unavailable: HTTP 429"]
    sector = ""

    def get(self, key):
        return None


try:
    _sec.financial_history = _sec_down
    _down = ideas._accounts_hold_up("DOWN")
    _sec.financial_history = lambda symbol, years=6: {"available": False}
    _none = ideas._accounts_hold_up("ETFX")
    _sec.financial_history = lambda symbol, years=6: {"available": True}
    _fund.load = lambda symbol, with_sec=True: _Refused()
    _refused = ideas._accounts_hold_up("REFU")
finally:
    _sec.financial_history, _fund.load = _real_hist, _real_load
check("filings the source would not send are not reported as none",
      _down is not None and _down.get("ok") is None
      and "503" in (_down.get("error") or ""), _down)
check("only a company that files nothing is 'not filed'", _none is None, _none)
check("and without the market value the distress check is not passed as sound",
      _refused is not None and _refused.get("ok") is None, _refused)

ideas._check_accounts = lambda symbols, *_a, **_k: {
    "DOWN": {"ok": None, "reasons": [], "error": "HTTP 503"}, "ETFX": None}
try:
    _labels = {r["symbol"]: r.get("accounts") for r in ideas._shortlist(
        {"market": "stocks", "interval": "1d", "asked_for": 2, "cut_short": False,
         "seconds": 1.0, "scanned_at": _time.time(),
         "rows": [row(symbol="DOWN", asset_class="equity", gap_multiple=1.2),
                  row(symbol="ETFX", asset_class="equity", gap_multiple=1.2)]},
        "long", "balanced")["passed"]}
finally:
    ideas._check_accounts = _real_check2
check("the list keeps it and says the filings could not be read",
      _labels == {"DOWN": "unreadable", "ETFX": "not filed"}, _labels)

_reads_of_filings = []


def _counting_hold(symbol):
    _reads_of_filings.append(symbol)
    return {"ok": None, "reasons": [], "error": "x"} if symbol == "FAILA" \
        else {"ok": True, "reasons": [], "strength": 7}


ideas._accounts_hold_up = _counting_hold
try:
    for _sym in ("KEEPA", "KEEPB", "FAILA"):
        ideas._VERDICTS.pop(_sym, None)
    ideas._check_accounts(["KEEPA", "KEEPB", "FAILA"])
    ideas._check_accounts(["KEEPA", "KEEPB"])
finally:
    ideas._accounts_hold_up = _real_hold
check("filings are read once and kept, not again on every page view",
      sorted(_reads_of_filings) == ["FAILA", "KEEPA", "KEEPB"], _reads_of_filings)
check("a good verdict is kept for most of a day",
      ideas._VERDICTS["KEEPA"][0] - _time.time() > 12 * 3600)
check("a read that failed is tried again within minutes",
      ideas._VERDICTS["FAILA"][0] - _time.time() <= ideas.VERDICT_RETRY + 1)

print()
print("=" * 72)
print("'BOTH' COVERS EVERY COIN, AND SHARES THE WORK")
print("=" * 72)
_real_stock_list, _real_analyse = ideas._stock_list, ideas._analyse
ideas._stock_list = lambda: ["S%03d" % i for i in range(150)]
try:
    _both = ideas._universe("both", 150)
finally:
    ideas._stock_list = _real_stock_list
check("'Both' covers the whole stocks list and every coin",
      len(_both) == 150 + len(ideas.CRYPTO) and set(ideas.CRYPTO) <= set(_both),
      len(_both))
check("with coins from the start, so a scan cut short still reaches them",
      sum(1 for s in _both[:20] if s.endswith("-USD")) == 10)

_analysed = []


def _counting_analyse(symbol, interval, cfg):
    _analysed.append(symbol)
    return dict(row(symbol=symbol, asset_class="crypto" if symbol.endswith("-USD")
                    else "equity", gap_multiple=1.2, price=1.0),
                read_at=_time.time())


ideas._analyse = _counting_analyse
ideas._stock_list = lambda: ["S%03d" % i for i in range(30)]
_cfg_rows = {"row-reuse": 1}
try:
    ideas.find("stocks", "medium", _cfg_rows, wait=30)
    ideas.find("crypto", "medium", _cfg_rows, wait=30)
    _before_both = len(_analysed)
    _both_found = ideas.find("both", "medium", _cfg_rows, wait=30)
    _after_both = len(_analysed)
    _same = ideas.find("stocks", "medium", dict(
        _cfg_rows, account_size=500.0, risk_per_trade_pct=2.0, max_position_pct=50.0,
        interval="1h", calm_candles=True, daily_loss_limit_r=2.0, max_trades_per_day=3,
        last_entry_time="15:00", cost_bps_futures=4.0), wait=30)
    _after_same = len(_analysed)
    _other = ideas.find("stocks", "medium", {"other-settings": 1}, wait=30)
    _costly = [ideas._fingerprint(dict(config_mod.DEFAULTS, **{k: v})) for k, v in (
        ("cost_bps_equity", 9.0), ("cost_bps_crypto", 90.0), ("stop_atr_multiple", 3.0),
        ("target_atr_multiple", 5.0), ("min_conviction", 0.3), ("horizon_bars", 20),
        ("allow_shorts", True), ("require_positive_expectancy", False))]
finally:
    ideas._stock_list, ideas._analyse = _real_stock_list, _real_analyse
check("settings that change nothing in a scan share the one already made",
      _after_same == _after_both and _same["complete"], (_after_same, _after_both))
check("while each setting that does change it gets its own",
      len(set(_costly + [ideas._fingerprint(config_mod.DEFAULTS)])) == len(_costly) + 1)
_sized = []
from bot import engine as _engine_mod


def _keep_size(symbol, cfg, **_kw):
    _sized.append(cfg["account_size"])
    raise RuntimeError("enough")


_real_engine = _engine_mod.analyse
_engine_mod.analyse = _keep_size
try:
    for _size in (50.0, 1e6):
        ideas._analyse("ZZSIZE", "1d", dict(config_mod.DEFAULTS, account_size=_size))
finally:
    _engine_mod.analyse = _real_engine
check("so a shared row never depends on whose account it was made for",
      len(_sized) == 2 and _sized[0] == _sized[1], _sized)
check("'Both' reuses the rows the stocks and crypto scans just read",
      _after_both == _before_both == 30 + len(ideas.CRYPTO)
      and _both_found["complete"]
      and _both_found["asked_for"] == 30 + len(ideas.CRYPTO),
      (_after_both, _before_both, _both_found["asked_for"]))
check("but never across different settings",
      len(_analysed) == _before_both + 30 and _other["complete"],
      (len(_analysed), _before_both))

print()
print("=" * 72)
print("ONE SCAN AT A TIME, AND A PAGE REFRESH NEVER WAITS")
print("=" * 72)
_gate2 = _threading.Event()
_active, _peak = [0], [0]
_track_lock = _threading.Lock()
_real_scan = ideas._scan


def _tracking_scan(*a, **k):
    with _track_lock:
        _active[0] += 1
        _peak[0] = max(_peak[0], _active[0])
    try:
        _gate2.wait(15)
        return _real_scan(*a, **k)
    finally:
        with _track_lock:
            _active[0] -= 1


ideas._scan = _tracking_scan
ideas._universe = lambda market, limit: ["Q%02d" % i for i in range(5)]
ideas._look_at = lambda s, i, cfg: row(symbol=s, asset_class="crypto",
                                       gap_multiple=1.2, price=1.0)
_real_wait = ideas.PAGE_WAIT
try:
    _one = ideas.find("crypto", "medium", {"slot": 1}, wait=0.3)
    _two = ideas.find("crypto", "medium", {"slot": 2}, wait=0.3)
    check("a second question waits its turn instead of running alongside",
          _one["running"] and not _one["queued"]
          and _two["running"] and _two["queued"], (_one.get("queued"), _two.get("queued")))
    ideas.find("crypto", "medium", {"slot": 3}, wait=0)
    ideas.find("crypto", "medium", {"slot": 4}, wait=0)
    try:
        ideas.find("crypto", "medium", {"slot": 5}, wait=0)
        _busy = False
    except ideas.Busy:
        _busy = True
    check("past a few waiting, a new question is turned away", _busy)
    _web._cfg = lambda: {"slot": 6}
    _r = c.get("/ideas?market=crypto&horizon=medium&risk=balanced")
    _busy_page = _r.data.decode("utf-8", "replace")
    check("and the page says so in plain words, with the form to ask again",
          _r.status_code == 503 and "Try again in a few minutes" in _busy_page
          and "Find something</button>" in _busy_page, _r.status_code)
    _web._cfg = lambda: {"slot": 2}
    _page_q = c.get("/ideas?market=crypto&horizon=medium&risk=balanced"
                    ).data.decode("utf-8", "replace")
    check("a question waiting its turn says so", "Waiting its turn" in _page_q)
    ideas.PAGE_WAIT = 5.0
    _web._cfg = lambda: {"slot": 1}
    _t0 = _time.time()
    c.get("/ideas?market=crypto&horizon=medium&risk=balanced")
    _poll = _time.time() - _t0
    check("a page refreshing itself on a running scan answers at once",
          _poll < 1.5, "%.1fs" % _poll)
    _t0 = _time.time()
    ideas.find("crypto", "medium", {"slot": 1}, wait=5, join_wait=0)
    check("and so does anyone else joining it", _time.time() - _t0 < 1.0)
    _gate2.set()
    _last = ideas.find("crypto", "medium", {"slot": 4}, wait=20)
    check("the queue drains one scan at a time", _peak[0] == 1 and _last["complete"],
          _peak[0])
finally:
    _gate2.set()
    _web._cfg = _web_cfg
    ideas.PAGE_WAIT = _real_wait
    ideas._scan = _real_scan
    ideas._universe, ideas._look_at = _real_universe, _real_look

print()
print("=" * 72)
print("THE SCAN ASKS THE DATA SOURCE FOR NO MORE THAN IT USES")
print("=" * 72)
import numpy as _np


def _synthetic(symbol, n=1258):
    rng = _np.random.default_rng(7)
    close = 100 * _np.exp(_np.cumsum(rng.normal(0.0004, 0.02, n)))
    high = close * (1 + _np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - _np.abs(rng.normal(0, 0.008, n)))
    end = int(_time.time()) - 2 * 86400
    return {"chart": {"result": [{
        "meta": {"symbol": symbol, "currency": "USD", "exchangeName": "NMS",
                 "instrumentType": "EQUITY", "gmtoffset": 0,
                 "regularMarketPrice": float(close[-1]),
                 "currentTradingPeriod": {"regular": {"start": 0, "end": 0}}},
        "timestamp": [end - (n - 1 - i) * 86400 for i in range(n)],
        "indicators": {"quote": [{"open": _np.r_[close[0], close[:-1]].tolist(),
                                  "high": high.tolist(), "low": low.tolist(),
                                  "close": close.tolist(),
                                  "volume": [2e6] * n}]}}]}}


_asked = {"search": 0, "chart": 0, "other": 0}


def _fake_get(url, params, tries=3):
    if "search" in url:
        _asked["search"] += 1
        return {"quotes": [{"symbol": params["q"], "quoteType": "EQUITY",
                            "shortname": params["q"]}]}
    _asked["chart"] += 1
    return _synthetic(url.rsplit("/", 1)[-1])


def _fake_yahoo(url, params=None, tries=3):
    _asked["other"] += 1
    raise _yahoo.YahooError("offline")


_real_get, _real_yget = _market._get, _yahoo.get
_market._get, _yahoo.get = _fake_get, _fake_yahoo
try:
    _scanned_row = ideas._analyse("ZZSYN", "1d", config_mod.load())
    _per_scan = dict(_asked)
    from bot import engine as _engine
    _engine.analyse("ZZSYN", config_mod.load(), interval="1d", with_news=False,
                    record=False)
finally:
    _market._get, _yahoo.get = _real_get, _real_yget
check("a scanned instrument costs one request: no name search, no calendar",
      _scanned_row is not None and _per_scan == {"search": 0, "chart": 1, "other": 0},
      _per_scan)
check("while a full analysis still looks both up",
      _asked["search"] == 1 and _asked["other"] >= 1 and _asked["chart"] == 2, _asked)

_screens = []


def _counting_screen(name, count=100):
    _screens.append(name)
    return ["%s%s" % (name[:2].upper(), chr(65 + i)) for i in range(5)]


def _screen_down(name, count=100):
    raise RuntimeError("screens down")


_real_us = _screener.universe_symbols
ideas._UNIVERSE.clear()
_screener.universe_symbols = _counting_screen
try:
    _list1 = ideas._stock_list()
    _list2 = ideas._stock_list()
    _reads_first = len(_screens)
    ideas._UNIVERSE["stocks"] = (_time.time() - ideas.UNIVERSE_TTL - 1, _list1)
    _screener.universe_symbols = _screen_down
    _list3 = ideas._stock_list()
finally:
    _screener.universe_symbols = _real_us
    ideas._UNIVERSE.clear()
check("the stock list is read once for several scans",
      _reads_first == len(ideas._SCREENS) and _list1 == _list2, _reads_first)
check("and when the screens fail, the last list they gave stands in",
      _list3 == _list1 and _list3 != ideas.FALLBACK_STOCKS)


def _slow_universe(market, limit):
    _time.sleep(0.3)
    return ["W%02d" % i for i in range(5)]


def _slowish_look(symbol, interval, cfg):
    _time.sleep(0.05)
    return row(symbol=symbol)


ideas._universe, ideas._look_at = _slow_universe, _slowish_look
_real_budget = ideas.SCAN_BUDGET
ideas.SCAN_BUDGET = 0.2
try:
    _budgeted = ideas._scan("crypto", "1d", {"budget-test": 1}, 150)
finally:
    ideas.SCAN_BUDGET = _real_budget
    ideas._universe, ideas._look_at = _real_universe, _real_look
check("time spent fetching the list counts against the scan's budget",
      _budgeted["cut_short"] and not _budgeted["complete"])

print()
print("=" * 72)
print("THE COIN LIST NAMES THE COINS IT MEANS")
print("=" * 72)
# ARB-USD on the data source is "ARbit", a token with no trading at all;
# Arbitrum is ARB11841-USD. UNI-USD and SUI-USD have no prices at all.
check("no ticker that belongs to some other token",
      not {"ARB-USD", "UNI-USD", "SUI-USD"} & set(ideas.CRYPTO))
check("the coins meant are there under their own ids",
      {"ARB11841-USD", "UNI7083-USD", "SUI20947-USD"} <= set(ideas.CRYPTO))

import sys as _exit_sys
print()
print("%d failure(s)" % len(fails))
print("IDEAS OK" if not fails else "FAILURES: %s" % fails)
_exit_sys.exit(1 if fails else 0)
