"""News and social sentiment.

Three free sources are combined:

* Google News RSS      - broad financial press coverage
* Yahoo Finance        - ticker-specific business headlines, from its search
                         or, for a coin, its headline feed
* StockTwits           - retail trader chatter, often with explicit
                         Bullish/Bearish tags from the poster

A note on "Twitter posts": the X/Twitter API no longer has a usable free
tier, so StockTwits stands in for it. StockTwits is where retail equity and
crypto traders actually post, and its self-tagged sentiment is more reliable
than inferring tone from free text. If you have paid X API access, add it as
another source in `gather_social`.

Scoring uses Claude when an API key is present, because financial language is
full of traps a bag-of-words model gets backwards ("misses on revenue but
raises guidance"). Without a key it falls back to a finance-tuned lexicon.
"""
from __future__ import annotations

import calendar
import html
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import feedparser
import requests

from . import upstream
from .market import coin_ticker

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

_GOOGLE_NEWS = "https://news.google.com/rss/search"
_YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
_YAHOO_FEED = "https://feeds.finance.yahoo.com/rss/2.0/headline"
_STOCKTWITS = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

# How long an analysis waits for the news and the posts. They are read side by
# side, so it is the longest the page waits for all of them together; read one
# after another, slow sources added up to most of a minute.
SOURCES_WAIT = 12.0
_TIMEOUT = (5, 10)


class SocialUnavailable(RuntimeError):
    """StockTwits refused or did not answer, which is not the same as nobody
    having posted."""

# Finance-specific polarity. Values are deliberately asymmetric: markets react
# harder to bad news than good, so negative terms carry more weight.
_LEXICON = {
    # bullish
    "beat": 1.0, "beats": 1.0, "surge": 1.2, "surges": 1.2, "soar": 1.3, "soars": 1.3,
    "rally": 1.0, "rallies": 1.0, "jump": 0.9, "jumps": 0.9, "climb": 0.7, "climbs": 0.7,
    "upgrade": 1.2, "upgraded": 1.2, "outperform": 1.0, "overweight": 0.8,
    "record": 0.9, "records": 0.6, "strong": 0.7, "growth": 0.6, "profit": 0.7,
    "profitable": 0.9, "expands": 0.6, "expansion": 0.6, "partnership": 0.7,
    "approval": 1.0, "approved": 1.0, "wins": 0.8, "won": 0.6, "contract": 0.5,
    "raises": 0.9, "raised": 0.8, "boost": 0.8, "boosts": 0.8, "tops": 0.9,
    "bullish": 1.1, "buyback": 0.9, "dividend": 0.5, "breakout": 0.8,
    "accelerate": 0.7, "accelerating": 0.7, "momentum": 0.6, "gains": 0.7,
    "optimistic": 0.7, "upside": 0.8, "undervalued": 0.7, "rebound": 0.8,
    "rise": 0.7, "rises": 0.7, "rising": 0.7, "rose": 0.7, "advance": 0.6,
    "advances": 0.6, "higher": 0.6, "gain": 0.7, "gained": 0.7, "surged": 1.2,
    "rallied": 1.0, "jumped": 0.9, "soared": 1.3, "outperforms": 1.0,
    "recovery": 0.7, "recovers": 0.7, "strength": 0.7,
    # bearish
    "miss": -1.1, "misses": -1.1, "missed": -1.1, "plunge": -1.4, "plunges": -1.4,
    "plummet": -1.4, "crash": -1.5, "crashes": -1.5, "tumble": -1.2, "tumbles": -1.2,
    "slump": -1.1, "slumps": -1.1, "sink": -1.1, "sinks": -1.1, "slide": -0.9,
    "slides": -0.9, "fall": -0.8, "falls": -0.8, "drop": -0.8, "drops": -0.8,
    "downgrade": -1.3, "downgraded": -1.3, "underperform": -1.1, "underweight": -0.9,
    "warning": -1.2, "warns": -1.2, "lawsuit": -1.0, "sued": -1.0, "probe": -1.1,
    "investigation": -1.1, "subpoena": -1.2, "recall": -1.2, "recalls": -1.2,
    "bankruptcy": -1.8, "delisting": -1.6, "fraud": -1.7, "halt": -1.3,
    "halted": -1.3, "layoffs": -1.0, "layoff": -1.0, "cuts": -0.9, "cut": -0.8,
    "weak": -0.9, "weakness": -0.9, "decline": -0.8, "declines": -0.8,
    "loss": -0.9, "losses": -0.9, "bearish": -1.1, "resign": -1.0, "resigns": -1.0,
    "overvalued": -0.8, "downside": -0.8, "selloff": -1.2,
    "correction": -0.7, "risk": -0.4, "concerns": -0.7, "disappointing": -1.2,
    "scandal": -1.5, "delay": -0.8, "delayed": -0.8, "denies": -0.6,
    "fell": -0.8, "falling": -0.8, "dropped": -0.8, "slipped": -0.7,
    "slipping": -0.7, "lower": -0.6, "declined": -0.8, "plunged": -1.4,
    "tumbled": -1.2, "sank": -1.1, "sliding": -0.9, "struggles": -0.9,
    "struggling": -0.9, "pressure": -0.6, "headwinds": -0.8, "slowdown": -1.0,
}

