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
    ("test_upstream.py",     True,  "caching, single-flight and backing off a throttled source"),
    ("test_indicators.py",   True,  "indicator maths against known values"),
    ("test_pivots.py",       False, "pivot levels sit in the right units"),
    ("test_structure.py",    False, "swings, breaks, sweeps, gaps and the liquidity chain, no peeking"),
    ("test_clock.py",        False, "New York time, trading days, sessions, day levels and gaps"),
    ("test_bias.py",         False, "the daily lean from hourly bars, rule by rule, read as of a time"),
    ("test_labstore.py",     False, "the Setup lab's store and catalog, futures costs, daily-rule settings"),
    ("test_strategies.py",   True,  "every strategy fires sanely, and a BUY rests on a demonstrated edge"),
    ("test_liquidity_strategies.py", False, "the ten price-action setups fire on their scenes, vote, and never read a later bar"),
    ("test_alert_kinds.py",  False, "price-action alerts fire once per event, in plain facts"),
    ("test_options.py",      True,  "Black-Scholes, Greeks, implied volatility, parity"),
    ("test_quality.py",      True,  "Piotroski, Altman, Beneish and accruals against known values"),
    ("test_valuation.py",    True,  "discounted cash flow, multiples, risk measures"),
    ("test_fundamentals.py", True,  "Yahoo ratios reconciled against SEC filings"),
    ("test_screener.py",     True,  "filters accept and reject for stated reasons"),
    ("test_ideas.py",        True,  "the idea finder, and what it refuses to pad out"),
    ("test_modules.py",      True,  "thesis, alerts, reasoning payload and workflows"),
    ("test_thesis.py",       True,  "closing a thesis out, scoring the record, safe redirects"),
    ("test_operations.py",   True,  "earnings in the window, background checking, export, overlap"),
    ("test_ops.py",          True,  "charts, scrubbed error reports, verified backups, usage access"),
    ("test_offsite.py",      False, "off-site backup copies: signing, checks, history, restore"),
    ("test_accounts.py",     True,  "sign-in, sessions, and one person's data staying theirs"),
    ("test_signin_flow.py",  False, "the whole Google sign-in path, against a stubbed Google"),
    ("test_apple_signin.py", False, "Sign in with Apple on the site and in the app, against a stubbed Apple"),
    ("test_app_handoff.py",  False, "the iPhone app's browser sheet handing its sign-in back"),
    ("test_database.py",     False, "schema, journal, statistics and invariants"),
    ("test_learning.py",     True,  "strategy weight learning stays bounded"),
    ("test_audit.py",        True,  "what has no meaning on daily bars has no say there"),
    ("test_pipeline.py",     True,  "a full analysis end to end on several instruments"),
    ("test_web_forms.py",    True,  "settings, journal and analysis form handling"),
    ("test_web.py",          True,  "research, screener, portfolio, monitor and links"),
    ("test_codes.py",        False, "sign-in codes by email and text, and their limits"),
    ("test_billing.py",      False, "paid plans: signed webhooks, allowances, checkout"),
    ("test_appstore.py",     False, "Pro bought in the iPhone app: Apple's signatures, owners, renewals, refunds"),
    ("test_growth.py",       False, "popular pages, sharing, invites, weekly email"),
    ("test_learn.py",        False, "the Learn pages, the lab box on strategy tests, practice"),
    ("test_push.py",         False, "notifications to phones and browsers, and the installable app"),
    ("test_portfolio_risk.py", False, "open risk, breached stops, shorts, gaps and costs"),
    ("test_alert_loop.py",   False, "call changes, crossed stops, history, email, privacy"),
    ("test_sessions.py",     False, "market hours, and the alert loop leaving a closed market"),
    ("test_trade_chart.py",  False, "the analysis page's candle chart and plan levels"),
    ("test_levelview.py",    False, "what price is doing: levels, New York clock, the day's lean"),
    ("test_method.py",       False, "odds per side, gap fills, overlap, the BUY bar, plain words"),
    ("test_record.py",       False, "the public track record, splits, and the strategy tests' margins"),
    ("test_setuplab.py",     False, "the Setup lab: own stops and targets, random entries, no peeking"),
    ("test_claims.py",       False, "the video's claims on scripted days, against what usually happens"),
    ("test_market.py",       False, "refusals in words, whole funds, a coin's blank day, splits, tickers"),
    ("test_tickers.py",      False, "a ticker shared by a coin and a stock: both reachable, kept apart"),
    ("test_features.py",     False, "the front page, the trade review, plan against result, counts"),
    ("test_wording.py",      False, "what visitors read on a public site: no setup text, plain errors"),
    ("test_since.py",        False, "what changed on a watchlist since yesterday, page and email"),
    ("test_help.py",         False, "help beside each figure; no setup words or errors to visitors"),
    ("test_privacy.py",      False, "what the privacy page and terms promise, against what the code does"),
    ("test_journal_exits.py", False, "trailing stops, closing in parts, and closing after a split"),
    ("test_journal_tags.py", False, "journal tags, review, breakdown, daily rules, export, trade picture"),
    ("test_risk_tools.py",   False, "the size calculator and the survival check, sums and pages"),
    ("test_plan_recheck.py", False, "a lapsing paid plan read from Stripe; how the server starts, listens and stops"),
    ("test_lock.py",         False, "the lock file the server installs from agrees with requirements.txt"),
    ("test_ai_consent.py",   False, "a screenshot goes to Anthropic only after the person says yes"),
    ("test_app_mode.py",     False, "the iPhone app: no Stripe, counting, website links or downloads inside it; the review sign-in"),
    ("test_browser.py",      False, "every page at phone sizes in a real browser, and the main flows by touch"),
]

