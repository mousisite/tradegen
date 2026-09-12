#!/usr/bin/env python
"""Paper trading journal.

Record what you actually took, let the bot mark it to market, and measure
whether the process works before any real money is involved.

    python paper.py take AAPL              open a trade from the bot's last plan
    python paper.py open AAPL --entry 315 --stop 313.5 --target 317.5 --qty 60
    python paper.py check                  mark open trades to market
    python paper.py close 3 --price 317.5  close a position
    python paper.py list                   show open positions
    python paper.py stats                  realised performance so far
    python paper.py leaderboard            which strategies measure best
    python paper.py runs                   recent analyses

Run with --help, or `python paper.py <command> --help`, for the full options.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must run before bot.report is imported: that module chooses its glyph set
# from the stream encoding, and legacy Windows code pages cannot encode it.
from bot.terminal import prepare_output
prepare_output()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bot import config as config_mod
from bot import database as db
from bot.market import DataError, fetch_bars, format_price

console = Console()


def _age(ts: int) -> str:
    """Human-readable age of a timestamp."""
    secs = max(0, int(time.time()) - int(ts))
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _side(direction: int) -> str:
    return "LONG" if direction > 0 else "SHORT"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_open(args, conn) -> int:
    """Open a position from explicit numbers."""
    direction = -1 if args.short else 1
    try:
        trade_id = db.open_trade(
            conn, symbol=args.symbol, interval=args.interval, direction=direction,
            entry=args.entry, stop=args.stop, target1=args.target,
            target2=args.target2, quantity=args.qty,
            risk_amount=(abs(args.entry - args.stop) * args.qty) if args.qty else None,
            account=args.account, strategy_note=args.note or "", notes=args.notes or "")
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2

    risk = abs(args.entry - args.stop)
    console.print(Panel(
        "[bold]#%d  %s %s[/]\nentry %s   stop %s   target %s\n"
        "risk %s per unit%s" % (
            trade_id, _side(direction), args.symbol.upper(),
            format_price(args.entry), format_price(args.stop),
            format_price(args.target) if args.target else "-",
            format_price(risk),
            "   ·   %s units   ·   %s total risk" % (
                args.qty, format_price(risk * args.qty)) if args.qty else ""),
        title="Opened", border_style="green"))
    return 0


def cmd_take(args, conn) -> int:
    """Open a position straight from the bot's most recent plan for a symbol.

    This is the path that keeps the journal honest: the trade recorded is the
    trade the bot actually proposed, not a remembered version of it.
    """
    rows = db.recent_runs(conn, symbol=args.symbol, limit=10)
    usable = [r for r in rows if r["action"] in ("BUY", "SHORT", "WAIT")
              and r["entry"] and r["stop"]]
    if not usable:
        sym = args.symbol.upper()
        if not rows:
            console.print("[yellow]No analysis recorded for %s yet.[/]" % sym)
            console.print("[dim]Run: run.bat --symbol %s[/]" % sym)
            return 1
        # Distinguish "never analysed" from "analysed and told you not to".
        latest = rows[0]
        console.print(Panel(
            "The most recent %s analysis said [bold]%s[/], %s ago.\n\n%s\n\n"
            "[dim]The journal only takes plans the bot actually proposed. To "
            "record a trade it advised against, use `paper.py open` with "
            "explicit numbers.[/]" % (
                sym, latest["action"], _age(latest["ts"]),
                latest["headline"] or ""),
            title="Nothing to take", border_style="yellow"))
        return 1

    run = usable[0]
    age_min = (int(time.time()) - int(run["ts"])) / 60.0
    if age_min > args.max_age and not args.force:
        console.print("[yellow]The most recent %s plan is %.0f minutes old, past "
                      "the %d minute limit.[/]" % (args.symbol.upper(), age_min, args.max_age))
        console.print("[dim]Re-run the analysis, or pass --force to take it anyway.[/]")
        return 1

    direction = 1 if run["action"] in ("BUY", "WAIT") else -1
    if run["action"] == "WAIT" and not args.force:
        console.print("[yellow]That plan said WAIT for %s, meaning price had not "
                      "reached the entry yet.[/]" % format_price(run["entry"]))
        console.print("[dim]Pass --force if the level has since been reached.[/]")
        return 1

    qty = args.qty
    risk_amount = None
    if qty is None:
        cfg = config_mod.load()
        account = args.account_size or cfg["account_size"]
        risk_amount = account * cfg["risk_per_trade_pct"] / 100.0
        per_unit = abs(run["entry"] - run["stop"])
        qty = (risk_amount / per_unit) if per_unit > 0 else 0
        cap = account * cfg["max_position_pct"] / 100.0
        if qty * run["entry"] > cap:
            qty = cap / run["entry"]
        qty = round(qty, 6) if run["asset_class"] == "crypto" else float(int(qty))
        if qty <= 0:
            console.print("[red]Account too small for one unit at this stop distance.[/]")
            return 2
        risk_amount = qty * per_unit

    try:
        trade_id = db.open_trade(
            conn, symbol=run["symbol"], interval=run["interval"], direction=direction,
            entry=float(run["entry"]), stop=float(run["stop"]),
            target1=run["target1"], target2=run["target2"], quantity=qty,
            risk_amount=risk_amount, run_id=int(run["id"]), account=args.account,
            strategy_note="from run #%d (%s)" % (run["id"], run["action"]))
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2

    console.print(Panel(
        "[bold]#%d  %s %s[/]   from analysis #%d, %s old\n"
        "entry %s   stop %s   target %s\n%s units   ·   risking %s" % (
            trade_id, _side(direction), run["symbol"], run["id"], _age(run["ts"]),
            format_price(run["entry"]), format_price(run["stop"]),
            format_price(run["target1"]) if run["target1"] else "-",
            qty, format_price(risk_amount or 0)),
        title="Opened from plan", border_style="green"))
    return 0


def cmd_close(args, conn) -> int:
    try:
        result = db.close_trade(conn, args.trade_id, args.price, args.reason,
                                args.notes or "")
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2
    colour = "green" if result["r_multiple"] > 0 else "red"
    console.print(Panel(
        "[bold]#%d %s[/] closed at %s\n[%s]%+.2fR[/]   ·   P&L %s%.2f" % (
            result["id"], result["symbol"], format_price(result["exit"]),
            colour, result["r_multiple"],
            "+" if (result["pnl"] or 0) >= 0 else "", result["pnl"] or 0.0),
        title="Closed", border_style=colour))
    return 0


def cmd_delete(args, conn) -> int:
    """Remove a trade outright. A mistyped one skews every later statistic."""
    try:
        gone = db.delete_trade(conn, args.trade_id)
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2
    console.print(Panel(
        "Removed #%d, the %s %s at %s. It no longer counts toward anything." % (
            gone["id"], _side(gone["direction"]), gone["symbol"],
            format_price(gone["entry"])),
        title="Deleted", border_style="yellow"))
    return 0


def cmd_check(args, conn) -> int:
    """Mark open positions to market and flag stop or target hits.

    Uses the bar high and low since entry, not just the last price. A stop that
    was touched intraday and recovered is still a stop that would have filled,
    and only checking the current price would hide that.
    """
    open_trades = db.list_trades(conn, status="open", account=args.account)
    if not open_trades:
        console.print("[dim]No open positions.[/]")
        return 0

    table = Table(box=None, padding=(0, 1))
    for col, width, just in (("#", 3, "right"), ("symbol", 8, "left"),
                             ("side", 5, "left"), ("entry", 10, "right"),
                             ("now", 10, "right"), ("R", 6, "right"),
                             ("age", 4, "right"), ("status", 11, "left")):
        table.add_column(col, width=width, justify=just, no_wrap=True)

    # Fetch every instrument at once. One at a time, twenty positions is five
    # seconds of staring at nothing.
    from concurrent.futures import ThreadPoolExecutor

    def grab(trade):
        try:
            return trade["id"], fetch_bars(trade["symbol"], trade["interval"] or "5m")
        except Exception:
            return trade["id"], None

    with ThreadPoolExecutor(max_workers=min(8, len(open_trades))) as pool:
        fetched = dict(pool.map(grab, open_trades))

    alerts = []
    for t in open_trades:
        bars = fetched.get(t["id"])
        if bars is None:
            table.add_row(str(t["id"]), t["symbol"], _side(t["direction"]),
                          format_price(t["entry"]), "-", "-", _age(t["ts_opened"]),
                          "[yellow]no data[/]")
            continue

        price = bars.last_price
        direction = int(t["direction"])
        entry, stop = float(t["entry"]), float(t["stop"])
        risk = abs(entry - stop)
        r_now = ((price - entry) * direction / risk) if risk > 0 else 0.0

        # Scan bars since the trade opened for a stop or target touch.
        since = bars.timestamp >= int(t["ts_opened"])
        status = "open"
        target = t["target1"]
        if since.any():
            highs, lows = bars.high[since], bars.low[since]
            if direction > 0:
                if float(lows.min()) <= stop:
                    status = "[red]STOP HIT[/]"
                elif target and float(highs.max()) >= float(target):
                    status = "[green]TARGET HIT[/]"
            else:
                if float(highs.max()) >= stop:
                    status = "[red]STOP HIT[/]"
                elif target and float(lows.min()) <= float(target):
                    status = "[green]TARGET HIT[/]"

        # Fall back to the live price. No bars print while a market is closed,
        # and a gap can carry price through the stop before any bar covers it,
        # so the bar scan alone would report a blown stop as still protected.
        if status == "open":
            beyond_stop = (price <= stop) if direction > 0 else (price >= stop)
            hit_target = target and (
                (price >= float(target)) if direction > 0 else (price <= float(target)))
            if beyond_stop:
                status = "[red]PAST STOP[/]"
            elif hit_target:
                status = "[green]PAST TARGET[/]"

        if "HIT" in status or "PAST" in status:
            alerts.append((t["id"], t["symbol"], status))

        colour = "green" if r_now > 0 else "red"
        table.add_row(str(t["id"]), t["symbol"], _side(direction),
                      format_price(entry), format_price(price),
                      "[%s]%+.2f[/]" % (colour, r_now), _age(t["ts_opened"]), status)

    console.print(Panel(table, title="Open positions marked to market",
                        border_style="cyan"))
    if alerts:
        lines = ["#%d %s is at or beyond its %s" % (
            i, s, "stop" if "STOP" in st else "target") for i, s, st in alerts]
        console.print(Panel("\n".join(lines) +
                            "\n\n[dim]Close them with: python paper.py close <id> "
                            "--price <fill> --reason stop|target[/]",
                            title="Action needed", border_style="yellow"))
    return 0


def cmd_list(args, conn) -> int:
    status = None if args.all else "open"
    trades = db.list_trades(conn, status=status, account=args.account,
                            symbol=args.symbol, limit=args.limit)
    if not trades:
        console.print("[dim]No trades recorded.[/]")
        return 0

    table = Table(box=None, padding=(0, 1))
    for col, width, just in (("#", 3, "right"), ("symbol", 8, "left"),
                             ("side", 5, "left"), ("entry", 9, "right"),
                             ("stop", 9, "right"), ("exit", 9, "right"),
                             ("R", 6, "right"), ("status", 6, "left"),
                             ("age", 4, "right")):
        table.add_column(col, width=width, justify=just, no_wrap=True)
    for t in trades:
        r = t["r_multiple"]
        r_txt = "-" if r is None else "[%s]%+.2f[/]" % ("green" if r > 0 else "red", r)
        table.add_row(str(t["id"]), t["symbol"], _side(t["direction"]),
                      format_price(t["entry"]), format_price(t["stop"]),
                      format_price(t["exit_price"]) if t["exit_price"] else "-",
                      r_txt, t["status"], _age(t["ts_opened"]))
    console.print(Panel(table, title="Trades", border_style="cyan"))
    return 0


def cmd_stats(args, conn) -> int:
    s = db.journal_summary(conn, account=args.account)
    if s["trades"] == 0:
        console.print(Panel("No closed trades yet. %d open.\n\n"
                            "[dim]Record trades as you take them, then run this "
                            "again once you have twenty or more closed. Fewer "
                            "than that cannot distinguish skill from luck.[/]"
                            % s.get("open", 0),
                            title="Journal", border_style="yellow"))
        return 0

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", width=18)
    t.add_column()
    t.add_row("Closed trades", "%d   ·   %d open" % (s["trades"], s["open"]))
    t.add_row("Hit rate", "%.0f%%   (%d wins, %d losses)" % (
        s["hit_rate"] * 100, s["wins"], s["losses"]))
    colour = "green" if s["expectancy_r"] > 0 else "red"
    t.add_row("Expectancy", "[%s]%+.3fR[/] per trade" % (colour, s["expectancy_r"]))
    t.add_row("Total", "[%s]%+.2fR[/]   ·   P&L %.2f" % (
        colour, s["total_r"], s["pnl"]))
    t.add_row("Average win", "%+.2fR" % s["avg_win_r"])
    t.add_row("Average loss", "%+.2fR" % s["avg_loss_r"])
    t.add_row("Profit factor", "%.2f" % s["profit_factor"] if s["profit_factor"] else "-")
    t.add_row("Max drawdown", "%.2fR" % s["max_drawdown_r"])

    note = ""
    if s["trades"] < 20:
        note = ("\n[yellow]%d trades is far too few to judge a process. "
                "Treat everything above as provisional until you pass about "
                "thirty.[/]" % s["trades"])
    console.print(Panel(t, title="Paper trading performance", border_style="blue"))
    if note:
        console.print(note)
    return 0


def cmd_leaderboard(args, conn) -> int:
    rows = db.strategy_leaderboard(conn, symbol=args.symbol, regime=args.regime,
                                   minimum=args.minimum, limit=args.limit,
                                   rank_by=args.rank_by)
    if not rows:
        console.print("[yellow]No strategy statistics recorded yet.[/]")
        console.print("[dim]They are written each time you run an analysis.[/]")
        return 0

    table = Table(box=None, padding=(0, 1))
    for col, width, just, style in (("strategy", 20, "left", "dim"),
                                    ("family", 14, "left", "dim"),
                                    ("n", 6, "right", None),
                                    ("hit", 4, "right", None),
                                    ("gross R", 7, "right", None),
                                    ("net R", 7, "right", None),
                                    ("sym", 3, "right", "dim")):
        table.add_column(col, width=width, justify=just, style=style, no_wrap=True)
    for r in rows:
        g, n = r["gross_exp"], r["net_exp"]
        table.add_row(r["strategy"], r["family"], str(r["n"]),
                      "%.0f%%" % (r["hit_rate"] * 100),
                      "[%s]%+.3f[/]" % ("green" if g > 0 else "red", g),
                      "[%s]%+.3f[/]" % ("green" if n > 0 else "red", n),
                      str(r["symbols"]))
    scope = args.symbol.upper() if args.symbol else "all instruments"
    console.print(Panel(table, title="Strategy leaderboard: %s, %s regime" % (
        scope, args.regime), border_style="blue"))
    console.print("[dim]Gross expectancy is skill before costs. Net is what you "
                  "would actually have kept. A large gap means the strategy works "
                  "but the instrument is too expensive to trade this way.[/]")
    return 0


def cmd_runs(args, conn) -> int:
    rows = db.recent_runs(conn, symbol=args.symbol, limit=args.limit)
    if not rows:
        console.print("[dim]No analyses recorded yet.[/]")
        return 0
    # Explicit widths: left to itself rich squeezes the headers into ellipses.
    table = Table(box=None, padding=(0, 1))
    for col, width, just in (("#", 4, "right"), ("when", 5, "right"),
                             ("symbol", 9, "left"), ("iv", 4, "left"),
                             ("regime", 13, "left"), ("comp", 6, "right"),
                             ("action", 6, "left"), ("entry", 10, "right"),
                             ("exp R", 7, "right")):
        table.add_column(col, width=width, justify=just, no_wrap=True)
    for r in rows:
        table.add_row(str(r["id"]), _age(r["ts"]), r["symbol"], r["interval"],
                      r["regime_trend"] or "-", "%+.2f" % (r["composite"] or 0),
                      r["action"] or "-",
                      format_price(r["entry"]) if r["entry"] else "-",
                      "%+.3f" % r["expectancy_r"] if r["expectancy_r"] is not None else "-")
    console.print(Panel(table, title="Recent analyses", border_style="cyan"))
    return 0


def cmd_db(args, conn) -> int:
    s = db.stats(conn)
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", width=18)
    t.add_column(justify="right")
    for key, label in (("runs", "Analyses"), ("run_signals", "Signals stored"),
                       ("strategy_stats", "Strategy records"), ("trades", "Trades"),
                       ("run_news", "Headlines stored"),
                       ("symbols_seen", "Instruments seen")):
        t.add_row(label, "{:,}".format(s[key]))
    console.print(Panel(t, title="Database: %s" % db.default_path(),
                        border_style="blue"))
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper.py", description="Paper trading journal and strategy records.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Run with --help")[0].split("\n", 2)[2])
    p.add_argument("--account", default="paper",
                   help="journal name, so paper and live stay separate")
    p.add_argument("--db", help="path to the database file")
    sub = p.add_subparsers(dest="command", required=True)

    o = sub.add_parser("open", help="open a position from explicit numbers")
    o.add_argument("symbol")
    o.add_argument("--entry", type=float, required=True)
    o.add_argument("--stop", type=float, required=True)
    o.add_argument("--target", type=float)
    o.add_argument("--target2", type=float)
    o.add_argument("--qty", type=float)
    o.add_argument("--short", action="store_true", help="record a short instead of a long")
    o.add_argument("--interval", default="5m")
    o.add_argument("--note", help="which strategy or reason drove this")
    o.add_argument("--notes")
    o.set_defaults(func=cmd_open)

    t = sub.add_parser("take", help="open a position from the bot's last plan")
    t.add_argument("symbol")
    t.add_argument("--qty", type=float, help="override the computed size")
    t.add_argument("--account-size", type=float, help="override account size for sizing")
    t.add_argument("--max-age", type=int, default=120,
                   help="reject plans older than this many minutes (default 120)")
    t.add_argument("--force", action="store_true",
                   help="take the plan even if stale or marked WAIT")
    t.set_defaults(func=cmd_take)

    c = sub.add_parser("close", help="close an open position")
    c.add_argument("trade_id", type=int)
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--reason", default="manual",
                   choices=["target", "stop", "manual", "timeout", "scratch"])
    c.add_argument("--notes")
    c.set_defaults(func=cmd_close)

    dl = sub.add_parser("delete", help="remove a trade entirely")
    dl.add_argument("trade_id", type=int)
    dl.set_defaults(func=cmd_delete)

    ck = sub.add_parser("check", help="mark open positions to market")
    ck.set_defaults(func=cmd_check)

    ls = sub.add_parser("list", help="list trades")
    ls.add_argument("--all", action="store_true", help="include closed trades")
    ls.add_argument("--symbol")
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("stats", help="realised journal performance")
    st.set_defaults(func=cmd_stats)

    lb = sub.add_parser("leaderboard", help="which strategies measure best")
    lb.add_argument("--symbol")
    lb.add_argument("--regime", default="all",
                    choices=["all", "trending", "ranging", "transitional"])
    lb.add_argument("--minimum", type=int, default=40, help="minimum sample size")
    lb.add_argument("--limit", type=int, default=15)
    lb.add_argument("--rank-by", default="gross", choices=["gross", "net"])
    lb.set_defaults(func=cmd_leaderboard)

    r = sub.add_parser("runs", help="recent analyses")
    r.add_argument("--symbol")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_runs)

    d = sub.add_parser("db", help="database size and location")
    d.set_defaults(func=cmd_db)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        return args.func(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/]")
        sys.exit(130)