_NEGATORS = {"not", "no", "never", "without", "isn't", "wasn't", "doesn't",
             "didn't", "won't", "aren't", "cannot", "can't", "fails", "failed"}

_TOKEN = re.compile(r"[a-z']+")
_ADDRESS = re.compile(r"(?:https?://|www\.)\S+", re.I)
_TICKERS = re.compile(r"[$#@][\w.]+")


@dataclass
class Item:
    """One headline or post."""
    text: str
    source: str
    kind: str                 # "news" | "social"
    age_hours: float
    score: float = 0.0
    tagged: Optional[str] = None
    url: str = ""
    via: str = ""             # which of the sources it was read from

    @property
    def recency_weight(self) -> float:
        """Exponential decay, ~12h half-life.

        For day trading a headline from three days ago is close to irrelevant,
        while one from twenty minutes ago may be the whole story.
        """
        return float(math.pow(0.5, max(self.age_hours, 0.0) / 12.0))


@dataclass
class Sentiment:
    """Aggregate view across all sources."""
    score: float                     # -1 .. +1, recency weighted
    news_score: float
    social_score: float
    n_news: int
    n_social: int
    bullish_tags: int
    bearish_tags: int
    method: str
    items: List[Item] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    fresh_catalyst: bool = False
    # How whoever runs this copy could score headlines better. Kept out of
    # `notes`, which become the call's risks: an analysis is shared by every
    # viewer of the same ticker, and only the owner of the copy can add a key,
    # so the page decides who is shown this.
    setup_hint: str = ""
    # Why there are no posts, when there are none. Not in `notes` either: it
    # says nothing against a trade, and the page shows it with the news.
    social_note: str = ""
    # The sources that gave something, in words. One that answered nothing is
    # not named on the page as where the headlines came from.
    read_from: List[str] = field(default_factory=list)

    def label(self) -> str:
        s = self.score
        if s >= 0.35:
            return "bullish"
        if s >= 0.12:
            return "mildly bullish"
        if s <= -0.35:
            return "bearish"
        if s <= -0.12:
            return "mildly bearish"
        return "neutral"


def _age_hours(published_struct) -> float:
    if not published_struct:
        return 48.0
    try:
        # feedparser returns UTC.
        ts = calendar.timegm(published_struct)
        return max(0.0, (time.time() - ts) / 3600.0)
    except Exception:
        return 48.0


