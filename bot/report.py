"""Terminal presentation.

The output is deliberately ordered: the call first, the reasoning second, the
evidence last. A trader reading this at 09:47 needs the decision and the levels
before anything else.
"""
from __future__ import annotations

import json
import sys
from typing import Dict, List

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .calibrate import Calibration
from .decision import Plan
from .market import Bars, format_price
from .sentiment import Sentiment
from .strategies import Regime, Signal

_ACTION_STYLE = {"BUY": "bold white on green", "SHORT": "bold white on red",
                 "WAIT": "bold black on yellow", "AVOID": "bold white on red"}


def _encodable(chars: str) -> bool:
    """Can the current output stream actually represent these characters?"""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        chars.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Windows consoles still default to cp1252 or cp437, where these glyphs raise
# UnicodeEncodeError. Crashing halfway through a report the user waited for is
# far worse than plain ASCII, so the glyph set is chosen once at import.
_FANCY = _encodable("█·│▇")
BLOCK = "█" if _FANCY else "#"
DOT = "·" if _FANCY else "."
PIPE = "│" if _FANCY else "|"
TICK = "▇" if _FANCY else "="
SEP = "   ·   " if _FANCY else "   |   "


def _fmt(value, digits=4):
    """Significant-figure format, for ratios and counts rather than prices."""
    if value is None:
        return "-"
    return "%.*g" % (digits, value) if isinstance(value, float) else str(value)


def _bar(score: float, width: int = 11) -> Text:
    """Centre-anchored bar showing a -1..+1 score."""
    mid = width // 2
    filled = int(round(abs(score) * mid))
    cells = [DOT] * width
    if score >= 0:
        for k in range(mid, min(width, mid + filled)):
            cells[k] = BLOCK
        style = "green"
    else:
        for k in range(max(0, mid - filled), mid):
            cells[k] = BLOCK
        style = "red"
    cells[mid] = PIPE if filled == 0 else cells[mid]
    return Text("".join(cells), style=style)


