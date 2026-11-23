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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from . import fundamentals as fund
from . import risk as risk_mod
from .clock import ny_clock
from .market import DataError, fetch_bars, split_factor_since, split_words


# The most holdings compared with each other. Every one is read in turn and
# is a row and a column of the table, so a very long list is a slow page and
# a table nobody can read.
COMPARE_MOST = 40


@dataclass
class Trail:
    """A trailing stop: how far behind the best price since opening it sits.

    Either a share of that price (pct, 0.05 for 5%) or a fixed distance in the
    trade's own shares (distance). An ATR trail is a distance: the multiple of
    the ATR measured when the trail was set, kept as it was then, so a jump in
    volatility later cannot widen it and loosen the stop. since is when it was
    set: the trailed level counts on bars that started after that. best and
    best_at are the best price seen since opening as last stored, so a reader
    whose bars start later than the trade still trails from it.
    """
    pct: Optional[float] = None
    distance: Optional[float] = None
    atr: Optional[float] = None          # the ATR multiple a distance came from
    since: int = 0
    best: Optional[float] = None
    best_at: Optional[int] = None

    def level(self, direction: int, best: float) -> float:
        """Where the trail puts the stop, given the best price so far."""
        if self.pct is not None:
            return best * (1.0 - self.pct) if direction > 0 else best * (1.0 + self.pct)
        return best - self.distance if direction > 0 else best + self.distance

    def words(self) -> str:
        """The trail as people say it: "5%" or "2 ATR (3.10)"."""
        if self.pct is not None:
            return "%g%%" % round(self.pct * 100, 4)
        if self.atr:
            return "%g ATR (%s)" % (round(self.atr, 4), _px(self.distance))
        return _px(self.distance)


