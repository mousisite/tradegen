"""Portfolio analysis: what you actually own, and what could go wrong with it.

Positions come from the trade journal, so this reflects what was really taken
rather than a hypothetical allocation. Everything is marked to live prices.

The question this module exists to answer is not "how am I doing?" but "what
am I exposed to?". Those differ: a portfolio can be up while being one bad day
in a single sector away from giving it all back, and the profit-and-loss figure
says nothing about that.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from . import fundamentals as fund
from . import risk as risk_mod
from .market import DataError, fetch_bars


@dataclass
class Position:
    """One live holding, marked to market."""
    trade_id: int
    symbol: str
    direction: int
    quantity: float
    entry: float
    stop: float
    target: Optional[float]
    opened: int
    price: Optional[float] = None
    sector: str = ""
    asset_class: str = ""
    notional: float = 0.0
    open_pnl: float = 0.0
    open_r: Optional[float] = None
    risk_remaining: float = 0.0
    planned_risk: float = 0.0
    exit_cost: float = 0.0
    breached: bool = False
    touched: Optional[float] = None       # the price that crossed the stop since opening
    touched_at: Optional[int] = None
    beta_assumed: bool = False
    status: str = "open"
    beta: Optional[float] = None
    volatility: Optional[float] = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def stop_valid(self) -> bool:
        """A stop at the entry measures nothing, so R cannot be worked out."""
        return self.risk_per_unit > 0

    @property
    def sized(self) -> bool:
        """Whether a size was logged. Without one, cash figures are unknown."""
        return self.quantity > 0


@dataclass
class PortfolioView:
    positions: List[Position] = field(default_factory=list)
    account: float = 0.0
    invested: float = 0.0
    cash: float = 0.0
    open_pnl: float = 0.0
    total_risk: float = 0.0
    planned_risk: float = 0.0
    exit_costs: float = 0.0
    breached: List[Position] = field(default_factory=list)
    breached_pnl: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    by_sector: Dict[str, float] = field(default_factory=dict)
    by_asset: Dict[str, float] = field(default_factory=dict)
    concentration: Optional[float] = None
    portfolio_beta: Optional[float] = None
    correlation: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def stop_touched(bars, opened: int, stop: float, direction: int):
    """The first bar after opening whose range reached the stop: (time, price).

    The bar the position opened in is left out: part of its range may have
    traded before the entry, and a low from before you bought is not your
    stop being hit. The forming bar is not in the bars at all, so today's
    touch shows up through the live price instead.
    """
    times = getattr(bars, "timestamp", None)
    if times is None:
        return None, None
    for k in range(len(times)):
        if int(times[k]) <= opened:
            continue
        if direction > 0 and float(bars.low[k]) <= stop:
            return int(times[k]), float(bars.low[k])
        if direction < 0 and float(bars.high[k]) >= stop:
            return int(times[k]), float(bars.high[k])
    return None, None


def mark(p: Position, cost_bps: float = 0.0) -> None:
    """Mark one priced position: P&L, R, and what is still at risk.

    Three risk figures, because they answer different questions:
      planned_risk    what the plan put at risk on day one, entry to stop.
      risk_remaining  what a stop hit from today's price would still cost.
      breached        the price is already past the stop. A stop in the
                      journal is a plan, not an order: unless one was placed
                      with a broker, the position is still open and the loss
                      is whatever the price says, not "nothing left at risk".
    exit_cost is the trading cost of getting out, from the cost settings, so a
    stop hit costs what it would really cost.
    """
    p.notional = p.price * p.quantity
    p.open_pnl = (p.price - p.entry) * p.direction * p.quantity
    p.open_r = ((p.price - p.entry) * p.direction / p.risk_per_unit
                if p.stop_valid else None)
    # Positive when the stop sits below a long's entry (or above a short's).
    # Zero when it has been moved past the entry to lock in a profit.
    p.planned_risk = max(0.0, (p.entry - p.stop) * p.direction) * p.quantity
    p.exit_cost = abs(p.notional) * cost_bps / 10000.0 / 2.0
    p.breached = (p.price - p.stop) * p.direction <= 0 or p.touched is not None
    if p.breached:
        p.status = "stop_breached"
        p.risk_remaining = 0.0
    else:
        # Distance from here to the stop, not from the entry: a position in
        # profit with a trailed stop risks less than it did on day one.
        p.risk_remaining = (p.price - p.stop) * p.direction * p.quantity


def build(rows, account: float, with_correlation: bool = True,
          cost_bps: Optional[Dict[str, float]] = None) -> PortfolioView:
    """Mark open positions to market and describe the resulting exposure.

    cost_bps maps an asset class ("equity", "crypto", other) to its round-trip
    cost in basis points; half of it is charged to get out of each position.
    """
    view = PortfolioView(account=account)
    if not rows:
        view.cash = account
        view.observations.append("No open positions. Everything is in cash.")
        return view

    positions = [
        Position(trade_id=int(r["id"]), symbol=r["symbol"],
                 direction=int(r["direction"]), quantity=float(r["quantity"] or 0),
                 entry=float(r["entry"]), stop=float(r["stop"]),
                 target=float(r["target1"]) if r["target1"] else None,
                 opened=int(r["ts_opened"]))
        for r in rows
    ]

    _enrich(positions, view)

    costs = cost_bps or {}
    for p in positions:
        if p.price is None:
            continue
        mark(p, costs.get(p.asset_class, costs.get("other", 0.0)))

    # Positions with no price are left out of every sum, and so are positions
    # logged without a size, whose cash figures cannot be known.
    priced = [p for p in positions if p.price is not None]
    sized = [p for p in priced if p.sized]
    view.positions = positions
    view.invested = sum(p.notional for p in sized)
    view.open_pnl = sum(p.open_pnl for p in sized)
    view.breached = [p for p in priced if p.breached]
    view.breached_pnl = sum(p.open_pnl for p in view.breached if p.sized)
    view.total_risk = sum(p.risk_remaining + p.exit_cost
                          for p in sized if not p.breached)
    view.planned_risk = sum(p.planned_risk for p in sized)
    view.exit_costs = sum(p.exit_cost for p in sized)
    view.gross_exposure = sum(abs(p.notional) for p in sized)
    view.net_exposure = sum(p.notional * p.direction for p in sized)
    # An unpriced holding still holds money: its entry cost stays out of cash.
    unpriced_cost = sum(p.entry * p.quantity for p in positions
                        if p.price is None and p.sized)
    view.cash = max(0.0, account - view.invested - unpriced_cost)

    for p in sized:
        key = p.sector or ("Crypto" if p.asset_class == "crypto" else "Unclassified")
        view.by_sector[key] = view.by_sector.get(key, 0.0) + abs(p.notional)
        view.by_asset[p.asset_class or "unknown"] = \
            view.by_asset.get(p.asset_class or "unknown", 0.0) + abs(p.notional)

    if view.gross_exposure > 0:
        # Herfindahl index over names, not journal rows: three entries in one
        # stock are one position. 1.0 is everything in one name; 1/n is
        # perfectly even.
        view.concentration = sum(w * w for w in _name_weights(sized, view.gross_exposure).values())

        # Signed: a short adds negative market exposure, not more of it.
        betas = [(p.beta, p.notional * p.direction) for p in sized if p.beta is not None]
        if betas:
            view.portfolio_beta = sum(b * w for b, w in betas) / view.gross_exposure

    if with_correlation and len({p.symbol for p in priced}) >= 2:
        try:
            view.correlation = risk_mod.correlation_matrix(
                sorted({p.symbol for p in priced}), "1d")
        except Exception:
            view.correlation = {}

    _assess(view, priced)
    return view


def _enrich(positions: List[Position], view: PortfolioView) -> None:
    """Attach live price, sector and risk stats to each holding, concurrently."""
    def grab(p: Position):
        try:
            bars = fetch_bars(p.symbol, "1d")
            p.price = bars.last_price
            p.asset_class = bars.asset_class
            p.touched_at, p.touched = stop_touched(bars, p.opened, p.stop, p.direction)
        except (DataError, Exception):
            view.failed.append(p.symbol)
            return
        try:
            if p.asset_class == "equity":
                f = fund.load(p.symbol, with_sec=False)
                p.sector = f.sector
                p.beta = f.get("beta")
        except Exception:
            pass
        try:
            # Measured against SPY on shared trading days, so coins and ETFs,
            # which Yahoo gives no beta for, get a real one instead of 1.0.
            prof = risk_mod.profile(p.symbol, "1d")
            p.volatility = prof.volatility
            if p.beta is None:
                p.beta = prof.beta
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(6, len(positions))) as pool:
        list(pool.map(grab, positions))


def _name_weights(positions: List[Position], gross: float) -> Dict[str, float]:
    """Each symbol's share of the gross exposure, adding up its journal rows."""
    out: Dict[str, float] = {}
    for p in positions:
        out[p.symbol] = out.get(p.symbol, 0.0) + abs(p.notional) / gross
    return out


