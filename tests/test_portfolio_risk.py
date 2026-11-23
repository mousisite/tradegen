"""Portfolio risk arithmetic, with no network: every price is set by hand."""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from bot import portfolio as P  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def pos(direction, entry, stop, price, qty=10.0, symbol="X", asset="equity", target=None):
    p = P.Position(trade_id=1, symbol=symbol, direction=direction, quantity=qty,
                   entry=entry, stop=stop, target=target, opened=0)
    p.price = price
    p.asset_class = asset
    return p


def close(a, b):
    return abs(a - b) < 1e-9


print("=" * 72)
print("ONE POSITION")
print("=" * 72)
p = pos(1, 100, 95, 110)
P.mark(p)
check("a long in profit: P&L and R", close(p.open_pnl, 100) and close(p.open_r, 2.0), (p.open_pnl, p.open_r))
check("planned risk is entry to stop", close(p.planned_risk, 50))
check("still at risk is from today's price to the stop", close(p.risk_remaining, 150))
check("and it is not breached", not p.breached and p.status == "open")

p = pos(1, 100, 95, 94)
P.mark(p)
check("a long below its stop is breached, not 'nothing at risk'",
      p.breached and p.status == "stop_breached" and close(p.open_r, -1.2), (p.breached, p.open_r))
check("its loss is in the P&L, not hidden", close(p.open_pnl, -60))

p = pos(1, 100, 95, 95)
P.mark(p)
check("a long exactly at its stop counts as breached", p.breached)

p = pos(-1, 100, 105, 90, qty=5)
P.mark(p)
check("a short in profit: P&L, R and remaining risk",
      close(p.open_pnl, 50) and close(p.open_r, 2.0) and close(p.risk_remaining, 75)
      and close(p.planned_risk, 25), (p.open_pnl, p.open_r, p.risk_remaining, p.planned_risk))

p = pos(-1, 100, 105, 106, qty=5)
P.mark(p)
check("a short above its stop is breached", p.breached and close(p.open_pnl, -30))

p = pos(1, 100, 105, 110)
P.mark(p)
check("a stop trailed above the entry plans no risk and locks in a profit",
      close(p.planned_risk, 0) and close(p.risk_remaining, 50) and not p.breached)

p = pos(1, 100, 100, 101)
P.mark(p)
check("a stop at the entry has no R", not p.stop_valid and p.open_r is None)

p = pos(1, 100, 95, 85)
P.mark(p)
check("a gap through the stop shows the real loss, beyond 1R",
      p.breached and close(p.open_r, -3.0) and close(p.open_pnl, -150))

p = pos(1, 1000, 950, 1000, qty=1, asset="crypto")
P.mark(p, cost_bps=20)
check("getting out costs half the round trip", close(p.exit_cost, 1.0), p.exit_cost)

p = pos(1, 0.5, 0.4, 0.45, qty=12345.678, asset="crypto")
P.mark(p)
check("fractional quantities work", close(p.open_pnl, -0.05 * 12345.678))

print()
print("=" * 72)
print("A WHOLE PORTFOLIO")
print("=" * 72)
PRICES = {"UP": 110.0, "DOWN": 85.0, "SHORT": 90.0, "GONE": None}
real_enrich = P._enrich


def fake_enrich(positions, view):
    for q in positions:
        q.price = PRICES[q.symbol]
        q.asset_class = "equity"
        if q.price is None:
            view.failed.append(q.symbol)


P._enrich = fake_enrich
try:
    rows = [
        {"id": 1, "symbol": "UP", "direction": 1, "quantity": 10, "entry": 100,
         "stop": 95, "target1": 120, "ts_opened": 0},
        {"id": 2, "symbol": "DOWN", "direction": 1, "quantity": 10, "entry": 100,
         "stop": 95, "target1": 120, "ts_opened": 0},
        {"id": 3, "symbol": "SHORT", "direction": -1, "quantity": 5, "entry": 100,
         "stop": 105, "target1": 80, "ts_opened": 0},
        {"id": 4, "symbol": "GONE", "direction": 1, "quantity": 1, "entry": 10,
         "stop": 9, "target1": None, "ts_opened": 0},
    ]
    v = P.build(rows, account=10000, with_correlation=False,
                cost_bps={"equity": 2.0})
finally:
    P._enrich = real_enrich

