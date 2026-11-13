"""Portfolio risk arithmetic, with no network: every price is set by hand."""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
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
print("%d failure(s)" % len(fails))
print("PORTFOLIO RISK OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
