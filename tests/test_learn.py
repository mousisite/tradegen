"""The Learn pages: the course's ideas in plain words, beside what was measured.

/learn and every /learn/<slug> are fetched as a visitor on a site that asks
people to sign in, first with nothing measured (each says "Being tested" and
asks search engines to wait), then with the Setup lab's own fixtures written
to the isolated database (each shows the counts, margins, random entries and
verdict words those fixtures hold, and nothing typed in). Also: an unknown
topic is a 404; no page shows None, nan, undefined or the jargon the plain
words forbid; the video's link is there on the web and gone inside the
iPhone app; the sitemap lists only measured topics; a strategy's test page
shows the lab box only when the lab has measured it; and the practice page
keeps its tally in the session only. No network.
"""
import html as _html
import os as _os
import re
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402

_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"

import web  # noqa: E402
from bot import database as db  # noqa: E402
from bot import labstore, learn, setups  # noqa: E402

c, DB = harness.isolated_web("learn")
web.app.config["AUTH_MODE"] = None

fails = []
JARGON = ("expectancy", "r-multiple", "confidence interval", "atr", "wilson",
          "basis point", "sharpe", "drawdown")
# Never said on these pages: hype, or who traded (price bars cannot show it).
HYPE = ("guaranteed", "high probability", "smart money", "manipulat", "every single day")
APP_UA = {"User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 OrenthApp/1.0"}


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


def get(path, **kw):
    r = c.get(path, **kw)
    return r.status_code, r.data.decode("utf-8", "replace")


def main_text(page):
    """What a person reads in the page's main part: tags, styles and scripts out."""
    body = page[page.find("<main"):page.find("</main>")]
    body = re.sub(r"<(style|script|svg)\b.*?</\1>", " ", body, flags=re.S)
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def leaks(page):
    text = main_text(page)
    found = [w for w in ("None", "nan", "undefined", "NaN") if re.search(r"\b%s\b" % w, text)]
    found += [w for w in JARGON + HYPE if w in text.lower()]
    return found


SLUGS = [t.slug for t in setups.TOPICS]

