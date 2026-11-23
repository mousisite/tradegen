"""The size calculator and the survival check, in sums and on their pages.

No network. Sizes round down, never up, so the stop loses at most the risk
set: whole shares, coins to six decimals, whole futures contracts, and when
not even one contract fits, the page says so and names the micro contract.
The fixed-contract table and the break-even hit rate are exact. The survival
check is the same every time for the same seed, a bigger risk never makes a
50% fall less likely on a losing method, and it uses a person's own journal
only from 20 closed trades. Both pages open for anyone, in the iPhone app too.
"""
import math
import os as _os
import re
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for _key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SMTP_HOST", "STRIPE_SECRET_KEY",
             "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(_key, None)

c, DB = harness.isolated_web("risktools")

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import database as db  # noqa: E402
from bot import journal  # noqa: E402

web._example_state["busy"] = True
web.app.config["AUTH_MODE"] = "google"

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


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) < tol


def flat(response):
    return " ".join(response.data.decode("utf-8", "replace").split())


def refused(fn, words, *a, **k):
    try:
        fn(*a, **k)
    except ValueError as exc:
        return words in str(exc)
    return False


# ---------------------------------------------------------------------------
head("SIZE: SHARES, COINS AND CONTRACTS, ROUNDED DOWN")
# ---------------------------------------------------------------------------
s = journal.size(10000, 1, 100, 97, "shares")
check("shares are whole, rounded down: 100 / 3 is 33", s["quantity"] == 33
      and close(s["at_risk"], 99.0) and close(s["pct"], 0.99) and s["direction"] == 1, s)
s = journal.size(10000, 1, 100, 103, "shares")
check("a short has its stop above", s["direction"] == -1 and s["quantity"] == 33
      and close(s["one_r"]["price"], 97.0))
s = journal.size(10000, 1, 60000, 58500, "coins")
check("coins to six decimals, rounded down", close(s["quantity"], 0.066666)
      and s["quantity_text"] == "0.066666", s["quantity"])
check("and never risk more than the setting", s["at_risk"] <= 100.0 + 1e-9)
s = journal.size(10000, 1, 6000, 5990, "futures", "MES")
check("contracts are whole: 2 MES at a 10-point stop risk $100",
      s["quantity"] == 2 and close(s["at_risk"], 100.0) and close(s["per_unit"], 50.0), s)
s = journal.size(10000, 1, 6000, 5990, "futures", "ES")
check("when not even one ES fits, it says so", not s["fits"] and s["quantity"] == 0
      and "Not even one ES contract fits" in s["why_none"], s["why_none"])
check("and names the micro contract and what one risks",
      "The micro contract MES is a tenth of the size: one risks $50.00" in s["why_none"])
s = journal.size(10000, 1, 6000, 5990, "futures", "MES", cost_bps=0)
check("the same stop on MES fits", s["fits"])
s = journal.size(5000, 1, 20000, 19950, "futures", "MNQ")
check("a micro that does not fit has no smaller one to name", not s["fits"]
      and "micro contract" not in s["why_none"])
s = journal.size(10000, 2, 80.0, 79.5, "futures", "own", point_value=1000)
check("a contract of your own, by its dollars a point", s["quantity"] == 0
      and "one risks $500.00" in s["why_none"])
s = journal.size(100000, 2, 80.0, 79.5, "futures", "own", point_value=1000)
check("which fits on a larger account", s["quantity"] == 4)
check("one with no dollars a point is refused", refused(
    journal.size, "dollars per point", 10000, 1, 80, 79, "futures", "own"))
down = journal.size(10000, 1, 10, 16)
check("a short is told no price at or below zero", close(down["one_r"]["price"], 4.0)
      and down["two_r"]["price"] is None, down)
