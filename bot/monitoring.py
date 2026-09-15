"""Knowing when it broke, without waiting for someone to tell you.

An app nobody is watching fails silently. The first you hear of a 500 at three
in the morning is a user's email on Monday, if they bother, which most will
not; they will simply leave.

Sentry is optional and off unless a DSN is configured, so nothing here changes
how the app behaves on your own machine. What it does add, when enabled, is a
notification the moment something raises, with the traceback and the request
that caused it.

Two rules are enforced here rather than left to configuration, because getting
them wrong is worse than having no monitoring at all:

  Personal data never leaves. Trades, holdings, saved theses and email
  addresses are not error-report material. `send_default_pii` stays off and
  anything that looks like a credential is stripped before sending.

  Expected failures are not errors. A rate-limited data source and an unknown
  ticker are conditions the app already handles and explains. Reporting them
  trains you to ignore the alerts, which is how a real error gets missed.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

_enabled = False
_reason = "not initialised"

# Values that must never appear in an error report, whatever the key is called.
_SECRETISH = re.compile(
    r"(secret|password|token|api[_-]?key|authorization|cookie|crumb|client[_-]?secret)",
    re.I)

# Exceptions that mean "the world is as expected and the app handled it".
_EXPECTED = (
    "DataError",        # unknown ticker, or the source had nothing
    "Throttled",        # a rate limit, already surfaced to the user
    "YahooError",       # upstream refused; the page says so
    "AuthError",        # a sign-in that did not complete
    "NotFound", "MethodNotAllowed", "BadRequest", "Forbidden",
)


def _scrub(event: Dict, _hint: Optional[Dict] = None) -> Optional[Dict]:
    """Strip anything personal or secret before the report leaves the process."""
    try:
        request = event.get("request") or {}
        request.pop("cookies", None)
        request.pop("data", None)

        headers = request.get("headers") or {}
        for key in list(headers):
            if _SECRETISH.search(key):
                headers[key] = "[removed]"
        request["headers"] = headers

        # A query string can carry a symbol, which is fine, and a session
        # token, which is not.
        query = request.get("query_string")
        if isinstance(query, str) and _SECRETISH.search(query):
            request["query_string"] = "[removed]"
        event["request"] = request

        # The user is identified by an opaque id only. An email address is not
        # needed to fix a bug.
        user = event.get("user") or {}
        user.pop("email", None)
        user.pop("username", None)
        user.pop("ip_address", None)
        event["user"] = user

        for frame in _frames(event):
            local_variables = frame.get("vars") or {}
            for name in list(local_variables):
                if _SECRETISH.search(name):
                    local_variables[name] = "[removed]"
    except Exception:
        # A scrubber that raises must not take the process with it, but an
        # unscrubbed event must not be sent either.
        return None
    return event


def _frames(event: Dict):
    for entry in (event.get("exception") or {}).get("values") or []:
        for frame in (entry.get("stacktrace") or {}).get("frames") or []:
            yield frame


def _is_expected(hint: Optional[Dict]) -> bool:
    exc_info = (hint or {}).get("exc_info")
    if not exc_info:
        return False
    name = getattr(exc_info[0], "__name__", "")
    return any(name == expected or name.endswith(expected)
               for expected in _EXPECTED)


def _before_send(event: Dict, hint: Optional[Dict] = None) -> Optional[Dict]:
    if _is_expected(hint):
        return None
    return _scrub(event, hint)


def start(app=None, release: str = "") -> bool:
    """Turn on error reporting if a DSN is configured. Never raises."""
    global _enabled, _reason

    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        _reason = ("SENTRY_DSN is not set, so errors are only visible in the "
                   "server log.")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        _reason = ("SENTRY_DSN is set but the sentry-sdk package is not "
                   "installed. Add it to requirements.txt and redeploy.")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=release or os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None,
            # A small sample is plenty to spot a slow page and costs nothing.
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.05")),
            # Never. Personal data is not error-report material.
            send_default_pii=False,
            max_breadcrumbs=25,
            before_send=_before_send,
        )
        _enabled = True
        _reason = "reporting to Sentry"
        return True
    except Exception as exc:
        _reason = "Sentry could not start: %s" % exc
        return False


def status() -> Dict[str, Any]:
    return {"enabled": _enabled, "detail": _reason}


def note(message: str, **context) -> None:
    """Record something worth knowing that is not an exception."""
    if not _enabled:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                if not _SECRETISH.search(key):
                    scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level="warning")
    except Exception:
        pass