def _column(row, key):
    """A journal row's column, or None for a row from before it existed, or a
    stand-in in a test, that has no such column."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def trail_of(row) -> Optional[Trail]:
    """The trail kept on a journal row, or None when it has none.

    A row from before trails existed, or a stand-in in a test, has no such
    columns, and reads as a trade that does not trail.
    """
    def get(key):
        return _column(row, key)

    pct, distance = get("trail_pct"), get("trail_dist")
    if not pct and not distance:
        return None
    best = get("trail_best")
    return Trail(pct=float(pct) if pct else None,
                 distance=None if pct else float(distance),
                 atr=float(get("trail_atr")) if get("trail_atr") else None,
                 since=int(get("trail_ts") or get("ts_opened") or 0),
                 best=float(best) if best is not None else None,
                 best_at=int(get("trail_best_ts")) if get("trail_best_ts") is not None else None)


def crossing_of(row):
    """The first crossing of a journal row's stop any reader saw and kept
    (database.stop_crossed), as (when, price, stop), or None."""
    when, price, stop = (_column(row, "trail_hit_ts"), _column(row, "trail_hit_price"),
                         _column(row, "trail_hit_stop"))
    if when is None or price is None or stop is None:
        return None
    return int(when), float(price), float(stop)


@dataclass
class StopRead:
    """A position's stop as the journal reads it: where it is, and whether it was hit.

    Every price is in the shares the trade was opened with.
    """
    stop: float                          # the stop in force now
    initial: float                       # the stop the trade was opened with
    price: float                         # today's price
    best: float                          # best price since opening: the high of a long, the low of a short
    best_at: Optional[int] = None
    trail: Optional[Trail] = None
    touched_at: Optional[int] = None     # the first bar that reached the stop in force then
    touched: Optional[float] = None      # the price that reached it
    touched_stop: Optional[float] = None
    past: bool = False                   # today's price is at or past the stop in force now

    @property
    def hit(self) -> bool:
        """Past the stop now, or traded through it since opening: the plan said get out."""
        return self.past or self.touched_at is not None

    @property
    def moved(self) -> bool:
        """Whether the trail has tightened the stop from where it started."""
        return abs(self.stop - self.initial) > 1e-12 * max(1.0, abs(self.initial))

    def crossing(self, now: float):
        """The crossing this read saw, as (when, price, stop) to keep, or None.

        The first bar that reached the stop, or else today's price past it,
        at now. A reader keeps it (database.stop_crossed), whether the stop
        trails or not.
        """
        if self.touched_at is not None:
            return self.touched_at, self.touched, self.touched_stop
        if self.past:
            return int(now), self.price, self.stop
        return None


def _tighter(direction: int, a: float, b: float) -> float:
    """Of two stops, the one nearer the price: the higher for a long."""
    return max(a, b) if direction > 0 else min(a, b)


def _walk(bars, direction: int, entry: float, stop: float, opened: int,
          split: float = 1.0, trail: Optional[Trail] = None):
    """Read the bars since opening by the stop rule. Prices in the trade's shares.

    Returns (touched_at, touched, touched_stop, best, best_at). The bar the
    position opened in is left out: part of its range may have traded before
    the entry, and a low from before you bought is not your stop being hit,
    nor a high you could have trailed from. Each bar is held to the stop in
    force when it started, from the best price of the bars before it, because
    a bar's high and low do not say which came first. The forming bar is not
    in the bars at all, so today's touch shows up through the live price.
    """
    times = getattr(bars, "timestamp", None)
    best, best_at = float(entry), int(opened)
    if times is None:
        return None, None, None, best, best_at
    kept = trail.best if trail is not None else None
    for k in range(len(times)):
        start = int(times[k])
        if start <= opened:
            continue
        if (kept is not None and trail.best_at is not None and trail.best_at < start
                and (kept - best) * direction > 0):
            best, best_at = kept, int(trail.best_at)
        level = stop
        if trail is not None and start > trail.since:
            level = _tighter(direction, stop, trail.level(direction, best))
        low, high = float(bars.low[k]) * split, float(bars.high[k]) * split
        if direction > 0 and low <= level:
            return start, low, level, best, best_at
        if direction < 0 and high >= level:
            return start, high, level, best, best_at
        far = high if direction > 0 else low
        if far == far and (far - best) * direction > 0:
            best, best_at = far, start
    return None, None, None, best, best_at


def read_stop(bars, direction: int, entry: float, stop: float, opened: int,
              split: float = 1.0, trail: Optional[Trail] = None,
              now: Optional[int] = None, kept=None) -> StopRead:
    """The one stop rule. The portfolio, the journal, paper.py and the
    background stop notice all read a position through this, so they agree.

    entry, stop and the result are in the shares the trade was opened with;
    the bars are in today's, and split (shares today per share then) turns
    them back. Without a trail the stop is the one logged. With one, the stop
    in force is the tighter of that and the trail behind the best price since
    opening: for a long, max(stop, best high - trail); for a short, the
    mirror. The best only ever improves, so the stop only ever tightens. A
    trail set after opening trails from the best since opening too, but a
    bar from before it was set is held only to the logged stop. kept is a
    crossing of the stop that a reader saw and kept, as (when, price, stop)
    (crossing_of), and counts as one: seen live, it is not in that bar's own
    reading once the bar is done, and in time it is not in the bars at all.
    """
    direction = 1 if direction > 0 else -1
    touched_at, touched, touched_stop, best, best_at = _walk(
        bars, direction, entry, stop, opened, split, trail)
    if kept is not None and (touched_at is None or kept[0] < touched_at):
        touched_at, touched, touched_stop = kept
    price = float(bars.last_price) * split
    if trail is not None and trail.best is not None and (trail.best - best) * direction > 0:
        best = trail.best
        best_at = trail.best_at if trail.best_at is not None else best_at
    if (price - best) * direction > 0:
        best, best_at = price, int(now if now is not None else time.time())
    held = stop if trail is None else _tighter(direction, stop, trail.level(direction, best))
    return StopRead(stop=held, initial=float(stop), price=price, best=best,
                    best_at=best_at, trail=trail, touched_at=touched_at,
                    touched=touched, touched_stop=touched_stop,
                    past=(price - held) * direction <= 0)


def read_trade_stop(row, bars, split: float = 1.0) -> StopRead:
    """read_stop for a journal row, with whatever trail and crossing are kept on it."""
    return read_stop(bars, int(row["direction"]), float(row["entry"]), float(row["stop"]),
                     int(row["ts_opened"]), split, trail_of(row), kept=crossing_of(row))


def trade_interval(row) -> str:
    """The bars a journal trade is read on: its own bar size, daily when it has none.

    Every reader of its stop reads the same bars, so a stop crossed and
    recovered inside an hourly trade's day is a crossing to all of them.
    """
    try:
        return row["interval"] or "1d"
    except (KeyError, IndexError):
        return "1d"


def atr_of(bars, n: int = 14) -> Optional[float]:
    """The bars' latest ATR, in their own prices, or None when it cannot be measured."""
    from .indicators import atr
    try:
        values = atr(np.asarray(bars.high, dtype=float), np.asarray(bars.low, dtype=float),
                     np.asarray(bars.close, dtype=float), n)
    except (AttributeError, TypeError, ValueError, IndexError):
        return None
    finite = values[np.isfinite(values)]
    return float(finite[-1]) if len(finite) and finite[-1] > 0 else None


