"""Verify screener and portfolio modules."""
import sys, time
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness
from bot import screener as S, portfolio as P, database as db

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

print("=" * 72)
print("SCREENER: universes")
print("=" * 72)
syms = S.universe_symbols("most_actives", 20)
print("   most_actives -> %d symbols: %s" % (len(syms), syms[:8]))
check("universe returns symbols", len(syms) >= 5)

print()
print("=" * 72)
print("SCREENER: explicit list with shallow filters")
print("=" * 72)
t0 = time.time()
r = S.run(symbols=["AAPL","MSFT","NVDA","KO","T","F","PFE","XOM","JNJ","WMT"],
          filters={"market_cap_min": 5e10, "pe_max": 30}, limit=20)
print("   scanned %d, passed %d, rejected %d in %.1fs" % (
    r.scanned, len(r.passed), r.rejected, time.time()-t0))
for f in r.filters:
    print("      filter: %-32s %s" % (f["label"], f["value"]))
for c in r.passed:
    print("      %-6s %-28s cap=%s pe=%s" % (
        c.symbol, c.name[:28],
        "%.0fB" % (c.market_cap/1e9) if c.market_cap else "—",
        "%.1f" % c.pe if c.pe else "—"))
check("screen returned candidates", len(r.passed) > 0)
check("every survivor meets the cap filter",
      all(c.market_cap and c.market_cap >= 5e10 for c in r.passed))
check("every survivor meets the PE filter",
      all(c.pe and c.pe <= 30 for c in r.passed))

print()
print("   --- impossible filter should return nothing, not crash ---")
r2 = S.run(symbols=["AAPL","MSFT"], filters={"pe_max": 0.1}, limit=10)
check("impossible filter yields none", len(r2.passed) == 0)
print("      note: %s" % (r2.notes[0][:90] if r2.notes else "—"))

print()
print("   --- unknown data excluded by default ---")
r3 = S.run(symbols=["BTC-USD","AAPL"], filters={"pe_max": 100}, limit=10)
got = [c.symbol for c in r3.passed]
print("      passed: %s" % got)
check("crypto without a PE is excluded", "BTC-USD" not in got)

print()
print("=" * 72)
print("SCREENER: deep filters (fundamentals)")
print("=" * 72)
t0 = time.time()
r4 = S.run(symbols=["AAPL","MSFT","NVDA","KO","XOM","JNJ","WMT","PG"],
           filters={"roe_min": 0.15, "margin_min": 0.10}, limit=20)
print("   scanned %d, passed %d in %.1fs (deep=%s)" % (
    r4.scanned, len(r4.passed), time.time()-t0, r4.deep))
for c in r4.passed:
    print("      %-6s roe=%s margin=%s growth=%s quality=%s" % (
        c.symbol,
        "%.0f%%" % (c.roe*100) if c.roe else "—",
        "%.0f%%" % (c.net_margin*100) if c.net_margin else "—",
        "%.0f%%" % (c.revenue_growth*100) if c.revenue_growth is not None else "—",
        "%.2f" % c.quality if c.quality else "—"))
check("deep screen ran", r4.deep)
check("deep survivors meet ROE", all(c.roe and c.roe >= 0.15 for c in r4.passed))
check("deep survivors meet margin", all(c.net_margin and c.net_margin >= 0.10 for c in r4.passed))

print()
print("=" * 72)
print("SCREENER: presets are all well-formed")
print("=" * 72)
for key, preset in S.PRESETS.items():
    bad = [f for f in preset["filters"] if f not in S.FILTERS]
    check("preset %s uses real filters" % key, not bad, str(bad))
    check("preset %s has a rationale" % key, len(preset.get("why", "")) > 30)
print("   %d presets checked" % len(S.PRESETS))

print()
print("=" * 72)
print("PORTFOLIO")
print("=" * 72)
import os
# A private database, so this suite neither depends on nor destroys
# whatever is in the real one.
DB = harness.isolate("screener")[0]
conn = db.connect(DB)
db.open_trade(conn, "AAPL", "1d", 1, entry=300, stop=285, target1=340, quantity=30)
db.open_trade(conn, "MSFT", "1d", 1, entry=480, stop=455, target1=540, quantity=12)
db.open_trade(conn, "NVDA", "1d", 1, entry=210, stop=198, target1=240, quantity=40)
db.open_trade(conn, "KO",   "1d", 1, entry=85,  stop=80,  target1=95,  quantity=60)
rows = db.list_trades(conn, status="open")
conn.close()

t0 = time.time()
v = P.build(rows, account=100000)
print("   built in %.1fs" % (time.time()-t0))
print("   invested %s  cash %s  open P&L %s" % (
    "{:,.0f}".format(v.invested), "{:,.0f}".format(v.cash), "{:+,.0f}".format(v.open_pnl)))
print("   gross exposure %s  net %s  total risk %s" % (
    "{:,.0f}".format(v.gross_exposure), "{:,.0f}".format(v.net_exposure),
    "{:,.0f}".format(v.total_risk)))
print("   concentration %.3f (%.1f effective positions)" % (
    v.concentration or 0, 1/v.concentration if v.concentration else 0))