def lexicon_score(text: str) -> float:
    """Score text in [-1, 1] using the finance lexicon.

    Handles two constructions that a plain bag-of-words gets wrong:

    * Negation binds only to the nearest scoring word. Without that rule,
      "not strong and misses estimates" flips "misses" positive because "not"
      sits three tokens back, inverting the whole headline.
    * Contrast markers ("but", "however") signal where the real verdict is.
      In "beats earnings but cuts guidance" the two halves otherwise cancel to
      neutral, when the market reads it as clearly negative.
    """
    tokens = _TOKEN.findall((text or "").lower())
    if not tokens:
        return 0.0

    contrast_at = None
    for marker in ("but", "however", "though", "yet", "although"):
        if marker in tokens:
            contrast_at = tokens.index(marker)
            break
    concession_at = tokens.index("despite") if "despite" in tokens else None

    total = 0.0
    hits = 0
    for idx, tok in enumerate(tokens):
        val = _LEXICON.get(tok)
        if val is None:
            continue

        # Negation: nearest negator within three tokens, with no other scoring
        # word standing between it and this one.
        for back in range(1, 4):
            p = idx - back
            if p < 0:
                break
            if tokens[p] in _NEGATORS:
                if not any(t in _LEXICON for t in tokens[p + 1:idx]):
                    val = -val * 0.8
                break
            if tokens[p] in _LEXICON:
                break

        if contrast_at is not None:
            val *= 1.6 if idx > contrast_at else 0.5
        if concession_at is not None:
            val *= 0.5 if idx > concession_at else 1.4

        total += val
        hits += 1

    if hits == 0:
        return 0.0
    avg = total / math.sqrt(hits)          # dampen long keyword-stuffed text
    return max(-1.0, min(1.0, avg / 1.6))


# The breaker's name for Yahoo, the same one bot/market.py and bot/yahoo.py use.
_YAHOO_HOST = "Yahoo Finance"


# When a source that gave no headlines was last said to have, by its name. The
# page is drawn without it either way, so the log is the only place it shows;
# once an hour is enough.
_SAID: Dict[str, float] = {}
SAID_EVERY = 3600.0


def _gave_nothing(source: str, why: str) -> None:
    now = time.time()
    if now - _SAID.get(source, 0.0) >= SAID_EVERY:
        _SAID[source] = now
        log.warning("%s gave no headlines: %s", source, why)


def _google_news(query: str, limit: int, within: float = math.inf) -> List[Item]:
    """Headlines from Google News no more than so many hours old, or none when
    it does not answer."""
    items: List[Item] = []
    try:
        url = "%s?q=%s&hl=en-US&gl=US&ceid=US:en" % (
            _GOOGLE_NEWS, requests.utils.quote(query))
        # Fetched here rather than by feedparser, which waits with no time
        # limit at all, inside the request of whoever opened the page.
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        feed = feedparser.parse(r.content if r.status_code == 200 else b"")
        if not feed.get("version"):
            # A refusal, or a page in place of the feed: its consent page has
            # an address of its own. A feed with nothing in it is an answer.
            _gave_nothing("Google News", "HTTP %s from %s" % (
                r.status_code, (getattr(r, "url", "") or "").split("?")[0]))
        for e in feed.entries:
            age = _age_hours(e.get("published_parsed"))
            if age <= within and len(items) < limit:
                items.append(Item(text=html.unescape(e.get("title", "")), kind="news",
                                  source=(e.get("source", {}) or {}).get("title", "Google News"),
                                  age_hours=age, url=e.get("link", ""), via="Google News"))
    except Exception as exc:
        _gave_nothing("Google News", type(exc).__name__)
    return items


# Yahoo's headline feed keeps a little-written-about coin's for months, and
# Google its price pages for years. One older than this says nothing about now,
# and with nothing newer beside it, it would have been the whole of the news.
FEED_HOURS = 7 * 24.0


