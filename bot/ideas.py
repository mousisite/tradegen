"""What is worth looking at right now, given how you actually trade.

The screener answers "which companies match these numbers". This answers the
question people really arrive with: *out of everything, what should I look at
today?* You pick what you trade and roughly how long you hold; it runs the
whole analysis over a list and ranks what comes back.

Two things it refuses to do, and both are the reason to trust it.

It does not fill a list. If nothing clears the bar, it returns nothing and says
so. Every screener on the internet always has ten results, because a page with
ten rows feels like it worked. That is how people end up trading the least bad
of a bad batch.

It does not rank by how exciting something looks. The order is the measured
edge on that instrument's own history after costs, so the thing at the top is
the thing that has most often worked, not the thing that moved most today.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Dict, List, Optional, Tuple

from . import upstream

# What you trade.
MARKETS = {
    "stocks": {"label": "Stocks", "why": "Shares in companies."},
    "crypto": {"label": "Crypto", "why": "Coins. Open all hours, moves harder."},
    "both": {"label": "Both", "why": "Look at everything."},
}

# How long you hold. The interval has to match, because a setup measured on
# five-minute bars says nothing about a position held for a month.
HORIZONS = {
    "short": {
        "label": "Days", "interval": "1h",
        "why": "In and out within a few days.",
        "warning": ("Short holds pay the spread most often. By this app's own "
                    "back-tests that is where the costs eat the edge."),
    },
    "medium": {
        "label": "Weeks", "interval": "1d",
        "why": "Hold for a couple of weeks.",
        "warning": "",
    },
    "long": {
        "label": "Months", "interval": "1d",
        "why": "Hold for months. The company has to stand up, not just the chart. "
               "Stocks only: a coin files no accounts to check.",
        "warning": "",
        # Over months the accounts matter more than the pattern, so this one
        # additionally requires the filings to look sound. Without that it
        # would be an identical copy of the weeks option, which is worse than
        # not offering it.
        "needs_sound_accounts": True,
    },
}

# How much you are willing to be wrong for a bigger payoff.
#
# Every one of these filters *within* what already cleared the bar. None of
# them lowers it. "High risk, high reward" means the instruments that swing
# hardest among setups that have actually made money after costs on their own
# history. It never means showing something that lost money: there is no
# appetite for risk that makes a losing setup worth taking.
#
# Reward-to-risk is deliberately not one of the axes. Every plan is built at
# target_atr_multiple over stop_atr_multiple, so with the default settings the
# ratio is 1.5 on everything. It describes your own configuration, not the
# instrument, and filtering on it would sort by nothing at all.
#
# What does vary, five-fold across instruments, is gap risk: how much worse
# than your stop an overnight move could actually be. That is the honest risk
# axis here, and it is measured rather than assumed.
RISKS = {
    "steady": {
        "label": "Steady",
        "why": "Gaps less. Wins more often.",
        "min_probability": 0.30,
        "max_gap_multiple": 2.0,
        "crypto_only": False,
        "rank_by": "probability",
        "ranked_as": "how often this setup has worked before",
        "note": ("A gap past your stop on these costs at most about twice "
                 "what you planned to risk, and they have worked more often "
                 "than the rest. That is the calmer end of what cleared the "
                 "bar, not a promise of safety."),
    },
    "balanced": {
        "label": "Balanced",
        "why": "The strongest measured edge, whatever shape it takes.",
        "min_probability": 0.0,
        "max_gap_multiple": None,
        "crypto_only": False,
        "rank_by": "expectancy",
        "ranked_as": "measured edge after costs",
        "note": "",
    },
    "wild": {
        "label": "High risk, high reward",
        "why": "Swings hardest. Hurts hardest when wrong.",
        "min_probability": 0.0,
        "min_gap_multiple": 2.5,
        "max_gap_multiple": None,
        "crypto_only": False,
        "rank_by": "expectancy",
        "ranked_as": "measured edge after costs",
        "note": ("A gap past your stop on these could cost two and a half "
                 "times what you planned to risk, or more. That is the "
                 "reward you are being paid for and the way it goes wrong. "
                 "Your stop does not protect you through a gap, so size these "
                 "smaller than the number on the screen suggests."),
    },
}

# Hand-kept because the free screener endpoints cover no crypto at all.
#
# Three coins share their short ticker with something else on the data source,
# which gives the well-known coin a numbered id instead. ARB-USD there is
# "ARbit", a token priced at a fraction of a cent with no trading at all, so
# the list was analysing it as if it were Arbitrum. UNI-USD and SUI-USD have no
# prices at all and failed on every scan. Each entry was checked against the
# name the source gives it.
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD",
          "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "BCH-USD", "SHIB-USD",
          "UNI7083-USD", "ATOM-USD", "NEAR-USD", "XLM-USD", "HBAR-USD",
          "FIL-USD", "ETC-USD", "ICP-USD", "ARB11841-USD", "OP-USD", "INJ-USD",
          "SUI20947-USD", "TRX-USD", "VET-USD", "ALGO-USD",
          "AAVE-USD"]

# A spread of liquid names, used when the live screener is unavailable.
FALLBACK_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                   "AMD", "AVGO", "JPM", "V", "UNH", "XOM", "KO", "WMT",
                   "COST", "NFLX", "DIS", "BA", "PFE"]

# Raised from 40 now that a scan is shared between everyone who asks the same
# question: one person pays for it and the rest read the answer. This is not
# every listed stock and cannot be. Yahoo publishes saved screens, not the full
# tape, and asking for thousands of instruments gets this server rate limited
# within a minute. What it does cover is every actively traded name those
# screens surface, which is where a setup worth taking almost always is.
MAX_SCANNED = 150

# A scan of the market is the same question for everybody, and the answer does
# not meaningfully change minute to minute. Without this, every visitor pays
# the full scan: on hourly crypto bars that is the better part of a minute of
# waiting, often to be told nothing cleared the bar.
CACHE_TTL = 300

# How long a caller that wants the finished answer (a test, the terminal)
# waits for it. Longer than the slowest scan measured.
SCAN_WAIT = 150.0

# A scan runs in a background thread, not inside the page request: Cloudflare
# abandons any request that has not answered within 100 seconds, and a full
# list on a slow data source takes longer than that. It used to stop at 60
# seconds and show what it had, which is how a scan of 54 out of 150 came to
# be ranked as if it were the whole market. Now the page shows progress, and
# this is only the backstop for a data source that has stopped answering.
SCAN_BUDGET = 15 * 60.0

# How long the page itself waits for a scan before showing its progress
# instead. A scan already cached, or a short list, finishes inside this. Only
# the request that started the scan waits: the page reloads itself every few
# seconds while a scan runs, and each reload holding one of the server's few
# request threads for this long would starve every other page.
PAGE_WAIT = 12.0

# Finished scans no longer asked for are dropped after this, so per-person
# settings cannot grow the store without limit.
JOB_KEEP = 3 * 3600.0

# The months horizon then reads the filings of whatever cleared the bar. The
# two budgets together stay under Cloudflare's limit with room for the page
# itself to render.
ACCOUNTS_BUDGET = 20.0

# The server has half a CPU, and each instrument is a few tenths of a second of
# arithmetic. Two scans at once do not finish sooner; they slow each other and
# every other page. So one runs at a time across the whole server, a few more
# wait their turn, and a question beyond that is turned away with a plain
# answer rather than piling up threads.
RUNNING_SCANS = 1
WAITING_SCANS = 3

# Threads inside one scan: enough for one instrument's network wait to overlap
# another's arithmetic. More only queue for the same half a CPU.
SCAN_THREADS = 2

# A scan may miss the odd instrument the data source has no prices for. Past
# this share of its list (and never fewer than three) it is a part-scan: it is
# shown as such, never ranked, and never kept as the answer. _is_complete is
# the one place that decides.
MISSING_ALLOWED = 0.05

# After a scan the data source refused, or that failed, how long before another
# is started. Asking again at once is what keeps a rate limit shut.
RETRY_AFTER = 120.0

# Each instrument's row is kept for a while and reused by any scan asking the
# same question with the same settings: the next refresh, "Both", which covers
# the stocks and the coins together, and a scan retried after the source
# refused part of it. On daily bars the signals and the record behind them
# change once a day; within the day only the latest price, and what is set
# from it, moves. Every row carries when its price was read and the page shows
# the oldest, so a reused row is never passed off as fresh.
ROW_TTL = {"1d": 30 * 60.0, "1h": 10 * 60.0}
ROW_KEEP = 2000

# The stock list comes from ten saved screens, one request each, and changes
# slowly.
UNIVERSE_TTL = 10 * 60.0

# A company's filings change a few times a year, so a verdict on them is kept
# for a day. A read that failed is kept only briefly, so a later view tries
# again.
VERDICT_TTL = 24 * 3600.0
VERDICT_RETRY = 10 * 60.0

# The calls that are something to do today. Anything else is a stand-aside.
ACTIONABLE = ("BUY", "WAIT", "SHORT")

# How many of the names to watch are listed, strongest record first.
WATCH_SHOWN = 10

# Every price this module reads comes from here; its circuit breaker says when
# the source has started refusing.
_SOURCE = "Yahoo Finance"

# Every screen Yahoo publishes, so the scan is not just a repetitive slice of
# the most-traded names. Losers matter as much as gainers: the app takes short
# setups too.
_SCREENS = ("most_actives", "day_gainers", "day_losers",
            "undervalued_growth_stocks", "growth_technology_stocks",
            "undervalued_large_caps", "aggressive_small_caps",
            "small_cap_gainers", "most_shorted_stocks", "portfolio_anchors")


class Busy(RuntimeError):
    """Every scan slot is taken, so a new question cannot start just now."""

    def __init__(self):
        super().__init__(
            "The server is already running as many scans as it can, for other "
            "people's questions. Try again in a few minutes.")


def _clock(ts: float) -> str:
    """A time of day in UTC, for saying when something was read."""
    return time.strftime("%H:%M UTC", time.gmtime(ts))


# Settings no row of a scan depends on: how big an account is and how its
# positions are sized, the bars an analysis opens on (a scan names its own), the
# journal's daily rules, how candles are drawn, futures (none is scanned), news
# (none is read) and how often alerts are checked. Anyone who had changed one
# of these used to get a scan of their own, minutes long, of the same answer.
NOT_ASKED = frozenset((
    "account_size", "risk_per_trade_pct", "max_position_pct", "interval",
    "daily_loss_limit_r", "max_trades_per_day", "last_entry_time",
    "calm_candles", "cost_bps_futures", "use_llm_sentiment",
    "alert_check_minutes"))

# What a scan sizes its plans for. The one figure a row takes from the size,
# how much worse than the stop a gap would be, is the same for any account that
# can hold one share; it is only missing for one that cannot.
_SIZED_FOR = {"account_size": 1e9, "risk_per_trade_pct": 1.0, "max_position_pct": 25.0}


def _fingerprint(cfg: Dict) -> str:
    """The settings a scan's answer depends on, as a short key.

    Trading costs are a per-person figure and they decide what clears the bar,
    so a scan or a row made with one person's settings answers a different
    question for anybody with others. Settings in NOT_ASKED do not change the
    answer, so they do not split it.
    """
    return hashlib.sha1(
        repr(sorted((str(k), repr(v)) for k, v in cfg.items() if k not in NOT_ASKED))
        .encode("utf-8")).hexdigest()[:12]


_UNIVERSE: Dict[str, Tuple[float, List[str]]] = {}
_UNIVERSE_LOCK = threading.Lock()


def _stock_list() -> List[str]:
    """Every actively traded name Yahoo's saved screens surface.

    Ten requests, so the list is reused for a few minutes rather than fetched
    for every scan. When the screens do not answer, the last list they gave
    stands in: it is closer to today's market than the hand-kept fallback.
    """
    from . import screener as screener_mod

    with _UNIVERSE_LOCK:
        kept = _UNIVERSE.get("stocks")
        if kept is not None and time.time() - kept[0] < UNIVERSE_TTL:
            return list(kept[1])
        # Several screens rather than one, because "most active" alone is a
        # narrow and repetitive slice of the market.
        seen = set()
        stocks: List[str] = []
        for screen in _SCREENS:
            try:
                for symbol in screener_mod.universe_symbols(screen, 60) or []:
                    if symbol not in seen:
                        seen.add(symbol)
                        stocks.append(symbol)
            except Exception:
                continue
        # Anything with a suffix is a foreign listing or a warrant; both are
        # noise in a "what should I look at" list.
        stocks = [s for s in stocks if s.isalpha() and len(s) <= 5]
        if stocks:
            _UNIVERSE["stocks"] = (time.time(), stocks)
            return list(stocks)
        return list(kept[1]) if kept is not None else list(FALLBACK_STOCKS)


def _interleave(first: List[str], second: List[str]) -> List[str]:
    """One from each in turn, then the rest of the longer one."""
    out: List[str] = []
    for i in range(max(len(first), len(second))):
        out.extend(names[i] for names in (first, second) if i < len(names))
    return out


def _universe(market: str, limit: int) -> List[str]:
    stocks = _stock_list() if market in ("stocks", "both") else []
    coins = list(CRYPTO) if market in ("crypto", "both") else []

    if market == "both":
        # The whole stocks list and every coin, taken in turn. Stocks first
        # then coins put the 28 coins behind 75 stocks, so a scan that ran
        # short reached none of them and "Both" came back stocks only. The rows
        # are shared with the stocks and crypto scans, so asking for both costs
        # little once either has run.
        return _interleave(stocks[:limit], coins[:limit])
    return (stocks or coins)[:limit]


_ROWS: "OrderedDict[Tuple[str, str, str], Dict]" = OrderedDict()
_ROWS_LOCK = threading.Lock()


def _look_at(symbol: str, interval: str, cfg: Dict) -> Optional[Dict]:
    """One instrument's row, reusing a recent analysis of it. Never raises."""
    key = (symbol.upper(), interval, _fingerprint(cfg))
    keep = ROW_TTL.get(interval, min(ROW_TTL.values()))
    with _ROWS_LOCK:
        kept = _ROWS.get(key)
        if kept is not None and time.time() - kept["read_at"] < keep:
            _ROWS.move_to_end(key)
            return dict(kept)
    row = _analyse(symbol, interval, cfg)
    if row is None:
        # Not kept: the next scan should ask again, not repeat the failure.
        return None
    with _ROWS_LOCK:
        _ROWS[key] = row
        _ROWS.move_to_end(key)
        while len(_ROWS) > ROW_KEEP:
            _ROWS.popitem(last=False)
    return dict(row)


