"""AI financial reasoning over the evidence this bot has already gathered.

The model is never asked what it thinks about a stock. It is given the measured
facts and asked to reason about *those*, because a language model's unaided
opinion on a ticker is a recollection of its training data, not analysis, and
it will state it with exactly the same confidence either way.

Three safeguards:

* **Everything in the prompt is a fact this bot computed or fetched**, with its
  source. The model synthesises; it does not supply the data.
* **It is asked to name what would change its mind** and to say when the
  evidence is insufficient, which is the behaviour that makes it useful rather
  than merely fluent.
* **Nothing here is required.** With no API key the rest of the bot works
  unchanged, and the interface says the reasoning step is unavailable instead
  of quietly degrading.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 2000


@dataclass
class Reasoning:
    """What the model concluded, and whether it could be asked at all."""
    available: bool
    summary: str = ""
    key_question: str = ""
    strongest_bull: str = ""
    strongest_bear: str = ""
    what_would_change_it: List[str] = field(default_factory=list)
    blind_spots: List[str] = field(default_factory=list)
    confidence: str = ""
    model: str = ""
    reason_unavailable: str = ""
    raw: str = ""


def available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _facts_block(payload: Dict) -> str:
    """Render the gathered evidence as compact labelled text."""
    lines = []
    for section, content in payload.items():
        if not content:
            continue
        lines.append("## %s" % section)
        if isinstance(content, dict):
            for key, value in content.items():
                if value is None or value == "":
                    continue
                lines.append("- %s: %s" % (key, value))
        elif isinstance(content, list):
            for item in content[:14]:
                lines.append("- %s" % item)
        else:
            lines.append(str(content))
        lines.append("")
    return "\n".join(lines)


_PROMPT = """You are reasoning about an investment for someone who will risk \
their own money on the result.

Everything below was measured or fetched by a tool. Treat it as the only \
evidence available. Do not add facts from memory about this company: if \
something important is missing, say that it is missing.

{facts}

Respond with ONLY a JSON object, no prose around it:

{{
  "summary": "3-4 sentences on what this evidence actually supports. Plain \
language. No hedging filler.",
  "key_question": "The single question that most determines whether this works. \
One sentence.",
  "strongest_bull": "The strongest argument for, naming the specific evidence.",
  "strongest_bear": "The strongest argument against, naming the specific evidence.",
  "what_would_change_it": ["2-4 specific, observable things that would change \
this conclusion"],
  "blind_spots": ["2-3 things this evidence cannot see that matter for this \
particular company"],
  "confidence": "high | medium | low, based on how much evidence was available \
and how consistent it is"
}}

Rules:
- Never recommend buying or selling. Describe what the evidence supports.
- If the evidence is thin or contradictory, say so and set confidence to low.
- Prefer the specific to the general. "Margins fell from 28% to 19% over three \
years" beats "margins are under pressure".
- Blind spots must be specific to this company or sector, not generic warnings \
about markets being unpredictable."""


def analyse(payload: Dict, model: Optional[str] = None) -> Reasoning:
    """Ask the model to reason over gathered evidence."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return Reasoning(False, reason_unavailable=(
            "No Anthropic API key is set, so the reasoning step is unavailable. "
            "Every measured figure elsewhere is unaffected."))
    try:
        import anthropic
    except ImportError:
        return Reasoning(False, reason_unavailable=(
            "The anthropic package is not installed."))

    chosen = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
    prompt = _PROMPT.format(facts=_facts_block(payload))

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=chosen, max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text").strip()
    except Exception as exc:
        return Reasoning(False, reason_unavailable=(
            "The reasoning request failed: %s" % str(exc)[:160]))

    parsed = _parse(text)
    if parsed is None:
        return Reasoning(False, raw=text, reason_unavailable=(
            "The model's reply could not be read as structured output."))

    return Reasoning(
        available=True,
        summary=parsed.get("summary", ""),
        key_question=parsed.get("key_question", ""),
        strongest_bull=parsed.get("strongest_bull", ""),
        strongest_bear=parsed.get("strongest_bear", ""),
        what_would_change_it=_as_list(parsed.get("what_would_change_it")),
        blind_spots=_as_list(parsed.get("blind_spots")),
        confidence=(parsed.get("confidence") or "").lower(),
        model=chosen, raw=text)