check("the breached position is listed on its own",
      [q.symbol for q in v.breached] == ["DOWN"], [q.symbol for q in v.breached])
check("its open loss is counted", close(v.breached_pnl, -150))
expected = 150 + 75 + (1100 + 450) * 2 / 10000 / 2
check("still at risk counts only stops still ahead, plus the cost of getting out",
      close(v.total_risk, expected), (v.total_risk, expected))
check("planned risk is the day-one total", close(v.planned_risk, 50 + 50 + 25))
check("the breach is the first warning, and says it needs doing",
      v.warnings and v.warnings[0].startswith("Stop breached on DOWN") and "not an order" in v.warnings[0],
      v.warnings[:1])
check("a gap past 1R is called out", any("more than its planned risk" in w for w in v.warnings))
check("the summary never says nothing is at risk while a stop is breached",
      not any("the loss would be 0" in o for o in v.observations))
check("an unpriced position is excluded and said so",
      "GONE" in v.failed and any("No price available for GONE" in w for w in v.warnings))

print()
print("=" * 72)
print("MORE THAN ONE ROW, NO SIZE, NO PRICE")
print("=" * 72)
PRICES.update({"AAA": 10.0, "BBB": 10.0, "NOSIZE": 10.0, "DEAD": None})
P._enrich = fake_enrich
try:
    rows = [{"id": i, "symbol": "AAA", "direction": 1, "quantity": 300, "entry": 10,
             "stop": 9, "target1": None, "ts_opened": 0} for i in (1, 2, 3)]
    rows.append({"id": 4, "symbol": "BBB", "direction": 1, "quantity": 100, "entry": 10,
                 "stop": 9, "target1": None, "ts_opened": 0})
    rows.append({"id": 5, "symbol": "NOSIZE", "direction": 1, "quantity": None, "entry": 10,
                 "stop": 9, "target1": None, "ts_opened": 0})
    rows.append({"id": 6, "symbol": "DEAD", "direction": 1, "quantity": 50, "entry": 10,
                 "stop": 9, "target1": None, "ts_opened": 0})
    v = P.build(rows, account=10000, with_correlation=False)
finally:
    P._enrich = real_enrich
check("three journal rows in one stock are one position for concentration",
      abs(v.concentration - (0.9 ** 2 + 0.1 ** 2)) < 1e-9, v.concentration)
check("and the one-stock warning names it",
      any("AAA alone is 90%" in w for w in v.warnings), v.warnings)
check("a position with no size is left out of the money, and said so",
      close(v.invested, 10000) and any("No size is logged for NOSIZE" in w for w in v.warnings))
check("an unpriced holding's cost stays out of cash",
      close(v.cash, max(0.0, 10000 - 10000 - 500)), v.cash)


def whole(prices, rows, betas=None, account=10000, asset="equity", cost_bps=None,
          with_correlation=False):
    """A portfolio of rows at the given prices and betas, nothing fetched."""
    def enrich(positions, view):
        for q in positions:
            q.price, q.asset_class = prices[q.symbol], asset
            q.beta = (betas or {}).get(q.symbol)
            if q.price is None:
                view.failed.append(q.symbol)
    real, P._enrich = P._enrich, enrich
    try:
        return P.build(rows, account=account, with_correlation=with_correlation,
                       cost_bps=cost_bps)
    finally:
        P._enrich = real


def held(i, symbol, direction=1, qty=10, entry=100, stop=90):
    return {"id": i, "symbol": symbol, "direction": direction, "quantity": qty,
            "entry": entry, "stop": stop, "target1": None, "ts_opened": 0}


up, down = whole({"AAA": 200.0}, [held(1, "AAA")]), whole({"AAA": 50.0}, [held(1, "AAA")])
check("cash is what was not paid out, whatever the holding is worth now",
      close(up.cash, 9000) and close(down.cash, 9000), (up.cash, down.cash))
check("and deployed is still today's value", close(up.invested, 2000) and close(down.invested, 500))
part = whole({"AAA": 60.0, "GONE": None}, [held(1, "AAA"), held(2, "GONE", qty=5)])
check("with a price missing too", close(part.cash, 10000 - 1000 - 500), part.cash)
mes = whole({"MES=F": 6000.0}, [held(1, "MES=F", qty=5, entry=6000, stop=5980)], asset="other")
check("a futures contract is held on a deposit, so its full value is not taken out of cash",
      close(mes.cash, 10000) and close(mes.gross_exposure, 30000)
      and any("held on a deposit" in o for o in mes.observations), (mes.cash, mes.observations))

