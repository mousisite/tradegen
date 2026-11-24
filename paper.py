#!/usr/bin/env python
"""Paper trading journal.

Record what you actually took, let the bot mark it to market, and measure
whether the process works before any real money is involved.

    paper.bat take AAPL              open a trade from the bot's last plan
    paper.bat open AAPL --entry 315 --stop 313.5 --target 317.5 --qty 60
    paper.bat check                  mark open trades to market
    paper.bat close 3 --price 317.5  close a position
    paper.bat close 3 --price 317.5 --qty 20   close part of it
    paper.bat trail 3 --pct 5        trail its stop 5% behind the best price
    paper.bat list                   show open positions
    paper.bat stats                  realised performance so far
    paper.bat leaderboard            which strategies measure best
    paper.bat runs                   recent analyses

Run with --help, or `paper.bat <command> --help`, for the full options.
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
from bot.decision import size_futures
from bot.market import (DataError, fetch_bars, format_price, normalise_symbol,
                        split_factor_since)
from bot.portfolio import atr_of, read_trade_stop, trade_interval, trail_from, trail_problem

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

def _settings():
    """The settings file, or the reason it cannot be used, said as analyze.py says it."""
    try:
        return config_mod.load()
    except RuntimeError as exc:
        console.print("[red]Config error:[/] %s" % exc)
        raise SystemExit(2)


def _bars_for(trade):
    """A trade's own bars, and its shares today per share the day it opened.

    Raises DataError when either cannot be had just now.
    """
    bars = fetch_bars(trade["symbol"], trade_interval(trade))
    return bars, split_factor_since(trade["symbol"], int(trade["ts_opened"]), bars)


def _trade(conn, trade_id: int):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError("no trade with id %d" % trade_id)
    return row


def cmd_open(args, conn) -> int:
    """Open a position from explicit numbers."""
    direction = -1 if args.short else 1
    interval = args.interval or _settings()["interval"]
    trail = {}
    try:
        if args.trail is not None and args.trail_atr is not None:
            raise ValueError("give the trail as --trail or --trail-atr, not both")
        if args.trail is not None:
            trail = {"trail_pct": args.trail / 100.0}
        elif args.trail_atr is not None:
            try:
                unit = atr_of(fetch_bars(args.symbol.upper(), interval))
            except DataError as exc:
                raise ValueError("could not read %s's prices to measure its ATR: %s"
                                 % (args.symbol.upper(), exc))
            if unit is None:
                raise ValueError("not enough price history to measure %s's ATR; "
                                 "use --trail with a percentage" % args.symbol.upper())
            trail = {"trail_dist": args.trail_atr * unit, "trail_atr": args.trail_atr}
        trade_id = db.open_trade(
            conn, symbol=args.symbol, interval=interval, direction=direction,
            entry=args.entry, stop=args.stop, target1=args.target,
            target2=args.target2, quantity=args.qty,
            risk_amount=(abs(args.entry - args.stop) * args.qty) if args.qty else None,
            account=args.account, strategy_note=args.note or "", notes=args.notes or "",
            **trail)
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2

    risk = abs(args.entry - args.stop)
    trailing = ""
    if trail:
        trailing = "\nthe stop trails %s behind the best price, and only tightens" % (
            "%g%%" % args.trail if args.trail is not None
            else "%g ATR (%s)" % (args.trail_atr, format_price(trail["trail_dist"])))
    levels = (args.entry, args.stop, args.target)
    console.print(Panel(
        "[bold]#%d  %s %s[/]\nentry %s   stop %s   target %s\n"
        "risk %s per unit%s%s" % (
            trade_id, _side(direction), args.symbol.upper(),
            format_price(args.entry, beside=levels), format_price(args.stop, beside=levels),
            format_price(args.target, beside=levels) if args.target else "-",
            format_price(risk),
            "   ·   %s units   ·   %s total risk" % (
                args.qty, format_price(risk * args.qty)) if args.qty else "",
            trailing),
        title="Opened", border_style="green"))
    return 0


def cmd_take(args, conn) -> int:
    """Open a position straight from the bot's most recent plan for a symbol.

    This is the path that keeps the journal honest: the trade recorded is the
    trade the bot actually proposed, not a remembered version of it.
    """
    # The ticker an analysis would use, so "btc" finds the run of BTC-USD; as
    # typed when there is none, which is the fund listed as BTC.
    try:
        sym = normalise_symbol(args.symbol)
    except DataError:
        sym = args.symbol.upper()
    rows = db.recent_runs(conn, symbol=sym, limit=1)
    if not rows and sym != args.symbol.upper():
        rows = db.recent_runs(conn, symbol=args.symbol, limit=1)
        sym = args.symbol.upper() if rows else sym
    if not rows:
        console.print("[yellow]No analysis recorded for %s yet.[/]" % sym)
        console.print("[dim]Run: run.bat --symbol %s[/]" % sym)
        return 1
    # Only the newest: an older plan is one the bot has since replaced.
    run = latest = rows[0]
    if not (run["action"] in ("BUY", "SHORT", "WAIT") and run["entry"] and run["stop"]):
        # Distinguish "never analysed" from "analysed and told you not to".
        console.print(Panel(
            "The most recent %s analysis said [bold]%s[/], %s ago.\n\n%s\n\n"
            "[dim]The journal only takes plans the bot actually proposed. To "
            "record a trade it advised against, use `paper.bat open` with "
            "explicit numbers.[/]" % (
                sym, latest["action"], _age(latest["ts"]),
                latest["headline"] or ""),
            title="Nothing to take", border_style="yellow"))
        return 1

    age_min = (int(time.time()) - int(run["ts"])) / 60.0
    if age_min > args.max_age and not args.force:
        console.print("[yellow]The most recent %s plan is %.0f minutes old, past "
                      "the %d minute limit.[/]" % (sym, age_min, args.max_age))
        console.print("[dim]Re-run the analysis, or pass --force to take it anyway.[/]")
        return 1

    # Read off the plan itself, as the website does: a WAIT may be either side.
    direction = 1 if run["stop"] < run["entry"] else -1
    if run["action"] == "WAIT" and not args.force:
        console.print("[yellow]That plan said WAIT for %s, meaning price had not "
                      "reached the entry yet.[/]" % format_price(run["entry"]))
        console.print("[dim]Pass --force if the level has since been reached.[/]")
        return 1

    held = conn.execute("SELECT id FROM trades WHERE run_id=? AND account=? AND "
                        "status='open' ORDER BY id LIMIT 1",
                        (run["id"], args.account)).fetchone()
    if held is not None and not args.force:
        console.print("[yellow]That plan is already in the journal, as #%d.[/]" % held["id"])
        console.print("[dim]Pass --force to open it a second time.[/]")
        return 1

    per_unit = abs(run["entry"] - run["stop"])
    qty = args.qty
    if qty is None:
        cfg = _settings()
        account = args.account_size if args.account_size is not None else cfg["account_size"]
        if not 0 < account < float("inf"):
            console.print("[red]Rejected:[/] the account size must be more than zero")
            return 2
        if run["symbol"].upper().endswith("=F"):
            # The whole contracts the analysis sized, written down as their
            # dollars a point, as the website logs the same plan.
            sized = size_futures(run["symbol"], float(run["entry"]), float(run["stop"]),
                                 dict(cfg, account_size=account))
            if "error" in sized:
                console.print("[red]Rejected:[/] %s" % sized["error"])
                return 2
            qty = sized["quantity"] * sized["multiplier"]
        else:
            risk_amount = account * cfg["risk_per_trade_pct"] / 100.0
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
            # A target at or below zero is no price the market can reach.
            target1=run["target1"] if (run["target1"] or 0) > 0 else None,
            target2=run["target2"] if (run["target2"] or 0) > 0 else None, quantity=qty,
            risk_amount=risk_amount, run_id=int(run["id"]), account=args.account,
            strategy_note="from run #%d (%s)" % (run["id"], run["action"]))
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2

    levels = (run["entry"], run["stop"], run["target1"])
    console.print(Panel(
        "[bold]#%d  %s %s[/]   from analysis #%d, %s old\n"
        "entry %s   stop %s   target %s\n%s units   ·   risking %s" % (
            trade_id, _side(direction), run["symbol"], run["id"], _age(run["ts"]),
            format_price(run["entry"], beside=levels), format_price(run["stop"], beside=levels),
            format_price(run["target1"], beside=levels) if run["target1"] else "-",
            qty, format_price(risk_amount)),
        title="Opened from plan", border_style="green"))
    return 0


def cmd_close(args, conn) -> int:
    """Close a position, or part of it with --qty.

    The price and size are in the shares the trade was opened with, as
    `check` shows it, unless --today says they are in today's: after a
    ten-for-one split, 10 shares at 101 today are 1 at 1,010 then.
    """
    price, qty, converted = args.price, args.qty, ""
    try:
        if args.today:
            try:
                _bars, split = _bars_for(_trade(conn, args.trade_id))
            except DataError as exc:
                raise ValueError("could not check for a share split just now, so a "
                                 "price in today's shares cannot be converted yet (%s). "
                                 "Try again, or give it in the shares you opened with."
                                 % exc)
            if split != 1.0:
                from bot.market import split_words
                price = args.price * split
                qty = args.qty / split if args.qty is not None else None
                converted = ("\n%s in today's shares is %s in the shares you opened "
                             "with, after a %s split" % (format_price(args.price),
                                                         format_price(price),
                                                         split_words(split)))
        else:
            # Kept with the exit for the exports, which give a sale in the
            # shares a broker lists. The exit does not need it, so a price
            # source that does not answer leaves it unknown.
            try:
                split = _bars_for(_trade(conn, args.trade_id))[1]
            except DataError:
                split = None
        result = db.exit_trade(conn, args.trade_id, price, qty, args.reason,
                               args.notes or "", split=split)
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2
    colour = "green" if result["r_multiple"] > 0 else "red"
    if result["closed"] and result["parts"]:
        title, head = "Closed", ("[bold]#%d %s[/] closed in %d parts at %s on average, "
                                 "the last at %s" % (result["id"], result["symbol"],
                                                     result["parts"],
                                                     format_price(result["average"]),
                                                     format_price(result["exit"])))
    elif result["closed"]:
        title, head = "Closed", "[bold]#%d %s[/] closed at %s" % (
            result["id"], result["symbol"], format_price(result["exit"]))
    else:
        title, head = "Closed in part", ("[bold]#%d %s[/] %s closed at %s, %s still open"
                                         % (result["id"], result["symbol"],
                                            db._qty(result["quantity"]),
                                            format_price(result["exit"]),
                                            db._qty(result["left"])))
    console.print(Panel(
        "%s\n[%s]%+.2fR[/]   ·   P&L %s%.2f%s%s" % (
            head, colour, result["r_multiple"],
            "+" if (result["pnl"] or 0) >= 0 else "", result["pnl"] or 0.0,
            "" if result["closed"] else "   [dim](R of this part)[/]", converted),
        title=title, border_style=colour))
    return 0


def cmd_trail(args, conn) -> int:
    """Trail an open trade's stop behind the best price since it opened.

    The stop only ever tightens: a trail that would loosen it, or put it past
    today's price, is refused, and so is one on a position already past its
    stop.
    """
    try:
        if (args.pct is None) == (args.atr is None):
            raise ValueError("give the trail as --pct or --atr, one of them")
        db.check_trail(args.pct / 100.0 if args.pct is not None else None, None, args.atr)
        row = _trade(conn, args.trade_id)
        if row["status"] != "open":
            raise ValueError("trade %d is already %s" % (args.trade_id, row["status"]))
        try:
            bars, split = _bars_for(row)
        except DataError as exc:
            raise ValueError("could not read %s's prices just now: %s" % (row["symbol"], exc))
        trail, before, after = trail_from(row, bars, split,
                                          pct=args.pct / 100.0 if args.pct is not None else None,
                                          atr=args.atr)
        problem = trail_problem(row["symbol"], int(row["direction"]), before, after)
        if problem:
            raise ValueError(problem)
        db.set_trail(conn, args.trade_id, pct=trail.pct, distance=trail.distance,
                     atr=trail.atr, best=after.best, best_at=after.best_at)
    except ValueError as exc:
        console.print("[red]Rejected:[/] %s" % exc)
        return 2
    console.print(Panel(
        "[bold]#%d %s[/] trails %s behind its best price since opening, %s.\n"
        "Its stop is %s now, from %s when it opened. It only ever tightens." % (
            args.trade_id, row["symbol"], trail.words(), format_price(after.best),
            format_price(after.stop), format_price(after.initial)),
        title="Trailing stop", border_style="green"))
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
    open_trades = db.list_trades(conn, status="open", account=args.account, limit=None)
    if not open_trades:
        console.print("[dim]No open positions.[/]")
        return 0

    table = Table(box=None, padding=(0, 1))
    for col, width, just in (("#", 3, "right"), ("symbol", 8, "left"),
                             ("side", 5, "left"), ("entry", 10, "right"),
                             ("stop", 10, "right"), ("now", 10, "right"),
                             ("R", 6, "right"), ("age", 4, "right"),
                             ("status", 13, "left")):
        table.add_column(col, width=width, justify=just, no_wrap=True)

    # Fetch every instrument at once. One at a time, twenty positions is five
    # seconds of staring at nothing.
    from concurrent.futures import ThreadPoolExecutor

    def grab(trade):
        try:
            return trade["id"], fetch_bars(trade["symbol"], trade_interval(trade))
        except Exception:
            return trade["id"], None

    with ThreadPoolExecutor(max_workers=min(8, len(open_trades))) as pool:
        fetched = dict(pool.map(grab, open_trades))

    alerts = []
    notes = []
    for t in open_trades:
        bars = fetched.get(t["id"])
        if bars is None:
            table.add_row(str(t["id"]), t["symbol"], _side(t["direction"]),
                          format_price(t["entry"], beside=(t["stop"],)),
                          format_price(t["stop"], beside=(t["entry"],)), "-", "-",
                          _age(t["ts_opened"]), "[yellow]no data[/]")
            continue

        # In the shares the trade was opened with, as its entry and stop
        # are: after a split the quote and the bars are in today's.
        opened = int(t["ts_opened"])
        try:
            split = split_factor_since(t["symbol"], opened, bars)
        except DataError:
            # A split cannot be ruled out just now.
            table.add_row(str(t["id"]), t["symbol"], _side(t["direction"]),
                          format_price(t["entry"], beside=(t["stop"],)),
                          format_price(t["stop"], beside=(t["entry"],)), "-", "-",
                          _age(t["ts_opened"]), "[yellow]no split data[/]")
            continue
        if split != 1.0:
            from bot.market import split_words
            notes.append("#%d %s split %s since it was opened: its prices here are "
                         "in the shares it was opened with (quoted %s today)."
                         % (t["id"], t["symbol"], split_words(split),
                            format_price(bars.last_price)))
        direction = int(t["direction"])
        entry, stop = float(t["entry"]), float(t["stop"])
        risk = abs(entry - stop)

        # The stop by the rule the journal pages and the background notice
        # use (portfolio.read_stop): only bars that started after the trade
        # did, because part of the bar it opened in may have traded before
        # the entry, and a low from before you bought is not your stop being
        # hit; a trail, if it has one, behind the best price since opening;
        # and the live price, which catches a gap through the stop before any
        # bar covers it, and a market that is closed.
        read = read_trade_stop(t, bars, split)
        price = read.price
        r_now = ((price - entry) * direction / risk) if risk > 0 else 0.0
        crossed = read.crossing(time.time())
        if crossed is not None:
            db.stop_crossed(conn, int(t["id"]), *crossed)
        if read.trail is not None:
            db.trail_seen(conn, int(t["id"]), read.best, read.best_at)
            where = "where it started"
            if read.moved:
                where = "%s from %s" % ("up" if direction > 0 else "down",
                                        format_price(stop))
            notes.append("#%d %s: the stop trails %s behind the best price since "
                         "opening, %s, and is at %s, %s." % (
                             t["id"], t["symbol"], read.trail.words(),
                             format_price(read.best), format_price(read.stop), where))
        after = bars.timestamp > opened
        status = "open"
        target = t["target1"]
        if read.touched_at is not None:
            status = "[red]STOP HIT[/]"
        elif target and after.any():
            if (float(bars.high[after].max()) * split >= float(target) if direction > 0
                    else float(bars.low[after].min()) * split <= float(target)):
                status = "[green]TARGET HIT[/]"

        if status == "open":
            hit_target = target and (
                (price >= float(target)) if direction > 0 else (price <= float(target)))
            if read.past:
                status = "[red]PAST STOP[/]"
            elif hit_target:
                status = "[green]PAST TARGET[/]"

        if "HIT" in status or "PAST" in status:
            alerts.append((t["id"], t["symbol"], status))

        colour = "green" if r_now > 0 else "red"
        table.add_row(str(t["id"]), t["symbol"], _side(direction),
                      format_price(entry, beside=(read.stop,)),
                      format_price(read.stop, beside=(entry,)), format_price(price),
                      "[%s]%+.2f[/]" % (colour, r_now), _age(t["ts_opened"]), status)

    console.print(Panel(table, title="Open positions marked to market",
                        border_style="cyan"))
    for note in notes:
        console.print("[dim]%s[/]" % note)
    if alerts:
        lines = ["#%d %s is at or beyond its %s" % (
            i, s, "stop" if "STOP" in st else "target") for i, s, st in alerts]
        console.print(Panel("\n".join(lines) +
                            "\n\n[dim]Close them with: paper.bat close <id> "
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
                             ("stop", 9, "right"), ("size", 13, "right"),
                             ("exit", 9, "right"), ("R", 6, "right"),
                             ("status", 6, "left"), ("age", 4, "right")):
        table.add_column(col, width=width, justify=just, no_wrap=True)
    exits = db.exits_for(conn, [t["id"] for t in trades])
    for t in trades:
        parts = db.trade_parts(t, exits.get(t["id"], []))
        # A position closed in part shows what is left of it, and the R and
        # exit price of what is closed so far.
        size = "-" if parts["size"] is None else db._qty(parts["size"])
        if parts["parts"] and t["status"] == "open":
            size = "%s of %s" % (db._qty(parts["left"]), size)
        r = parts["r"]
        r_txt = "-" if r is None else "[%s]%+.2f[/]" % ("green" if r > 0 else "red", r)
        table.add_row(str(t["id"]), t["symbol"], _side(t["direction"]),
                      format_price(t["entry"], beside=(t["stop"], parts["avg_exit"])),
                      format_price(t["stop"], beside=(t["entry"], parts["avg_exit"])), size,
                      format_price(parts["avg_exit"], beside=(t["entry"], t["stop"]))
                      if parts["avg_exit"] else "-",
                      r_txt, t["status"], _age(t["ts_opened"]))
    console.print(Panel(table, title="Trades", border_style="cyan"))
    if any(exits.get(t["id"]) and t["status"] == "open" for t in trades):
        console.print("[dim]For a position closed in part, the exit and R are of what "
                      "is closed so far, the R counted against the whole position.[/]")
    return 0


def cmd_stats(args, conn) -> int:
    # After costs, as the website counts the same trades.
    cfg = _settings()
    s = db.journal_summary(conn, account=args.account, costs={
        kind: float(cfg["cost_bps_" + kind]) / 10000.0
        for kind in ("equity", "crypto", "futures", "other")})
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
    t.add_row("Expectancy", "[%s]%+.3fR[/] per trade, after costs" % (
        colour, s["expectancy_r"]))
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
    console.print("[dim]Every figure here is after trading costs. The R of each trade "
                  "in `paper.bat list --all` is before them.[/]")
    if s.get("open_in_part"):
        console.print("[dim]Not counted above: %.2f taken by closing part of %d "
                      "position%s still open. A trade counts once it is closed in "
                      "full.[/]" % (s["open_realised"], s["open_in_part"],
                                    "" if s["open_in_part"] == 1 else "s"))
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
    for key, label in (("runs", "Analyses"),
                       ("strategy_stats", "Strategy records"), ("trades", "Trades"),
                       ("symbols_seen", "Instruments seen")):
        t.add_row(label, "{:,}".format(s[key]))
    console.print(Panel(t, title="Database: %s" % (
        os.path.abspath(args.db) if args.db else db.default_path()),
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
    o.add_argument("--interval", help="bar size it is marked on (default: the one in "
                                      "your settings)")
    o.add_argument("--note", help="which strategy or reason drove this")
    o.add_argument("--notes")
    o.add_argument("--trail", type=float, metavar="PCT",
                   help="trail the stop this many percent behind the best price")
    o.add_argument("--trail-atr", type=float, metavar="N",
                   help="trail the stop N times the ATR behind the best price")
    o.set_defaults(func=cmd_open)

    t = sub.add_parser("take", help="open a position from the bot's last plan")
    t.add_argument("symbol")
    t.add_argument("--qty", type=float, help="override the computed size")
    t.add_argument("--account-size", type=float, help="override account size for sizing")
    t.add_argument("--max-age", type=int, default=120,
                   help="reject plans older than this many minutes (default 120)")
    t.add_argument("--force", action="store_true",
                   help="take the plan even if stale, marked WAIT or already taken")
    t.set_defaults(func=cmd_take)

    c = sub.add_parser("close", help="close an open position, or part of it")
    c.add_argument("trade_id", type=int)
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--qty", type=float,
                   help="close only this much of it; the rest stays open")
    c.add_argument("--today", action="store_true",
                   help="the price and size are in today's shares, after a split")
    c.add_argument("--reason", default="manual",
                   choices=["target", "stop", "manual", "timeout", "scratch"])
    c.add_argument("--notes")
    c.set_defaults(func=cmd_close)

    tr = sub.add_parser("trail", help="trail an open position's stop")
    tr.add_argument("trade_id", type=int)
    tr.add_argument("--pct", type=float, help="this many percent behind the best price")
    tr.add_argument("--atr", type=float, help="this many ATR behind the best price")
    tr.set_defaults(func=cmd_trail)

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
