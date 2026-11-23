"""Not hammering somebody else's free API once there is more than one user.

On a laptop this layer does nothing visible. On a server it is the difference
between fifty people costing fifty upstream requests or costing one, and
between a rate limit degrading the app or hanging it.
"""
import os as _os
import sys as _sys
import threading
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

from bot import upstream

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


print("=" * 72)
print("IDENTICAL REQUESTS ARE MADE ONCE")
print("=" * 72)

upstream.reset()
calls = {"n": 0}


def counted():
    calls["n"] += 1
    return {"value": calls["n"]}


first = upstream.cached("k", counted)
second = upstream.cached("k", counted)
third = upstream.cached("k", counted)
check("three asks, one fetch", calls["n"] == 1, calls["n"])
check("and all three get the same answer",
      first == second == third == {"value": 1})

check("a different key is a different fetch",
      upstream.cached("other", counted) == {"value": 2})

# The cache must expire, or prices would freeze.
upstream.reset()
calls["n"] = 0
upstream.cached("k", counted, ttl=0.2)
time.sleep(0.35)
upstream.cached("k", counted, ttl=0.2)
check("a stale entry is refetched", calls["n"] == 2, calls["n"])

print()
print("=" * 72)
print("A STAMPEDE IS STILL ONE REQUEST")
print("=" * 72)

# This is the case caching alone does not solve: the cache is empty when all
# of them look, so without single-flight every one of them fetches.
upstream.reset()
slow_calls = {"n": 0}
started = threading.Barrier(13)


def slow():
    slow_calls["n"] += 1
    time.sleep(0.4)                      # pretend it is a network round trip
    return {"ok": True}


results = []
errors = []


def racer():
    try:
        started.wait(timeout=5)
        results.append(upstream.cached("hot", slow))
    except Exception as exc:             # a thread must not die silently
        errors.append(exc)


threads = [threading.Thread(target=racer) for _ in range(12)]
begin = time.time()
for t in threads:
    t.start()
started.wait(timeout=5)
for t in threads:
    t.join(timeout=15)
elapsed = time.time() - begin

print("   12 threads asked at once, took %.2fs" % elapsed)
print("   upstream was called %d time(s)" % slow_calls["n"])
check("no thread failed", not errors, errors[:1])
check("every thread got an answer", len(results) == 12, len(results))
check("they all got the same answer", all(r == {"ok": True} for r in results))
check("twelve simultaneous asks cost one upstream request",
      slow_calls["n"] == 1, slow_calls["n"])
check("and they waited rather than queueing serially",
      elapsed < 2.0, "%.2fs" % elapsed)

print()
print("=" * 72)
print("WHEN THE SOURCE SAYS STOP")
print("=" * 72)

upstream.reset()
HOST = "Test Source"

check("nothing is refused to begin with", upstream.check(HOST) is None)

# One refusal is noise, not a pattern.
upstream.note_throttled(HOST)
check("one refusal does not close the door", upstream.check(HOST) is None)
upstream.note_throttled(HOST)
check("two do not either", upstream.check(HOST) is None)

upstream.note_throttled(HOST)
try:
    upstream.check(HOST)
    check("three in a row closes it", False, "still open")
except upstream.Throttled as exc:
    check("three in a row closes it", True)
    print("   message: %s" % str(exc)[:100])
    # The host stays on the exception for the log and for Sentry, but out of
    # the sentence: a reader cannot act on a hostname, and it reads like the
    # app blaming a machine they have never heard of.
    check("the source is still on the exception for the log", exc.host == HOST)
    check("but not in what the reader is shown", HOST not in str(exc))
    check("it says roughly how long", exc.seconds > 0)
    check("and in a unit a person can act on",
          "minute" in str(exc) or "second" in str(exc) or "hour" in str(exc))
    check("and says it is not the reader's fault",
          "nothing to do with your account" in str(exc))

# While it is closed, no request is even attempted.
attempted = {"n": 0}


def must_not_run():
    attempted["n"] += 1
    return {}


try:
    upstream.cached("blocked", must_not_run, host=HOST)
    check("a closed breaker stops the request being sent", False, "it was sent")
