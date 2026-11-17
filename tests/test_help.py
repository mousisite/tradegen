"""The "?" beside each figure on the analysis page.

Every help used has words, the words live in one place (plain.HELP) and say
what this code does, the settings' own numbers are filled in, and the page
draws each one as a real button with a name that needs no script, in a spot
the browser will not tear apart, big enough for a finger. Also: what the
page says about the news when no Claude key is set never names a setting.

No network: the analysis runs on made-up prices.
"""
import json
import os as _os
import re
import sys as _sys
from html.parser import HTMLParser
from types import SimpleNamespace as NS

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

for key in ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET", "STOCKBOT_PUBLIC_URL", "STOCKBOT_BEHIND_PROXY"):
    _os.environ.pop(key, None)

import numpy as np  # noqa: E402

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import calibrate, catalysts, config, engine, indicators, learning, plain  # noqa: E402
from bot import database as db  # noqa: E402
from bot import reasoning as reasoning_mod  # noqa: E402
from bot import sentiment as sentiment_mod  # noqa: E402
from bot import strategies  # noqa: E402
from bot.market import Bars  # noqa: E402

fails = []
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
TEMPLATES = _os.path.join(ROOT, "templates")


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def flat(html):
    return " ".join(html.split())


head("EVERY HELP USED HAS WORDS, AND THEY LIVE IN ONE PLACE")
used = set()
for name in sorted(_os.listdir(TEMPLATES)):
    if not name.endswith(".html"):
        continue
    text = open(_os.path.join(TEMPLATES, name), encoding="utf-8").read()
    used |= set(re.findall(r"\bhelp\(\s*'(\w+)'\s*\)", text))
    for group in re.findall(r"\bhelp_list\(\s*\[([^\]]*)\]", text):
        used |= set(re.findall(r"'(\w+)'", group))
check("the analysis page uses a good number of them", len(used) >= 30, sorted(used))
check("every key a template uses has words in plain.HELP",
      used <= set(plain.HELP), sorted(used - set(plain.HELP)))
check("and every entry in plain.HELP is used somewhere", set(plain.HELP) <= used,
      sorted(set(plain.HELP) - used))
filled = plain.help_for(config.DEFAULTS)
check("help_for fills every entry", set(filled) == set(plain.HELP))
long_ones, braces, empty = [], [], []
for key, h in filled.items():
    if not h["name"].strip() or not h["text"].strip():
        empty.append(key)
    if "{" in h["text"] or "}" in h["text"]:
        braces.append(key)
    # A sentence ends at a full stop, question or exclamation mark followed
    # by a space or the end; "0.10R" and "2.5 ATR" are not ends.
    if len(re.findall(r"[.!?](?:\s|$)", h["text"])) > 2:
        long_ones.append(key)
check("each has a name and words", not empty, empty)
check("one or two sentences each", not long_ones, long_ones)
check("with every setting filled in", not braces, braces)
check("and no setting's name or a file in them",
      not any(re.search(r"[A-Z]{3,}_[A-Z_]+|\.env\b|config\.json", h["text"])
              for h in filled.values()))

head("THE WORDS CARRY THE PERSON'S OWN NUMBERS")
mine = dict(config.DEFAULTS, stop_atr_multiple=2.0, target_atr_multiple=3.0,
            risk_per_trade_pct=0.5, account_size=25000.0, max_position_pct=20.0,
            cost_bps_equity=5.0, cost_bps_crypto=10.0, min_bucket_samples=30,
            sentiment_weight=0.2)
said = plain.help_for(mine)
check("the stop and target distances", "It is 2 ATR from the entry" in said["stop"]["text"]
      and "3 ATR from the entry" in said["target"]["text"])
check("the size rule", "loses 0.5% of the 25,000 account" in said["size"]["text"]
      and "never more than 20% of the account" in said["size"]["text"], said["size"]["text"])
check("the costs", "0.05% of the price for a stock or fund and 0.1% for a coin"
      in said["costs"]["text"], said["costs"]["text"])
check("the smallest sample trusted", "fewer than 30" in said["sample"]["text"])
check("and what separate tests are", "count as fewer separate tests"
      in said["sample"]["text"])
check("the news's share of the say", "20% of the say" in said["with_news"]["text"]
      and "20% of the say" in said["news"]["text"])
check("the holding window, in the bars' own time",
      "within 10 trading days" in plain.help_for(config.DEFAULTS)["hit_rate"]["text"]
      and "within 2.0 hours" in plain.help_for(config.DEFAULTS, "5m")["hit_rate"]["text"]
      and "10-bar limit" in plain.help_for(config.DEFAULTS)["hold"]["text"])

