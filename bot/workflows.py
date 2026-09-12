"""Automated routines: the jobs you would otherwise remember to do by hand.

Each workflow is a single function that gathers, decides and reports. None of
them place orders or run in the background on a timer; they run when asked,
either from the interface or from a scheduled task you set up yourself.

That restraint is deliberate. A locally run tool that claims to be watching
markets while the laptop is asleep would be lying, and a trading tool that
lies about when it last looked is worse than no tool.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import alerts as alerts_mod
from . import config as config_mod
from . import database as db
from . import engine
from . import portfolio as portfolio_mod
from . import screener as screener_mod
from .market import DataError, fetch_bars, format_price


@dataclass
class Step:
    """One thing a workflow did, and what came of it."""
    label: str
    detail: str
    status: str = "ok"        # ok | attention | failed
    data: Dict = field(default_factory=dict)


@dataclass
class RunReport:
    """What a workflow found."""
    name: str
    started: int
    finished: int
    steps: List[Step] = field(default_factory=list)
    headline: str = ""
    attention: List[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return max(0.0, self.finished - self.started)


WORKFLOWS: Dict[str, Dict] = {
    "morning": {
        "label": "Morning review",
        "why": "Checks alerts, marks open positions, and re-runs the watchlist. "
               "The things worth knowing before the day starts.",
    },
    "watchlist": {
        "label": "Re-analyse the watchlist",
        "why": "Runs a full analysis on every instrument you are watching and "
               "reports only the ones where the call changed.",
    },
    "positions": {
        "label": "Position check",
        "why": "Marks every open trade to market and flags anything at its stop "
               "or target, or that has grown too large.",
    },
    "hunt": {
        "label": "Find candidates",
        "why": "Runs the quality screen and analyses the best few results, so "
               "you start from evidence rather than a ticker someone mentioned.",
    },
}


def run(name: str, cfg: Optional[Dict] = None, db_path: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
        user: int = 1) -> RunReport:
    """Run a named workflow on one person's watchlist, positions and alerts."""
    cfg = cfg or config_mod.load()
    started = int(time.time())
    report = RunReport(name=WORKFLOWS.get(name, {}).get("label", name),
                       started=started, finished=started)

    def step(msg):
        if progress:
            progress(msg)

    handlers = {"morning": _morning, "watchlist": _watchlist,
                "positions": _positions, "hunt": _hunt}
    handler = handlers.get(name)
    if handler is None:
        report.steps.append(Step("Unknown workflow", "No routine named %r." % name,
                                 "failed"))
        report.finished = int(time.time())
        return report

    conn = db.connect(db_path)
    try:
        handler(conn, cfg, report, step, db_path, user)
    finally:
        conn.close()

    report.finished = int(time.time())
    report.attention = [s.detail for s in report.steps if s.status == "attention"]
    if not report.headline:
        count = len(report.attention)
        report.headline = ("%d thing%s attention." % (
            count, " needs" if count == 1 else "s need")
            if count else "Nothing needs attention.")
    return report


# ---------------------------------------------------------------------------
# The routines
# ---------------------------------------------------------------------------

def _check_alerts(conn, report: RunReport, step, user=1) -> None:
    step("Checking alerts")
    result = alerts_mod.check(conn)
    if not result["checked"]:
        report.steps.append(Step("Alerts", "No alerts are set.", "ok"))
        return
    if result["fired"]:
        for hit in result["fired"]:
            report.steps.append(Step("Alert: %s" % hit.symbol, hit.message,
                                     "attention", {"alert_id": hit.alert_id}))
    else:
        report.steps.append(Step(
            "Alerts", "Checked %d alert%s, none triggered." % (
                result["checked"], "" if result["checked"] == 1 else "s"), "ok"))
    for err in result["errors"]:
        report.steps.append(Step("Alert check failed", "%s: %s" % (
            err["symbol"], err["error"]), "failed"))


def _check_positions(conn, cfg, report: RunReport, step, user=1) -> None:
    step("Marking positions to market")
    rows = db.list_trades(conn, status="open", user=user)
    if not rows:
        report.steps.append(Step("Positions", "Nothing open.", "ok"))
        return

    view = portfolio_mod.build(rows, cfg["account_size"], with_correlation=True)
    for p in view.positions:
        if p.price is None:
            report.steps.append(Step("Position: %s" % p.symbol,
                                     "No price available.", "failed"))
            continue
        at_stop = ((p.price <= p.stop) if p.direction > 0
                   else (p.price >= p.stop))
        at_target = (p.target is not None and
                     ((p.price >= p.target) if p.direction > 0
                      else (p.price <= p.target)))
        if at_stop:
            report.steps.append(Step(
                "Position: %s" % p.symbol,
                "%s is at %s, past its stop of %s. It should be closed."
                % (p.symbol, format_price(p.price), format_price(p.stop)),
                "attention", {"trade_id": p.trade_id}))
        elif at_target:
            report.steps.append(Step(
                "Position: %s" % p.symbol,
                "%s is at %s, at or past its target of %s."
                % (p.symbol, format_price(p.price), format_price(p.target)),
                "attention", {"trade_id": p.trade_id}))

    for warning in view.warnings:
        report.steps.append(Step("Portfolio", warning, "attention"))
    for note in view.observations[:3]:
        report.steps.append(Step("Portfolio", note, "ok"))