except upstream.Throttled:
    check("a closed breaker stops the request being sent", attempted["n"] == 0)

# A success clears the count, so a single blip does not accumulate over hours.
upstream.reset()
upstream.note_throttled(HOST)
upstream.note_throttled(HOST)
upstream.note_ok(HOST)
upstream.note_throttled(HOST)
upstream.note_throttled(HOST)
check("a success resets the run of failures", upstream.check(HOST) is None)

# Retry-After is honoured rather than argued with.
upstream.reset()
for _ in range(3):
    upstream.note_throttled(HOST, retry_after=5.0)
try:
    upstream.check(HOST)
    check("a stated Retry-After is honoured", False)
except upstream.Throttled as exc:
    check("a stated Retry-After is honoured", 3 <= exc.seconds <= 6, exc.seconds)

# An absurd Retry-After must not lock the app out for a day.
upstream.reset()
for _ in range(3):
    upstream.note_throttled(HOST, retry_after=86400.0)
try:
    upstream.check(HOST)
    check("an absurd Retry-After is capped", False)
except upstream.Throttled as exc:
    check("an absurd Retry-After is capped", exc.seconds <= 900, exc.seconds)


class FakeResponse:
    def __init__(self, headers):
        self.headers = headers


check("Retry-After is read when present",
      upstream.retry_after_seconds(FakeResponse({"Retry-After": "30"})) == 30.0)
check("its absence is not an error",
      upstream.retry_after_seconds(FakeResponse({})) is None)
