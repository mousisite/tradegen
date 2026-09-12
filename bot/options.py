"""Options: chains, Greeks, implied volatility and what the market is pricing.

Yahoo supplies the chain and its implied volatility. Everything analytical here
is computed locally with Black-Scholes, because the Greeks a data provider
returns are frequently stale, inconsistently defined, or simply absent.

Two things this module is careful about:

* **Implied volatility from a quote is only as good as the quote.** A contract
  with no bid, or a spread wider than its own value, produces a nonsense IV.
  Those rows are excluded from every aggregate rather than quietly averaged in.
* **The expected move is the market's own estimate**, taken from the at-the-money
  straddle. It is one of the few genuinely forward-looking numbers available,
  and it is far more useful than any indicator when sizing a trade around an
  event.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import yahoo
from .market import DataError, fetch_bars

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function, accurate to ~1e-15."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, years: float, rate: float,
                  vol: float, is_call: bool = True) -> Dict:
    """Price and Greeks for a European option.

    Greeks are returned in the units traders actually use: theta per calendar
    day rather than per year, and vega per one percentage point of volatility
    rather than per unit. Quoting them per year makes theta look catastrophic
    and vega look enormous.
    """
    if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
        return {"price": intrinsic, "delta": 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    discount = math.exp(-rate * years)

    if is_call:
        price = spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        rho = strike * years * discount * _norm_cdf(d2) / 100.0
        theta_year = (-spot * _norm_pdf(d1) * vol / (2 * sqrt_t)
                      - rate * strike * discount * _norm_cdf(d2))
    else:
        price = strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        rho = -strike * years * discount * _norm_cdf(-d2) / 100.0
        theta_year = (-spot * _norm_pdf(d1) * vol / (2 * sqrt_t)
                      + rate * strike * discount * _norm_cdf(-d2))

    return {
        "price": price,
        "delta": delta,
        "gamma": _norm_pdf(d1) / (spot * vol * sqrt_t),
        "theta": theta_year / 365.0,          # per calendar day
        "vega": spot * _norm_pdf(d1) * sqrt_t / 100.0,   # per 1 vol point
        "rho": rho,
    }


def implied_vol(price: float, spot: float, strike: float, years: float,
                rate: float, is_call: bool = True) -> Optional[float]:
    """Back out volatility from a price by bisection.

    Bisection rather than Newton-Raphson: it cannot diverge, which matters on
    deep-in-the-money contracts where vega is almost zero and Newton's step
    explodes.
    """
    if price <= 0 or years <= 0 or spot <= 0:
        return None
    intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
    if price < intrinsic - 1e-6:
        return None            # quote is below intrinsic; the quote is wrong

    low, high = 1e-4, 5.0
    for _ in range(80):
        mid = 0.5 * (low + high)
        theo = black_scholes(spot, strike, years, rate, mid, is_call)["price"]
        if abs(theo - price) < 1e-6:
            return mid
        if theo > price:
            high = mid
        else:
            low = mid
    result = 0.5 * (low + high)
    return result if 0.001 < result < 4.99 else None


@dataclass
class Contract:
    """One option, with locally computed Greeks."""
    symbol: str
    strike: float
    is_call: bool
    expiry: int
    days: int
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    iv: Optional[float]
    in_the_money: bool
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    quality: str = "ok"       # ok | wide | no-bid | stale

    @property
    def mid(self) -> Optional[float]:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last if self.last > 0 else None

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if not m or m <= 0 or self.ask <= 0 or self.bid <= 0:
            return None
        return (self.ask - self.bid) / m

    @property
    def priced(self) -> bool:
        """Whether this contract carries a usable price for analysis.

        Separate from `tradable`. Outside market hours there are no bids, but
        the last traded price is still a real number that says what volatility
        the market assigned. Refusing to analyse it would make the whole
        options feature useless for most of the day.
        """
        return self.quality in ("ok", "wide", "stale") and self.iv is not None

    @property
    def tradable(self) -> bool:
        """Whether this contract could actually be traded near the quoted price."""
        return (self.quality == "ok" and self.open_interest >= 10
                and (self.spread_pct or 1.0) <= 0.25)


@dataclass
class Chain:
    """One expiry's options, plus what they say about expectations."""
    symbol: str
    spot: float
    expiry: int
    expiry_date: str
    days: int
    rate: float
    calls: List[Contract] = field(default_factory=list)
    puts: List[Contract] = field(default_factory=list)
    expirations: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def all(self) -> List[Contract]:
        return self.calls + self.puts


def risk_free_rate(default: float = 0.042) -> float:
    """Short-term rate from the 13-week Treasury bill, with a sane fallback."""
    try:
        bars = fetch_bars("^IRX", "1d", "1mo")
        value = float(bars.close[-1]) / 100.0
        if 0.0 <= value < 0.25:
            return value
    except (DataError, Exception):
        pass
    return default


