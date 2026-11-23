"""The price-action alerts: yesterday's high and low, the opening gap, a break
of the last swing, the newest gap and London's range.

Each says what the price did, with the symbol, a price and the New York time,
and never what it will do. An alert about an event on a finished bar tells it
once, does not miss one that happened between two checks, keeps quiet while
nothing new happens, and tells the next one. One about the live price fires
when it crosses, stays quiet while it stays across, and fires again after it
has been back. One that cannot happen on its instrument says so. The analysis
page suggests them with no value, never "None". No network: prices are made up.
"""
import os as _os
import re
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for key in ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET", "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(key, None)

import dataclasses  # noqa: E402

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
import web  # noqa: E402
from bot import alerts, catalysts, clock, engine, liquidity  # noqa: E402
from bot import database as db  # noqa: E402

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


c, DB = harness.isolated_web("alertkinds")
web.app.config["AUTH_MODE"] = None
conn = db.connect(DB)

# What the next check downloads, per symbol.
FEED = {}
alerts.fetch_bars = lambda symbol, interval="1d", lookback=None: FEED[symbol]


def add(symbol, kind, interval="5m", since=None):
    """An alert that keeps watching, set at `since` (a time in the bars)."""
    aid = db.alert_add(conn, symbol, kind, interval=interval, repeat=True)
    if since is not None:
        conn.execute("UPDATE alerts SET created_ts=? WHERE id=?", (int(since), aid))
        conn.commit()
    return aid


def told(aid):
    """Check every alert; what this one said, or None."""
    out = alerts.check(conn)
    hits = [f for f in out["fired"] if f.alert_id == aid]
    return hits[0].message if hits else None


def row(aid):
    return conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()


def live(bars, price, at):
    """The bars with a live price set at time `at`."""
    return dataclasses.replace(bars, live_price=float(price),
                               meta=dict(bars.meta, regularMarketTime=int(at)))


# ---------------------------------------------------------------------------
# A scripted future: three trading days from the Sunday 18:00 reopen, quiet
# at 100 (99.9 to 100.1). Day 1 reaches 101.0 at 12:00. Day 2's London session
# reaches 100.6 at 05:00; at 09:00 New York a bar trades to 101.3 and closes
# back at 100.8: yesterday's high (101.0) taken and closed back below, and
# London's high (100.6) taken. Day 3 does it again at 09:30: to 101.5 (above
# day 2's 101.3), closing at 101.0.
# ---------------------------------------------------------------------------
TS = sc.futures_times(276 * 3)
N = len(TS)
O = np.full(N, 100.0)
C = np.full(N, 100.0)
H = np.full(N, 100.1)
L = np.full(N, 99.9)
DAYS = [int(clock.trading_day(int(t), "future").toordinal()) for t in TS]
FIRST = sorted({DAYS.index(d) for d in DAYS})


def at(day, hhmm):
    """The bar of trading day `day` (0, 1, 2) that opens at hhmm New York time."""
    for j in range(FIRST[day], N):
        if DAYS[j] != DAYS[FIRST[day]]:
            break
        if clock.fmt_ny(int(TS[j])) == hhmm:
            return j
    raise ValueError(hhmm)


H[at(0, "12:00")] = 101.0
H[at(1, "05:00")] = 100.6
SWEEP1 = at(1, "09:00")
O[SWEEP1], H[SWEEP1], L[SWEEP1], C[SWEEP1] = 100.5, 101.3, 100.4, 100.8
SWEEP2 = at(2, "09:30")
O[SWEEP2], H[SWEEP2], L[SWEEP2], C[SWEEP2] = 100.6, 101.5, 100.5, 101.0
ES = sc.make(O, H, L, C, TS, kind="future", symbol="ES=F")
STEP = 300

# ---------------------------------------------------------------------------
head("YESTERDAY'S HIGH TAKEN, THEN CLOSED BACK BELOW")
# ---------------------------------------------------------------------------
aid = add("ES=F", "sweeps_prior_high", since=TS[FIRST[1]])
FEED["ES=F"] = clock.head(ES, SWEEP1)
check("quiet before it happens", told(aid) is None and row(aid)["last_error"] is None,
      row(aid)["last_error"])
