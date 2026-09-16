"""What is worth looking at right now, given how you actually trade.

The screener answers "which companies match these numbers". This answers the
question people really arrive with: *out of everything, what should I look at
today?* You pick what you trade and roughly how long you hold; it runs the
whole analysis over a list and ranks what comes back.

Two things it refuses to do, and both are the reason to trust it.

It does not fill a list. If nothing clears the bar, it returns nothing and says
so. Every screener on the internet always has ten results, because a page with
ten rows feels like it worked. That is how people end up trading the least bad
of a bad batch.

It does not rank by how exciting something looks. The order is the measured
edge on that instrument's own history after costs, so the thing at the top is
the thing that has most often worked, not the thing that moved most today.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

# What you trade.
MARKETS = {
    "stocks": {"label": "Stocks", "why": "Shares in companies."},
    "crypto": {"label": "Crypto", "why": "Coins. Open all hours, moves harder."},
    "both": {"label": "Both", "why": "Look at everything."},
}

# How long you hold. The interval has to match, because a setup measured on
# five-minute bars says nothing about a position held for a month.
HORIZONS = {
    "short": {
        "label": "Days", "interval": "1h",
        "why": "In and out within a few days.",
        "warning": ("Short holds pay the spread most often. By this app's own "
                    "back-tests that is where the costs eat the edge."),
    },
    "medium": {
        "label": "Weeks", "interval": "1d",
        "why": "Hold for a couple of weeks.",
        "warning": "",
    },
    "long": {
        "label": "Months", "interval": "1d",
        "why": "Hold for months. The company has to stand up, not just the chart.",
        "warning": "",
        # Over months the accounts matter more than the pattern, so this one
        # additionally requires the filings to look sound. Without that it
        # would be an identical copy of the weeks option, which is worse than
        # not offering it.
        "needs_sound_accounts": True,
    },
}

# Hand-kept because the free screener endpoints cover no crypto at all.
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD",
          "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "BCH-USD", "SHIB-USD",
          "UNI-USD", "ATOM-USD", "NEAR-USD", "XLM-USD", "HBAR-USD",
          "FIL-USD", "ETC-USD", "ICP-USD", "ARB-USD", "OP-USD", "INJ-USD",
          "SUI-USD", "TRX-USD", "VET-USD", "ALGO-USD",
          "AAVE-USD"]

# A spread of liquid names, used when the live screener is unavailable.
FALLBACK_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                   "AMD", "AVGO", "JPM", "V", "UNH", "XOM", "KO", "WMT",
                   "COST", "NFLX", "DIS", "BA", "PFE"]

MAX_SCANNED = 40


def _universe(market: str, limit: int) -> List[str]:
    from . import screener as screener_mod

    stocks: List[str] = []
    if market in ("stocks", "both"):
        # Several screens rather than one, because "most active" alone is a
        # narrow and repetitive slice of the market.
        seen = set()
        for screen in ("most_actives", "day_gainers", "undervalued_growth_stocks",
                       "growth_technology_stocks"):
            try:
                for symbol in screener_mod.universe_symbols(screen, 30) or []:
                    if symbol not in seen:
                        seen.add(symbol)
                        stocks.append(symbol)
            except Exception:
                continue
        if not stocks:
            stocks = list(FALLBACK_STOCKS)
        # Anything with a suffix is a foreign listing or a warrant; both are
        # noise in a "what should I look at" list.
        stocks = [s for s in stocks if s.isalpha() and len(s) <= 5]

    coins = list(CRYPTO) if market in ("crypto", "both") else []

    if market == "both":
        half = max(1, limit // 2)
        return stocks[:half] + coins[:limit - half]
    return (stocks or coins)[:limit]


def _look_at(symbol: str, interval: str, cfg: Dict) -> Optional[Dict]:
    """Run the real analysis on one instrument. Never raises."""
    from . import engine

    try:
        result = engine.analyse(symbol, cfg, interval=interval, with_news=False,
                                with_learning=True, record=False)
    except Exception:
        return None

    # The resolver falls back to a fuzzy search when a ticker is dead, so a
    # request for APT-USD can come back as STAPT-USD. That is helpful when a
    # person mistypes and unacceptable here: this list would be recommending an
    # instrument nobody asked about and nothing vetted.
    got = (result.bars.symbol or "").upper()
    if got != symbol.upper():
        return None

    plan = result.plan
    calib = result.calibration
    return {
        "symbol": result.bars.symbol,
        "name": getattr(result.bars, "name", "") or result.bars.symbol,
        "price": result.bars.last_price,
        "action": plan.action,
        "conviction": plan.conviction,
        "entry": plan.entry,
        "stop": plan.stop,
        "target": plan.target1,
        "probability": plan.probability,
        "samples": plan.prob_samples,
        "reliable": plan.prob_reliable,
        "expectancy": plan.expectancy_r,
        "breakeven": plan.breakeven_rate,
        "reward_risk": plan.reward_risk,
        "asset_class": result.bars.asset_class,
        "headline": plan.headline,
        "gap_multiple": (plan.position or {}).get("gap_multiple"),
        "edge_verdict": plan.edge_verdict,
        "cost_r": getattr(calib, "cost_r", None),
    }


def _worth_showing(row: Dict) -> bool:
    """Whether this cleared the bar, rather than merely being the least bad.

    Three conditions, all of them things the app already measured: it has to
    be an actionable call, the measured edge after costs has to be positive,
    and there has to have been enough history to mean anything.
    """
    if row["action"] not in ("BUY", "WAIT", "SHORT"):
        return False
    if row.get("expectancy") is None or row["expectancy"] <= 0:
        return False
    if not row.get("samples") or row["samples"] < 30:
        return False
    return True


def _accounts_hold_up(symbol: str) -> Optional[Dict]:
    """Whether the filings support holding this for months. None when unknown.

    Only called for names that already cleared the price test, so this costs a
    handful of requests rather than one per instrument scanned.
    """
    from . import fundamentals as fundamentals_mod
    from . import quality as quality_mod
    from . import sec as sec_mod

    try:
        filings = sec_mod.financial_history(symbol, years=8)
        if not filings.get("available"):
            return None
        facts = fundamentals_mod.load(symbol, with_sec=False)
        scored = quality_mod.assess(filings, facts.get("market_cap"), facts.sector)
    except Exception:
        return None
    if not scored.get("available"):
        return None

    models = scored["scores"]
    strength = models["piotroski"]
    distress = models["altman"]
    earnings = models["accruals"]

    reasons = []
    if strength.usable and strength.value <= 3:
        reasons.append("its finances got worse in most ways last year")
    if distress.usable and distress.value < 1.81:
        reasons.append("the balance sheet scores in the distress range")
    if earnings.usable and earnings.value > 0.10:
        reasons.append("reported profit is running well ahead of cash")

    return {"ok": not reasons, "reasons": reasons,
            "strength": strength.value if strength.usable else None}


def find(market: str = "stocks", horizon: str = "medium",
         cfg: Optional[Dict] = None, limit: int = MAX_SCANNED,
         progress=None) -> Dict:
    """Scan a list and return only what actually cleared the bar."""
    from . import config as config_mod

    cfg = cfg or config_mod.load()
    market = market if market in MARKETS else "stocks"
    horizon = horizon if horizon in HORIZONS else "medium"
    interval = HORIZONS[horizon]["interval"]

    started = time.time()
    symbols = _universe(market, min(limit, MAX_SCANNED))

    rows: List[Dict] = []
    # Concurrent because each one is mostly waiting on the network, and the
    # shared cache means overlapping symbols cost one request between them.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_look_at, s, interval, cfg): s for s in symbols}
        for future in as_completed(futures):
            row = future.result()
            if row:
                rows.append(row)
            if progress:
                progress("Looked at %d of %d" % (len(rows), len(symbols)))

    passed = [r for r in rows if _worth_showing(r)]
    passed.sort(key=lambda r: (-(r["expectancy"] or 0), -(r["conviction"] or 0)))

    # Holding for months is a bet on the business, not on the pattern, so the
    # long option additionally reads the filings. Anything that fails is moved
    # out with the reason attached rather than silently dropped.
    dropped: List[Dict] = []
    if HORIZONS[horizon].get("needs_sound_accounts") and passed:
        kept = []
        for candidate in passed:
            if candidate.get("asset_class") == "crypto":
                candidate["dropped_because"] = (
                    "A coin files no accounts, so there is nothing to check "
                    "before holding it for months.")
                dropped.append(candidate)
                continue
            verdict = _accounts_hold_up(candidate["symbol"])
            if verdict is None:
                candidate["accounts"] = "not filed"
                kept.append(candidate)
            elif verdict["ok"]:
                candidate["accounts"] = "sound"
                kept.append(candidate)
            else:
                candidate["dropped_because"] = verdict["reasons"][0].capitalize() + "."
                dropped.append(candidate)
        passed = kept

    # A positive edge measured over years does not mean a setup is firing
    # today, and on most days none is. Without this second tier the page would
    # be empty almost every time, which is honest and useless. These are the
    # names where the approach has paid on this instrument's own history, with
    # nothing to act on right now: the thing to do with them is watch, not buy.
    ready = {r["symbol"] for r in passed}
    watch = [r for r in rows
             if r["symbol"] not in ready
             and (r.get("expectancy") or 0) > 0
             and (r.get("samples") or 0) >= 30]
    watch.sort(key=lambda r: -(r["expectancy"] or 0))

    avoided = [r for r in rows if r["action"] == "AVOID"]
    thin = [r for r in rows
            if r["action"] != "AVOID" and not _worth_showing(r)]

    return {
        "market": market,
        "horizon": horizon,
        "interval": interval,
        "scanned": len(rows),
        "asked_for": len(symbols),
        "passed": passed,
        "watch": watch[:10],
        "dropped": dropped,
        "avoided": len(avoided),
        "thin": len(thin),
        "seconds": time.time() - started,
        "warning": HORIZONS[horizon]["warning"],
        "summary": _summary(len(rows), passed, len(avoided), len(thin), horizon,
                            len(watch)),
    }


def _summary(scanned: int, passed: List[Dict], avoided: int, thin: int,
             horizon: str, watching: int = 0) -> str:
    if not scanned:
        return ("Nothing could be read just now. The data source may be busy; "
                "try again in a minute.")

    if not passed:
        text = ("Nothing to act on today out of %d looked at. A setup has to be "
                "firing now *and* have made money after costs on that "
                "instrument's own history, and nothing managed both."
                % scanned)
        if watching:
            text += (" %d of them have paid off historically but are not doing "
                     "anything today, so they are listed below to watch rather "
                     "than to buy." % watching)
        else:
            text += " Doing nothing is a real answer, and today it is the answer."
        return text

    lead = ("%d of %d cleared the bar." % (len(passed), scanned))
    if len(passed) == 1:
        lead = "One of %d cleared the bar." % scanned

    if horizon == "short":
        lead += (" Short holds pay trading costs most often, so treat even "
                 "these carefully.")
    else:
        lead += (" Clearing the bar means the setup has made money after costs "
                 "on this instrument's own history. It is a shortlist to look "
                 "into, not a list to buy.")
    return lead