def _yahoo_news(symbol: str) -> List[Item]:
    """Headlines from Yahoo Finance, or none when it does not answer.

    Asked by Yahoo's own symbol. A coin's bare ticker is often a company's or
    a fund's there (LINK, LTC, BTC), and its headlines were scored as the
    coin's. Yahoo's search has none under a coin's symbol, and its headline
    feed has.
    """
    coin = coin_ticker(symbol) is not None
    items: List[Item] = []
    try:
        # Yahoo's news search shares the rate limit with its prices. It asks
        # nothing while the source is refusing, and a refusal here is told to
        # the shared breaker so the price requests stand back too.
        upstream.check(_YAHOO_HOST)
    except upstream.Throttled:
        return items
    try:
        if coin:
            r = requests.get(_YAHOO_FEED, params={"s": symbol, "region": "US", "lang": "en-US"},
                             headers=_HEADERS, timeout=_TIMEOUT)
        else:
            r = requests.get(_YAHOO_SEARCH,
                             params={"q": symbol, "newsCount": 15, "quotesCount": 0},
                             headers=_HEADERS, timeout=_TIMEOUT)
        if upstream.is_throttle(r.status_code):
            upstream.note_throttled(_YAHOO_HOST, upstream.retry_after_seconds(r),
                                    r.status_code)
        elif r.status_code == 200:
            upstream.note_ok(_YAHOO_HOST)
            for e in feedparser.parse(r.content).entries if coin else ():
                age = _age_hours(e.get("published_parsed"))
                if age <= FEED_HOURS:
                    items.append(Item(text=html.unescape(e.get("title", "")), kind="news",
                                      source="Yahoo Finance", age_hours=age,
                                      url=e.get("link", ""), via="Yahoo Finance"))
            for n in () if coin else r.json().get("news", []):
                pub = n.get("providerPublishTime")
                age = (time.time() - pub) / 3600.0 if pub else 48.0
                items.append(Item(text=html.unescape(n.get("title", "")), kind="news",
                                  source=n.get("publisher", "Yahoo Finance"),
                                  age_hours=max(0.0, age), url=n.get("link", ""),
                                  via="Yahoo Finance"))
        else:
            _gave_nothing("Yahoo Finance", "HTTP %s" % r.status_code)
            upstream.note_unclear(_YAHOO_HOST)
    except Exception as exc:
        _gave_nothing("Yahoo Finance", type(exc).__name__)
        upstream.note_unclear(_YAHOO_HOST)
    return items


