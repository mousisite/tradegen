"""Run every test suite and report what passed.

    python tests/run_all.py            all suites
    python tests/run_all.py --offline  only the ones that need no network
    python tests/run_all.py options    only suites whose name contains "options"

Each suite is a normal script that exits non-zero when something fails, so it
can be run on its own too. They are run as separate processes because several
of them build their own database and reset module state.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (file, needs network, what it proves)
SUITES = [
    ("test_templates.py",    False, "every page template compiles and links resolve"),
    ("test_sentiment.py",    False, "the news lexicon scores phrases the right way"),
    ("test_indicators.py",   True,  "indicator maths against known values"),
    ("test_pivots.py",       True,  "pivot levels sit in the right units"),
    ("test_strategies.py",   True,  "the 34 strategies fire sanely and agree on direction"),
    ("test_options.py",      True,  "Black-Scholes, Greeks, implied volatility, parity"),
    ("test_valuation.py",    True,  "discounted cash flow, multiples, risk measures"),
    ("test_fundamentals.py", True,  "Yahoo ratios reconciled against SEC filings"),
    ("test_screener.py",     True,  "filters accept and reject for stated reasons"),
    ("test_modules.py",      True,  "thesis, alerts, reasoning payload and workflows"),
    ("test_thesis.py",       True,  "closing a thesis out, scoring the record, safe redirects"),
    ("test_operations.py",   True,  "earnings in the window, background checking, export, overlap"),
    ("test_accounts.py",     True,  "sign-in, sessions, and one person's data staying theirs"),
    ("test_signin_flow.py",  False, "the whole Google sign-in path, against a stubbed Google"),
    ("test_database.py",     True,  "schema, journal, statistics and invariants"),
    ("test_learning.py",     True,  "strategy weight learning stays bounded"),
    ("test_audit.py",        True,  "self-audit finds no contradictions in a report"),
    ("test_pipeline.py",     True,  "a full analysis end to end on several instruments"),
    ("test_web_forms.py",    True,  "settings, journal and analysis form handling"),
    ("test_web.py",          True,  "research, screener, portfolio, monitor and links"),
]

TIMEOUT = 900


def main() -> int:
    args = [a for a in sys.argv[1:]]
    offline_only = "--offline" in args
    pattern = next((a for a in args if not a.startswith("-")), "")

    chosen = [s for s in SUITES
              if (not offline_only or not s[1])
              and (not pattern or pattern in s[0])]
    if not chosen:
        print("nothing matched %r" % pattern)
        return 1

    width = max(len(s[0]) for s in chosen)
    print("Running %d suite%s from %s\n"
          % (len(chosen), "" if len(chosen) == 1 else "s", HERE))

    results = []
    # On a terminal the pending line is overwritten in place; when the output is
    # piped to a file a carriage return is not honoured, so there it prints once.
    live = sys.stdout.isatty()

    for name, needs_net, why in chosen:
        pending = "  %-*s  %s" % (width, name, why)
        if live:
            sys.stdout.write(pending)
            sys.stdout.flush()
        started = time.time()
        proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                              capture_output=True, text=True, timeout=TIMEOUT,
                              cwd=ROOT)
        took = time.time() - started
        ok = proc.returncode == 0
        done = "  %-*s  %-4s  %5.1fs  %s" % (width, name, "PASS" if ok else "FAIL",
                                             took, why)
        print(("\r" + done + " " * max(0, len(pending) - len(done))) if live else done)
        if not ok:
            tail = (proc.stdout or "").strip().splitlines()[-25:]
            for line in tail:
                print("      | %s" % line)
            err = (proc.stderr or "").strip().splitlines()[-12:]
            for line in err:
                print("      ! %s" % line)
        results.append((name, ok, took, why))

    passed = sum(1 for _, ok, _, _ in results if ok)
    total_time = sum(t for _, _, t, _ in results)
    print()
    print("%d of %d suites passed in %.0fs" % (passed, len(results), total_time))
    if passed != len(results):
        print("failed: %s" % ", ".join(n for n, ok, _, _ in results if not ok))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