# A source that answers slowly holds the page for so long and no longer.
import time as _time  # noqa: E402

real_slow = (P.fetch_bars, P.READ_FOR, P.fund.load, P.risk_mod.profile)
asked_prices = []


def slow_prices(symbol, interval="1d", lookback=None):
    asked_prices.append(symbol)
    _time.sleep(0.2)
    raise P.DataError("no prices")


P.fetch_bars, P.READ_FOR = slow_prices, 0.5
try:
    began = _time.time()
    v = P.build([held(n, "S%03d" % n) for n in range(200)], account=10000)
    took = _time.time() - began
finally:
    P.fetch_bars, P.READ_FOR, P.fund.load, P.risk_mod.profile = real_slow
check("two hundred positions and a slow source do not hold the page for minutes",
      took < 3 and len(asked_prices) < 60 and len(v.failed) == 200,
      (took, len(asked_prices), len(v.failed)))

# The day a stop was crossed is the same on every page: New York's, or a daily
# bar's own.
from types import SimpleNamespace as _NS  # noqa: E402

midnight = 1795219200                                  # 21 Nov 2026 00:00 UTC
daily = _NS(timestamp=[midnight - 86400, midnight])
check("a crossing in the New York evening is that day's, as in the journal",
      P.day_words(P.crossed_day("5m", daily, midnight)) == "20 Nov"
      and P.day_words(P.crossed_day("1d", daily, midnight + 5400)) == "20 Nov")
check("and a daily bar keeps its own day",
      P.day_words(P.crossed_day("1d", daily, midnight)) == "21 Nov"
      and P.crossed_day("1d", daily, None) is None)

v = whole({"AAA": 100.0, "NEW": 100.0}, [held(1, "AAA"), held(2, "NEW")], {"AAA": 1.5})
check("a holding with no beta counts as one that moves with the market, as in the shock table",
      close(v.portfolio_beta, 1.25) and close(P.what_if(v, -0.10)["total_change"], -250),
      (v.portfolio_beta, P.what_if(v, -0.10)["total_change"]))
check("and is named", any("No beta could be measured for NEW, so it is counted" in o
                          for o in v.observations), v.observations)
check("with no beta at all there is no weighted beta",
      whole({"AAA": 100.0}, [held(1, "AAA")]).portfolio_beta is None)
v = whole({"AAA": 100.0}, [held(1, "AAA", direction=-1, stop=110)], {"AAA": 0.6})
check("a portfolio short on balance is said to move against the market",
      any("Weighted beta is -0.60, so the portfolio tends to move 60% as much as the "
          "market, in the opposite direction." == o for o in v.observations), v.observations)
v = whole({"AAA": 100.0}, [held(1, "AAA")], {"AAA": -0.003})
check("a beta that rounds to nothing is 0.00, with no minus sign",
      any(o.startswith("Weighted beta is 0.00,") for o in v.observations), v.observations)
v = whole({"AAA": 100.0}, [held(1, "AAA")], {"AAA": 0.6})
check("and one that is long is not",
      any(o.endswith("60% as much as the market.") for o in v.observations), v.observations)

CFG_COSTS = {"equity": 2.0, "crypto": 20.0, "other": 5.0, "futures": 1.0}
v = whole({"AAA": 250.0}, [held(1, "AAA", direction=-1, entry=250, stop=260)],
          cost_bps=CFG_COSTS)
said = next(o for o in v.observations if o.startswith("If every stop"))
check("trading costs that round to nothing are not mentioned, and a short is not above its stop",
      said == "If every stop still ahead were hit from here, the positions not yet at "
              "their stops would lose a further 100, or 1.0% of the account.", said)
v = whole({"AAA": 250.0}, [held(1, "AAA", qty=4000, entry=250, stop=240)], cost_bps=CFG_COSTS)
check("and costs that do not are",
      any(o.endswith("of the account, including about 100 in trading costs to get out.")
          for o in v.observations), v.observations)
fut = [held(1, "ES=F", qty=50, entry=6000, stop=5900)]
check("a futures contract pays the futures cost to get out, as in the journal",
      close(whole({"ES=F": 6000.0}, fut, asset="other", cost_bps=CFG_COSTS).exit_costs, 15.0))
