"""Background checking, so an alert is an alert rather than a bookmark.

An alert that is only evaluated when you happen to open the page is not an
alert. This runs a loop inside the web server process that checks the active
alerts on a timer, records what fired, and keeps enough history for the page to
say when it last actually looked.

What it deliberately does not do is pretend to be a service. Nothing here runs
when the server is stopped, and the interface says so in those words rather
than letting someone believe a level is being watched overnight. Making that
true needs the app hosted somewhere that stays up, which is a different problem
from this file.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional


class Scheduler:
    """A single background thread running one job on an interval.

    Kept deliberately small: one job, one thread, no queue. The work it does is
    a handful of network requests every few minutes, and anything more elaborate
    would be more machinery than the problem has.
    """

    def __init__(self, job: Callable[[], Dict], interval_seconds: float = 300.0,
                 name: str = "stockbot-scheduler",
                 on_error: Optional[Callable[[BaseException], None]] = None):
        self._job = job
        self._interval = max(30.0, float(interval_seconds))
        self._name = name
        self._on_error = on_error
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.started_at: Optional[float] = None
        self.last_run: Optional[float] = None
        self.last_error: str = ""
        self.runs = 0
        self.failures = 0
        # Bounded, because this lives for as long as the server does.
        self.history: Deque[Dict] = deque(maxlen=50)
        self.recent_fires: Deque[Dict] = deque(maxlen=50)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Begin checking. Returns False if it was already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._loop, name=self._name,
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def check_now(self) -> None:
        """Run the job without waiting for the next tick."""
        self._wake.set()

    def set_interval(self, seconds: float) -> None:
        self._interval = max(30.0, float(seconds))
        self._wake.set()

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        # Wait one interval before the first run. Checking the instant the
        # server starts would fire alerts against a market the user has not
        # looked at yet, and the page checks on load anyway.
        while not self._stop.is_set():
            woken = self._wake.wait(timeout=self._interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            self._run_once(triggered="manual" if woken else "timer")

    @staticmethod
    def _as_dict(item) -> Dict:
        """A fired alert as plain data, whatever shape the job returned it in.

        The job is free to return dataclasses; this has to survive being called
        with any of them and must never be the thing that kills the loop.
        """
        if isinstance(item, dict):
            return dict(item)
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.asdict(item)
        return {"detail": str(item)}

    def _run_once(self, triggered: str = "timer") -> None:
        """Run the job and record it. Nothing in here may raise.

        The whole body is guarded, not just the job call: a loop that dies
        while writing down what happened is exactly as broken as one that dies
        doing the work, and is harder to notice because the last thing it
        recorded says it succeeded.
        """
        started = time.time()
        try:
            result = self._job() or {}
            fired = [self._as_dict(x) for x in (result.get("fired") or [])]

            self.runs += 1
            self.last_run = time.time()
            self.last_error = ""
            for entry in fired:
                entry["at"] = self.last_run
                self.recent_fires.appendleft(entry)
            self.history.appendleft({
                "at": started,
                "ok": True,
                "seconds": self.last_run - started,
                "checked": result.get("checked", 0),
                "fired": len(fired),
                "errors": len(result.get("errors") or []),
                "triggered": triggered,
            })
        except BaseException as exc:               # a loop must not die
            self.failures += 1
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            try:
                self.history.appendleft({"at": started, "ok": False,
                                         "seconds": time.time() - started,
                                         "detail": self.last_error,
                                         "triggered": triggered})
            except Exception:
                pass
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception:
                    pass

    # -- what the page needs to say ----------------------------------------

    def status(self) -> Dict:
        return {
            "running": self.running,
            "interval": self._interval,
            "started_at": self.started_at,
            "last_run": self.last_run,
            "next_run": (self.last_run + self._interval) if self.last_run
                        else ((self.started_at + self._interval)
                              if self.started_at else None),
            "runs": self.runs,
            "failures": self.failures,
            "last_error": self.last_error,
            "history": list(self.history)[:10],
            "recent_fires": list(self.recent_fires)[:10],
        }

    def unseen_fires(self, since: float) -> List[Dict]:
        """Alerts that fired after a given moment, newest first."""
        return [f for f in self.recent_fires if f.get("at", 0) > since]