print("   portfolio beta %s" % ("%.2f" % v.portfolio_beta if v.portfolio_beta else "—"))
print("   by sector: %s" % {k: "%.0f%%" % (val/v.gross_exposure*100)
                            for k, val in v.by_sector.items()})
check("all positions priced", len(v.failed) == 0, str(v.failed))
check("invested is positive", v.invested > 0)
check("concentration between 0 and 1", 0 < (v.concentration or 0) <= 1)
check("cash plus invested is about the account",
      abs((v.cash + v.invested) - 100000) < 1 or v.invested > 100000)
check("correlation computed", bool(v.correlation.get("usable")))
if v.correlation.get("usable"):
    print("   average correlation %.3f" % v.correlation["average"])
print("   observations:")
for o in v.observations:
    print("      - %s" % o[:110])
print("   warnings:")
for w in v.warnings:
    print("      ! %s" % w[:110])

print()
print("   --- what if the market drops 10%% ---")
wi = P.what_if(v, -0.10)
print("      total change %s (%.1f%% of account)" % (
    "{:+,.0f}".format(wi["total_change"]), wi["pct_of_account"]))
for row in wi["rows"][:3]:
    print("      %-6s beta %.2f -> %s" % (row["symbol"], row["beta"],
                                          "{:+,.0f}".format(row["change"])))
check("shock produces a loss for a long book", wi["total_change"] < 0)

print()
print("   --- empty portfolio ---")
ev = P.build([], account=50000)
check("empty portfolio handled", ev.cash == 50000 and not ev.warnings)
print("      %s" % ev.observations[0])

print()
print("=" * 72)
print("SCREENER: every name scanned is accounted for (stubbed, no network)")
print("=" * 72)
import re as _re
import html as _html
from bot import yahoo as _Y

_real = (S.universe_symbols, _Y.quotes, _Y.quote_summary, S.fund.load,
         S.fund.quality_score)


def _quotes_for(symbols):
    out = []
    for s in symbols:
        n = int(s[1:])
        # The biggest companies sit late in the data source's own order.
        cap = (1e9 + n * 1e9) if n < 100 else 1e8
        out.append({"symbol": s, "shortName": s, "regularMarketPrice": 50.0,
                    "regularMarketChangePercent": 1.0,
                    "regularMarketVolume": 2_000_000, "marketCap": cap,
                    "trailingPE": 15.0, "fiftyTwoWeekHigh": 60.0})
    return out


def _good_fund(symbol, with_sec=False):
    f = S.fund.Fundamentals(symbol=symbol, sector="Tech")
    for key, value in (("roe", 0.2), ("net_margin", 0.15), ("revenue_growth", 0.1),
                       ("debt_to_equity", 50.0), ("free_cashflow", 1e9)):
        S.fund._put(f.metrics, key, key, value)
    return f


def _adds_up(res):
    return res.scanned == (len(res.passed) + res.rejected + res.not_checked
                           + len(res.unread) + res.not_shown)


S.universe_symbols = lambda name, count=100: ["S%03d" % i for i in range(120)][:count]
_Y.quotes = _quotes_for
S.fund.load = _good_fund
S.fund.quality_score = lambda f: {"score": 0.7, "reliable": True}
try:
    r5 = S.run(universe="most_actives", filters={"market_cap_min": 1e9}, limit=40)
    check("more passing than the page lists: the rest are counted, not lost",
          len(r5.passed) == 40 and r5.not_shown == 60 and _adds_up(r5)
          and any("60 more passed" in n for n in r5.notes),
          "%d passed, %d not shown, notes %s" % (len(r5.passed), r5.not_shown, r5.notes))
    r6 = S.run(universe="most_actives",
               filters={"market_cap_min": 1e9, "roe_min": 0.1}, limit=40)
    check("the slower checks go to the largest names, the ones the list shows",
          r6.passed and r6.passed[0].symbol == "S099" and r6.passed[0].deep_loaded,
          str([c.symbol for c in r6.passed[:3]]))
    check("and the names they never reached are counted and named as such",
          r6.not_checked == 60 and _adds_up(r6)
          and any("not checked" in n for n in r6.notes), str(r6.not_checked))

    def _refused(symbol, modules):
        raise _Y.YahooError("Yahoo is rate limiting this server (HTTP 429).")

    S.fund.load = _real[3]
    _Y.quote_summary = _refused
    _Y.quotes = lambda syms: [dict(q, symbol=s) for q, s in
                              zip(_quotes_for(["S050"] * 3), ["AAA", "BBB", "CCC"])]
    r7 = S.run(symbols=["AAA", "BBB", "CCC"], filters={"roe_min": 0.1}, limit=40)
    check("figures the source refused are neither passed nor rejected",
          not r7.passed and r7.rejected == 0 and r7.unread == ["AAA", "BBB", "CCC"],
          "rejected %d, unread %s" % (r7.rejected, r7.unread))
    check("and the notes say they could not be read, not that the filters were tight",
          any("could not be read just now" in n for n in r7.notes)
          and not any("too tight" in n for n in r7.notes)
          and not any("were loaded" in n for n in r7.notes), str(r7.notes))
    check("so that screen is not one to keep and share", not r7.complete)

    def _slow_fund(symbol, with_sec=False):
        time.sleep(2.0)
        return _good_fund(symbol)

    _Y.quote_summary = _real[2]
    S.fund.load = _slow_fund
    t0 = time.time()
    r8 = S.run(symbols=["AAA", "BBB", "CCC"], filters={"roe_min": 0.1}, limit=40,
               budget=0.5)
    took = time.time() - t0
    check("a screen keeps to its time limit when the source is slow",
          took < 1.5 and r8.unread == ["AAA", "BBB", "CCC"] and r8.rejected == 0,
          "%.1fs, unread %s" % (took, r8.unread))