try:
    db.migrate(DB)

    head("THE COPY")
    check("every topic has its words", set(learn.CONCEPTS) == set(SLUGS),
          set(SLUGS) ^ set(learn.CONCEPTS))
    bad = []
    for slug, copy in learn.CONCEPTS.items():
        if not copy.get("what") or not copy.get("course") or not copy.get("rule"):
            bad.append(slug)
        for stamp, _ in copy["course"]:
            for part in stamp.split("-"):
                try:
                    if not 0 <= setups.seconds(part) <= setups.seconds("6:40:00"):
                        bad.append((slug, stamp))
                except ValueError:
                    bad.append((slug, stamp))
        if any(src not in ("course", "app") for src, _ in copy["rule"]):
            bad.append((slug, "rule source"))
    check("each says what it is, what the course teaches and the exact rule, "
          "with timestamps inside the video", not bad, bad)
    words = []
    for copy in learn.CONCEPTS.values():
        words += [copy["what"]] + [t for _, t in copy["course"]] + \
                 [t for _, t in copy["rule"]] + list(copy.get("limits") or ())
    words += list(learn.METHOD) + list(learn.LIMITS) + [learn.SOURCE]
    found = [w for w in JARGON + HYPE for t in words if w in t.lower()]
    check("in plain words, with no jargon or hype", not found, found)
    # The method's own example ("+0.10R means 10 cents...") explains R; the
    # copy of the ideas must state no result.
    check("and no result typed in: the copy of the ideas states no R figure",
          not [t for copy in learn.CONCEPTS.values()
               for t in [copy["what"]] + [x for _, x in copy["course"]]
               + [x for _, x in copy["rule"]] + list(copy.get("limits") or ())
               if re.search(r"[+-]?\d+\.\d+R\b", t)])
    check("the rules mark the app's own choices",
          sum(1 for copy in learn.CONCEPTS.values()
              for src, _ in copy["rule"] if src == "app") >= 15)

    head("NOTHING MEASURED YET")
    status, page = get("/learn")
    check("the hub opens to a visitor", status == 200, status)
    check("and says it is being tested", "Being tested. This fills in after the first "
          "nightly run." in page)
    check("asking search engines to wait", 'name="robots" content="noindex"' in page)
    check("it keeps builder text and jargon out", not leaks(page), leaks(page))
    empty_ok = []
    for slug in SLUGS:
        status, page = get("/learn/" + slug)
        if not (status == 200 and "Being tested" in page and "noindex" in page
                and not leaks(page)):
            empty_ok.append((slug, status, leaks(page)))
    check("every topic opens, says it is being tested, and waits for search engines",
          not empty_ok, empty_ok)
    check("an unknown topic has no page", get("/learn/order-blocks")[0] == 404)
    status, page = get("/learn/practice")
    check("practice says it has no charts yet", status == 200 and "No charts yet" in page
          and "noindex" in page, status)
    sitemap = get("/sitemap.xml")[1]
    check("the sitemap leaves the Learn pages out until measured", "/learn" not in sitemap)
    page = get("/backtests")[1]
    check("the footer links to the Learn pages", 'href="/learn"' in page)

    head("WITH THE LAB'S FIXTURES")
    conn = db.connect(DB)
    labstore.save_result(conn, labstore.sample_result("ES=F", "5m"))
    labstore.save_result(conn, labstore.sample_result("SPY", "15m"))
    for cid in ("london_takes_asia", "pd_sweep_reverse", "sweep_alone_loses"):
        labstore.save_claim(conn, labstore.sample_claim(cid))
    labstore.save_bias(conn, labstore.POOL_ALL, labstore.sample_bias())
    conn.close()

    # The fixture's 5-minute cell: 41 trades, -3.2R in all, a standard error
    # of 0.14, its random entries -4.0R over 41.
    status, page = get("/learn")
    text = main_text(page)
    check("the hub opens, open to search engines", status == 200 and "noindex" not in page,
          status)
    check("its short answer counts the setups by verdict",
          "0 of the 1 setup measured kept money by more than its margin of error, 0 lost "
          "money by more than its margin, and 1 cannot be told apart from luck" in text,
          text[:600])
    check("with the trades, what a trade kept and its margin",
          "41" in text and "-0.08R ±0.28" in text)
    check("beside random entries", "-0.10R" in text and "Random entries" in text)
    check("the claims carry their counts and the usual rate",
          "54 of 120" in text and "50 of 120" in text
          and "cannot be told apart from what usually happens" in text.lower())
    check("unmeasured claims say they are being tested", "Being tested" in text)
    check("how many results got a verdict, and that 1 in 20 can by luck",
          "1 in 20" in text)
    check("it keeps builder text and jargon out", not leaks(page), leaks(page))
    check("it credits the video once, linked on the web",
          page.count("youtu.be/yiuFUp0kFz8") == 1 and "Orenth is not connected" in page)
    page_app = get("/learn", headers=APP_UA)[1]
    check("and never links out of the iPhone app",
          "youtu" not in page_app and "Orenth is not connected" in page_app)

    status, page = get("/learn/sweep-break-retest")
    text = main_text(page)
    check("the main model's topic opens, open to search engines",
          status == 200 and "noindex" not in page, status)
    check("it says what it is and what the course teaches, with times",
          "four steps" in text and "6:24:12-6:30:28" in text)
    check("the exact rule, the app's choices marked", "Orenth's choice" in text
          and "As taught" in text)
    check("the result pooled per bar size and target, with random entries",
          "-0.08R ±0.28" in text and "-0.10R" in text and "15-minute" in text
          and "next old high or low" in text)
    check("every instrument is there", "S&P 500 futures" in text and "S&P 500 fund" in text)
    check("the wider-swing check is said", "three bars each side" in text and "30 trades" in text)
    check("the skipped setups are counted", "6 with a stop too close" in text)
    check("an example is drawn from the stored bars, in New York time",
          'class="chart trade"' in page and "Entry" in page and "Target" in page
          and "Times are New York time" in text)
    check("its claims are there with their verdicts", "54 of 120" in text)
    check("it keeps builder text and jargon out", not leaks(page), leaks(page))
    check("the video's link is there on the web", "youtu.be/yiuFUp0kFz8" in page)
    check("and gone in the app", "youtu" not in get("/learn/sweep-break-retest",
                                                    headers=APP_UA)[1])

    status, page = get("/learn/daily-bias")
    text = main_text(page)
    check("the daily lean shows its record beside always guessing up",
          status == 200 and "101 of 190" in text and "Always guessing up" in text
          and "52%" in text, text[:400])
    status, page = get("/learn/premium-discount")
    check("a topic the lab has not measured still says so", status == 200
          and "Being tested" in page and "noindex" in page)

    every = []
    for slug in SLUGS:
        status, page = get("/learn/" + slug)
        if status != 200 or leaks(page):
            every.append((slug, status, leaks(page)))
    check("every topic opens with nothing leaking", not every, every)

    sitemap = get("/sitemap.xml")[1]
    listed = set(re.findall(r"/learn/([a-z-]+)</loc>", sitemap))
    check("the sitemap lists the hub and the measured topics",
          "/learn</loc>" in sitemap and {"sweep-break-retest", "sessions",
                                         "previous-day-levels", "liquidity-sweep",
                                         "daily-bias"} <= listed, sorted(listed))
    check("and not the ones with nothing measured",
          not listed & {"premium-discount", "smt-divergence", "fair-value-gap"},
          sorted(listed))

    # Every link a visitor can follow from the Learn pages answers.
    targets = set()
    for path in ["/learn"] + ["/learn/" + s for s in SLUGS]:
        for href in re.findall(r'href="(/[^"#]*)', get(path)[1]):
            targets.add(_html.unescape(href))
    broken = [(t, get(t)[0]) for t in sorted(targets)
              if not t.startswith(("/static/", "/auth/")) and get(t)[0] >= 400]
    check("all %d links from them answer" % len(targets), not broken, broken[:8])

    learn_words = " ".join(get("/learn")[1].split())
    check("the hub says ten of the setups have a say in the calls, and which one has not",
          "Ten of these setups also have a say in Orenth's Buy, Wait or Avoid calls" in learn_words
          and "do not change Orenth" not in learn_words)

    head("THE LAB BOX ON A STRATEGY'S TEST PAGE")
    status, page = get("/backtest/liquidity-model")
    check("the sweep, break and retest test page shows the lab's result",
          status == 200 and "Inside the day, with the course" in page
          and "-0.08R" in page and "/learn/sweep-break-retest" in page, status)
    check("before the house method", page.find("Inside the day") < page.find("How we tested"))
    # The house method below it names ATR on purpose; only blanks are checked here.
    blanks = [w for w in leaks(page) if w in ("None", "nan", "undefined", "NaN")]
    check("with no blank printed as a word", not blanks, blanks)
    check("a setup the lab has not measured gets no box",
          web._lab_box_data("fvg_retest") is None)
    check("nor does a strategy that is not a setup",
          web._lab_box_data("pin_bar") is None
          and "Inside the day" not in get("/backtest/pin-bar")[1])

    head("PRACTICE: CALL THE NEXT MOVE")
    item = {"cut": 10, "h": [11.0] * 30, "l": [9.0] * 30,
            "c": [10.0] * 11 + [10.0] * 11 + [12.0] + [10.0] * 7}
    check("up when price closed higher by more than half a usual range",
          learn.practice_answer(item) == "up")
    item["c"][22] = 9.5
    check("unclear when it hardly moved", learn.practice_answer(item) == "unclear")
    item["c"][22] = 8.0
    check("down when it fell", learn.practice_answer(item) == "down")

    conn = db.connect(DB)
    items = learn.practice_items(conn)
    conn.close()
    check("the stored examples become charts", len(items) == 2, len(items))
    status, page = get("/learn/practice")
    check("a chart and three choices", status == 200 and 'class="chart trade"' in page
          and 'value="up"' in page and 'value="unclear"' in page, status)
    check("the instrument is not named before the answer", "S&P 500" not in main_text(page))
    r = c.post("/learn/practice", data={"item": 0, "pick": "up"},
               headers={"Sec-Fetch-Site": "same-origin"})
    check("a pick goes back to a page that can be reloaded", r.status_code == 303
          and "shown=0" in r.headers.get("Location", ""), r.status_code)
    text = main_text(get(r.headers["Location"])[1])
    answer = learn.practice_answer(items[0])
    check("the answer is shown, with the instrument",
          ("Right." if answer == "up" else "Not this time.") in text and "S&P 500" in text)
    check("and counted", "right of 1" in text)
    c.post("/learn/practice", data={"item": 0, "pick": "down"},
           headers={"Sec-Fetch-Site": "same-origin"})
    check("the same chart again is not counted twice",
          "right of 1" in main_text(get("/learn/practice?shown=0")[1]))
    r = c.post("/learn/practice", data={"item": 1, "pick": "up"},
               headers={"Sec-Fetch-Site": "cross-site"})
    check("a pick sent from another site is refused", r.status_code == 403, r.status_code)
    check("a pick that is not a choice is refused",
          c.post("/learn/practice", data={"item": 0, "pick": "sideways"},
                 headers={"Sec-Fetch-Site": "same-origin"}).status_code == 400)
    with c.session_transaction() as sess:
        kept = dict(sess.get("practice_tally") or {})
    check("the tally lives in the session", kept.get("n") == 1, kept)
    c.post("/learn/practice", data={"reset": "1"}, headers={"Sec-Fetch-Site": "same-origin"})
    with c.session_transaction() as sess:
        check("and Start over clears it", "practice_tally" not in sess)
    conn = db.connect(DB)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    check("nothing about practice is written to the database",
          not [t for t in tables if "practice" in t], sorted(tables))
    page = get("/learn/practice")[1]
    check("it keeps builder text and jargon out", not leaks(page), leaks(page))
finally:
    pass

print()
if fails:
    print("%d FAILED" % len(fails))
    _sys.exit(1)
print("ALL PASSED")