def _analyse(symbol: str, interval: str, cfg: Dict) -> Optional[Dict]:
    """Run the real analysis on one instrument. Never raises."""
    from . import engine

    read_at = time.time()
    try:
        # The ticker is used exactly as listed, and the earnings calendar is
        # not read because nothing here uses it: each would be one more request
        # per instrument to the source every scan shares.
        result = engine.analyse(symbol, dict(cfg, **_SIZED_FOR), interval=interval,
                                with_news=False, with_learning=True, record=False,
                                resolve_symbol=False, with_calendar=False)
    except Exception:
        return None

    # A ticker the source has renamed or folded into another can come back
    # under a different symbol. That would be recommending an instrument
    # nobody asked about and nothing vetted, so it counts as not found.
    got = (result.bars.symbol or "").upper()
    if got != symbol.upper():
        return None

    plan = result.plan
    calib = result.calibration
    return {
        "symbol": result.bars.symbol,
        "name": getattr(result.bars, "name", "") or result.bars.symbol,
        "price": result.bars.last_price,
        "action": plan.action,
        "conviction": plan.conviction,
        "entry": plan.entry,
        "stop": plan.stop,
        "target": plan.target1,
        "probability": plan.probability,
        "samples": plan.prob_samples,
        "reliable": plan.prob_reliable,
        "expectancy": plan.expectancy_r,
        # Which side that record is for. The odds are measured for the side
        # the call leans to, so a bearish call's expectancy is a short's.
        "odds_direction": getattr(plan, "odds_direction", 0) or 0,
        "breakeven": plan.breakeven_rate,
        "reward_risk": plan.reward_risk,
        "asset_class": result.bars.asset_class,
        "headline": plan.headline,
        "gap_multiple": (plan.position or {}).get("gap_multiple"),
        "edge_verdict": plan.edge_verdict,
        "cost_r": getattr(calib, "cost_r", None),
        "read_at": read_at,
    }