check("an HTTP-date Retry-After does not crash",
      upstream.retry_after_seconds(
          FakeResponse({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None)

for status, throttle in ((429, True), (503, True), (502, True),
                         (200, False), (404, False), (401, False)):
    check("%d is %streated as throttling" % (status, "" if throttle else "not "),
          upstream.is_throttle(status) is throttle)

print()
print("=" * 72)
print("A FAILING FETCH DOES NOT POISON THE CACHE")
print("=" * 72)

upstream.reset()
attempts = {"n": 0}


def flaky():
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise RuntimeError("upstream blew up")
    return {"recovered": True}


try:
    upstream.cached("flaky", flaky)
    check("the first failure propagates", False, "it was swallowed")
except RuntimeError:
    check("the first failure propagates", True)

check("and nothing bad was cached",
      upstream.cached("flaky", flaky) == {"recovered": True})
check("the in-flight marker was released", upstream.stats()["in_flight"] == 0)

print()
print("=" * 72)
print("A FAILURE IS SHARED WITH THOSE WAITING, NOT REPEATED BY EACH")
print("=" * 72)

# Callers waiting on a fetch that failed used to run it again themselves,
# past the breaker and without keeping the answer: eight people waiting on a
# request the source refused sent eight more.
upstream.reset()
sent = {"n": 0}


class Refused(RuntimeError):
    pass


def refusing():
    sent["n"] += 1
    time.sleep(0.4)
    raise Refused("the source said no")


told = []
gate = threading.Barrier(9)


def waits_on_refusal():
    gate.wait(timeout=5)
    try:
        upstream.cached("refused", refusing, host=HOST)
        told.append("answered")
    except Refused as exc:
        told.append(exc)


threads = [threading.Thread(target=waits_on_refusal) for _ in range(8)]
for t in threads:
    t.start()
gate.wait(timeout=5)
for t in threads:
    t.join(timeout=15)
check("eight callers waiting on a failed fetch cost one fetch", sent["n"] == 1, sent["n"])
check("and every one of them is told why it failed",
      len(told) == 8 and all(isinstance(e, Refused) for e in told), told)
check("nothing is left in flight", upstream.stats()["in_flight"] == 0)
check("and the failure is not kept for whoever asks next",
      upstream.cached("refused", lambda: "fine", host=HOST) == "fine")

# A waiter whose wait runs out while the fetch is still going asks for itself,
# which is the guard against a stuck fetch. It must not do so past a source
# that has said stop in the meantime.
upstream.reset()
sent["n"] = 0


def stuck():
    sent["n"] += 1
    for _ in range(upstream.THRESHOLD):
        upstream.note_throttled(HOST)
    time.sleep(0.6)
    return "late"


first = threading.Thread(target=lambda: upstream.cached("stuck", stuck, host=HOST, wait=0.1))
first.start()
time.sleep(0.1)
try:
    upstream.cached("stuck", stuck, host=HOST, wait=0.1)
    check("a waiter that gives up does not ask a source that has said stop", False,
          "it asked")
except upstream.Throttled:
    check("a waiter that gives up does not ask a source that has said stop",
          sent["n"] == 1, sent["n"])
first.join()

# An old answer stays in the cache until the next is stored. A waiter whose
# fetch failed was handed it, so one person saw an error page and the other an
# analysis built on prices hours old, shown as current.
upstream.reset()
upstream._cache["stale"] = (time.time() - 3 * 3600, "three hours old")


def refuses_slowly():
    time.sleep(0.4)
    raise Refused("the source said no")


got = []


def asks_for_stale():
    try:
        got.append(upstream.cached("stale", refuses_slowly))
    except Refused:
        got.append("told it failed")


threads = [threading.Thread(target=asks_for_stale) for _ in range(2)]
for t in threads:
    t.start()
    time.sleep(0.1)
for t in threads:
    t.join()
check("a waiter whose fetch failed is told so, not handed an expired answer",
      got == ["told it failed"] * 2, got)

print()
print("=" * 72)
print("A SOURCE THAT STOPS ANSWERING IS LEFT BE, AND TRIED AGAIN BY ONE")
print("=" * 72)


def refused_by(host):
    try:
        upstream.check(host)
    except upstream.Throttled as exc:
        return exc
    return None


# Timeouts never counted, so during an outage every request held a server
# thread for its full time.
upstream.reset()
for _ in range(upstream.THRESHOLD):
    upstream.note_failed(HOST)
exc = refused_by(HOST)
check("requests that got no answer close the door, as refusals do", exc is not None)
check("and the words are about a source not answering, not a limit people share",
      exc is not None and "not answering" in str(exc) and "rate limit" not in str(exc)
      and "shared" not in str(exc), str(exc))
check("the status page can say which it was", upstream.stats()["hosts"][HOST]["why"] == "down")

upstream.reset()
for _ in range(upstream.THRESHOLD):
    upstream.note_throttled(HOST, 5.0, 503)
exc = refused_by(HOST)
check("a source that answers it is down is not called a rate limit",
      exc is not None and "rate limiting" not in str(exc), str(exc))
check("one second is one second", "1 second" in str(upstream.Throttled(HOST, 1))
      and "1 seconds" not in str(upstream.Throttled(HOST, 1)))


def wait_over():
    """The wait is over, as if its seconds had passed."""
    upstream._breakers[HOST]["open_until"] = time.time() - 1


# After the wait three more requests used to be sent before the door shut.
real_wait = upstream.PROBE_WAIT
upstream.PROBE_WAIT = 0.3
try:
    wait_over()
    check("after the wait one request may go", refused_by(HOST) is None)
    check("and the next is held back until it is known what that one found",
          upstream.refusing(HOST) and refused_by(HOST) is not None)
    upstream.note_throttled(HOST, 5.0, 503)
    check("one refusal then is enough to shut the door again",
          refused_by(HOST) is not None and upstream.stats()["hosts"][HOST]["open_for"] > 0)

    wait_over()
    check("looking whether the door is open takes nobody's place",
          not upstream.refusing(HOST) and not upstream.refusing(HOST))
    upstream.PROBE_WAIT = 5.0
    check("the next wait over, one goes again", refused_by(HOST) is None)
    held = []
    waiter = threading.Thread(target=lambda: held.append((refused_by(HOST), time.time())))
    waiter.start()
    time.sleep(0.2)
    waiting = not held
    answered = time.time()
    upstream.note_ok(HOST)
    waiter.join(timeout=10)
    check("when it is answered, whoever was held back goes straight on",
          waiting and held and held[0][0] is None and held[0][1] - answered < 2.0, held)
    check("and the door is open to everyone", not upstream.refusing(HOST)
          and refused_by(HOST) is None)

    # A request that shows neither (an error of its own) lets another find out.
    for _ in range(upstream.THRESHOLD):
        upstream.note_failed(HOST)
    wait_over()

    def breaks():
        raise ValueError("something else")

    try:
        upstream.cached("odd", breaks, host=HOST)
    except ValueError:
        pass
    check("a first request that settles nothing does not keep the door shut",
          refused_by(HOST) is None)
finally:
    upstream.PROBE_WAIT = real_wait
    upstream.reset()

print()
print("=" * 72)
print("OLD ANSWERS ARE LET GO")
print("=" * 72)

# Each answer can be a megabyte of price history. Expired ones used to stay
# until four hundred others pushed them out.
upstream.reset()
upstream.cached("old", lambda: "x", ttl=0.2)
time.sleep(0.3)
upstream.cached("new", lambda: "y", ttl=0.2)
check("an answer nobody can reuse is dropped when the next is stored",
      "old" not in upstream._cache, sorted(upstream._cache))
check("and the new one is kept", "new" in upstream._cache)

upstream.reset()
upstream.cached("long", lambda: 1, ttl=60)
upstream.cached("short", lambda: 2, ttl=0.1)
time.sleep(0.2)
upstream.cached("later", lambda: 3, ttl=0.1)
check("an answer some caller may still reuse is kept",
      "long" in upstream._cache and "short" in upstream._cache, sorted(upstream._cache))

upstream.reset()
total = upstream.MAX_ENTRIES + 50
for n in range(total):
    upstream.cached("k%d" % n, lambda: n, ttl=60)
check("the cache never holds more than its limit",
      upstream.stats()["cached_keys"] == upstream.MAX_ENTRIES,
      upstream.stats()["cached_keys"])
check("and what it lets go is the oldest",
      "k0" not in upstream._cache and "k%d" % (total - 1) in upstream._cache)

print()
print("=" * 72)
print("A FAILING YAHOO HANDSHAKE IS TRIED ONCE, NOT ONCE PER CALLER")
print("=" * 72)

from types import SimpleNamespace  # noqa: E402

from bot import yahoo


class HandshakeAnswer:
    def __init__(self, status, text):
        self.status_code, self.text, self.headers = status, text, {}


class FakeSession:
    """Stands in for requests.Session, so nothing here touches the network."""
    calls = []
    answer = (200, "<html>a consent page, not a crumb</html>")
    delay = 0.3

    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None, **_):
        FakeSession.calls.append((url, timeout))
        time.sleep(FakeSession.delay)
        return HandshakeAnswer(*FakeSession.answer)


def fresh_handshake():
    yahoo._SESSION, yahoo._CRUMB = None, None
    yahoo._OBTAINED_AT = yahoo._FAILED_AT = 0.0
    FakeSession.calls = []


real_session = yahoo.requests.Session
yahoo.requests.Session = FakeSession
try:
    one = len(yahoo._SEED_URLS) + len(yahoo._CRUMB_URLS)
    fresh_handshake()
    told = []

    def asks():
        try:
            yahoo._ensure()
            told.append("ok")
        except yahoo.YahooError:
            told.append("refused")

    threads = [threading.Thread(target=asks) for _ in range(4)]
    begin = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - begin
    print("   4 callers, %d requests, %.2fs" % (len(FakeSession.calls), elapsed))
    check("four callers cost one handshake", len(FakeSession.calls) == one,
          len(FakeSession.calls))
    check("and all four are told it failed", told == ["refused"] * 4, told)
    check("none of them waited for a handshake of its own",
          elapsed < 2 * one * FakeSession.delay, "%.2fs" % elapsed)
    check("each handshake request gives up after a few seconds",
          all(t is not None and t <= 5 for _, t in FakeSession.calls),
          FakeSession.calls)

    FakeSession.calls = []
    begin = time.time()
    try:
        yahoo._ensure()
        check("asking again at once is refused", False, "it was allowed")
    except yahoo.YahooError:
        check("asking again at once is refused without a request",
              not FakeSession.calls and time.time() - begin < 0.1)

    # A crumb past its half hour usually still works, so a failed refresh
    # keeps using it instead of failing every page for a minute.
    fresh_handshake()
    yahoo._SESSION, yahoo._CRUMB = FakeSession(), "old-crumb"
    yahoo._OBTAINED_AT = time.time() - yahoo._TTL - 10
    try:
        yahoo._ensure()
        kept = yahoo._CRUMB == "old-crumb"
    except yahoo.YahooError:
        kept = False
    check("a failed refresh keeps the crumb it had", kept)
    check("after really trying to refresh it", len(FakeSession.calls) == one,
          len(FakeSession.calls))

    # Refused for rate limiting while getting the token: the same limit the
    # data requests would hit, so it counts towards the breaker.
    fresh_handshake()
    upstream.reset()
    FakeSession.answer, FakeSession.delay = (429, ""), 0.0
    for _ in range(upstream.THRESHOLD):
        yahoo._FAILED_AT = 0.0
        try:
            yahoo._ensure()
        except yahoo.YahooError:
            pass
    try:
        upstream.check(yahoo._HOST)
        check("rate limiting during the handshake closes the breaker", False)
    except upstream.Throttled:
        check("rate limiting during the handshake closes the breaker", True)
    check("and the second address is not tried after a refusal",
          len(FakeSession.calls) == upstream.THRESHOLD * (len(yahoo._SEED_URLS) + 1),
          len(FakeSession.calls))

    # Two callers refused with the same expired crumb: the second finds it
    # already replaced and does not shake hands again.
    fresh_handshake()
    upstream.reset()
    yahoo._SESSION, yahoo._CRUMB = FakeSession(), "renewed"
    yahoo._OBTAINED_AT = time.time()
    yahoo._ensure(force=True, seen="expired")
    check("a crumb another caller already renewed is not renewed again",
          not FakeSession.calls and yahoo._CRUMB == "renewed")

    # A dropped connection's own text holds the address asked, with the session
    # token in it, and a screen's notes showed it to visitors.
    class Dropped(FakeSession):
        error = yahoo.requests.ConnectionError(
            "HTTPSConnectionPool(host='query1.finance.yahoo.com', port=443): Max retries "
            "exceeded with url: /v7/finance/quote?crumb=sEcReT (Caused by [Errno 11001])")

        def get(self, url, params=None, timeout=None, **_):
            FakeSession.calls.append((url, timeout))
            raise Dropped.error

    pauses = []
    real_time, yahoo.time = yahoo.time, SimpleNamespace(time=time.time, sleep=pauses.append)

    def said_by_yahoo():
        FakeSession.calls, pauses[:] = [], []
        try:
            yahoo.get("https://query1.finance.yahoo.com/v7/finance/quote", {"symbols": "AAPL"})
        except yahoo.YahooError as exc:
            return str(exc)
        return ""

    try:
        fresh_handshake()
        upstream.reset()
        yahoo._SESSION, yahoo._CRUMB = Dropped(), "sEcReT"
        yahoo._OBTAINED_AT = time.time()
        said = said_by_yahoo()
        check("a dropped connection is said in words, with no address or token",
              said == "Yahoo could not be reached.", said)
        check("with no pause after the last try", len(pauses) == len(FakeSession.calls) - 1,
              (pauses, len(FakeSession.calls)))
        Dropped.error = yahoo.requests.Timeout("read timed out")
        check("and a slow one as slow", said_by_yahoo() == "Yahoo took too long to answer.")
        said_by_yahoo()
        check("three requests with no answer close the door, and the next is not sent",
              "not answering" in said_by_yahoo() and not FakeSession.calls, FakeSession.calls)
    finally:
        yahoo.time = real_time

    slowest = (sum(yahoo._TIMEOUT) * yahoo._TRIES + 0.5
               + yahoo._TRIES * one * yahoo._HANDSHAKE_TIMEOUT)
    check("callers wait longer than a Yahoo request can take", slowest < yahoo._WAIT, slowest)

    asked_at = []
    real_get, yahoo.get = yahoo.get, lambda url, params=None: asked_at.append(url) or {}
    try:
        for ask in (lambda: yahoo.quote_summary("AAPL#X", "price"),
                    lambda: yahoo.options("AAPL?X=1")):
            try:
                ask()
            except yahoo.YahooError:
                pass
    finally:
        yahoo.get = real_get
    check("a # or ? in a ticker stays part of it",
          [a.rsplit("/", 1)[-1] for a in asked_at] == ["AAPL%23X", "AAPL%3FX=1"], asked_at)
finally:
    yahoo.requests.Session = real_session
    yahoo._SESSION, yahoo._CRUMB = None, None
    yahoo._OBTAINED_AT = yahoo._FAILED_AT = 0.0
    upstream.reset()

print()
print("=" * 72)
print("AGAINST THE REAL SOURCE")
print("=" * 72)

upstream.reset()
from bot.market import fetch_bars

begin = time.time()
one = fetch_bars("AAPL", "1d")
cold = time.time() - begin

begin = time.time()
two = fetch_bars("AAPL", "1d")
warm = time.time() - begin

print("   cold %.2fs, warm %.3fs" % (cold, warm))
check("the same bars come back", one.last_price == two.last_price)
check("the second ask does not hit the network", warm < cold / 2 or warm < 0.05,
      "%.3fs vs %.3fs" % (warm, cold))
check("something is cached", upstream.stats()["cached_keys"] >= 1)

# Different instruments and different intervals must not collide.
other = fetch_bars("MSFT", "1d")
check("a different instrument is fetched separately",
      other.symbol == "MSFT" and one.symbol == "AAPL")
check("each is cached under its own key", upstream.stats()["cached_keys"] >= 2)

print()
print("%d failure(s)" % len(fails))
# A fetch slower than the waiter's patience used to mean the waiter gave up,
# found an empty cache and ran the whole thing again. That is the stampede
# single-flight exists to prevent, and it only showed up when two people asked
# at once.
upstream.reset()
calls = []


def _slow():
    calls.append(1)
    time.sleep(1.2)
    return "answer"


results = []
threads = [threading.Thread(
    target=lambda: results.append(
        upstream.cached("slow", _slow, ttl=60, wait=5.0)))
    for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("a slow fetch runs once for four callers", len(calls) == 1, len(calls))
check("and all four get the answer",
      results == ["answer"] * 4, results)

upstream.reset()
impatient = []


def _slower():
    impatient.append(1)
    time.sleep(1.2)
    return "answer"


out = []
threads = [threading.Thread(
    target=lambda: out.append(
        upstream.cached("impatient", _slower, ttl=60, wait=0.2)))
    for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("a waiter that gives up early does refetch, which is why wait matters",
      len(impatient) == 2, len(impatient))

print()
print("=" * 72)
print("THE NEWS SEARCH SHARES YAHOO'S DOOR")
print("=" * 72)
# Yahoo's news search went round the breaker: a refusal there was not told to
# it, and it kept asking while the price requests stood back.
from bot import sentiment  # noqa: E402

real_get = sentiment.requests.get
asked = []


def fake_get(url, params=None, headers=None, timeout=None):
    asked.append(url)
    if url == sentiment._YAHOO_SEARCH:
        return SimpleNamespace(status_code=429, headers={"Retry-After": "120"},
                               content=b"", json=lambda: {})
    return SimpleNamespace(status_code=404, headers={}, content=b"", json=lambda: {})


sentiment.requests.get = fake_get
try:
    upstream.reset()
    for _ in range(3):
        sentiment.gather_news("AAPL", "Apple")
    try:
        upstream.check(sentiment._YAHOO_HOST)
        closed = False
    except upstream.Throttled:
        closed = True
    check("refusals on the news search close the door for Yahoo's prices too", closed)
    del asked[:]
    sentiment.gather_news("AAPL", "Apple")
    check("and while it is closed the news search asks nothing",
          sentiment._YAHOO_SEARCH not in asked, asked)
finally:
    sentiment.requests.get = real_get
    upstream.reset()

print("UPSTREAM OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
