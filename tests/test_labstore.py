"""The Setup lab's store and catalog, and the futures cost and new settings.

labstore: its fixtures pass its own checks; a missing key or a wrong type is
refused with the key named; records round-trip through the database; its
verdict is the strategy tests' rule (public._verdict) and says "too few"
below the minimum; its words carry no jargon.

setups: every name, label, timestamp, topic and claim is consistent, and the
plain words are plain.

F7: a future is charged its own cost (1 basis point round trip by default),
stocks and coins what they were; bad settings are refused in words; the new
settings are kept per person. No network.
"""
import copy
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

DB, CFG_PATH = harness.isolate("labstore")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import catalysts, config, database as db, decision, engine, labstore, public  # noqa: E402
from bot import plain, setups  # noqa: E402

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


JARGON = ("expectancy", "r-multiple", "confidence interval", "atr", "wilson",
          "basis point", "sharpe", "drawdown")


def refuses(fn, data, key):
    """Whether fn refuses data with a ValueError that names key."""
    try:
        fn(data)
    except ValueError as exc:
        return key in str(exc)
    return False


# ---------------------------------------------------------------------------
head("THE FIXTURES PASS THEIR OWN CHECKS")
# ---------------------------------------------------------------------------
check("a lab result", labstore.validate_result(labstore.sample_result()) is not None)
check("a claim", labstore.validate_claim(labstore.sample_claim()) is not None)
check("a bias record", labstore.validate_bias(labstore.sample_bias()) is not None)
check("a claim fixture for every claim id",
      all(labstore.validate_claim(labstore.sample_claim(c.id)) for c in setups.CLAIMS))

