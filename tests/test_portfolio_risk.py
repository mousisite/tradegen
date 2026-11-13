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
DB = harness.isolate("journal")[0]
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


real = (P.fetch_bars, P.fund.load, P.risk_mod.profile)
P.fetch_bars, P.fund.load, P.risk_mod.profile = split_quote, offline, offline
try:
    v = P.build([{"id": 9, "symbol": "SPL", "direction": 1, "quantity": 2,
                  "entry": 1000, "stop": 950, "target1": 1200, "ts_opened": opened}],
                account=10000, with_correlation=False)
finally:
    P.fetch_bars, P.fund.load, P.risk_mod.profile = real
spl = v.positions[0]
check("it is priced in the shares it was opened with", close(spl.price, 1010.0), spl.price)
check("so it is not past its stop", not spl.breached and not v.breached)
check("its open profit is what it is", close(spl.open_pnl, 20.0), spl.open_pnl)
check("and the page says why its price is not today's quote",
      any("split 10-for-1" in o for o in v.observations), v.observations)

print()
print("%d failure(s)" % len(fails))
print("PORTFOLIO RISK OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