def _worth_showing(row: Dict) -> bool:
    """Whether this cleared the bar, rather than merely being the least bad.

    Three conditions, all of them things the app already measured: it has to
    be an actionable call, the measured edge after costs has to be positive,
    and there has to have been enough history to mean anything.
    """
    if row["action"] not in ("BUY", "WAIT", "SHORT"):
        return False
    if row.get("expectancy") is None or row["expectancy"] <= 0:
        return False
    if not row.get("samples") or row["samples"] < 30:
        return False
    return True


def _matches_risk(row: Dict, spec: Dict) -> bool:
    """Whether this suits the appetite asked for.

    Only ever narrows what already cleared the bar. A figure the app could not
    measure is not counted against an instrument: the filter judges what was
    measured rather than inventing a failure out of a blank.
    """
    if (row.get("probability") or 0.0) < spec["min_probability"]:
        return False

    gap = row.get("gap_multiple")
    cap = spec.get("max_gap_multiple")
    if cap is not None and gap is not None and gap > cap:
        return False

    floor = spec.get("min_gap_multiple")
    if floor is not None:
        # Unknown gap risk cannot be counted as high gap risk. An appetite for
        # danger is not an appetite for guesses.
        if gap is None or gap < floor:
            return False
    return True


# Verdicts on a company's filings, kept per symbol: (good until, verdict).
_VERDICTS: Dict[str, Tuple[float, Optional[Dict]]] = {}
_VERDICT_JOBS: Dict[str, object] = {}
_VERDICT_LOCK = threading.Lock()
_VERDICT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="orenth-filings")