check("anything else the source files under other pays that rate",
      close(whole({"EURUSD=X": 1.0}, [held(1, "EURUSD=X", qty=100, entry=1, stop=0.9)],
                  asset="other", cost_bps=CFG_COSTS).exit_costs, 0.025))

compared = []
real_matrix = P.risk_mod.correlation_matrix
P.risk_mod.correlation_matrix = lambda names, interval: compared.append(list(names)) or {
    "usable": True, "symbols": names, "average": 0.5, "notes": []}
try:
    many = ["S%03d" % n for n in range(P.COMPARE_MOST + 5)]
    v = whole({s: 10.0 for s in many},
              [held(n, s, qty=n + 1, entry=10, stop=9) for n, s in enumerate(many)],
              with_correlation=True)
finally:
    P.risk_mod.correlation_matrix = real_matrix
check("only the largest holdings are compared with each other, and the page says so",
      compared == [many[5:]] and v.correlation["notes"]
      == ["Compared between your 40 largest holdings, of 45."], (compared, v.correlation))

print()
print("=" * 72)
print("A STOP THAT TRADED AND RECOVERED")
print("=" * 72)
from types import SimpleNamespace as _NS
bars = _NS(timestamp=[100, 200, 300], low=[8.0, 9.5, 8.9], high=[11, 11, 11])
check("a low through the stop after opening counts as hit",
      P.stop_touched(bars, 150, 9.0, 1) == (300, 8.9))
check("a low from before the position opened does not",
      P.stop_touched(bars, 150, 8.5, 1) == (None, None))
check("for a short it is the high", P.stop_touched(bars, 0, 10.9, -1) == (100, 11.0))
p = pos(1, 10, 9, 9.6)
p.touched, p.touched_at = 8.9, 300
P.mark(p)
check("a position back above its stop after touching it is still breached", p.breached)

print()
print("=" * 72)
print("RETURNS ON THE DAYS BOTH TRADED")
print("=" * 72)
import numpy as np
from bot import risk as R
# A coin trades every day; a stock skips the weekend (days 5 and 6).
coin_close = np.array([100, 101, 102, 103, 104, 110, 120, 121], dtype=float)
coin = _NS(session_id=np.arange(8), close=coin_close)
stock = _NS(session_id=np.array([0, 1, 2, 3, 4, 7]), close=np.array([50, 50.5, 51, 51.5, 52, 59.0]))
rets = R.aligned_returns([coin, stock])
check("the coin's weekend move lands on the Monday the stock reopened",
      rets.shape == (2, 5) and abs(rets[0][-1] - (121 / 104 - 1)) < 1e-12
      and abs(rets[1][-1] - (59.0 / 52 - 1)) < 1e-12, str(rets.tolist()))

print()
print("=" * 72)
print("THE JOURNAL'S OWN NUMBERS")
print("=" * 72)
import harness
from bot import database as db
client, DB = harness.isolated_web("journal")
conn = db.connect(DB)
for bad, kw in (("a size of zero", {"quantity": 0}), ("a negative size", {"quantity": -5}),
                ("a stop of zero", {"stop": 0}), ("an infinite entry", {"entry": float("inf")}),
                ("a NaN target", {"target1": float("nan")})):
    args = dict(entry=100, stop=95, target1=110, quantity=10)
    args.update(kw)
    try:
        db.open_trade(conn, "AAA", "1d", 1, **args)
        refused = False
    except ValueError:
        refused = True
    check("%s is refused" % bad, refused)
t1 = db.open_trade(conn, "AAA", "1d", 1, entry=100, stop=90, quantity=10)
t2 = db.open_trade(conn, "BBB", "1d", 1, entry=100, stop=90, quantity=10)
t3 = db.open_trade(conn, "CCC", "1d", 1, entry=100, stop=90, quantity=10)
try:
    db.close_trade(conn, t1, -1)
    refused = False
except ValueError:
    refused = True
