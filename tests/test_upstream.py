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

print("UPSTREAM OK" if not fails else "FAILURES: %s" % fails)

import sys as _exit_sys
_exit_sys.exit(1 if fails else 0)