FEED["ES=F"] = clock.head(ES, SWEEP1 + 4)
said = told(aid)
print("   " + str(said))
check("told once it has, though it happened between two checks",
      said is not None and said.startswith("ES=F took yesterday's high of 101")
      and "closed back below it, at 100.8" in said
      and "09:00 New York time" in said and said.endswith(alerts.FACT), said)
check("not told twice", told(aid) is None)
FEED["ES=F"] = clock.head(ES, SWEEP2)
check("quiet while nothing new happens, over the rest of the day and the next morning",
      told(aid) is None)
FEED["ES=F"] = clock.head(ES, SWEEP2 + 1)
said = told(aid)
check("told again on the next day's sweep",
      said is not None and "yesterday's high of 101.3" in said and "09:30 New York time" in said,
      said)
check("its state is the last bar it looked at",
      row(aid)["state"] == "bar:%d" % int(TS[SWEEP2]), row(aid)["state"])
FEED["ES=F"] = clock.head(ES, SWEEP1 + 4)
stale = told(aid)
FEED["ES=F"] = clock.head(ES, SWEEP2 + 1)
check("stale data from the source does not make it tell an old sweep again",
      stale is None and told(aid) is None
      and row(aid)["state"] == "bar:%d" % int(TS[SWEEP2]), row(aid)["state"])

aid = add("ES=F", "sweeps_prior_high", since=TS[SWEEP1 + 10])
FEED["ES=F"] = clock.head(ES, SWEEP1 + 20)
check("a sweep from before the alert was set is not news", told(aid) is None)

# The first checks fail; the sweep happens meanwhile; then one works.
aid = add("ES=F", "sweeps_prior_high", since=TS[SWEEP1 - 3])
FEED["ES=F"] = None                                   # nothing that can be read
alerts.check(conn)
check("a check that failed is said on the alert", row(aid)["last_error"] is not None)
FEED["ES=F"] = clock.head(ES, SWEEP1 + 4)
said = told(aid)
check("what happened while the first checks failed is told once one works",
      said is not None and "09:00 New York time" in said, said)
db.alert_set_active(conn, aid, False)
db.alert_set_active(conn, aid, True)
conn.execute("UPDATE alerts SET state=? WHERE id=?", ("since:%d" % int(TS[SWEEP2 + 5]), aid))
conn.commit()
FEED["ES=F"] = clock.head(ES, SWEEP2 + 10)
check("one resumed looks from then: what happened while it was paused is not told",
      told(aid) is None and row(aid)["state"] == "bar:%d" % int(TS[SWEEP2 + 9]), row(aid)["state"])
db.alert_set_active(conn, aid, False)
db.alert_set_active(conn, aid, True)
check("and resuming is what sets that", row(aid)["state"].startswith("since:"), row(aid)["state"])

# ---------------------------------------------------------------------------
head("A DAILY BAR IS OVER AT THE CLOSE, NOT A DAY AFTER IT OPENED")
# ---------------------------------------------------------------------------
DTS = sc.daily_times(120, start=sc.date(2026, 3, 2))
dn = len(DTS)
dO, dC = np.full(dn, 100.0), np.full(dn, 100.0)
dH, dL = np.full(dn, 100.5), np.full(dn, 99.5)
dH[-2] = 101.0
dO[-1], dH[-1], dL[-1], dC[-1] = 100.2, 102.0, 100.0, 100.4
SWP = sc.make(dO, dH, dL, dC, DTS, kind="stock", interval="1d", symbol="SWP")
FEED["SWP"] = SWP
OPENED = int(DTS[-1])
CLOSED = OPENED + 6 * 3600 + 1800
aid = add("SWP", "sweeps_prior_high", interval="1d", since=CLOSED + 2 * 3600)
check("set in the evening, the session that has closed is not news", told(aid) is None)
aid = add("SWP", "sweeps_prior_high", interval="1d", since=OPENED + 3600)
check("set while it traded, it is told after the close", told(aid) is not None)
conn.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
db.alert_reset(conn, aid)
check("re-arming looks from that moment", row(aid)["state"].startswith("since:"), row(aid)["state"])
conn.execute("UPDATE alerts SET state=? WHERE id=?", ("since:%d" % (CLOSED + 3600), aid))
conn.commit()
check("so re-armed the same evening, the same event is not told again", told(aid) is None)
check("and the exchange's own closing time is used when the data gives it",
      int(alerts._bar_ends(dataclasses.replace(SWP, gmt_offset=-14400, meta=dict(
          SWP.meta, currentTradingPeriod={"regular": {
              "start": OPENED, "end": OPENED + 3 * 3600 + 1800}})))[-1]) == OPENED + 3 * 3600 + 1800
      and int(alerts._bar_ends(SWP)[-1]) == CLOSED, alerts._bar_ends(SWP)[-1] - OPENED)