check("a negative exit price is refused", refused)
# Opened in the order 1, 2, 3, closed in the order 3, 1, 2.
conn.execute("UPDATE trades SET ts_opened=? WHERE id=?", (1, t1))
db.close_trade(conn, t3, 120)            # +2R, closed first
conn.execute("UPDATE trades SET ts_closed=1000 WHERE id=?", (t3,))
db.close_trade(conn, t1, 80)             # -2R
conn.execute("UPDATE trades SET ts_closed=2000 WHERE id=?", (t1,))
db.close_trade(conn, t2, 100)            # even
conn.execute("UPDATE trades SET ts_closed=3000 WHERE id=?", (t2,))
conn.commit()
plain = db.journal_summary(conn)
check("drawdown follows the order trades closed", close(plain["max_drawdown_r"], 2.0), plain["max_drawdown_r"])
check("an even trade is neither a win nor a loss",
      (plain["wins"], plain["losses"], plain["scratches"]) == (1, 1, 1))
net = db.journal_summary(conn, costs={"equity": 0.001})
check("after costs, every trade pays 0.01R here",
      close(net["expectancy_r"], plain["expectancy_r"] - 0.01) and net["after_costs"]
      and close(net["gross_expectancy_r"], plain["expectancy_r"]))
check("and the even trade becomes a small loss, as it would in an account",
      net["scratches"] == 0 and net["losses"] == 2)
conn.close()

print()
print("=" * 72)
print("A GAP IS MEASURED FROM THE STOP")
print("=" * 72)
from bot import decision as D
closes = np.array([100.0] * 40 + [90.0] + [100.0] * 10)       # one -10% day
gap = D._gap_risk(entry=100.0, stop=95.0, qty=10, account=100000, at_risk=50.0,
                  bars=_NS(close=closes))
check("a worst-day gap from a price resting at the stop",
      close(gap["gap_price"], 95.0 * 0.9) and close(gap["gap_loss"], (100 - 85.5) * 10), gap)

print()
print("=" * 72)
print("A POSITION OPENED BEFORE A TEN-FOR-ONE SPLIT")
print("=" * 72)
# Opened at 1,000 a share; the stock split ten for one; Yahoo now quotes 101
# and has rescaled the history. Read as is, the long was 90% down and past
# its stop.
split_at = 1_700_000_000
opened = split_at - 30 * 86400
stamps = [opened + k * 86400 for k in range(1, 40)]


def split_quote(symbol, interval="1d", lookback=None):
    return _NS(last_price=101.0, asset_class="equity", timestamp=stamps,
               low=[99.0] * len(stamps), high=[102.0] * len(stamps),
               splits=[(split_at, 10.0)])


def offline(*_a, **_k):
    raise RuntimeError("offline")


from bot import market as M  # noqa: E402


def no_history(symbol):
    raise M.DataError("the source did not answer")


# The bars here start after the position opened, so its split comes from the
# symbol's whole history; and when that cannot be had, from the bars.
real = (P.fetch_bars, P.fund.load, P.risk_mod.profile, M.split_history)
P.fetch_bars, P.fund.load, P.risk_mod.profile = split_quote, offline, offline
M.split_history = lambda symbol: [(split_at, 10.0)]
try:
    v = P.build([{"id": 9, "symbol": "SPL", "direction": 1, "quantity": 2,
                  "entry": 1000, "stop": 950, "target1": 1200, "ts_opened": opened}],
                account=10000, with_correlation=False)
    M.split_history = no_history
    fallback = P.build([{"id": 9, "symbol": "SPL", "direction": 1, "quantity": 2,
                         "entry": 1000, "stop": 950, "target1": 1200,
                         "ts_opened": opened}],
                       account=10000, with_correlation=False)
finally:
    P.fetch_bars, P.fund.load, P.risk_mod.profile, M.split_history = real
check("with no history to hand, the split in the bars still counts",
      close(fallback.positions[0].price, 1010.0), fallback.positions[0].price)
spl = v.positions[0]
check("it is priced in the shares it was opened with", close(spl.price, 1010.0), spl.price)
check("so it is not past its stop", not spl.breached and not v.breached)
check("its open profit is what it is", close(spl.open_pnl, 20.0), spl.open_pnl)
check("and the page says why its price is not today's quote",
      any("split 10-for-1" in o for o in v.observations), v.observations)

print()
print("=" * 72)
print("THE PAGE SAYS WHAT IS NOT KNOWN, NOT 0.00")
print("=" * 72)
import re as _re  # noqa: E402