def load_chain(symbol: str, target_days: Optional[int] = None) -> Chain:
    """Fetch a chain and compute Greeks for every contract.

    Without a target, picks the nearest expiry at least a week out: the front
    week is dominated by gamma and pin risk and says little about direction.
    """
    first = yahoo.options(symbol)
    quote = (first.get("quote") or {})
    spot = quote.get("regularMarketPrice") or quote.get("postMarketPrice")
    if not spot:
        raise yahoo.YahooError("No live price for %s, cannot value options." % symbol)

    now = time.time()
    expirations = []
    for ts in first.get("expirationDates", []):
        days = max(0, int((ts - now) / 86400))
        expirations.append({"timestamp": ts, "days": days,
                            "date": time.strftime("%Y-%m-%d", time.gmtime(ts))})

    if not expirations:
        raise yahoo.YahooError("No option expiries listed for %s." % symbol)

    if target_days is None:
        future = [e for e in expirations if e["days"] >= 7]
        chosen = future[0] if future else expirations[-1]
    else:
        chosen = min(expirations, key=lambda e: abs(e["days"] - target_days))

    raw = first
    if chosen["timestamp"] != (first.get("options") or [{}])[0].get("expirationDate"):
        raw = yahoo.options(symbol, chosen["timestamp"])

    block = (raw.get("options") or [{}])[0]
    rate = risk_free_rate()
    years = max(chosen["days"], 0) / 365.0

    chain = Chain(symbol=symbol.upper(), spot=float(spot),
                  expiry=chosen["timestamp"], expiry_date=chosen["date"],
                  days=chosen["days"], rate=rate, expirations=expirations)

    for side, is_call in (("calls", True), ("puts", False)):
        for row in block.get(side, []):
            contract = _build(row, chain.spot, years, rate, is_call,
                              chosen["timestamp"], chosen["days"])
            (chain.calls if is_call else chain.puts).append(contract)

    chain.calls.sort(key=lambda c: c.strike)
    chain.puts.sort(key=lambda c: c.strike)

    if chain.days <= 2:
        chain.notes.append(
            "This expiry is within two days. Prices are dominated by time decay "
            "and pinning, so read direction from a later one.")
    stale = sum(1 for c in chain.all if c.quality == "stale")
    if chain.all and stale / len(chain.all) > 0.6:
        chain.notes.append(
            "The market is closed, so these are last traded prices rather than "
            "live quotes. The volatility read is still meaningful; the prices "
            "are not fillable until it opens.")
    else:
        untradable = sum(1 for c in chain.all if not c.tradable)
        if chain.all and untradable / len(chain.all) > 0.6:
            chain.notes.append(
                "Most contracts here have wide spreads or almost no open "
                "interest. Quoted prices are unlikely to be fillable.")
    return chain


def _build(row: Dict, spot: float, years: float, rate: float, is_call: bool,
           expiry: int, days: int) -> Contract:
    bid = float(row.get("bid") or 0.0)
    ask = float(row.get("ask") or 0.0)
    last = float(row.get("lastPrice") or 0.0)
    vendor_iv = row.get("impliedVolatility")
    strike = float(row.get("strike") or 0.0)

    # A vendor implied volatility of 0.0000 is a placeholder, not a measurement.
    # Yahoo returns those outside market hours, and feeding one into
    # Black-Scholes yields deltas of exactly 1.000 or 0.000, which look like
    # facts on a screen. Anything outside a plausible band is discarded.
    vendor = None
    try:
        candidate = float(vendor_iv) if vendor_iv is not None else None
        if candidate is not None and 0.01 < candidate < 4.0:
            vendor = candidate
    except (TypeError, ValueError):
        vendor = None

    c = Contract(
        symbol=row.get("contractSymbol", ""), strike=strike, is_call=is_call,
        expiry=expiry, days=days, bid=bid, ask=ask, last=last,
        volume=int(row.get("volume") or 0),
        open_interest=int(row.get("openInterest") or 0),
        iv=vendor, in_the_money=bool(row.get("inTheMoney")))

    # Grade the quote. "stale" means the market is shut: there is no bid, but
    # the last trade is a real price that can still be analysed.
    if bid > 0 and ask > 0:
        c.quality = "wide" if (c.spread_pct or 0) > 0.5 else "ok"
    elif last > 0:
        c.quality = "stale"
    else:
        c.quality = "no-bid"

    mid = c.mid
    if mid and years > 0 and c.quality != "no-bid":
        own = implied_vol(mid, spot, strike, years, rate, is_call)
        if own:
            c.iv = own

    if c.iv and years > 0:
        greeks = black_scholes(spot, strike, years, rate, c.iv, is_call)
        c.delta, c.gamma = greeks["delta"], greeks["gamma"]
        c.theta, c.vega = greeks["theta"], greeks["vega"]
    return c