head("THE WORDS SAY WHAT THIS CODE DOES")
# Each fact the help states, checked against the code that sets it, so a
# change to one without the other fails here.
check("ATR and RSI over 14 bars",
      "14 bars" in filled["atr"]["text"] and "14 bars" in filled["rsi"]["text"]
      and "ind.atr(h, l, c, 14)" in open(_os.path.join(ROOT, "bot", "strategies.py")).read()
      and "ind.rsi(c, 14)" in open(_os.path.join(ROOT, "bot", "strategies.py")).read())
check("the weight given runs from the learner's floor to its ceiling",
      "from %g to %g" % (learning.MIN_MULT, learning.MAX_MULT) in filled["weight_given"]["text"])
check("the stop is within 2.5 ATR when it sits past a level",
      "2.5 ATR" in filled["stop"]["text"]
      and "2.5 * atr_value" in open(_os.path.join(ROOT, "bot", "decision.py")).read())


class Regime:
    """classify_regime reads named series at a bar; a stand-in holds them."""

    def __init__(self, adx, bw):
        self.s = {"adx": adx, "bw_pctile": bw, "ema21_slope": 0.0,
                  "plus_di": 20.0, "minus_di": 20.0}

    def at(self, key, i):
        return self.s[key]


check("trending from an ADX of 25, ranging at 17 or less",
      strategies.classify_regime(Regime(25.0, 0.5), 0).trend == "trending"
      and strategies.classify_regime(Regime(17.0, 0.5), 0).trend == "ranging"
      and strategies.classify_regime(Regime(20.0, 0.5), 0).trend == "transitional"
      and "25 or more, ranging at 17 or less" in filled["regime"]["text"])
check("a bar that touched both stop and target counts as the stop",
      "counts as the stop" in filled["hit_rate"]["text"])
ctx = type("Ctx", (), {})()
DAY = 86400


def bars_from(close, high, low, open_=None):
    n = len(close)
    ts = np.arange(n, dtype=np.int64) * DAY + 1_600_000_000
    return Bars(symbol="T", name="T", asset_class="equity", currency="USD", exchange="T",
                interval="1d", timestamp=ts, open=np.asarray(open_ or close, float),
                high=np.asarray(high, float), low=np.asarray(low, float),
                close=np.asarray(close, float), volume=np.full(n, 1e6),
                session_id=ts // DAY, gmt_offset=0, prev_close=close[-2])


both = bars_from([100.0, 100.0, 100.0], [100.0, 104.0, 100.0], [100.0, 96.0, 100.0])
ctx.bars, ctx.atr = both, np.array([2.0, 2.0, 2.0])
won, r, _held, _cost = calibrate.simulate_trade(ctx, 0, 1, 1.5, 1.5, 1)
check("and the replay does count it so", won is False and r == -1.0, (won, r))
check("the 95% range holds 19 times in 20", "19 times in 20" in filled["range"]["text"]
      and calibrate.wilson_interval.__defaults__ == (1.96,))
check("headlines count about twice as much as posts",
      "about twice" in filled["news"]["text"]
      and "0.68 * news_score + 0.32 * social_score"
      in open(_os.path.join(ROOT, "bot", "sentiment.py")).read())
check("the indicator periods the glossary names",
      "ind.ema(c, 9)" in open(_os.path.join(ROOT, "bot", "strategies.py")).read()
      and indicators.macd.__defaults__ == (12, 26, 9)
      and "12-bar and a 26-bar" in filled["macd"]["text"]
      and "9-bar average" in filled["macd"]["text"])

head("THE PAGE")
c, DB = harness.isolated_web("help")
web.app.config["AUTH_MODE"] = None
web._example_state["busy"] = True
with open(harness.CFG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"stop_atr_multiple": 2.0, "target_atr_multiple": 3.0}, fh)


