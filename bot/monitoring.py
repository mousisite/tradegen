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

  As little personal data as possible leaves. `send_default_pii` stays off,
  form contents are never read, and cookies, anything that looks like a
  credential and a header saying where the visitor is are stripped before
  sending. What is left is the error, the page address, the browser's own
  description and the values the failing code was working with, which can
  include an email or network address; the privacy page says the same. Page
  loads that worked are sent only when SENTRY_TRACES_RATE asks for a sample
  of them, and then with no query string at all; the privacy page says so
  whenever it is on.

  Expected failures are not errors. A rate-limited data source and an unknown
  ticker are conditions the app already handles and explains. Reporting them
  trains you to ignore the alerts, which is how a real error gets missed.
"""
from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_enabled = False
_reason = "not initialised"
_sampled = 0.0

# Values that must never appear in an error report, whatever the key is called.
_SECRETISH = re.compile(
    r"(secret|password|token|api[_-]?key|authorization|cookie|crumb|client[_-]?secret)",
    re.I)

# Query parameters that carry a credential under a short name: the weekly
# email's stop link (t) and Google's answer to a sign-in (code, state).
_SECRET_PARAM = re.compile(r"(?:^|&)(?:t|code|state)=", re.I)

# Request headers that say where a visitor is: the network address Cloudflare
# and other proxies pass on under names Sentry does not blank (it knows only
# X-Forwarded-For and X-Real-IP), and Cloudflare's guess at the country. No
# fault needs them, and every report would otherwise carry them.
_WHEREABOUTS = re.compile(
    r"(?:^|[-_])ip(?:v[46])?$|forwarded|client[-_]?ip|ipcountry|addr", re.I)

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
            if _SECRETISH.search(key) or _WHEREABOUTS.search(key):
                headers[key] = "[removed]"
            elif key.lower() == "referer" and isinstance(headers[key], str):
                # The page before, on this site too, whose query can hold
                # the weekly email's stop token.
                headers[key] = headers[key].split("?")[0]
        request["headers"] = headers

        # A query string can carry a symbol, which is fine, and a session
        # token, which is not.
        query = request.get("query_string")
        if isinstance(query, str) and (_SECRETISH.search(query)
                                       or _SECRET_PARAM.search(query)):
            request["query_string"] = "[removed]"
        event["request"] = request

        # The user is identified by an opaque id only. An email address is not
        # needed to fix a bug.
        user = event.get("user") or {}
        user.pop("email", None)
        user.pop("username", None)
        user.pop("ip_address", None)
        event["user"] = user

        # What was called just before. A path can hold a phone's push address
        # or a customer's number, and a query a key, so only the site is kept.
        crumbs = event.get("breadcrumbs")
        if isinstance(crumbs, dict):
            crumbs = crumbs.get("values")
        for crumb in crumbs or []:
            if not isinstance(crumb, dict):
                continue
            data = crumb.get("data")
            if isinstance(data, dict):
                _cut_to_site(data)
            if isinstance(crumb.get("message"), str):
                crumb["message"] = _SITE.sub(r"\1", crumb["message"])

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


def _scrub_sample(event: Dict, hint: Optional[Dict] = None) -> Optional[Dict]:
    """A page load that worked, sampled for timing: scrubbed as an error report
    is, and then cut to the page's address and how long it and each call it
    made took, since no fault needs explaining. No query string, no headers,
    and a call is named by the site it went to, not its path, which can hold
    a phone's push address. Sentry does not run before_send on these."""
    event = _scrub(event, hint)
    if event is None:
        return None
    try:
        event["request"].pop("query_string", None)
        event["request"].pop("headers", None)
        for span in event.get("spans") or []:
            _cut_to_site(span.get("data") or {})
            if isinstance(span.get("description"), str):
                span["description"] = _SITE.sub(r"\1", span["description"])
    except Exception:
        return None
    return event


# An address cut to its site: "POST https://host/path?q" becomes "POST https://host".
_SITE = re.compile(r"(\w+://[^/\s?#]+)[^\s]*")


def _cut_to_site(data: Dict) -> None:
    """Cut the address of one outside call to the site it went to."""
    for key in ("http.query", "http.fragment"):
        data.pop(key, None)
    if isinstance(data.get("url"), str):
        data["url"] = _SITE.sub(r"\1", data["url"])


def _sample_rate() -> float:
    """The share of page loads sent even when nothing breaks: none, unless
    SENTRY_TRACES_RATE asks for some (0.05 is one in twenty)."""
    try:
        rate = float(os.environ.get("SENTRY_TRACES_RATE") or 0)
    except ValueError:
        return 0.0
    return min(max(rate, 0.0), 1.0) if math.isfinite(rate) else 0.0


def start(app=None, release: str = "") -> bool:
    """Turn on error reporting if a DSN is configured. Never raises."""
    global _enabled, _reason, _sampled

    _sampled = 0.0
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

    rate = _sample_rate()
    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=release or os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None,
            # Off unless asked for: a sample is sent even when nothing broke,
            # which the privacy page then has to say, and does.
            traces_sample_rate=rate or None,
            # Never. Personal data is not error-report material.
            send_default_pii=False,
            # Nor is what anyone typed into a form, so it is not even read.
            max_request_body_size="never",
            max_breadcrumbs=25,
            before_send=_before_send,
            before_send_transaction=_scrub_sample,
        )
        _enabled = True
        _sampled = rate
        _reason = "reporting to Sentry"
        return True
    except Exception as exc:
        _reason = "Sentry could not start: %s" % exc
        return False


def status() -> Dict[str, Any]:
    """Whether reports go to Sentry, why not, and the share of page loads
    sampled even when nothing breaks (0 when none are)."""
    return {"enabled": _enabled, "detail": _reason,
            "sampled": _sampled if _enabled else 0.0}


def note(message: str, **context) -> None:
    """Record something worth knowing that is not an exception.

    Always written to the server log, so it is seen even with error reporting
    off; sent to Sentry as well when that is on.
    """
    shown = {k: v for k, v in context.items() if not _SECRETISH.search(k)}
    log.warning("%s%s", message,
                "".join("  %s=%s" % (k, v) for k, v in shown.items()))
    if not _enabled:
        return
    try:
        import sentry_sdk
        # The message is sent inside the scope, or the context set on it is
        # thrown away before it is attached.
        with sentry_sdk.new_scope() as scope:
            for key, value in shown.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level="warning")
    except Exception:
        pass
