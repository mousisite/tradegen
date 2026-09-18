"""Being a good citizen of somebody else's free API.

On one person's laptop this does not matter. The moment the app is on a server
with a handful of users it matters a great deal, because every request now
leaves from one address and the free data sources count requests per address,
not per person.

Three problems, which compound:

1. **Repetition.** Ten people analysing SPY fetch the same bars ten times. The
   portfolio page, the overlap check, the correlation matrix and the alert loop
   each fetch them again. Nothing about those bars changed in between.

2. **Stampede.** Ten simultaneous requests for the same thing are ten upstream
   requests, because each starts before any finishes. Caching alone does not
   fix this; the cache is still empty when all ten look.

3. **Cascade.** When the source starts refusing, the naive response is to
   retry, which is exactly the wrong thing. Every retry deepens the hole, every
   thread sits in a sleep, and the app appears hung rather than degraded.

So: cache identical requests briefly, let only one caller actually fetch while
the others wait for its answer, and when the source says stop, stop asking for
a while and say so plainly instead of pretending it is a mystery.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple

# How long an identical response may be reused. Short on purpose: this exists
# to collapse the burst of duplicate requests a single page view causes across
# several modules, not to serve stale prices. A minute of staleness is
# invisible on daily bars and acceptable on intraday ones, where the analysis
# layer caches for five minutes anyway.
DEFAULT_TTL = 45.0

# Once the source starts refusing, how long to stop asking. Long enough that a
# burst of traffic does not keep the door shut, short enough that a user who
# waits and retries gets through.
COOLDOWN = 60.0

# Refusals in a row before the breaker opens. One 429 is noise; three in a row
# is a message.
THRESHOLD = 3

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, object]] = {}
_inflight: Dict[str, threading.Event] = {}
_breakers: Dict[str, Dict] = {}


def _plainly(seconds: int) -> str:
    """A wait a person can act on. Nobody plans around 847 seconds."""
    if seconds < 90:
        return "%d seconds" % seconds
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    hours = seconds / 3600.0
    return "%.1f hours" % hours


class Throttled(RuntimeError):
    """The source is refusing, and it is not this request's fault."""

    def __init__(self, host: str, seconds: float):
        self.host = host
        self.seconds = max(1, int(round(seconds)))
        super().__init__(
            "The market data source is rate limiting this site, so the "
            "request was not sent rather than being sent and failing. Try "
            "again in about %s. The limit is shared by everyone using the "
            "site right now, so it is nothing to do with your account."
            % _plainly(self.seconds))


# --- the circuit breaker ----------------------------------------------------

def _breaker(host: str) -> Dict:
    state = _breakers.get(host)
    if state is None:
        state = {"failures": 0, "open_until": 0.0, "opened": 0}
        _breakers[host] = state
    return state


def check(host: str) -> None:
    """Raise rather than send a request the source is currently refusing."""
    with _lock:
        state = _breaker(host)
        remaining = state["open_until"] - time.time()
    if remaining > 0:
        raise Throttled(host, remaining)


def note_throttled(host: str, retry_after: Optional[float] = None) -> None:
    """Record a refusal, and open the breaker once they stop looking like noise."""
    with _lock:
        state = _breaker(host)
        state["failures"] += 1
        if state["failures"] >= THRESHOLD:
            wait = retry_after if retry_after and retry_after > 0 else COOLDOWN
            # Honour a stated Retry-After even when it is long: arguing with it
            # is how an address gets blocked rather than throttled.
            state["open_until"] = time.time() + min(float(wait), 900.0)
            state["opened"] += 1
            state["failures"] = 0


def note_ok(host: str) -> None:
    """A success means whatever was wrong has passed."""
    with _lock:
        state = _breaker(host)
        if state["failures"]:
            state["failures"] = 0


def retry_after_seconds(response) -> Optional[float]:
    """The Retry-After header, as seconds, when the source sends one."""
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # The header may also be an HTTP date. Not worth parsing precisely;
        # the default cooldown is a reasonable answer.
        return None


def is_throttle(status: int) -> bool:
    """Statuses that mean "stop asking" rather than "that request was wrong"."""
    return status in (429, 503, 502)


# --- cached, de-duplicated fetching -----------------------------------------

def cached(key: str, fetch: Callable[[], object], ttl: float = DEFAULT_TTL,
           host: str = "", wait: float = 30.0) -> object:
    """Return a cached answer, or fetch one while everyone else waits.

    The waiting is the point. Without it, ten simultaneous callers for the same
    key are ten upstream requests, because the cache is still empty when each
    of them looks.

    `wait` has to exceed how long `fetch` actually takes. A waiter that gives
    up early finds the cache still empty and runs the whole fetch itself, which
    is the stampede this exists to prevent, and it does it at the worst moment:
    the defect is invisible until two people ask at once.
    """
    now = time.time()

    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]

        waiting = _inflight.get(key)
        if waiting is None:
            # This caller does the work; others will wait on this event.
            event = threading.Event()
            _inflight[key] = event
            mine = True
        else:
            event, mine = waiting, False

    if not mine:
        # Somebody else is already fetching this. Wait for them rather than
        # asking the source the same question at the same moment.
        event.wait(timeout=wait)
        with _lock:
            hit = _cache.get(key)
        if hit:
            return hit[1]
        # The other caller failed. Fall through and try once, rather than
        # failing on somebody else's behalf.
        return fetch()

    try:
        if host:
            check(host)
        result = fetch()
        with _lock:
            _cache[key] = (time.time(), result)
            if len(_cache) > 400:
                oldest = min(_cache, key=lambda k: _cache[k][0])
                _cache.pop(oldest, None)
        return result
    finally:
        with _lock:
            _inflight.pop(key, None)
        event.set()


def stats() -> Dict:
    """What the layer is doing, for a status page or a test."""
    with _lock:
        return {
            "cached_keys": len(_cache),
            "in_flight": len(_inflight),
            "hosts": {
                host: {
                    "failing": state["failures"],
                    "open_for": max(0.0, state["open_until"] - time.time()),
                    "times_opened": state["opened"],
                }
                for host, state in _breakers.items()
            },
        }


def reset() -> None:
    """Forget everything. For tests, and for a deliberate retry after a pause."""
    with _lock:
        _cache.clear()
        _inflight.clear()
        _breakers.clear()
