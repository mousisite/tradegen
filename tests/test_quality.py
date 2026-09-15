"""The published scoring models, checked against hand-computed values.

A formula that runs is not a formula that is right. Each model here is checked
against a fixture whose answer was worked out by hand from the published
definition, so a coefficient typed wrong fails loudly rather than producing a
plausible number nobody questions.
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

from bot import quality

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def history(**series):
    """Build a filings table in the shape financial_history returns."""
    years = sorted({year for values in series.values() for year in values})
    table = {}
    for key, values in series.items():
        table[key] = [{"fy": year, "value": value, "end": "%d-12-31" % year,
                       "form": "10-K", "filed": "%d-02-01" % (year + 1),
                       "url": "https://sec.gov/x"}
                      for year, value in sorted(values.items())]
    return {"available": True, "entity": "Test Co", "cik": 1,
            "years": years, "table": table, "derived": {}, "cagr": {}}


print("=" * 72)
print("PIOTROSKI F-SCORE")
print("=" * 72)

# Every one of the nine improves. Built so each test has an unambiguous answer.
perfect = history(
    net_income={2023: 100.0, 2024: 200.0},         # ROA up, positive
    assets={2023: 1000.0, 2024: 1000.0},
    operating_cashflow={2023: 150.0, 2024: 300.0},  # positive, above net income
    long_term_debt={2023: 200.0, 2024: 100.0},      # leverage down
    current_assets={2023: 300.0, 2024: 500.0},      # current ratio up
    current_liabilities={2023: 200.0, 2024: 200.0},
    shares_diluted={2023: 100.0, 2024: 99.0},       # no dilution
    gross_profit={2023: 400.0, 2024: 600.0},        # margin up
    revenue={2023: 1000.0, 2024: 1100.0},           # turnover up
)
score = quality.piotroski(perfect)
print("   every measure improving -> %s" % score.value)
for t in score.tests:
    print("      %-24s %s  %s" % (t.name,
                                  "yes" if t.passed else "no " if t.passed is False else "?",
                                  t.detail[:44]))
check("a company improving on every measure scores 9", score.value == 9.0, score.value)
check("all nine were measurable", score.reliable)
check("the verdict says it is strong", "Strong" in score.verdict)

# Everything worsens.
awful = history(
    net_income={2023: 200.0, 2024: -50.0},
    assets={2023: 1000.0, 2024: 1000.0},
    operating_cashflow={2023: 150.0, 2024: -80.0},
    long_term_debt={2023: 100.0, 2024: 400.0},
    current_assets={2023: 500.0, 2024: 200.0},
    current_liabilities={2023: 200.0, 2024: 400.0},
    shares_diluted={2023: 100.0, 2024: 140.0},
    gross_profit={2023: 600.0, 2024: 200.0},
    revenue={2023: 1100.0, 2024: 700.0},
)
score = quality.piotroski(awful)
print("   every measure worsening -> %s" % score.value)
check("a company worsening on every measure scores 0", score.value == 0.0, score.value)
check("the verdict says it is weak", "Weak" in score.verdict)

# The accruals test specifically: profit without cash must fail it.
paper = history(
    net_income={2023: 100.0, 2024: 300.0},
    assets={2023: 1000.0, 2024: 1000.0},
    operating_cashflow={2023: 150.0, 2024: 50.0},   # far below net income
    revenue={2023: 1000.0, 2024: 1100.0},
)
score = quality.piotroski(paper)
cash_test = [t for t in score.tests if t.name == "Profit backed by cash"][0]
check("profit not backed by cash fails that test", cash_test.passed is False)

# A partial score must be reported as partial, not silently rescaled.
partial = history(
    net_income={2023: 100.0, 2024: 200.0},
    assets={2023: 1000.0, 2024: 1000.0},
    operating_cashflow={2023: 150.0, 2024: 300.0},
    revenue={2023: 1000.0, 2024: 1100.0},
    gross_profit={2023: 400.0, 2024: 600.0},
)
score = quality.piotroski(partial)
print("   partial data -> %s, reliable=%s, missing %d"
      % (score.value, score.reliable, len(score.missing)))
check("a partial score still reports a value", score.usable)
check("and is flagged as not fully measurable", score.reliable is False)
check("and names what was missing", len(score.missing) > 0)
check("and says so in the verdict", "rather than 9" in score.verdict)

thin = history(net_income={2024: 100.0}, assets={2024: 1000.0})
check("one year of filings produces no score",
      quality.piotroski(thin).usable is False)

print()
print("=" * 72)
print("ALTMAN Z-SCORE")
print("=" * 72)

# Worked by hand from Z = 1.2A + 1.4B + 3.3C + 0.6D + 1.0E
#   A = (500-200)/1000 = 0.30      1.2 * 0.30 = 0.36
#   B = 400/1000       = 0.40      1.4 * 0.40 = 0.56
#   C = 150/1000       = 0.15      3.3 * 0.15 = 0.495
#   D = 2000/600       = 3.3333    0.6 * 3.3333 = 2.0
#   E = 1200/1000      = 1.20      1.0 * 1.20 = 1.20
#                                  total      = 4.615
fixture = history(
    assets={2024: 1000.0}, current_assets={2024: 500.0},
    current_liabilities={2024: 200.0}, retained_earnings={2024: 400.0},
    operating_income={2024: 150.0}, liabilities={2024: 600.0},
    revenue={2024: 1200.0},
    net_income={2024: 100.0}, operating_cashflow={2024: 120.0},
)
score = quality.altman_z(fixture, market_cap=2000.0)
print("   computed %.4f, expected 4.6150" % (score.value or 0))
check("Z matches the hand-computed value",
      score.usable and abs(score.value - 4.615) < 0.001, score.value)
check("it reads as out of distress", "out of distress" in score.verdict)
check("the components are shown", len(score.inputs) == 5)

# Distressed: negative working capital, no retained earnings, losses.
distressed = history(
    assets={2024: 1000.0}, current_assets={2024: 100.0},
    current_liabilities={2024: 600.0}, retained_earnings={2024: -300.0},
    operating_income={2024: -80.0}, liabilities={2024: 900.0},
    revenue={2024: 400.0},
)
score = quality.altman_z(distressed, market_cap=150.0)
print("   distressed fixture -> %.2f" % score.value)
check("a weak balance sheet lands below 1.81", score.value < 1.81, score.value)
check("and is described as distress", "distress" in score.verdict)

# Banks must be refused rather than scored.
for sector in ("Financial Services", "Banks", "Insurance"):
    score = quality.altman_z(fixture, market_cap=2000.0, sector=sector)
    check("refuses to score a %s company" % sector.lower(), not score.usable)
    check("and explains why (%s)" % sector.lower(), "manufacturers" in score.verdict)

score = quality.altman_z(fixture, market_cap=2000.0, sector="Technology")
check("a non-financial company is still scored", score.usable)

score = quality.altman_z(fixture, market_cap=None)
check("no market value means no score", not score.usable)
check("and it names what was missing",
      any("market" in m for m in score.missing), score.missing)

print()
print("=" * 72)
print("BENEISH M-SCORE")
print("=" * 72)

# Every ratio held at exactly 1 and accruals at zero, so the score must be the
# sum of the coefficients plus the constant. Worked by hand:
#   -4.84 + 0.920 + 0.528 + 0.404 + 0.892 + 0.115 - 0.172 + 0 - 0.327 = -2.480
neutral = history(
    revenue={2023: 1000.0, 2024: 1000.0},
    receivables={2023: 100.0, 2024: 100.0},
    gross_profit={2023: 400.0, 2024: 400.0},
    current_assets={2023: 300.0, 2024: 300.0},
    ppe_net={2023: 500.0, 2024: 500.0},
    assets={2023: 1000.0, 2024: 1000.0},
    depreciation={2023: 50.0, 2024: 50.0},
    sga_expense={2023: 200.0, 2024: 200.0},
    current_liabilities={2023: 200.0, 2024: 200.0},
    long_term_debt={2023: 100.0, 2024: 100.0},
    net_income={2023: 100.0, 2024: 100.0},
    operating_cashflow={2023: 100.0, 2024: 100.0},   # accruals exactly zero
)
score = quality.beneish_m(neutral)
print("   unchanged company -> %.4f, expected -2.4800" % (score.value or 0))
check("M matches the hand-computed neutral value",
      score.usable and abs(score.value - (-2.480)) < 0.001, score.value)
check("and sits below the -1.78 flag", score.value < -1.78)
check("the eight ratios are shown", len(score.inputs) == 8)

# Receivables ballooning while cash lags: the classic pattern.
suspicious = history(
    revenue={2023: 1000.0, 2024: 1400.0},
    receivables={2023: 100.0, 2024: 400.0},          # far faster than sales
    gross_profit={2023: 400.0, 2024: 400.0},
    current_assets={2023: 300.0, 2024: 300.0},
    ppe_net={2023: 500.0, 2024: 400.0},
    assets={2023: 1000.0, 2024: 1200.0},
    depreciation={2023: 50.0, 2024: 30.0},
    sga_expense={2023: 200.0, 2024: 200.0},
    current_liabilities={2023: 200.0, 2024: 200.0},
    long_term_debt={2023: 100.0, 2024: 100.0},
    net_income={2023: 100.0, 2024: 300.0},
    operating_cashflow={2023: 100.0, 2024: 20.0},    # profit without cash
)
score = quality.beneish_m(suspicious)
print("   receivables and accruals inflated -> %.2f" % score.value)
check("the suspicious pattern scores higher", score.value > -2.480, score.value)
check("it crosses the flag", score.value > -1.78, score.value)
check("and the verdict refuses to accuse", "rather than drawing a conclusion"
      in score.verdict or "as readily as" in score.verdict)

print()
print("=" * 72)
print("EARNINGS QUALITY")
print("=" * 72)

# (net income - cash flow) / average assets = (300-100)/1000 = 0.20
gap = history(
    net_income={2023: 100.0, 2024: 300.0},
    operating_cashflow={2023: 100.0, 2024: 100.0},
    assets={2023: 1000.0, 2024: 1000.0},
)
score = quality.accruals(gap)
print("   profit far above cash -> %.4f, expected 0.2000" % score.value)
check("accruals match the hand-computed value",
      abs(score.value - 0.20) < 1e-9, score.value)
check("and are called out as likely to reverse", "reverse" in score.verdict)

cash_rich = history(
    net_income={2023: 100.0, 2024: 100.0},
    operating_cashflow={2023: 100.0, 2024: 250.0},
    assets={2023: 1000.0, 2024: 1000.0},
)
score = quality.accruals(cash_rich)
check("cash above profit is negative", score.value < 0, score.value)
check("and is described as the good direction", "good direction" in score.verdict)

print()
print("=" * 72)
print("PUTTING THEM TOGETHER")
print("=" * 72)

strong = quality.assess(history(
    net_income={2023: 100.0, 2024: 200.0}, assets={2023: 1000.0, 2024: 1000.0},
    operating_cashflow={2023: 150.0, 2024: 300.0},
    long_term_debt={2023: 200.0, 2024: 100.0},
    current_assets={2023: 300.0, 2024: 500.0},
    current_liabilities={2023: 200.0, 2024: 200.0},
    shares_diluted={2023: 100.0, 2024: 99.0},
    gross_profit={2023: 400.0, 2024: 600.0},
    revenue={2023: 1000.0, 2024: 1100.0},
    retained_earnings={2023: 300.0, 2024: 400.0},
    operating_income={2023: 120.0, 2024: 150.0},
    liabilities={2023: 600.0, 2024: 600.0},
    # The manipulation model needs its own inputs; without them the combined
    # assessment would be quietly running three models rather than four.
    receivables={2023: 100.0, 2024: 100.0},
    ppe_net={2023: 500.0, 2024: 500.0},
    depreciation={2023: 50.0, 2024: 50.0},
    sga_expense={2023: 200.0, 2024: 200.0},
), market_cap=2000.0, sector="Technology")
print("   %s" % strong["headline"][:150])
check("a strong company reports strengths", len(strong["strengths"]) > 0)
check("and no concerns", len(strong["concerns"]) == 0, strong["concerns"])
check("every model produced a score",
      all(s.usable for s in strong["scores"].values()))
check("the note refuses to call it a buy", "not a reason to buy" in strong["note"])

check("no filings means no scores",
      quality.assess({"available": False, "reason": "no filings"})["available"]
      is False)
check("and the specific reason is kept",
      quality.assess({"available": False, "reason": "reorganised"})["reason"]
      == "reorganised")

print()
print("=" * 72)
print("AGAINST REAL FILINGS")
print("=" * 72)

from bot import fundamentals as F
from bot import sec

for symbol in ("AAPL", "KO", "JPM"):
    filings = sec.financial_history(symbol, years=8)
    facts = F.load(symbol, with_sec=False)
    out = quality.assess(filings, facts.get("market_cap"), facts.sector)
    if not out["available"]:
        print("   %-6s %s" % (symbol, out["reason"][:60]))
        continue
    scores = out["scores"]
    shown = " ".join(
        "%s=%s" % (key[:4], ("%.2f" % s.value) if s.usable else "n/a")
        for key, s in scores.items())
    print("   %-6s %s" % (symbol, shown))
    check("%s produced a verdict for every model" % symbol,
          all(s.verdict for s in scores.values()))
    f = scores["piotroski"]
    if f.usable:
        check("%s F-score is within 0 to 9" % symbol, 0 <= f.value <= 9, f.value)
    a = scores["accruals"]
    if a.usable:
        check("%s accruals are a plausible fraction" % symbol,
              -1 < a.value < 1, a.value)

# A bank must be refused the Z-score on real data too, not only in a fixture.
bank = F.load("JPM", with_sec=False)
out = quality.assess(sec.financial_history("JPM", years=8),
                     bank.get("market_cap"), bank.sector)
if out["available"]:
    check("a real bank is refused a Z-score",
          not out["scores"]["altman"].usable)

print()
print("%d failure(s)" % len(fails))
print("QUALITY MODELS OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
