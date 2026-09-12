"""News and social sentiment.

Three free sources are combined:

* Google News RSS      - broad financial press coverage
* Yahoo Finance search - ticker-specific business headlines
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

import html
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

import feedparser
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

_GOOGLE_NEWS = "https://news.google.com/rss/search"
_YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
_STOCKTWITS = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

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
    "short seller": -1.3, "overvalued": -0.8, "downside": -0.8, "selloff": -1.2,
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
        ts = time.mktime(published_struct)
        # feedparser returns UTC struct_time; mktime reads it as local.
        ts -= time.timezone if not time.daylight else time.altzone
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


def gather_news(symbol: str, name: str, limit: int = 25) -> List[Item]:
    """Pull recent headlines from Google News and Yahoo Finance."""
    items: List[Item] = []
    query = name if name and name.upper() != symbol.upper() else symbol
    base = symbol.replace("-USD", "")

    try:
        url = "%s?q=%s&hl=en-US&gl=US&ceid=US:en" % (
            _GOOGLE_NEWS, requests.utils.quote('"%s" stock OR crypto' % query))
        feed = feedparser.parse(url)
        for e in feed.entries[:limit]:
            items.append(Item(text=html.unescape(e.get("title", "")), kind="news",
                              source=(e.get("source", {}) or {}).get("title", "Google News"),
                              age_hours=_age_hours(e.get("published_parsed")),
                              url=e.get("link", "")))
    except Exception:
        pass

    try:
        r = requests.get(_YAHOO_SEARCH, params={"q": base, "newsCount": 15, "quotesCount": 0},
                         headers=_HEADERS, timeout=15)
        if r.status_code == 200:
            for n in r.json().get("news", []):
                pub = n.get("providerPublishTime")
                age = (time.time() - pub) / 3600.0 if pub else 48.0
                items.append(Item(text=html.unescape(n.get("title", "")), kind="news",
                                  source=n.get("publisher", "Yahoo Finance"),
                                  age_hours=max(0.0, age), url=n.get("link", "")))
    except Exception:
        pass

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

    A post listing five or more tickers is almost always promotional noise
    rather than an opinion about this instrument, so it is discarded.
    """
    base = symbol.replace("-USD", ".X") if symbol.endswith("-USD") else symbol
    out: List[Item] = []
    for candidate in (base, symbol.replace("-USD", "")):
        try:
            r = requests.get(_STOCKTWITS.format(sym=candidate), headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            messages = r.json().get("messages", [])
        except Exception:
            continue

        for m in messages[:limit]:
            body = html.unescape((m.get("body") or "").strip())
            if not body:
                continue
            if len(m.get("symbols") or []) >= 4:        # ticker-spam filter
                continue
            # Short @-replies are conversation between users, not a view on the
            # instrument, and they add noise without adding signal.
            if body.startswith("@") and len(body) < 80:
                continue
            created = m.get("created_at", "")
            try:
                ts = time.mktime(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
                ts -= time.timezone if not time.daylight else time.altzone
                age = max(0.0, (time.time() - ts) / 3600.0)
            except Exception:
                age = 12.0
            tag = ((m.get("entities") or {}).get("sentiment") or {})
            tag = (tag or {}).get("basic")
            out.append(Item(text=body, kind="social", source="StockTwits",
                            age_hours=age, tagged=tag))
        if out:
            break
    return out


def _score_with_claude(items: List[Item], symbol: str) -> bool:
    """Score items with Claude. Returns True if scoring succeeded.

    Financial headlines routinely invert naive sentiment. "Beats earnings but
    guides lower" is bearish; a lexicon reads it as positive. When a key is
    available this is worth the call.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return False
    try:
        import anthropic
    except ImportError:
        return False

    numbered = "\n".join("%d. %s" % (i + 1, it.text[:220])
                         for i, it in enumerate(items))
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
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception:
        return False

    got = 0
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s*[:.\)]\s*(-?\d*\.?\d+)", line)
        if not m:
            continue
        idx, val = int(m.group(1)) - 1, float(m.group(2))
        if 0 <= idx < len(items):
            items[idx].score = max(-1.0, min(1.0, val))
            got += 1
    return got >= max(1, len(items) // 3)


def analyse(symbol: str, name: str = "", use_llm: bool = True) -> Sentiment:
    """Collect and score everything, then blend into one directional read."""
    news = gather_news(symbol, name)
    social = gather_social(symbol)
    notes: List[str] = []

    scored_by = "finance lexicon"
    all_items = news + social
    if use_llm and _score_with_claude(all_items, symbol):
        scored_by = "Claude (%s)" % os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    else:
        for it in all_items:
            it.score = lexicon_score(it.text)
        if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
            notes.append("No ANTHROPIC_API_KEY set, so headlines were scored by "
                         "keyword lexicon. LLM scoring is materially better on "
                         "financial phrasing.")

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

    fresh = any(i.age_hours <= 3.0 and abs(i.score) >= 0.5 for i in news)
    if fresh:
        notes.append("A high-impact headline landed in the last 3 hours. "
                     "Technical levels are less reliable right after a catalyst.")
    if not news:
        notes.append("No news retrieved for this symbol.")
    if not social:
        notes.append("No StockTwits activity found for this symbol.")

    return Sentiment(
        score=max(-1.0, min(1.0, combined)),
        news_score=news_score, social_score=social_score,
        n_news=len(news), n_social=len(social),
        bullish_tags=bullish, bearish_tags=bearish,
        method=scored_by,
        items=sorted(all_items, key=lambda i: -abs(i.score) * i.recency_weight)[:12],
        notes=notes, fresh_catalyst=fresh,
    )