check("nor is one at exactly zero", journal.size(10000, 1, 10, 20)["one_r"]["price"] is None)
check("a long always has both", close(journal.size(10000, 1, 10, 1)["two_r"]["price"], 28.0))
check("a hit rate of 0% or 100% is refused by the rule it breaks", all(refused(
    journal.survival, "more than 0% and less than 100%", p=p, win=1.5, loss=1.0)
    for p in (0.0, 1.0)))
check("a stop at the entry is refused", refused(journal.size, "away from the entry",
                                                10000, 1, 100, 100))
check("a target on the wrong side is refused", refused(
    journal.size, "winning side", 10000, 1, 100, 99, target=98))
for label, args, extra in (
        ("account", (1e308, 100, 10, 9), {}),
        ("account in coins", (1e305, 1, 10, 9, "coins"), {}),
        ("contract", (10000, 1, 10, 1, "futures", "own"), {"point_value": 1e308}),
        ("tiny contract", (10000, 1, 10, 9, "futures", "own"), {"point_value": 1e-320}),
        ("contract worth nothing a point", (10000, 1, 1.001, 1, "futures", "own"),
         {"point_value": 5e-324}),
        ("cost", (10000, 1, 10, 9), {"cost_bps": 1e308}),
        ("target", (10000, 1, 10, 9.9999), {"target": 1.7e308})):
    check("a number too big or small to work with is refused: %s" % label, refused(
        journal.size, "too large or too small", *args, **extra))
check("an account too small to work a share of out is refused too", refused(
    journal.fixed_contracts, "too small", 1e-320))
check("a risk over 100% is refused", refused(journal.size, "at most 100%",
                                             10000, 150, 100, 99))

# ---------------------------------------------------------------------------
head("WHAT IT COSTS, AND THE HIT RATE IT NEEDS")
# ---------------------------------------------------------------------------
s = journal.size(10000, 1, 100, 99, "shares", cost_bps=10, target=103)
check("the cost in R is the price's cost over the stop distance",
      close(s["cost_r"], 100 * 10 / 10000.0 / 1.0), s["cost_r"])
needed = {round(r["reward"], 6): r["needed"] for r in s["breakeven"]}
check("break-even is (1 + c) / (1 + R)", close(needed[2.0], 1.1 / 3.0)
      and close(needed[1.0], 1.1 / 2.0), needed)
check("and is worked out for the target too", close(needed[3.0], 1.1 / 4.0)
      and any(r["target"] for r in s["breakeven"]) and close(s["target_r"], 3.0))
check("1R and 2R after costs", close(s["one_r"]["cash"], 100 * (1 - 0.1))
      and close(s["two_r"]["cash"], 100 * (2 - 0.1)))
check("breakeven_for alone", close(journal.breakeven_for(2.0, 0.0), 1 / 3.0))

# ---------------------------------------------------------------------------
head("FIXED CONTRACTS")
# ---------------------------------------------------------------------------
f = journal.fixed_contracts(10000, "MES", 3, (16, 28, 35))
pcts = [round(r["pct"], 4) for r in f["rows"]]
check("3 MES with a 16, 28 or 35 tick stop on $10,000 risk 0.6%, 1.05% and 1.3125%",
      pcts == [0.6, 1.05, 1.3125] and close(f["tick_value"], 1.25), pcts)
f = journal.fixed_contracts(50000, "ES", 1, (8,))
check("one ES, 8 ticks: $100, 0.2%", close(f["rows"][0]["cash"], 100.0)
      and close(f["rows"][0]["pct"], 0.2))
check("every contract has a point value and a tick",
      all(v[1] > 0 and v[2] > 0 for v in journal.CONTRACTS.values())
      and all(m in journal.CONTRACTS for m in journal.MICRO.values()))