# ---------------------------------------------------------------------------
head("A MISSING KEY OR A WRONG TYPE IS REFUSED, NAMING THE KEY")
# ---------------------------------------------------------------------------
base = labstore.sample_result()
cases = []
r = copy.deepcopy(base); del r["symbol"]; cases.append((r, "result.symbol"))
r = copy.deepcopy(base); r["days"] = "59"; cases.append((r, "result.days"))
r = copy.deepcopy(base); r["cost_bps"] = float("nan"); cases.append((r, "result.cost_bps"))
r = copy.deepcopy(base); r["setups"]["made_up"] = r["setups"]["liquidity_model"]
cases.append((r, "result.setups.made_up"))
r = copy.deepcopy(base); del r["setups"]["liquidity_model"]["rules"]["level"]
cases.append((r, "result.setups.liquidity_model.rules.level"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["n"] = 4.5
cases.append((r, "result.setups.liquidity_model.rules.2r.n"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["wins"] = 99
cases.append((r, "result.setups.liquidity_model.rules.2r.wins"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["se"] = "big"
cases.append((r, "result.setups.liquidity_model.rules.2r.se"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["by_window"]["noon"] = [1, 0, 0.5]
cases.append((r, "result.setups.liquidity_model.rules.2r.by_window.noon"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["control"] = {"n": 3}
cases.append((r, "result.setups.liquidity_model.rules.2r.control.sum_r"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["example"]["c"] = [1.0]
cases.append((r, "result.setups.liquidity_model.example.c"))
r = copy.deepcopy(base); r["setups"]["liquidity_model"]["rules"]["2r"]["stable"] = "yes"
cases.append((r, "result.setups.liquidity_model.rules.2r.stable"))
bad = [key for data, key in cases if not refuses(labstore.validate_result, data, key)]
check("each of %d broken results is refused naming its key" % len(cases), not bad, bad)

claim = labstore.sample_claim()
ccases = []
c = copy.deepcopy(claim); c["claim"] = "nobody_said_this"; ccases.append((c, "claim.claim"))
c = copy.deepcopy(claim); c["pooled"]["hits"] = 500; ccases.append((c, "claim.pooled.hits"))
c = copy.deepcopy(claim); del c["pooled"]["base_trials"]; ccases.append((c, "claim.pooled.base_trials"))
c = copy.deepcopy(claim); c["verdict"] = 2; ccases.append((c, "claim.verdict"))
c = copy.deepcopy(claim); c["verdict"] = True; ccases.append((c, "claim.verdict"))
c = copy.deepcopy(claim); c["by_symbol"]["ES=F"]["trials"] = -1
ccases.append((c, "claim.by_symbol.ES=F.trials"))
c = copy.deepcopy(claim); c["notes"] = "one note"; ccases.append((c, "claim.notes"))
bad = [key for data, key in ccases if not refuses(labstore.validate_claim, data, key)]
check("each of %d broken claims is refused naming its key" % len(ccases), not bad, bad)

bcases = []
b = labstore.sample_bias(); b["right"] = 500; bcases.append((b, "bias.right"))
b = labstore.sample_bias(); del b["unclear"]; bcases.append((b, "bias.unclear"))
b = labstore.sample_bias(); b["days"] = 3.5; bcases.append((b, "bias.days"))
bad = [key for data, key in bcases if not refuses(labstore.validate_bias, data, key)]
check("each of %d broken bias records is refused naming its key" % len(bcases), not bad, bad)

# ---------------------------------------------------------------------------
head("RECORDS ROUND-TRIP THROUGH THE DATABASE")
# ---------------------------------------------------------------------------
conn = db.connect(DB)
labstore.save_result(conn, labstore.sample_result("NQ=F", "15m"), now=1795000000)
labstore.save_result(conn, labstore.sample_result("ES=F", "5m"), now=1795000100)
labstore.save_result(conn, labstore.sample_result("ES=F", "15m"), now=1795000200)
got = labstore.result(conn, "ES=F", "5m")
check("a result comes back as it went in, with when it was written",
      got is not None and got["updated"] == 1795000100
      and {k: v for k, v in got.items() if k != "updated"} == labstore.sample_result("ES=F", "5m"))
check("results come back in the lab's own order",
      [(r["symbol"], r["interval"]) for r in labstore.results(conn)]
      == [("ES=F", "5m"), ("ES=F", "15m"), ("NQ=F", "15m")])
check("and by bar size", [r["symbol"] for r in labstore.results(conn, "15m")] == ["ES=F", "NQ=F"])
check("one never written is None", labstore.result(conn, "SPY", "5m") is None)
try:
    broken = labstore.sample_result()
    broken["days"] = None
    labstore.save_result(conn, broken)
    saved_broken = True
except ValueError:
    saved_broken = False
check("a broken record is never written", not saved_broken
      and labstore.result(conn, "ES=F", "5m")["days"] == 59)
labstore.save_claim(conn, labstore.sample_claim(), now=1795000300)
check("a claim round-trips", labstore.claim(conn, "london_takes_asia")["pooled"]["hits"] == 54
      and list(labstore.claims(conn)) == ["london_takes_asia"])
labstore.save_bias(conn, "ES=F", labstore.sample_bias())
labstore.save_bias(conn, labstore.POOL_ALL, labstore.sample_bias())
check("a bias record round-trips, per instrument and pooled",
      labstore.bias(conn, "ES=F")["right"] == 101 and labstore.bias(conn, "_all")["days"] == 312
      and labstore.bias(conn, "SPY") is None)
first = labstore.save_run(conn, 1795000000, 1795000500, {"symbols": 8, "failed": []})
second = labstore.save_run(conn, 1795086400, None, {"symbols": 8, "failed": ["NVDA"]})
run = labstore.last_run(conn)
check("the newest run is the one returned", run["id"] == second and run["finished"] is None
      and run["summary"]["failed"] == ["NVDA"] and first != second)
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check("its four tables, none of them anyone's personal data",
      {"lab_results", "lab_claims", "bias_record", "lab_runs"} <= tables
      and not {"lab_results", "lab_claims", "bias_record", "lab_runs"} & set(A.OWNED_TABLES))
conn.close()

# ---------------------------------------------------------------------------
head("ONE VERDICT RULE, THE STRATEGY TESTS' OWN")
# ---------------------------------------------------------------------------
rng = np.random.RandomState(9)
pairs = [(float(m), float(s)) for m, s in zip(rng.normal(0, 0.3, 17), rng.uniform(0.01, 0.3, 17))]
pairs += [(0.2, 0.1), (-0.2, 0.1), (0.0, 0.05)]           # exactly on the edges
check("matches public._verdict on 20 cases",
      all(labstore.verdict(m, s, 100) == public._verdict(m, s) for m, s in pairs),
      [(m, s, labstore.verdict(m, s, 100), public._verdict(m, s)) for m, s in pairs
       if labstore.verdict(m, s, 100) != public._verdict(m, s)])
check("and says nothing below the minimum",
      labstore.verdict(1.0, 0.01, 29) is None and labstore.verdict(1.0, 0.01, 30) == 1
      and labstore.verdict(1.0, 0.01, 9, minimum=10) is None)
check("or without a margin", labstore.verdict(0.5, None, 100) is None
      and labstore.verdict(0.5, float("nan"), 100) is None)
check("or when the trades came on fewer than 10 separate days",
      labstore.verdict(1.0, 0.01, 50, days=9) is None
      and labstore.verdict(1.0, 0.01, 50, days=10) == 1)
check("its words", labstore.words(1) == "kept money by more than its margin of error"
      and labstore.words(-1) == "lost money by more than its margin of error"
      and labstore.words(0) == "cannot be told apart from luck"
      and labstore.words(None) == "too few to say")
check("the claims' words compare with what usually happens",
      labstore.claim_words(1) == "happens more often than usual"
      and labstore.claim_words(0).startswith("cannot be told apart"))
said = " ".join([labstore.words(v) for v in (1, -1, 0, None)]
                + [labstore.claim_words(v) for v in (1, -1, 0, None)]).lower()
check("with no jargon in them", not [w for w in JARGON if w in said])
low, high = labstore.wilson(54, 120)
check("a rate's range (Wilson) holds the rate", low < 54 / 120.0 < high and 0 <= low and high <= 1)
check("a rate against its control by two margins",
      labstore.rate_verdict(90, 120, 60, 120) == 1 and labstore.rate_verdict(54, 120, 50, 120) == 0
      and labstore.rate_verdict(9, 20, 5, 20) is None)
pool = labstore.pooled([labstore.sample_result("ES=F"), labstore.sample_result("NQ=F")],
                       "liquidity_model", "2r")
check("pooling adds trades and days, and keeps a margin",
      pool["n"] == 82 and pool["days"] == 60 and abs(pool["sum_r"] + 6.4) < 1e-9
      and pool["se"] is not None and pool["verdict"] in (1, 0, -1)
      and pool["symbols"] == ["ES=F", "NQ=F"])
check("and a group with nothing in it is None",
      labstore.pooled([labstore.sample_result()], "liquidity_model", "2r", group="coin") is None)

# ---------------------------------------------------------------------------
head("THE SETUP CATALOG")
# ---------------------------------------------------------------------------
names = [s.name for s in setups.SETUPS]
check("names are unique", len(names) == len(set(names)))
TEN = ("sweep_reclaim", "prior_day_sweep", "equal_levels_sweep", "session_sweep",
       "structure_flip", "sweep_then_flip", "fvg_retest", "ifvg_flip", "premium_discount",
       "liquidity_model")
check("the ten strategies on trial are there, and the breakout the lab measures beside them",
      set(setups.REGISTRY_NAMES) == set(TEN) and "crowd_breakout" in names
      and not setups.setup("crowd_breakout").registry)
check("only the session sweep needs intraday bars",
      [s.name for s in setups.SETUPS if s.intraday_only] == ["session_sweep"])
check("every stop rule is one the lab knows",
      all(s.stop_rule in setups.STOP_RULES for s in setups.SETUPS))
end = setups.seconds(setups.VIDEO["length"])
stamps = [part for item in list(setups.SETUPS) + list(setups.CLAIMS) for stamp in item.video
          for part in stamp.split("-")]
wrong = []
for part in stamps:
    try:
        if not 0 <= setups.seconds(part) <= end:
            wrong.append(part)
    except ValueError:
        wrong.append(part)
check("every timestamp is h:mm:ss within the video's 6:40:00 (%d of them)" % len(stamps),
      not wrong and len(stamps) > 30, wrong)
check("a range runs forwards", all(setups.seconds(a) < setups.seconds(b) for item in
                                   list(setups.SETUPS) + list(setups.CLAIMS)
                                   for stamp in item.video if "-" in stamp
                                   for a, b in [stamp.split("-")]))
check("labels are at most 40 characters", all(len(s.label) <= 40 for s in setups.SETUPS),
      [s.label for s in setups.SETUPS if len(s.label) > 40])
slugs = [t.slug for t in setups.TOPICS]
check("15 Learn topics with unique slugs", len(slugs) == 15 == len(set(slugs)))
check("every setup and claim a topic names exists",
      all(n in setups.BY_NAME for t in setups.TOPICS for n in t.setups)
      and all(c in setups.CLAIMS_BY_ID for t in setups.TOPICS for c in t.claims))
check("every setup's and claim's topic is one of them",
      all(s.learn in slugs for s in setups.SETUPS) and all(c.learn in slugs for c in setups.CLAIMS))
check("every setup and claim is on its own topic's page",
      all(s.name in setups.topic(s.learn).setups for s in setups.SETUPS)
      and all(c.id in setups.topic(c.learn).claims for c in setups.CLAIMS))
check("claim ids are unique, 15 to measure first and 4 later",
      len(setups.CLAIMS) == len(setups.CLAIMS_BY_ID) == 19
      and sum(c.must for c in setups.CLAIMS) == 15)
plain_text = [s.plain for s in setups.SETUPS] + [c.statement for c in setups.CLAIMS]
leaked = [(w, t[:40]) for t in plain_text for w in JARGON if w in t.lower()]
check("plain words and claims carry none of the jargon test_method forbids", not leaked, leaked)
named = [(s.name, s.label) for s in setups.SETUPS
         if s.label.lower() in s.plain.lower() and s.label.lower() not in ("the 50% line",)]
check("a setup's plain words do not lean on its own name", not named, named)
said = " ".join(s.plain + " " + s.course_says for s in setups.SETUPS).lower() + " " + \
    " ".join(c.statement + " " + c.how for c in setups.CLAIMS).lower()
check("nothing promises: no 'will', 'guaranteed', 'high probability' or 'smart money'",
      not any(w in said for w in (" will ", "guarantee", "high probability", "smart money",
                                  "manipulat", "every day")))
check("the journal's lists have keys and labels",
      all(k and v for k, v in setups.JOURNAL_SETUPS + setups.EMOTIONS + setups.FOLLOWED
          + setups.CHECKLIST) and len(setups.CHECKLIST) == 8
      and [k for k, _ in setups.JOURNAL_SETUPS][-3:] == ["orenth_call", "own", "other"])
check("eight lab instruments, two bar sizes, three pairs",
      len(setups.LAB_SYMBOLS) == 8 and setups.LAB_INTERVALS == ("5m", "15m")
      and all(a in dict((s, n) for s, n, _ in setups.LAB_SYMBOLS) for pair in setups.PAIRS
              for a in pair))
check("the video is credited with a link to the second",
      setups.video_link("1:36:14-1:44:37") == "https://youtu.be/yiuFUp0kFz8?t=5774")

# ---------------------------------------------------------------------------
head("FUTURES ARE CHARGED THEIR OWN COST")
# ---------------------------------------------------------------------------
CFG = config.load()
check("1 basis point by default, the others as they were",
      config.cost_bps_for_symbol("ES=F", "other", CFG) == 1.0
      and config.cost_bps_for_symbol("AAPL", "equity", CFG) == 2.0
      and config.cost_bps_for_symbol("BTC-USD", "crypto", CFG) == 20.0
      and config.cost_bps_for_symbol("^GSPC", "other", CFG) == 5.0
      and config.cost_bps_for_symbol("nq=f", "other", CFG) == 1.0)
_FEED = {}
engine.resolve = lambda s: {"symbol": s, "name": s}
engine.fetch_bars = lambda s, iv: _FEED[s]


def _no_calendar(symbol):
    raise RuntimeError("no calendar in tests")


catalysts.fetch = _no_calendar
seen = []
_real_cost = decision.cost_bps_for_symbol


def _spy(symbol, asset_class, cfg):
    got = _real_cost(symbol, asset_class, cfg)
    seen.append((symbol, got))
    return got


def edge_series(seed, symbol, kind, price, n=1250, leg=60, drift=0.005, vol=0.01, into=40,
                dip=4):
    """test_method's series with a real edge in its signals, ending on a pullback
    and a turn back up inside a rise: the plan builds a trade on it."""
    rng = np.random.RandomState(seed)
    k = np.arange(n)
    sign = np.where(((k - (n - into)) // leg) % 2 == 0, 1.0, -1.0)
    rets = sign * drift + rng.normal(0.0, vol, n)
    rets[-dip - 1:-1] = -0.012 + rng.normal(0.0, vol * 0.2, dip)
    rets[-1] = 0.02
    close = price * np.exp(np.cumsum(rets))
    prev = np.concatenate([[price], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, vol * 0.2, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.3, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.3, n)))
    open_[-1] = low[-1] = close[-2] * 0.998
    high[-1] = close[-1] = max(close[-1], high[-2] * 1.002)
    return sc.make(open_, high, low, close, sc.daily_times(n), kind=kind, interval="1d",
                   symbol=symbol)


decision.cost_bps_for_symbol = _spy
for symbol, kind, price in (("ES=F", "future", 6000.0), ("AAPL", "stock", 190.0),
                            ("BTC-USD", "coin", 90000.0)):
    bars = edge_series(5, symbol, kind, price)
    _FEED[symbol] = bars
    result = engine.analyse(symbol, CFG, interval="1d", with_news=False, record=False)
    want = {"ES=F": 1.0, "AAPL": 2.0, "BTC-USD": 20.0}[symbol]
    check("%s (%s): the analysis charges %g bp, and so do its odds"
          % (symbol, bars.asset_class, want),
          result.cost_bps == want and result.calibration.cost_bps == want,
          (result.cost_bps, result.calibration.cost_bps))
decision.cost_bps_for_symbol = _real_cost
check("the plan's own cost check uses the same figure, when it builds a trade",
      ("ES=F", 1.0) in seen
      and all(got == {"ES=F": 1.0, "AAPL": 2.0, "BTC-USD": 20.0}[sym] for sym, got in seen),
      seen)
check("the help on costs names the futures figure",
      "0.01% for a futures contract" in plain.help_for(config.DEFAULTS)["costs"]["text"]
      and "0.02% for a futures contract" in plain.help_for(
          dict(config.DEFAULTS, cost_bps_futures=2.0))["costs"]["text"])

# ---------------------------------------------------------------------------
head("THE NEW SETTINGS: CHECKED IN WORDS, KEPT PER PERSON")
# ---------------------------------------------------------------------------
fresh = config.DEFAULTS
check("they are off by default", fresh["daily_loss_limit_r"] == 0 and fresh["max_trades_per_day"] == 0
      and fresh["last_entry_time"] == "" and fresh["calm_candles"] is False
      and fresh["cost_bps_futures"] == 1.0)
good = [{"cost_bps_futures": 0.0}, {"daily_loss_limit_r": 2.0}, {"daily_loss_limit_r": 20},
        {"max_trades_per_day": 3}, {"max_trades_per_day": 3.0}, {"max_trades_per_day": 100},
        {"last_entry_time": "10:30"}, {"last_entry_time": "9:30"}, {"last_entry_time": ""},
        {"calm_candles": True}]
refused = []
for change in good:
    try:
        config.validate(dict(config.DEFAULTS, **change))
    except RuntimeError as exc:
        refused.append((change, str(exc)))
check("good values pass", not refused, refused)
bad_values = [{"cost_bps_futures": -0.5}, {"cost_bps_futures": "cheap"},
              {"daily_loss_limit_r": -1}, {"daily_loss_limit_r": 25}, {"daily_loss_limit_r": True},
              {"max_trades_per_day": 2.5}, {"max_trades_per_day": 101}, {"max_trades_per_day": -1},
              {"last_entry_time": "25:00"}, {"last_entry_time": "10:3"},
              {"last_entry_time": "ten thirty"}, {"last_entry_time": 1030},
              {"calm_candles": "yes"}]
messages = {}
let_through = []
for change in bad_values:
    try:
        config.validate(dict(config.DEFAULTS, **change))
        let_through.append(change)
    except RuntimeError as exc:
        messages[str(list(change))] = str(exc)
check("bad values are refused (%d of them)" % len(bad_values), not let_through, let_through)
check("the time rule reads as minutes after midnight",
      config.clock_minutes("10:30") == 630 and config.clock_minutes("9:30") == 570
      and config.clock_minutes("24:00") is None and config.clock_minutes("") is None)
check("in words, with no setting's name in them",
      all(m.endswith(".") and "_" not in m for m in messages.values()), messages)
conn = db.connect(DB)
trader = A.upsert_email_user(conn, "trader@example.com")
other = A.upsert_email_user(conn, "other@example.com")
mine = dict(config.DEFAULTS, cost_bps_futures=0.6, daily_loss_limit_r=2.0, max_trades_per_day=3,
            last_entry_time="10:30", calm_candles=True)
A.save_settings(conn, trader.id, mine)
kept = A.settings_for(conn, trader.id)
check("saving settings keeps the five new ones for that person",
      kept["cost_bps_futures"] == 0.6 and kept["daily_loss_limit_r"] == 2.0
      and kept["max_trades_per_day"] == 3 and kept["last_entry_time"] == "10:30"
      and kept["calm_candles"] is True, kept)
check("and nobody else", A.settings_for(conn, other.id) == {})
conn.close()
import web  # noqa: E402
check("a different futures cost is a different cached analysis",
      web._settings_key(dict(config.DEFAULTS, cost_bps_futures=1.0))
      != web._settings_key(dict(config.DEFAULTS, cost_bps_futures=2.0)))


print()
print("%d failure(s)" % len(fails))
print("LABSTORE OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