check("a coin's day lasts a day, and a five-minute bar five minutes",
      int(alerts._bar_ends(sc.coin_series(60))[-1]) == int(sc.coin_series(60).timestamp[-1]) + 300
      and int(alerts._bar_ends(ES)[0]) == int(TS[0]) + 300)

# ---------------------------------------------------------------------------
head("LONDON'S RANGE, IN THE NEW YORK MORNING")
# ---------------------------------------------------------------------------
aid = add("ES=F", "takes_session_range", since=TS[FIRST[1]])
FEED["ES=F"] = clock.head(ES, SWEEP1)
check("quiet before", told(aid) is None)
FEED["ES=F"] = clock.head(ES, SWEEP1 + 1)
said = told(aid)
print("   " + str(said))
check("told when New York trades above London's high",
      said is not None and "above the London session's high of 100.6" in said
      and "09:00 New York time" in said and "closed at 100.8" in said, said)
FEED["ES=F"] = clock.head(ES, SWEEP2 + 1)
said = told(aid)
check("and again the next day", said is not None and "09:30 New York time" in said, said)

STOCK = sc.stock_series(600)
FEED["AAPL"] = dataclasses.replace(STOCK, symbol="AAPL")
aid = add("AAPL", "takes_session_range")
out = alerts.check(conn)
note = row(aid)["last_error"] or ""
check("a stock says it has no London session instead of firing",
      aid not in [f.alert_id for f in out["fired"]]
      and note == "AAPL trades only in the day session, so it has no London session."
      and not [e for e in out["errors"] if e["symbol"] == "AAPL"], (note, out["errors"]))
COIN = sc.coin_series(600)
FEED["BTC-USD"] = COIN
aid = add("BTC-USD", "gap_fills")
alerts.check(conn)
check("and a coin says its day opens with no gap",
      "round the clock" in (row(aid)["last_error"] or ""), row(aid)["last_error"])
FEED["YM=F"] = sc.walk(sc.daily_times(300), seed=3, kind="future", interval="1d",
                       symbol="YM=F")
aid = add("YM=F", "takes_session_range", interval="1d")
alerts.check(conn)
check("daily bars have no sessions, and the alert says so",
      "shorter than a day" in (row(aid)["last_error"] or ""), row(aid)["last_error"])

# ---------------------------------------------------------------------------
head("THE LIVE PRICE PAST YESTERDAY'S HIGH OR LOW")
# ---------------------------------------------------------------------------
before = clock.head(ES, SWEEP1)              # day 2, the bar before 09:00 finished
moment = int(TS[SWEEP1]) + 60
aid = add("ES=F", "takes_prior_high")
FEED["ES=F"] = live(before, 100.5, moment)
check("quiet below it", told(aid) is None and row(aid)["state"] == "clear", row(aid)["state"])
FEED["ES=F"] = live(before, 101.2, moment)
said = told(aid)
print("   " + str(said))
check("told when it trades above",
      said == "ES=F is at 101.2, above yesterday's high of 101. " + alerts.FACT, said)
FEED["ES=F"] = live(before, 101.25, moment)
check("quiet while it stays above", told(aid) is None)
FEED["ES=F"] = live(before, 100.9, moment)
check("clears when it is back below", told(aid) is None and row(aid)["state"] == "clear")
FEED["ES=F"] = live(before, 101.1, moment)
check("and is told again when it crosses again", told(aid) is not None)