# ---------------------------------------------------------------------------
head("SURVIVAL")
# ---------------------------------------------------------------------------
a = journal.survival(p=0.4, win=1.5, loss=1.0)
b = journal.survival(p=0.4, win=1.5, loss=1.0)
check("the same seed gives the same answer", a == b)
other = journal.survival(p=0.4, win=1.5, loss=1.0, seed=8)
check("another seed, other draws", other["rows"] != a["rows"])
check("2,000 runs of 100 trades, six risks",
      a["paths"] == 2000 and a["trades"] == 100
      and [r["risk"] for r in a["rows"]] == [0.5, 1.0, 2.0, 3.0, 5.0, 10.0])
losing = journal.survival(p=0.35, win=1.5, loss=1.0)
fall50 = [r["fall50"] for r in losing["rows"]]
fall20 = [r["fall20"] for r in losing["rows"]]
ends = [r["median_end"] for r in losing["rows"]]
check("on a losing method a bigger risk never makes a 50% fall less likely",
      all(x <= y for x, y in zip(fall50, fall50[1:])), fall50)
check("nor a 20% one", all(x <= y for x, y in zip(fall20, fall20[1:])), fall20)
check("and the typical end only gets lower", all(x >= y for x, y in zip(ends, ends[1:])),
      ends)
check("at 10% a 50% fall is nearly certain on it", fall50[-1] > 0.9, fall50[-1])
check("a losing run is counted", losing["longest_losing_run"] >= 5)
check("the source is said", losing["source"]["from"] == "typed"
      and close(losing["source"]["avg_r"], 0.35 * 1.5 - 0.65))
drawn = journal.survival(rs=[2.0, -1.0, -1.0, 0.5])
check("results can be drawn from a record", drawn["source"]["from"] == "journal"
      and drawn["source"]["trades"] == 4 and close(drawn["source"]["avg_r"], 0.125))
check("a hit rate of 100% is refused", refused(journal.survival, "more than 0% and less than 100%",
                                               p=1.0, win=1, loss=1))
check("a balance cannot go under zero", all(
    r["median_end"] >= 0 for r in journal.survival(p=0.1, win=1, loss=20)["rows"]))

# ---------------------------------------------------------------------------
head("YOUR OWN JOURNAL, FROM 20 TRADES")
# ---------------------------------------------------------------------------
conn = db.connect(DB)
fay = A.upsert_email_user(conn, "fay@example.com")
COSTS = {"equity": 0.0002, "crypto": 0.002, "futures": 0.0001}
for k in range(19):
    tid = db.open_trade(conn, "ZZS", "1d", 1, 100.0, 99.0, user=fay.id)
    db.close_trade(conn, tid, 102.0 if k % 3 == 0 else 99.0, user=fay.id)
check("19 closed trades are not enough", journal.survival_from_journal(
    conn, fay.id, COSTS) is None)
with c.session_transaction() as sess:
    sess[web.SESSION_KEY] = A.start_session(conn, fay.id, "test")
page = flat(c.get("/tools/survival?source=journal"))
check("so the page uses the numbers typed, and says how many are needed",
      "A trade wins 45%" in page and "With 20 or more closed trades" in page
      and "you have 19" in page)
tid = db.open_trade(conn, "ZZS", "1d", 1, 100.0, 99.0, user=fay.id)
db.close_trade(conn, tid, 102.0, user=fay.id)
own = journal.survival_from_journal(conn, fay.id, COSTS)
check("20 are", own is not None and own["source"]["trades"] == 20)
page = flat(c.get("/tools/survival?source=journal"))
check("and the page draws from them", "Drawn from your 20 closed trades, after costs"
      in page and "My journal: 20 closed trades" in page)

# ---------------------------------------------------------------------------
head("BOTH PAGES OPEN FOR ANYONE")
# ---------------------------------------------------------------------------
visitor = web.app.test_client()
r = visitor.get("/tools/size")
check("the size tool opens for a visitor", r.status_code == 200
      and "How big a position?" in flat(r))
page = flat(visitor.get("/tools/size?account=10000&risk=1&entry=100&stop=97&kind=shares"))
check("and sizes shares", re.search(r'class="v fig">33<', page) is not None
      and "0.99% of the account" in page)