def _positions(conn, cfg, report, step, db_path, user=1) -> None:
    _check_positions(conn, cfg, report, step, user)


def _watchlist(conn, cfg, report: RunReport, step, db_path, user=1) -> None:
    rows = db.watchlist(conn, user=user)
    if not rows:
        report.steps.append(Step(
            "Watchlist", "Nothing on the watchlist. Add instruments to have them "
            "re-analysed automatically.", "ok"))
        return

    # What the bot said last time, so only genuine changes get reported.
    previous = {}
    for row in rows:
        last = db.recent_runs(conn, symbol=row["symbol"], limit=1, user=user)
        if last:
            previous[row["symbol"]] = last[0]["action"]

    changed = 0
    for row in rows:
        symbol, interval = row["symbol"], row["interval"] or cfg["interval"]
        step("Analysing %s" % symbol)
        try:
            result = engine.analyse(symbol, cfg, interval=interval,
                                    with_news=False, record=True,
                                    db_path=db_path, source="workflow")
        except (DataError, Exception) as exc:
            report.steps.append(Step("Watchlist: %s" % symbol,
                                     str(exc)[:140], "failed"))
            continue

        db.watch_touch(conn, symbol, user=user)
        was = previous.get(symbol)
        now = result.plan.action
        if was and was != now:
            changed += 1
            report.steps.append(Step(
                "Changed: %s" % symbol,
                "%s went from %s to %s. %s" % (symbol, was, now,
                                               result.plan.headline),
                "attention", {"symbol": symbol, "interval": interval}))
        else:
            report.steps.append(Step(
                "Watchlist: %s" % symbol,
                "%s, unchanged. %s" % (now, result.plan.headline), "ok",
                {"symbol": symbol, "interval": interval}))

    report.headline = ("%d of %d watched instruments changed their call."
                       % (changed, len(rows)) if changed
                       else "None of the %d watched instruments changed." % len(rows))


def _morning(conn, cfg, report: RunReport, step, db_path, user=1) -> None:
    _check_alerts(conn, report, step, user)
    _check_positions(conn, cfg, report, step, user)

    rows = db.watchlist(conn, user=user)
    if rows:
        step("Re-checking the watchlist")
        # Prices only here. A full re-analysis of a long watchlist would make a
        # morning check take minutes, so the deeper pass is its own routine.
        def grab(row):
            try:
                return row["symbol"], fetch_bars(row["symbol"], row["interval"] or "1d")
            except Exception:
                return row["symbol"], None

        with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            for symbol, bars in pool.map(grab, rows):
                if bars is None:
                    continue
                if bars.prev_close:
                    move = (bars.last_price - bars.prev_close) / bars.prev_close
                    if abs(move) >= 0.04:
                        report.steps.append(Step(
                            "Watchlist: %s" % symbol,
                            "%s moved %.1f%% to %s since the previous close."
                            % (symbol, move * 100, format_price(bars.last_price)),
                            "attention", {"symbol": symbol}))

    counts = db.stats(conn, user=user)
    report.steps.append(Step(
        "Summary",
        "%d analyses recorded, %d trades logged, %d alerts set, %d instruments watched."
        % (counts["runs"], counts["trades"], counts["alerts"], counts["watchlist"]),
        "ok"))


def _hunt(conn, cfg, report: RunReport, step, db_path, user=1) -> None:
    preset = screener_mod.PRESETS["quality_value"]
    step("Screening for quality at a reasonable price")
    try:
        found = screener_mod.run(universe=preset["universe"],
                                 filters=preset["filters"], limit=12)
    except Exception as exc:
        report.steps.append(Step("Screen failed", str(exc)[:140], "failed"))
        return

    report.steps.append(Step(
        "Screen", "%s: %d of %d scanned passed." % (
            preset["label"], len(found.passed), found.scanned), "ok"))
    if not found.passed:
        for note in found.notes:
            report.steps.append(Step("Screen", note, "ok"))
        return

    for candidate in found.passed[:4]:
        step("Analysing %s" % candidate.symbol)
        try:
            result = engine.analyse(candidate.symbol, cfg, interval=cfg["interval"],
                                    with_news=False, record=True, db_path=db_path,
                                    source="workflow")
        except Exception as exc:
            report.steps.append(Step("Candidate: %s" % candidate.symbol,
                                     str(exc)[:140], "failed"))
            continue
        status = "attention" if result.plan.action in ("BUY", "WAIT") else "ok"
        report.steps.append(Step(
            "Candidate: %s" % candidate.symbol,
            "%s. %s" % (result.plan.action, result.plan.headline), status,
            {"symbol": candidate.symbol, "interval": cfg["interval"]}))

    report.headline = "%d candidates passed the screen; the best few were analysed." \
                      % len(found.passed)