def _read_verdict(symbol: str) -> Optional[Dict]:
    """Read one company's filings, and keep the verdict however late it lands."""
    try:
        verdict = _accounts_hold_up(symbol)
    except Exception as exc:
        verdict = _unreadable(exc)
    failed = verdict is not None and verdict.get("ok") is None
    with _VERDICT_LOCK:
        _VERDICTS[symbol] = (time.time() + (VERDICT_RETRY if failed else VERDICT_TTL),
                             verdict)
        _VERDICT_JOBS.pop(symbol, None)
    return verdict


def _check_accounts(symbols: List[str],
                    budget: Optional[float] = None) -> Dict[str, Optional[Dict]]:
    """Read several instruments' filings at once, inside a time limit.

    One at a time, twenty candidates took eighteen seconds on a warm machine,
    and on a slower server after a cold scan that is how a page runs into
    Cloudflare's cutoff. Whatever has not answered when the budget runs out is
    simply absent from the result, which the caller reports as unchecked; it
    carries on in the background and is kept, so the next view has it.

    Verdicts are kept for a day, because filings change a few times a year and
    reading them again on every page view cost every visitor the wait.
    """
    found: Dict[str, Optional[Dict]] = {}
    waiting = {}
    now = time.time()
    with _VERDICT_LOCK:
        for symbol in dict.fromkeys(symbols):
            kept = _VERDICTS.get(symbol)
            if kept is not None and kept[0] > now:
                found[symbol] = kept[1]
                continue
            future = _VERDICT_JOBS.get(symbol)
            if future is None:
                future = _VERDICT_POOL.submit(_read_verdict, symbol)
                _VERDICT_JOBS[symbol] = future
            waiting[future] = symbol
    if waiting:
        try:
            for future in as_completed(
                    waiting, timeout=ACCOUNTS_BUDGET if budget is None else budget):
                found[waiting[future]] = future.result()
        except FuturesTimeout:
            pass
    return found


def _unreadable(why) -> Dict:
    """A verdict that could not be reached, which is not a verdict either way."""
    return {"ok": None, "reasons": [], "error": str(why) or type(why).__name__}


def _accounts_hold_up(symbol: str) -> Optional[Dict]:
    """Whether the filings support holding this for months.

    None only when the company files nothing to check. When the filings or the
    figures beside them could not be read, the answer says so ({"ok": None})
    instead: a source that did not answer says nothing about the company, and
    calling that "no filings" would be a false claim about it.

    Only called for names that already cleared the price test, so this costs a
    handful of requests rather than one per instrument scanned.
    """
    from . import fundamentals as fundamentals_mod
    from . import quality as quality_mod
    from . import sec as sec_mod

    try:
        filings = sec_mod.financial_history(symbol, years=8)
    except Exception as exc:
        return _unreadable(exc)
    if not filings.get("available"):
        return None
    try:
        facts = fundamentals_mod.load(symbol, with_sec=False)
        refused = [n for n in facts.notes
                   if n.startswith("Yahoo fundamentals unavailable")]
        if refused:
            # Without the market value the distress score cannot run, and a
            # check that skipped it would pass a distressed company as sound.
            return _unreadable(refused[0])
        scored = quality_mod.assess(filings, facts.get("market_cap"), facts.sector)
    except Exception as exc:
        return _unreadable(exc)
    if not scored.get("available"):
        return _unreadable(scored.get("reason") or "the filings could not be scored")

    models = scored["scores"]
    strength = models["piotroski"]
    distress = models["altman"]
    earnings = models["accruals"]

    reasons = []
    if strength.usable and strength.value <= 3:
        reasons.append("its finances got worse in most ways last year")
    if distress.usable and distress.value < 1.81:
        reasons.append("the balance sheet scores in the distress range")
    if earnings.usable and earnings.value > 0.10:
        reasons.append("reported profit is running well ahead of cash")

    return {"ok": not reasons, "reasons": reasons,
            "strength": strength.value if strength.usable else None}