finally:
    (S.universe_symbols, _Y.quotes, _Y.quote_summary, S.fund.load,
     S.fund.quality_score) = _real

print()
print("=" * 72)
print("SCREENER: the page (stubbed, no network)")
print("=" * 72)
import web as _web
_client, _ = harness.isolated_web("screener-page")
_web.app.config["AUTH_MODE"] = "off"


def _text(body):
    body = _re.sub(r"<script.*?</script>", "", body, flags=_re.S)
    return _re.sub(r"\s+", " ", _html.unescape(_re.sub(r"<[^>]+>", " ", body)))


def _two_quotes(syms):
    return [{"symbol": "LOSR", "shortName": "Down 5%", "regularMarketPrice": 10.0,
             "regularMarketChangePercent": -5.0, "marketCap": 2e9,
             "regularMarketVolume": 2e6},
            {"symbol": "FLAT", "shortName": "Unchanged", "regularMarketPrice": 10.0,
             "regularMarketChangePercent": 0.0, "marketCap": 3e9,
             "regularMarketVolume": 2e6}]


S.universe_symbols = lambda name, count=100: ["LOSR", "FLAT"]
_Y.quotes = _two_quotes
try:
    body = _client.get("/screener?go=1&universe=day_losers&change_max=-3"
                       ).data.decode("utf-8", "replace")
    page = _text(body)
    check("'up at most -3' means down at least 3%, not -300%",
          "Up at most -3.0%" in page and "-300" not in page
          and "<td><b>LOSR</b>" in body and "<td><b>FLAT</b>" not in body, page[:0])
    check("and the box still shows what was typed", 'value="-3"' in body)
    page = _text(_client.get("/screener?go=1&universe=day_losers&change_min=1"
                             ).data.decode("utf-8", "replace"))
    check("'up at least 1' means 1%", "Up at least 1.0%" in page
          and "100.0%" not in page)
    page = _text(_client.get("/screener?go=1&universe=day_losers&change_min=0.5"
                             ).data.decode("utf-8", "replace"))
    check("and 0.5 means half a percent", "Up at least 0.5%" in page)
    body = _client.get("/screener?go=1&universe=day_losers"
                       ).data.decode("utf-8", "replace")
    flat = body[body.find("<td><b>FLAT</b>"):]
    flat = flat[:flat.find("</tr>")]
    # The cell may carry more attributes after its class (a phone shows each
    # cell's column name from data-label), so only the class is matched.
    today = _re.findall(r'<td class="r fig([^"]*)"[^>]*>\s*0\.0%', flat)
    check("a move of 0.0% is not coloured as a loss",
          today and "loss" not in today[0] and "gain" not in today[0], str(today))
    check("and the page says when the quotes were read",
          _re.search(r"Quotes read at \d\d:\d\d UTC", _text(body)) is not None)

    # A visitor's ready-made screen is shared, but never a refused one.
    _web.app.config["AUTH_MODE"] = "google"

    def _refused_quotes(syms):
        raise _Y.YahooError("Yahoo is rate limiting this server (HTTP 429).")

    S.universe_symbols = lambda name, count=100: ["GAIN"]
    _Y.quotes = _refused_quotes
    first = _text(_client.get("/screener?preset=momentum").data.decode("utf-8", "replace"))
    _Y.quotes = lambda syms: [{"symbol": "GAIN", "shortName": "Gainer",
                               "regularMarketPrice": 10.0,
                               "regularMarketChangePercent": 5.0,
                               "regularMarketVolume": 5e6, "marketCap": 2e9}]
    second = _text(_client.get("/screener?preset=momentum").data.decode("utf-8", "replace"))
    check("a visitor whose screen the source refused is told so",
          "Could not fetch quotes" in first
          and "Nothing could be screened just now" in first
          and "Nothing passed these filters" not in first)
    check("and the next visitor gets a fresh try, not that failure for 15 minutes",
          "1 passed" in second and "Could not fetch quotes" not in second,
          second[second.find("Results"):second.find("Results") + 90])
finally:
    (S.universe_symbols, _Y.quotes, _Y.quote_summary, S.fund.load,
     S.fund.quality_score) = _real
    _web.app.config["AUTH_MODE"] = None

print()
print("%d failure(s)" % len(fails))
print("SCREENER AND PORTFOLIO OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
