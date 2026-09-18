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

import hashlib
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

# How much you are willing to be wrong for a bigger payoff.
#
# Every one of these filters *within* what already cleared the bar. None of
# them lowers it. "High risk, high reward" means the instruments that swing
# hardest among setups that have actually made money after costs on their own
# history. It never means showing something that lost money: there is no
# appetite for risk that makes a losing setup worth taking.
#
# Reward-to-risk is deliberately not one of the axes. Every plan is built at
# target_atr_multiple over stop_atr_multiple, so with the default settings the
# ratio is 1.5 on everything. It describes your own configuration, not the
# instrument, and filtering on it would sort by nothing at all.
#
# What does vary, five-fold across instruments, is gap risk: how much worse
# than your stop an overnight move could actually be. That is the honest risk
# axis here, and it is measured rather than assumed.
RISKS = {
    "steady": {
        "label": "Steady",
        "why": "Gaps less. Wins more often.",
        "min_probability": 0.30,
        "max_gap_multiple": 2.0,
        "crypto_only": False,
        "rank_by": "probability",
        "ranked_as": "how often this setup has worked before",
        "note": ("A gap past your stop on these costs at most about twice "
                 "what you planned to risk, and they have worked more often "
                 "than the rest. That is the calmer end of what cleared the "
                 "bar, not a promise of safety."),
    },
    "balanced": {
        "label": "Balanced",
        "why": "The strongest measured edge, whatever shape it takes.",
        "min_probability": 0.0,
        "max_gap_multiple": None,
        "crypto_only": False,
        "rank_by": "expectancy",
        "ranked_as": "measured edge after costs",
        "note": "",
    },
    "wild": {
        "label": "High risk, high reward",
        "why": "Swings hardest. Hurts hardest when wrong.",
        "min_probability": 0.0,
        "min_gap_multiple": 2.5,
        "max_gap_multiple": None,
        "crypto_only": False,
        "rank_by": "expectancy",
        "ranked_as": "measured edge after costs",
        "note": ("A gap past your stop on these could cost two and a half "
                 "times what you planned to risk, or more. That is the "
                 "reward you are being paid for and the way it goes wrong. "
                 "Your stop does not protect you through a gap, so size these "
                 "smaller than the number on the screen suggests."),
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

# Raised from 40 now that a scan is shared between everyone who asks the same
# question: one person pays for it and the rest read the answer. This is not
# every listed stock and cannot be. Yahoo publishes saved screens, not the full
# tape, and asking for thousands of instruments gets this server rate limited
# within a minute. What it does cover is every actively traded name those
# screens surface, which is where a setup worth taking almost always is.
MAX_SCANNED = 150

# A scan of the market is the same question for everybody, and the answer does
# not meaningfully change minute to minute. Without this, every visitor pays
# the full scan: on hourly crypto bars that is the better part of a minute of
# waiting, often to be told nothing cleared the bar.
CACHE_TTL = 300

# Longer than the slowest scan measured (hourly crypto, ~45s).
SCAN_WAIT = 150.0


def _universe(market: str, limit: int) -> List[str]:
    from . import screener as screener_mod

    stocks: List[str] = []
    if market in ("stocks", "both"):
        # Several screens rather than one, because "most active" alone is a
        # narrow and repetitive slice of the market.
        seen = set()
        # Every screen Yahoo publishes, so the scan is not just a repetitive
        # slice of the most-traded names. Losers matter as much as gainers:
        # the app takes short setups too.
        for screen in ("most_actives", "day_gainers", "day_losers",
                       "undervalued_growth_stocks", "growth_technology_stocks",
                       "undervalued_large_caps", "aggressive_small_caps",
                       "small_cap_gainers", "most_shorted_stocks",
                       "portfolio_anchors"):
            try:
                for symbol in screener_mod.universe_symbols(screen, 60) or []:
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


def _matches_risk(row: Dict, spec: Dict) -> bool:
    """Whether this suits the appetite asked for.

    Only ever narrows what already cleared the bar. A figure the app could not
    measure is not counted against an instrument: the filter judges what was
    measured rather than inventing a failure out of a blank.
    """
    if (row.get("probability") or 0.0) < spec["min_probability"]:
        return False

    gap = row.get("gap_multiple")
    cap = spec.get("max_gap_multiple")
    if cap is not None and gap is not None and gap > cap:
        return False

    floor = spec.get("min_gap_multiple")
    if floor is not None:
        # Unknown gap risk cannot be counted as high gap risk. An appetite for
        # danger is not an appetite for guesses.
        if gap is None or gap < floor:
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
         cfg: Optional[Dict] = None, risk: str = "balanced",
         limit: int = MAX_SCANNED, progress=None) -> Dict:
    """Scan a list and return only what actually cleared the bar.

    Shared between callers asking the same question, because they would
    otherwise each run an identical scan against the same upstream data.
    """
    from . import config as config_mod
    from . import upstream

    cfg = cfg or config_mod.load()
    market = market if market in MARKETS else "stocks"
    horizon = horizon if horizon in HORIZONS else "medium"
    risk = risk if risk in RISKS else "balanced"

    # The settings are part of the key. Trading costs are a per-person figure
    # and they decide what clears the bar, so handing one person's scan to
    # somebody with different costs would quietly answer the wrong question.
    fingerprint = hashlib.sha1(
        repr(sorted((str(k), repr(v)) for k, v in cfg.items()))
        .encode("utf-8")).hexdigest()[:12]
    key = "ideas:%s:%s:%s:%s:%d" % (market, horizon, risk, fingerprint,
                                    limit)

    # The wait has to outlast the scan itself. Hourly crypto bars take the
    # better part of a minute, and a waiter that gives up early runs the whole
    # scan a second time.
    return upstream.cached(
        key, lambda: _scan(market, horizon, risk, cfg, limit, progress),
        ttl=CACHE_TTL, wait=SCAN_WAIT)


def _scan(market: str, horizon: str, risk: str, cfg: Dict, limit: int,
          progress=None) -> Dict:
    """Run the scan for real. Always the slow path."""
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

    spec = RISKS[risk]
    cleared = [r for r in rows if _worth_showing(r)]

    # The appetite narrows what cleared the bar; it never lowers the bar. What
    # it excludes is counted so the page can say so rather than quietly
    # presenting a short list as though that were all there was.
    passed = [r for r in cleared if _matches_risk(r, spec)]
    wrong_shape = len(cleared) - len(passed)

    passed.sort(key=lambda r: (-(r.get(spec["rank_by"]) or 0),
                               -(r.get("expectancy") or 0),
                               -(r.get("conviction") or 0)))

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

    # Placed last, so the numbers run 1..N over what actually survives rather
    # than leaving gaps where something was dropped further up.
    for place, row in enumerate(passed, 1):
        row["rank"] = place

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
        "risk": risk,
        "risk_label": spec["label"],
        "ranked_as": spec["ranked_as"],
        "risk_note": spec["note"],
        "wrong_shape": wrong_shape,
        "interval": interval,
        "scanned": len(rows),
        "asked_for": len(symbols),
        "passed": passed,
        "watch": watch[:10],
        "dropped": dropped,
        "avoided": len(avoided),
        "thin": len(thin),
        "seconds": time.time() - started,
        "scanned_at": time.time(),
        "warning": HORIZONS[horizon]["warning"],
        "summary": _summary(len(rows), passed, len(avoided), len(thin), horizon,
                            len(watch), risk, wrong_shape),
    }


def _summary(scanned: int, passed: List[Dict], avoided: int, thin: int,
             horizon: str, watching: int = 0, risk: str = "balanced",
             wrong_shape: int = 0) -> str:
    if not scanned:
        return ("Nothing could be read just now. The data source may be busy; "
                "try again in a minute.")

    if not passed:
        text = ("Nothing to act on today out of %d looked at. A setup has to be "
                "firing now *and* have made money after costs on that "
                "instrument's own history, and nothing managed both."
                % scanned)
        if wrong_shape:
            text = ("Nothing matching %s out of %d looked at. %d did clear the "
                    "bar, but not with the payoff shape you asked for."
                    % (RISKS[risk]["label"].lower(), scanned, wrong_shape))
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
    if wrong_shape:
        lead += (" Another %d cleared it with a different payoff shape than "
                 "you asked for." % wrong_shape)

    if horizon == "short":
        lead += (" Short holds pay trading costs most often, so treat even "
                 "these carefully.")
    else:
        lead += (" Clearing the bar means the setup has made money after costs "
                 "on this instrument's own history. It is a shortlist to look "
                 "into, not a list to buy.")
    return lead