# --- background scans ------------------------------------------------------
#
# One job per question (market, bar interval, settings, list length), shared
# by everyone asking it. A second person asking while it runs joins it rather
# than starting another. The finished scan is reused until it is older than
# the cache window; after that the next request starts a fresh one and, while
# it runs, shows the last finished one with its age. Only a complete scan is
# ever kept as the answer: one the source refused stays the answer to nobody.

_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(RUNNING_SCANS)


def _job(key: str, market: str, interval: str, cfg: Dict, limit: int,
         ttl: float) -> Tuple[Dict, bool]:
    """The job for this question, and whether this call started it.

    Fresh and finished, running, recently refused (whose answer stands until
    it may be tried again), or new. Raises Busy when every slot is taken and
    there is nothing at all to show for this question.
    """
    now = time.time()
    with _JOBS_LOCK:
        for old_key in [k for k, j in _JOBS.items()
                        if not j["running"] and now - j["started"] > JOB_KEEP]:
            del _JOBS[old_key]
        job = _JOBS.get(key)
        if job is not None:
            if job["running"]:
                return job, False
            if job["scan"] is not None and now - job["scan"]["finished_at"] < ttl:
                return job, False
            if now < job["retry_at"]:
                return job, False
        if sum(1 for j in _JOBS.values() if j["running"]) >= RUNNING_SCANS + WAITING_SCANS:
            if job is not None:
                return job, False           # what there is, with its age
            raise Busy()
        previous = None
        if job is not None:
            previous = job["scan"] or job["previous"]
        job = {"running": True, "queued": True, "rows": [], "failed": [],
               "done": 0, "total": 0, "started": now, "scan": None,
               "previous": previous, "attempt": None, "retry_at": 0.0,
               "error": None, "finished": threading.Event()}
        _JOBS[key] = job
    threading.Thread(target=_run, args=(job, market, interval, cfg, limit),
                     daemon=True, name="orenth-scan").start()
    return job, True


def _run(job: Dict, market: str, interval: str, cfg: Dict, limit: int) -> None:
    try:
        with _SLOTS:
            with _JOBS_LOCK:
                job["queued"] = False
                job["started"] = time.time()
            scan = _scan(market, interval, cfg, limit, job=job)
        with _JOBS_LOCK:
            if scan["complete"]:
                job["scan"] = scan
            else:
                # Shown for what it is, but not published: the last complete
                # scan, if there is one, stays the answer, and the next try
                # waits a little rather than asking a refusing source again.
                job["attempt"] = scan
                job["retry_at"] = time.time() + RETRY_AFTER
    except Exception as exc:                # the page says so; the server lives on
        with _JOBS_LOCK:
            job["error"] = str(exc) or type(exc).__name__
            job["retry_at"] = time.time() + RETRY_AFTER
    finally:
        with _JOBS_LOCK:
            job["running"] = False
        job["finished"].set()


def find(market: str = "stocks", horizon: str = "medium",
         cfg: Optional[Dict] = None, risk: str = "balanced",
         limit: int = MAX_SCANNED, progress=None, wait: float = SCAN_WAIT,
         ttl: float = CACHE_TTL, join_wait: Optional[float] = None) -> Dict:
    """What cleared the bar, for this market, holding period and appetite.

    The slow part, analysing every instrument, depends only on the market and
    the bar interval, so it runs once and is shared: between visitors, between
    the three risk appetites, and between the weeks and months horizons, which
    both read daily bars. Changing appetite re-filters a scan that already
    exists instead of repeating a thirty-second one.

    The scan runs in the background. wait is how long to wait for it to
    finish; join_wait, when given, caps that for a scan somebody else started,
    which is what a page reloading itself to show progress is doing. If it is
    still running after that, the answer says so: either the last finished
    scan with its age, or what has been looked at so far, marked as not yet
    the whole list. complete is True only for a scan that read enough of its
    list to rank, as _is_complete decides.
    """
    from . import config as config_mod

    cfg = cfg or config_mod.load()
    market = market if market in MARKETS else "stocks"
    horizon = horizon if horizon in HORIZONS else "medium"
    risk = risk if risk in RISKS else "balanced"
    interval = HORIZONS[horizon]["interval"]

    # The settings are part of the key, so one person's costs never answer
    # somebody else's question.
    key = "ideas:%s:%s:%s:%d" % (market, interval, _fingerprint(cfg), limit)

    if progress is not None:
        # The terminal: one person, watching it go, so no shared job.
        return dict(_shortlist(_scan(market, interval, cfg, limit, progress),
                               horizon, risk), running=False)

    job, started_here = _job(key, market, interval, cfg, limit, ttl)
    if not started_here and join_wait is not None:
        wait = min(wait, join_wait)
    if wait and job["running"]:
        job["finished"].wait(wait)
    return _answer(job, market, interval, limit, horizon, risk)