conn = db.connect(DB)
conn.execute("DELETE FROM trades")
conn.commit()
for symbol, qty in (("KNOWN", 10), ("NOSIZE", None), ("DEAD", 50)):
    db.open_trade(conn, symbol, "1d", 1, entry=100, stop=95, target1=None, quantity=qty)
conn.close()


def page_prices(positions, view):
    for q in positions:
        q.price = {"KNOWN": 110.0, "NOSIZE": 90.0, "DEAD": None}[q.symbol]
        q.asset_class = "equity"
        if q.price is None:
            view.failed.append(q.symbol)


real = (P._enrich, P.risk_mod.correlation_matrix)
P._enrich, P.risk_mod.correlation_matrix = page_prices, offline
try:
    html = client.get("/portfolio").data.decode("utf-8")
finally:
    P._enrich, P.risk_mod.correlation_matrix = real


def cells(symbol):
    row = html[html.index("<td><b>%s</b></td>" % symbol):]
    row = row[:row.index("</tr>")]
    return [" ".join(_re.sub(r"<[^>]+>", " ", c).split())
            for c in _re.findall(r"<td[^>]*>(.*?)</td>", row, _re.S)], row


# Instrument, side, entry, now, value, open P&L, R, still at risk, weight.
known, _row = cells("KNOWN")
check("a holding with a size and a price shows its money",
      known[4:6] == ["1,100.00", "100.00"] and known[8] == "100%", known)
sizeless, row = cells("NOSIZE")
check("one with no size has a dash for its money and its weight",
      sizeless[4] == sizeless[5] == sizeless[8] == "—" and sizeless[6] == "-2.00", sizeless)
check("and one with no price too",
      cells("DEAD")[0][3:9] == ["—"] * 6, cells("DEAD")[0])
check("a dash is not coloured as a loss",
      'fig loss' not in cells("DEAD")[1], cells("DEAD")[1])
breach = " ".join(_re.sub(r"<[^>]+>", " ", html[html.index("Stop breached"):
                                                html.index("A stop in the journal")]).split())
check("the red box says the size was not logged, not that it lost -0.00",
      "stop of 95: size not logged (-2.00R)." in breach
      and "0.00 (" not in breach, breach)
check("and so does the figure beside it", "0.00 open" not in html)
holdings = html[html.index("<h2>Holdings</h2>"):]
holdings = holdings[:holdings.index("</table>")]
check("on a phone each holding is a card, every figure under its column's name",
      '<table class="stack tight five">' in holdings
      and _re.findall(r'data-label="([^"]+)"', cells("KNOWN")[1])
      == [h for h in _re.findall(r"<th[^>]*>(.*?)</th>", holdings) if h != "Instrument"],
      _re.findall(r'data-label="([^"]+)"', cells("KNOWN")[1]))
check("the shock table says what it leaves out",
      "so a genuine crash would hurt more than this straight-line estimate." in html)

# A long journal is read whole: nothing past the newest hundred went unsaid.
conn = db.connect(DB)
conn.execute("DELETE FROM trades")
conn.commit()
for n in range(105):
    db.open_trade(conn, "L%03d" % n, "1d", 1, entry=100, stop=95, target1=None, quantity=1)
conn.close()


def long_prices(positions, view):
    for q in positions:
        q.price, q.asset_class = 100.0, "equity"


asked = []
P._enrich = long_prices
P.risk_mod.correlation_matrix = offline
W = __import__("bot.workflows", fromlist=["workflows"])
real_build = P.build
P.build = lambda rows, *a, **k: asked.append((len(rows), k.get("cost_bps"))) or real_build(
    rows, *a, **k)
try:
    html = client.get("/portfolio").data.decode("utf-8")
    conn = db.connect(DB)
    W._check_positions(conn, __import__("web").config_mod.load(harness.CFG_PATH),
                       W.RunReport(name="positions", started=0, finished=0),
                       lambda _label: None, user=1)
    conn.close()
finally:
    P._enrich, P.risk_mod.correlation_matrix, P.build = real[0], real[1], real_build
check("every open position is on the page and in the routine, not the newest hundred",
      [n for n, _c in asked] == [105, 105] and "<b>L000</b>" in html and "<b>L104</b>" in html,
      [n for n, _c in asked])
check("and both charge a future its own cost",
      all(c and c.get("futures") == 1.0 for _n, c in asked), asked)

print()
print("%d failure(s)" % len(fails))
print("PORTFOLIO RISK OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