def trail_from(row, bars, split: float = 1.0, pct: Optional[float] = None,
               atr: Optional[float] = None, now: Optional[int] = None):
    """A trail for a journal row as it would be set now: (trail, before, after).

    pct is a share of the best price (0.05 for 5%); atr a multiple of the ATR
    of the trade's own bars, measured now and kept as a distance in the
    trade's own shares. before reads the position as it stands and after
    with the new trail, for trail_problem. Raises ValueError, in words, when
    the ATR cannot be measured.
    """
    now = int(now if now is not None else time.time())
    before = read_trade_stop(row, bars, split)
    distance = None
    if atr is not None:
        unit = atr_of(bars)
        if unit is None:
            raise ValueError("There is not enough price history to measure %s's ATR "
                             "just now. Trail it by a percentage instead." % row["symbol"])
        distance = float(atr) * unit * split
    trail = Trail(pct=pct, distance=distance, atr=atr if distance else None, since=now,
                  best=before.best, best_at=before.best_at)
    after = read_stop(bars, int(row["direction"]), float(row["entry"]), float(row["stop"]),
                      int(row["ts_opened"]), split, trail, now=now)
    return trail, before, after


def trail_problem(symbol: str, direction: int, before: StopRead,
                  after: StopRead) -> Optional[str]:
    """Why a new trail cannot be set, or None. A stop only ever tightens.

    before is the position read as it stands, after with the new trail. A
    position already past its stop is not given a new one: the journal counts
    the stop as hit, and moving it would hide that. A trail that would put
    the stop past today's price, or loosen one already trailing, is refused.
    """
    if before.hit:
        return ("%s is already past its stop, so the journal counts the stop as hit. "
                "Close it rather than move the stop." % symbol)
    if after.past:
        return ("That trail would put the stop at %s, past today's price of %s, so the "
                "position would be stopped at once. Choose a wider trail."
                % (_px(after.stop), _px(after.price)))
    if _tighter(direction, before.stop, after.stop) != after.stop:
        return ("That trail would move the stop from %s back to %s. A trailing stop only "
                "tightens: choose a narrower trail, or keep the one it has."
                % (_px(before.stop), _px(after.stop)))
    return None


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
    touched_day: Optional[date] = None    # the day to say it was crossed on (crossed_day)
    touched_stop: Optional[float] = None  # the stop in force when it was crossed
    beta_assumed: bool = False
    status: str = "open"
    beta: Optional[float] = None
    volatility: Optional[float] = None
    # Shares today per share the day it opened: 10.0 after a ten-for-one.
    split: float = 1.0
    # The stop it was opened with. stop above is the one in force, which a
    # trail moves; R is always measured from this one, the plan's.
    initial_stop: Optional[float] = None
    trail: Optional[Trail] = None
    # The trade's own bar size, which its stop is read on (trade_interval).
    interval: str = "1d"
    # A crossing of the stop a reader saw and kept (crossing_of), or None.
    kept: Optional[tuple] = None
    # How its stop was read just now, for keep_reads; None when unpriced.
    read: Optional[StopRead] = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - (self.initial_stop if self.initial_stop is not None
                                 else self.stop))

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

    read_stop's rule for a stop that does not trail, in the bars' own prices.
    The bar the position opened in is left out: part of its range may have
    traded before the entry, and a low from before you bought is not your
    stop being hit. The forming bar is not in the bars at all, so today's
    touch shows up through the live price instead.
    """
    touched_at, touched, _stop, _best, _at = _walk(bars, 1 if direction > 0 else -1,
                                                   stop, stop, opened)
    return touched_at, touched


def mark(p: Position, cost_bps: float = 0.0) -> None:
    """Mark one priced position: P&L, R, and what is still at risk.

    Three risk figures, because they answer different questions:
      planned_risk    what the plan put at risk on day one, entry to stop.
      risk_remaining  what a stop hit from today's price would still cost,
                      at the stop in force now, which a trail may have moved.
      breached        the price is already past the stop. A stop in the
                      journal is a plan, not an order: unless one was placed
                      with a broker, the position is still open and the loss
                      is whatever the price says, not "nothing left at risk".
    exit_cost is the trading cost of getting out, from the cost settings, so a
    stop hit costs what it would really cost. quantity is what is still held,
    so a position closed in part counts only the part still open.
    """
    p.notional = p.price * p.quantity
    p.open_pnl = (p.price - p.entry) * p.direction * p.quantity
    p.open_r = ((p.price - p.entry) * p.direction / p.risk_per_unit
                if p.stop_valid else None)
    # Positive when the stop sits below a long's entry (or above a short's).
    # Zero when it has been moved past the entry to lock in a profit.
    planned = p.initial_stop if p.initial_stop is not None else p.stop
    p.planned_risk = max(0.0, (p.entry - planned) * p.direction) * p.quantity
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

    rows are open journal trades with quantity set to what is still held
    (database.open_positions), so a position closed in part counts only the
    part still open. cost_bps maps an asset class ("equity", "crypto", other)
    to its round-trip cost in basis points; half of it is charged to get out
    of each position. "futures", where given, is a futures contract's, which
    the data source files under other.
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
                 opened=int(r["ts_opened"]), initial_stop=float(r["stop"]),
                 trail=trail_of(r), interval=trade_interval(r), kept=crossing_of(r))
        for r in rows
    ]

    _enrich(positions, view)
    for p in positions:
        if p.split != 1.0:
            view.observations.append(
                "%s split %s since it was opened. Its prices here are in the "
                "shares you opened with: today's quote times %g."
                % (p.symbol, split_words(p.split), p.split))

    costs = cost_bps or {}
    for p in positions:
        if p.price is None:
            continue
        kind = p.asset_class
        if "futures" in costs and p.symbol.upper().endswith("=F"):
            kind = "futures"
        mark(p, costs.get(kind, costs.get("other", 0.0)))

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
    # What is left of the account is what was not paid out: a holding that has
    # risen since was still bought at its entry, and one without a price still
    # holds its money. Counted at today's value, cash fell when a holding rose.
    # A futures contract is held on a deposit, not bought at its full value:
    # one micro contract, worth more than a small account, left no cash at all.
    paid = sum(p.entry * p.quantity for p in positions
               if p.sized and not p.symbol.upper().endswith("=F"))
    view.cash = max(0.0, account - paid)

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

        # Signed: a short adds negative market exposure, not more of it. One
        # with no measured beta is taken to move with the market, as what_if
        # takes it, so this figure and the shock table agree; _assess names it.
        if any(p.beta is not None for p in sized):
            view.portfolio_beta = sum(
                (p.beta if p.beta is not None else 1.0) * p.notional * p.direction
                for p in sized) / view.gross_exposure

    if with_correlation and len({p.symbol for p in priced}) >= 2:
        worth: Dict[str, float] = {}
        for p in priced:
            worth[p.symbol] = worth.get(p.symbol, 0.0) + abs(p.notional)
        names = sorted(worth, key=lambda s: (-worth[s], s))[:COMPARE_MOST]
        try:
            view.correlation = risk_mod.correlation_matrix(sorted(names), "1d")
            if len(names) < len(worth):
                view.correlation.setdefault("notes", []).append(
                    "Compared between your %d largest holdings, of %d."
                    % (len(names), len(worth)))
        except Exception:
            view.correlation = {}

    _assess(view, priced)
    return view


def keep_reads(conn, view: PortfolioView, now: Optional[float] = None) -> None:
    """Keep what reading the positions saw, as every reader of a stop does.

    The best price under a trailing stop (database.trail_seen) and the first
    crossing of any stop (database.stop_crossed). A dip through a stop
    between two rounds of the background check may be seen here alone, and
    once that bar is finished its high and low no longer show it: kept, it
    is still a crossing for the journal, the notice and everything else.
    """
    from . import database as db
    now = time.time() if now is None else now
    for p in view.positions:
        if p.read is None:
            continue
        if p.trail is not None and p.read.best_at is not None:
            db.trail_seen(conn, p.trade_id, p.read.best, p.read.best_at)
        crossed = p.read.crossing(now)
        if crossed is not None:
            db.stop_crossed(conn, p.trade_id, *crossed)


def _enrich(positions: List[Position], view: PortfolioView) -> None:
    """Attach live price, sector and risk stats to each holding, concurrently."""
    def grab(p: Position):
        try:
            # The trade's own bars, which the journal, paper.py and the stop
            # notice read it on: on daily bars a whole day is held to the stop
            # from before it, and an hourly trade's crossing inside it would
            # not show. Beta and volatility below stay daily.
            bars = fetch_bars(p.symbol, p.interval or "1d")
            # The journal's prices are in the shares of the day it opened.
            # After a split Yahoo quotes today's, ten times fewer dollars a
            # share after a ten-for-one: read as is, a long looked 90% down and
            # past its stop. The quote is turned back into those shares.
            p.split = split_factor_since(p.symbol, p.opened, bars)
            p.asset_class = bars.asset_class
            # The stop by the journal's own rule, trail, split and any
            # crossing kept from before these bars included.
            read = read_stop(bars, p.direction, p.entry,
                             p.initial_stop if p.initial_stop is not None else p.stop,
                             p.opened, p.split, p.trail, kept=p.kept)
            p.price, p.stop = read.price, read.stop
            p.touched_at, p.touched = read.touched_at, read.touched
            p.touched_stop = read.touched_stop
            p.touched_day = crossed_day(p.interval or "1d", bars, read.touched_at)
            p.read = read
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
    if any(p.symbol.upper().endswith("=F") for p in sized):
        view.observations.append(
            "A futures contract is held on a deposit, so its full value is counted "
            "as deployed but is not taken out of cash.")

    # Said first and loudest: a breached stop is the one thing on this page
    # that needs doing today, and "nothing at risk" would hide it.
    for p in view.breached:
        kind = "trailing stop" if p.trail is not None else "stop"
        if (p.price - p.stop) * p.direction > 0 and p.touched is not None:
            crossed = p.touched_stop if p.touched_stop is not None else p.stop
            where = ("traded through its %s of %s on %s (at %s) and is back at %s"
                     % (kind, _px(crossed), day_words(p.touched_day), _px(p.touched),
                        _px(p.price)))
        else:
            where = "is at %s against a %s of %s" % (_px(p.price), kind, _px(p.stop))
        if not p.sized:
            result = "size not logged"
        elif p.open_pnl < 0:
            result = "open loss %s" % "{:,.0f}".format(-p.open_pnl)
        else:
            result = "open gain %s" % "{:,.0f}".format(p.open_pnl)
        view.warnings.append(
            "Stop breached on %s: it %s (%s, %s). The stop in the journal is a "
            "plan, not an order, so unless you placed a stop order with your "
            "broker this position is still open. Close it in the journal once "
            "you are out."
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
        # A small position's costs round to nothing, and "about 0 in trading
        # costs" says nothing; a short's stop is above it, not below.
        costs = "{:,.0f}".format(sum(p.exit_cost for p in unbreached))
        view.observations.append(
            "If every stop still ahead were hit from here, the positions not yet "
            "at their stops would lose a further %s, or %.1f%% of the account%s."
            % ("{:,.0f}".format(view.total_risk), risk_pct,
               "" if costs == "0" else
               ", including about %s in trading costs to get out" % costs))
    if view.planned_risk:
        view.observations.append(
            "The plans put %s at risk on what is still open, entry to the stop "
            "each position was opened with." % "{:,.0f}".format(view.planned_risk))
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
        # A book that is short on balance gains when the market falls.
        against = ", in the opposite direction" if view.portfolio_beta <= -0.005 else ""
        view.observations.append(
            "Weighted beta is %.2f, so the portfolio tends to move %.0f%% as "
            "much as the market%s." % (view.portfolio_beta,
                                       abs(view.portfolio_beta) * 100, against))
        assumed = sorted({p.symbol for p in sized if p.beta is None})
        if assumed:
            view.observations.append(
                "No beta could be measured for %s, so %s counted as moving exactly "
                "with the market." % (", ".join(assumed),
                                      "it is" if len(assumed) == 1 else "they are"))

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


def crossed_day(interval: str, bars, when: Optional[int]):
    """The date a stop was traded through, the same wherever it is said, or None.

    The New York date, as the journal counts its days. A daily bar is named
    by its own day instead: a coin's starts at midnight UTC, and a Hong Kong
    session opens, while it is still the evening before in New York.
    """
    if when is None:
        return None
    times = getattr(bars, "timestamp", None)
    if (interval == "1d" and times is not None
            and int(when) in {int(t) for t in times}):
        return datetime.fromtimestamp(int(when), timezone.utc).date()
    return ny_clock(when)[0]


def day_words(day) -> str:
    """'5 Nov' for a date."""
    return day.strftime("%d %b").lstrip("0") if day else "a recent day"


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
    largest few holdings.
    """
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