def _answer(job: Dict, market: str, interval: str, limit: int, horizon: str,
            risk: str) -> Dict:
    """The best answer a job has right now, saying exactly what it is."""
    with _JOBS_LOCK:
        running, scan = job["running"], job["scan"]
        previous, attempt, error = job["previous"], job["attempt"], job["error"]
        status = {"running": True, "queued": job["queued"], "looked_at": job["done"],
                  "asked_for_now": job["total"], "started_at": job["started"]}
        partial = {"market": market, "interval": interval,
                   "rows": list(job["rows"]), "failed": list(job["failed"]),
                   "asked_for": job["total"] or min(limit, MAX_SCANNED),
                   "cut_short": False, "throttled": False, "complete": False,
                   "seconds": time.time() - job["started"],
                   "scanned_at": job["started"], "finished_at": None}

    if not running:
        if scan is not None:
            return dict(_shortlist(scan, horizon, risk), running=False)
        if previous is not None:
            # The refresh did not give a complete scan, so the last one that
            # did stays the answer, and the page says why it was not replaced.
            return dict(_shortlist(previous, horizon, risk), running=False,
                        refresh_note=_refresh_note(attempt, error))
        if attempt is not None:
            # Nothing complete to fall back on: shown as the part-scan it is.
            return dict(_shortlist(attempt, horizon, risk), running=False)
        raise RuntimeError(error or "The scan stopped without an answer.")

    if previous is not None:
        # A finished scan from earlier is a better answer than a part-done
        # one, as long as it says how old it is. Filings already read are
        # used; the rest are read in the background rather than holding this
        # request, which the page repeats every few seconds.
        return dict(_shortlist(previous, horizon, risk, accounts_wait=0),
                    refreshing=True, **status)
    return dict(_shortlist(partial, horizon, risk, partial=True), **status)


def _refresh_note(attempt: Optional[Dict], error: Optional[str]) -> str:
    """Why the list shown is not the one from the scan that just ran."""
    if attempt is not None:
        return ("A fresh scan at %s could read only %d of the %d instruments%s, "
                "so this is the last complete scan instead. Another will be "
                "tried in a couple of minutes."
                % (_clock(attempt["finished_at"]), len(attempt["rows"]),
                   attempt["asked_for"],
                   " before the data source started refusing"
                   if attempt.get("throttled") else ""))
    return ("A fresh scan failed (%s), so this is the last complete scan "
            "instead." % (error or "no reason given"))


def _is_complete(scan: Dict) -> bool:
    """Whether a finished scan read enough of its list to be ranked and kept.

    The one place this is decided. Not when it ran out of time, not when the
    data source started refusing, and not when more than a few instruments
    could not be read: a list missing a tenth of the market is not the
    market, and ranking it as if it were is the quiet overstatement this page
    exists to avoid. A few are allowed, because the source has no prices at
    all for the odd delisted name, and that never clears.
    """
    failed = len(scan.get("failed") or [])
    allowed = max(3, int(MISSING_ALLOWED * (scan.get("asked_for") or 0)))
    return (bool(scan.get("rows")) and not scan.get("cut_short")
            and not scan.get("throttled") and failed <= allowed)


def _source_refusing() -> bool:
    """Whether the data source's circuit breaker is open."""
    try:
        upstream.check(_SOURCE)
    except upstream.Throttled:
        return True
    return False


def _scan(market: str, interval: str, cfg: Dict, limit: int,
          progress=None, job: Optional[Dict] = None) -> Dict:
    """Analyse every instrument on the list once. Always the slow path.

    Each instrument ends up in exactly one place: rows (read and analysed),
    failed (the data source gave nothing usable), or not looked at because
    the budget ran out (cut_short) or the source started refusing (throttled).
    The three always add up to asked_for.
    """
    started = time.time()
    # The budget covers fetching the list as well as reading it.
    deadline = started + SCAN_BUDGET
    symbols = _universe(market, min(limit, MAX_SCANNED))
    if job is not None:
        with _JOBS_LOCK:
            job["total"] = len(symbols)

    rows: List[Dict] = []
    failed: List[str] = []
    cut_short = False
    refusing = threading.Event()

    def look(symbol: str):
        # Once the source has started refusing, the rest are not looked at:
        # each would be refused too, and every request asked keeps the limit
        # shut for everyone on the site. Checked by the workers themselves, as
        # a failure that comes back at once can otherwise run through the
        # whole list before anyone reads the results.
        if refusing.is_set():
            return None, False
        row = _look_at(symbol, interval, cfg)
        if row is None and _source_refusing():
            refusing.set()
        return row, True

    # Two at a time, so one instrument's network wait overlaps another's
    # arithmetic, and the shared cache means overlapping symbols cost one
    # request between them.
    pool = ThreadPoolExecutor(max_workers=SCAN_THREADS)
    futures = {pool.submit(look, s): s for s in symbols}
    try:
        for future in as_completed(futures,
                                   timeout=max(0.0, deadline - time.time())):
            row, looked = future.result()
            if not looked:
                continue                    # the scan stopped before this one
            if row:
                rows.append(row)
            else:
                failed.append(futures[future])
            if job is not None:
                with _JOBS_LOCK:
                    if row:
                        job["rows"].append(row)
                    else:
                        job["failed"].append(futures[future])
                    job["done"] += 1
            if progress:
                progress("Looked at %d of %d" % (len(rows) + len(failed), len(symbols)))
    except FuturesTimeout:
        cut_short = True
    finally:
        # Work not yet started is abandoned. The few already running finish in
        # the background and are discarded; a thread cannot be stopped midway.
        pool.shutdown(wait=False, cancel_futures=True)
    throttled = refusing.is_set()

    finished = time.time()
    scan = {"market": market, "interval": interval, "rows": rows,
            "failed": sorted(failed), "asked_for": len(symbols),
            "cut_short": cut_short, "throttled": throttled,
            "seconds": finished - started,
            # When the oldest price on the list was read. A row can be reused
            # from a recent scan, so this can be earlier than the scan itself.
            "scanned_at": min([r.get("read_at") or started for r in rows] or [started]),
            "finished_at": finished}
    scan["complete"] = _is_complete(scan)
    return scan


