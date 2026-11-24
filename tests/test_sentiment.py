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
    check("and the headlines by the coin's own id, not a company's ticker, from the "
          "feed that has coins",
          [p for u, p in asked if "yahoo" in u]
          == [{"s": "LINK-USD", "region": "US", "lang": "en-US"}], asked)
    def asked_of_google(symbol, name):
        answering(Reply(404))
        sentiment.gather_news(symbol, name)
        return [sentiment.requests.utils.unquote(u.split("?q=")[1].split("&")[0])
                for u, _ in asked if "google" in u]

    # "Optimism" beside "stock" is Intel's headlines, and any other company's.
    for symbol, name, words in (
            ("LINK-USD", "Chainlink USD", '"Chainlink" LINK crypto'),
            ("OP-USD", "Optimism USD", '"Optimism" OP crypto'),
            ("USDC-USD", "USD Coin USD", '"USD Coin" USDC crypto'),
            ("SUI20947-USD", "Sui USD", '"Sui" SUI crypto'),
            # The search was down, so there is no name.
            ("SUI20947-USD", "SUI20947-USD", '"SUI" SUI crypto'),
            ("GRAM-USD", "", '"TON" TON crypto')):
        check("%s, %r: by the name headlines use and its ticker, and nothing about stocks"
              % (symbol, name), asked_of_google(symbol, name) == [words],
              asked_of_google(symbol, name))
    answering(Reply(404))
    sentiment.gather_news("AAPL", "Apple Inc.")
    check("a company is asked for as it was",
          [p["q"] for u, p in asked if u == sentiment._YAHOO_SEARCH] == ["AAPL"]
          and asked_of_google("AAPL", "Apple Inc.") == ['"Apple Inc." stock OR crypto']
          and asked_of_google("AAPL", "AAPL") == ['"AAPL" stock OR crypto'], asked)

    print()
    print("A COIN'S HEADLINES, AND WHERE THEY CAME FROM")

    def stamp(hours):
        return time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                             time.gmtime(time.time() - hours * 3600))

    feed = ("<rss version='2.0'><channel><title>x</title>%s</channel></rss>" % "".join(
        "<item><title>%s</title><link>https://news.example/%d</link><pubDate>%s</pubDate></item>"
        % (t, k, stamp(h)) for k, (t, h) in enumerate((
            ("Bitcoin surges to a record", 2), ("Bitcoin ETFs see strong growth", 30),
            ("A headline from three weeks ago", 500))))).encode()

    def from_the_feed(google):
        def get(url, params=None, headers=None, timeout=None):
            if "stocktwits" in url:
                return Reply(200, {"messages": []})
            if url == sentiment._YAHOO_FEED:
                return Reply(200, content=feed)
            return google
        upstream.reset()
        sentiment._SAID.clear()
        sentiment.requests.get = get
        said = []
        was, sentiment.log.warning = sentiment.log.warning, lambda m, *a: said.append(m % a)
        try:
            return sentiment.analyse("BTC-USD", "Bitcoin USD", use_llm=False), said, \
                sentiment.analyse("BTC-USD", "Bitcoin USD", use_llm=False)
        finally:
            sentiment.log.warning = was

    read, said, _again = from_the_feed(Reply(429))
    check("the feed's are read, all but the ones too old to matter",
          read.n_news == 2 and read.news_score > 0.3 and not any(
              "three weeks" in i.text for i in read.items),
          [(i.text, i.age_hours) for i in read.items])
    check("only the source that gave something is named", read.read_from == ["Yahoo Finance"],
          read.read_from)
    check("and the log says, once, that the other refused",
          len(said) == 1 and "Google News" in said[0] and "429" in said[0], said)
    read, said, _again = from_the_feed(Reply(200, content=b"<html>Before you continue</html>"))
    check("a page in place of its feed is said too", len(said) == 1, said)

    def yahoo_said(reply, symbol="BTC-USD"):
        upstream.reset()
        sentiment._SAID.clear()
        sentiment.requests.get = lambda url, **k: reply
        said = []
        was, sentiment.log.warning = sentiment.log.warning, lambda m, *a: said.append(m % a)
        try:
            return len(sentiment._yahoo_news(symbol)), said
        finally:
            sentiment.log.warning = was

    for what, reply in (("a page in place of it", Reply(200, content=b"<html>Back soon</html>")),
                        ("a rate limit", Reply(429)), ("an error", Reply(500))):
        got, said = yahoo_said(reply)
        check("so is %s, from Yahoo's" % what,
              got == 0 and len(said) == 1 and said[0].startswith("Yahoo Finance gave no"),
              (got, said))
    check("but not a feed with nothing in it", yahoo_said(Reply(200, content=(
        b"<rss version='2.0'><channel><title>x</title></channel></rss>"))) == (0, []))
    check("nor a company with no news", yahoo_said(Reply(200, {"news": []}), "AAPL") == (0, []))
    # Each headline is paid for where the model reads them. Yahoo is asked for
    # fifteen of a company's, and its feed gives as many as it has.
    many = ("<rss version='2.0'><channel><title>x</title>%s</channel></rss>" % "".join(
        "<item><title>Story %s</title><pubDate>%s</pubDate></item>" % ("abcdefghij"[k % 10] * (
            k // 10 + 1), stamp(k)) for k in range(40))).encode()
    check("no more of a coin's are read than of a company's",
          yahoo_said(Reply(200, content=many)) == (15, []),
          yahoo_said(Reply(200, content=many)))
    check("and they are the newest",
          max(i.age_hours for i in sentiment._yahoo_news("BTC-USD")) < 15)

    # The feed is on a host of its own. Three refusals there stopped everybody's
    # prices for as long as it said, up to a quarter of an hour.
    tried = []

    def feed_refuses(url, **k):
        tried.append(url)
        refusal = Reply(429)
        refusal.headers = {"Retry-After": "900"}
        return refusal if url == sentiment._YAHOO_FEED else Reply(200, {"news": []})

    def refused(host):
        try:
            upstream.check(host)
        except upstream.Throttled:
            return True
        return False

    upstream.reset()
    sentiment.requests.get = feed_refuses
    for symbol in ("BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD"):
        sentiment._yahoo_news(symbol)
    check("a coin's headlines refused are left alone, and prices are still asked for",
          tried == [sentiment._YAHOO_FEED] * 3 and not refused(sentiment._YAHOO_HOST),
          (tried, refused(sentiment._YAHOO_HOST)))
    upstream.reset()
    sentiment.requests.get = lambda url, **k: Reply(429)
    for _ in range(3):
        sentiment._yahoo_news("AAPL")
    check("a company's, from the host prices come from, still stop both",
          refused(sentiment._YAHOO_HOST))
    sentiment.requests.get = lambda url, **k: Reply(200, content=many)
    upstream._breakers[sentiment._YAHOO_HOST]["open_until"] = 0.0
    sentiment._yahoo_news("BTC-USD")
    check("and a coin's answering says nothing about whether prices are back",
          upstream._breakers[sentiment._YAHOO_HOST]["tripped"])

    # Yahoo's feed goes by the letters. What it really gave for Dai, the dollar
    # coin, was a Japanese builder's buyback, which read as bullish; the rupee
    # was in every coin's.
    def from_yahoo(symbol, name, *titles):
        page = ("<rss version='2.0'><channel><title>x</title>%s</channel></rss>" % "".join(
            "<item><title>%s</title><pubDate>%s</pubDate></item>" % (t, stamp(k + 1))
            for k, t in enumerate(titles))).encode()
        upstream.reset()
        sentiment.requests.get = lambda url, **k: (
            Reply(200, content=page) if url == sentiment._YAHOO_FEED else
            Reply(200, {"news": [{"title": t} for t in titles]})
            if url == sentiment._YAHOO_SEARCH else Reply(404))
        return [i.text for i in sentiment.gather_news(symbol, name)]

    others = ("Is Dai-Dan (TSE:1980) Still Cheap After Its Buyback Plan?",
              "Rupee under pressure", "Sector Update: Financial Stocks Higher")
    check("a headline that does not name the coin is not its news",
          from_yahoo("DAI-USD", "Dai USD", "Better Buy: Tether or Dai?", *others)
          == ["Better Buy: Tether or Dai?"],
          from_yahoo("DAI-USD", "Dai USD", "Better Buy: Tether or Dai?", *others))
    check("one that gives its ticker, or its name in any letters, is",
          from_yahoo("BTC-USD", "Bitcoin USD", "BITCOIN falls", "What bitcoin's fall means",
                     "Is $BTC cheap?", "These (BTC) miners rose", "BTCS sells its ether")
          == ["BITCOIN falls", "What bitcoin's fall means", "Is $BTC cheap?",
              "These (BTC) miners rose"])
    check("Ethereum's go by its coin's own name too, and TRON's by either way of writing it",
          from_yahoo("ETH-USD", "Ethereum USD", "$72B in Ether sits exposed", "ether rises",
                     "Ripple News: XRP falls", "Is ethereum cheap?")
          == ["$72B in Ether sits exposed", "ether rises", "Is ethereum cheap?"]
          and from_yahoo("TRX-USD", "TRON USD", "Tron joins a task force", "Solana to rebound")
          == ["Tron joins a task force"])
    check("but not a word that only has the ticker's letters, or the name's in small ones",
          from_yahoo("NEAR-USD", "NEAR Protocol USD", "Gold near a record",
                     "NEAR gains 5%") == ["NEAR gains 5%"]
          and from_yahoo("OP-USD", "Optimism USD", "Stocks rise on AI optimism",
                         "Optimism pays out") == ["Optimism pays out"])
    check("and a company's are as Yahoo gives them",
          from_yahoo("AAPL", "Apple Inc.", *others) == list(others))
    read, said, _again = from_the_feed(Reply(200, content=feed.replace(
        b"<title>Bitcoin surges to a record", b"<title>Miners raise money as it rises")))
    check("a source that answered is named and nothing is logged",
          read.read_from == ["Yahoo Finance", "Google News"] and not said,
          (read.read_from, said))
    check("the warning names headlines when there were only those",
          any(n.startswith("Headlines were scored") for n in read.notes), read.notes)
    # A stablecoin's price pages of years ago were nineteen headlines about it.
    read, said, _again = from_the_feed(Reply(200, content=feed.replace(
        b"<title>A headline from", b"<title>Another from").replace(
        b"<title>Bitcoin", b"<title>Coins")))
    check("nor are Google's that old, for a coin",
          read.n_news == 4 and not any(i.age_hours > sentiment.FEED_HOURS for i in read.items),
          [(i.text, i.age_hours) for i in read.items])
    answering(Reply(404))
    sentiment.requests.get = lambda url, **k: Reply(200, {"news": []}, content=feed)
    check("a company's are kept as they were", any(
        i.age_hours > sentiment.FEED_HOURS for i in sentiment.gather_news("AAPL", "Apple Inc.")))
    answering(Reply(200, {"messages": [
        post("$BTC.X https://coins.example/currencies/bitcoin/"),
        post("$BTC.X going up https://coins.example/a"), post("$BTC.X")]}))
    texts = [i.text for i in sentiment.gather_social("BTC-USD")]
    check("a post that is a ticker and a link is left out, and no address is shown",
          texts == ["$BTC.X going up", "$BTC.X"], texts)
    # Only the address goes: not the end of "Awww", a post in another language,
    # the words after an address in one written without spaces, or a view.
    bullish = dict(post("$BTC.X https://coins.example/a"),
                   entities={"sentiment": {"basic": "Bullish"}})
    for said, left in ((post("Awww...poor bears"), "Awww...poor bears"),
                       (post("$BTC.X see www.coins.example/a now"), "$BTC.X see now"),
                       (post("Big news (https://coins.example/a) today"), "Big news today"),
                       (post("急騰 https://coins.example/a"), "急騰"),
                       (post("$BTC.X растёт https://coins.example/a"),
                        "$BTC.X растёт"),
                       (post("見てhttps://coins.example/a急騰"),
                        "見て 急騰"),
                       (post("$BTC.X \U0001F680\U0001F680 https://coins.example/a"),
                        "$BTC.X \U0001F680\U0001F680"),
                       (post("$BTC.X 100,000 https://coins.example/a"), "$BTC.X 100,000"),
                       (bullish, "$BTC.X"),
                       (post("$BTC.X - https://coins.example/a"), None),
                       (post("$BTC.X #btc @x https://coins.example/a"), None)):
        answering(Reply(200, {"messages": [said]}))
        got = [i.text for i in sentiment.gather_social("BTC-USD")]
        check("a post of %s is %s" % (ascii(said["body"]), ascii(left) if left else "left out"),
              got == ([left] if left else []), ascii(got))
    answering(Reply(200, {"messages": [post("$BTC.X going up"), post("$BTC.X")]}))
    only = sentiment.analyse("BTC-USD", "Bitcoin USD", use_llm=False)
    check("with no headlines the warning is about the posts",
          any(n.startswith("Posts were scored") for n in only.notes)
          and "No news retrieved for this symbol." in only.notes
          and only.read_from == ["StockTwits"], (only.notes, only.read_from))

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
