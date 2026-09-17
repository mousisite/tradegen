#!/usr/bin/env python
"""Interactive menu, for driving the bot without remembering commands.

This exists because the batch equivalent could not be tested reliably: cmd's
`set /p` behaves differently depending on whether stdin is a console or a pipe,
which makes an interactive .bat file effectively unverifiable. Python's input()
does not have that problem, so this can be exercised end to end.

Every option here just calls the same entry points as the command line, so the
menu and the CLI can never drift apart.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must run before bot.report is imported: that module chooses its glyph set
# from the stream encoding, and legacy Windows code pages cannot encode it.
from bot.terminal import prepare_output
prepare_output()

from rich.console import Console
from rich.panel import Panel

import analyze
import paper

console = Console()

MENU = [
    ("ANALYSE", None),
    ("1", "Analyse a ticker"),
    ("2", "Analyse the chart on my clipboard"),
    ("3", "Analyse a screenshot file"),
    ("PAPER TRADING", None),
    ("4", "Check my open trades"),
    ("5", "My performance so far"),
    ("6", "List all my trades"),
    ("7", "Take a trade from the last analysis"),
    ("8", "Add a trade by hand"),
    ("9", "Close a trade"),
    ("10", "Delete a trade"),
    ("RESEARCH", None),
    ("11", "Strategy leaderboard"),
    ("12", "Recent analyses"),
    ("13", "Where is my data"),
    ("", None),
    ("0", "Exit"),
]

TIMEFRAMES = {"1": ("5m", "5 minute, day trading"),
              "2": ("15m", "15 minute"),
              "3": ("1h", "1 hour"),
              "4": ("1d", "daily, best measured odds")}


def ask(prompt: str, default: str = "") -> str:
    """Read a line, returning the default on empty input or end of stream."""
    try:
        value = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def draw_menu() -> None:
    lines = []
    for key, label in MENU:
        if label is None:
            lines.append("" if not key else "[dim]%s[/]" % key)
        else:
            lines.append("   [bold cyan]%2s[/]  %s" % (key, label))
    console.print(Panel("\n".join(lines), title="Orenth",
                        border_style="cyan", padding=(1, 2)))


def choose_timeframe() -> str:
    console.print("\n[bold]Timeframe[/]")
    for key, (code, label) in TIMEFRAMES.items():
        console.print("   [cyan]%s[/]  %-4s  %s" % (key, code, label))
    choice = ask("\n   Choose [4]: ", "4")
    return TIMEFRAMES.get(choice, TIMEFRAMES["4"])[0]


def run(argv, module=analyze) -> None:
    """Call an entry point, keeping the menu alive if it raises."""
    try:
        module.main(argv)
    except SystemExit:
        pass
    except Exception as exc:                       # never drop the user out
        console.print("[red]That did not work:[/] %s" % exc)


def do_ticker() -> None:
    symbol = ask("\n   Ticker (AAPL, NVDA, BTC-USD): ")
    if not symbol:
        return
    interval = choose_timeframe()
    console.print()
    run(["--symbol", symbol, "--interval", interval])


def do_clipboard() -> None:
    console.print("\n[dim]Copy a chart first with Win+Shift+S, then continue.[/]")
    ask("   Press Enter when the chart is copied: ")
    run(["--clipboard"])


def do_image() -> None:
    path = ask("\n   Drag the image here and press Enter: ").strip('"')
    if not path:
        return
    if not os.path.exists(path):
        console.print("[red]No file at that path.[/]")
        return
    run([path])


def do_take() -> None:
    symbol = ask("\n   Ticker you want to take a trade on: ")
    if not symbol:
        return
    run(["take", symbol], paper)


def do_close() -> None:
    run(["list"], paper)
    trade_id = ask("\n   Trade number to close: ")
    if not trade_id.isdigit():
        console.print("[yellow]That is not a trade number.[/]")
        return
    price = ask("   Price you exited at: ")
    try:
        float(price)
    except ValueError:
        console.print("[yellow]That is not a price.[/]")
        return
    console.print("\n   [cyan]1[/] hit target    [cyan]2[/] hit stop    "
                  "[cyan]3[/] closed manually")
    reason = {"1": "target", "2": "stop"}.get(ask("   Choose [3]: ", "3"), "manual")
    run(["close", trade_id, "--price", price, "--reason", reason], paper)


def do_manual() -> None:
    """Log a trade the bot did not propose."""
    symbol = ask("\n   Ticker: ")
    if not symbol:
        return
    side = ask("   Long or short [long]: ", "long").lower()
    entry = ask("   Entry price: ")
    stop = ask("   Stop price: ")
    try:
        float(entry), float(stop)
    except ValueError:
        console.print("[yellow]Entry and stop both need to be numbers.[/]")
        return
    target = ask("   Target (Enter to skip): ")
    qty = ask("   Size in units (Enter to skip): ")

    argv = ["open", symbol, "--entry", entry, "--stop", stop]
    if side.startswith("s"):
        argv.append("--short")
    if target:
        argv += ["--target", target]
    if qty:
        argv += ["--qty", qty]
    run(argv, paper)


def do_delete() -> None:
    run(["list", "--all"], paper)
    trade_id = ask("\n   Trade number to delete: ")
    if not trade_id.isdigit():
        console.print("[yellow]That is not a trade number.[/]")
        return
    if ask("   Type yes to confirm: ").lower() not in ("yes", "y"):
        console.print("[dim]Left alone.[/]")
        return
    run(["delete", trade_id], paper)


ACTIONS = {
    "1": do_ticker,
    "2": do_clipboard,
    "3": do_image,
    "4": lambda: run(["check"], paper),
    "5": lambda: run(["stats"], paper),
    "6": lambda: run(["list", "--all"], paper),
    "7": do_take,
    "8": do_manual,
    "9": do_close,
    "10": do_delete,
    "11": lambda: run(["leaderboard", "--minimum", "100", "--limit", "12"], paper),
    "12": lambda: run(["runs", "--limit", "15"], paper),
    "13": lambda: run(["db"], paper),
}


def main() -> int:
    console.print("\n[dim]Analysis software, not financial advice. "
                  "Day trading loses money for most people who try it.[/]")
    while True:
        draw_menu()
        choice = ask("   Choose a number: ")
        if choice == "0":
            console.print("[dim]Bye.[/]")
            return 0
        action = ACTIONS.get(choice)
        if action is None:
            if choice:
                console.print("[yellow]%r is not on the menu.[/]" % choice)
            # An empty line at end of input means the stream is done.
            elif not sys.stdin.isatty():
                return 0
            continue
        action()
        if sys.stdin.isatty():
            ask("\n   Press Enter for the menu: ")
        console.print()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/]")
        sys.exit(0)
