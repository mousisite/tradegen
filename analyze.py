#!/usr/bin/env python
"""Screenshot-driven day-trading analysis.

    python analyze.py chart.png              read the ticker off an image
    python analyze.py --clipboard            use whatever you just snipped
    python analyze.py --symbol BTC-USD       skip the image entirely

Run with --help for the full option list.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Make the package importable when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must run before bot.report is imported: that module chooses its glyph set
# from the stream encoding, and legacy Windows code pages cannot encode it.
from bot.terminal import prepare_output
prepare_output()

# Load .env before anything reads ANTHROPIC_API_KEY.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from rich.console import Console

from bot import config as config_mod
from bot import engine
from bot import report as report_mod
from bot.market import DataError, resolve
from bot.vision import VisionError, grab_clipboard, read_chart

console = Console()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="analyze.py",
        description="Analyse a stock or crypto chart screenshot and produce a "
                    "day-trade plan with measured odds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python analyze.py chart.png\n"
               "  python analyze.py --clipboard --account 5000 --risk 0.5\n"
               "  python analyze.py --symbol NVDA --interval 15m\n"
               "  python analyze.py --symbol ETH-USD --json out.json\n")
    p.add_argument("image", nargs="?", help="path to a chart screenshot")
    p.add_argument("--clipboard", action="store_true",
                   help="read the screenshot from the clipboard")
    p.add_argument("--symbol", "-s",
                   help="analyse this ticker directly, no image needed")
    p.add_argument("--interval", "-i", help="bar interval: 1m, 5m, 15m, 1h, 1d")
    p.add_argument("--account", type=float, help="account size for position sizing")
    p.add_argument("--risk", type=float, help="percent of account risked per trade")
    p.add_argument("--allow-shorts", action="store_true",
                   help="permit bearish trade plans as well as long ones")
    p.add_argument("--no-news", action="store_true",
                   help="skip news and social sentiment (faster, technicals only)")
    p.add_argument("--no-llm", action="store_true",
                   help="never call Claude for sentiment scoring")
    p.add_argument("--json", metavar="PATH", help="also write the analysis as JSON")
    p.add_argument("--config", metavar="PATH", help="path to a config.json")
    p.add_argument("--no-learn", action="store_true",
                   help="skip per-strategy measurement and use fixed weights")
    p.add_argument("--no-db", action="store_true",
                   help="do not record this run in the database")
    p.add_argument("--all-signals", action="store_true",
                   help="print every strategy, not just the top contributors")
    p.add_argument("--db", metavar="PATH", help="path to the database file")
    return p.parse_args(argv)


def resolve_symbol(args, cfg):
    """Work out which instrument to analyse, and any note from the screenshot."""
    if args.symbol:
        info = resolve(args.symbol)
        return info["symbol"], info.get("name") or info["symbol"], "", None

    image_path = args.image
    if args.clipboard:
        target = os.path.join(config_mod.project_root(), "_clipboard.png")
        with console.status("[cyan]Reading clipboard..."):
            image_path = grab_clipboard(target)
        console.print("[dim]Clipboard image saved to %s[/]" % target)

    if not image_path:
        raise SystemExit("Nothing to analyse. Pass an image path, --clipboard, "
                         "or --symbol TICKER. See --help.")
    if not os.path.exists(image_path):
        raise SystemExit("No such file: %s" % image_path)

    with console.status("[cyan]Reading the screenshot..."):
        read = read_chart(image_path)

    if not read.symbol:
        raise SystemExit(
            "Could not identify an instrument in that image. Re-run with "
            "--symbol TICKER to analyse it directly.")

    info = resolve(read.symbol)
    bits = ["Read [bold]%s[/] from the image (confidence %.0f%%)." % (
        read.symbol, read.confidence * 100)]
    if info["symbol"].upper() != read.symbol.upper():
        bits.append("Resolved to [bold]%s[/]." % info["symbol"])
    if read.timeframe:
        bits.append("Chart timeframe shown: %s." % read.timeframe)
    if read.price_seen:
        bits.append("Price visible in image: %g." % read.price_seen)
    if read.observations:
        bits.append("\n" + "\n".join("  • %s" % o for o in read.observations))
    return info["symbol"], info.get("name") or info["symbol"], " ".join(bits), read


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except RuntimeError as exc:
        console.print("[red]Config error:[/] %s" % exc)
        return 2

    if args.account is not None:
        cfg["account_size"] = args.account
    if args.risk is not None:
        cfg["risk_per_trade_pct"] = args.risk
    if args.interval:
        cfg["interval"] = args.interval
    if args.allow_shorts:
        cfg["allow_shorts"] = True
    if args.no_llm:
        cfg["use_llm_sentiment"] = False

    started = time.time()
    try:
        symbol, name, chart_note, _read = resolve_symbol(args, cfg)
    except VisionError as exc:
        console.print("[red]Could not read the screenshot:[/] %s" % exc)
        return 2

    # The pipeline itself lives in bot.engine, shared with the web interface,
    # so both front ends can never give different answers to the same question.
    status = console.status("[cyan]Analysing %s..." % symbol)
    status.start()

    def progress(message: str) -> None:
        status.update("[cyan]%s..." % message)

    try:
        result = engine.analyse(symbol, cfg, interval=cfg["interval"],
                                with_news=not args.no_news,
                                with_learning=not args.no_learn,
                                record=not args.no_db, db_path=args.db,
                                progress=progress, source="analyze")
    except DataError as exc:
        status.stop()
        console.print("[red]Market data error:[/] %s" % exc)
        console.print("[dim]Check the ticker, or try a different --interval.[/]")
        return 2
    finally:
        status.stop()

    bars, plan, calib = result.bars, result.plan, result.calibration
    signals, composite, regime = result.signals, result.composite, result.regime
    sentiment = result.sentiment

    report_mod.render(console, bars, plan, signals, composite, regime, calib,
                      sentiment, cfg, chart_note,
                      strategy_report=result.strategy_report,
                      show_all_signals=args.all_signals)
    tail = "[dim]Analysed in %.1fs. Data from Yahoo Finance, Google News and StockTwits." % (
        time.time() - started)
    if result.run_id:
        tail += "  Recorded as run #%d; take it with: python paper.py take %s" % (
            result.run_id, bars.symbol)
    console.print(tail + "[/]\n")

    if args.json:
        payload = report_mod.to_json(bars, plan, signals, composite, regime,
                                     calib, sentiment)
        try:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(payload)
            console.print("[dim]JSON written to %s[/]" % args.json)
        except OSError as exc:
            console.print("[yellow]Could not write JSON: %s[/]" % exc)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/]")
        sys.exit(130)
