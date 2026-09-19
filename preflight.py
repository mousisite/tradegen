"""Check a deployment before anyone else sees it.

The checklist in DEPLOY.md is a list of things a person has to remember to do.
This is the same list as something that either passes or does not, run against
the real address over the real internet, so "I think that is fine" is replaced
by a result.

    python preflight.py https://stockbot-abcd.onrender.com

It is read-only. It signs nothing in, writes nothing, and creates no account;
every request it makes is a GET that any visitor could make. Safe to run
against a live deployment at any time.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

TIMEOUT = 30
AGENT = "stockbot-preflight/1.0"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, verdict: str, what: str, detail: str = "") -> None:
        self.rows.append((verdict, what, detail))
        mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[verdict]
        print("%s %-52s %s" % (mark, what, detail))

    def ok(self, what, detail=""):
        self.add(PASS, what, detail)

    def bad(self, what, detail=""):
        self.add(FAIL, what, detail)

    def warn(self, what, detail=""):
        self.add(WARN, what, detail)

    @property
    def failures(self) -> int:
        return sum(1 for v, _, _ in self.rows if v == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for v, _, _ in self.rows if v == WARN)


def fetch(url: str, redirect: bool = False, _retried: bool = False) -> Dict:
    """One GET. Never raises; a failure is a result like any other."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(
        *( [] if redirect else [NoRedirect] ))
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    started = time.time()
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(400000)
            return {"status": response.status, "body": body,
                    "headers": dict(response.headers),
                    "seconds": time.time() - started, "url": response.url}
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(400000)
        except Exception:
            pass
        return {"status": exc.code, "body": body,
                "headers": dict(exc.headers or {}),
                "seconds": time.time() - started, "url": url}
    except Exception as exc:
        # A DNS hiccup or a dropped connection is not the site being down, and
        # reporting it as such is the worst thing this tool can get wrong. One
        # retry costs a second and removes almost all of that noise.
        if not _retried:
            time.sleep(1.5)
            return fetch(url, redirect, _retried=True)
        return {"status": 0, "body": b"", "headers": {},
                "seconds": time.time() - started, "error": str(exc), "url": url}


def text(result: Dict) -> str:
    return result["body"].decode("utf-8", "replace")


def header(result: Dict, name: str) -> str:
    """One header, found regardless of case.

    Header names are case-insensitive per the HTTP spec and proxies normalise
    them freely: Render returns "location" where the origin sent "Location".
    Looking one up in a plain dict is therefore a bug that only shows up behind
    a proxy, which is to say only in production.
    """
    wanted = name.lower()
    for key, value in result.get("headers", {}).items():
        if key.lower() == wanted:
            return value
    return ""


