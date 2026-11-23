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

# The same for a source that is not answering at all. Shorter: nothing has
# asked for a wait, and one request is enough to find out it is back.
DOWN_COOLDOWN = 30.0

# After the wait, one request goes first to see whether the source is back.
# The rest wait this long for what it finds, and if it has not settled in
# PROBE_FOR seconds another may go.
PROBE_WAIT = 3.0
PROBE_FOR = 30.0

# Refusals in a row before the breaker opens. One 429 is noise; three in a row
# is a message.
THRESHOLD = 3

# Most answers kept at once. One parsed price history can be over a megabyte,
# the server has half a gigabyte, and the cache is only here to collapse the
# repeats of a single page view, which never need more than a few dozen.
MAX_ENTRIES = 100

_lock = threading.Lock()
# Told when a request sent to see whether the source is back has settled.
_settled = threading.Condition(_lock)
_cache: Dict[str, Tuple[float, object]] = {}
# One entry per question being asked right now: an event set when the answer
# is in, and the error, when asking failed.
_inflight: Dict[str, Dict] = {}
_breakers: Dict[str, Dict] = {}
# The longest reuse any caller has asked for. An entry older than that can
# never be returned again, so it is let go rather than kept until pushed out.
_max_ttl = 0.0


def _plainly(seconds: int) -> str:
    """A wait a person can act on. Nobody plans around 847 seconds."""
    if seconds < 90:
        return "%d second%s" % (seconds, "" if seconds == 1 else "s")
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    hours = seconds / 3600.0
    return "%.1f hours" % hours


class Throttled(RuntimeError):
    """The source is refusing, and it is not this request's fault.

    `why` is "limit" for a source that said it is being asked too often, and
    "down" for one that is busy or not answering. Only the first is a limit
    people share.
    """

    def __init__(self, host: str, seconds: float, why: str = "limit"):
        self.host = host
        self.seconds = max(1, int(round(seconds)))
        self.why = why
        if why == "down":
            super().__init__(
                "The market data source is not answering, so the request was "
                "not sent rather than being left to time out. Try again in "
                "about %s. It is nothing to do with your account."
                % _plainly(self.seconds))
            return
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
        # "tripped" from the moment the breaker opens until a request
        # succeeds, so once the wait is over one request goes first
        # ("probe_until" is how long it has) and a single refusal shuts the
        # door again.
        state = {"failures": 0, "open_until": 0.0, "opened": 0,
                 "tripped": False, "probe_until": 0.0, "why": "limit"}
        _breakers[host] = state
    return state


def check(host: str) -> None:
    """Raise rather than send a request the source is currently refusing.

    Call it once for each request about to be sent: after a wait the first
    caller here is the one whose request finds out whether the source is back.
    """
    with _lock:
        give_up = time.time() + PROBE_WAIT
        while True:
            state = _breaker(host)
            now = time.time()
            why = state.get("why", "limit")
            if state["open_until"] > now:
                raise Throttled(host, state["open_until"] - now, why)
            if not state.get("tripped"):
                return
            probe_until = state.get("probe_until", 0.0)
            if probe_until <= now:
                state["probe_until"] = now + PROBE_FOR
                return
            if now >= give_up:
                raise Throttled(host, min(probe_until - now, 10.0), why)
            _settled.wait(min(give_up, probe_until) - now)


def refusing(host: str) -> bool:
    """Whether a request would be turned away just now. Sends nothing, waits
    for nothing, and takes nobody's place."""
    with _lock:
        state = _breaker(host)
        now = time.time()
        return (state["open_until"] > now
                or bool(state.get("tripped")) and state.get("probe_until", 0.0) > now)


def _failed(host: str, wait: float, why: str) -> None:
    with _settled:
        state = _breaker(host)
        state["failures"] += 1
        if state["failures"] >= THRESHOLD or state.get("tripped"):
            now = time.time()
            if state["open_until"] <= now:
                state["opened"] += 1
                state["why"] = why
            # Never shorter than a wait already being kept.
            state["open_until"] = max(state["open_until"], now + min(wait, 900.0))
            state["tripped"] = True
            state["failures"] = 0
        state["probe_until"] = 0.0
        _settled.notify_all()