def gather_news(symbol: str, name: str, limit: int = 25) -> List[Item]:
    """Pull recent headlines from Google News and Yahoo Finance, side by side.

    A source that has not answered in SOURCES_WAIT seconds is left out.
    """
    known = name if name and name.upper() != symbol.upper() else ""
    coin = coin_ticker(symbol)
    if coin:
        # Yahoo calls a coin by the pair, "Bitcoin USD", and headlines do not.
        # Its ticker is asked for as well, and nothing about stocks: many are
        # named by an ordinary word, and the headlines of every stock that rose
        # on optimism were scored as the Optimism coin's.
        query = '"%s" %s crypto' % (re.sub(r"[\s-]+USD$", "", known, flags=re.I) or coin, coin)
    else:
        query = '"%s" stock OR crypto' % (known or symbol)
    give_up = time.monotonic() + SOURCES_WAIT
    pool = ThreadPoolExecutor(max_workers=2)
    items: List[Item] = []
    try:
        for job in (pool.submit(_google_news, query, limit, FEED_HOURS if coin else math.inf),
                    pool.submit(_yahoo_news, symbol)):
            try:
                items += job.result(timeout=max(0.0, give_up - time.monotonic()))
            except Exception:
                pass
    finally:
        pool.shutdown(wait=False)

    # Drop near-duplicate headlines syndicated across outlets.
    seen, unique = set(), []
    for it in items:
        key = " ".join(_TOKEN.findall(it.text.lower())[:8])
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def gather_social(symbol: str, limit: int = 30) -> List[Item]:
    """Pull recent StockTwits posts, filtering out ticker-spam.

    A post listing four or more tickers is almost always promotional noise
    rather than an opinion about this instrument, so it is discarded.

    A coin's posts are under its ticker with .X on the end, and nowhere else:
    the bare ticker is a company's or a fund's. Raises SocialUnavailable when
    StockTwits refuses or does not answer.
    """
    coin = coin_ticker(symbol)
    try:
        r = requests.get(_STOCKTWITS.format(sym=requests.utils.quote(
            coin + ".X" if coin else symbol, safe="")), headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 404:               # a symbol it does not have
            return []
        if r.status_code != 200:
            raise SocialUnavailable("HTTP %s" % r.status_code)
        messages = r.json().get("messages") or []
    except SocialUnavailable:
        raise
    except Exception as exc:
        raise SocialUnavailable(type(exc).__name__) from exc

    out: List[Item] = []
    for m in messages[:limit]:
        # Without its web addresses: nobody can open them here, and a post that
        # is a ticker and a link says nothing.
        said = html.unescape((m.get("body") or "").strip())
        body = " ".join(_ADDRESS.sub(" ", said).split())
        if not body or (_ADDRESS.search(said)
                        and not _TOKEN.search(_TICKERS.sub(" ", body).lower())):
            continue
        if len(m.get("symbols") or []) >= 4:        # ticker-spam filter
            continue
        # Short @-replies are conversation between users, not a view on the
        # instrument, and they add noise without adding signal.
        if body.startswith("@") and len(body) < 80:
            continue
        created = m.get("created_at", "")
        try:
            ts = calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
            age = max(0.0, (time.time() - ts) / 3600.0)
        except Exception:
            age = 12.0
        tag = ((m.get("entities") or {}).get("sentiment") or {})
        tag = (tag or {}).get("basic")
        out.append(Item(text=body, kind="social", source="StockTwits",
                        age_hours=age, tagged=tag, via="StockTwits"))
    return out


# Scores Claude has given, by exactly what it was asked: when, how many it
# scored, and the scores.
SCORED_TTL = 30 * 60.0
SCORED_KEPT = 200
_SCORED: Dict[Tuple[str, str, str], Tuple[float, int, List[float]]] = {}
_SCORED_LOCK = threading.Lock()


def _score_with_claude(items: List[Item], symbol: str) -> int:
    """Score items with Claude. Returns how many it scored, or 0 when that was
    too few to use; the word list scores any it left out.

    Financial headlines routinely invert naive sentiment. "Beats earnings but
    guides lower" is bearish; a lexicon reads it as positive. When a key is
    available this is worth the call.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return 0
    try:
        import anthropic
    except ImportError:
        return 0

    numbered = "\n".join("%d. %s" % (i + 1, it.text[:220])
                         for i, it in enumerate(items))
    # The same items are the same question, and each asking is paid for. The
    # analysis above this is kept per person's settings, so one account with a
    # different size in each address asked it again every time.
    asked = (os.environ.get("CLAUDE_MODEL", ""), symbol, numbered)
    with _SCORED_LOCK:
        kept = _SCORED.get(asked)
    if kept is not None and time.time() - kept[0] < SCORED_TTL:
        for it, score in zip(items, kept[2]):
            it.score = score
        return kept[1]
    prompt = (
        "You are scoring market sentiment for the instrument %s for a SHORT-TERM "
        "day trade (hours, not months).\n\n"
        "For each numbered item output one line: '<number>: <score>' where score "
        "is a number from -1.0 (strongly bearish for the next few hours) to 1.0 "
        "(strongly bullish). Use 0.0 for irrelevant or purely neutral items.\n\n"
        "Judge the market impact, not the tone. 'Beats earnings but guides lower' "
        "is negative. Routine analyst chatter is near zero. Output nothing except "
        "the numbered scores.\n\nItems:\n%s" % (symbol, numbered)
    )
    try:
        # The SDK would otherwise wait ten minutes, three times, inside a page
        # request. A failure here only means the word list scores the items,
        # so it is not tried twice.
        client = anthropic.Anthropic(api_key=key, timeout=20.0, max_retries=0)
        resp = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception:
        return 0

    # An answer that ran out of room stops part-way, perhaps inside a number,
    # so its last line is not read.
    ended = getattr(resp, "stop_reason", None)
    if ended == "refusal":
        return 0
    lines = text.splitlines()
    if ended == "max_tokens":
        lines = lines[:-1]

    got = {}
    for line in lines:
        m = re.match(r"\s*(\d+)\s*[:.\)]\s*(-?\d*\.?\d+)", line)
        if not m:
            continue
        idx, val = int(m.group(1)) - 1, float(m.group(2))
        if 0 <= idx < len(items):
            got[idx] = max(-1.0, min(1.0, val))
    if len(got) < max(1, len(items) // 3):
        return 0
    # An item it gave no score for used to count as neutral, whatever it said.
    for idx, it in enumerate(items):
        it.score = got[idx] if idx in got else lexicon_score(it.text)
    # One cut short is worth asking again.
    if ended != "max_tokens":
        with _SCORED_LOCK:
            _SCORED[asked] = (time.time(), len(got), [it.score for it in items])
            while len(_SCORED) > SCORED_KEPT:
                del _SCORED[min(_SCORED, key=lambda k: _SCORED[k][0])]
    return len(got)


def analyse(symbol: str, name: str = "", use_llm: bool = True) -> Sentiment:
    """Collect and score everything, then blend into one directional read."""
    give_up = time.monotonic() + SOURCES_WAIT
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        posts = pool.submit(gather_social, symbol)
        news = gather_news(symbol, name)
        social_note = "No recent posts about this symbol on StockTwits."
        try:
            social = posts.result(timeout=max(0.0, give_up - time.monotonic()))
        except Exception:
            social = []
            social_note = "StockTwits did not answer, so posts were not read."
    finally:
        pool.shutdown(wait=False)
    notes: List[str] = []
    setup_hint = ""

    scored_by = "a list of finance words"
    all_items = news + social
    by_claude = _score_with_claude(all_items, symbol) if use_llm else 0
    if by_claude:
        scored_by = "Claude (%s)" % os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
        if by_claude < len(all_items):
            scored_by += ", and a list of finance words where it gave no score"
    else:
        for it in all_items:
            it.score = lexicon_score(it.text)
        # For every reader, in words about the reading rather than about a
        # setting: only whoever runs the server can add a key (setup_hint).
        if all_items:
            notes.append("%s were scored with a list of finance words, "
                         "which can misread financial phrasing, so treat the "
                         "news reading as rough."
                         % ("Headlines and posts" if news and social else
                            "Headlines" if news else "Posts"))
        if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
            setup_hint = ("No ANTHROPIC_API_KEY is set, so headlines were scored "
                          "with the built-in word list. Claude reads financial "
                          "phrasing much better: add the key to .env and start "
                          "Orenth again.")

    # StockTwits self-tags are a direct statement of intent. Trust them over
    # inferred text sentiment, but do not let them fully override it.
    bullish = bearish = 0
    for it in social:
        if it.tagged == "Bullish":
            bullish += 1
            it.score = max(it.score, 0.55) if it.score >= 0 else (it.score + 0.55) / 2
        elif it.tagged == "Bearish":
            bearish += 1
            it.score = min(it.score, -0.55) if it.score <= 0 else (it.score - 0.55) / 2

    def blend(group: List[Item]) -> float:
        if not group:
            return 0.0
        tw = sum(i.recency_weight for i in group)
        if tw <= 0:
            return 0.0
        return sum(i.score * i.recency_weight for i in group) / tw

    news_score = blend(news)
    social_score = blend(social)

    # News leads, social confirms. Retail chatter is noisier and more prone to
    # coordinated pumping, so it gets the smaller share.
    if news and social:
        combined = 0.68 * news_score + 0.32 * social_score
    else:
        combined = news_score if news else social_score

    # Said once, by the plan, which reads fresh_catalyst. A note here as well
    # put the same warning on the page twice in different words.
    fresh = any(i.age_hours <= 3.0 and abs(i.score) >= 0.5 for i in news)
    if not news:
        notes.append("No news retrieved for this symbol.")

    return Sentiment(
        score=max(-1.0, min(1.0, combined)),
        news_score=news_score, social_score=social_score,
        n_news=len(news), n_social=len(social),
        bullish_tags=bullish, bearish_tags=bearish,
        method=scored_by,
        items=sorted(all_items, key=lambda i: -abs(i.score) * i.recency_weight)[:12],
        notes=notes, fresh_catalyst=fresh, setup_hint=setup_hint,
        social_note="" if social else social_note,
        read_from=[s for s in ("Yahoo Finance", "Google News", "StockTwits")
                   if any(i.via == s for i in all_items)],
    )