def _shortlist(scan: Dict, horizon: str, risk: str, partial: bool = False,
               accounts_wait: Optional[float] = None) -> Dict:
    """Filter and rank a scan for one horizon and appetite. Cheap.

    partial is a scan still running. Neither it nor a finished scan that is not
    complete gets a rank, because a "number 1" of part of a list is not number
    1 of the list, and neither has its filings read, because the list will
    change. accounts_wait caps how long to wait for filings (None: the usual
    budget).
    """
    # Copies, because the scan is shared. A rank or a dropped reason written
    # here for one request must not turn up in somebody else's list.
    rows = [dict(r) for r in scan["rows"]]
    spec = RISKS[risk]
    complete = not partial and (scan["complete"] if "complete" in scan
                                else _is_complete(scan))
    cleared = [r for r in rows if _worth_showing(r)]

    # The appetite narrows what cleared the bar; it never lowers the bar. What
    # it excludes is counted so the page can say so rather than quietly
    # presenting a short list as though that were all there was.
    passed = [r for r in cleared if _matches_risk(r, spec)]
    wrong_shape = len(cleared) - len(passed)

    passed.sort(key=lambda r: (-(r.get(spec["rank_by"]) or 0),
                               -(r.get("expectancy") or 0),
                               -(r.get("conviction") or 0)))

    # Holding for months is a bet on the business, not on the pattern, so the
    # long option additionally reads the filings. Anything that fails is moved
    # out with the reason attached rather than silently dropped.
    dropped: List[Dict] = []
    if HORIZONS[horizon].get("needs_sound_accounts") and passed and complete:
        wanted = [c["symbol"] for c in passed if c.get("asset_class") != "crypto"]
        verdicts = (_check_accounts(wanted) if accounts_wait is None
                    else _check_accounts(wanted, accounts_wait))
        kept = []
        for candidate in passed:
            if candidate.get("asset_class") == "crypto":
                candidate["dropped_because"] = (
                    "A coin files no accounts, so there is nothing to check "
                    "before holding it for months.")
                dropped.append(candidate)
                continue
            if candidate["symbol"] not in verdicts:
                # Ran out of time rather than found nothing. Kept, and labelled
                # as unchecked, because "no filings" would be a false claim.
                candidate["accounts"] = "unchecked"
                kept.append(candidate)
                continue
            verdict = verdicts[candidate["symbol"]]
            if verdict is None:
                candidate["accounts"] = "not filed"
                kept.append(candidate)
            elif verdict.get("ok") is None:
                # Asked and not answered: kept, and labelled for what it is.
                candidate["accounts"] = "unreadable"
                kept.append(candidate)
            elif verdict["ok"]:
                candidate["accounts"] = "sound"
                kept.append(candidate)
            else:
                candidate["dropped_because"] = verdict["reasons"][0].capitalize() + "."
                dropped.append(candidate)
        passed = kept

    # Placed last, so the numbers run 1..N over what actually survives rather
    # than leaving gaps where something was dropped further up.
    for place, row in enumerate(passed, 1):
        row["rank"] = place if complete else None

    # A positive edge measured over years does not mean a setup is firing
    # today, and on most days none is. Without this second tier the page would
    # be empty almost every time, which is honest and useless. These are the
    # names where the approach has paid on this instrument's own history, with
    # nothing to act on right now: the thing to do with them is watch, not buy.
    # So only stand-asides belong here. A call that is firing but did not suit
    # the appetite is counted in wrong_shape, and one left out for its
    # accounts is listed with the reason; calling either "nothing firing"
    # would be false.
    elsewhere = {r["symbol"] for r in passed} | {r["symbol"] for r in dropped}
    # And only a record for buying: a bearish name's expectancy is a short's,
    # and "paid off before" read as a reason to buy it would be backwards.
    watch = [r for r in rows
             if r["action"] not in ACTIONABLE
             and r["symbol"] not in elsewhere
             and (r.get("odds_direction") or 1) > 0
             and (r.get("expectancy") or 0) > 0
             and (r.get("samples") or 0) >= 30]
    watch.sort(key=lambda r: -(r["expectancy"] or 0))

    avoided = [r for r in rows if r["action"] == "AVOID"]
    thin = [r for r in rows
            if r["action"] != "AVOID" and not _worth_showing(r)]

    return {
        "market": scan["market"],
        "horizon": horizon,
        "risk": risk,
        "risk_label": spec["label"],
        "ranked_as": spec["ranked_as"],
        "risk_note": spec["note"],
        "wrong_shape": wrong_shape,
        "interval": scan["interval"],
        "scanned": len(rows),
        "asked_for": scan["asked_for"],
        "cut_short": scan["cut_short"],
        "throttled": bool(scan.get("throttled")),
        "failed": list(scan.get("failed") or []),
        "partial": partial,
        "complete": complete,
        "passed": passed,
        # Counted before any list is cut for display, so a count shown beside a
        # shortened list is the real one.
        "passed_total": len(passed),
        "watch_total": len(watch),
        "watch": watch[:WATCH_SHOWN],
        "watch_shown": len(watch[:WATCH_SHOWN]),
        "dropped": dropped,
        "avoided": len(avoided),
        "thin": len(thin),
        "seconds": scan["seconds"],
        "scanned_at": scan["scanned_at"],
        "finished_at": scan.get("finished_at"),
        "warning": HORIZONS[horizon]["warning"],
        "summary": _summary(len(rows), passed, len(avoided), len(thin), horizon,
                            len(watch), risk, wrong_shape,
                            scan["cut_short"], scan["asked_for"],
                            len(scan.get("failed") or []), partial,
                            bool(scan.get("throttled")), complete, len(dropped)),
    }