def note_throttled(host: str, retry_after: Optional[float] = None,
                   status: int = 429) -> None:
    """Record a refusal, and open the breaker once they stop looking like noise."""
    # Honour a stated Retry-After even when it is long: arguing with it is how
    # an address gets blocked rather than throttled.
    _failed(host, float(retry_after) if retry_after and retry_after > 0 else COOLDOWN,
            "limit" if status == 429 else "down")


def note_failed(host: str) -> None:
    """Record a request that got no answer at all, on any of its tries.

    Each one holds a server thread for as long as it is waited for, so with
    the source down every page waited its full time. Enough in a row and
    nothing is sent for a while.
    """
    _failed(host, DOWN_COOLDOWN, "down")


def note_ok(host: str) -> None:
    """A success means whatever was wrong has passed."""
    with _settled:
        state = _breaker(host)
        state["failures"] = 0
        # One sent before the door shut says nothing about now.
        if state["open_until"] <= time.time():
            state["tripped"] = False
            state["probe_until"] = 0.0
            _settled.notify_all()


def note_unclear(host: str) -> None:
    """A request ended without showing whether the source is back, so the next
    caller may find out instead."""
    with _settled:
        _breaker(host)["probe_until"] = 0.0
        _settled.notify_all()


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

    A failure is shared the same way. Those waiting get the error the fetch
    raised rather than each asking again: eight people waiting on a request
    the source refused used to send eight more, and on a rate limit every one
    of them keeps the door shut longer.
    """
    global _max_ttl
    now = time.time()

    with _lock:
        _max_ttl = max(_max_ttl, float(ttl))
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]

        flight = _inflight.get(key)
        mine = flight is None
        if mine:
            # This caller does the work; others will wait for its answer.
            flight = {"done": threading.Event(), "error": None}
            _inflight[key] = flight

    def ask() -> object:
        if host:
            check(host)
        try:
            return fetch()
        except BaseException:
            if host:
                note_unclear(host)
            raise

    if not mine:
        # Somebody else is already fetching this. Wait for them rather than
        # asking the source the same question at the same moment.
        finished = flight["done"].wait(timeout=wait)
        with _lock:
            hit = _cache.get(key)
        # Only a fresh answer: when the fetch failed, what is kept under this
        # key is an old one, and old prices were handed out as current.
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        if finished and flight["error"] is not None:
            raise flight["error"]
        # Still not answered after `wait`, so the other caller is stuck. Ask
        # once here, but not past a source that has said stop in the meantime,
        # and keep what comes back for the next caller.
        result = ask()
        with _lock:
            _store(key, result)
        return result

    try:
        result = ask()
        with _lock:
            _store(key, result)
        return result
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        with _lock:
            _inflight.pop(key, None)
        flight["done"].set()


def _store(key: str, result: object) -> None:
    """Keep an answer and let go of what can no longer be served. Lock held."""
    now = time.time()
    _cache[key] = (now, result)
    # Expired entries used to stay until four hundred others pushed them out,
    # which at a megabyte or more each could outgrow the whole server.
    for old in [k for k, (at, _) in _cache.items() if now - at >= _max_ttl]:
        if old != key:
            del _cache[old]
    while len(_cache) > MAX_ENTRIES:
        del _cache[min((k for k in _cache if k != key),
                       key=lambda k: _cache[k][0])]


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
                    "why": state.get("why", "limit"),
                    "times_opened": state["opened"],
                }
                for host, state in _breakers.items()
            },
        }


def reset() -> None:
    """Forget everything. For tests, and for a deliberate retry after a pause."""
    global _max_ttl
    with _lock:
        _cache.clear()
        _inflight.clear()
        _breakers.clear()
        _max_ttl = 0.0
