"""The news lexicon, and what the news and post sources are asked.

No network: requests.get is a stand-in that records each address asked.
"""
import sys
import time
import types
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot import sentiment, upstream
from bot.sentiment import lexicon_score

cases = [
    ("Tesla beats earnings but cuts full-year guidance", "neg"),
    ("Company is not strong and misses estimates", "neg"),
    ("Nvidia surges on record data center growth", "pos"),
    ("SEC opens investigation into company accounting", "neg"),
    ("Stock closed unchanged on light volume", "flat"),
    ("Shares rise despite weak guidance", "pos"),
    ("Not a strong quarter", "neg"),
    ("Analyst upgrades stock to outperform, raises target", "pos"),
    ("Revenue misses but company raises guidance and announces buyback", "pos"),
    ("Bankruptcy fears mount as lawsuit expands", "neg"),
]
ok = True
for text, want in cases:
    s = lexicon_score(text)
    got = "pos" if s > 0.10 else ("neg" if s < -0.10 else "flat")
    mark = "OK " if got == want else "BAD"
    if got != want:
        ok = False
    print("%s %+.3f want=%-4s got=%-4s | %s" % (mark, s, want, got, text))


def check(name, good, detail=""):
    global ok
    print("   %s %s%s" % ("OK  " if good else "FAIL", name,
                          ("  " + str(detail)) if detail and not good else ""))
    if not good:
        ok = False


class Reply:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content, self.headers = status, body, content, {}

    def json(self):
        return self._body


real_get, real_time = sentiment.requests.get, sentiment.time
asked = []


def answering(stocktwits, delay=0.0):
    def get(url, params=None, headers=None, timeout=None):
        asked.append((url, dict(params or {})))
        if delay:
            time.sleep(delay)
        if "stocktwits" in url:
            if isinstance(stocktwits, Exception):
                raise stocktwits
            return stocktwits
        return Reply(200, {"news": []})
    del asked[:]
    upstream.reset()
    sentiment.requests.get = get


def posts_asked():
    return [u.rsplit("/", 1)[-1] for u, _ in asked if "stocktwits" in u]


def post(body, created="2026-01-15T12:00:00Z"):
    return {"body": body, "created_at": created, "symbols": []}


def social(symbol):
    try:
        return sentiment.gather_social(symbol)
    except sentiment.SocialUnavailable:
        return "unavailable"


try:
    print()
    print("COINS ARE ASKED FOR AS COINS")
    # SUI20947.X is nothing, and LINK is Interlink Electronics: its posts were
    # scored as the coin's whenever the coin's own stream was empty.
    for symbol, stream in (("SUI20947-USD", "SUI.X.json"), ("LINK-USD", "LINK.X.json"),
                           ("AAPL", "AAPL.json")):
        answering(Reply(404))
        check("%s asks for %s, once and nothing else" % (symbol, stream),
              social(symbol) == [] and posts_asked() == [stream], posts_asked())
    answering(Reply(404))
    sentiment.gather_news("LINK-USD", "Chainlink USD")
    check("and the headlines by the coin's own id, not a company's ticker",
          [p["q"] for u, p in asked if u == sentiment._YAHOO_SEARCH] == ["LINK-USD"], asked)

    print()
    print("A REFUSAL IS NOT A QUIET SYMBOL, AND IS ASKED ONCE")
    for what, answer in (("a refusal", Reply(403)), ("a rate limit", Reply(429)),
                         ("no answer", sentiment.requests.Timeout("slow"))):
        answering(answer)
        check("%s is said to be one, after a single request" % what,
              social("AAPL") == "unavailable" and len(posts_asked()) == 1, posts_asked())
    answering(Reply(403))
    read = sentiment.analyse("AAPL", "Apple", use_llm=False)
    check("an analysis says the posts were not read",
          read.social_note == "StockTwits did not answer, so posts were not read.",
          read.social_note)
    answering(Reply(200, {"messages": []}))
    quiet = sentiment.analyse("AAPL", "Apple", use_llm=False)
    check("a quiet symbol says there were none",
          quiet.social_note == "No recent posts about this symbol on StockTwits.")
    check("and neither is one of the plan's risks",
          not any("StockTwits" in n for n in read.notes + quiet.notes), read.notes)
    answering(Reply(200, {"messages": [post("going up")]}))
    check("with posts there is nothing to say",
          sentiment.analyse("AAPL", "Apple", use_llm=False).social_note == "")

    print()
    print("THE SOURCES ARE READ SIDE BY SIDE, UNDER ONE LIMIT")
    answering(Reply(200, {"messages": [post("going up")]}), delay=0.6)
    began = time.time()
    sentiment.analyse("AAPL", "Apple", use_llm=False)
    check("three slow sources take as long as one", time.time() - began < 1.5,
          "%.1fs" % (time.time() - began))
    real_wait, sentiment.SOURCES_WAIT = sentiment.SOURCES_WAIT, 0.3
    try:
        answering(Reply(200, {"messages": [post("going up")]}), delay=1.0)
        began = time.time()
        late = sentiment.analyse("AAPL", "Apple", use_llm=False)
        check("and a page waits no longer than the limit for all of them",
              time.time() - began < 0.8 and late.n_social == 0
              and "did not answer" in late.social_note, "%.1fs" % (time.time() - began))
        time.sleep(1.0)                    # the stragglers finish
    finally:
        sentiment.SOURCES_WAIT = real_wait

    print()
    print("AGES ARE IN UTC, WHATEVER THE MACHINE'S CLOCK")
    # On a machine eight hours behind UTC that also keeps summer time, every
    # age was an hour out.
    import calendar
    then = time.strptime("2026-01-15T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ")
    now = calendar.timegm(then) + 2 * 3600
    sentiment.time = types.SimpleNamespace(
        time=lambda: now, strptime=time.strptime, monotonic=time.monotonic,
        mktime=lambda s: calendar.timegm(s) + 28800, timezone=28800, altzone=25200, daylight=1)
    check("a headline two hours old is two hours old",
          abs(sentiment._age_hours(then) - 2.0) < 1e-6, sentiment._age_hours(then))
    answering(Reply(200, {"messages": [post("going up")]}))
    check("and so is a post", abs(social("AAPL")[0].age_hours - 2.0) < 1e-6)