def _assess(view: PortfolioView, priced: List[Position]) -> None:
    """Say plainly what the exposure means."""
    if not priced:
        view.warnings.append("None of the open positions could be priced.")
        return

    account = view.account or 1.0
    sized = [p for p in priced if p.sized]

    exposure_pct = view.gross_exposure / account * 100
    count = len({p.symbol for p in priced})
    view.observations.append(
        "%.0f%% of the account is deployed across %d position%s, leaving %s in cash."
        % (exposure_pct, count, "" if count == 1 else "s",
           "{:,.0f}".format(view.cash)))

    # Said first and loudest: a breached stop is the one thing on this page
    # that needs doing today, and "nothing at risk" would hide it.
    for p in view.breached:
        if (p.price - p.stop) * p.direction > 0 and p.touched is not None:
            where = ("traded through its stop of %s on %s (at %s) and is back at %s"
                     % (_px(p.stop), _day(p.touched_at), _px(p.touched), _px(p.price)))
        else:
            where = "is at %s against a stop of %s" % (_px(p.price), _px(p.stop))
        if not p.sized:
            result = "size not logged"
        elif p.open_pnl < 0:
            result = "open loss %s" % "{:,.0f}".format(-p.open_pnl)
        else:
            result = "open gain %s" % "{:,.0f}".format(p.open_pnl)
        view.warnings.append(
            "Stop breached on %s: it %s (%s, %s). The stop in the journal is a "
            "plan, not an order, so unless you placed a stop order with your "
            "broker this position is still open. Close it or set a new stop."
            % (p.symbol, where, result,
               "%.2fR" % p.open_r if p.open_r is not None else "R unknown"))
        if p.open_r is not None and p.open_r < -1.05:
            view.warnings.append(
                "%s has lost more than its planned risk (%.2fR against 1R): the "
                "stop was gapped through or not acted on." % (p.symbol, p.open_r))
    for p in priced:
        if not p.sized:
            view.warnings.append(
                "No size is logged for %s, so its cash risk and value are unknown "
                "and it is left out of the totals." % p.symbol)

    unbreached = [p for p in priced if not p.breached and p.sized]
    risk_pct = view.total_risk / account * 100
    if unbreached:
        view.observations.append(
            "If every stop still ahead were hit from here, the positions above "
            "their stops would lose a further %s, or %.1f%% of the account, "
            "including about %s in trading costs to get out."
            % ("{:,.0f}".format(view.total_risk), risk_pct,
               "{:,.0f}".format(sum(p.exit_cost for p in unbreached))))
    if view.planned_risk:
        view.observations.append(
            "The plans put %s at risk when the positions were opened, entry to stop."
            % "{:,.0f}".format(view.planned_risk))
    for p in priced:
        if not p.stop_valid:
            view.warnings.append(
                "%s has its stop at the entry price, so its risk cannot be "
                "measured. Set a real stop in the journal." % p.symbol)
    if risk_pct > 6:
        view.warnings.append(
            "Total open risk is %.1f%% of the account. A correlated bad week "
            "takes all of it at once, not one position at a time." % risk_pct)

    if view.concentration is not None:
        effective = 1.0 / view.concentration if view.concentration > 0 else 0
        view.observations.append(
            "Concentration is equivalent to %.1f equally sized positions."
            % effective)
        if view.concentration > 0.5:
            weights = _name_weights(sized, view.gross_exposure)
            biggest = max(weights, key=weights.get)
            view.warnings.append(
                "This is effectively a single-position portfolio. %s alone is "
                "%.0f%% of the exposure." % (biggest, weights[biggest] * 100))

    if view.by_sector:
        top_sector, amount = max(view.by_sector.items(), key=lambda kv: kv[1])
        share = amount / view.gross_exposure * 100 if view.gross_exposure else 0
        if share > 50 and top_sector != "Unclassified":
            view.warnings.append(
                "%.0f%% of the exposure is in %s. A sector-wide move decides "
                "the whole result." % (share, top_sector))

    corr = view.correlation or {}
    if corr.get("usable"):
        avg = corr.get("average", 0)
        if avg > 0.7:
            view.warnings.append(
                "Average correlation between holdings is %.2f. These are close "
                "to the same position held several times, so the diversification "
                "is apparent rather than real." % avg)
        elif avg < 0.35:
            view.observations.append(
                "Average correlation of %.2f means these are genuinely different "
                "exposures." % avg)

    if view.portfolio_beta is not None:
        view.observations.append(
            "Weighted beta is %.2f, so the portfolio tends to move %.0f%% as "
            "much as the market." % (view.portfolio_beta,
                                     abs(view.portfolio_beta) * 100))

    if view.net_exposure < 0 and view.gross_exposure > 0:
        view.observations.append("Net exposure is short.")

    names: Dict[str, float] = {}
    for p in sized:
        names[p.symbol] = names.get(p.symbol, 0.0) + abs(p.notional)
    for symbol, amount in names.items():
        if amount / account > 0.35:
            view.warnings.append(
                "%s is %.0f%% of the account on its own."
                % (symbol, amount / account * 100))

    if view.failed:
        view.warnings.append(
            "No price available for %s, so those holdings are excluded from "
            "every figure above." % ", ".join(sorted(set(view.failed))))