def _summary(scanned: int, passed: List[Dict], avoided: int, thin: int,
             horizon: str, watching: int = 0, risk: str = "balanced",
             wrong_shape: int = 0, cut_short: bool = False,
             asked_for: int = 0, failed: int = 0, partial: bool = False,
             throttled: bool = False, complete: bool = True,
             left_out: int = 0) -> str:
    if partial:
        return ("Still checking: %d of %d looked at so far. What is below is "
                "only those, not ranked, and will change as the rest come in."
                % (scanned + failed, asked_for))
    text = _verdict(scanned, passed, avoided, thin, horizon, watching, risk,
                    wrong_shape, complete, left_out)
    if failed and scanned:
        text += (" %d could not be read from the data source, out of %d on its "
                 "list, so they are not in any list." % (failed, asked_for))
    # Said plainly, because a partial list read as a complete one is the quiet
    # kind of overstatement this app exists to avoid.
    if throttled:
        text += (" The data source started refusing requests after %d of the %d "
                 "instruments on its list, so the scan stopped there rather than "
                 "keep asking. The rest were not looked at, so something worth "
                 "seeing may be missing." % (scanned + failed, asked_for))
    elif cut_short:
        text += (" It stopped after %d of the %d instruments on its list, "
                 "because the data source stopped answering in time. The rest "
                 "were not looked at, so something worth seeing may be missing."
                 % (scanned + failed, asked_for))
    if not complete and scanned:
        text += (" With that much of the list unread this is not the whole "
                 "market, so nothing here is ranked. Ask again in a couple of "
                 "minutes for a fresh try.")
    return text


def _verdict(scanned: int, passed: List[Dict], avoided: int, thin: int,
             horizon: str, watching: int, risk: str, wrong_shape: int,
             complete: bool = True, left_out: int = 0) -> str:
    if not scanned:
        return ("Nothing could be read just now. The data source may be busy; "
                "ask again in a couple of minutes.")

    if not passed:
        text = ("Nothing to act on today out of %d looked at. A setup has to be "
                "firing now and have made money after costs on that "
                "instrument's own history, and nothing managed both."
                % scanned)
        if left_out:
            # These did clear the bar, so "nothing managed both" would be false.
            text = ("Nothing to hold for months out of %d looked at. %d did clear "
                    "the bar on the chart but %s left out, listed below with the "
                    "reason." % (scanned, left_out,
                                 "was" if left_out == 1 else "were"))
            if wrong_shape:
                text += (" %d more cleared it, but not with the payoff shape you "
                         "asked for." % wrong_shape)
        elif wrong_shape:
            text = ("Nothing matching %s out of %d looked at. %d did clear the "
                    "bar, but not with the payoff shape you asked for."
                    % (RISKS[risk]["label"].lower(), scanned, wrong_shape))
        if watching:
            text += (" %d of them have paid off historically, but today's "
                     "reading on them is to stand aside, so they are listed "
                     "below to watch rather than to buy." % watching)
        elif complete:
            # Only a scan that read the whole list can say this about today.
            text += " Doing nothing is a real answer, and today it is the answer."
        return text

    lead = ("%d of %d cleared the bar." % (len(passed), scanned))
    if len(passed) == 1:
        lead = "One of %d cleared the bar." % scanned
    if wrong_shape:
        lead += (" Another %d cleared it with a different payoff shape than "
                 "you asked for." % wrong_shape)
    if left_out:
        lead += (" %d more did on the chart but %s left out of a months hold, "
                 "listed below with the reason."
                 % (left_out, "was" if left_out == 1 else "were"))

    if horizon == "short":
        lead += (" Short holds pay trading costs most often, so treat even "
                 "these carefully.")
    else:
        lead += (" Clearing the bar means the setup has made money after costs "
                 "on this instrument's own history. It is a shortlist to look "
                 "into, not a list to buy.")
    return lead