finally:
    sentiment.requests.get, sentiment.time = real_get, real_time
    upstream.reset()

print()
print("AN ANSWER FROM CLAUDE THAT STOPS SHORT")
# The items past the cut were left neutral while the page said Claude had
# scored them.
replies = {}


class FakeAnthropic:
    def __init__(self, **_):
        self.messages = types.SimpleNamespace(create=lambda **kw: types.SimpleNamespace(
            stop_reason=replies["stop"],
            content=[types.SimpleNamespace(type="text", text=replies["text"])]))


real_module = _sys.modules.get("anthropic")
real_key = _os.environ.get("ANTHROPIC_API_KEY")
_sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeAnthropic)
_os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
try:
    def headlines():
        return [sentiment.Item("Bankruptcy fears mount after fraud", "x", "news", 1.0)
                for _ in range(6)]

    items = headlines()
    replies.update(stop="max_tokens", text="1: 0.5\n2: -0.4\n3: 0.2\n4: -0")
    scored = sentiment._score_with_claude(items, "AAPL")
    check("the scores it finished are used", scored == 3
          and [i.score for i in items[:3]] == [0.5, -0.4, 0.2], [i.score for i in items])
    check("and the rest, with the line it broke off in, are read by the word list",
          all(i.score < -0.3 for i in items[3:]), [i.score for i in items[3:]])
    items = headlines()
    replies.update(stop="refusal", text="1: 0.5\n2: 0.5\n3: 0.5")
    check("a refusal scores nothing", sentiment._score_with_claude(items, "AAPL") == 0
          and all(i.score == 0.0 for i in items))
    replies.update(stop="end_turn", text="1: 0.1")
    check("nor does an answer for too few of them",
          sentiment._score_with_claude(headlines(), "AAPL") == 0)
    real_news, real_social = sentiment.gather_news, sentiment.gather_social
    sentiment.gather_news = lambda symbol, name="": headlines()
    sentiment.gather_social = lambda symbol: []
    try:
        replies.update(stop="max_tokens", text="1: 0.5\n2: -0.4\n3: 0.2\n4: -0")
        part = sentiment.analyse("AAPL").method
        replies.update(stop="end_turn", text="\n".join("%d: 0.1" % k for k in range(1, 7)))
        whole = sentiment.analyse("AAPL").method
    finally:
        sentiment.gather_news, sentiment.gather_social = real_news, real_social
    check("the page says so when the word list did part of it",
          "Claude" in part and "list of finance words" in part
          and "Claude" in whole and "finance words" not in whole, (part, whole))
    # Each asking is paid for, and the analysis above it is kept per person's
    # settings: a different account size in each address asked again each time.
    replies.update(stop="end_turn", text="\n".join("%d: 0.9" % k for k in range(1, 7)))
    items = headlines()
    check("the same headlines are not paid for twice",
          sentiment._score_with_claude(items, "AAPL") == 6
          and [i.score for i in items] == [0.1] * 6, [i.score for i in items])
    items = headlines() + [sentiment.Item("A new one", "x", "news", 1.0)]
    replies.update(text="\n".join("%d: 0.9" % k for k in range(1, 8)))
    check("but a new one among them is read", sentiment._score_with_claude(items, "AAPL")
          == 7 and items[0].score == 0.9, [i.score for i in items])
finally:
    if real_module is None:
        _sys.modules.pop("anthropic", None)
    else:
        _sys.modules["anthropic"] = real_module
    if real_key is None:
        _os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        _os.environ["ANTHROPIC_API_KEY"] = real_key

print("\nALL PASS" if ok else "\nSOME FAILED")


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