def run(base: str, report: Report) -> None:
    base = base.rstrip("/")

    print()
    print("=" * 74)
    print("REACHABLE AND HEALTHY")
    print("=" * 74)

    health = fetch(base + "/healthz")
    if health["status"] == 0:
        report.bad("the address answers at all", health.get("error", "")[:60])
        print()
        print("  Nothing else can be checked until it responds.")
        return
    report.ok("the address answers", "%d in %.1fs"
              % (health["status"], health["seconds"]))

    if health["status"] == 200:
        try:
            payload = json.loads(text(health))
            if payload.get("ok") is True:
                report.ok("the health check passes",
                          "it reached the database")
            else:
                report.bad("the health check passes", str(payload)[:60])
        except ValueError:
            report.bad("the health check returns json", text(health)[:60])
    else:
        report.bad("the health check returns 200", "got %d" % health["status"])

    # Google permits plain http for loopback addresses and nothing else, so a
    # local test is not the same failure as a public deployment on http.
    local = re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", base)
    if base.startswith("https://"):
        report.ok("served over https", "required by Google")
    elif local:
        report.warn("served over https",
                    "loopback, which Google allows; a real host must be https")
    else:
        report.bad("served over https",
                   "Google will not accept an http redirect address")

    plain = base.replace("https://", "http://", 1)
    if base.startswith("https://"):
        redirected = fetch(plain + "/healthz")
        if redirected["status"] in (301, 302, 307, 308):
            report.ok("plain http redirects to https")
        elif redirected["status"] == 200:
            report.warn("plain http redirects to https",
                        "http serves directly; prefer forcing https")
        else:
            report.ok("plain http is not served", "status %d" % redirected["status"])

    print()
    print("=" * 74)
    print("WHAT GOOGLE WILL FETCH")
    print("=" * 74)

    for path, must_contain, label in (
        ("/privacy", "openid", "privacy policy"),
        ("/terms", "not financial advice", "terms of use"),
    ):
        page = fetch(base + path)
        if page["status"] != 200:
            report.bad("%s is readable by anyone" % label,
                       "got %d; Google cannot approve the consent screen"
                       % page["status"])
            continue
        report.ok("%s is readable by anyone" % label)
        if must_contain in text(page):
            report.ok("%s says what it should" % label)
        else:
            report.warn("%s says what it should" % label,
                        "did not find %r" % must_contain)

    callback = base + "/auth/google/callback"
    print()
    print("  Paste this into Google exactly, as the authorised redirect URI:")
    print()
    print("      %s" % callback)
    print()

    print("=" * 74)
    print("SIGN-IN")
    print("=" * 74)

    # Whether sign-in is on is decided by asking for a page that holds somebody
    # else's data, not by whether the front door redirects. The front door is
    # deliberately public -- a crawler handed a redirect to a login form has
    # nothing to index, and a visitor arriving from a link should learn what
    # this is before being asked to commit. Reading auth from "/" alone
    # reported a correctly locked app as wide open.
    home = fetch(base + "/")
    if home["status"] == 200:
        report.ok("the front door answers a stranger")
    elif home["status"] in (301, 302, 307, 308):
        report.warn("the front door answers a stranger",
                    "it redirects to %s, so there is nothing public to index"
                    % header(home, "Location")[:40])
    else:
        report.bad("the front door answers a stranger",
                   "got %d" % home["status"])

    guarded = fetch(base + "/trades")
    if guarded["status"] in (301, 302, 307, 308) and             "/signin" in header(guarded, "Location"):
        report.ok("sign-in is switched on", "the journal requires an account")
        signed_in_mode = True
    elif guarded["status"] == 200:
        report.warn("sign-in is switched on",
                    "the journal served content to a stranger")
        print()
        print("  A page holding personal data loaded without an account, which")
        print("  means GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not both")
        print("  set. Correct for a private test, wrong for a public launch.")
        signed_in_mode = False
    else:
        report.bad("sign-in is switched on",
                   "the journal returned %d" % guarded["status"])
        signed_in_mode = True

    page = fetch(base + "/signin")
    if page["status"] == 200:
        report.ok("the sign-in page loads")
        body = text(page)
        if "Continue with Google" in body:
            report.ok("it offers Google sign-in")
        elif "not configured" in body or "Not set up" in body:
            report.warn("it offers Google sign-in",
                        "credentials are not set on the server")
        else:
            report.warn("it offers Google sign-in", "no button found")
    elif page["status"] in (301, 302, 307, 308) and not signed_in_mode:
        # With sign-in off every visitor is already the local account, so the
        # sign-in page redirecting away is right rather than broken.
        report.ok("the sign-in page is not needed",
                  "sign-in is off, so it sends you to the app")
    else:
        report.bad("the sign-in page loads", "got %d" % page["status"])

    start = fetch(base + "/auth/google")
    if start["status"] in (301, 302, 307, 308):
        where = header(start, "Location")
        if where.startswith("https://accounts.google.com/"):
            report.ok("starting sign-in reaches Google")
            query = where.split("?", 1)[-1]
            sent = re.search(r"redirect_uri=([^&]+)", query)
            if sent:
                from urllib.parse import unquote
                actual = unquote(sent.group(1))
                if actual == callback:
                    report.ok("the callback address matches this deployment",
                              actual)
                else:
                    report.bad("the callback address matches this deployment",
                               "app sends %s" % actual)
            for needed, label in (("code_challenge_method=S256", "PKCE is on"),
                                  ("response_type=code", "uses the code flow"),
                                  ("state=", "sends a state parameter")):
                if needed in query:
                    report.ok(label)
                else:
                    report.bad(label, "missing from the authorize url")
            if "client_secret" in query:
                report.bad("the client secret stays on the server",
                           "it is in the redirect url")
            else:
                report.ok("the client secret stays on the server")
        else:
            report.bad("starting sign-in reaches Google", where[:60])
    elif start["status"] == 503:
        report.warn("starting sign-in reaches Google",
                    "credentials not set on the server yet")
    else:
        report.bad("starting sign-in reaches Google",
                   "got %d" % start["status"])

    print()
    print("=" * 74)
    print("THE PRIVATE PAGES")
    print("=" * 74)

    for path in ("/trades", "/account", "/portfolio", "/monitor",
                 "/export/trades.csv"):
        page = fetch(base + path)
        if signed_in_mode:
            if page["status"] in (301, 302, 307, 308) and \
                    "/signin" in header(page, "Location"):
                report.ok("%s needs an account" % path)
            elif page["status"] == 200:
                report.bad("%s needs an account" % path,
                           "it served content to a stranger")
            else:
                report.warn("%s needs an account" % path,
                            "got %d" % page["status"])
        else:
            if page["status"] == 200:
                report.ok("%s loads" % path)
            else:
                report.bad("%s loads" % path, "got %d" % page["status"])

    print()
    print("=" * 74)
    print("COOKIES AND HEADERS")
    print("=" * 74)

    # A page view stores nothing in the session, so no cookie is set and there
    # is nothing to inspect. Starting sign-in stores the state and verifier,
    # which is the first point at which a real cookie exists.
    cookie = None
    for path in ("/auth/google", "/signin", "/"):
        raw = header(fetch(base + path), "Set-Cookie")
        if raw:
            cookie = raw
            break
    if cookie:
        lowered = cookie.lower()
        for flag, label, verdict in (
            ("httponly", "the session cookie is HttpOnly", FAIL),
            ("samesite", "the session cookie sets SameSite", WARN),
        ):
            if flag in lowered:
                report.ok(label)
            else:
                report.add(verdict, label, "not set")
        if base.startswith("https://"):
            if "secure" in lowered:
                report.ok("the session cookie is Secure")
            else:
                report.bad("the session cookie is Secure",
                           "set STOCKBOT_PUBLIC_URL to the https address")
    else:
        report.warn("a session cookie was issued",
                    "none seen, so its flags could not be checked")

    print()
    print("=" * 74)
    print("SPEED")
    print("=" * 74)

    timings = []
    for path in ("/healthz", "/signin", "/privacy"):
        result = fetch(base + path)
        timings.append((path, result["seconds"]))
    slowest = max(timings, key=lambda t: t[1])
    for path, seconds in timings:
        print("        %-12s %.2fs" % (path, seconds))
    if slowest[1] < 3:
        report.ok("pages answer quickly", "slowest %.1fs" % slowest[1])
    elif slowest[1] < 10:
        report.warn("pages answer quickly",
                    "slowest %.1fs, possibly waking from sleep" % slowest[1])
    else:
        report.bad("pages answer quickly", "slowest %.1fs" % slowest[1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="the deployed address, e.g. https://x.onrender.com")
    args = parser.parse_args(argv)

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    print()
    print("  Checking %s" % url)

    report = Report()
    run(url, report)

    print()
    print("=" * 74)
    checks = len(report.rows)
    if report.failures:
        print("  %d of %d checks failed. Fix those before telling anyone."
              % (report.failures, checks))
    elif report.warnings:
        print("  %d checks passed, %d worth a look. Nothing is broken."
              % (checks - report.warnings, report.warnings))
    else:
        print("  All %d checks passed." % checks)
    print("=" * 74)

    if report.failures:
        print()
        print("  Failed:")
        for verdict, what, detail in report.rows:
            if verdict == FAIL:
                print("    - %s%s" % (what, ("  (%s)" % detail) if detail else ""))

    print()
    print("  Still to check by hand, because nothing automated can:")
    print("    - Sign in with a second Google account and confirm it sees an")
    print("      empty journal, not yours.")
    print("    - Log a paper trade, redeploy, confirm it survived. If it did")
    print("      not, the disk is not mounted and every deploy destroys data.")
    print("    - Open it on a phone.")
    print()
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