def render(console: Console, bars: Bars, plan: Plan, signals: List[Signal],
           composite: float, regime: Regime, calib: Calibration,
           sentiment: Sentiment, cfg: Dict, chart_note: str = "",
           strategy_report=None, show_all_signals: bool = False) -> None:
    """Print the full analysis card."""
    price = bars.last_price
    change = ""
    if bars.prev_close:
        pct = (price - bars.prev_close) / bars.prev_close * 100.0
        change = " [%s]%+.2f%%[/]" % ("green" if pct >= 0 else "red", pct)

    status = "[green]market open[/]" if bars.market_open else "[yellow]market closed[/]"
    console.print()
    console.print(Panel(
        "[bold]%s[/]  %s\n%s %s%s   ·   %s   ·   %s bars, last %s   ·   %s" % (
            bars.symbol, bars.name, bars.currency, format_price(price), change,
            bars.exchange or bars.asset_class, bars.interval,
            bars.last_bar_time, status),
        title="Instrument", border_style="cyan", padding=(0, 1)))

    if chart_note:
        console.print(Panel(chart_note, title="From your screenshot",
                            border_style="magenta", padding=(0, 1)))

    # --- the call ---------------------------------------------------------
    console.print(Panel(
        Text(" %s " % plan.action, style=_ACTION_STYLE.get(plan.action, "bold")) +
        Text("  " + plan.headline, style="bold"),
        border_style="white", padding=(0, 1)))

    # --- trade plan -------------------------------------------------------
    if plan.entry is not None and plan.action != "AVOID":
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style="dim", width=16)
        t.add_column()
        t.add_row("Entry", "[bold]%s[/] (%s)  %s" % (
            format_price(plan.entry), plan.entry_type, plan.entry_rationale))
        t.add_row("Trigger", plan.trigger)
        t.add_row("Stop", "[red]%s[/]  %s" % (format_price(plan.stop), plan.stop_rationale))
        t.add_row("Target 1", "[green]%s[/]  %s" % (format_price(plan.target1),
                                                     plan.target_rationale))
        t.add_row("Target 2", "[green]%s[/]  runner" % format_price(plan.target2))
        t.add_row("Reward:risk", "%.2f to 1   ·   needs %.0f%% win rate to break even" % (
            plan.reward_risk or 0, (plan.breakeven_rate or 0) * 100))

        pos = plan.position
        if pos.get("error"):
            t.add_row("Position", "[yellow]%s[/]" % pos["error"])
        else:
            # Cash amounts get two decimals, not the instrument's tick size.
            t.add_row("Position", "%s units   ·   %s %s notional   ·   risking %s %s (%.2f%%)" % (
                _fmt(pos["quantity"], 8), bars.currency,
                "{:,.2f}".format(pos["notional"]), bars.currency,
                "{:,.2f}".format(pos["risk_amount"]),
                pos["risk_pct_of_account"]))
            if pos.get("capped_by_max_position"):
                t.add_row("", "[yellow]size capped by max_position_pct[/]")
        t.add_row("Invalidation", plan.invalidation)
        console.print(Panel(t, title="Trade plan", border_style="green"
                            if plan.action in ("BUY", "SHORT") else "yellow"))

    # --- odds -------------------------------------------------------------
    odds = Table(show_header=False, box=None, padding=(0, 2))
    odds.add_column(style="dim", width=16)
    odds.add_column()
    if plan.probability is None:
        odds.add_row("Measured", "[yellow]no comparable history available[/]")
    else:
        flag = "" if plan.prob_reliable else "  [yellow](small sample)[/]"
        odds.add_row("Hit rate", "[bold]%.0f%%[/]  95%% CI %.0f-%.0f%%   on %d comparable setups%s" % (
            plan.probability * 100, (plan.prob_low or 0) * 100,
            (plan.prob_high or 0) * 100, plan.prob_samples, flag))
        odds.add_row("Expectancy", "%s%+.3fR[/] per trade, net of costs, over %d bars held on average" % (
            "[green]" if (plan.expectancy_r or 0) > 0 else "[red]",
            plan.expectancy_r or 0, int(calib.avg_bars_held or 0)))
        cost_style = "red" if calib.cost_r >= 0.25 else "dim"
        # When no trade was constructed the plan has no break-even of its own,
        # so fall back to the calibrator's figure rather than printing 0%.
        breakeven = plan.breakeven_rate if plan.breakeven_rate else calib.breakeven
        odds.add_row("Trading cost", "[%s]%.2fR[/] per round trip at %.0fbp   ·   "
                                     "break-even lands at %.0f%%" % (
            cost_style, calib.cost_r, calib.cost_bps, (breakeven or 0) * 100))
        if calib.holdout_hit_rate is not None:
            odds.add_row("Recent third", "%.0f%% hit rate, %+.3fR  (%d samples)" % (
                calib.holdout_hit_rate * 100, calib.holdout_expectancy or 0,
                calib.holdout_samples))
    odds.add_row("Verdict", plan.edge_verdict)
    console.print(Panel(odds, title="Measured odds, not a forecast",
                        border_style="blue"))

    if calib.curve:
        c = Table(box=None, padding=(0, 2))
        c.add_column("conviction", style="dim")
        c.add_column("samples", justify="right", style="dim")
        c.add_column("hit rate", justify="right")
        c.add_column("")
        for row in calib.curve:
            rate = row["hit_rate"]
            style = "green" if rate > (plan.breakeven_rate or 0.4) else "red"
            c.add_row("%.2f - %.2f" % (row["low"], row["high"]), str(row["n"]),
                      "[%s]%.0f%%[/]" % (style, rate * 100),
                      "[%s]%s[/]" % (style, TICK * max(1, int(rate * 24))))
        console.print(Panel(c, title="Does conviction actually predict outcome?",
                            border_style="blue"))

    # --- family roll-up ---------------------------------------------------
    # With three dozen strategies a flat list is unreadable and, worse,
    # misleading: it invites counting votes when what matters is which
    # families agree and how strongly.
    active = [s for s in signals if s.weight > 0 and abs(s.score) > 0.05]
    fam_rows: Dict[str, List[Signal]] = {}
    for sig in active:
        fam_rows.setdefault(sig.family, []).append(sig)

    f = Table(box=None, padding=(0, 1), expand=True)
    f.add_column("family", style="dim", width=15, no_wrap=True)
    f.add_column("", width=11, no_wrap=True)
    f.add_column("lean", justify="right", width=6, no_wrap=True)
    f.add_column("active", justify="right", style="dim", width=6, no_wrap=True)
    f.add_column("loudest voice", ratio=1, overflow="fold")
    for fam in sorted(fam_rows, key=lambda k: -abs(
            sum(s.contribution for s in fam_rows[k]))):
        group = fam_rows[fam]
        wsum = sum(s.weight for s in group)
        lean = (sum(s.contribution for s in group) / wsum) if wsum > 0 else 0.0
        loudest = max(group, key=lambda s: abs(s.contribution))
        f.add_row(fam, _bar(lean), "%+.2f" % lean,
                  "%d/%d" % (len(group), sum(1 for s in signals if s.family == fam)),
                  "%s: %s" % (loudest.name, loudest.reason))
    f.add_row("", "", "", "", "")
    f.add_row("[bold]composite[/]", _bar(composite), "[bold]%+.2f[/]" % composite,
              "", "regime [bold]%s[/]" % regime.describe())
    f.add_row("[bold]conviction[/]", _bar(plan.conviction),
              "[bold]%+.2f[/]" % plan.conviction, "",
              "after %.0f%% sentiment blend" % (cfg["sentiment_weight"] * 100))
    console.print(Panel(f, title="Signal by family (%d of %d strategies active)" % (
        len(active), len(signals)), border_style="cyan"))

    # --- individual strategies -------------------------------------------
    ranked = sorted(signals, key=lambda s: -abs(s.contribution))
    shown = ranked if show_all_signals else ranked[:10]
    s = Table(box=None, padding=(0, 1), expand=True)
    s.add_column("strategy", style="dim", width=20, no_wrap=True)
    s.add_column("", width=11, no_wrap=True)
    s.add_column("score", justify="right", width=6, no_wrap=True)
    s.add_column("wt", justify="right", style="dim", width=5, no_wrap=True)
    s.add_column("reading", ratio=1, overflow="fold")
    for sig in shown:
        s.add_row(sig.name, _bar(sig.score), "%+.2f" % sig.score,
                  "%.2f" % sig.weight, sig.reason)
    title = ("All %d strategies" % len(signals) if show_all_signals
             else "Top %d contributors (of %d)" % (len(shown), len(signals)))
    console.print(Panel(s, title=title, border_style="cyan"))
    if not show_all_signals:
        console.print("[dim]Pass --all-signals to see every strategy.[/]")

    # --- what this instrument taught the bot ------------------------------
    if strategy_report is not None and strategy_report.overall:
        best = strategy_report.best(minimum=40, limit=5)
        worst = strategy_report.worst(minimum=40, limit=3)
        if best or worst:
            lr = Table(box=None, padding=(0, 1), expand=True)
            lr.add_column("strategy", style="dim", width=20, no_wrap=True)
            lr.add_column("family", style="dim", width=15, no_wrap=True)
            lr.add_column("n", justify="right", width=6)
            lr.add_column("hit", justify="right", width=5)
            lr.add_column("net R", justify="right", width=7)
            lr.add_column("weight", justify="right", width=6)
            for st in best:
                lr.add_row(st.name, st.family, str(st.n),
                           "%.0f%%" % (st.hit_rate * 100),
                           "[green]%+.3f[/]" % st.expectancy,
                           "%.2f" % st.multiplier())
            if worst:
                lr.add_row("[dim]...[/]", "", "", "", "", "")
                for st in worst:
                    lr.add_row(st.name, st.family, str(st.n),
                               "%.0f%%" % (st.hit_rate * 100),
                               "[red]%+.3f[/]" % st.expectancy,
                               "%.2f" % st.multiplier())
            console.print(Panel(
                lr, title="Measured on %s: best and worst strategies (%d setups tested)"
                % (bars.symbol, strategy_report.samples), border_style="magenta"))
            console.print("[dim]Weights above were applied to this call. A strategy "
                          "that lost money on this instrument is trusted less here, "
                          "regardless of its reputation.[/]")

    # --- sentiment --------------------------------------------------------
    sent = Table(show_header=False, box=None, padding=(0, 2))
    sent.add_column(style="dim", width=16)
    sent.add_column()
    sent.add_row("Overall", "[bold]%s[/]  %+.2f   ·   scored by %s" % (
        sentiment.label(), sentiment.score, sentiment.method))
    sent.add_row("News", "%+.2f from %d headlines" % (sentiment.news_score, sentiment.n_news))
    sent.add_row("Social", "%+.2f from %d posts   ·   %d tagged bullish, %d bearish" % (
        sentiment.social_score, sentiment.n_social,
        sentiment.bullish_tags, sentiment.bearish_tags))
    if sentiment.items:
        lines = []
        for it in sentiment.items[:6]:
            colour = "green" if it.score > 0.1 else ("red" if it.score < -0.1 else "dim")
            lines.append("[%s]%+.2f[/] [dim]%4.1fh %-11s[/] %s" % (
                colour, it.score, it.age_hours, it.kind,
                it.text[:78].replace("\n", " ")))
        sent.add_row("Top items", "\n".join(lines))
    console.print(Panel(sent, title="News and social", border_style="magenta"))

    # --- risks and confirmations -----------------------------------------
    blocks = []
    if plan.risks:
        blocks.append(Text.from_markup("[bold yellow]Risks[/]\n" +
                                       "\n".join("  • %s" % r for r in plan.risks)))
    if plan.confirmations:
        blocks.append(Text.from_markup("[bold cyan]Confirm before entering[/]\n" +
                                       "\n".join("  • %s" % c for c in plan.confirmations)))
    if blocks:
        console.print(Panel(Group(*blocks), border_style="yellow"))

    console.print(Panel(
        "This is analysis software, not financial advice. Every number above is "
        "measured from historical price data and can stop describing the future "
        "at any time. Day trading loses money for the large majority of retail "
        "participants. Never risk capital you cannot afford to lose.",
        border_style="red", padding=(0, 1)))
    console.print()


