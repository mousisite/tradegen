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
    status: str = "open"
    beta: Optional[float] = None
    volatility: Optional[float] = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


@dataclass
class PortfolioView:
    positions: List[Position] = field(default_factory=list)
    account: float = 0.0
    invested: float = 0.0
    cash: float = 0.0
    open_pnl: float = 0.0
    total_risk: float = 0.0
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


def build(rows, account: float, with_correlation: bool = True) -> PortfolioView:
    """Mark open positions to market and describe the resulting exposure."""
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

    for p in positions:
        if p.price is None:
            continue
        p.notional = p.price * p.quantity
        p.open_pnl = (p.price - p.entry) * p.direction * p.quantity
        if p.risk_per_unit > 0:
            p.open_r = (p.price - p.entry) * p.direction / p.risk_per_unit
        # Risk still on the table: distance from here to the stop, not from
        # the entry. A position in profit with a stop below entry risks less
        # than it did on day one, and a naive sum would overstate the danger.
        if p.direction > 0:
            remaining = max(0.0, p.price - p.stop)
        else:
            remaining = max(0.0, p.stop - p.price)
        p.risk_remaining = remaining * p.quantity

    priced = [p for p in positions if p.price is not None]
    view.positions = positions
    view.invested = sum(p.notional for p in priced)
    view.open_pnl = sum(p.open_pnl for p in priced)
    view.total_risk = sum(p.risk_remaining for p in priced)
    view.gross_exposure = sum(abs(p.notional) for p in priced)
    view.net_exposure = sum(p.notional * p.direction for p in priced)
    view.cash = max(0.0, account - view.invested)

    for p in priced:
        key = p.sector or ("Crypto" if p.asset_class == "crypto" else "Unclassified")
        view.by_sector[key] = view.by_sector.get(key, 0.0) + abs(p.notional)
        view.by_asset[p.asset_class or "unknown"] = \
            view.by_asset.get(p.asset_class or "unknown", 0.0) + abs(p.notional)

    if view.gross_exposure > 0:
        # Herfindahl index: the sum of squared weights. 1.0 is everything in
        # one name; 1/n is perfectly even. A far better concentration measure
        # than "number of positions", which treats a 90% holding and a 1%
        # holding as equals.
        weights = [abs(p.notional) / view.gross_exposure for p in priced]
        view.concentration = sum(w * w for w in weights)

        betas = [(p.beta, abs(p.notional)) for p in priced if p.beta is not None]
        if betas:
            total = sum(w for _, w in betas)
            view.portfolio_beta = sum(b * w for b, w in betas) / total if total else None

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
            prof = risk_mod.profile(p.symbol, "1d", benchmark="")
            p.volatility = prof.volatility
            if p.beta is None:
                p.beta = prof.beta
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(6, len(positions))) as pool:
        list(pool.map(grab, positions))


def _assess(view: PortfolioView, priced: List[Position]) -> None:
    """Say plainly what the exposure means."""
    if not priced:
        view.warnings.append("None of the open positions could be priced.")
        return

    account = view.account or 1.0

    exposure_pct = view.gross_exposure / account * 100
    view.observations.append(
        "%.0f%% of the account is deployed across %d position%s, leaving %s in cash."
        % (exposure_pct, len(priced), "" if len(priced) == 1 else "s",
           "{:,.0f}".format(view.cash)))

    risk_pct = view.total_risk / account * 100
    view.observations.append(
        "If every stop were hit from here, the loss would be %s, or %.1f%% of the account."
        % ("{:,.0f}".format(view.total_risk), risk_pct))
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
            biggest = max(priced, key=lambda p: abs(p.notional))
            view.warnings.append(
                "This is effectively a single-position portfolio. %s alone is "
                "%.0f%% of the exposure." % (
                    biggest.symbol, abs(biggest.notional) / view.gross_exposure * 100))

    if view.by_sector:
        top_sector, amount = max(view.by_sector.items(), key=lambda kv: kv[1])
        share = amount / view.gross_exposure * 100 if view.gross_exposure else 0
        if share > 50 and len(view.by_sector) > 1:
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

    for p in priced:
        if abs(p.notional) / account > 0.35:
            view.warnings.append(
                "%s is %.0f%% of the account on its own."
                % (p.symbol, abs(p.notional) / account * 100))
        if p.open_r is not None and p.open_r < -0.9:
            view.warnings.append(
                "%s is at or past its stop, showing %.2fR. It should already "
                "be closed." % (p.symbol, p.open_r))

    if view.failed:
        view.warnings.append(
            "No price available for %s, so those holdings are excluded from "
            "every figure above." % ", ".join(sorted(set(view.failed))))


def what_if(view: PortfolioView, shock_pct: float) -> Dict:
    """Estimate the effect of a market-wide move, using each holding's beta.

    A simple linear shock. It ignores that correlations rise toward one in a
    real crash, so the true loss in a severe move would be worse than this.
    """
    total = 0.0
    rows = []
    for p in view.positions:
        if p.price is None:
            continue
        beta = p.beta if p.beta is not None else 1.0
        move = shock_pct * beta
        change = p.notional * move * (1 if p.direction > 0 else -1)
        total += change
        rows.append({"symbol": p.symbol, "beta": beta, "change": change,
                     "move_pct": move})
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