# The first bar of a new day still forming: the finished bars all belong to
# day 1, and its high is yesterday's, not the day before's.
day1 = clock.head(ES, FIRST[1])
aid = add("ES=F", "takes_prior_high")
FEED["ES=F"] = live(day1, 101.2, int(TS[FIRST[1]]) + 60)
said = told(aid)
check("from the first minute of a new day, yesterday is the day that just ended",
      said is not None and "yesterday's high of 101" in said, said)
aid = add("ES=F", "takes_prior_high")
FEED["ES=F"] = live(before, 101.2, moment)
told(aid)
day3 = clock.head(ES, FIRST[2] + 1)
FEED["ES=F"] = live(day3, 101.6, int(TS[FIRST[2] + 1]) + 60)
said = told(aid)
check("still above at the next day's first check, it is past a new yesterday's high, and told",
      said is not None and "yesterday's high of 101.3" in said, said)
check("once", told(aid) is None)
aid = add("ES=F", "takes_prior_low")
FEED["ES=F"] = live(before, 99.8, moment)
said = told(aid)
check("and below yesterday's low", said is not None and "below yesterday's low of 99.9" in said,
      said)

# ---------------------------------------------------------------------------
head("BREAKS, GAPS AND FILLS ON A WALK")
# ---------------------------------------------------------------------------
WALK = sc.walk(sc.futures_times(1500), seed=5, kind="future", vol=0.0008, price=6000.0,
               symbol="NQ=F")
S = liquidity.series(WALK)
trend_before = np.concatenate([[0], S["st_trend"][:-1]])
filled = S["ck_gap_filled"] == 1
EVENTS = {
    "structure_breaks": np.flatnonzero(S["st_flip"] != 0),
    "back_in_gap": np.flatnonzero((S["st_fvg_touch"] != 0) & (S["st_fvg_touch"] == trend_before)),
    "gap_fills": np.array([j for j in range(1, len(WALK)) if filled[j]
                           and (not filled[j - 1] or S["ck_day"][j] != S["ck_day"][j - 1])]),
}
WORDS = {"structure_breaks": "its last swing", "back_in_gap": "came back into the gap",
         "gap_fills": "filling the gap its day opened with"}
for kind, found in EVENTS.items():
    # An event with three quiet bars after it, and a later one.
    pick = [e for k, e in enumerate(found[:-1]) if found[k + 1] > e + 3 and e > 300
            and (k == 0 or found[k - 1] < e - 1)]
    if not pick:
        check("%s has events to test on" % kind, False, found[:10])
        continue
    e1 = int(pick[0])
    e2 = int(found[list(found).index(e1) + 1])
    aid = add("NQ=F", kind, since=WALK.timestamp[e1])
    FEED["NQ=F"] = clock.head(WALK, e1)
    quiet = told(aid) is None
    FEED["NQ=F"] = clock.head(WALK, e1 + 3)
    said = told(aid)
    when = clock.label_ny(int(WALK.timestamp[e1])).rpartition(" ")[2]
    check("%s: told once, naming the symbol, a price and the New York time" % kind,
          quiet and said is not None and said.startswith("NQ=F ") and WORDS[kind] in said
          and ("%s New York time" % when) in said and re.search(r"\d,\d{3}", said)
          and said.endswith(alerts.FACT), said)
    again = told(aid)
    FEED["NQ=F"] = clock.head(WALK, e2)
    later = told(aid)
    FEED["NQ=F"] = clock.head(WALK, e2 + 1)
    next_one = told(aid)
    when2 = clock.label_ny(int(WALK.timestamp[e2])).rpartition(" ")[2]
    check("%s: quiet until the next one, then told again" % kind,
          again is None and later is None and next_one is not None
          and ("%s New York time" % when2) in next_one, (again, later, next_one))

# ---------------------------------------------------------------------------
head("HOW THEY READ AND WHERE THEY ARE OFFERED")
# ---------------------------------------------------------------------------
new = ("takes_prior_high", "takes_prior_low", "sweeps_prior_high", "sweeps_prior_low",
       "gap_fills", "structure_breaks", "back_in_gap", "takes_session_range")