def to_json(bars: Bars, plan: Plan, signals: List[Signal], composite: float,
            regime: Regime, calib: Calibration, sentiment: Sentiment) -> str:
    """Machine-readable form of the same analysis, for logging or automation."""
    return json.dumps({
        "instrument": {
            "symbol": bars.symbol, "name": bars.name,
            "asset_class": bars.asset_class, "currency": bars.currency,
            "price": bars.last_price, "interval": bars.interval,
            "market_open": bars.market_open, "last_bar": bars.last_bar_time,
        },
        "call": {
            "action": plan.action, "headline": plan.headline,
            "conviction": plan.conviction, "entry": plan.entry,
            "entry_type": plan.entry_type, "trigger": plan.trigger,
            "stop": plan.stop, "target1": plan.target1, "target2": plan.target2,
            "reward_risk": plan.reward_risk, "breakeven_rate": plan.breakeven_rate,
            "invalidation": plan.invalidation, "position": plan.position,
        },
        "odds": {
            "hit_rate": plan.probability, "ci_low": plan.prob_low,
            "ci_high": plan.prob_high, "samples": plan.prob_samples,
            "reliable": plan.prob_reliable, "expectancy_r": plan.expectancy_r,
            "total_backtest_samples": calib.samples,
            "cost_r": calib.cost_r, "cost_bps": calib.cost_bps,
            "holdout_hit_rate": calib.holdout_hit_rate,
            "holdout_expectancy": calib.holdout_expectancy,
            "curve": calib.curve, "verdict": plan.edge_verdict,
        },
        "signals": [{"name": s.name, "score": s.score, "weight": s.weight,
                     "reason": s.reason} for s in signals],
        "composite": composite,
        "regime": {"trend": regime.trend, "volatility": regime.volatility,
                   "direction": regime.direction, "adx": regime.adx,
                   "detail": regime.detail},
        "sentiment": {
            "score": sentiment.score, "label": sentiment.label(),
            "news": sentiment.news_score, "social": sentiment.social_score,
            "n_news": sentiment.n_news, "n_social": sentiment.n_social,
            "method": sentiment.method,
            "items": [{"text": i.text, "score": i.score, "kind": i.kind,
                       "age_hours": i.age_hours, "source": i.source}
                      for i in sentiment.items],
        },
        "risks": plan.risks, "confirmations": plan.confirmations,
    }, indent=2)