def _parse(text: str) -> Optional[Dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _as_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()][:5]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def build_payload(symbol: str, price: float, fundamentals=None, quality=None,
                  valuation=None, risk_profile=None, plan=None,
                  calibration=None, sentiment=None, sec_history=None,
                  filings=None, options_stats=None, thesis=None) -> Dict:
    """Collect everything measured into a compact, labelled brief."""
    out: Dict = {"Instrument": {"symbol": symbol, "price": round(price, 4)}}

    if fundamentals is not None:
        f = fundamentals
        out["Instrument"].update({
            "name": f.name, "sector": f.sector or "unknown",
            "industry": f.industry or "unknown"})
        block = {}
        for key in ("market_cap", "pe", "forward_pe", "pb", "ps", "ev_ebitda",
                    "net_margin", "operating_margin", "roe", "revenue_growth",
                    "free_cashflow", "fcf_yield", "debt_to_equity",
                    "current_ratio", "beta", "dividend_yield", "short_pct_float"):
            metric = f.metrics.get(key)
            if metric and metric.known:
                block[metric.label] = metric.display()
        if block:
            out["Fundamentals (Yahoo Finance)"] = block
        if f.summary:
            out["Business description (from SEC profile)"] = f.summary[:900]

    if quality and quality.get("known"):
        out["Quality checks"] = {
            "passed": ", ".join(p["label"] for p in quality["passed"]) or "none",
            "failed": ", ".join(p["label"] for p in quality["failed"]) or "none",
            "not reported": ", ".join(p["label"] for p in quality["unknown"]) or "none",
        }

    if sec_history and sec_history.get("available"):
        block = {}
        cagr = sec_history.get("cagr") or {}
        for key, label in (("revenue", "Revenue 5-year CAGR"),
                           ("net_income", "Net income 5-year CAGR"),
                           ("operating_income", "Operating income 5-year CAGR")):
            if cagr.get(key) is not None:
                block[label] = "%.1f%%" % (cagr[key] * 100)
        rev = (sec_history.get("table") or {}).get("revenue") or []
        if rev:
            block["Revenue by fiscal year (as filed)"] = ", ".join(
                "FY%s %.1fB" % (r["fy"], r["value"] / 1e9) for r in rev[-5:])
        derived = sec_history.get("derived") or {}
        margins = [(y, derived[y]["net_margin"]) for y in sorted(derived)
                   if "net_margin" in derived[y]]
        if margins:
            block["Net margin by year (as filed)"] = ", ".join(
                "FY%s %.1f%%" % (y, m * 100) for y, m in margins[-5:])
        if block:
            out["Filed financials (SEC EDGAR)"] = block

    if valuation and valuation.get("usable"):
        block = {"modelled range": "%.2f to %.2f" % (valuation["low"], valuation["high"]),
                 "midpoint": "%.2f" % valuation["midpoint"],
                 "agreement between models": valuation.get("agreement", "")}
        if valuation.get("upside") is not None:
            block["gap to price"] = "%.1f%%" % (valuation["upside"] * 100)
        for model in valuation.get("models", []):
            if getattr(model, "implied_growth", None) is not None:
                block["growth the current price implies"] = \
                    "%.1f%% a year for a decade" % (model.implied_growth * 100)
        out["Valuation models"] = block

    if risk_profile is not None:
        r = risk_profile
        block = {}
        if r.volatility:
            block["annualised volatility"] = "%.1f%%" % (r.volatility * 100)
        if r.max_drawdown is not None:
            block["worst peak-to-trough fall"] = "%.1f%%" % (r.max_drawdown * 100)
        if r.var_95 is not None:
            block["worst day in twenty"] = "%.2f%%" % (r.var_95 * 100)
        if r.beta is not None:
            block["beta versus the market"] = "%.2f" % r.beta
        if r.sharpe is not None:
            block["Sharpe ratio"] = "%.2f" % r.sharpe
        block["measured over"] = "%d returns" % r.samples
        out["Measured risk"] = block

    if plan is not None:
        block = {"call": plan.action, "reasoning given": plan.headline,
                 "conviction": "%+.2f" % plan.conviction}
        if plan.entry:
            block["entry"] = "%.4g" % plan.entry
            block["stop"] = "%.4g" % plan.stop
            block["first target"] = "%.4g" % plan.target1
            block["reward to risk"] = "%.2f" % (plan.reward_risk or 0)
        out["Technical plan (34-strategy engine)"] = block

    if calibration is not None and calibration.hit_rate is not None:
        out["Back-tested odds for this setup"] = {
            "hit rate": "%.0f%%" % (calibration.hit_rate * 100),
            "sample size": "%d comparable setups" % calibration.bucket_samples,
            "expectancy after costs": "%+.3fR per trade" % (calibration.expectancy_r or 0),
            "break-even needed": "%.0f%%" % (calibration.breakeven * 100),
            "trading cost": "%.2fR per round trip" % calibration.cost_r,
        }

    if sentiment is not None and (sentiment.n_news or sentiment.n_social):
        out["News and social"] = {
            "overall": "%+.2f (%s)" % (sentiment.score, sentiment.label()),
            "sample": "%d headlines, %d posts" % (sentiment.n_news, sentiment.n_social),
            "scored by": sentiment.method,
        }
        heads = [i.text[:130] for i in sentiment.items[:6] if i.kind == "news"]
        if heads:
            out["Recent headlines"] = heads

    if filings:
        out["Recent SEC filings"] = [
            "%s filed %s: %s" % (f.form, f.filed, f.meaning) for f in list(filings)[:8]]

    if options_stats:
        block = {}
        if options_stats.get("atm_iv"):
            block["implied volatility"] = "%.1f%%" % (options_stats["atm_iv"] * 100)
        em = options_stats.get("expected_move")
        if em and em.get("percent"):
            block["expected move by expiry"] = "%.1f%%" % (em["percent"] * 100)
        if options_stats.get("put_call_oi"):
            block["put/call open interest"] = "%.2f" % options_stats["put_call_oi"]
        if block:
            out["Options market"] = block

    if thesis is not None:
        out["Evidence-based case"] = {
            "bull points": "; ".join(p.headline for p in thesis.bull.points) or "none",
            "bear points": "; ".join(p.headline for p in thesis.bear.points) or "none",
            "balance": "%+.2f (positive is bullish)" % thesis.balance,
        }
        if thesis.unknowns:
            out["What could not be measured"] = thesis.unknowns

    return out