def _px(value: float) -> str:
    from .market import format_price
    return format_price(value)


def _day(ts: Optional[int]) -> str:
    import time
    return time.strftime("%d %b", time.gmtime(int(ts))).lstrip("0") if ts else "a recent day"


def what_if(view: PortfolioView, shock_pct: float) -> Dict:
    """Estimate the effect of a market-wide move, using each holding's beta.

    A simple linear shock. It ignores that correlations rise toward one in a
    real crash, so the true loss in a severe move would be worse than this.
    """
    total = 0.0
    rows = []
    for p in view.positions:
        if p.price is None or not p.sized:
            continue
        # Without a measured beta the market's own move is assumed, and the
        # row says so rather than presenting 1.0 as a measurement.
        beta = p.beta if p.beta is not None else 1.0
        move = shock_pct * beta
        change = p.notional * move * (1 if p.direction > 0 else -1)
        total += change
        rows.append({"symbol": p.symbol, "beta": beta, "change": change,
                     "move_pct": move, "assumed": p.beta is None})
    rows.sort(key=lambda r: r["change"])
    return {
        "shock_pct": shock_pct,
        "total_change": total,
        "pct_of_account": (total / view.account * 100) if view.account else None,
        "rows": rows,
        "caveat": "Correlations move toward one in a real sell-off, so a genuine "
                  "crash would hurt more than this straight-line estimate.",
    }