def analyse(chain: Chain) -> Dict:
    """What the option market is saying about this instrument."""
    spot = chain.spot
    usable = [c for c in chain.all if c.priced]

    atm_call = _nearest(chain.calls, spot)
    atm_put = _nearest(chain.puts, spot)

    expected_move = None
    if atm_call and atm_put and atm_call.mid and atm_put.mid:
        # The at-the-money straddle is the market's own price for movement in
        # either direction by expiry. No model, no assumption: a real quote.
        straddle = atm_call.mid + atm_put.mid
        expected_move = {
            "absolute": straddle,
            "percent": straddle / spot if spot else None,
            "upper": spot + straddle,
            "lower": spot - straddle,
            "from": "at-the-money straddle at %.2f" % atm_call.strike,
        }

    call_oi = sum(c.open_interest for c in chain.calls)
    put_oi = sum(c.open_interest for c in chain.puts)
    call_vol = sum(c.volume for c in chain.calls)
    put_vol = sum(c.volume for c in chain.puts)

    atm_iv = None
    if atm_call and atm_call.iv and atm_put and atm_put.iv:
        atm_iv = (atm_call.iv + atm_put.iv) / 2.0
    elif usable:
        atm_iv = min(usable, key=lambda c: abs(c.strike - spot)).iv

    # Skew: what downside protection costs relative to upside speculation.
    # A high number means the market is paying up for puts, which is fear.
    skew = None
    otm_put = _nearest([c for c in chain.puts if c.strike < spot * 0.95 and c.priced], spot * 0.93)
    otm_call = _nearest([c for c in chain.calls if c.strike > spot * 1.05 and c.priced], spot * 1.07)
    if otm_put and otm_call and otm_put.iv and otm_call.iv:
        skew = otm_put.iv - otm_call.iv

    unusual = sorted(
        [c for c in chain.all
         if c.open_interest >= 100 and c.volume >= max(200, c.open_interest)],
        key=lambda c: -c.volume)[:6]

    magnets = sorted([c for c in chain.all if c.open_interest > 0],
                     key=lambda c: -c.open_interest)[:5]

    return {
        "spot": spot,
        "expiry_date": chain.expiry_date,
        "days": chain.days,
        "rate": chain.rate,
        "atm_iv": atm_iv,
        "expected_move": expected_move,
        "put_call_oi": (put_oi / call_oi) if call_oi else None,
        "put_call_volume": (put_vol / call_vol) if call_vol else None,
        "call_oi": call_oi, "put_oi": put_oi,
        "call_volume": call_vol, "put_volume": put_vol,
        "skew": skew,
        "unusual": unusual,
        "open_interest_magnets": magnets,
        "tradable_count": sum(1 for c in chain.all if c.tradable),
        "total_count": len(chain.all),
        "notes": chain.notes,
        "reading": _reading(atm_iv, skew, put_oi, call_oi, expected_move),
    }


def _nearest(contracts: List[Contract], target: float) -> Optional[Contract]:
    pool = [c for c in contracts if c.strike > 0]
    return min(pool, key=lambda c: abs(c.strike - target)) if pool else None


def _reading(atm_iv, skew, put_oi, call_oi, expected_move) -> List[str]:
    """Plain sentences describing what the numbers mean."""
    out = []
    if atm_iv:
        if atm_iv > 0.8:
            out.append("Implied volatility is very high at %.0f%%. Something is "
                       "expected to happen, and options are expensive." % (atm_iv * 100))
        elif atm_iv > 0.45:
            out.append("Implied volatility of %.0f%% is elevated. Buying premium "
                       "here needs a big move just to break even." % (atm_iv * 100))
        elif atm_iv < 0.2:
            out.append("Implied volatility of %.0f%% is low. Options are cheap, "
                       "and the market expects a quiet period." % (atm_iv * 100))
        else:
            out.append("Implied volatility of %.0f%% is unremarkable." % (atm_iv * 100))

    if expected_move and expected_move.get("percent"):
        out.append("The market is pricing a move of about %.1f%% either way by "
                   "expiry, to roughly %.2f or %.2f." % (
                       expected_move["percent"] * 100,
                       expected_move["lower"], expected_move["upper"]))

    if skew is not None:
        if skew > 0.06:
            out.append("Puts cost notably more than equivalent calls, which is "
                       "the market paying up for downside protection.")
        elif skew < -0.02:
            out.append("Calls cost more than equivalent puts, which usually means "
                       "speculation on upside rather than hedging.")

    if call_oi and put_oi:
        ratio = put_oi / call_oi
        if ratio > 1.3:
            out.append("Open interest leans to puts (%.2f to 1), so positioning "
                       "is defensive." % ratio)
        elif ratio < 0.6:
            out.append("Open interest leans to calls (%.2f puts per call), so "
                       "positioning is optimistic." % ratio)
    return out