check("eight price-action kinds, none needing a value, grouped",
      all(k in alerts.KINDS and not alerts.KINDS[k]["needs_threshold"]
          and alerts.KINDS[k]["group"] == "Price action" for k in new)
      and all(spec.get("group") in alerts.GROUPS for spec in alerts.KINDS.values()))
check("each described with its capitals kept",
      alerts.describe("takes_session_range", None, "ES=F")
      == "ES=F trades outside London's range between 08:30 and 12:00 New York time"
      and alerts.describe("crosses_ma", None, "X") == "X closes above its 50-bar average")

page = c.get("/monitor").data.decode("utf-8", "replace")
groups = re.findall(r'<optgroup label="([^"]+)">', page)
check("the Monitor form lists the kinds in groups",
      groups == list(alerts.GROUPS) and 'value="sweeps_prior_high"' in page
      and "the price action ones" in page, groups)

morning = clock.head(ES, at(1, "07:00"))
morning = live(morning, 100.0, int(TS[at(1, "07:00")]))
sug = {s["kind"]: s for s in alerts.suggest_for("ES=F", None, morning)}
check("an analysis suggests the sweep of the nearer of yesterday's levels, and a break",
      "sweeps_prior_low" in sug and "sweeps_prior_high" not in sug
      and "99.9" in sug["sweeps_prior_low"]["why"] and "structure_breaks" in sug, list(sug))
check("each with an empty value, never None",
      all(sug[k]["form_value"] == "" for k in ("sweeps_prior_low", "structure_breaks")))
gappy = None
for j in range(400, len(WALK)):
    if np.isfinite(S["ck_gap_lo"][j]) and not S["ck_gap_filled"][j]:
        gappy = j
        break
sug = {s["kind"]: s for s in alerts.suggest_for(
    "NQ=F", None, live(clock.head(WALK, gappy + 1), float(WALK.close[gappy]),
                       int(WALK.timestamp[gappy]) + STEP))} if gappy else {}
check("and the gap filling while today's gap is open", "gap_fills" in sug
      and sug["gap_fills"]["form_value"] == "", list(sug))
check("nothing new when the bars cannot be read that way",
      [s["kind"] for s in alerts.suggest_for("EEE", None, object())]
      == ["pct_down", "volume_spike", "near_52w_high"])

# The analysis page, on the scripted future: the suggestions render with an
# empty value.
real = (engine.resolve, engine.fetch_bars, catalysts.fetch)
engine.resolve = lambda s: {"symbol": s, "name": "E-mini S&P 500"}
engine.fetch_bars = lambda s, iv: dataclasses.replace(
    sc.walk(sc.futures_times(1500), seed=5, kind="future", vol=0.0008, price=6000.0),
    live_price=None, meta=dict(ES.meta))
catalysts.fetch = lambda s: (_ for _ in ()).throw(RuntimeError("no calendar in tests"))
try:
    r = c.get("/analyse?symbol=ES%3DF&interval=5m&news=0")
    page = r.data.decode("utf-8", "replace")
finally:
    engine.resolve, engine.fetch_bars, catalysts.fetch = real
forms = re.findall(r'<input type="hidden" name="kind" value="([a-z_0-9]+)">\s*'
                   r'<input type="hidden" name="threshold" value="([^"]*)">', page)
check("the analysis page offers them", r.status_code == 200
      and ("structure_breaks", "") in forms, (r.status_code, forms))
check("with no None anywhere a visitor reads", ">None<" not in page
      and 'value="None"' not in page and "nan" not in re.sub(r"<[^>]+>", " ", page).split(),
      [f for f in forms if f[1] == "None"])
detail = page[page.find("<h2>Strategy detail</h2>"):page.find("What the columns and words mean")]
rows = re.findall(r"<tr>\s*<td>([^<]+)</td>", detail)
check("the strategy table has its twelve rows, and nothing on the page speaks of a trial",
      len(rows) == 12 and not web.strategies_mod.TRIAL
      and "on trial" not in page.lower(), rows[:3])
check("the Liquidity row is among the families", re.search(r"<td>Liquidity</td>", page))

conn.close()
print()
print("%d failed" % len(fails) if fails else "ALL PASSED")
_sys.exit(1 if fails else 0)