page = flat(visitor.get("/tools/size?account=10000&risk=1&entry=6000&stop=5990&kind=futures"
                        "&contract=ES"))
check("and says when no contract fits", "Not even one ES contract fits" in page
      and "MES" in page)
page = flat(visitor.get("/tools/size"))
check("the fixed-contract table is there from the start",
      "3 MES" in page and "0.60%" in page and "1.05%" in page and "1.31%" in page, page)
r = visitor.get("/tools/size?entry=abc&stop=97")
check("a word where a number goes is said plainly", r.status_code == 200
      and "The entry must be a number." in flat(r))
r = visitor.get("/tools/size?entry=100&stop=100")
check("as is a stop at the entry", "The stop must be away from the entry." in flat(r))
for query in ("account=1e308&risk=100&entry=10&stop=9",
              "account=1e305&entry=10&stop=9&kind=coins",
              "entry=10&stop=9&kind=futures&contract=own&point_value=1e-320"):
    r = visitor.get("/tools/size?" + query)
    check("numbers too big to size are said plainly, not a broken page: %s" % query,
          r.status_code == 200 and "too large or too small to size" in flat(r),
          r.status_code)
r = visitor.get("/tools/survival")
page = flat(r)
check("the survival check opens for a visitor, worked out on its example numbers",
      r.status_code == 200 and "Fell 50% from a high" in page and "of runs" in page
      and "Signed in, with 20 or more" in page)
r = visitor.get("/tools/survival?hit=150")
check("a hit rate over 100% is said plainly", "more than 0% and less than 100%" in flat(r))
page = flat(visitor.get("/tools/size?entry=10&stop=16"))
check("a short that cannot make 2R says so, and shows no price below zero",
      "At 4 the trade makes 1R" in page and "A short from here cannot make 2R" in page
      and "at -2" not in page, page)
check("the dollars a point read as money", "Micro E-mini Dow, $0.50 a point" in page
      and "$1,000 a point" in page and "$0.5 a" not in page, page)
page = flat(visitor.get("/tools/size?entry=10&stop=25"))
check("nor one that cannot make 1R", "A short from here cannot make 1R" in page
      and "at -" not in page, page)
real_cfg = web._cfg
for account, shown in ((1234567, "1234567"), (125430.75, "125430.75"), (10000, "10000")):
    web._cfg = lambda: dict(real_cfg(), account_size=account)
    try:
        page = visitor.get("/tools/size").data.decode("utf-8")
    finally:
        web._cfg = real_cfg
    check("an account of %s is filled in as it is" % shown,
          'id="account" name="account" type="number" step="any" min="0" '
          'inputmode="decimal" value="%s"' % shown in " ".join(page.split()))
for path in ("/tools/size", "/tools/survival"):
    check("%s keeps its help out of its headings" % path, not re.search(
        r"<h2[^>]*>(?:(?!</h2>).)*<details", flat(visitor.get(path))))
for path in ("/tools/size?entry=100&stop=97", "/tools/survival"):
    page = flat(visitor.get(path))
    check("%s has nothing unfinished" % path, ">None<" not in page
          and not re.search(r"\bnan\b", page) and "undefined" not in page)
APP = {"User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Mobile/15E148 OrenthApp/1"}
for path in ("/tools/size", "/tools/survival"):
    page = visitor.get(path, headers=APP).data.decode("utf-8")
    outside = [h for h in re.findall(r'<a\b[^>]*\bhref="(https?://[^"]+)"', page)
               if not h.startswith("http://localhost/")]
    check("%s links nowhere outside the app" % path, not outside, outside)
check("both are in the sitemap", all(p in [x[0] for x in web.PUBLIC_PAGES]
                                     for p in ("/tools/size", "/tools/survival")))
conn.close()

print()
print("%d failure(s)" % len(fails))
if fails:
    for f in fails:
        print("   - " + f)
    _sys.exit(1)
print("RISK TOOLS OK")
