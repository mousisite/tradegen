"""The analysis pipeline as a plain function.

Both front ends call this: the terminal CLI and the web app. Keeping the
pipeline here rather than inside either one means they cannot drift into giving
different answers to the same question, which for a tool whose whole value is
honesty would be fatal.

Nothing in this module prints. It returns data; presentation is somebody
else's job.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import config as config_mod
from . import database as db_mod
from .calibrate import Calibration, calibrate
from .decision import Plan, decide
from .learning import StrategyReport, learned_weights, measure_strategies
from .levels import Level, build_levels
from .market import Bars, DataError, fetch_bars, resolve
from .sentiment import Sentiment, analyse as analyse_sentiment
from .strategies import Context, Regime, Signal, build_context, evaluate


@dataclass
class Research:
    """The fundamental side: what the business is, worth, and risks.

    Every field is optional. An instrument with no filings, no earnings or no
    options still produces a Research object saying so, rather than failing.
    """
    symbol: str
    price: float
    fundamentals: Optional[object] = None
    quality: Optional[Dict] = None
    valuation: Optional[Dict] = None
    sec_history: Optional[Dict] = None
    scores: Optional[Dict] = None
    filings: List = field(default_factory=list)
    risk: Optional[object] = None
    options: Optional[Dict] = None
    option_ideas: List[Dict] = field(default_factory=list)
    thesis: Optional[object] = None
    reasoning: Optional[object] = None
    elapsed: float = 0.0
    unavailable: List[str] = field(default_factory=list)


@dataclass
class Analysis:
    """Everything one analysis produced."""
    bars: Bars
    context: Context
    signals: List[Signal]
    composite: float
    regime: Regime
    levels: List[Level]
    calibration: Calibration
    sentiment: Sentiment
    plan: Plan
    strategy_report: Optional[StrategyReport] = None
    learned: Dict[str, float] = field(default_factory=dict)
    cost_bps: float = 0.0
    run_id: Optional[int] = None
    calendar: Optional[object] = None
    elapsed: float = 0.0
    chart_note: str = ""

    @property
    def symbol(self) -> str:
        return self.bars.symbol


def analyse(symbol: str, cfg: Dict, interval: Optional[str] = None,
            with_news: bool = True, with_learning: bool = True,
            record: bool = True, db_path: Optional[str] = None,
            user: int = 1,
            progress: Optional[Callable[[str], None]] = None,
            source: str = "engine") -> Analysis:
    """Run the full pipeline for one instrument.

    `progress` is called with a short status string at each stage, so a caller
    can show something while the user waits. It is optional and ignored when
    absent.
    """
    started = time.time()

    def step(message: str) -> None:
        if progress:
            progress(message)

    interval = interval or cfg["interval"]

    step("Resolving %s" % symbol)
    info = resolve(symbol)
    resolved, name = info["symbol"], info.get("name") or info["symbol"]

    step("Fetching market data")
    bars = fetch_bars(resolved, interval)

    step("Computing indicators")
    ctx = build_context(bars)
    last = len(bars) - 1
    signals, composite, regime = evaluate(ctx, last, cfg["strategy_weights"])
    levels = build_levels(bars, float(ctx.atr[last]))
    cost_bps = config_mod.cost_bps_for(bars.asset_class, cfg)
    # Scaled to the interval, so a "day trade" on 5-minute bars does not quietly
    # become a five-week hold when the same number is applied to daily bars.
    horizon = config_mod.horizon_for(bars.interval, cfg)

    # Measure each strategy on this instrument, then re-weight by what actually
    # worked here rather than by reputation.
    report = None
    learned: Dict[str, float] = {}
    if with_learning:
        step("Measuring strategies on history")
        report = measure_strategies(
            ctx, cfg["stop_atr_multiple"], cfg["target_atr_multiple"],
            horizon, cfg["calibration_stride"], cost_bps,
            weights=cfg["strategy_weights"])
        learned = learned_weights(report, regime)
        signals, composite, regime = evaluate(
            ctx, last, cfg["strategy_weights"], learned=learned, regime=regime)

    step("Back-testing this setup")
    calib = calibrate(ctx, composite,
                      stop_atr=cfg["stop_atr_multiple"],
                      target_atr=cfg["target_atr_multiple"],
                      horizon=horizon,
                      stride=cfg["calibration_stride"],
                      weights=cfg["strategy_weights"],
                      min_bucket=cfg["min_bucket_samples"],
                      cost_bps=cost_bps)

    # The calendar is its own round trip and nothing above depends on it, so it
    # runs while the news is being read rather than after.
    from concurrent.futures import ThreadPoolExecutor
    from . import catalysts as catalysts_mod

    pool = ThreadPoolExecutor(max_workers=2)
    calendar_job = pool.submit(catalysts_mod.fetch, bars.symbol)

    if with_news:
        step("Reading news and social")
        sentiment = analyse_sentiment(resolved, name,
                                      use_llm=cfg.get("use_llm_sentiment", True))
    else:
        sentiment = Sentiment(0.0, 0.0, 0.0, 0, 0, 0, 0, "skipped",
                              notes=["Sentiment analysis was skipped."])

    try:
        calendar = calendar_job.result(timeout=12)
    except Exception:
        calendar = catalysts_mod.Calendar(symbol=bars.symbol, available=False)
    finally:
        pool.shutdown(wait=False)

    step("Deciding")
    plan = decide(ctx, bars, signals, composite, regime, calib, sentiment,
                  levels, cfg)

    # A scheduled event inside the holding window changes the trade, so it
    # belongs with the plan's other risks rather than in a separate corner.
    for note in catalysts_mod.warnings_for(calendar, bars.interval, horizon,
                                           plan.direction):
        if note not in plan.risks:
            plan.risks.append(note)

    run_id = None
    if record:
        try:
            conn = db_mod.connect(db_path)
            try:
                run_id = db_mod.record_run(conn, bars, plan, signals, composite,
                                           regime, calib, sentiment,
                                           source=source, user=user)
                if report is not None:
                    db_mod.save_strategy_stats(conn, bars.symbol, bars.interval,
                                               report)
            finally:
                conn.close()
        except Exception:
            # A database problem must never destroy a completed analysis.
            run_id = None

    return Analysis(bars=bars, context=ctx, signals=signals, composite=composite,
                    regime=regime, levels=levels, calibration=calib,
                    sentiment=sentiment, plan=plan, strategy_report=report,
                    learned=learned, cost_bps=cost_bps, run_id=run_id,
                    calendar=calendar, elapsed=time.time() - started)


def research(symbol: str, price: float, cfg: Dict, analysis: Optional[Analysis] = None,
             with_options: bool = True, with_reasoning: bool = True,
             progress: Optional[Callable[[str], None]] = None) -> Research:
    """The fundamental, valuation and risk work, plus a two-sided thesis.

    Independent pieces run concurrently because each is a separate network
    round trip and they do not depend on one another. Any piece that fails is
    recorded in `unavailable` rather than taking the whole report down: a
    company with no options listed should still get a valuation.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import fundamentals as fund_mod
    from . import options as options_mod
    from . import reasoning as reasoning_mod
    from . import risk as risk_mod
    from . import sec as sec_mod
    from . import thesis as thesis_mod
    from . import valuation as val_mod

    started = time.time()
    out = Research(symbol=symbol.upper(), price=price)

    def step(message: str) -> None:
        if progress:
            progress(message)

    jobs = {
        "fundamentals": lambda: fund_mod.load(symbol),
        "sec_history": lambda: sec_mod.financial_history(symbol, years=8),
        "filings": lambda: sec_mod.recent_material(symbol, limit=10),
        "risk": lambda: risk_mod.profile(symbol, "1d"),
    }
    if with_options:
        jobs["options"] = lambda: options_mod.load_chain(symbol)

    step("Gathering fundamentals, filings and risk")
    results: Dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = None
                out.unavailable.append("%s: %s" % (name, str(exc)[:110]))

    out.fundamentals = results.get("fundamentals")
    out.sec_history = results.get("sec_history")
    out.filings = results.get("filings") or []
    out.risk = results.get("risk")

    chain = results.get("options")
    if chain is not None:
        try:
            out.options = options_mod.analyse(chain)
        except Exception as exc:
            out.unavailable.append("options analysis: %s" % str(exc)[:110])

    if out.fundamentals is not None:
        step("Valuing the business")
        out.quality = fund_mod.quality_score(out.fundamentals)

    # Published models applied to the filed figures. Runs on data already
    # fetched, so it costs no extra network request.
    if out.sec_history:
        from . import quality as quality_mod
        try:
            out.scores = quality_mod.assess(
                out.sec_history,
                out.fundamentals.get("market_cap") if out.fundamentals else None,
                getattr(out.fundamentals, "sector", "") if out.fundamentals else "")
        except Exception as exc:
            out.scores = {"available": False,
                          "reason": "The scored models could not be run: %s" % exc}
        try:
            models = [val_mod.discounted_cash_flow(out.fundamentals, price),
                      val_mod.reverse_dcf(out.fundamentals, price),
                      val_mod.multiples(out.fundamentals, price)]
            out.valuation = val_mod.combine(models, price)
        except Exception as exc:
            out.unavailable.append("valuation: %s" % str(exc)[:110])

    technical = None
    if analysis is not None:
        technical = {
            "composite": analysis.composite,
            "regime": analysis.regime.trend,
            "expectancy": analysis.calibration.expectancy_r,
            "hit_rate": analysis.calibration.hit_rate,
            "samples": analysis.calibration.bucket_samples,
        }

    step("Building the case")
    out.thesis = thesis_mod.build(
        symbol, price, fundamentals=out.fundamentals, quality=out.quality,
        valuation=out.valuation, risk_profile=out.risk, technical=technical,
        sentiment=analysis.sentiment if analysis else None,
        filings=out.filings, options_stats=out.options,
        sec_history=out.sec_history)

    if chain is not None and out.thesis is not None:
        view = ("bullish" if out.thesis.balance > 0.15
                else "bearish" if out.thesis.balance < -0.15 else "neutral")
        try:
            out.option_ideas = options_mod.suggest(chain, view,
                                                   abs(out.thesis.balance))
        except Exception:
            pass

    if with_reasoning and reasoning_mod.available():
        step("Reasoning over the evidence")
        payload = reasoning_mod.build_payload(
            symbol, price, fundamentals=out.fundamentals, quality=out.quality,
            valuation=out.valuation, risk_profile=out.risk,
            plan=analysis.plan if analysis else None,
            calibration=analysis.calibration if analysis else None,
            sentiment=analysis.sentiment if analysis else None,
            sec_history=out.sec_history, filings=out.filings,
            options_stats=out.options, thesis=out.thesis)
        out.reasoning = reasoning_mod.analyse(payload)
    elif with_reasoning:
        out.reasoning = reasoning_mod.Reasoning(
            False, reason_unavailable=(
                "No Anthropic API key is set, so the reasoning step is "
                "unavailable. Every measured figure on this page is unaffected."))

    out.elapsed = time.time() - started
    return out