def edge_series(seed, n=1250, leg=60, drift=0.005, vol=0.01, into=40, dip=4):
    """Rises and falls in turn, ending on a turn up: a plan with every figure."""
    rng = np.random.RandomState(seed)
    k = np.arange(n)
    sign = np.where(((k - (n - into)) // leg) % 2 == 0, 1.0, -1.0)
    rets = sign * drift + rng.normal(0.0, vol, n)
    rets[-dip - 1:-1] = -0.012 + rng.normal(0.0, vol * 0.2, dip)
    rets[-1] = 0.02
    close = 100.0 * np.exp(np.cumsum(rets))
    prev = np.concatenate([[100.0], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, vol * 0.2, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.3, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.3, n)))
    open_[-1] = low[-1] = close[-2] * 0.998
    high[-1] = close[-1] = max(close[-1], high[-2] * 1.002)
    start = 1_600_000_000 - (1_600_000_000 % DAY) + 52200
    ts = np.array([start + i * DAY for i in range(n)], dtype=np.int64)
    return Bars(symbol="TEST", name="Test Corp", asset_class="equity", currency="USD",
                exchange="TEST", interval="1d", timestamp=ts, open=open_, high=high,
                low=low, close=close, volume=np.exp(rng.normal(15.0, 0.3, n)),
                session_id=((ts - 18000) // DAY).astype(np.int64), gmt_offset=-18000,
                prev_close=float(close[-2]), live_price=None, market_open=False,
                last_bar_time="", meta={})


real = {"resolve": engine.resolve, "bars": engine.fetch_bars, "calendar": catalysts.fetch,
        "news": sentiment_mod.gather_news, "social": sentiment_mod.gather_social}
engine.resolve = lambda s: {"symbol": s, "name": "Test Corp"}
engine.fetch_bars = lambda s, iv: edge_series(0)


def no_calendar(symbol):
    raise RuntimeError("no calendar in tests")


catalysts.fetch = no_calendar
try:
    r = c.get("/analyse?symbol=TEST&interval=1d&news=0")
    page = r.data.decode("utf-8", "replace")
    check("the analysis page draws", r.status_code == 200, r.status_code)
    toggles = re.findall(r'<details class="help(?: list)?">(.*?)</details>', page, re.S)
    check("with a help toggle beside each figure", len(toggles) >= 20, len(toggles))
    named = [t for t in toggles if re.search(
        r'<summary><span class="q" aria-hidden="true">\?</span>'
        r'<span class="(sr|lbl)">[^<]+</span></summary>', t)]
    check("each a summary, which is a button to a keyboard and a screen reader, "
          "with a name", len(named) == len(toggles), len(toggles) - len(named))
    check("named as a question about the figure",
          '<span class="sr">What expectancy means</span>' in page
          and '<span class="sr">What the stop means</span>' in page
          and '<span class="sr">What the call means</span>' in page)
    check("and no script needed to open one",
          not any("<script" in t or "onclick" in t for t in toggles))

    def near(label, key, width=400):
        at = page.find(label)
        return at >= 0 and ('<span class="sr">What %s means</span>'
                            % plain.HELP[key]["name"]) in page[at:at + width]

    check("beside the call", near('<div class="line">', "call", 200))
    check("beside the plan's figures", near("<dt>Entry</dt>", "entry")
          and near("<dt>Stop</dt>", "stop") and near("<dt>First target</dt>", "target")
          and near("<dt>Reward to risk</dt>", "reward_risk", 700)
          and near("<dt>Size</dt>", "size") and near("<dt>You risk</dt>", "risk"))
    check("beside the odds", near(">Reached target first", "hit_rate", 300)
          and near(">Expectancy", "expectancy", 300) and near(">Needs", "breakeven", 300)
          and near(">Sample", "sample", 300) and near(">Typical hold", "hold", 300)
          and near("95% range", "range", 300))
    check("and under the tables, with every column",
          "What the columns and words mean" in page and "What the columns mean" in page
          and "<dt>RSI</dt>" in page and "<dt>Weight given</dt>" in page)
    check("in the person's own numbers", "It is 2 ATR from the entry" in page
          and "target 3 ATR away" in page)

    class Nesting(HTMLParser):
        """Where each help sits. A details inside a p is torn out of it by
        the browser, which ends the paragraph there; inside a span or b it is
        not valid HTML."""

        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag == "details" and any(t in ("p", "span", "b", "a", "label", "button")
                                        for t, _ in self.stack):
                self.bad.append([t for t, _ in self.stack][-3:])
            if tag not in ("br", "img", "input", "meta", "link", "hr", "circle",
                           "path", "rect", "line", "polyline", "polygon"):
                self.stack.append((tag, attrs))

        def handle_endtag(self, tag):
            for k in range(len(self.stack) - 1, -1, -1):
                if self.stack[k][0] == tag:
                    del self.stack[k:]
                    break

    nest = Nesting()
    nest.feed(page)
    check("never inside a paragraph or a phrase, where browsers break it",
          not nest.bad, nest.bad[:3])
    check("a news-less analysis says so in words, not 'Scored by skipped'",
          "The news was not read for this analysis." in flat(page)
          and "Scored by skipped" not in page)

    head("WHAT THE PAGE SAYS ABOUT THE NEWS WITHOUT A CLAUDE KEY")
    sentiment_mod.gather_news = lambda symbol, name="": [sentiment_mod.Item(
        text="Test Corp beats estimates", kind="news", source="Test", age_hours=30.0)]
    sentiment_mod.gather_social = lambda symbol: []
    web._CACHE.clear()
    page = c.get("/analyse?symbol=TEST&interval=1d").data.decode("utf-8", "replace")
    worth = page[page.find("Worth knowing"):page.find("Worth knowing") + 2000]
    check("it says the reading is rough, in plain words",
          "Headlines were scored with a list of finance words" in flat(worth), worth[:600])
    check("never the name of a setting or a key",
          not re.search(r"ANTHROPIC|API key|\.env|restart", page), re.findall(
              r".{40}(?:ANTHROPIC|API key|\.env|restart).{40}", page)[:2])
    check("and says how the news was scored", "Scored by a list of finance words." in page)

    head("AN ANALYSIS THAT BREAKS SAYS SO IN WORDS")

    def broken(symbol, interval):
        raise RuntimeError(r"C:\srv\orenth\bot\engine.py line 42: boom")

    engine.fetch_bars = broken
    web._CACHE.clear()
    web.app.logger.disabled = True
    # The research page's reasoning, which needs a Claude key, without one.
    real_cached, real_research = web.cached_analysis, web.engine.research
    stand_in = NS(symbol="ZZRES", name="Res Corp", last_price=101.0, interval="1d",
                  close=[101.0] * 3, timestamp=[1_700_000_000 + k * DAY for k in range(3)],
                  splits=[], meta={}, market_open=True, last_bar_time="",
                  asset_class="equity", currency="USD")
    no_key = reasoning_mod.Reasoning(False, reason_unavailable=(
        "No Anthropic API key is set, so the reasoning step is unavailable."))

    def research_page():
        web.cached_analysis = lambda *a, **k: NS(bars=stand_in)
        web.engine.research = lambda symbol, price, cfg, **k: web.engine.Research(
            symbol=symbol, price=price, reasoning=no_key)
        web._RESEARCH_CACHE.clear()
        try:
            return c.get("/research?symbol=ZZRES&interval=1d").data.decode("utf-8", "replace")
        finally:
            web.cached_analysis, web.engine.research = real_cached, real_research

    try:
        r = c.get("/analyse?symbol=ZZFAIL&interval=1d&news=0")
        page = r.data.decode("utf-8", "replace")
        check("whoever runs it on their own machine is shown what broke, to fix it",
              r.status_code == 500 and "Something broke while analysing ZZFAIL" in page
              and "boom" in page, page[-1500:])
        check("with no advice about tickers that do not exist",
              "the ticker does not exist" not in page)
        page = research_page()
        check("on their own machine, the research page says why there is no reasoning",
              "What the evidence supports" in page and "No Anthropic API key" in page)
        _os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
        _os.environ["GOOGLE_CLIENT_SECRET"] = "x"
        conn = db.connect(DB)
        reader = A.upsert_email_user(conn, "reader@example.com")
        token = A.start_session(conn, reader.id, "test")
        conn.close()
        with c.session_transaction() as sess:
            sess[web.SESSION_KEY] = token
        r = c.get("/analyse?symbol=ZZFAIL&interval=1d&news=0")
        page = r.data.decode("utf-8", "replace")
        check("anyone else gets the sentence, never the exception's text",
              r.status_code == 500 and "Something broke while analysing ZZFAIL" in page
              and "boom" not in page and "engine.py" not in page, page[-1500:])
        page = research_page()
        check("and the research page shows them no section about a missing key",
              "ZZRES" in page and "What the evidence supports" not in page
              and "Anthropic" not in page and "API key" not in page)
    finally:
        web.app.logger.disabled = False
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            _os.environ.pop(key, None)
finally:
    engine.resolve, engine.fetch_bars = real["resolve"], real["bars"]
    catalysts.fetch = real["calendar"]
    sentiment_mod.gather_news, sentiment_mod.gather_social = real["news"], real["social"]

head("BIG ENOUGH FOR A FINGER, QUIET ON A DESK")
css = open(_os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
coarse = css[css.index("@media (pointer: coarse) {\n  .help"):]
coarse = coarse[:coarse.index("}\n}") + 3]
check("on a touch screen each toggle is at least 44 pixels each way",
      "min-width: 44px" in coarse and "min-height: 44px" in coarse, coarse)
check("with a mouse it is an 18-pixel ring",
      re.search(r"\.help \.q \{[^}]*width: 18px; height: 18px", css) is not None)
check("and shows no browser triangle", "list-style: none" in css[css.index(".help > summary"):]
      and ".help > summary::-webkit-details-marker { display: none; }" in css)

head("A HELP WITH NO WORDS DRAWS NOTHING")
env = web.app.jinja_env
with web.app.test_request_context("/"):
    one = env.from_string('{% from "_help.html" import help with context %}[{{ help("nope") }}]')
    check("an unknown key draws nothing rather than breaking the page",
          one.render(help_text=filled) == "[]" and one.render(help_text={}) == "[]")
    two = env.from_string('{% from "_help.html" import help_list with context %}'
                          '[{{ help_list(["nope"], "x") }}]')
    check("nor does a list of them", two.render(help_text=filled) == "[]")

print()
print("%d failure(s)" % len(fails))
print("HELP OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
