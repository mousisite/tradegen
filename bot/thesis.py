"""Bull and bear cases, assembled from evidence rather than adjectives.

Every point carries the number behind it and a link to where that number came
from. A claim with no citation does not get made, which rules out the confident
vagueness that makes most automated research worthless.

The cases are built from what is measurable, and each one ends by stating what
would prove it wrong. A thesis that cannot be falsified is not a thesis, it is
a preference, and writing the falsifier down at the start is the difference
between changing your mind on evidence and rationalising afterwards.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Evidence:
    """One verifiable fact supporting a point."""
    claim: str
    value: str
    source: str
    url: str = ""
    as_of: str = ""
    strength: str = "medium"          # strong | medium | weak

    def cite(self) -> str:
        bits = [self.source]
        if self.as_of:
            bits.append(self.as_of)
        return " · ".join(bits)


@dataclass
class Point:
    """One argument in a case."""
    headline: str
    detail: str
    evidence: List[Evidence] = field(default_factory=list)
    weight: float = 1.0               # how much this should count


@dataclass
class Case:
    """One side of the argument."""
    side: str                         # bull | bear
    points: List[Point] = field(default_factory=list)
    strength: float = 0.0
    summary: str = ""


@dataclass
class Thesis:
    """The full two-sided view, with what would change it."""
    symbol: str
    price: float
    created: int
    bull: Case
    bear: Case
    verdict: str = ""
    balance: float = 0.0              # -1 bear, +1 bull
    falsifiers: List[str] = field(default_factory=list)
    unknowns: List[str] = field(default_factory=list)
    all_sources: List[Dict] = field(default_factory=list)

    @property
    def evidence_count(self) -> int:
        return sum(len(p.evidence) for p in self.bull.points + self.bear.points)


def _pct(v, digits=1):
    return "%.*f%%" % (digits, v * 100) if v is not None else "—"


def _money(v):
    if v is None:
        return "—"
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= cut:
            return "%.2f%s" % (v / cut, suffix)
    return "%.2f" % v


def build(symbol: str, price: float, fundamentals=None, quality=None,
          valuation=None, risk_profile=None, technical=None, sentiment=None,
          filings=None, options_stats=None, sec_history=None) -> Thesis:
    """Assemble both cases from whatever analysis is available.

    Every input is optional. A thesis built from three of eight inputs says so
    rather than presenting itself as complete.
    """
    bull, bear = Case("bull"), Case("bear")
    unknowns: List[str] = []
    sources: List[Dict] = []
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%d", time.gmtime(now))

    f = fundamentals
    if f is not None:
        sources.extend(f.sources)
        _fundamental_points(f, bull, bear, stamp)
    else:
        unknowns.append("Company fundamentals were not loaded.")

    if quality is not None and quality.get("reliable"):
        passed, failed = quality["passed"], quality["failed"]
        if len(passed) >= 5:
            bull.points.append(Point(
                headline="Passes most quality tests",
                detail="%d of %d checks passed: %s." % (
                    len(passed), quality["known"],
                    ", ".join(p["label"].lower() for p in passed[:4])),
                evidence=[Evidence("Business quality checks",
                                   "%d of %d passed" % (len(passed), quality["known"]),
                                   "computed from reported fundamentals", "", stamp,
                                   "strong")],
                weight=1.2))
        if len(failed) >= 3:
            bear.points.append(Point(
                headline="Fails several quality tests",
                detail="%d checks failed: %s." % (
                    len(failed), ", ".join(p["why"] for p in failed[:3])),
                evidence=[Evidence("Business quality checks",
                                   "%d of %d failed" % (len(failed), quality["known"]),
                                   "computed from reported fundamentals", "", stamp,
                                   "strong")],
                weight=1.2))

    if valuation is not None and valuation.get("usable"):
        _valuation_points(valuation, price, bull, bear, stamp)
    else:
        unknowns.append("No valuation model could run, usually because free "
                        "cash flow or share count was not reported.")

    if sec_history and sec_history.get("available"):
        _filed_points(sec_history, bull, bear, stamp)
        sources.append({"name": "SEC EDGAR filed financials",
                        "url": sec_history.get("source_url", ""),
                        "detail": "%s, fiscal years %s" % (
                            sec_history.get("entity", ""),
                            ", ".join(str(y) for y in sec_history.get("years", [])[-4:]))})

    if risk_profile is not None and risk_profile.reliable:
        _risk_points(risk_profile, bear, bull, stamp)
    elif risk_profile is not None:
        unknowns.append("Risk measures are based on too little history to rely on.")

    if technical is not None:
        _technical_points(technical, bull, bear, stamp)

    if sentiment is not None and (sentiment.n_news or sentiment.n_social):
        _sentiment_points(sentiment, bull, bear, stamp)
        sources.append({"name": "News and social sentiment",
                        "url": "", "detail": "%d headlines, %d posts, scored by %s"
                        % (sentiment.n_news, sentiment.n_social, sentiment.method)})

    if filings:
        _filing_points(filings, bull, bear, stamp)

    if options_stats:
        _options_points(options_stats, bull, bear, stamp)

    bull.strength = sum(p.weight for p in bull.points)
    bear.strength = sum(p.weight for p in bear.points)
    total = bull.strength + bear.strength
    balance = ((bull.strength - bear.strength) / total) if total else 0.0

    bull.summary = _summarise(bull, "supports owning this")
    bear.summary = _summarise(bear, "argues against it")

    thesis = Thesis(symbol=symbol.upper(), price=price, created=now,
                    bull=bull, bear=bear, balance=balance,
                    unknowns=unknowns, all_sources=sources)
    thesis.verdict = _verdict(balance, bull, bear, thesis.evidence_count)
    thesis.falsifiers = _falsifiers(bull, bear, f, price, balance)
    return thesis


def _fundamental_points(f, bull: Case, bear: Case, stamp: str) -> None:
    url = "https://finance.yahoo.com/quote/%s/key-statistics" % f.symbol

    margin = f.get("net_margin")
    if margin is not None:
        if margin > 0.18:
            bull.points.append(Point(
                "Highly profitable",
                "Keeps %s of every dollar of revenue as profit, which gives room "
                "to absorb cost increases and invest without borrowing." % _pct(margin),
                [Evidence("Net margin", _pct(margin), "Yahoo Finance", url, stamp, "strong")],
                1.3))
        elif margin < 0:
            bear.points.append(Point(
                "Loses money",
                "Net margin is %s. The business consumes cash rather than "
                "producing it, so it depends on markets staying open to it."
                % _pct(margin),
                [Evidence("Net margin", _pct(margin), "Yahoo Finance", url, stamp, "strong")],
                1.6))

    roe = f.get("roe")
    if roe is not None and roe > 0.20:
        bull.points.append(Point(
            "Strong return on capital",
            "Earns %s on shareholder equity, meaning reinvested profits compound "
            "rather than merely sustain the business." % _pct(roe),
            [Evidence("Return on equity", _pct(roe), "Yahoo Finance", url, stamp, "strong")],
            1.2))

    growth = f.get("revenue_growth")
    if growth is not None:
        if growth > 0.15:
            bull.points.append(Point(
                "Growing quickly",
                "Revenue up %s over the last year." % _pct(growth),
                [Evidence("Revenue growth", _pct(growth), "Yahoo Finance", url, stamp)],
                1.1))
        elif growth < -0.05:
            bear.points.append(Point(
                "Shrinking",
                "Revenue down %s over the last year. Something is going wrong, "
                "and the reason matters more than the number." % _pct(abs(growth)),
                [Evidence("Revenue growth", _pct(growth), "Yahoo Finance", url, stamp,
                          "strong")],
                1.4))

    debt = f.get("debt_to_equity")
    if debt is not None and debt > 200:
        bear.points.append(Point(
            "Heavily indebted",
            "Debt to equity of %.0f%%. Leverage magnifies both outcomes and "
            "removes the option to wait out a bad year." % debt,
            [Evidence("Debt to equity", "%.0f%%" % debt, "Yahoo Finance", url, stamp,
                      "strong")],
            1.3))

    fcf = f.get("free_cashflow")
    if fcf is not None:
        if fcf > 0:
            bull.points.append(Point(
                "Generates cash",
                "Free cash flow of %s after paying for its own capital spending."
                % _money(fcf),
                [Evidence("Free cash flow", _money(fcf), "Yahoo Finance", url, stamp,
                          "strong")], 1.2))
        else:
            bear.points.append(Point(
                "Burns cash",
                "Free cash flow is %s. Continuing requires either existing cash "
                "or new money from somebody." % _money(fcf),
                [Evidence("Free cash flow", _money(fcf), "Yahoo Finance", url, stamp,
                          "strong")], 1.5))

    short = f.get("short_pct_float")
    if short is not None and short > 0.15:
        bear.points.append(Point(
            "Heavily shorted",
            "%s of the free float is sold short. Others with access to the same "
            "information are betting against it." % _pct(short),
            [Evidence("Short interest of float", _pct(short), "Yahoo Finance", url,
                      stamp)], 1.0))

    analysts = f.analysts or {}
    target, count = analysts.get("target_mean"), analysts.get("analyst_count")
    if target and count and f.get("price"):
        gap = (target - f.get("price")) / f.get("price")
        side = bull if gap > 0.12 else (bear if gap < -0.08 else None)
        if side is not None:
            side.points.append(Point(
                "Analysts see %s" % ("upside" if gap > 0 else "downside"),
                "Average target of %.2f against %.2f today, from %d analysts. "
                "Worth knowing, but targets follow prices at least as often as "
                "they lead them." % (target, f.get("price"), int(count)),
                [Evidence("Mean analyst target", "%.2f" % target,
                          "Yahoo Finance, %d analysts" % int(count), url, stamp, "weak")],
                0.6))


def _valuation_points(v: Dict, price: float, bull: Case, bear: Case, stamp: str) -> None:
    mid, low, high = v.get("midpoint"), v.get("low"), v.get("high")
    upside = v.get("upside")
    if mid is None or upside is None:
        return

    detail = "Models centre on %.2f against %.2f today, a range of %.2f to %.2f." % (
        mid, price, low or mid, high or mid)
    if v.get("agreement") == "wide":
        detail += (" They disagree widely, so this is weak evidence either way.")

    strength = "weak" if v.get("agreement") == "wide" else "medium"
    weight = 0.7 if v.get("agreement") == "wide" else 1.2

    if upside > 0.20:
        bull.points.append(Point(
            "Looks cheap on the models", detail,
            [Evidence("Modelled fair value", "%.2f" % mid,
                      "discounted cash flow and multiples", "", stamp, strength)],
            weight))
    elif upside < -0.20:
        bear.points.append(Point(
            "Looks expensive on the models", detail,
            [Evidence("Modelled fair value", "%.2f" % mid,
                      "discounted cash flow and multiples", "", stamp, strength)],
            weight))

    for model in v.get("models", []):
        if getattr(model, "implied_growth", None) is not None:
            g = model.implied_growth
            if g > 0.25:
                bear.points.append(Point(
                    "The price already assumes a lot",
                    "To justify %.2f, free cash flow has to grow about %s a year "
                    "for a decade. That is a demanding bar, and missing it does "
                    "not require anything to go wrong, only for things to go "
                    "less than perfectly." % (price, _pct(g)),
                    [Evidence("Growth implied by the price", _pct(g),
                              "reverse discounted cash flow", "", stamp, "strong")],
                    1.3))
            elif g < 0.03:
                bull.points.append(Point(
                    "The price assumes very little",
                    "At %.2f the market is pricing in only about %s annual growth. "
                    "Modest delivery would be enough." % (price, _pct(g)),
                    [Evidence("Growth implied by the price", _pct(g),
                              "reverse discounted cash flow", "", stamp, "strong")],
                    1.3))


def _filed_points(h: Dict, bull: Case, bear: Case, stamp: str) -> None:
    url = h.get("source_url", "")
    cagr = h.get("cagr") or {}

    rev_cagr = cagr.get("revenue")
    if rev_cagr is not None:
        if rev_cagr > 0.12:
            bull.points.append(Point(
                "Long record of growth",
                "Revenue has compounded at %s a year over the filed history. "
                "That is delivery, not a forecast." % _pct(rev_cagr),
                [Evidence("Revenue CAGR from filings", _pct(rev_cagr),
                          "SEC EDGAR", url, stamp, "strong")], 1.4))
        elif rev_cagr < 0:
            bear.points.append(Point(
                "Long-term decline",
                "Revenue has shrunk at %s a year across the filed history."
                % _pct(abs(rev_cagr)),
                [Evidence("Revenue CAGR from filings", _pct(rev_cagr),
                          "SEC EDGAR", url, stamp, "strong")], 1.5))

    derived = h.get("derived") or {}
    years = sorted(derived)
    if len(years) >= 3:
        margins = [derived[y].get("net_margin") for y in years
                   if derived[y].get("net_margin") is not None]
        if len(margins) >= 3:
            trend = margins[-1] - margins[0]
            if trend > 0.03:
                bull.points.append(Point(
                    "Margins improving",
                    "Net margin has gone from %s to %s across the filed years, "
                    "which usually means pricing power or operating leverage."
                    % (_pct(margins[0]), _pct(margins[-1])),
                    [Evidence("Net margin trend", "%s to %s" % (
                        _pct(margins[0]), _pct(margins[-1])), "SEC EDGAR", url,
                        stamp, "strong")], 1.2))
            elif trend < -0.03:
                bear.points.append(Point(
                    "Margins eroding",
                    "Net margin has fallen from %s to %s across the filed years."
                    % (_pct(margins[0]), _pct(margins[-1])),
                    [Evidence("Net margin trend", "%s to %s" % (
                        _pct(margins[0]), _pct(margins[-1])), "SEC EDGAR", url,
                        stamp, "strong")], 1.3))


def _risk_points(r, bear: Case, bull: Case, stamp: str) -> None:
    if r.volatility and r.volatility > 0.55:
        bear.points.append(Point(
            "Very volatile",
            "Annualised volatility of %s. Position size matters more than "
            "direction here." % _pct(r.volatility),
            [Evidence("Annualised volatility", _pct(r.volatility),
                      "computed from %d returns" % r.samples, "", stamp, "strong")],
            1.1))
    if r.max_drawdown is not None and r.max_drawdown < -0.45:
        bear.points.append(Point(
            "Has fallen hard before",
            "Peak-to-trough decline of %s within the measured period. It can "
            "happen again." % _pct(abs(r.max_drawdown)),
            [Evidence("Maximum drawdown", _pct(r.max_drawdown),
                      "computed from %d returns" % r.samples, "", stamp, "strong")],
            1.2))
    if r.sharpe is not None and r.sharpe > 1.0:
        bull.points.append(Point(
            "Has paid for its risk",
            "Sharpe ratio of %.2f over the measured period, meaning returns have "
            "more than compensated for volatility." % r.sharpe,
            [Evidence("Sharpe ratio", "%.2f" % r.sharpe,
                      "computed from %d returns" % r.samples, "", stamp)], 0.9))
    if r.beta is not None and r.beta < 0.4:
        bull.points.append(Point(
            "Moves independently",
            "Beta of %.2f against the market, so it can diversify a portfolio "
            "rather than double an existing bet." % r.beta,
            [Evidence("Beta", "%.2f" % r.beta, "computed against SPY", "", stamp)],
            0.8))


def _technical_points(t: Dict, bull: Case, bear: Case, stamp: str) -> None:
    composite = t.get("composite")
    regime = t.get("regime")
    expectancy = t.get("expectancy")
    hit_rate = t.get("hit_rate")
    samples = t.get("samples")

    if composite is None:
        return
    detail = "Composite signal of %+.2f across 34 strategies in a %s market." % (
        composite, regime or "mixed")
    if expectancy is not None and samples:
        detail += (" Setups scoring like this have returned %+.3fR on %d "
                   "comparable historical cases." % (expectancy, samples))

    if composite > 0.25:
        bull.points.append(Point("Price action is constructive", detail,
                                 [Evidence("Technical composite", "%+.2f" % composite,
                                           "34-strategy back-test", "", stamp,
                                           "medium" if samples else "weak")], 0.9))
    elif composite < -0.25:
        bear.points.append(Point("Price action is weak", detail,
                                 [Evidence("Technical composite", "%+.2f" % composite,
                                           "34-strategy back-test", "", stamp,
                                           "medium" if samples else "weak")], 0.9))

    if expectancy is not None and samples and samples > 100:
        if expectancy < -0.10:
            bear.points.append(Point(
                "This setup has lost money here",
                "Measured expectancy of %+.3fR per trade across %d comparable "
                "setups on this instrument, after costs." % (expectancy, samples),
                [Evidence("Measured expectancy", "%+.3fR" % expectancy,
                          "back-test on this instrument", "", stamp, "strong")], 1.3))
        elif expectancy > 0.05:
            bull.points.append(Point(
                "This setup has paid here",
                "Measured expectancy of %+.3fR per trade across %d comparable "
                "setups, after costs." % (expectancy, samples),
                [Evidence("Measured expectancy", "%+.3fR" % expectancy,
                          "back-test on this instrument", "", stamp, "strong")], 1.2))


def _sentiment_points(s, bull: Case, bear: Case, stamp: str) -> None:
    if s.score > 0.30:
        bull.points.append(Point(
            "Coverage is positive",
            "Sentiment of %+.2f across %d headlines and %d posts." % (
                s.score, s.n_news, s.n_social),
            [Evidence("News and social sentiment", "%+.2f" % s.score,
                      s.method, "", stamp, "weak")], 0.5))
    elif s.score < -0.30:
        bear.points.append(Point(
            "Coverage is negative",
            "Sentiment of %+.2f across %d headlines and %d posts." % (
                s.score, s.n_news, s.n_social),
            [Evidence("News and social sentiment", "%+.2f" % s.score,
                      s.method, "", stamp, "weak")], 0.5))
    if getattr(s, "fresh_catalyst", False):
        bear.points.append(Point(
            "A catalyst just landed",
            "A high-impact headline appeared within the last three hours. Prices "
            "and levels are unreliable immediately after news.",
            [Evidence("Fresh catalyst", "within 3 hours", s.method, "", stamp)], 0.7))


def _filing_points(filings, bull: Case, bear: Case, stamp: str) -> None:
    recent = list(filings)[:10]
    eights = [f for f in recent if f.form == "8-K"]
    offerings = [f for f in recent if f.form in ("424B5", "S-1")]
    activist = [f for f in recent if f.form == "SC 13D"]

    if len(eights) >= 3:
        bear.points.append(Point(
            "Unusually busy news flow",
            "%d current reports filed recently. Companies file 8-Ks when "
            "something happens, and a cluster is worth reading before buying."
            % len(eights),
            [Evidence("Recent 8-K filings", "%d" % len(eights), "SEC EDGAR",
                      eights[0].index_url, eights[0].filed)], 0.8))
    if offerings:
        f = offerings[0]
        bear.points.append(Point(
            "Raising money from shareholders",
            "A %s was filed on %s. New shares usually mean existing holders own "
            "a smaller share of the same business." % (f.form, f.filed),
            [Evidence("Share offering filing", f.form, "SEC EDGAR", f.index_url,
                      f.filed, "strong")], 1.2))
    if activist:
        f = activist[0]
        bull.points.append(Point(
            "An activist has taken a stake",
            "A Schedule 13D was filed on %s, meaning a holder above 5%% intends "
            "to influence the company." % f.filed,
            [Evidence("Schedule 13D", "filed %s" % f.filed, "SEC EDGAR",
                      f.index_url, f.filed)], 0.9))

    annual = [f for f in recent if f.form in ("10-K", "20-F")]
    if annual:
        f = annual[0]
        bull.points.append(Point(
            "Recent annual report available",
            "The %s filed %s is the primary source for anything above. Read the "
            "risk factors before acting on any of it." % (f.form, f.filed),
            [Evidence("Annual report", "%s filed %s" % (f.form, f.filed),
                      "SEC EDGAR", f.index_url, f.filed, "strong")], 0.3))


def _options_points(stats: Dict, bull: Case, bear: Case, stamp: str) -> None:
    iv = stats.get("atm_iv")
    pc = stats.get("put_call_oi")
    em = stats.get("expected_move")

    if em and em.get("percent"):
        bull.points.append(Point(
            "The options market has a view on the size of the move",
            "Roughly %s either way by %s, priced from the at-the-money straddle. "
            "That is a forward-looking number, unlike everything derived from "
            "past prices." % (_pct(em["percent"]), stats.get("expiry_date", "expiry")),
            [Evidence("Expected move", _pct(em["percent"]),
                      "at-the-money straddle", "", stamp, "strong")], 0.4))

    if iv and iv > 0.7:
        bear.points.append(Point(
            "Options are pricing trouble",
            "Implied volatility of %s is high. The market expects something, and "
            "premium sellers are being paid a lot to take the other side."
            % _pct(iv),
            [Evidence("At-the-money implied volatility", _pct(iv),
                      "option chain", "", stamp)], 0.8))

    if pc and pc > 1.4:
        bear.points.append(Point(
            "Options positioning is defensive",
            "%.2f puts outstanding for every call." % pc,
            [Evidence("Put/call open interest", "%.2f" % pc, "option chain", "",
                      stamp, "weak")], 0.5))
    elif pc and pc < 0.5:
        bull.points.append(Point(
            "Options positioning is optimistic",
            "Only %.2f puts outstanding for every call." % pc,
            [Evidence("Put/call open interest", "%.2f" % pc, "option chain", "",
                      stamp, "weak")], 0.5))


def _summarise(case: Case, phrase: str) -> str:
    if not case.points:
        return "Nothing in the available evidence %s." % phrase
    strong = [p for p in case.points if any(e.strength == "strong" for e in p.evidence)]
    lead = case.points[0].headline.lower()
    return "%d point%s %s, %d of them on strong evidence. The clearest is that it %s." % (
        len(case.points), "" if len(case.points) == 1 else "s", phrase,
        len(strong), lead)


def _verdict(balance: float, bull: Case, bear: Case, evidence_count: int) -> str:
    if evidence_count < 4:
        return ("Too little evidence was available to form a view. Do not read "
                "the balance below as meaningful.")
    if balance > 0.45:
        return ("The evidence leans clearly bullish. The bear case is thin, which "
                "is itself worth suspicion: look for what is missing rather than "
                "treating it as confirmation.")
    if balance > 0.15:
        return ("The evidence leans bullish, with real arguments on both sides. "
                "The bear points are the ones to monitor.")
    if balance < -0.45:
        return ("The evidence leans clearly bearish. Whatever the price action "
                "says, the business case is weak.")
    if balance < -0.15:
        return "The evidence leans bearish, though the bull case is not empty."
    return ("The two cases are genuinely balanced. That is not a reason to split "
            "the difference; it usually means waiting costs nothing.")


def _falsifiers(bull: Case, bear: Case, f, price: float, balance: float) -> List[str]:
    """What would have to happen for the leading case to be wrong."""
    out: List[str] = []
    leaning_bull = balance > 0

    if leaning_bull:
        out.append("Revenue growth turning negative for two consecutive quarters "
                   "would remove the main support for this case.")
        if f is not None and f.get("net_margin") is not None:
            out.append("Net margin falling below %s would mean the profitability "
                       "argument no longer holds."
                       % _pct(max(0.0, f.get("net_margin") - 0.05)))
        out.append("A price below %.2f, roughly 20%% under today, would say the "
                   "market has seen something this evidence has not." % (price * 0.8))
    else:
        out.append("Two consecutive quarters of margin improvement would undermine "
                   "the central bear argument.")
        out.append("A price above %.2f on rising volume would say the market "
                   "disagrees and is acting on it." % (price * 1.2))
        if f is not None and f.get("free_cashflow") is not None and f.get("free_cashflow") < 0:
            out.append("Free cash flow turning positive would remove the cash-burn "
                       "objection entirely.")

    out.append("Any 8-K describing a change of chief executive, an auditor "
               "resignation, or a restatement would invalidate this analysis "
               "until it has been read.")
    return out


def review(stored: Dict, current_price: float) -> Dict:
    """Judge a saved thesis against what happened afterwards.

    The point of writing a thesis down is being able to find out later whether
    it was right, rather than remembering it as having been right.
    """
    then = stored.get("price") or 0
    if not then:
        return {"usable": False, "reason": "The saved thesis has no price."}

    move = (current_price - then) / then
    balance = stored.get("balance", 0)
    days = max(0, (int(time.time()) - int(stored.get("created", 0))) // 86400)

    if abs(balance) < 0.15:
        outcome = "no call"
        note = ("The thesis was balanced, so the price move neither confirms nor "
                "refutes it.")
    else:
        directionally_right = (balance > 0 and move > 0) or (balance < 0 and move < 0)
        if abs(move) < 0.03:
            outcome = "undecided"
            note = "The price has barely moved, so there is nothing to judge yet."
        elif directionally_right:
            outcome = "right so far"
            note = ("The price has moved %s since, in the direction the thesis "
                    "argued. Being right for the wrong reason still counts as "
                    "luck, so check whether the reasoning held." % _pct(move))
        else:
            outcome = "wrong so far"
            note = ("The price has moved %s since, against the thesis. Worth "
                    "re-reading which point failed before writing a new one."
                    % _pct(move))

    return {"usable": True, "outcome": outcome, "note": note, "move": move,
            "days": days, "then": then, "now": current_price}


# What a closed thesis can be recorded as. Deliberately four options, not two:
# forcing every call into right or wrong is what turns a record into a story.
OUTCOMES = {
    "right":     "Right. The reasoning held and the price followed.",
    "wrong":     "Wrong. Worth naming which point failed.",
    "luck":      "Right for the wrong reason. The price moved, the reasoning did not hold.",
    "undecided": "Nothing to judge. Too little happened either way.",
}


def suggested_outcome(stored: Dict, current_price: float) -> str:
    """What the app would record, so the default is measured rather than recalled.

    A person closing a thesis months later remembers having been right. This
    proposes the answer the price actually supports; the person can override it,
    but they have to do so deliberately.
    """
    judged = review(stored, current_price)
    if not judged.get("usable"):
        return "undecided"
    return {"right so far": "right", "wrong so far": "wrong",
            "undecided": "undecided", "no call": "undecided"}.get(
                judged["outcome"], "undecided")


def scoreboard(rows: List[Dict]) -> Dict:
    """How the closed theses actually turned out.

    The only number that matters about a thesis journal. Without it, saving a
    thesis is journalling; with it, it is a record you can be wrong in front of.
    """
    closed = [r for r in rows if r.get("outcome")]
    counts = {k: 0 for k in OUTCOMES}
    for r in closed:
        if r["outcome"] in counts:
            counts[r["outcome"]] += 1

    decided = counts["right"] + counts["wrong"] + counts["luck"]
    out = {
        "open": len(rows) - len(closed),
        "closed": len(closed),
        "decided": decided,
        "counts": counts,
        "hit_rate": (counts["right"] / decided) if decided else None,
        "note": "",
    }

    if decided < 5:
        out["note"] = ("Too few closed theses to read anything into. It takes "
                       "a couple of dozen before a hit rate means much.")
    elif out["hit_rate"] is not None and counts["luck"] > counts["right"]:
        out["note"] = ("More theses were right for the wrong reason than right "
                       "for the right one. The calls are working better than "
                       "the reasoning behind them, which does not last.")
    elif out["hit_rate"] is not None:
        out["note"] = ("%d of %d decided theses were right for the stated "
                       "reason." % (counts["right"], decided))
    return out