TIMEOUT = 900


def run_suite(name):
    """Run one suite. Returns (verdict, its output, its errors), never raises."""
    def words(said):
        return said.decode("utf-8", "replace") if isinstance(said, bytes) else said or ""

    try:
        proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                              capture_output=True, text=True, timeout=TIMEOUT,
                              cwd=ROOT)
    except subprocess.TimeoutExpired as exc:
        return ("FAIL", words(exc.stdout),
                words(exc.stderr) + "\ntimed out after %d s" % TIMEOUT)
    out, err = words(proc.stdout), words(proc.stderr)
    if proc.returncode != 0:
        return "FAIL", out, err
    # A suite that could not run here says so first, and has proved nothing.
    return ("SKIP" if out.lstrip().startswith("SKIP") else "PASS"), out, err


def main() -> int:
    # A suite missing from the list above would otherwise never run, silently.
    unlisted = sorted(set(f for f in os.listdir(HERE)
                          if f.startswith("test_") and f.endswith(".py"))
                      - set(s[0] for s in SUITES))
    if unlisted:
        print("not in SUITES, so never run: %s" % ", ".join(unlisted))
        return 1

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
        verdict, out, err = run_suite(name)
        took = time.time() - started
        done = "  %-*s  %-4s  %5.1fs  %s" % (width, name, verdict, took, why)
        print(("\r" + done + " " * max(0, len(pending) - len(done))) if live else done)
        if verdict == "SKIP":
            print("      | %s" % out.strip().splitlines()[0])
        if verdict == "FAIL":
            for line in out.strip().splitlines()[-25:]:
                print("      | %s" % line)
            for line in err.strip().splitlines()[-12:]:
                print("      ! %s" % line)
        results.append((name, verdict, took, why))

    passed = sum(1 for _, v, _, _ in results if v == "PASS")
    skipped = [n for n, v, _, _ in results if v == "SKIP"]
    failed = [n for n, v, _, _ in results if v == "FAIL"]
    total_time = sum(t for _, _, t, _ in results)
    print()
    print("%d of %d suites passed%s in %.0fs"
          % (passed, len(results),
             ", %d skipped" % len(skipped) if skipped else "", total_time))
    if skipped:
        print("skipped, so not checked: %s" % ", ".join(skipped))
    if failed:
        print("failed: %s" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