def suggest(chain: Chain, view: str, conviction: float = 0.5) -> List[Dict]:
    """Structures that fit a directional view and the current volatility.

    Whether options are cheap or expensive matters more than direction. Buying
    premium into high implied volatility loses money even when the direction is
    right, which is the most common way beginners lose on options.
    """
    stats = analyse(chain)
    iv = stats.get("atm_iv")
    spot = chain.spot
    out: List[Dict] = []

    if iv is None:
        return [{"name": "No usable quotes",
                 "why": "Implied volatility could not be established from these "
                        "quotes, so no structure can be priced honestly.",
                 "risk": ""}]

    expensive = iv > 0.5
    cheap = iv < 0.25

    if view == "bullish":
        if cheap:
            c = _nearest([c for c in chain.calls if c.priced and 0.30 <= c.delta <= 0.55], spot)
            if c:
                out.append({
                    "name": "Long call, %.2f strike, %s" % (c.strike, chain.expiry_date),
                    "why": "Volatility is cheap at %.0f%%, so buying the move "
                           "directly is reasonably priced." % (iv * 100),
                    "cost": c.mid, "delta": c.delta, "theta": c.theta,
                    "risk": "Maximum loss is the premium. Time decay costs about "
                            "%.2f a day." % abs(c.theta)})
        elif expensive:
            short = _nearest([c for c in chain.puts if c.priced and -0.35 <= c.delta <= -0.15], spot * 0.95)
            if short:
                out.append({
                    "name": "Cash-secured put, %.2f strike" % short.strike,
                    "why": "Volatility is expensive at %.0f%%, so selling premium "
                           "is better paid than buying it. You get paid to wait "
                           "for a lower entry." % (iv * 100),
                    "cost": -(short.mid or 0), "delta": short.delta,
                    "theta": short.theta,
                    "risk": "You must be willing to own the shares at %.2f. Loss "
                            "below that is the same as owning the stock."
                            % short.strike})
        else:
            long_leg = _nearest([c for c in chain.calls if c.priced and 0.45 <= c.delta <= 0.65], spot)
            short_leg = _nearest([c for c in chain.calls if c.priced and 0.15 <= c.delta <= 0.32],
                                 spot * 1.06)
            if long_leg and short_leg and short_leg.strike > long_leg.strike:
                debit = (long_leg.mid or 0) - (short_leg.mid or 0)
                width = short_leg.strike - long_leg.strike
                out.append({
                    "name": "Call spread, %.2f / %.2f" % (long_leg.strike, short_leg.strike),
                    "why": "Volatility is middling, so a spread caps the cost of "
                           "being long without paying full premium.",
                    "cost": debit,
                    "max_gain": width - debit if debit else None,
                    "delta": long_leg.delta - short_leg.delta,
                    "risk": "Maximum loss is the %.2f paid. Gain is capped at "
                            "%.2f." % (debit, width - debit) if debit else ""})

    elif view == "bearish":
        if cheap:
            p = _nearest([c for c in chain.puts if c.priced and -0.55 <= c.delta <= -0.30], spot)
            if p:
                out.append({
                    "name": "Long put, %.2f strike, %s" % (p.strike, chain.expiry_date),
                    "why": "Volatility is cheap at %.0f%%, so downside protection "
                           "is affordable." % (iv * 100),
                    "cost": p.mid, "delta": p.delta, "theta": p.theta,
                    "risk": "Maximum loss is the premium paid."})
        else:
            long_leg = _nearest([c for c in chain.puts if c.priced and -0.65 <= c.delta <= -0.45], spot)
            short_leg = _nearest([c for c in chain.puts if c.priced and -0.32 <= c.delta <= -0.15],
                                 spot * 0.94)
            if long_leg and short_leg and short_leg.strike < long_leg.strike:
                debit = (long_leg.mid or 0) - (short_leg.mid or 0)
                width = long_leg.strike - short_leg.strike
                out.append({
                    "name": "Put spread, %.2f / %.2f" % (long_leg.strike, short_leg.strike),
                    "why": "Volatility is not cheap, so a spread reduces the "
                           "premium at risk.",
                    "cost": debit,
                    "max_gain": width - debit if debit else None,
                    "delta": long_leg.delta - short_leg.delta,
                    "risk": "Maximum loss is the %.2f paid." % debit if debit else ""})

    else:
        out.append({
            "name": "No directional structure",
            "why": "Without a directional view, an options position is a bet on "
                   "volatility rather than on the company. That is a different "
                   "discipline and needs its own reasoning.",
            "risk": ""})

    if conviction < 0.35 and out and out[0].get("cost"):
        out[0]["risk"] = (out[0].get("risk", "") + " Conviction is low, so this "
                          "belongs in a small size if at all.").strip()
    return out