def overlap(rows, symbol: str, interval: str = "1d",
            max_compare: int = 6) -> Dict:
    """Whether a new position would really be more of what is already held.

    The common way a retail account fails is not a single bad trade. It is
    holding five positions that are one position wearing five tickers, so a
    single sector move takes all of them at once. The portfolio page shows this
    after the fact; this answers it before the trade, which is when it can
    still change the decision.

    Bars have to be fetched per instrument, so the comparison is capped at the
    largest few holdings and runs concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import risk as risk_mod

    want = symbol.upper()
    out = {"usable": False, "warnings": [], "held": False, "pairs": [],
           "average": None, "compared": []}

    open_rows = [r for r in rows if (r["status"] or "") == "open"]
    if not open_rows:
        return out

    existing = []
    for r in open_rows:
        sym = (r["symbol"] or "").upper()
        if sym == want:
            out["held"] = True
            continue
        if sym and sym not in existing:
            existing.append(sym)

    if out["held"]:
        out["warnings"].append(
            "You already hold %s. Adding to it doubles the position and the "
            "risk, which is a different decision from opening one." % want)

    if not existing:
        return out

    # Compare against the biggest holdings, since those are the ones whose
    # correlation actually matters to the account.
    def notional(r):
        try:
            return abs(float(r["entry"] or 0) * float(r["quantity"] or 0))
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted((r for r in open_rows
                     if (r["symbol"] or "").upper() in existing),
                    key=notional, reverse=True)
    compare = []
    for r in ranked:
        sym = r["symbol"].upper()
        if sym not in compare:
            compare.append(sym)
        if len(compare) >= max_compare:
            break

    out["compared"] = compare
    with ThreadPoolExecutor(max_workers=4):
        matrix = risk_mod.correlation_matrix([want] + compare, interval)
    if not matrix.get("usable"):
        out["note"] = matrix.get("reason", "Not enough shared history.")
        return out

    symbols = matrix["symbols"]
    if want not in symbols:
        out["note"] = "Could not measure %s against the open positions." % want
        return out

    i = symbols.index(want)
    pairs = []
    for j, other in enumerate(symbols):
        if j == i:
            continue
        pairs.append({"symbol": other, "correlation": matrix["matrix"][i][j]})
    pairs.sort(key=lambda p: -abs(p["correlation"]))

    out["usable"] = True
    out["pairs"] = pairs
    out["samples"] = matrix.get("samples")
    out["average"] = (sum(p["correlation"] for p in pairs) / len(pairs)
                      if pairs else None)

    tight = [p for p in pairs if p["correlation"] >= 0.75]
    close = [p for p in pairs if 0.6 <= p["correlation"] < 0.75]

    if tight:
        names = ", ".join("%s (%.2f)" % (p["symbol"], p["correlation"])
                          for p in tight[:3])
        out["warnings"].append(
            "This moves almost identically to %s. Taking it is closer to "
            "increasing an existing position than to adding a new one, and a "
            "bad day for one is a bad day for all of them." % names)
    elif close:
        p = close[0]
        out["warnings"].append(
            "This correlates %.2f with %s already held. Not the same position, "
            "but not an independent one either."
            % (p["correlation"], p["symbol"]))

    if out["average"] is not None and out["average"] >= 0.6 and len(pairs) >= 3:
        out["warnings"].append(
            "Average correlation of %.2f against everything currently open. "
            "The account is concentrated in one kind of move, and correlations "
            "rise further in a sell-off." % out["average"])

    return out
