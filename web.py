#!/usr/bin/env python
"""Web interface for the bot.

Runs a small local server so the whole tool can be driven from a browser
instead of a terminal. It calls `bot.engine`, exactly like the command line
does, so both front ends always give the same answer.

    python web.py                 then open http://127.0.0.1:8000
    python web.py --port 9000
    python web.py --host 0.0.0.0  reachable from other devices on your network

The server is bound to localhost by default. Market data, your trades and your
API key never leave the machine.
"""
from __future__ import annotations

import argparse
import dataclasses
from datetime import timedelta
import hmac
import io
import json
import math
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from typing import Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.terminal import prepare_output
prepare_output()

try:
    from dotenv import load_dotenv
    # tests/harness.py sets this, so a test run never picks up real keys.
    if not os.environ.get("STOCKBOT_NO_DOTENV"):
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from flask.json.provider import DefaultJSONProvider

from bot import accounts as accounts_mod
from bot import alerts as alerts_mod
from bot import apple as apple_mod
from bot import apple_store as apple_store_mod
from bot import appstore as appstore_mod
from bot import auth as auth_mod
from bot import billing as billing_mod
from bot import catalysts as catalysts_mod
from bot import codes as codes_mod
from bot import charts as charts_mod
from bot import config as config_mod
from bot import database as db_mod
from bot import demo as demo_mod
from bot import digest as digest_mod
from bot import engine
from bot import events as events_mod
from bot import ideas as ideas_mod
from bot import invites as invites_mod
from bot import export as export_mod
from bot import market as market_mod
from bot import plain as plain_mod
from bot import portfolio as portfolio_mod
from bot import public as public_mod
from bot import push as push_mod
from bot import safejson
from bot import scheduler as scheduler_mod
from bot import screener as screener_mod
from bot import sessions as sessions_mod
from bot import strategies as strategies_mod
from bot import thesis as thesis_mod
from bot import usage as usage_mod
from bot import webapp as webapp_mod
from bot import workflows as workflows_mod
from bot.market import DataError, format_price
from bot.strategies import FAMILIES
from bot.vision import MAX_EDGE, MAX_PIXELS, VisionError, read_chart

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# Chart screenshots are small. This cap stops a huge upload from tying up the
# single-process dev server.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
# Every typed field here is a ticker, a number or a short note. Flask's own
# default lets each field carry half a megabyte, so a few hundred posts of
# padded notes could fill the data disk. The screenshot upload sets a larger
# limit for its own request, because the form reader holds file data to it.
app.config["MAX_FORM_MEMORY_SIZE"] = 20_000


class _RequestJSON(DefaultJSONProvider):
    """Flask's JSON, except that a body nested deeper than bot/safejson.py
    allows is refused before it is read, as text that is not JSON.

    request.get_json reads every JSON body through this. JSON nested a few
    thousand deep would otherwise raise RecursionError, which get_json's
    silent= does not cover: a few kilobytes from anyone, to any address that
    takes JSON, would be a 500, a traceback and an error report. Refused
    here, get_json gives None (silent) or a 400, as for any other junk.
    """

    def loads(self, s, **kwargs):
        return safejson.loads(s, **kwargs)


app.json = _RequestJSON(app)

# Signed cookies need a key that survives a restart, or everyone is signed out
# every time the server starts. bot/auth.py keeps one next to the database.
app.secret_key = auth_mod.session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=accounts_mod.SESSION_DAYS),
    # The cookie is set at sign-in for as long as the server's own record of
    # it lasts, and again only when it changes. Sent with every answer, it
    # rode on files marked as the same for everybody, which no cache in front
    # of the site may then keep.
    SESSION_REFRESH_EACH_REQUEST=False,
)

# Behind a reverse proxy the app sees plain http on an internal hostname, so
# url_for(_external=True) would build an http:// callback that Google refuses,
# because the redirect address has to match the registered one exactly. These
# headers are what the proxy uses to say what the browser actually asked for.
# Only enabled deliberately, since trusting them when there is no proxy in
# front would let a client spoof its own scheme and address. The address
# counts the one hop nearest the app, and the Host header is taken as it
# arrives: the hosts pass the visitor's own on, and do not set
# X-Forwarded-Host or X-Forwarded-Prefix, so those could only be a visitor's.
# Under serve.py, waitress hands the headers on only behind a proxy.
_public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()


def behind_proxy() -> bool:
    """Whether the operator says a proxy is in front of this server."""
    return ((os.environ.get("STOCKBOT_BEHIND_PROXY") or "").strip().lower()
            in ("1", "true", "yes"))


# An https public address is an operator saying there is a TLS terminator in
# front, which is the only way these headers get set at all.
if behind_proxy() or _public.startswith("https://"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_prefix=0)

# A session cookie sent over plain http can be read in transit. On a public
# deployment that is unacceptable, so the flag is set whenever the app knows it
# is served over https; on localhost it stays off or the cookie never arrives.
if _public.startswith("https://"):
    app.config["SESSION_COOKIE_SECURE"] = True


@app.after_request
def _harden(response):
    """Headers that close doors nothing here uses.

    Nothing on this site is meant to be framed, sniffed into another type, or
    to hand a full address to the next site. HSTS goes only where cookies are
    already secure, which is the same question: is this served over https.
    """
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config.get("SESSION_COOKIE_SECURE"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Anything sent to someone signed in may hold their journal, so no copy
    # is kept: not in the browser's cache, nor the app's, nor for Back after
    # signing out on a shared computer. What is the same for everybody, and
    # a route that sets its own, keeps its caching.
    if (getattr(g, "user", None) is not None and not getattr(g, "demo", False)
            and request.endpoint != "static"
            and response.mimetype in ("text/html", "application/json", "text/csv")):
        h.setdefault("Cache-Control", "private, no-store")
    # A file asked for by this release's fingerprint never changes under that
    # address, so a browser need not ask for it again on every page.
    if (request.endpoint == "static" and response.status_code in (200, 206, 304)
            and request.args.get("v") == ASSET_VERSION):
        h["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# Free space below which the health check says the disk is low. The database
# and its backups share one small disk, and a full one stops every write.
LOW_DISK = 100 * 1024 * 1024
_LOW_DISK_SAID = [0.0]


@app.route("/healthz")
def healthcheck():
    """Liveness, for whatever is watching the process.

    Deliberately cheap and unauthenticated: it reads the database to prove the
    process is actually able to serve, and returns nothing about anyone. A
    read never waits for a writer, so a long write cannot fail the check.
    """
    import shutil

    path = app.config.get("DB_PATH") or db_mod.default_path()
    try:
        conn = db_mod.connect(path)
        try:
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except Exception:
        # Why goes to the log; the check itself is public.
        app.logger.exception("Health check could not read the database")
        return jsonify({"ok": False}), 503
    out = {"ok": True, "service": "stockbot"}
    try:
        free = shutil.disk_usage(os.path.dirname(path)).free
    except OSError:
        free = None
    # Said, not failed: restarting the instance would not free any space, and
    # the site still reads while it is short.
    if free is not None and free < LOW_DISK:
        out["detail"] = "The data disk is nearly full."
        if time.time() - _LOW_DISK_SAID[0] > 3600:
            _LOW_DISK_SAID[0] = time.time()
            from bot import monitoring
            monitoring.note("The data disk is nearly full",
                            free_mb=int(free / (1024 * 1024)))
    return jsonify(out)


# Pages reachable without being signed in. Everything else needs an account
# once Google sign-in is configured.
PUBLIC_ENDPOINTS = {"signin", "google_start", "google_callback", "signout",
                    "apple_start", "apple_callback", "apple_nonce",
                    "apple_native", "apple_notifications", "app_finish",
                    # Apple's server, about in-app purchases: signed, so it
                    # needs no sign-in and has no Origin to check.
                    "appstore_notifications",
                    "static", "healthcheck", "privacy", "terms",
                    "robots", "sitemap", "index", "favicon",
                    "manifest", "service_worker", "offline",
                    "code_start", "code_verify", "pricing",
                    "billing_webhook", "stocks_index", "stock_page",
                    "stock_card", "share_page", "share_card", "join",
                    "email_stop", "backtests", "backtest_page", "record",
                    "record_csv", "record_fingerprints",
                    # The size and survival tools work on what is typed.
                    "size_tool", "survival_tool"}

SESSION_KEY = "stockbot_session"

# Bumped by hand when the legal pages change, so the date on them is
# the date they were actually revised.
LEGAL_UPDATED = "16 November 2026"


def _served_publicly() -> bool:
    """Whether this runs behind a public address rather than on one machine.

    Read from how it is deployed, not from which sign-in keys happen to be
    set: both host configs set the proxy flag, and an https public address
    means a TLS terminator in front.
    """
    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip().lower()
    return public.startswith("https://") or behind_proxy()


def auth_required() -> bool:
    """Whether this installation asks people to sign in.

    Default is "auto": required when any way to sign in is configured, or
    when the app is served publicly, so running it on your own machine needs
    no login and nothing to configure. A public deployment whose sign-in keys
    went missing used to fall back to the local account, which is the
    operator, for every visitor. Now it shows the sign-in page, saying
    signing in is off for now (the start-up log says what is missing),
    unless auth_mode is set to "off" on purpose.
    """
    mode = app.config.get("AUTH_MODE")
    if mode is None:
        mode = config_mod.load(app.config.get("CFG_PATH")).get("auth_mode", "auto")
    if mode == "off":
        return False
    if mode == "google":
        return True
    # Any configured way in means this is a shared installation.
    return (auth_mod.configured() or codes_mod.email_ready()
            or codes_mod.phone_ready() or apple_mod.native_ready()
            or _served_publicly())


# Addresses this app used to answer on. A link somebody saved still works, and
# a search engine moves what it learned to the new name instead of treating the
# two as rival copies of the same site.
RETIRED_HOSTS = {"tradegen.app", "www.tradegen.app"}


@app.before_request
def _moved_permanently():
    """Send a request for an old address to the current one.

    Registered before _identify so a visitor on the old name is moved rather
    than first being asked to sign in at an address that is no longer the
    product's.
    """
    host = (request.host or "").split(":")[0].lower()
    if host not in RETIRED_HOSTS:
        return None

    target = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip().rstrip("/")
    if not target:
        return None

    # Without this the app redirects to itself forever, which is what happens
    # the moment the public address has not been switched over yet.
    if target.split("//")[-1].split("/")[0].lower() == host:
        return None

    # full_path always carries a "?", even with nothing after it.
    path = request.full_path
    if path.endswith("?"):
        path = path[:-1]
    return redirect(target + path, code=301)


# Pages a visitor who is not signed in may open, as the example account. Only
# ever for GET: nothing here can write, and every form on these pages posts to
# an endpoint that is not on this list, so it sends them to sign up instead.
DEMO_ENDPOINTS = {"analyse", "research", "ideas", "screener", "portfolio",
                  "trades", "monitor", "strategies", "settings",
                  # The journal's breakdown and a trade's picture, of the
                  # example's own trades.
                  "journal_breakdown", "trade_picture"}


@app.before_request
def _identify():
    """Attach the signed-in person, or the local account, to this request."""
    g.user = None
    g.demo = False
    # Neither depends on who asks. A style sheet needs no database, and the
    # health check has to reach its own answer when there is none to open.
    if request.endpoint in ("static", "healthcheck"):
        return None
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        token = session.get(SESSION_KEY)
        if token:
            g.user = accounts_mod.user_for_session(conn, token)
        if g.user is None and not auth_required():
            g.user = accounts_mod.local_user(conn)
    finally:
        conn.close()

    if g.user is not None:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.method == "GET" and request.endpoint in DEMO_ENDPOINTS:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            g.user = demo_mod.user(conn)
        finally:
            conn.close()
        g.demo = True
        return None
    if request.endpoint is None:
        # No such page. Sending a typo to the sign-in door tells the person
        # nothing, and tells a crawler the address exists. Let the 404 answer.
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not signed in."}), 401
    # A visitor who tried to do something. A POST cannot be replayed as a GET
    # after signing in, so they are brought back to the page they were on.
    if request.method != "GET":
        return redirect(url_for("signin", next=_referrer_path()))
    # full_path always ends in "?" when there is no query string.
    return redirect(url_for("signin", next=request.full_path.rstrip("?")))


def _referrer_path() -> str:
    """The page a request came from, as a safe path on this site, or "/"."""
    from urllib.parse import urlparse
    ref = urlparse(request.referrer or "")
    if ref.netloc and ref.netloc != request.host:
        return "/"
    path = ref.path or "/"
    if ref.query:
        path += "?" + ref.query
    return _safe_back(path, "/")


def _pro() -> bool:
    """Whether this person has everything. Everyone does while billing is off."""
    return billing_mod.is_pro(getattr(g, "user", None), _is_operator())


def _pricing_page(reason: str = "", status: int = 200, code: str = "",
                  applied=None, code_error: str = ""):
    """Free and Pro side by side, saying why they arrived if there is a reason.

    code, applied and code_error are what the discount box last said, so a
    person sees their code worked before they are sent to pay.
    """
    user = getattr(g, "user", None)
    customer = ""
    if user is not None and not g.demo:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            customer = billing_mod.customer_of(conn, user.id)
        finally:
            conn.close()
    # Inside the iPhone app no web price is shown, so Stripe is not asked.
    return render_template("pricing.html", reason=reason,
                           limits=billing_mod.free_limits(),
                           offer={} if _in_app() else billing_mod.offers(),
                           code=code, applied=applied, code_error=code_error,
                           is_pro=user is not None and _pro(),
                           has_customer=bool(customer), fair=_fair_use(),
                           apple=_apple_plan()), status


def _apple_plan() -> Dict:
    """Pro through Apple, for the pricing and account pages.

    on: whether the app sells Pro. pro: the Apple subscription that gives
    this person Pro now, or None. billing: one Apple could still charge for
    (running, or retrying a failed renewal), test purchases included, for the
    warning before an account is deleted. retrying: whether Apple may yet
    charge for Pro that has ended, so the website sells none meanwhile.
    via: how Pro was got otherwise, so the app can say why it sells nothing:
    "web" (paid on the website), "invite" or "other".
    """
    out = {"on": apple_store_mod.enabled(), "pro": None, "billing": None,
           "retrying": False, "products": apple_store_mod.product_ids(), "via": "",
           "twice": False, "invite_until": 0, "subscriptions_url": APPLE_SUBSCRIPTIONS}
    user = getattr(g, "user", None)
    if user is None or getattr(g, "demo", False):
        return out
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        out["pro"] = apple_store_mod.subscription(conn, user.id)
        out["billing"] = apple_store_mod.subscription(conn, user.id, counted_only=False)
        out["retrying"] = apple_store_mod.may_still_charge(conn, user.id)
    finally:
        conn.close()
    plain = dataclasses.replace(user, apple_until=0)
    if billing_mod.ready() and billing_mod.is_pro(plain, _is_operator()):
        if plain.plan == "pro" and plain.plan_status in billing_mod.ACTIVE:
            out["via"] = "web"
        elif plain.bonus_until > time.time():
            out["via"], out["invite_until"] = "invite", plain.bonus_until
        else:
            out["via"] = "other"
    # Paying Apple and Stripe for the same Pro, which checkout and the app
    # both try to prevent: said plainly wherever the plan is shown.
    out["twice"] = bool(out["pro"]) and out["via"] == "web"
    return out


def _fair_use() -> Dict[str, int]:
    """The hourly limits Pro keeps, read from the limits themselves.

    Screenshot reads are left out where no key is set: the page would be
    putting a limit on something this site does not offer.
    """
    return {"images": (IMAGE_READS_PER_HOUR
                       if os.environ.get("ANTHROPIC_API_KEY") else 0),
            "theses": THESES_PER_HOUR}


def _upgrade(reason: str):
    """The pricing page, saying why they arrived at it."""
    return _pricing_page(reason, 402)


def _from_another_site() -> bool:
    """Whether the browser says another site sent this request here.

    Browsers set this header themselves and a page cannot change it. A link
    on someone else's site arrives as a GET carrying the session cookie, so
    without this it could spend a free account's looks for the day.
    """
    return request.headers.get("Sec-Fetch-Site", "") == "cross-site"


# How long after signing in a link from elsewhere still counts as the page the
# person signed in to reach. Google sends the browser back through its own
# redirect, so that page arrives marked as coming from another site.
SIGNED_IN_JUST_NOW = 120


def _just_signed_in(conn) -> bool:
    age = accounts_mod.session_age(conn, session.get(SESSION_KEY, ""))
    return age is not None and age < SIGNED_IN_JUST_NOW


def _look_item(symbol: str, interval: str, exact: bool = False) -> str:
    """What a free look is counted against: an instrument at a bar size.

    LINK exactly (Interlink Electronics) is not the bare LINK (the Chainlink
    coin), so each is its own look. The routines count the same way
    (workflows._Looks).
    """
    return "%s|%s%s" % (symbol.upper(), interval, "|exact" if exact else "")


def _allowance_left(symbol: str, interval: str, exact: bool = False) -> bool:
    """Whether a free account may open this instrument now, taking the look if so.

    The look is taken before the analysis runs, in one transaction with the
    count, and handed back when the request ends if nothing was shown for
    it. Checking first and counting afterwards let ten requests sent at once
    all pass a limit of three.
    """
    if g.demo or _pro():
        return True
    item = _look_item(symbol, interval, exact)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if _from_another_site() and not _just_signed_in(conn) \
                and not billing_mod.opened_today(conn, g.user.id, item):
            g.allowance_refused = "elsewhere"
            return False
        taken = billing_mod.reserve_view(conn, g.user.id, item)
    finally:
        conn.close()
    if taken == "new":
        g.setdefault("views_pending", set()).add(item)
    return bool(taken)


def _spend_view(symbol: str, interval: str, exact: bool = False) -> None:
    """Counted once the analysis is on screen, so a mistyped ticker costs nothing."""
    if g.demo or _pro():
        return
    item = _look_item(symbol, interval, exact)
    pending = g.get("views_pending") or set()
    if item in pending:
        pending.discard(item)       # shown, so the look taken for it stands
        return
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        billing_mod.count_view(conn, g.user.id, item)
    finally:
        conn.close()


@app.teardown_request
def _return_unshown_views(_exc):
    """Hand back looks taken for analyses that were never shown.

    Runs however the request ended, so an unknown ticker or a failed data
    source costs nothing, whichever page asked.
    """
    pending = g.get("views_pending")
    user = g.get("user")
    if not pending or user is None:
        return
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            for item in pending:
                billing_mod.release_view(conn, user.id, item)
        finally:
            conn.close()
    except Exception:
        # The page has already been answered; the cost of failing here is
        # one look counted that should not have been.
        app.logger.exception("Could not hand back an unused free look")


def _allowance_used():
    if g.get("allowance_refused") == "elsewhere":
        return _pricing_page(
            "You came here from a link on another site, so it was not opened: "
            "that would have used one of today's free looks. Search for it on "
            "the Analyse page to open it.")
    return _upgrade("You have looked at %d different instruments today, which "
                    "is the free allowance. It resets at midnight UTC, or Pro "
                    "has no limit." % billing_mod.free_limits()["analyses_per_day"])


# Hourly budgets for the requests that cost money or a shared quota: a paid
# call to read a screenshot, a full research run, a discount code checked
# with Stripe. One process serves the site, so counting in memory is enough,
# and a restart forgetting the counts only errs toward letting people in.
_RATE: Dict[Tuple[str, str], list] = {}
_RATE_LOCK = threading.Lock()
RATE_WINDOW = 3600.0


def _rate_limited(action: str, per_hour: int) -> bool:
    """True when this account, or its network, has used its hourly budget.

    Otherwise the attempt is counted and False is returned. A network gets
    three accounts' worth, so a household is not refused for one person's
    use, while one connection running many accounts still is. Your own
    machine and the operator are never limited.
    """
    user = getattr(g, "user", None)
    if user is not None and (user.is_local or _is_operator()):
        return False
    budgets = []
    if user is not None:
        budgets.append(("user:%d" % user.id, per_hour))
    ip = _client_ip()
    if ip:
        budgets.append(("ip:" + ip, per_hour * 3))
    now = time.time()
    with _RATE_LOCK:
        if len(_RATE) > 20000:
            # Forget the idle rather than grow for ever.
            for key in [k for k, v in _RATE.items()
                        if not v or now - v[-1] >= RATE_WINDOW]:
                del _RATE[key]
        for who, most in budgets:
            recent = [t for t in _RATE.get((action, who), ())
                      if now - t < RATE_WINDOW]
            _RATE[(action, who)] = recent
            if len(recent) >= most:
                return True
        for who, _most in budgets:
            _RATE[(action, who)].append(now)
    return False


def _too_often(what: str):
    return render_template("error.html", symbol="",
                           message="You have %s a lot in the last hour. Wait a "
                                   "while, then try again." % what), 429


def _plainly(exc: Exception, plain: str) -> str:
    """What an error page says about a failure nobody planned for.

    The exception's own text names files, hosts and libraries. It helps the
    one person who can act on it, whoever runs this copy, so they are shown
    it, and it goes to the log for them too. Anyone else reads plain words.
    """
    app.logger.warning("%s failed: %s", request.path, exc, exc_info=exc)
    return str(exc) if _is_operator() else plain


# Seconds to wait when the source did not answer and said nothing of when.
UNAVAILABLE_RETRY = 60


def _source_wait(exc) -> Optional[int]:
    """Seconds to come back after, when the source turned a request away for
    now or did not answer; None for any other failure."""
    if isinstance(exc, market_mod.DataThrottled):
        return int(exc.seconds)
    if isinstance(exc, market_mod.DataUnavailable):
        return UNAVAILABLE_RETRY
    return None


def _no_prices(exc: DataError, symbol: str = "", status: int = 404,
               message: str = ""):
    """The page for prices that could not be had.

    When the source turned the request away for now (a rate limit) or did not
    answer (a timeout, an outage), that is a 503 with Retry-After, and the page
    leaves out the advice about tickers that do not exist: the ticker was
    fine, and that advice sent people hunting for a typo. Anything else keeps
    the status the caller gives.
    """
    wait = _source_wait(exc)
    if wait is not None:
        page = app.make_response((render_template(
            "error.html", message=str(exc), symbol=symbol, throttled=True), 503))
        page.headers["Retry-After"] = str(wait)
        return page
    return render_template("error.html", message=message or str(exc),
                           symbol=symbol), status


def _locked(headline: str, detail: str, examples=True):
    """The page a visitor sees when they reach past what the example can do."""
    return render_template(
        "locked.html", headline=headline, detail=detail,
        next_url=request.full_path,
        back_url=_referrer_path() if request.referrer else url_for("index"),
        examples=demo_mod.SYMBOLS if examples else ())


@app.context_processor
def _expose_user():
    """Templates need to know who is signed in and whether signing in exists."""
    return {"current_user": getattr(g, "user", None),
            "demo": getattr(g, "demo", False),
            "auth_on": auth_required(),
            "email_ready": codes_mod.email_ready(),
            "billing_on": billing_mod.ready(),
            # The upgrade button, for signed-in free accounts only, and in
            # the iPhone app only while Pro can be bought there, through Apple.
            "go_pro": (billing_mod.ready() and getattr(g, "user", None) is not None
                       and not getattr(g, "demo", False) and not _pro()
                       and (not _in_app() or apple_store_mod.enabled())),
            # Pro sold in the iPhone app through Apple: the app's pages load
            # static/app-purchase.js while it is on.
            "iap_on": apple_store_mod.enabled(),
            "apple_pro_said": APPLE_PRO_SAID,
            "apple_retrying_said": APPLE_RETRYING_SAID,
            "phone_ready": codes_mod.phone_ready(),
            "google_ready": auth_mod.configured(),
            # Apple on the website, and Apple anywhere (the app needs less).
            "apple_ready": apple_mod.web_ready(),
            "apple_native": apple_mod.native_ready(),
            "apple_client": apple_mod.bundle_id(),
            # The button shows wherever it can work: on the website once the
            # website is set up, and in the app as soon as the app's half is.
            "apple_button": apple_mod.web_ready()
                            or (apple_mod.native_ready() and _in_app())}


def _in_app() -> bool:
    """Whether this page is drawn inside the Orenth iPhone app.

    The app's web view adds OrenthApp/<version> to its User-Agent. It only
    ever chooses what to show, never what is allowed: anyone can send any
    User-Agent.
    """
    return "OrenthApp/" in request.headers.get("User-Agent", "")


# The iPhone app is this same site inside a native shell, and it says so by
# adding this to its User-Agent. Knowing it changes only what is drawn: no
# visit counting by a third party, no web prices or Stripe buttons (inside an
# App Store app, Pro may only be sold through Apple), and the small script
# that reaches the phone's share sheet, status bar and sign-in sheets. It is
# never a reason to trust a request, because anyone can send any User-Agent.
APP_AGENT = "OrenthApp/"


def _in_app() -> bool:
    """Whether this page is being drawn inside the Orenth iPhone app."""
    found = g.get("in_app")
    if found is None:
        found = APP_AGENT in (request.headers.get("User-Agent") or "")
        g.in_app = found
    return found


@app.before_request
def _note_app():
    g.in_app = APP_AGENT in (request.headers.get("User-Agent") or "")


@app.context_processor
def _app_mode():
    return {"in_app": _in_app()}


def uid() -> int:
    """The id every personal query is filtered by."""
    user = getattr(g, "user", None)
    if user is None:
        abort(401)
    return user.id

# An analysis takes several seconds. Re-rendering the same page, or hitting
# refresh, should not re-run the whole pipeline and re-hammer the data source.
# Each entry: when it was made, the analysis, and each person's run of it.
_CACHE: Dict[Tuple, Tuple[float, engine.Analysis, Dict[int, int]]] = {}
# Research costs several network round trips, so it gets its own cache.
_RESEARCH_CACHE: Dict[Tuple, Tuple[float, object]] = {}
_CACHE_TTL = 90.0
# How long the example account's analyses are reused. Long, because visitors
# looking around should cost the server one analysis per instrument per
# quarter hour, however many of them there are.
DEMO_TTL = 15 * 60.0
_CACHE_LOCK = threading.Lock()
# Analyses running right now, by cache key, so a second request for the same
# one waits for it instead of starting its own.
_RUNNING: Dict[Tuple, Dict] = {}
# Longer than an analysis takes, news included. A waiter that gave up sooner
# would start its own copy, which is what waiting is there to prevent.
_RUNNING_WAIT = 150.0

INTERVALS = [("5m", "5 minutes"), ("15m", "15 minutes"), ("30m", "30 minutes"),
             ("1h", "1 hour"), ("1d", "Daily")]


def _cfg() -> Dict:
    """The settings for this request: the app's, with this person's on top.

    On your own machine there is one person and the settings file is theirs.
    On a deployment each person has their own, stored against their account,
    because one shared file meant anyone who signed in changed everybody's
    account size, costs and thresholds.
    """
    cfg = config_mod.load(app.config.get("CFG_PATH"))
    user = getattr(g, "user", None)
    if user is not None and not user.is_local:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            cfg.update(accounts_mod.settings_for(conn, user.id))
        finally:
            conn.close()
    return cfg


def _settings_key(cfg: Dict) -> str:
    """A short fingerprint of everything that changes what an analysis says."""
    import hashlib
    picked = sorted((k, repr(cfg.get(k))) for k in accounts_mod.PERSONAL_SETTINGS)
    return hashlib.sha1(repr(picked).encode("utf-8")).hexdigest()[:12]


def _costs(cfg: Optional[Dict] = None) -> Dict[str, float]:
    """Round-trip trading costs as fractions of the price, from the settings."""
    cfg = cfg or _cfg()
    return {"equity": float(cfg.get("cost_bps_equity", 2.0)) / 10000.0,
            "crypto": float(cfg.get("cost_bps_crypto", 20.0)) / 10000.0,
            "futures": float(cfg.get("cost_bps_futures", 1.0)) / 10000.0}


def cached_analysis(symbol: str, interval: str, cfg: Dict,
                    with_news: bool = True, record: bool = True,
                    ttl: Optional[float] = None,
                    exact: bool = False) -> engine.Analysis:
    # The settings are part of the key. Position sizes, stops and the call
    # itself depend on them, and without this one person's ?shorts=1 or larger
    # account was served to the next person who asked for the same ticker.
    # So is exact: LINK exactly is Interlink Electronics, and the bare LINK
    # the Chainlink coin.
    key = (symbol.upper(), bool(exact), interval, bool(with_news),
           _settings_key(cfg))
    me = uid()
    owned = None
    while True:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and (time.time() - hit[0]) < (ttl or _CACHE_TTL):
                break
            hit = None
            running = _RUNNING.get(key)
            if running is None:
                owned = _RUNNING[key] = {"done": threading.Event(), "error": None}
                break
        # The same analysis is already running for another request. Its answer
        # is this one's too, and running a second copy beside it on half a CPU
        # only makes both slower.
        if not running["done"].wait(_RUNNING_WAIT):
            break                        # stuck: run one here rather than fail
        if running["error"] is not None:
            raise running["error"]
        # Finished: the answer is in the cache now, so look again.

    if hit is not None:
        result, runs = hit[1], hit[2]
        mine = runs.get(me)
        # The analysis is shared; the record of having run it is not. Each
        # person gets a run of their own, so it is in their history and
        # "take this trade", which only opens your own runs, works for them.
        if mine is None and record:
            mine = _record_cached_run(result, me)
            events_mod.first_analysis(app.config.get("DB_PATH"), me, mine)
            if mine is not None:
                with _CACHE_LOCK:
                    runs[me] = mine
        if dataclasses.is_dataclass(result):
            return dataclasses.replace(result, run_id=mine)
        return result

    # The cache key is deliberately not per user: an analysis of AAPL is the
    # same analysis whoever asked for it, and recomputing it per person would
    # be pure waste. Only the record of having run it belongs to someone, and
    # that is written inside engine.analyse before the result is cached.
    try:
        result = engine.analyse(symbol, cfg, interval=interval, with_news=with_news,
                                source="web", db_path=app.config.get("DB_PATH"),
                                user=me, record=record, exact=exact)
        events_mod.first_analysis(app.config.get("DB_PATH"), me, result.run_id)
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), result,
                           {me: result.run_id} if result.run_id else {})
            # Keep the cache from growing without bound in a long session.
            if len(_CACHE) > 40:
                oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
                _CACHE.pop(oldest, None)
        return result
    except Exception as exc:
        # Those waiting get the same answer, failure included: asking again at
        # once would only fail again, one after another.
        if owned is not None:
            owned["error"] = exc
        raise
    finally:
        # Only now, with the answer in the cache, are the waiters let go.
        if owned is not None:
            with _CACHE_LOCK:
                if _RUNNING.get(key) is owned:
                    _RUNNING.pop(key, None)
            owned["done"].set()


def _record_cached_run(result: engine.Analysis, user: int) -> Optional[int]:
    """Write a run of a cached analysis to one person's history."""
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            return db_mod.record_run(conn, result.bars, result.plan,
                                     result.signals, result.composite,
                                     result.regime, result.calibration,
                                     result.sentiment, source="web", user=user)
        finally:
            conn.close()
    except Exception:
        # As in engine.analyse: a database problem must never cost the
        # person the analysis they asked for.
        app.logger.exception("Could not record a run of a cached analysis")
        return None


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _number(value) -> Optional[float]:
    """A value as a finite number, or None when it is not one.

    A NaN or an infinity printed as it comes reads "nan%", "+nan" or "inf"
    on the page, which looks broken. The filters below show "-" for it, as
    they do for a missing value.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@app.template_filter("price")
def _price(value):
    number = _number(value)
    return format_price(number) if number is not None else "-"


@app.template_filter("money")
def _money(value):
    number = _number(value)
    return "{:,.2f}".format(number) if number is not None else "-"


@app.template_filter("pct")
def _pct(value, digits=0):
    number = _number(value)
    return "%.*f%%" % (digits, number * 100) if number is not None else "-"


@app.template_filter("signed")
def _signed(value, digits=2):
    number = _number(value)
    if number is None:
        return "-"
    number = round(number, digits)
    # -0.004 rounds to zero, and "-0.00" reads as a loss that is not there.
    return "%.*f" % (digits, 0.0) if number == 0 else "%+.*f" % (digits, number)


@app.template_filter("strategy")
def _strategy(name):
    """rsi_reversion as "RSI reversion", the name the public pages use too."""
    return public_mod.strategy_label(str(name or ""))


# Whether a coin's newest finished day is one the source left blank.
app.jinja_env.globals["blank_day"] = public_mod.blank_day
# A call's past examples, said as the test on past prices they are, the same
# way on every page that counts them.
app.jinja_env.globals["counts_line"] = public_mod.counts_line


@app.template_global()
def exact_arg(symbol, saved=None) -> Optional[int]:
    """exact=1 for a link or a form about Yahoo's own symbol, where it is needed.

    An analysis, a run, a thesis and a scan's row all hold Yahoo's symbol, so
    the mark goes on wherever the bare ticker reads otherwise: LINK is the
    Chainlink coin unless it says exact=1, which makes it Interlink
    Electronics. A watchlist entry or an alert carries its own answer (saved):
    one saved before there was a mark was typed into the box, and keeps the
    meaning the box gives it.
    """
    if saved is not None and not saved:
        return None
    return 1 if market_mod.needs_exact(symbol or "") else None

# The words behind each "?" beside a figure (templates/_help.html), with the
# house settings in them. The analysis page passes its own, with the
# person's settings and bar interval in them instead.
app.jinja_env.globals["help_text"] = plain_mod.help_for(config_mod.DEFAULTS)


@app.template_filter("day")
def _day(ts):
    try:
        return time.strftime("%d %b %Y", time.gmtime(int(ts))).lstrip("0")
    except (TypeError, ValueError):
        return "-"


@app.template_filter("ago")
def _ago(ts):
    try:
        secs = max(0, int(time.time()) - int(ts))
    except (TypeError, ValueError):
        return "-"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return "%dm ago" % (secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


@app.context_processor
def inject_globals():
    return {"intervals": INTERVALS, "families": FAMILIES}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# A real answer on the front door, so a stranger sees what the app does before
# being asked to sign in with Google. Someone arriving from a video will not
# hand an unknown app their Google account just to find out what it is; they
# will leave. One real call, in plain words, does the persuading instead.
#
# The front door must never wait on market data, so this is computed in the
# background and the page only ever reads what is already there. The very
# first visitor after a restart sees the page without it; everyone after sees
# a real analysis, labelled with how old it is.
EXAMPLE_SYMBOL = "NVDA"
EXAMPLE_TTL = 15 * 60
_example_state = {"at": 0.0, "data": None, "busy": False}
_example_lock = threading.Lock()


def _refresh_example() -> None:
    try:
        from bot import engine as engine_mod
        from bot import plain as plain_mod
        cfg = config_mod.load(app.config.get("CFG_PATH"))
        result = engine_mod.analyse(EXAMPLE_SYMBOL, cfg, interval="1d",
                                    with_news=False, with_learning=True,
                                    record=False)
        said = plain_mod.explain_plan(result.plan, result.bars)
        plan = result.plan
        samples = plan.prob_samples or 0
        data = {"symbol": result.bars.symbol,
                "samples": samples,
                "worked": (int(round(plan.probability * samples))
                           if plan.probability is not None and samples else 0),
                # The odds are the side the signals lean. Leaning down, the
                # examples are bets on a fall, and the front page says so.
                "fall": (getattr(plan, "odds_direction", 0) or 0) < 0,
                "action": result.plan.action,
                "headline": said["headline"],
                "sure": said["sure"],
                "price": result.bars.last_price,
                "at": time.time()}
        with _example_lock:
            _example_state["data"] = data
            _example_state["at"] = data["at"]
    except Exception:
        # A missing example costs nothing; a front door that errors costs the
        # visitor. The page simply renders without it.
        pass
    finally:
        with _example_lock:
            _example_state["busy"] = False


def _example():
    """The cached example, starting a refresh if it is stale. Never blocks."""
    now = time.time()
    with _example_lock:
        data = _example_state["data"]
        stale = data is None or now - _example_state["at"] >= EXAMPLE_TTL
        if stale and not _example_state["busy"]:
            _example_state["busy"] = True
            threading.Thread(target=_refresh_example, daemon=True).start()
    if data is None:
        return None
    # Say how old it is rather than presenting a quarter-hour-old call as live.
    return dict(data, age_min=int((now - data["at"]) // 60))


@app.route("/")
def index():
    # A signed-out visitor gets the landing page here rather than a redirect to
    # /signin. Search engines are reluctant to index a login page, and a
    # redirect from the front door is the weakest thing a crawler can be
    # handed: the address everyone shares would have had nothing behind it.
    # It is also the first thing a person arriving from a link sees, and a bare
    # login form asks them to commit before they know what this is.
    if getattr(g, "user", None) is None:
        events_mod.landing_view(app.config.get("DB_PATH"), request.method,
                                request.headers.get("User-Agent", ""))
        return render_template("landing.html", example=_example())

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        runs = db_mod.recent_runs(conn, user=uid(), limit=12)
        # As held now: a position closed in part counts only what is left.
        open_trades = db_mod.open_positions(conn, limit=20, user=uid())
        summary = db_mod.journal_summary(conn, user=uid(), costs=_costs())
        counts = db_mod.stats(conn, user=uid())
        stored = _attention_stored(conn, uid())
    finally:
        conn.close()
    attention = _attention(stored, open_trades)
    # What the prices just read saw is kept, as the journal keeps it: a dip
    # through a stop between two rounds of the background check may be seen
    # here alone. Nothing, if the prices were late.
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        _keep_trail_bests(conn, open_trades, attention["marks"])
    finally:
        conn.close()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return render_template("index.html", runs=runs,
                           open_trades=_held(open_trades, attention["marks"]),
                           summary=summary, counts=counts, attention=attention,
                           has_key=has_key,
                           ai_ok=has_key and _ai_consented())


# The signed-in front page starts with what needs the person now. It is built
# from what is already saved: their own records, and the popular checks the
# background job has already run. Nothing here analyses or scans anything, so
# the page stays as quick as the rest. The one thing fetched is the price of
# each open position, the way the journal fetches it, and the page waits only
# so long for that.
ATTENTION_DAYS = 3          # an alert that fired this recently is still news
DASHBOARD_WAIT = 4.0        # seconds the page waits for open positions' prices
POPULAR_FRESH = 24 * 3600   # a popular check older than this is not today's
ATTENTION_SHOWN = 6         # rows per list before "and N more"
WATCH_SHOWN = 8


def _quote_day(snap: Dict) -> str:
    """The session a popular check's price is from, when not the check's own day.

    On a weekend a stock's price is Friday's close, and "checked 3h ago" on its
    own would pass that off as a price three hours old.
    """
    day, at = snap.get("price_day"), snap.get("at")
    if day is None or not at or int(day) >= int(at) // 86400:
        return ""
    return time.strftime("%d %b", time.gmtime(int(day) * 86400)).lstrip("0")


def _latest_runs(conn, me: int, symbols) -> Tuple[Dict, Dict]:
    """This person's latest run of each symbol: by (symbol, interval), and overall."""
    symbols = sorted(set(symbols))
    by_pair: Dict = {}
    by_symbol: Dict = {}
    if not symbols:
        return by_pair, by_symbol
    # With one MAX() in the query, SQLite takes the other columns from the
    # row that holds it: the latest run of each pair.
    rows = conn.execute(
        "SELECT symbol, interval, action, price, market_open, MAX(ts) AS ts "
        "FROM runs WHERE user_id=? AND symbol IN (%s) GROUP BY symbol, interval"
        % ",".join("?" * len(symbols)), [int(me)] + symbols).fetchall()
    for row in rows:
        by_pair[(row["symbol"], row["interval"])] = row
        best = by_symbol.get(row["symbol"])
        if best is None or row["ts"] > best["ts"]:
            by_symbol[row["symbol"]] = row
    return by_pair, by_symbol


def _watched_instrument(row) -> str:
    """Yahoo's symbol for what a watchlist entry opens, to look its calls up by.

    An entry saved before the exact mark as a bare LINK opens the Chainlink
    coin, so its calls are LINK-USD's, never those of Interlink Electronics,
    whose analyses are kept under LINK.
    """
    try:
        return market_mod.normalise_symbol(row["symbol"], exact=bool(row["exact"]))
    except DataError:
        return row["symbol"]


def _attention_stored(conn, me: int) -> Dict:
    """The front page's first section, as far as the database has it."""
    now = time.time()
    fired = [dict(e) for e in db_mod.event_list(conn, me, limit=30)
             if e["ts"] >= now - ATTENTION_DAYS * 86400]
    popular = {v["symbol"]: v for v in public_mod.verdicts(conn) if v.get("at")}
    fresh = [v for v in popular.values() if now - v["at"] <= POPULAR_FRESH]
    watched = db_mod.watchlist(conn, user=me)
    shown = watched[:WATCH_SHOWN]
    meant = {w["symbol"]: _watched_instrument(w) for w in shown}
    by_pair, by_symbol = _latest_runs(conn, me, meant.values())
    # The background job's daily check of anything else watched. The free
    # plan is shown calls only on the popular stocks (digest.watched).
    full = _pro()
    checks = db_mod.watch_calls(conn, list(meant.values()), 1) if full else {}
    watch = []
    for w in shown:
        symbol, interval = w["symbol"], w["interval"] or "1d"
        instrument = meant[symbol]
        # The public check is a daily one, so it answers only a daily watch.
        snap = popular.get(instrument) if interval == "1d" else None
        daily = (checks.get(instrument) or [None])[0] if interval == "1d" else None
        own = by_pair.get((instrument, interval)) or by_symbol.get(instrument)
        item = {"symbol": symbol, "interval": interval, "exact": bool(w["exact"])}
        if snap and snap.get("action"):
            item.update(call=snap["action"], at=snap["at"], price=snap.get("price"),
                        public=True, quote_day=_quote_day(snap),
                        slug=public_mod.slug(instrument))
        elif daily is not None and daily["action"]:
            item.update(call=daily["action"], at=daily["at"], price=daily["price"],
                        public=False, daily=True)
        elif own is not None and own["action"]:
            item.update(call=own["action"], at=own["ts"], price=own["price"],
                        public=False, run_interval=own["interval"],
                        closed=not own["market_open"])
        watch.append(item)
    # What changed since the session before, from the calls the background
    # job wrote down.
    since = digest_mod.since_yesterday(conn, me, full, now)
    since["notes"] = digest_mod.notes(since)
    return {"fired": fired, "watch": watch, "since": since, "full": full,
            "watch_more": max(0, len(watched) - len(shown)),
            "checked": bool(fresh),
            "buys": [dict(v, quote_day=_quote_day(v), slug=public_mod.slug(v["symbol"]),
                          short=public_mod.short(v["symbol"]))
                     for v in fresh if v.get("action") == "BUY"]}


def _marks_within(open_trades, wait: float):
    """_mark_to_market's answer, and whether it was still not back after wait."""
    if not open_trades:
        return {}, False
    done: Dict = {}
    worker = threading.Thread(target=lambda: done.update(_mark_to_market(open_trades)),
                              name="orenth-front-page-prices", daemon=True)
    worker.start()
    worker.join(wait)
    if worker.is_alive():
        # Left to finish on its own: what it fetches is kept for a minute, so
        # the journal or the next look at this page gets it straight away.
        return {}, True
    return done, False


def _attention(stored: Dict, open_trades) -> Dict:
    """What needs the person now: stops and targets, alerts, watchlist, BUYs."""
    marks, late = _marks_within(open_trades, DASHBOARD_WAIT)
    positions = [{"trade": t, "mark": marks[t["id"]]} for t in open_trades
                 if (marks.get(t["id"]) or {}).get("status") in ("past stop",
                                                                 "past target")]
    crossed = {p["trade"]["id"] for p in positions
               if p["mark"]["status"] == "past stop"}
    # The background check writes a crossed stop into the alert history as
    # well. A position listed as past its stop is not said a second time.
    fired = [e for e in stored["fired"]
             if not (e["kind"] == "stop_breach" and e["trade_id"] in crossed)]
    # Marked "no data" (no price, or a split that could not be ruled out):
    # not checked, so the page must not say it is not past its stop.
    unchecked = sum(1 for t in open_trades
                    if (marks.get(t["id"]) or {}).get("status") == "no data")
    buys = stored["buys"]
    return {"positions": positions, "late": late, "crossed": len(crossed),
            "unchecked": unchecked, "marks": marks,
            "fired": fired[:ATTENTION_SHOWN],
            "fired_more": max(0, len(fired) - ATTENTION_SHOWN),
            "watch": stored["watch"], "watch_more": stored["watch_more"],
            "since": stored["since"], "full": stored["full"],
            "buys": buys[:ATTENTION_SHOWN],
            "buys_more": max(0, len(buys) - ATTENTION_SHOWN),
            "checked": stored["checked"],
            "any": bool(positions or fired or unchecked or stored["watch"] or buys)}


def _held(open_trades, marks) -> list:
    """The front page's open positions, at the stop in force and the size still held.

    A trail moves the stop and closing part of a position shrinks it, so
    stop_now is the stop the journal reads (the position's mark), and
    at_risk what that stop would lose from the entry on what is left:
    nothing once a trail has carried it past the entry. A position whose
    price did not come back in time shows the stop it was opened with.
    """
    out = []
    for t in open_trades:
        stop = (marks.get(t["id"]) or {}).get("stop", t["stop"])
        left = t["quantity"]
        at_risk = (None if left is None else
                   max(0.0, (float(t["entry"]) - float(stop)) * int(t["direction"]))
                   * float(left))
        out.append(dict(t, stop_now=stop, at_risk=at_risk))
    return out


def _safe_back(raw, fallback):
    """A redirect target from a form field, restricted to this app.

    A bare "//host" or "https://host" in a redirect is an open redirect, so only
    a single-slash relative path is accepted. Control characters and
    backslashes are refused too: the Location header drops a tab or newline,
    so "/<tab>/host" left the server as "//host", and browsers read a
    backslash as a slash.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    if any(ch < " " or ch in "\x7f\\" for ch in raw):
        return fallback
    return raw


def _symbol_from(values) -> Tuple[str, bool]:
    """The ticker a request names, and whether it names Yahoo's symbol exactly.

    exact=1 comes with a pick from the suggestions and with every link the
    site makes to a listing whose ticker is also a coin's: LINK alone is the
    Chainlink coin, LINK with exact=1 is Interlink Electronics. It is kept
    only where it changes the answer, so AAPL with or without it is one
    analysis, one cache entry and one free look.
    """
    symbol = (values.get("symbol") or "").strip()
    return symbol, values.get("exact") == "1" and market_mod.needs_exact(symbol)


def _canonical(endpoint: str, bars) -> str:
    """The public address of an analysis or research page, instrument and all.

    Without the query every analysis gave /analyse as its own address, so a
    shared Interlink Electronics page and a Chainlink one named the same page.
    """
    return _public_base() + url_for(endpoint, symbol=bars.symbol,
                                    interval=bars.interval,
                                    exact=exact_arg(bars.symbol))


@app.route("/analyse")
def analyse():
    symbol, exact = _symbol_from(request.args)
    interval = request.args.get("interval") or "1d"
    with_news = request.args.get("news", "1") != "0"
    if not symbol:
        return redirect(url_for("index"))

    if g.demo:
        # The example can open a fixed set of instruments on daily bars, each
        # computed once and shared. Anything else would let a crawler make the
        # server analyse every ticker it can think of.
        if symbol.upper() not in demo_mod.SYMBOLS:
            return _locked("Create an account to look up %s" % symbol.upper()[:12],
                           "Without an account you can look around the example. "
                           "With one, you can ask about any stock or coin.")
        if interval != "1d":
            return _locked("Create an account to change the timeframe",
                           "The example shows daily bars. With an account you "
                           "can look at any timeframe.")

    cfg = _cfg()
    if request.args.get("account"):
        try:
            cfg["account_size"] = float(request.args["account"])
        except ValueError:
            pass
    if request.args.get("risk"):
        try:
            cfg["risk_per_trade_pct"] = float(request.args["risk"])
        except ValueError:
            pass
    if request.args.get("shorts") == "1":
        cfg["allow_shorts"] = True


    if not _allowance_left(symbol, interval, exact):
        return _allowance_used()

    if g.demo:
        # Address overrides would give every visitor their own cache entry.
        cfg = config_mod.load(app.config.get("CFG_PATH"))
    try:
        result = cached_analysis(symbol, interval, cfg, with_news,
                                 record=not g.demo,
                                 ttl=DEMO_TTL if g.demo else None, exact=exact)
    except DataError as exc:
        return _no_prices(exc, symbol, 404)
    except Exception as exc:                        # keep the app usable
        # No symbol for the page: its advice about tickers that do not exist
        # is for a ticker the source refused, not for this. _plainly logs the
        # exception and shows its text only to whoever runs the server.
        said = ("The analysis of %s could not be finished. Try again in a minute."
                % symbol.upper()[:20])
        detail = _plainly(exc, said)
        return render_template("error.html", symbol="", message=(
            said if detail == said else "%s (%s)" % (said, detail))), 500
    _spend_view(symbol, interval, exact)

    families = _family_rollup(result)
    # What price is doing (levels, the clock, the day's lean): a part that
    # cannot be worked out must not cost the person the analysis.
    try:
        from bot import levelview as levelview_mod
        extras = levelview_mod.for_analysis(result, cfg, db_path=app.config.get("DB_PATH"))
    except Exception:
        app.logger.exception("Could not read the levels for %s", result.bars.symbol)
        extras = {}
    ranked = sorted(result.signals, key=lambda s: -abs(s.contribution))
    # "10 bars" means nothing without the interval attached to it.
    hold_text = config_mod.describe_horizon(
        result.bars.interval, int(result.calibration.avg_bars_held or 0))
    # Alerts worth setting for this specific setup, so the levels the plan
    # depends on can be watched without retyping them.
    suggestions = []
    for spec in alerts_mod.suggest_for(result.bars.symbol, result.plan, result.bars):
        spec = dict(spec)
        spec["label"] = alerts_mod.describe(spec["kind"], spec["threshold"],
                                            result.bars.symbol)
        suggestions.append(spec)

    calendar_rows = (catalysts_mod.describe(result.calendar)
                     if result.calendar else [])

    # Whether this would really be a new position or more of an existing one.
    # Only costs anything when something is actually open.
    overlap = {"warnings": [], "pairs": []}
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            open_rows = db_mod.list_trades(conn, status="open", user=uid())
        finally:
            conn.close()
        if open_rows:
            overlap = portfolio_mod.overlap(open_rows, result.bars.symbol,
                                            "1d")
    except Exception:
        # A correlation check failing must not cost the user their analysis.
        overlap = {"warnings": [], "pairs": []}

    return render_template("analysis.html", r=result, cfg=cfg,
                           families=families, ranked=ranked, hold_text=hold_text,
                           plan=result.plan, calib=result.calibration,
                           suggestions=suggestions, calendar_rows=calendar_rows,
                           overlap=overlap,
                           chart=_trade_chart(result, calm=bool(cfg.get("calm_candles"))),
                           plain=plain_mod.explain_plan(result.plan, result.bars,
                                                        hold_text),
                           help_text=plain_mod.help_for(cfg, result.bars.interval),
                           # A visitor cannot make a share, but every example
                           # symbol has a public page to pass around instead.
                           popular_url=(url_for("stock_page", sym=public_mod.slug(
                               result.bars.symbol))
                               if result.bars.symbol in public_mod.NAMES else ""),
                           canonical_url=_canonical("analyse", result.bars),
                           extras=extras)


def _trade_chart(result, calm: bool = False) -> Dict:
    """The analysis page's candle chart, from the bars already in hand.

    Drawn twice, wide and narrow, and the page shows one: scaled down to a
    phone, the wide drawing's labels shrink to a few pixels, so a phone gets
    fewer candles drawn larger instead. An AVOID has no plan, so only the
    prices are drawn.
    """
    bars, plan = result.bars, result.plan
    action = (plan.action or "").upper()
    levels = {}
    if plan.entry and action != "AVOID":
        side = plan.direction or (1 if (plan.stop or 0) < plan.entry else -1)
        levels = {"Buy at" if side > 0 else "Short at": plan.entry,
                  "Stop": plan.stop, "Target": plan.target1}
    as_of = alerts_mod.as_of(bars)
    drawn = dict(levels=levels, last=bars.last_price, as_of=as_of,
                 interval=bars.interval, calm=calm)
    if charts_mod.bar_words(bars.interval) != "daily":
        # The course, and most day traders, read intraday candles in New York time.
        from bot import levelview as levelview_mod
        as_of = levelview_mod.ny_as_of(bars)
        drawn.update(as_of=as_of, day_label=levelview_mod.ny_day_label)
    series = (bars.timestamp, bars.open, bars.high, bars.low, bars.close,
              bars.volume)
    try:
        wide = charts_mod.trade_chart(*series, **drawn)
        narrow = charts_mod.trade_chart(*series, width=340, height=260, bars=40,
                                        **drawn)
    except Exception:
        # A chart that cannot be drawn must not cost the person the analysis.
        app.logger.exception("Could not draw the trade chart for %s", bars.symbol)
        return {}
    if not wide:
        return {}
    return {"wide": wide, "narrow": narrow or wide, "as_of": as_of,
            "levels": bool(levels), "words": charts_mod.bar_words(bars.interval),
            "intraday": charts_mod.bar_words(bars.interval) != "daily"}


def _family_rollup(result: engine.Analysis):
    """Aggregate signals per family so the page shows structure, not a list.

    Only the strategies that vote: those on trial have no say in the call, so
    they are left out of the family leans, and a family with no voting member
    is not shown at all rather than as "0 active".
    """
    rows = []
    trial = strategies_mod.TRIAL
    voting = {s.family for s in strategies_mod.REGISTRY if s.votes}
    for family in FAMILIES:
        if family not in voting:
            continue
        group = [s for s in result.signals
                 if s.family == family and s.weight > 0 and abs(s.score) > 0.05
                 and s.name not in trial]
        total = sum(1 for s in result.signals if s.family == family and s.name not in trial)
        if not group:
            rows.append({"name": family, "lean": 0.0, "active": 0,
                         "total": total, "loudest": None})
            continue
        wsum = sum(s.weight for s in group)
        lean = (sum(s.contribution for s in group) / wsum) if wsum else 0.0
        rows.append({"name": family, "lean": lean, "active": len(group),
                     "total": total,
                     "loudest": max(group, key=lambda s: abs(s.contribution))})
    rows.sort(key=lambda r: -abs(r["lean"]))
    return rows


@app.route("/trades")
def trades():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        open_trades = db_mod.list_trades(conn, status="open", limit=50,
                                         user=uid())
        closed = db_mod.list_trades(conn, status="closed", limit=100,
                                    user=uid())
        summary = db_mod.journal_summary(conn, user=uid(), costs=_costs())
        every = list(open_trades) + list(closed)
        exits = db_mod.exits_for(conn, [t["id"] for t in every])
        # What each trade's exits add up to: what is left of a position
        # closed in part, and the whole of one closed in parts.
        parts = {t["id"]: db_mod.trade_parts(t, exits.get(t["id"], [])) for t in every}
        live = _mark_to_market(open_trades)
        _keep_trail_bests(conn, open_trades, live)
    finally:
        conn.close()
    # Said once, after an exit, by the request that wrote it. Kept in the
    # session rather than the address, so a link cannot put words here; and
    # looked for first, so a visitor's session is not touched.
    said = session.pop(EXIT_SAID, "") if EXIT_SAID in session else ""
    return render_template("trades.html", open_trades=open_trades,
                           closed=closed, summary=summary, live=live,
                           parts=parts, said=said, tags=_journal_lists())


def _journal_lists() -> Dict:
    """The journal's lists for its forms, and when a trade may be entered.

    The entry time is a New York date and time, filled in with now; the
    form says it back unchanged when it was not touched, so the trade is
    logged at the moment it is sent rather than when the page was drawn.
    """
    from bot import journal as journal_mod
    from bot import setups as setups_mod
    now = int(time.time())
    return {"setups": setups_mod.JOURNAL_SETUPS, "emotions": setups_mod.EMOTIONS,
            "checklist": setups_mod.CHECKLIST, "followed": setups_mod.FOLLOWED,
            "entry_now": journal_mod.entry_field(now),
            "entry_min": journal_mod.entry_field(now - journal_mod.BACKDATE_DAYS * 86400),
            "tags_of": journal_mod.tags_of, "broke_of": journal_mod.broke_rules_of,
            "checklist_of": journal_mod.checklist_of,
            "setup_labels": journal_mod.SETUP_LABELS,
            "emotion_labels": journal_mod.EMOTION_LABELS,
            "followed_labels": journal_mod.FOLLOWED_LABELS}


def _tags_asked() -> Dict:
    """The setup, feeling, checklist and entry time a log-a-trade form sends.

    A checklist with nothing ticked counts as not used: a form cannot tell
    a person who went through it and ticked nothing from one who skipped it.
    """
    from bot import journal as journal_mod
    setup = (request.form.get("setup") or "").strip() or None
    emotion = (request.form.get("emotion") or "").strip() or None
    checklist = [k for k in request.form.getlist("checklist") if k] or None
    journal_mod.check_tags(setup=setup, emotion=emotion, checklist=checklist)
    typed = (request.form.get("opened") or "").strip()
    opened = None
    if typed and typed != (request.form.get("opened_shown") or "").strip():
        opened = journal_mod.check_opened(journal_mod.parse_entry(typed))
    return {"setup": setup, "emotion": emotion, "checklist": checklist,
            "opened_ts": opened}


def _rules_stop(conn, cfg: Dict, opened_ts: Optional[int]):
    """The daily rules a trade about to be logged breaks: (page, broke_rule).

    page is the "your rule says stop" page to show instead of logging it, or
    None. When the person said to log it anyway (override=1), there is no
    page, and broke_rule names the rules it broke, to keep with the trade.
    """
    from bot import journal as journal_mod
    broken = journal_mod.rules_check(conn, uid(), cfg, opened_ts, costs=_costs(cfg))
    if not broken:
        return None, None
    if request.form.get("override") == "1":
        return None, ",".join(b["rule"] for b in broken)
    # Every field the form sent, to send again with override=1.
    fields = [(k, v) for k in request.form for v in request.form.getlist(k)
              if k != "override"]
    page = render_template("rule_stop.html", said=journal_mod.rules_words(broken),
                           broken=broken, fields=fields, action=request.path,
                           settings_url=url_for("settings") + "#rules")
    return page, None


def _keep_trail_bests(conn, open_trades, marks) -> None:
    """Keep the best price each trailing stop was just read with, and a crossing.

    Read here over the trade's whole history, it holds the trail up for a
    reader with fewer bars, such as the background check. Only ever raises
    a long's best (lowers a short's), so it changes nothing else. A crossing
    of any stop seen here is kept too, the first one only, so it is still a
    crossing for every reader once that day's bar is finished, and once it
    is older than the trade's bars reach.
    """
    for t in open_trades:
        mark = marks.get(t["id"]) or {}
        if mark.get("trail") is not None and mark.get("best_at") is not None:
            db_mod.trail_seen(conn, int(t["id"]), mark["best"], mark["best_at"])
        if mark.get("crossed"):
            db_mod.stop_crossed(conn, int(t["id"]), *mark["crossed"])


# The session key for what the journal says once after an exit.
EXIT_SAID = "journal_exit_said"


def _exit_words(symbol: str, done: Dict, typed: float, typed_qty: Optional[float],
                shares: str, split: float) -> str:
    """What the journal says once an exit is written: what was kept, and how.

    done is what exit_trade returned. A price typed in today's shares is
    said beside what it became in the shares the trade was opened with, so
    the conversion can be checked; shares is "today", "opened", or "" when
    there was no split to choose about.
    """
    q = db_mod._qty
    turned = shares == "today" and split != 1.0
    opened = " in the shares you opened with" if shares else ""
    if not done["closed"]:
        if turned:
            return ("Recorded: %s of today's %s shares at %s, which is %s at %s in the "
                    "shares you opened with. %s of those is still open."
                    % (q(typed_qty), symbol, format_price(typed), q(done["quantity"]),
                       format_price(done["exit"]), q(done["left"])))
        return ("Recorded: %s of %s closed at %s%s. %s of it is still open."
                % (q(done["quantity"]), symbol, format_price(done["exit"]), opened,
                   q(done["left"])))
    said = ("Closed %s at %s%s: %sR." % (symbol, format_price(done["exit"]), opened,
                                         _signed(done["r_multiple"])))
    if done["parts"]:
        said = ("Closed %s in %d parts at %s on average%s: %sR on the whole position."
                % (symbol, done["parts"], format_price(done["average"]), opened,
                   _signed(done["r_multiple"])))
    if turned:
        said += (" %s in today's shares is %s in the shares you opened with."
                 % (format_price(typed), format_price(done["exit"])))
    return said


def _trade_bars(trade):
    """A journal trade's own bars, and its shares today per share the day it opened.

    The bars the journal marks it with. The split is over the trade's whole
    life: an intraday chart reaches back sixty days, and a split older than
    that is not in its bars. Raises DataError when either cannot be had.
    """
    bars = market_mod.fetch_bars(trade["symbol"], trade["interval"] or "1d")
    return bars, market_mod.split_factor_since(trade["symbol"],
                                               int(trade["ts_opened"]), bars)


def _exit_split(trade) -> Optional[float]:
    """A trade's shares today per share it opened with, to keep with an exit, or None.

    Kept so the exports can give a sale in the shares a broker lists. The
    exit itself is in the trade's own shares and does not need it, so a
    price source that does not answer leaves it unknown rather than holding
    the exit back.
    """
    try:
        return _trade_bars(trade)[1]
    except DataError:
        return None


def _mark_one(trade):
    """Current price, open R, the stop in force and the state of one position.

    In the shares of the day it opened, as its entry, stop and target are.
    After a split Yahoo quotes today's shares, so the quote is turned back,
    and what it is today is said beside it. The stop is read by the rule the
    portfolio, paper.py and the background notice read it by
    (portfolio.read_stop), so a trail moves it the same way everywhere.
    """
    bars, split = _trade_bars(trade)
    direction = int(trade["direction"])
    entry, stop = float(trade["entry"]), float(trade["stop"])
    read = portfolio_mod.read_trade_stop(trade, bars, split)
    price = read.price
    # R is measured from the stop the trade opened with, wherever it is now.
    risk = abs(entry - stop)
    r_now = ((price - entry) * direction / risk) if risk > 0 else 0.0

    target = trade["target1"]
    status = "open"
    # A stop that traded and recovered was still hit: the plan said get out.
    if read.hit:
        status = "past stop"
    elif target and ((price >= float(target)) if direction > 0
                     else (price <= float(target))):
        status = "past target"
    return {"price": price, "r": r_now, "status": status,
            "as_of": alerts_mod.as_of(bars), "quoted": bars.last_price,
            "split": split,
            "split_words": market_mod.split_words(split) if split != 1.0 else "",
            # The stop in force now, and how a trail has moved it.
            "stop": read.stop, "moved": read.moved, "trail": read.trail,
            "best": read.best, "best_at": read.best_at,
            # Where and when the stop was traded through, in the same shares.
            "touched": read.touched, "touched_at": read.touched_at,
            "touched_stop": read.touched_stop,
            # The crossing to keep, when the stop trails (_keep_trail_bests).
            "crossed": read.crossing(time.time())}


def _mark_to_market(open_trades):
    """Mark every open position, fetching them concurrently.

    Each fetch is roughly a quarter of a second of network wait. Done one after
    another, twenty positions would hang the page for five seconds; done at the
    same time it stays under one. Failures are per-trade, so one delisted
    symbol cannot blank the whole table.
    """
    out = {}
    if not open_trades:
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = min(8, len(open_trades))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_mark_one, t): t["id"] for t in open_trades}
        for future in as_completed(futures):
            trade_id = futures[future]
            try:
                out[trade_id] = future.result()
            except Exception:
                out[trade_id] = {"price": None, "r": None, "status": "no data"}
    return out


def _form_number(name: str, label: str, required: bool = True) -> Optional[float]:
    """A number typed into a form, or a ValueError that says which, in words."""
    import math
    raw = (request.form.get(name) or "").strip()
    if not raw:
        if required:
            raise ValueError("%s is needed." % label)
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s must be a number." % label)
    if not math.isfinite(value):
        raise ValueError("%s must be an ordinary number." % label)
    return value


def _own_trade(conn, trade_id: int):
    """One of the signed-in person's own journal trades, or a ValueError."""
    sql, args = db_mod._own("SELECT * FROM trades WHERE id=?", [trade_id], uid())
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ValueError("There is no such trade in your journal.")
    return row


def _trail_asked(symbol: str, interval: str) -> Dict:
    """The trailing stop a form asks for on a new trade, as open_trade takes it.

    Empty when none is asked for. An ATR trail is measured now, on the bars
    the journal will mark the trade with. When they cannot be read, the trade
    is refused with the reason, rather than logged without the trail asked for.
    """
    amount = _form_number("trail", "The trail", required=False)
    if amount is None:
        return {}
    if request.form.get("trail_kind") != "atr":
        db_mod.check_trail(amount / 100.0, None, None)
        return {"trail_pct": amount / 100.0}
    db_mod.check_trail(None, None, amount)
    try:
        unit = portfolio_mod.atr_of(market_mod.fetch_bars(symbol, interval))
    except DataError:
        unit = None
        why = "Could not read %s's prices just now to measure its ATR." % symbol
    else:
        why = "There is not enough price history to measure %s's ATR." % symbol
    if unit is None:
        raise ValueError(why + " Trail it by a percentage instead, or add the trail "
                               "from the journal later.")
    return {"trail_dist": amount * unit, "trail_atr": amount}


# The reasons an exit can be filed under: the form's three, and two more
# paper.py offers.
EXIT_REASONS = ("target", "stop", "manual", "timeout", "scratch")


@app.route("/trades/close", methods=["POST"])
def close_trade():
    """Close a position, or part of it, at a price.

    The journal keeps a trade in the shares it was opened with. After a split
    the form asks which shares the typed price (and size) are in, defaulting
    to today's, which is what a broker shows, and one in today's shares is
    turned into the trade's own here. The split is looked up again rather
    than taken from the page. The form says what it last saw open, so a
    second press of Close does not close a second part.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        trade_id = int(_form_number("trade_id", "The trade"))
        price = _form_number("price", "The exit price")
        quantity = _form_number("quantity", "The size to close", required=False)
        # What the page showed still open: a size, or "all" for a trade
        # logged without one. Anything else is not from the page, and is
        # not held against the trade.
        seen = (request.form.get("left") or "").strip()
        try:
            expect_left = float(seen) if seen and seen != "all" else None
        except ValueError:
            seen, expect_left = "", None
        reason = request.form.get("reason", "manual")
        if reason not in EXIT_REASONS:
            reason = "manual"
        shares = request.form.get("shares", "")
        shares = shares if shares in ("today", "opened") else ""
        typed, typed_qty, split = price, quantity, 1.0
        row = _own_trade(conn, trade_id)
        if shares == "today":
            try:
                _bars, split = _trade_bars(row)
            except DataError as exc:
                wait = _source_wait(exc)
                page = app.make_response((render_template(
                    "error.html", symbol="", throttled=wait is not None,
                    message="Could not check %s for a share split just now, so a price "
                            "in today's shares cannot yet be turned into the shares you "
                            "opened with. Try again in a minute, or give the price in "
                            "the shares you opened with." % row["symbol"]), 503))
                page.headers["Retry-After"] = str(wait or UNAVAILABLE_RETRY)
                return page
            price *= split
            if quantity is not None:
                quantity /= split
        done = db_mod.exit_trade(conn, trade_id, price, quantity, reason,
                                 request.form.get("notes", ""), user=uid(),
                                 expect_left=expect_left, expect_open=bool(seen),
                                 split=split if shares == "today" else _exit_split(row))
        session[EXIT_SAID] = _exit_words(row["symbol"], done, typed, typed_qty, shares,
                                         split)
    except db_mod.TradeChanged:
        # Most often Close pressed twice: the first press did the work.
        return redirect(url_for("trades", changed=trade_id))
    except ValueError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


@app.route("/trades/trail", methods=["POST"])
def trail_trade():
    """Trail an open position's stop behind the best price since it opened.

    By a share of that price, or by a multiple of the ATR of the trade's own
    bars, measured now and kept as it is. The stop only ever tightens, so a
    trail that would loosen it, put it past today's price, or move the stop
    of a position already past it is refused, saying why.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        trade_id = int(_form_number("trade_id", "The trade"))
        amount = _form_number("trail", "The trail")
        atr = request.form.get("trail_kind") == "atr"
        pct = None if atr else amount / 100.0
        db_mod.check_trail(pct, None, amount if atr else None)
        row = _own_trade(conn, trade_id)
        if row["status"] != "open":
            raise ValueError("That trade is closed, so it has no stop to trail.")
        try:
            bars, split = _trade_bars(row)
        except DataError as exc:
            return _no_prices(exc, status=503, message=(
                "Could not read %s's prices just now, so the trail cannot be set "
                "yet. Try again in a minute." % row["symbol"]))
        trail, before, after = portfolio_mod.trail_from(
            row, bars, split, pct=pct, atr=amount if atr else None)
        problem = portfolio_mod.trail_problem(row["symbol"], int(row["direction"]),
                                              before, after)
        if problem:
            raise ValueError(problem)
        db_mod.set_trail(conn, trade_id, pct=trail.pct, distance=trail.distance,
                         atr=trail.atr, best=after.best, best_at=after.best_at,
                         user=uid())
    except ValueError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


class _NoTrade(ValueError):
    """A recorded analysis with no trade in it to log, said for the person."""


def _run_split(row) -> float:
    """Shares today per share on the day a plan was made: 10.0 after a ten-for-one.

    A plan's prices are in the shares of its day. Confirmed after a split,
    they were logged as if made today, and the journal and the stop check
    then read them against a quote ten times smaller. A plan under an hour old
    is not looked up: a split takes effect at a session's open, and one made
    in the hour before that open and confirmed after it is the only miss.
    """
    made = int(row["ts"] or 0)
    if (not made or time.time() - made < 3600 or row["asset_class"] == "crypto"
            or (row["symbol"] or "").upper().endswith("-USD")):
        return 1.0
    refused = _NoTrade("Could not check %s for a share split since this plan "
                       "was made, so its prices cannot yet be set beside "
                       "today's. Try again in a minute." % row["symbol"])
    try:
        # Fresh daily bars hold any split in the last three months, the
        # whole history any older one; a copy of it kept from before today
        # cannot hide today's split.
        bars = market_mod.fetch_bars(row["symbol"], "1d", lookback="3mo")
    except (market_mod.DataThrottled, market_mod.DataUnavailable):
        raise refused
    except DataError:
        # Too young for three months of daily bars, or no longer listed:
        # the whole history is all there is to go on.
        bars = None
    try:
        return market_mod.split_factor_since(row["symbol"], made, bars)
    except DataError:
        raise refused


def _paper_size(row, cfg: Dict, split: float = 1.0) -> Dict:
    """The paper position a recorded plan opens, by the journal's sizing rule.

    One rule for the review page and for logging the trade, so the size, the
    money at risk and its share of the account that are confirmed are the
    ones written down. Only a BUY or a SHORT is a trade. A WAIT names a price
    the market has not reached yet, so logging it would record a fill that
    never happened and count its make-believe gain or loss in the hit rate
    and expectancy.
    """
    if row is None or not row["entry"] or not row["stop"]:
        raise _NoTrade("That analysis has no entry and stop to trade.")
    action = (row["action"] or "").upper()
    if action == "WAIT":
        raise _NoTrade("That analysis said WAIT. Nothing has filled yet, so "
                       "there is no trade to log. Set its entry alert to hear "
                       "when the price gets there, and add the trade by hand "
                       "in the journal if you take it.")
    if action not in ("BUY", "SHORT"):
        raise _NoTrade("That analysis said %s: there is no trade in it to "
                       "simulate." % (action or "nothing"))
    # In today's shares: a plan made before a split is divided by it.
    entry, stop = float(row["entry"]) / split, float(row["stop"]) / split
    # The side is where the stop sits, which the plan keeps in step with
    # the call: below the entry for a BUY, above it for a SHORT.
    direction = 1 if stop < entry else -1
    per_unit = abs(entry - stop)
    account = float(cfg["account_size"])
    qty_raw = (account * cfg["risk_per_trade_pct"] / 100.0 / per_unit
               if per_unit > 0 else 0)
    cap = account * cfg["max_position_pct"] / 100.0
    capped = qty_raw * entry > cap
    if capped:
        qty_raw = cap / entry
    crypto = row["asset_class"] == "crypto"
    qty = round(qty_raw, 6) if crypto else float(int(qty_raw))
    if qty <= 0:
        raise _NoTrade("Account too small for one unit at this stop distance.")
    target = float(row["target1"]) / split if row["target1"] else None
    try:
        second = row["target2"]
    except (KeyError, IndexError):
        second = None
    return {"direction": direction, "entry": entry, "stop": stop,
            "target": target, "per_unit": per_unit, "quantity": qty,
            "target2": float(second) / split if second else None,
            "split_words": market_mod.split_words(split) if split != 1.0 else "",
            "quantity_text": (("%.6f" % qty).rstrip("0").rstrip(".") if crypto
                              else "{:,.0f}".format(qty)),
            "notional": qty * entry, "risk": qty * per_unit,
            "risk_pct": (qty * per_unit / account * 100.0) if account else 0.0,
            "account": account, "capped": capped,
            "reward_risk": (abs(target - entry) / per_unit
                            if target and per_unit > 0 else None)}


def _own_run(conn, run_id: int):
    """One of the signed-in person's own recorded analyses, or None."""
    sql, args = db_mod._own("SELECT * FROM runs WHERE id=?", [run_id], uid())
    return conn.execute(sql, args).fetchone()


def _logged_from(conn, run_id: int):
    """The open journal trade already made from this run, if there is one."""
    return conn.execute("SELECT * FROM trades WHERE run_id=? AND user_id=? AND "
                        "status='open' ORDER BY id LIMIT 1",
                        (run_id, uid())).fetchone()


@app.route("/trades/simulate")
def simulate_trade():
    """Show the paper trade a plan would open, before anything is written.

    Read from the stored run and the person's settings: nothing is analysed,
    so it opens at once, and Confirm logs exactly what it shows. Only a plan
    over an hour old fetches daily prices, to check for a share split since.
    """
    try:
        run_id = int(request.args.get("run_id") or "")
    except ValueError:
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        row = _own_run(conn, run_id)
        if row is None:
            abort(404)
        logged = _logged_from(conn, run_id)
    finally:
        conn.close()
    cfg = _cfg()
    try:
        # A trade at all first, so a WAIT says why before any split look-up.
        _paper_size(row, cfg)
        size = _paper_size(row, cfg, _run_split(row))
    except _NoTrade as exc:
        return render_template("error.html", symbol="", message=str(exc)), 400
    # Back to the analysis it came from. Without a page to go back to, the
    # journal, rather than the analysis again: opening that would run it,
    # and on a free account a new day's look at it costs one of the day's.
    back = (_referrer_path() if request.referrer else "") or url_for("trades")
    age = max(0, int(time.time()) - int(row["ts"] or 0))
    return render_template(
        "simulate.html", run=row, size=size, cfg=cfg, back=back, logged=logged,
        words=charts_mod.bar_words(row["interval"] or "1d"), tags=_journal_lists(),
        # A plan's entry is the price when it was analysed; the journal logs
        # that price, so a plan older than this says the price may have moved.
        stale=age > (3600 if (row["interval"] or "1d") == "1d" else 900))


@app.route("/trades/take", methods=["POST"])
def take_trade():
    """Open a paper position from a recorded plan, as the review page showed it."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        run_id = int(request.form["run_id"])
        row = _own_run(conn, run_id)
        # symbol="" on errors, because the error page's advice about mistyped
        # tickers is for an analysis that failed, and this one worked.
        _paper_size(row, _cfg())                  # a trade at all, before any look-up
        size = _paper_size(row, _cfg(), _run_split(row))
        if _logged_from(conn, run_id) is not None:
            # Confirm pressed twice: the first press already logged it.
            return redirect(url_for("trades", logged=1))
        # The plan's entry is the price when it was analysed, so its trade is
        # logged now: no other entry time is taken from this form.
        tags = _tags_asked()
        stop_page, broke = _rules_stop(conn, _cfg(), None)
        if stop_page is not None:
            return stop_page
        db_mod.open_trade(conn, symbol=row["symbol"], interval=row["interval"],
                          direction=size["direction"], entry=size["entry"],
                          stop=size["stop"], target1=size["target"],
                          target2=size["target2"], quantity=size["quantity"],
                          risk_amount=size["risk"], run_id=run_id, account="paper",
                          strategy_note="from run #%d (%s)" % (run_id, row["action"]),
                          user=uid(), setup=tags["setup"], emotion=tags["emotion"],
                          checklist=tags["checklist"], broke_rule=broke,
                          **_trail_asked(row["symbol"], row["interval"] or "1d"))
        events_mod.bump(conn, "paper_trade")
    except (ValueError, KeyError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    # The journal opens on its form for trades entered by hand, so it says
    # first that the confirmed one went in, and where to find it.
    return redirect(url_for("trades", logged=1))


@app.route("/trades/open", methods=["POST"])
def open_trade_manual():
    """Log a trade the bot did not propose, from explicit numbers.

    Without this the journal can only ever hold the bot's own suggestions,
    which makes it useless for recording what you actually did.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        direction = 1 if request.form.get("direction", "long") == "long" else -1
        entry = float(request.form["entry"])
        stop = float(request.form["stop"])
        target = request.form.get("target") or None
        qty = request.form.get("quantity") or None
        # The ticker an analysis would use, as the alert and watch forms do:
        # "btc" is Bitcoin, priced as BTC-USD, where it was the fund listed
        # as BTC. Picked from the suggestions with exact=1, BTC is that fund.
        # A journal entry is priced by its symbol as stored, so it is stored
        # as Yahoo's own, and its trail is measured on that same symbol.
        symbol, exact = _symbol_from(request.form)
        if not symbol:
            raise KeyError("symbol")
        symbol = market_mod.normalise_symbol(symbol, exact=exact)
        interval = request.form.get("interval") or "1d"
        tags = _tags_asked()
        stop_page, broke = _rules_stop(conn, _cfg(), tags["opened_ts"])
        if stop_page is not None:
            return stop_page
        db_mod.open_trade(
            conn,
            symbol=symbol, interval=interval,
            direction=direction, entry=entry, stop=stop,
            target1=float(target) if target else None,
            quantity=float(qty) if qty else None,
            risk_amount=(abs(entry - stop) * float(qty)) if qty else None,
            account="paper", strategy_note="entered by hand",
            notes=request.form.get("notes", ""), user=uid(),
            setup=tags["setup"], emotion=tags["emotion"], checklist=tags["checklist"],
            broke_rule=broke, opened_ts=tags["opened_ts"],
            **_trail_asked(symbol, interval))
        events_mod.bump(conn, "paper_trade")
    except (KeyError, ValueError, DataError) as exc:
        message = str(exc)
        if isinstance(exc, KeyError):
            message = "Instrument, entry and stop are all required."
        elif isinstance(exc, DataError):
            message = "Type a ticker, for example AAPL."
        elif "could not convert" in message or "invalid literal" in message:
            message = "Entry, stop, target and size must be numbers."
        return render_template("error.html", symbol="", message=message), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


@app.route("/trades/delete", methods=["POST"])
def delete_trade():
    """Remove a trade. A mistyped entry would otherwise skew every statistic."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        db_mod.delete_trade(conn, int(request.form["trade_id"]), user=uid())
    except (KeyError, ValueError) as exc:
        return render_template("error.html", symbol="", message=str(exc)), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


@app.route("/trades/review", methods=["POST"])
def review_trade():
    """Keep the review of a closed trade: was the plan followed, and the lesson.

    The course's habit of going over every win and loss (0:27:33), kept with
    the trade so the breakdown can say what following the plan was worth.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        trade_id = int(_form_number("trade_id", "The trade"))
        db_mod.review_trade(
            conn, trade_id, request.form.get("followed_plan"),
            request.form.get("lesson", ""), user=uid(),
            setup=request.form.get("setup") if "setup" in request.form else None,
            emotion=request.form.get("emotion") if "emotion" in request.form else None)
    except ValueError as exc:
        return render_template("error.html", symbol="", message=str(exc)), 400
    finally:
        conn.close()
    return redirect(url_for("trades", reviewed=trade_id) + "#t%d" % trade_id)


def _trade_picture(trade) -> Dict:
    """A journal trade drawn on its prices: entry, stop, target and exit.

    On the trade's own bars when they reach back to its entry, else hourly,
    else daily, and the page says which. Intraday bars are labelled in New
    York time. The trade's prices are in the shares it opened with; the
    bars are in today's, so after a split its prices are turned into
    today's to sit on them, and the page says so.
    """
    import inspect
    from bot import clock as clock_mod
    from bot import journal as journal_mod
    symbol, own = trade["symbol"], trade["interval"] or "1d"
    opened = int(trade["ts_opened"])
    closed = int(trade["ts_closed"]) if trade["ts_closed"] else None
    out = {"wide": "", "narrow": "", "note": "", "used": own, "split_words": "",
           "entered": clock_mod.label_ny(opened), "left": "",
           "own_words": charts_mod.bar_words(own)}
    if closed:
        out["left"] = clock_mod.label_ny(closed)
    bars, unreachable, short = None, False, False
    for interval in journal_mod.picture_intervals(own):
        try:
            got = market_mod.fetch_bars(symbol, interval)
        except DataError:
            unreachable = True
            continue
        if len(got.timestamp) < 10 or int(got.timestamp[0]) > opened:
            short = True
            continue
        bars, out["used"] = got, interval
        break
    if bars is None:
        out["note"] = ("Its prices could not be read just now. Try again in a minute."
                       if unreachable and not short else
                       "Prices from when it was entered are no longer available, even "
                       "as daily bars.")
        return out
    words = charts_mod.bar_words(out["used"])
    if out["used"] != own:
        out["note"] = ("Drawn on %s bars: %s bars from when it was entered are no longer "
                       "available." % (words, out["own_words"]))

    split = 1.0
    if not (symbol.endswith("-USD") or symbol.endswith("=F")):
        try:
            split = market_mod.split_factor_since(symbol, opened, bars)
        except DataError:
            split = 1.0
    if split != 1.0:
        out["split_words"] = market_mod.split_words(split)

    ts = [int(t) for t in bars.timestamp]
    n = len(ts)
    first = journal_mod.bar_at(ts, opened)
    last = journal_mod.bar_at(ts, closed) if closed else n - 1
    direction = int(trade["direction"])
    levels = {"Entry": float(trade["entry"]) / split, "Stop": float(trade["stop"]) / split}
    if trade["target1"]:
        levels["Target"] = float(trade["target1"]) / split
    exit_price = None
    if closed and trade["exit_price"]:
        exit_price = float(trade["exit_price"]) / split
        levels["Exit"] = exit_price
    intraday = words != "daily"
    drawn = {"levels": levels, "interval": out["used"],
             "as_of": alerts_mod.as_of(bars) if not closed else "",
             "last": float(bars.last_price) if not closed else None}
    if intraday:
        drawn["day_label"] = clock_mod.label_ny
    # Where the person went in and out, on charts that can mark a point.
    if "marks" in inspect.signature(charts_mod.trade_chart).parameters:
        marks = [(ts[first], levels["Entry"], direction, "Entry")]
        if exit_price is not None:
            marks.append((ts[last], exit_price, -direction, "Exit"))
        drawn["marks"] = marks

    def draw(before, after, most, **size):
        start, end = journal_mod.picture_window(n, first, last, before, after, most)
        cut = slice(start, end)
        return charts_mod.trade_chart(bars.timestamp[cut], bars.open[cut], bars.high[cut],
                                      bars.low[cut], bars.close[cut], bars.volume[cut],
                                      bars=end - start, **size, **drawn)
    try:
        out["wide"] = draw(40, 15, 160)
        out["narrow"] = draw(15, 6, 50, width=340, height=260) or out["wide"]
    except Exception:
        app.logger.exception("Could not draw the picture of trade %s", trade["id"])
        out["wide"] = out["narrow"] = ""
    if not out["wide"]:
        out["note"] = (out["note"] + " " if out["note"] else "") + (
            "There are too few prices around it to draw.")
    return out


@app.route("/trades/<int:trade_id>/picture")
def trade_picture(trade_id: int):
    """See the trade: its chart with entry, stop, target and exit, from prices.

    Drawn from the market's own bars rather than an uploaded screenshot, so
    nothing is stored. Only your own trades; a visitor sees the example's.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        try:
            row = _own_trade(conn, trade_id)
        except ValueError:
            abort(404)
        parts = db_mod.trade_parts(row, db_mod.exits_for(conn, [trade_id]).get(trade_id, []))
    finally:
        conn.close()
    from bot import journal as journal_mod
    return render_template("trade_picture.html", t=row, parts=parts,
                           pic=_trade_picture(row), tags=journal_mod.tags_of(row),
                           broke=journal_mod.broke_rules_of(row),
                           followed=journal_mod.FOLLOWED_LABELS.get(
                               row["followed_plan"] or "", ""))


@app.route("/trades/breakdown")
def journal_breakdown():
    """What your journal says: results after costs by setup, session, feeling and more.

    Each group's margin of error from ten trades; below thirty closed trades
    the page says nothing yet separates skill from luck. The loss-limit
    check walks the same trades against a limit the person types.
    """
    from bot import journal as journal_mod
    cfg = _cfg()
    limit_r, target_r, problem = 5.0, None, ""
    try:
        typed = _arg_number("limit", "The loss limit")
        if typed is not None:
            if not 0 < typed <= 100:
                raise ValueError("The loss limit must be more than 0 and at most 100R.")
            limit_r = typed
        target_r = _arg_number("target", "The profit target")
        if target_r is not None and not 0 < target_r <= 1000:
            raise ValueError("The profit target must be more than 0R.")
    except ValueError as exc:
        problem = str(exc)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        found = journal_mod.review(conn, uid(), _costs(cfg))
    finally:
        conn.close()
    limits = journal_mod.loss_limit_check(found["items"], limit_r, target_r)
    return render_template("journal_breakdown.html", r=found, limits=limits,
                           limit_r=limit_r, target_r=target_r, problem=problem,
                           min_margin=journal_mod.MIN_MARGIN,
                           min_trust=journal_mod.MIN_TRUST)


def _arg_number(name: str, label: str) -> Optional[float]:
    """A number from the address (a tool's GET form), None when empty."""
    raw = (request.args.get(name) or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        value = float(raw.rstrip("%"))
    except ValueError:
        raise ValueError("%s must be a number." % label)
    if not math.isfinite(value):
        raise ValueError("%s must be an ordinary number." % label)
    return value


@app.route("/tools/size")
def size_tool():
    """How many shares, coins or contracts a stop allows, and what that risks.

    Open to everyone. The course sizes futures by a fixed number of
    contracts (5:39:53-5:44:07); the second part shows what that risks of an
    account at a few stop sizes. Nothing is stored: the form is a GET.
    """
    from bot import journal as journal_mod
    cfg = _cfg()
    asked = {k: (request.args.get(k) or "").strip() for k in (
        "account", "risk", "entry", "stop", "target", "kind", "contract", "point_value",
        "cost", "fc_contract", "fc_count")}
    kind = asked["kind"] if asked["kind"] in journal_mod.KINDS else "shares"
    default_cost = {"shares": cfg.get("cost_bps_equity", 2.0),
                    "coins": cfg.get("cost_bps_crypto", 20.0),
                    "futures": cfg.get("cost_bps_futures", 1.0)}
    result, problem = None, ""
    account = float(cfg["account_size"])
    try:
        typed = _arg_number("account", "The account")
        if typed is not None:
            if typed <= 0:
                raise ValueError("The account must be more than zero.")
            account = typed
        risk = _arg_number("risk", "The risk")
        risk = float(cfg["risk_per_trade_pct"]) if risk is None else risk
        entry = _arg_number("entry", "The entry")
        stop = _arg_number("stop", "The stop")
        if entry is not None or stop is not None:
            if entry is None or stop is None:
                raise ValueError("Give both an entry and a stop.")
            cost = _arg_number("cost", "The cost")
            result = journal_mod.size(
                account, risk, entry, stop, kind, asked["contract"],
                _arg_number("point_value", "The dollars per point"),
                float(default_cost[kind] if cost is None else cost),
                _arg_number("target", "The target"))
    except ValueError as exc:
        problem, result = str(exc), None
    fixed, fixed_problem = None, ""
    try:
        count = _arg_number("fc_count", "The number of contracts")
        count = 3 if count is None else count
        if count != int(count) or not 1 <= count <= 1000:
            raise ValueError("The number of contracts must be a whole number from 1.")
        fixed = journal_mod.fixed_contracts(account if account > 0 else 10000.0,
                                            asked["fc_contract"] or "MES", int(count))
    except ValueError as exc:
        fixed_problem = str(exc)
    return render_template("tools_size.html", asked=asked, kind=kind, result=result,
                           problem=problem, fixed=fixed, fixed_problem=fixed_problem,
                           contracts=journal_mod.CONTRACTS, cfg=cfg,
                           default_cost=default_cost, account=account)


@app.route("/tools/survival")
def survival_tool():
    """What risking 0.5% to 10% a trade does to an account over 100 trades.

    Open to everyone, from a hit rate and an average win and loss typed in,
    or, signed in with 20 or more closed trades, from your own journal.
    """
    from bot import journal as journal_mod
    asked = {k: (request.args.get(k) or "").strip() for k in ("source", "hit", "win", "loss")}
    user = getattr(g, "user", None)
    own, closed = None, 0
    if user is not None:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            items = journal_mod.closed_results(conn, user.id, _costs())
            closed = len(items)
            if asked["source"] == "journal" and closed >= journal_mod.SURVIVAL_MIN:
                own = journal_mod.survival(rs=[i["r"] for i in items])
        finally:
            conn.close()
    result, problem = own, ""
    if own is None:
        try:
            hit = _arg_number("hit", "The hit rate")
            win = _arg_number("win", "The average win")
            loss = _arg_number("loss", "The average loss")
            hit, win, loss = (45.0 if hit is None else hit, 1.5 if win is None else win,
                              1.0 if loss is None else loss)
            result = journal_mod.survival(p=hit / 100.0, win=win, loss=loss)
        except ValueError as exc:
            problem = str(exc)
    return render_template("tools_survival.html", asked=asked, result=result,
                           problem=problem, closed=closed,
                           enough=closed >= journal_mod.SURVIVAL_MIN,
                           minimum=journal_mod.SURVIVAL_MIN,
                           wanted_journal=asked["source"] == "journal")


@app.route("/strategies")
def strategies():
    regime = request.args.get("regime", "all")
    rank_by = request.args.get("rank_by", "gross")
    symbol = request.args.get("symbol") or None
    # Which instruments the record covers says what people here have looked
    # up. A visitor is shown only the popular ones, which are public anyway.
    if g.demo and symbol and symbol.upper() not in public_mod.NAMES:
        symbol = None
    minimum = 40
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = db_mod.strategy_leaderboard(conn, symbol=symbol, regime=regime,
                                           minimum=minimum, limit=40,
                                           rank_by=rank_by)
        seen = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM strategy_stats ORDER BY symbol")]
    finally:
        conn.close()
    if g.demo:
        seen = [s for s in seen if s in public_mod.NAMES]
    return render_template("strategies.html", rows=rows, regime=regime,
                           rank_by=rank_by, symbol=symbol, seen=seen,
                           minimum=minimum)


# The formats screenshots actually come in. Pillow opens many more, and each
# extra decoder is more code an uploaded file can reach. A phone's multi-
# picture JPEG is opened by the JPEG reader.
_SCREENSHOT_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
# Decoding briefly takes about four bytes a pixel. One at a time keeps the
# worst case well inside the server's memory however many arrive together.
_DECODING = threading.BoundedSemaphore(1)
# Each read is a paid call to the vision model.
IMAGE_READS_PER_HOUR = 20


@app.route("/analyse/image", methods=["POST"])
def analyse_image():
    """Read a chart screenshot and analyse whatever instrument it shows.

    The image is only ever used to identify the instrument. Everything after
    that runs on live data, because a screenshot may already be minutes stale
    by the time it is uploaded.
    """
    # The form reader takes an upload in 64 KB pieces and holds each to the
    # limit for typed fields, which would refuse any real screenshot. The
    # picture itself is still capped by MAX_CONTENT_LENGTH.
    request.max_form_memory_size = 256 * 1024
    upload = request.files.get("chart")
    if upload is None or not upload.filename:
        return render_template("error.html", symbol="",
                               message="No image was attached."), 400

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Only the owner of a copy on their own machine can add a key, so
        # only they are told one is missing; the page goes on to say how.
        return render_template("error.html", symbol="", no_key=True,
                               message=("Reading a chart image needs an Anthropic "
                                        "API key, and none is set."
                                        if g.user.is_local else
                                        "Reading a chart from a picture is not "
                                        "switched on here.")), 400

    # A free account with no looks left could not open what the picture shows,
    # so it is not read at all rather than paid for and then refused.
    if not _pro():
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            used = billing_mod.views_today(conn, uid())
        finally:
            conn.close()
        if used >= billing_mod.free_limits()["analyses_per_day"]:
            return _allowance_used()
    if _rate_limited("image", IMAGE_READS_PER_HOUR):
        return _too_often("read screenshots")

    # Nothing a person uploads goes to another company's AI without their
    # explicit yes: the box on the Analyse page, ticked once and remembered,
    # or ticked with this picture. Asked after the allowance, so a used-up
    # one is said first; either way nothing has been sent.
    if not _ai_consented():
        if request.form.get("ai_ok") != "1":
            return render_template(
                "error.html", symbol="",
                message="To read a screenshot, Orenth sends the picture to "
                        "Anthropic, the company that makes Claude. Tick the box "
                        "under the picture area to agree, then try again. "
                        "Typing the ticker needs no picture at all."), 400
        if not g.demo and not g.user.is_local:
            conn = db_mod.connect(app.config.get("DB_PATH"))
            try:
                accounts_mod.give_ai_consent(conn, uid())
            finally:
                conn.close()

    from PIL import Image
    if not _DECODING.acquire(timeout=20):
        return render_template("error.html", symbol="",
                               message="Several screenshots are being read just "
                                       "now. Try again in a moment."), 503
    too_big = False
    try:
        # Re-encode through Pillow rather than trusting the upload: it both
        # validates that the bytes really are an image and strips anything
        # unusual the original file carried.
        img = Image.open(upload.stream, formats=_SCREENSHOT_FORMATS)
        # Only the header has been read, so the size the file claims is
        # checked before a single pixel of it is decoded.
        if img.width * img.height > MAX_PIXELS:
            too_big = True
        else:
            img.verify()
            upload.stream.seek(0)
            img = Image.open(upload.stream, formats=_SCREENSHOT_FORMATS)
            if img.mode in ("1", "P", "PA"):
                # Shrinking a palette image picks pixels instead of blending
                # them, which can make a chart's small print unreadable.
                img = img.convert("RGBA")
            # Shrunk to what the vision model uses, before anything else, so
            # no full-size copy is ever made or passed on.
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            png = io.BytesIO()
            img.save(png, format="PNG")
    except Image.DecompressionBombError:
        too_big = True
    except Exception:
        return render_template("error.html", symbol="",
                               message="That file could not be read as an image. "
                                       "PNG or JPEG works best."), 400
    finally:
        _DECODING.release()
    if too_big:
        return render_template("error.html", symbol="",
                               message="That image is too large to read. Crop it "
                                       "to just the chart and try again."), 413

    # Held in memory for this request alone. A single shared file on disk
    # let two uploads at once read, or delete, each other's picture.
    png.seek(0)
    try:
        read = read_chart(png)
    except VisionError as exc:
        return render_template("error.html", symbol="", message=_plainly(
            exc, "The picture could not be read just now. Try again in a "
                 "minute, or type the ticker.")), 502

    if not read.symbol:
        return render_template("error.html", symbol="",
                               message="No instrument could be identified in that "
                                       "image. Try a clearer screenshot, or just "
                                       "type the ticker."), 422

    interval = request.form.get("interval") or _interval_from(read.timeframe)
    return redirect(url_for("analyse", symbol=read.symbol, interval=interval,
                            fromimage="1"))


def _ai_consented() -> bool:
    """Whether this person has said yes to screenshots going to Anthropic.

    The owner of a copy on their own machine set the key up themselves, so
    the choice is theirs already. The example account is shared, so its yes
    is never kept: whoever is looking around ticks the box each time.
    """
    user = getattr(g, "user", None)
    if user is None or getattr(g, "demo", False):
        return False
    if user.is_local:
        return True
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return bool(accounts_mod.ai_consent(conn, uid()))
    finally:
        conn.close()


@app.route("/settings/ai-consent", methods=["POST"])
def ai_consent_withdraw():
    """Take back the yes to screenshots going to Anthropic."""
    if not g.demo and not g.user.is_local:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            accounts_mod.withdraw_ai_consent(conn, uid())
        finally:
            conn.close()
    return redirect(url_for("settings") + "#screenshots")


def _interval_from(timeframe: Optional[str]) -> str:
    """Map whatever the chart said its timeframe was onto one we support."""
    if not timeframe:
        return "1d"
    t = timeframe.strip().lower().replace(" ", "")
    known = {"1m": "5m", "2m": "5m", "3m": "5m", "5m": "5m", "10m": "15m",
             "15m": "15m", "30m": "30m", "45m": "30m", "1h": "1h", "60m": "1h",
             "2h": "1h", "4h": "1h", "1d": "1d", "d": "1d", "1day": "1d",
             "daily": "1d", "1w": "1d", "w": "1d"}
    return known.get(t, "1d")


@app.route("/research")
def research():
    """Fundamentals, valuation, filings, options and a two-sided thesis."""
    symbol, exact = _symbol_from(request.args)
    interval = request.args.get("interval") or "1d"
    if not symbol:
        return redirect(url_for("index"))

    if g.demo:
        # The example can open a fixed set of instruments on daily bars, each
        # computed once and shared. Anything else would let a crawler make the
        # server analyse every ticker it can think of.
        if symbol.upper() not in demo_mod.SYMBOLS:
            return _locked("Create an account to look up %s" % symbol.upper()[:12],
                           "Without an account you can look around the example. "
                           "With one, you can ask about any stock or coin.")
        if interval != "1d":
            return _locked("Create an account to change the timeframe",
                           "The example shows daily bars. With an account you "
                           "can look at any timeframe.")


    if not _allowance_left(symbol, interval, exact):
        return _allowance_used()

    cfg = _cfg()
    with_news = request.args.get("news", "1") != "0"
    with_options = request.args.get("options", "1") != "0"
    try:
        analysis = cached_analysis(symbol, interval, cfg, with_news,
                                   record=not g.demo,
                                   ttl=DEMO_TTL if g.demo else None, exact=exact)
    except DataError as exc:
        return _no_prices(exc, symbol, 404)
    _spend_view(symbol, interval, exact)

    # Keyed like the analysis it is built from: the thesis and the reasoning
    # quote that person's own plan, so one person's stops and targets were
    # being shown to the next person who opened the same ticker.
    key = (analysis.bars.symbol.upper(), interval, "research", _settings_key(cfg),
           with_news, with_options)
    now = time.time()
    with _CACHE_LOCK:
        hit = _RESEARCH_CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        found = hit[1]
    else:
        found = engine.research(analysis.bars.symbol, analysis.bars.last_price,
                                cfg, analysis=analysis, with_options=with_options)
        with _CACHE_LOCK:
            _RESEARCH_CACHE[key] = (time.time(), found)
            if len(_RESEARCH_CACHE) > 20:
                oldest = min(_RESEARCH_CACHE, key=lambda k: _RESEARCH_CACHE[k][0])
                _RESEARCH_CACHE.pop(oldest, None)

    # Drawn from the bars and filings already in hand, so no extra request.
    # The year's prices are drawn twice, wide and narrow, as the analysis
    # page's chart is: shrunk to a phone, the wide drawing's labels were 4
    # pixels tall. The narrow one is drawn for a phone's width.
    year = dict(closes=[float(v) for v in analysis.bars.close[-260:]],
                labels=("about a year ago", "now"),
                title="%s price history" % analysis.bars.symbol)
    price_svg = charts_mod.price_chart(**year)
    price_svg_narrow = charts_mod.price_chart(width=300, height=190, **year)
    filed_charts = charts_mod.charts_for(found.sec_history or {})

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        saved = db_mod.thesis_list(conn, analysis.bars.symbol, limit=6, user=uid())
        # Watched as this instrument: an entry saved as a bare LINK is the
        # coin, and does not make Interlink Electronics' page say watched.
        listing = exact_arg(analysis.bars.symbol) is not None
        watched = any(r["symbol"] == analysis.bars.symbol and bool(r["exact"]) == listing
                      for r in db_mod.watchlist(conn, user=uid()))
    finally:
        conn.close()

    reviews = []
    for row in saved:
        now_price, split = _thesis_price(row, analysis.bars)
        look = (thesis_mod.review({"price": row["price"], "balance": row["balance"],
                                   "created": row["created_ts"]}, now_price)
                if now_price else
                {"usable": False, "reason": "Could not price %s today." % row["symbol"]})
        reviews.append((row, dict(
            look, now=now_price,
            quoted=analysis.bars.last_price if split != 1.0 else None,
            split_words=market_mod.split_words(split) if split != 1.0 else "")))

    return render_template("research.html", r=found, a=analysis, cfg=cfg,
                           saved=reviews, watched=watched,
                           price_svg=price_svg, price_svg_narrow=price_svg_narrow,
                           filed_charts=filed_charts,
                           plain=plain_mod.explain_company(found),
                           canonical_url=_canonical("research", analysis.bars))


# Each save runs the research pipeline again: fundamentals, filings and risk.
THESES_PER_HOUR = 30
# A note is a reminder to yourself, not an essay, and it is stored for good.
NOTE_LIMIT = 1000


def _thesis_price(row, bars):
    """Today's price for a saved thesis, in the shares of the day it was saved,
    and the split between: (price, factor).

    After a ten-for-one the quote is in today's shares, and a thesis saved at
    1,000 read 101 as a 90% fall and proposed "wrong".
    """
    if bars is None:
        return None, 1.0
    try:
        split = market_mod.split_factor_since(row["symbol"],
                                              int(row["created_ts"] or 0), bars)
    except Exception:
        # A split cannot be ruled out just now: no price, rather than one
        # that may be ten times off.
        return None, 1.0
    return float(bars.last_price) * split, split


@app.route("/thesis/save", methods=["POST"])
def save_thesis():
    symbol, exact = _symbol_from(request.form)
    symbol = symbol.upper()[:20]
    interval = request.form.get("interval") or "1d"
    if not symbol or interval not in dict(INTERVALS):
        return render_template("error.html", symbol="",
                               message="Open the stock's research page and save "
                                       "the thesis from there."), 400
    # Saving analyses the instrument, so it costs a free look as opening it
    # does. Without this a free account could research anything from here.
    if not _allowance_left(symbol, interval, exact):
        return _allowance_used()
    if _rate_limited("thesis", THESES_PER_HOUR):
        return _too_often("saved theses")
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        cfg = _cfg()
        analysis = cached_analysis(symbol, interval, cfg, False, exact=exact)
        found = engine.research(symbol, analysis.bars.last_price, cfg,
                                analysis=analysis, with_options=False,
                                with_reasoning=False)
        t = found.thesis
        db_mod.thesis_save(
            conn, symbol, analysis.bars.last_price, t.balance, t.verdict,
            [{"headline": p.headline, "detail": p.detail,
              "evidence": [{"claim": e.claim, "value": e.value,
                            "source": e.source, "url": e.url} for e in p.evidence]}
             for p in t.bull.points],
            [{"headline": p.headline, "detail": p.detail,
              "evidence": [{"claim": e.claim, "value": e.value,
                            "source": e.source, "url": e.url} for e in p.evidence]}
             for p in t.bear.points],
            t.falsifiers, t.all_sources,
            (request.form.get("note") or "")[:NOTE_LIMIT], user=uid())
    except DataError as exc:
        return _no_prices(exc, symbol, 400)
    except Exception:
        # The text of an unexpected error can carry file paths or SQL, which
        # are for the log, not the page.
        app.logger.exception("Saving a thesis for %s failed", symbol)
        return render_template("error.html", symbol=symbol,
                               message="That thesis could not be saved just now. "
                                       "Try again in a minute."), 500
    finally:
        conn.close()
    _spend_view(symbol, interval, exact)
    return redirect(url_for("research", symbol=symbol, interval=interval,
                            exact=1 if exact else None))


@app.route("/thesis/delete", methods=["POST"])
def delete_thesis():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        db_mod.thesis_delete(conn, int(request.form["thesis_id"]), user=uid())
    except (KeyError, ValueError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(_safe_back(request.form.get("back"), url_for("monitor")))


@app.route("/thesis/close", methods=["POST"])
def close_thesis():
    """Record how a thesis actually turned out, at today's price."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        thesis_id = int(request.form["thesis_id"])
        row = db_mod.thesis_get(conn, thesis_id, user=uid())
        if row is None:
            return render_template("error.html", symbol="",
                                   message="No thesis with id %d." % thesis_id), 400

        outcome = request.form.get("outcome") or ""
        if outcome not in thesis_mod.OUTCOMES:
            return render_template("error.html", symbol=row["symbol"],
                                   message="%r is not an outcome this records."
                                           % outcome), 400

        # Price it at close, not at whatever the user remembers. A thesis is
        # judged against the market, so the market supplies the number.
        try:
            bars = market_mod.fetch_bars(row["symbol"], "1d")
        except DataError as exc:
            return _no_prices(exc, row["symbol"], 502,
                              "Could not price %s to close the thesis: %s"
                              % (row["symbol"], exc))
        # In the shares it was saved in, like the price it is set against.
        price = _thesis_price(row, bars)[0]
        if price is None:
            return render_template(
                "error.html", symbol=row["symbol"],
                message="Could not check %s for a share split just now, so it "
                        "cannot be priced to close the thesis. Try again in a "
                        "minute." % row["symbol"]), 503

        db_mod.thesis_close(conn, thesis_id, float(price), outcome, user=uid())
    except (KeyError, ValueError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(_safe_back(request.form.get("back"), url_for("monitor")))


@app.route("/thesis/reopen", methods=["POST"])
def reopen_thesis():
    """Undo a closing, for when it was recorded by mistake."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        thesis_id = int(request.form["thesis_id"])
        if db_mod.thesis_get(conn, thesis_id, user=uid()) is None:
            return render_template("error.html", symbol="",
                                   message="No thesis with id %d." % thesis_id), 400
        db_mod.thesis_reopen(conn, thesis_id, user=uid())
    except (KeyError, ValueError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(_safe_back(request.form.get("back"), url_for("monitor")))


# What the sign-in page can be asked to say, by name. Words taken from the
# address itself would let any link put a sentence of its choosing on the
# site, in the site's own notice box.
SIGNIN_SAID = {"out": "You are signed out.",
               "deleted": "That account and everything in it has been deleted."}

# Said on a public site when signing in with Google is broken by how the site
# is set up, which a visitor cannot fix. The detail goes to the log.
SIGN_IN_BROKEN = ("Signing in with Google is not working on this site just "
                  "now. Try again later.")


def _sign_in_trouble(exc) -> str:
    """A sign-in error, in words for whoever is signing in.

    A fault in how this copy is set up with Google is said in full on a copy
    run on its own machine, where whoever reads it is whoever runs it. On a
    public site the visitor is told plainly, and the detail goes to the log.
    """
    if getattr(exc, "setup", False) and _served_publicly():
        app.logger.warning("Google sign-in is not working: %s", exc)
        return SIGN_IN_BROKEN
    return str(exc)


@app.route("/signin")
def signin():
    """The page you land on when you are not signed in."""
    if getattr(g, "user", None) is not None:
        return redirect(url_for("index"))
    # How to set sign-in up is for whoever runs this copy on their own
    # machine. A visitor to a public site cannot set anything.
    setup = not _served_publicly()
    return render_template("signin.html",
                           reason=SIGNIN_SAID.get(request.args.get("said", ""), ""),
                           setup=setup, why_not=auth_mod.why_not() if setup else "",
                           next_url=_safe_back(request.args.get("next"), ""))


@app.route("/auth/google")
def google_start():
    """Begin the Google sign-in flow."""
    if not auth_mod.configured():
        setup = not _served_publicly()
        why = auth_mod.why_not() if setup else ""
        return render_template("signin.html", reason=why, setup=setup,
                               why_not=why, next_url=""), 503

    challenge, refused = _app_challenge()
    if refused:
        return _to_app(error="failed")

    state = auth_mod.new_state()
    verifier = auth_mod.new_verifier()
    # Held in the signed cookie, so the callback can prove it belongs to the
    # browser that started this and not to someone else's forged link.
    session["oauth_state"] = state
    session["oauth_verifier"] = verifier
    session["oauth_next"] = _safe_back(request.args.get("next"), "")
    # The app's check, when the iPhone app started this in its browser sheet.
    if challenge:
        session["oauth_app"] = challenge
    else:
        session.pop("oauth_app", None)

    try:
        url = auth_mod.authorization_url(_redirect_uri(), state, verifier)
    except auth_mod.AuthError as exc:
        return render_template("signin.html", reason=_sign_in_trouble(exc),
                               why_not="", next_url=""), 503
    return redirect(url)


@app.route("/auth/google/callback")
def google_callback():
    """Where Google sends the browser back."""
    expected = session.pop("oauth_state", "")
    verifier = session.pop("oauth_verifier", "")
    next_url = session.pop("oauth_next", "")
    # Set when the iPhone app started this: then the result goes to the app.
    challenge = session.pop("oauth_app", "")

    if request.args.get("error"):
        # Google's own code, such as access_denied, is not repeated: it means
        # nothing to the person, and the address can carry any text at all.
        return _sign_in_failed(
            challenge,
            ("You cancelled the sign-in at Google."
             if request.args.get("error") == "access_denied" else
             "Google did not complete the sign-in. Start again from "
             "this page."),
            "cancelled" if request.args.get("error") == "access_denied"
            else "failed")

    state = request.args.get("state", "")
    # Constant time, because this is a secret being compared.
    if not expected or not hmac.compare_digest(str(expected), str(state)):
        return _sign_in_failed(
            challenge, "That sign-in link did not match this browser session. "
                       "Start again from this page.", "expired")

    code = request.args.get("code", "")
    if not code:
        return _sign_in_failed(challenge, "Google returned no sign-in code.")

    try:
        profile = auth_mod.sign_in(code, _redirect_uri(), verifier)
    except auth_mod.AuthError as exc:
        return _sign_in_failed(challenge, _sign_in_trouble(exc))

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if _kept_for_review(conn, profile.get("email"), "Google sign-in"):
            return _sign_in_failed(challenge, REVIEW_KEPT % "Google", "refused")
        user = events_mod.new_account(conn, accounts_mod.upsert_google_user, profile)
    except ValueError as exc:
        return _sign_in_failed(challenge, str(exc), "refused")
    finally:
        conn.close()
    if challenge:
        return _hand_to_app(user, challenge, next_url)
    return _finish_sign_in(user, next_url)


# The Apple sign-in's own state, kept apart from the session. Apple posts the
# browser back from appleid.apple.com, a cross-site POST that the session
# cookie (SameSite=Lax) does not travel with, so the state, the nonce and
# where to go next ride in a cookie of their own: signed, ten minutes long,
# sent only to /auth/apple and only over https.
APPLE_FLOW = "apple_flow"
APPLE_FLOW_SECONDS = 10 * 60
# Every one of these is a call to Apple, and anyone can start a sign-in, so
# they are limited per network (_rate_limited allows a network three times
# this an hour).
APPLE_SIGNINS_PER_HOUR = 20
# Apple's JSON is a few kilobytes. The site's own limit is set for screenshot
# uploads, and these two answer anyone, signed in or not.
APPLE_JSON_LIMIT = 64 * 1024


def _flow_key() -> bytes:
    key = app.secret_key
    return key if isinstance(key, bytes) else str(key).encode("utf-8")


def _apple_flow() -> Dict:
    """This browser's Apple sign-in in progress: {} when there is none, when
    it has run out, or when the cookie is not one this server sealed."""
    return apple_mod.open_flow(request.cookies.get(APPLE_FLOW, ""), _flow_key(),
                               APPLE_FLOW_SECONDS)


def _apple_done(response):
    """A finished Apple sign-in, either way: its cookie is spent."""
    response.delete_cookie(APPLE_FLOW, path=url_for("apple_start"), secure=True,
                           httponly=True, samesite="None")
    return response


def _apple_redirect_uri() -> str:
    """Where Apple posts back. Registered with Apple, so it must match exactly."""
    return _public_base() + url_for("apple_callback")


@app.route("/auth/apple")
def apple_start():
    """Begin signing in with Apple, on the website or in the app's browser sheet."""
    if not apple_mod.web_ready():
        abort(404)
    challenge, refused = _app_challenge()
    if refused:
        return _to_app(error="failed")
    state, nonce = apple_mod.new_state(), apple_mod.new_nonce()
    flow = {"state": state, "nonce": apple_mod.nonce_hash(nonce),
            "next": _safe_back(request.args.get("next"), ""), "app": challenge,
            # A friend's invite this visitor arrived with: it lives in the
            # session cookie, which Apple's post back will not carry.
            "ref": session.get("ref", "")}
    response = redirect(apple_mod.authorization_url(_apple_redirect_uri(), state,
                                                    nonce))
    response.set_cookie(APPLE_FLOW, apple_mod.seal_flow(flow, _flow_key()),
                        max_age=APPLE_FLOW_SECONDS, path=url_for("apple_start"),
                        secure=True, httponly=True, samesite="None")
    # One visitor's state: nothing between here and them may keep a copy.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/auth/apple/callback", methods=["POST"])
def apple_callback():
    """Where Apple posts the browser back, from its own site."""
    if not apple_mod.web_ready():
        abort(404)
    flow = _apple_flow()
    challenge = flow.get("app", "")
    state = request.form.get("state", "")
    # Constant time, because this is a secret being compared.
    if not flow or not hmac.compare_digest(
            str(flow.get("state", "")).encode("utf-8"), state.encode("utf-8")):
        return _apple_done(_sign_in_failed(
            challenge, "That sign-in did not match this browser. Start again "
                       "from this page.", "expired"))
    error = request.form.get("error", "")
    if error:
        cancelled = error == "user_cancelled_authorize"
        return _apple_done(_sign_in_failed(
            challenge, "Signing in with Apple was cancelled." if cancelled
            else "Apple did not complete the sign-in.",
            "cancelled" if cancelled else "failed"))
    if _rate_limited("apple_signin", APPLE_SIGNINS_PER_HOUR):
        return _apple_done(_sign_in_failed(
            challenge, "Too many sign-ins were tried from this connection in "
                       "the last hour. Wait a while, then try again.",
            "failed", 429))

    client_id = apple_mod.services_id()
    try:
        tokens = apple_mod.redeem(request.form.get("code", ""), client_id,
                                  _apple_redirect_uri())
        # Checked by hand although it came straight from Apple over TLS: the
        # nonce in it is what ties it to the sign-in this browser started.
        identity = apple_mod.verify_identity(tokens.get("id_token"), client_id,
                                             flow.get("nonce", ""))
    except apple_mod.AppleError as exc:
        return _apple_done(_sign_in_failed(challenge, str(exc)))
    refresh_token = tokens.get("refresh_token")
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if _kept_for_review(conn, identity.get("email"), "Sign in with Apple"):
            return _apple_done(_sign_in_failed(challenge, REVIEW_KEPT % "Apple",
                                               "refused"))
        user = events_mod.new_account(
            conn, accounts_mod.upsert_apple_user, identity,
            apple_mod.name_from_post(request.form.get("user", "")),
            refresh_token if isinstance(refresh_token, str) else "", client_id)
    except ValueError as exc:
        return _apple_done(_sign_in_failed(challenge, str(exc), "refused"))
    finally:
        conn.close()
    if challenge:
        return _apple_done(_hand_to_app(user, challenge, flow.get("next", "")))
    if flow.get("ref"):
        session["ref"] = flow["ref"]
    return _apple_done(_finish_sign_in(user, flow.get("next", "")))


@app.route("/auth/apple/nonce")
def apple_nonce():
    """A fresh nonce for the app's own Apple sheet, remembered in this session.

    Apple signs it into the identity token, so a token is good only for the
    session that asked for it, and only once.
    """
    if not apple_mod.native_ready():
        abort(404)
    nonce = apple_mod.new_nonce()
    session["apple_nonce"] = {"hash": apple_mod.nonce_hash(nonce),
                              "at": int(time.time())}
    response = jsonify({"nonce": nonce})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/auth/apple/native", methods=["POST"])
def apple_native():
    """Sign in with what the app's own Apple sheet returned.

    JSON, from a page on this site only. It cannot ask for a signed-in
    session the way other JSON posts do, being what starts one; it asks
    instead for the nonce this session was given, which Apple has signed into
    the token.
    """
    if not apple_mod.native_ready():
        abort(404)
    if not _from_this_site():
        return jsonify({"ok": False, "error": "Start signing in from Orenth "
                                              "itself."}), 403
    request.max_content_length = APPLE_JSON_LIMIT
    # Named so that an error report strips it: it holds the identity token.
    # JSON nested too deep to read is None here too (_RequestJSON).
    token_post = request.get_json(silent=True)
    if not isinstance(token_post, dict):
        return jsonify({"ok": False, "error": "Nothing to sign in with."}), 400
    if _rate_limited("apple_native", APPLE_SIGNINS_PER_HOUR):
        return jsonify({"ok": False,
                        "error": "Too many sign-ins were tried from this "
                                 "connection in the last hour. Wait a while, "
                                 "then try again."}), 429
    issued = session.pop("apple_nonce", None)
    issued = issued if isinstance(issued, dict) else {}
    issued_hash = issued.get("hash") or ""
    if not issued_hash or \
            time.time() - issued.get("at", 0) > apple_mod.NONCE_SECONDS:
        return jsonify({"ok": False, "error": "That sign-in took too long. "
                                              "Try again."}), 400
    client_id = apple_mod.bundle_id()
    try:
        identity = apple_mod.verify_identity(token_post.get("identityToken"),
                                             client_id, issued_hash)
    except apple_mod.AppleError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # The session cookie holding the nonce could be sent again; this cannot.
    if not apple_mod.spend_nonce(issued_hash):
        return jsonify({"ok": False, "error": "That sign-in was already used. "
                                              "Try again."}), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        kept = _kept_for_review(conn, identity.get("email"), "Sign in with Apple")
    finally:
        conn.close()
    if kept:
        return jsonify({"ok": False, "error": REVIEW_KEPT % "Apple"}), 400
    refresh_token = _apple_refresh_token(token_post.get("authorizationCode"),
                                         identity["sub"], client_id)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        user = events_mod.new_account(
            conn, accounts_mod.upsert_apple_user, identity,
            apple_mod.name_from(token_post.get("givenName"),
                                token_post.get("familyName")),
            refresh_token, client_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()
    _open_session(user)
    next_url = token_post.get("next")
    next_url = next_url if isinstance(next_url, str) else ""
    return jsonify({"ok": True, "next": _safe_back(next_url, url_for("index"))})


def _apple_refresh_token(code, sub: str, client_id: str) -> str:
    """The refresh token for a sign-in in the app, kept so deleting the
    account can end its Apple side.

    Trading the app's one-time code is the only way to get one. A failure
    does not stop a sign-in already proved by the identity token: the next
    sign-in tries again, and the log says what is missing until then.
    """
    if not code:
        return ""
    try:
        tokens = apple_mod.redeem(code, client_id)
        again = apple_mod.verify_identity(tokens.get("id_token"), client_id,
                                          check_nonce=False)
    except apple_mod.AppleError:
        app.logger.warning("An Apple sign-in in the app gave no refresh token, so "
                           "until the next one, deleting that account cannot "
                           "end its Apple sign-in")
        return ""
    refresh_token = tokens.get("refresh_token")
    # Only the same person's: a token kept against the wrong account would
    # end somebody else's sign-in when this one is deleted.
    if again["sub"] != sub or not isinstance(refresh_token, str):
        return ""
    return refresh_token


@app.route("/auth/apple/notifications", methods=["POST"])
def apple_notifications():
    """Apple saying someone stopped using Sign in with Apple here, or deleted
    their Apple Account.

    Server to server: no cookie and no Origin to check. What makes it
    believable is Apple's signature, checked as a sign-in's is. Taking consent
    back unlinks Apple and signs the account out. A deleted Apple Account
    unlinks it too, and deletes the whole account only when nothing else can
    ever open it again: never one that Google or a code can still reach.
    """
    if not apple_mod.native_ready():
        abort(404)
    request.max_content_length = APPLE_JSON_LIMIT
    body = request.get_json(silent=True, force=True)
    try:
        event = apple_mod.read_notification(
            body.get("payload") if isinstance(body, dict) else None)
    except apple_mod.AppleError:
        return jsonify({"error": "not a notification signed by Apple"}), 400
    deleted = event["type"] in ("account-delete", "account-deleted")
    if event["type"] != "consent-revoked" and not deleted:
        # Email forwarding switched on or off: nothing kept here depends on it.
        return jsonify({"received": True})
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        owner = accounts_mod.identity_owner(conn, "apple", event["sub"])
        if owner is None and deleted:
            # Unlinked already, when its consent was taken back. The account
            # it made still names it, and goes too if nothing else opens it.
            owner = accounts_mod.made_by(conn, "apple", event["sub"])
        # Unknown here, or signed in again since it happened: nothing to undo.
        if owner is None or owner["last_used_ts"] > event["at"]:
            return jsonify({"received": True})
        user_id = int(owner["user_id"])
        if not deleted or accounts_mod.can_still_sign_in(conn, user_id, "apple",
                                                         event["sub"]):
            # Unlinking signs the account out everywhere. As /signout does
            # for one sign-in, the devices those sign-ins switched on stop
            # being sent its alerts, so a shared iPad stops showing them.
            accounts_mod.unlink_identity(
                conn, "apple", event["sub"],
                ending=lambda token: push_mod.forget_session(conn,
                                                             _session_ref(token)))
            return jsonify({"received": True})
        # Their Apple Account is gone and nothing else opens this one. The
        # subscription stops first, as when they delete it themselves.
        customer = billing_mod.customer_of(conn, user_id)
        if customer and billing_mod.ready():
            billing_mod.close_customer(customer)
        accounts_mod.delete_user(conn, user_id)
        app.logger.info("Deleted account %d: its Apple Account was deleted and "
                        "nothing else could open it", user_id)
    except billing_mod.BillingError:
        # Kept whole, so Apple's next try finds it as it was.
        app.logger.error("Could not cancel the subscription of an account whose "
                         "Apple Account was deleted, so it was kept for now")
        return jsonify({"error": "try again later"}), 503
    finally:
        conn.close()
    return jsonify({"received": True})


# The app's own address, where the browser sheet hands a sign-in back.
APP_FINISH = "app.orenth.ios://auth/finish"
# Thirty an hour from one network (_rate_limited allows a network three times
# this): a household signing in, never someone guessing.
APP_FINISHES_PER_HOUR = 10
HANDOFF_FAILED = "That sign-in took too long or was already used. Try again."


def _app_challenge() -> Tuple[str, bool]:
    """The app's check when the iPhone app started this sign-in, and whether
    the request has to be refused.

    The app opens the start address in the system browser sheet with app=1
    and the SHA-256 of a secret it keeps, base64url, 43 characters. A link on
    another site cannot start one: the browser marks it cross-site, and it
    becomes an ordinary sign-in in that browser, which hands nothing to
    anyone. Another app on the phone can start one, in a sheet of its own,
    and nothing here tells it from Orenth's app: see accounts.py.
    """
    if request.args.get("app") != "1":
        return "", False
    if request.headers.get("Sec-Fetch-Site", "") in ("cross-site", "same-site"):
        return "", False
    challenge = request.args.get("challenge", "")
    if not accounts_mod.valid_challenge(challenge):
        return "", True
    return challenge, False


def _to_app(**params):
    """Send the browser sheet back to the app, with a code or a short reason."""
    from urllib.parse import urlencode
    response = redirect(APP_FINISH + "?" + urlencode(params))
    # The address carries a one-time code: nothing should keep a copy.
    response.headers["Cache-Control"] = "no-store"
    return response


def _hand_to_app(user, challenge: str, next_url: str):
    """Give a finished sign-in to the app, not to this browser."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        handoff_token = accounts_mod.new_handoff(conn, user.id, challenge,
                                                 _safe_back(next_url, ""))
    finally:
        conn.close()
    return _to_app(code=handoff_token)


def _sign_in_failed(challenge: str, message: str, reason: str = "failed",
                    status: int = 400):
    """A sign-in that did not finish. Said on the sign-in page or, when the
    app started it, to the app in a word, so nobody is left on an error page
    inside the browser sheet."""
    if challenge:
        return _to_app(error=reason)
    return app.make_response((render_template(
        "signin.html", reason=message, why_not="", next_url=""), status))


def _from_this_site() -> bool:
    """Whether the browser says this POST came from a page on this site.

    The rest of the site leans on the session cookie being SameSite=Lax. A
    sign-in cannot, being what starts the session: a form on another site
    could otherwise sign its visitor in to an account of its choosing.
    Browsers set these headers themselves: Sec-Fetch-Site, then Origin, then,
    for an older browser sending neither, the Referer. One that sends none of
    them is let through, since then there is nothing to go on.
    """
    from urllib.parse import urlparse
    fetched = request.headers.get("Sec-Fetch-Site", "")
    if fetched and fetched != "same-origin":
        return False

    def origin_of(url: str) -> str:
        parts = urlparse(url)
        return ("%s://%s" % (parts.scheme, parts.netloc)).lower()

    allowed = {origin_of(request.host_url)}
    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()
    if public:
        allowed.add(origin_of(public))
    origin = request.headers.get("Origin", "")
    if origin:
        return origin.strip().lower() in allowed
    referrer = request.headers.get("Referer", "")
    if referrer:
        return origin_of(referrer) in allowed
    return True


@app.route("/auth/app/finish", methods=["POST"])
def app_finish():
    """The app's web view collecting a sign-in finished in the browser sheet.

    It posts the one-time code the sheet was handed and the verifier only it
    kept. Every failure says the same plain thing.
    """
    if not (auth_mod.configured() or apple_mod.web_ready()):
        abort(404)
    if not _from_this_site():
        return _sign_in_failed("", HANDOFF_FAILED, status=403)
    if _rate_limited("app_finish", APP_FINISHES_PER_HOUR):
        return _sign_in_failed("", "Too many sign-ins were tried from this "
                                   "connection in the last hour. Wait a while, "
                                   "then try again.", status=429)
    # Named so that an error report strips them, as it does any token.
    handoff_token = request.form.get("code", "")
    verifier_secret = request.form.get("verifier", "")
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        found = accounts_mod.redeem_handoff(conn, handoff_token, verifier_secret)
        user = accounts_mod.get_user(conn, found[0]) if found else None
    finally:
        conn.close()
    if user is None:
        return _sign_in_failed("", HANDOFF_FAILED)
    return _finish_sign_in(user, found[1] or request.form.get("next", ""))


def _finish_sign_in(user, next_url: str):
    """Start a session for someone who just proved who they are."""
    _open_session(user)
    return redirect(_safe_back(next_url, url_for("index")))


def _open_session(user) -> None:
    """The session itself, for a sign-in that answers with JSON, not a page."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        accounts_mod.purge_expired(conn)
        token = accounts_mod.start_session(
            conn, user.id, request.headers.get("User-Agent", ""))
        # A friend's link they arrived through, credited once the account
        # exists. accept() ignores it for anyone who was not brand new.
        ref = session.pop("ref", "")
        if ref:
            invites_mod.accept(conn, user.id, ref)
    finally:
        conn.close()
    session.permanent = True
    session[SESSION_KEY] = token


def _client_ip() -> str:
    """Who is asking, for the sending limits.

    Behind Cloudflare the connection comes from Cloudflare, shared by
    everyone, so its own header naming the visitor is preferred. It can be
    forged by going round Cloudflare to the host directly; that only weakens
    the per-network limit, and the per-address limit still holds.
    """
    return (request.headers.get("CF-Connecting-IP")
            or request.remote_addr or "")[:64]


_CHANNELS = {
    "email": {"ready": codes_mod.email_ready, "label": "email address",
              "clean": codes_mod.clean_email},
    "phone": {"ready": codes_mod.phone_ready, "label": "phone number",
              "clean": codes_mod.clean_phone},
}


@app.route("/signin/<channel>", methods=["GET", "POST"])
def code_start(channel):
    """Ask for an address, and send it a code."""
    spec = _CHANNELS.get(channel)
    if spec is None or not spec["ready"]():
        abort(404)
    next_url = _safe_back(request.values.get("next"), "")
    error = None
    value = ""
    if request.method == "POST":
        value = (request.form.get("to") or "").strip()[:200]
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            target = spec["clean"](value)
            if channel == "email" and _is_review_address(target):
                # App Review types its sign-in details where an app asks for
                # an email, and its account signs in only with its fixed
                # code. So that address is sent no email: the review page
                # opens instead, with the address already in it. The address
                # opens nothing by itself; the code is the secret.
                session["review_email"] = target
                return redirect(url_for("review_signin"))
            if channel == "email":
                codes_mod.send_email_code(conn, target, _client_ip(),
                                          app.secret_key)
            else:
                codes_mod.send_phone_code(conn, target, _client_ip())
            session["code_login"] = {"channel": channel, "to": target,
                                     "next": next_url}
            return redirect(url_for("code_verify", channel=channel))
        except codes_mod.CodeError as exc:
            error = str(exc)
        finally:
            conn.close()
    return render_template("code_start.html", channel=channel, spec=spec,
                           error=error, value=value, next_url=next_url)


@app.route("/signin/<channel>/code", methods=["GET", "POST"])
def code_verify(channel):
    """Take the code back, and sign the person in."""
    if channel not in _CHANNELS:
        abort(404)
    pending = session.get("code_login") or {}
    if pending.get("channel") != channel or not pending.get("to"):
        return redirect(url_for("code_start", channel=channel))
    error = None
    if request.method == "POST":
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            if channel == "email":
                codes_mod.check_email_code(conn, pending["to"],
                                           request.form.get("code", ""),
                                           app.secret_key)
                user = events_mod.new_account(conn, accounts_mod.upsert_email_user,
                                              pending["to"])
            else:
                codes_mod.check_phone_code(conn, pending["to"],
                                           request.form.get("code", ""))
                user = events_mod.new_account(conn, accounts_mod.upsert_phone_user,
                                              pending["to"])
        except (codes_mod.CodeError, ValueError) as exc:
            error = str(exc)
        else:
            session.pop("code_login", None)
            return _finish_sign_in(user, pending.get("next", ""))
        finally:
            conn.close()
    return render_template("code_verify.html", channel=channel,
                           to=pending["to"], error=error)


# Apple's reviewers must be able to sign in to the iPhone app, and they cannot
# receive an email or a text, so a code sign-in alone fails review. Two
# settings let one named account in with a fixed code instead:
# REVIEW_LOGIN_EMAIL and REVIEW_LOGIN_CODE. With either missing, or a code
# shorter than REVIEW_CODE_MIN, this door does not exist. Nothing links to it;
# the address and the code go only in App Store Connect's review notes.
REVIEW_CODE_MIN = 16
# Tries an hour, counted per network at three times this (see _rate_limited).
REVIEW_TRIES_PER_HOUR = 5
REVIEW_ERROR = "That email and code do not match. Check both, then try again."
REVIEW_TOO_OFTEN = "Too many tries from this network. Wait an hour, then try again."


def _review_login() -> Optional[Tuple[str, str]]:
    """The review account's address and code, or None when switched off."""
    email = (os.environ.get("REVIEW_LOGIN_EMAIL") or "").strip().lower()
    code = (os.environ.get("REVIEW_LOGIN_CODE") or "").strip()
    if not email or len(code) < REVIEW_CODE_MIN:
        return None
    return email, code


def _is_review_address(email: str) -> bool:
    """Whether an address is the review account's, while its sign-in is on."""
    login = _review_login()
    return login is not None and (email or "").strip().lower() == login[0]


# How an account the fixed code may not open was made, for the log.
_MADE_WITH = {"google": "Google sign-in", "apple": "Sign in with Apple",
              "phone": "a text-message code", "demo": "the example account"}


def _review_refusal(conn, email: str) -> str:
    """Why the fixed code may not open the account at this address, or ''.

    Never the local account, the example account, an operator, or an account
    someone signs in to another way: a fixed code must open nothing but the
    one account kept for App Review, and an account Google took over may be
    a real person's. The words are for the server's log, where the owner
    looks when App Review says the code does not work.
    """
    operators = {e.strip().lower()
                 for e in (os.environ.get("STOCKBOT_ADMIN_EMAIL") or "").split(",")
                 if e.strip()}
    if email in operators:
        return "REVIEW_LOGIN_EMAIL is an operator's address (STOCKBOT_ADMIN_EMAIL)"
    row = conn.execute("SELECT provider, is_local FROM users WHERE email=?",
                       (email,)).fetchone()
    if row is None or (row["provider"] == "email" and not row["is_local"]):
        return ""
    if row["is_local"]:
        return "REVIEW_LOGIN_EMAIL is the local account's address"
    return ("the account at REVIEW_LOGIN_EMAIL was made or joined with %s, and "
            "the review code opens only an account made with an email code. "
            "Set a new address that nothing else signs in to (mobile/APP_STORE.md, "
            "Part 4)" % _MADE_WITH.get(row["provider"], row["provider"]))


def _review_account(conn, email: str):
    """The review account, made on first use with the email provider, or
    None when _review_refusal says the code may not open it."""
    if _review_refusal(conn, email):
        return None
    try:
        return accounts_mod.upsert_email_user(conn, email)
    except ValueError:
        return None


def _kept_for_review(conn, email, way: str) -> bool:
    """Whether a sign-in another way is reaching for the review account.

    That account must stay one only the review code opens: joined to Google,
    the code would no longer open it, and App Review would be locked out.
    So while the review sign-in is on, an address it would open is refused
    to Google and to Apple, and the log says why. An account at that address
    made another way before is not the review account, and stays its
    owner's to sign in to.
    """
    if not _is_review_address(email if isinstance(email, str) else ""):
        return False
    if _review_refusal(conn, _review_login()[0]):
        return False
    app.logger.warning("Refused %s to the App Review account (REVIEW_LOGIN_EMAIL): "
                       "only the review sign-in may open it. Sign in to Orenth "
                       "with another account to test %s.", way, way)
    return True


# Said to whoever tries; only the owner of that address can reach this.
REVIEW_KEPT = ("That email address is kept for Apple's App Review, which signs "
               "in to it with its own code, so it cannot be used with %s here. "
               "Use a different account.")


def review_status() -> str:
    """The review sign-in as the start-up log says it, warning when its
    code cannot open its account."""
    email = (os.environ.get("REVIEW_LOGIN_EMAIL") or "").strip().lower()
    code = (os.environ.get("REVIEW_LOGIN_CODE") or "").strip()
    if not email and not code:
        return "off"
    if not email or len(code) < REVIEW_CODE_MIN:
        return ("off: it needs REVIEW_LOGIN_EMAIL and a REVIEW_LOGIN_CODE of at "
                "least %d characters" % REVIEW_CODE_MIN)
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            why = _review_refusal(conn, email)
        finally:
            conn.close()
    except sqlite3.Error:
        return "on"
    if why:
        app.logger.warning("The App Review sign-in cannot open its account: %s.", why)
        return "on, but its code cannot open its account: %s" % why
    return "on"


@app.route("/signin/review", methods=["GET", "POST"])
def review_signin():
    """The App Review account's way in: one address, one fixed code."""
    login = _review_login()
    if login is None:
        abort(404)
    if getattr(g, "user", None) is not None:
        return redirect(url_for("index"))
    error = ""
    # Typed already on "Continue with email", which sends this address here.
    said = session.pop("review_email", "")
    email = said[:200] if isinstance(said, str) else ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()[:200]
        if _rate_limited("review_signin", REVIEW_TRIES_PER_HOUR):
            return render_template("review_signin.html", error=REVIEW_TOO_OFTEN,
                                   email=email), 429
        want_email, want_code = login
        code = (request.form.get("code") or "").strip()[:200]
        # Both compared every time, in constant time, so neither the answer
        # nor how long it took says which of the two was wrong.
        email_ok = hmac.compare_digest(email.lower().encode("utf-8"),
                                       want_email.encode("utf-8"))
        code_ok = hmac.compare_digest(code.encode("utf-8"),
                                      want_code.encode("utf-8"))
        user = None
        if email_ok and code_ok:
            conn = db_mod.connect(app.config.get("DB_PATH"))
            try:
                why = _review_refusal(conn, want_email)
                user = None if why else _review_account(conn, want_email)
            finally:
                conn.close()
            if why:
                # The person is told only that it did not match, like any
                # wrong try. The owner, reading the log after App Review
                # says the code does not work, is told the cause.
                app.logger.warning("App Review typed the right email and code, but "
                                   "they cannot open its account: %s.", why)
        if user is not None:
            # An invite link opened earlier in this browser would hand the
            # account free Pro, and the reviewer must see the account as Free.
            session.pop("ref", None)
            return _finish_sign_in(user, "")
        error = REVIEW_ERROR
    return render_template("review_signin.html", error=error,
                           email=email), (400 if error else 200)


# Reachable signed out, like the other ways in.
PUBLIC_ENDPOINTS.add("review_signin")


@app.route("/pricing")
def pricing():
    """Free and Pro, side by side. Only exists once billing is set up.

    Its notice box says why someone was sent here only when this app sends
    them (_upgrade), never from the address: a ?reason= in a link let anyone
    write in it.
    """
    if not billing_mod.ready():
        abort(404)
    return _pricing_page()


CODE_ERROR = ("That code does not work, or it has run out. Check the "
              "spelling, or empty the box to pay full price.")
# Codes are passed on by word of mouth, so they are words someone chose and
# can be guessed, and every check is a call to Stripe.
CODE_CHECKS_PER_HOUR = 10
CODE_TOO_OFTEN = ("Too many codes were tried in the last hour. Wait a while, "
                  "or empty the box to pay full price.")


@app.route("/billing/code", methods=["POST"])
def billing_code():
    """Check a typed discount code and say so, without leaving the page.

    Only answers; nothing is changed. The pricing page puts the answer under
    the box. Without JavaScript, Apply posts to checkout instead, which says
    the same thing by drawing the whole page again.
    """
    # Never inside the iPhone app, where nothing is sold through Stripe.
    if not billing_mod.ready() or g.demo or _in_app():
        abort(404)
    code = (request.form.get("code") or "").strip()[:40]
    if code and _rate_limited("code", CODE_CHECKS_PER_HOUR):
        return render_template("_codesaid.html", code_error=CODE_TOO_OFTEN), 429
    try:
        found = billing_mod.promotion(code)
    except billing_mod.BillingError as exc:
        return render_template("_codesaid.html", code_error=str(exc)), 502
    if code and not found:
        return render_template("_codesaid.html", code_error=CODE_ERROR), 400
    return render_template("_codesaid.html", applied=found)


@app.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    """Send someone to Stripe's own page to pay."""
    if not billing_mod.ready() or g.demo or _in_app():
        abort(404)
    if g.user.apple_until > time.time():
        # Already paying Apple for the same Pro: never a second bill.
        return _pricing_page(APPLE_PRO_SAID, 409)
    if _pro():
        return redirect(url_for("account"))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        customer = billing_mod.customer_of(conn, g.user.id)
        retrying = apple_store_mod.may_still_charge(conn, g.user.id)
    finally:
        conn.close()
    if retrying:
        # Apple's renewal failed, but it keeps trying and may yet charge: a
        # second bill all the same, only later.
        return _pricing_page(APPLE_RETRYING_SAID, 409)
    code = (request.form.get("code") or "").strip()[:40]
    if code and _rate_limited("code", CODE_CHECKS_PER_HOUR):
        return _pricing_page(code=code, code_error=CODE_TOO_OFTEN, status=429)
    try:
        found = billing_mod.promotion(code)
        if code and not found:
            return _pricing_page(code=code, code_error=CODE_ERROR, status=400)
        if request.form.get("apply"):
            return _pricing_page(code=code, applied=found)
        url = billing_mod.checkout_url(
            g.user, _public_base(), customer,
            price_id=billing_mod.price_id_for(request.form.get("plan", "monthly")),
            promotion=found["id"] if found else "")
    except billing_mod.BillingError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 502
    return redirect(url, code=303)


@app.route("/billing/done")
def billing_done():
    """Where Stripe sends a buyer back. Asks Stripe directly, not the browser."""
    upgraded = False
    session_id = (request.args.get("session_id") or "").strip()
    if billing_mod.ready() and session_id and not g.demo:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            with events_mod.counting_upgrades(conn):
                upgraded = billing_mod.confirm_checkout(conn, session_id, g.user.id)
        except billing_mod.BillingError:
            upgraded = False
        finally:
            conn.close()
    return render_template("billing_done.html", upgraded=upgraded)


@app.route("/billing/portal", methods=["POST"])
def billing_portal():
    """Stripe's own page for cancelling, changing card and receipts."""
    if not billing_mod.ready() or g.demo or _in_app():
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        customer = billing_mod.customer_of(conn, g.user.id)
    finally:
        conn.close()
    if not customer:
        return redirect(url_for("pricing"))
    try:
        url = billing_mod.portal_url(customer, _public_base() + url_for("account"))
    except billing_mod.BillingError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 502
    return redirect(url, code=303)


@app.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    """Stripe telling the server a plan changed. Signed, so it can be believed."""
    if not billing_mod.ready():
        abort(404)
    payload = request.get_data()
    if not billing_mod.verify(payload, request.headers.get("Stripe-Signature", ""),
                              os.environ["STRIPE_WEBHOOK_SECRET"]):
        return jsonify({"error": "bad signature"}), 400
    try:
        event = safejson.loads(payload.decode("utf-8"))
    except ValueError:
        return jsonify({"error": "not json"}), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        with events_mod.counting_upgrades(conn):
            outcome = billing_mod.handle(conn, event)
    finally:
        conn.close()
    return jsonify({"received": True, "outcome": outcome})


# --- Pro bought in the iPhone app, through Apple -------------------------------
#
# Inside the App Store app, Pro may only be sold through Apple (rule 3.1.1),
# and Pro bought on either side works on both (3.1.3(b)). The app's pages send
# what StoreKit gave them, and Apple's server sends notifications; either way
# bot/apple_store.py checks Apple's signature and decides who has Pro. All of
# it is off, and these addresses are not found, until APPLE_APP_ID is set.

APPLE_PRO_SAID = ("You have Pro through Apple; manage it in your iPhone's "
                  "Settings > Apple Account > Subscriptions.")
APPLE_RETRYING_SAID = ("Apple is still trying to renew the Pro you bought in the "
                       "iPhone app, after a payment did not go through, and will "
                       "charge you once it can. To keep that, update the payment "
                       "method in your iPhone's Settings > Apple Account. To pay "
                       "here instead, first cancel it there, under Subscriptions, "
                       "so you are not charged twice.")
APPLE_SUBSCRIPTIONS = "https://apps.apple.com/account/subscriptions"
# Apple's notifications are a few kilobytes.
APPSTORE_NOTICE_LIMIT = 128 * 1024
# What the app sends: up to twenty transactions of a few kilobytes each.
APPSTORE_POST_LIMIT = 256 * 1024
APPSTORE_JWS_PER_POST = 20
# Per account an hour (_rate_limited allows a network three times this). A
# page load sends nothing unless StoreKit has something unfinished.
APPSTORE_STATE_PER_HOUR = 240
APPSTORE_POSTS_PER_HOUR = 60
APPSTORE_MOVES_PER_HOUR = 5
# Notifications refused by kind of failure, so one that keeps happening (a
# wrong APPLE_APP_ID or APPLE_BUNDLE_ID) is said loudly, once an hour.
_APPSTORE_REFUSED: Dict[str, list] = {}
_APPSTORE_REFUSED_LOCK = threading.Lock()


def _appstore_refused(status: str) -> None:
    app.logger.warning("An App Store notification was refused: %s", status)
    now = time.time()
    with _APPSTORE_REFUSED_LOCK:
        seen = [t for t in _APPSTORE_REFUSED.get(status, []) if now - t < 3600]
        seen.append(now)
        _APPSTORE_REFUSED[status] = seen
        loud = len(seen) == 3
    if loud:
        from bot import monitoring
        monitoring.note("App Store notifications keep failing their check; "
                        "APPLE_APP_ID or APPLE_BUNDLE_ID may be wrong",
                        status=status)


@app.route("/apple/notifications", methods=["POST"])
def appstore_notifications():
    """Apple's server saying what happened to a subscription bought in the app:
    App Store Server Notifications, version 2.

    Server to server, with no cookie and no Origin; Apple's signature, checked
    to Apple's root, is what makes it believable. 200 only once it is stored.
    Anything else makes Apple send it again later (for about a week, in
    production): 400 for one that does not check out, 500 for a database that
    would not take it.
    """
    if not apple_store_mod.enabled():
        abort(404)
    if (request.content_length or 0) > APPSTORE_NOTICE_LIMIT:
        return jsonify({"error": "That is far larger than a notification."}), 413
    # The same cap for a body sent in chunks, which states no length up front.
    request.max_content_length = APPSTORE_NOTICE_LIMIT
    body = request.get_json(silent=True, force=True)
    signed = body.get("signedPayload") if isinstance(body, dict) else None
    if not isinstance(signed, str) or not signed:
        return jsonify({"error": "No signedPayload."}), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        status, outcome = apple_store_mod.handle_notification(
            conn, apple_store_mod.verifier(), signed, apple_store_mod.product_ids())
    except sqlite3.Error:
        app.logger.exception("Could not store an App Store notification; Apple "
                             "will send it again")
        return jsonify({"error": "Try again later."}), 500
    finally:
        conn.close()
    if status != 200:
        _appstore_refused(outcome)
    return jsonify({"outcome": outcome}), status


def _apple_state(conn, mint: bool) -> Dict:
    """What the app's subscribe screen needs to know about this person.

    pro is Pro by any means; source says which: "apple", or "web" for
    anything else (Stripe, an invite week), so the app sells nothing to
    someone who already has it. The appAccountToken goes with every
    purchase and is made the first time it is asked for.
    """
    user = g.user
    on = apple_store_mod.enabled()
    until = apple_store_mod.pro_until_ms(conn, user.id)
    apple = until > time.time() * 1000
    # Read without the Apple column, which may be older than the change just
    # made: whether Pro comes from anywhere else.
    elsewhere = billing_mod.is_pro(dataclasses.replace(user, apple_until=0),
                                   _is_operator())
    return {"enabled": on, "pro": apple or elsewhere,
            "source": "apple" if apple else ("web" if elsewhere else ""),
            "appleUntil": until,
            "appAccountToken": (apple_store_mod.account_token(conn, user.id)
                                if on and mint else ""),
            "products": apple_store_mod.product_ids() if on else []}


def _no_store(payload: Dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/apple/state")
def apple_state():
    """The app's subscribe screen: whether it may sell, who has Pro, the
    products, and this account's appAccountToken."""
    if getattr(g, "user", None) is None or g.demo:
        return _no_store({"error": "Not signed in."}, 401)
    if _from_another_site():
        return _no_store({"error": "That request came from another site."}, 403)
    if _rate_limited("apple_state", APPSTORE_STATE_PER_HOUR):
        return _no_store({"error": "Asked a lot in the last hour. Wait a while, "
                                   "then try again."}, 429)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return _no_store(_apple_state(conn, mint=True))
    finally:
        conn.close()


def _appstore_post_refused():
    """Why a post from the app's subscribe screen is refused, or None.

    These decide who has Pro, so beyond the session cookie (SameSite=Lax,
    which another site's form never carries), the browser's own account of
    where the request came from must be this site, and the body must be
    JSON, which a page elsewhere could only send after a check this server
    never answers.
    """
    if not apple_store_mod.enabled():
        return _no_store({"error": "Pro is not sold in the app just now."}, 404)
    if getattr(g, "user", None) is None or g.demo:
        return _no_store({"error": "Not signed in."}, 401)
    origin = request.headers.get("Origin")
    ours = {_origin_of(_public_base()), _origin_of(request.host_url)}
    if (origin is not None and _origin_of(origin) not in ours - {""}) \
            or request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
        return _no_store({"error": "That request came from another site."}, 403)
    if (request.content_length or 0) > APPSTORE_POST_LIMIT:
        return _no_store({"error": "That is far more than a purchase."}, 413)
    request.max_content_length = APPSTORE_POST_LIMIT
    if not isinstance(request.get_json(silent=True), dict):
        return _no_store({"error": "Send it as JSON."}, 400)
    return None


@app.route("/api/apple/transactions", methods=["POST"])
def apple_transactions():
    """Transactions StoreKit gave the app: after a purchase, a restore, or a
    page load that found some unfinished. JSON {"jws": [...]}, up to twenty.

    Each is checked and applied for the signed-in person, and comes back with
    its outcome. The app finishes a transaction once it has an answer here
    other than "invalid".
    """
    refused = _appstore_post_refused()
    if refused is not None:
        return refused
    if _rate_limited("apple_post", APPSTORE_POSTS_PER_HOUR):
        return _no_store({"error": "Sent a lot in the last hour. Wait a while, "
                                   "then try again."}, 429)
    signed = request.get_json(silent=True).get("jws")
    if isinstance(signed, str):
        signed = [signed]
    if not isinstance(signed, list) or len(signed) > APPSTORE_JWS_PER_POST \
            or not all(isinstance(item, str) for item in signed):
        return _no_store({"error": "jws must be a list of up to %d signed "
                                   "transactions." % APPSTORE_JWS_PER_POST}, 400)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        results = apple_store_mod.handle_app_transactions(
            conn, apple_store_mod.verifier(), g.user.id, signed,
            apple_store_mod.product_ids())
        out = _apple_state(conn, mint=False)
    finally:
        conn.close()
    out["results"] = results
    return _no_store(out)


@app.route("/api/apple/move", methods=["POST"])
def apple_move():
    """Move this Apple Account's Pro here from another Orenth account.

    JSON {"jws": one transaction the phone read just now from its current
    entitlements}. Only the subscription's running transaction moves it, at
    most once in 30 days, and the account it leaves is told by email when it
    has an address that can be written to; who it was is kept either way.
    """
    refused = _appstore_post_refused()
    if refused is not None:
        return refused
    if _rate_limited("apple_move", APPSTORE_MOVES_PER_HOUR):
        return _no_store({"error": "Tried a lot in the last hour. Wait a while, "
                                   "then try again."}, 429)
    signed = request.get_json(silent=True).get("jws")
    if not isinstance(signed, str) or not signed:
        return _no_store({"error": "jws must be one signed transaction."}, 400)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        try:
            result = apple_store_mod.move_to(conn, apple_store_mod.verifier(), g.user.id,
                                             signed, apple_store_mod.product_ids())
        except appstore_mod.VerificationError as exc:
            return _no_store({"error": "That subscription could not be confirmed "
                                       "with Apple.", "reason": exc.status}, 400)
        left = (accounts_mod.get_user(conn, result["previous"])
                if result.get("previous") else None)
        out = _apple_state(conn, mint=False)
    finally:
        conn.close()
    if left is not None:
        _tell_pro_moved(left)
    out["outcome"] = result["outcome"]
    if result.get("after"):
        out["after"] = result["after"]
    return _no_store(out)


def _tell_pro_moved(person) -> None:
    """Tell an account its Apple subscription was moved to another account.

    By email, when email is set up and the account has an address that can
    be written to. A failure is logged and changes nothing: the move is
    already made, and apple_subs keeps who it moved from.
    """
    address = (person.email or "").strip()
    if person.is_local or not address or address.endswith(".invalid") \
            or not codes_mod.email_ready():
        app.logger.info("Moved an Apple subscription away from account %d; no "
                        "email could say so", person.id)
        return
    when = time.strftime("%d %B %Y at %H:%M UTC", time.gmtime()).lstrip("0")
    body = (
        "The Orenth Pro subscription bought through Apple for your account "
        "(%s) was moved to another Orenth account on %s. Someone signed in to "
        "Orenth on an iPhone that uses the Apple Account the subscription was "
        "bought with asked for it.\n\n"
        "Your account no longer has Pro from that subscription. Nothing else in "
        "your account changed, and Apple's billing did not change.\n\n"
        "If that was you, there is nothing to do. If it was not, check who can "
        "use that Apple Account. To bring Pro back, open Orenth on that iPhone "
        "signed in to this account, go to Pricing and tap Move Pro to this "
        "account. A subscription can move once every 30 days.\n" % (address, when))
    try:
        codes_mod.send_mail(address, "Your Orenth Pro moved to another account", body)
    except codes_mod.CodeError:
        app.logger.warning("Could not email account %d that its Apple "
                           "subscription moved", person.id)


@app.route("/signout", methods=["POST"])
def signout():
    """End this session everywhere, not just in this browser.

    POST only. The session cookie travels with a link followed from another
    site, so a GET here let any page sign its visitors out.
    """
    token = session.pop(SESSION_KEY, "")
    if token:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            # Notifications switched on in this sign-in go with it, so the
            # next person on a shared phone is not shown this one's alerts.
            push_mod.forget_session(conn, _session_ref(token))
            accounts_mod.end_session(conn, token)
        finally:
            conn.close()
    session.clear()
    return redirect(url_for("signin", said="out"))


@app.route("/account", methods=["GET"])
def account():
    """What this account holds, and how to leave."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        counts = accounts_mod.owned_counts(conn, uid())
        everyone = accounts_mod.list_users(conn) if g.user.is_local else []
        methods = accounts_mod.sign_in_methods(conn, g.user)
    finally:
        conn.close()
    plan = {"on": billing_mod.ready(), "pro": _pro(), "customer": ""}
    invite = {}
    if plan["on"]:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            plan["customer"] = billing_mod.customer_of(conn, uid())
            plan["used"] = billing_mod.views_today(conn, uid())
            if not g.user.is_local:
                invite = {"url": _public_base() + url_for(
                              "join", code=invites_mod.code_for(conn, uid())),
                          "joined": invites_mod.joined(conn, uid())}
        finally:
            conn.close()
        plan["limit"] = billing_mod.free_limits()["analyses_per_day"]
        plan["bonus_until"] = (g.user.bonus_until
                               if g.user.bonus_until > time.time() else 0)
    weekly = {"ready": digest_mod.ready() and digest_mod.can_receive(g.user),
              "on": g.user.weekly_email}
    return render_template("account.html", counts=counts, everyone=everyone,
                           methods=methods,
                           datasets=export_mod.DATASETS, plan=plan,
                           invite=invite, weekly=weekly,
                           invite_days=invites_mod.BONUS_DAYS,
                           apple=_apple_plan())


@app.route("/account/delete", methods=["POST"])
def delete_account():
    """Remove this account and everything personal in it."""
    # The word as a phone types it: an iPhone starts a box with a capital,
    # and its suggestions leave a space after the word.
    if (request.form.get("confirm") or "").strip().lower() != "delete":
        return render_template("error.html", symbol="",
                               message="Type delete to confirm."), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        # Stop the subscription first, or a deleted account keeps being
        # charged. If Stripe cannot be reached, nothing is deleted.
        customer = billing_mod.customer_of(conn, uid())
        if customer and billing_mod.ready():
            billing_mod.close_customer(customer)
        _end_apple_sign_in(conn, uid())
        accounts_mod.delete_user(conn, uid())
    except billing_mod.BillingError:
        return render_template(
            "error.html", symbol="",
            message="Your subscription could not be cancelled just now, so "
                    "nothing was deleted. Try again in a minute."), 502
    except ValueError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    session.clear()
    return redirect(url_for("signin", said="deleted"))


def _end_apple_sign_in(conn, user_id: int) -> None:
    """Ask Apple to end Sign in with Apple for an account about to go.

    The App Store asks for this. Unlike Stripe's, a failure does not stop the
    deletion: nothing keeps charging, and the person can still end it in their
    Apple Account settings, so it is logged and the deletion goes ahead. There
    is no queue to try again later, which would mean keeping a deleted
    person's token after their account is gone.
    """
    for identity_id, refresh_token, client_id in accounts_mod.apple_tokens(
            conn, user_id):
        if not apple_mod.revoke(refresh_token, client_id):
            app.logger.warning("Apple did not confirm ending sign-in %d of an "
                               "account being deleted; it is deleted anyway",
                               identity_id)


def _redirect_uri() -> str:
    """The callback address, which must match the OAuth client exactly.

    Three sources, most explicit first, because a mismatch here is the single
    most common reason a Google sign-in fails and the error it produces names
    the address rather than the cause.
    """
    configured = (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public + url_for("google_callback")
    return url_for("google_callback", _external=True)


def _is_operator() -> bool:
    """Whether this account may see figures covering everybody.

    The local account is whoever runs the server on their own machine. On a
    deployment there is no local account, so STOCKBOT_ADMIN_EMAIL names who the
    operator is. Anyone else gets a flat refusal: activity across all users is
    not something an ordinary user should be able to read.
    """
    user = getattr(g, "user", None)
    if user is None:
        return False
    if user.is_local:
        return True
    allowed = {e.strip().lower()
               for e in (os.environ.get("STOCKBOT_ADMIN_EMAIL") or "").split(",")
               if e.strip()}
    return bool(allowed) and user.email.lower() in allowed


@app.route("/usage")
def usage():
    """What people actually do with it. Operator only."""
    if not _is_operator():
        # How to become the operator is in DEPLOY.md, for whoever runs it;
        # an ordinary account cannot set anything on the server.
        return render_template(
            "error.html", symbol="",
            message="This page covers activity across every account, so it is "
                    "limited to whoever runs this installation."), 403

    try:
        days = max(1, min(365, int(request.args.get("days", "30"))))
    except ValueError:
        days = 30

    from bot import offsite as offsite_mod
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        data = usage_mod.read(conn, days)
        counts = events_mod.read(conn, days)
        # Read from the settings and the last result written down; opening
        # this page never reaches the storage itself.
        offsite = offsite_mod.status(conn)
    finally:
        conn.close()

    trend = ""
    if len(data["daily"]) > 1:
        # Days come as epoch seconds and runs as counts: said as dates and
        # whole numbers, not as the years and percents of a filed figure.
        trend = charts_mod.bar_chart(
            [d["day"] for d in data["daily"]],
            [float(d["runs"]) for d in data["daily"]],
            width=720, height=180, title="Analyses per day",
            label=lambda ts: time.strftime("%d %b", time.gmtime(ts)).lstrip("0"),
            fmt=lambda v: "%d" % round(v))

    # Where the database actually sits, because the answer decides whether a
    # deploy keeps everyone's journal or silently deletes it, and nothing else
    # in the app ever says. A path inside the container survives nothing.
    db_path = db_mod.default_path() if not app.config.get("DB_PATH")         else app.config["DB_PATH"]
    try:
        db_size = os.path.getsize(db_path)
    except OSError:
        db_size = 0
    storage = {
        "path": db_path,
        "size": db_size,
        # A mounted disk is somewhere the container is not. The check is crude
        # on purpose: it reports what it sees rather than claiming to know.
        "on_disk": db_path.startswith("/data") or db_path.startswith("/var/data"),
    }

    # The timed backups on this disk. serve.py says how often it takes them;
    # started any other way, none are taken. A missing folder is no backups.
    import backup as backup_mod
    hours = app.config.get("BACKUP_HOURS") or 0
    try:
        age, kept = (backup_mod.newest_age(backup_mod.folder()),
                     backup_mod.how_many(backup_mod.folder()))
    except OSError:
        age, kept = None, 0
    backups = {"hours": hours, "kept": kept, "age": age,
               "taken": None if age is None else time.time() - age,
               "late": hours > 0 and (age is None or age > 2 * hours * 3600)}

    from bot import upstream as upstream_mod
    return render_template("usage.html", data=data, days=days, trend=trend,
                           storage=storage, counts=counts, offsite=offsite,
                           backups=backups,
                           upstream=upstream_mod.stats(),
                           scheduler=(ALERTS.status() if ALERTS else None))


ROBOTS = """User-agent: *
Allow: /$
Allow: /signin
Allow: /privacy
Allow: /terms
Disallow: /analyse
Disallow: /research
Disallow: /trades
Disallow: /portfolio
Disallow: /monitor
Disallow: /account
Disallow: /usage
Disallow: /export
Disallow: /auth
Disallow: /api

Sitemap: %s/sitemap.xml
"""

# The pages a stranger can read without signing in. Everything else returns a
# redirect to a crawler, which reads as a dead end and is worth saying plainly
# rather than letting a robot discover one 302 at a time.
PUBLIC_PAGES = [("/", "weekly", "1.0"), ("/signin", "monthly", "0.6"),
                ("/privacy", "yearly", "0.3"), ("/terms", "yearly", "0.3")]
PUBLIC_PAGES.append(("/tools/size", "monthly", "0.5"))
PUBLIC_PAGES.append(("/tools/survival", "monthly", "0.5"))


def _public_base() -> str:
    """The address to print in robots.txt and the sitemap.

    Both files must name absolute URLs, and both are wrong if they name the
    internal host instead of the domain people typed.
    """
    return ((os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip().rstrip("/")
            or request.url_root.rstrip("/"))


# Other places this same product exists, for the sameAs property below.
#
# This is how a search engine tells one Orenth from another: there is a watch
# brand, a consulting group and an iOS app using the word, and without a set of
# corroborating profiles all pointing back here, they are one blurred entity.
#
# Only add addresses that resolve and that carry this name. A link to a profile
# branded something else is worse than no link, because it argues the opposite
# of what it is here to say. Add to this list as the profiles get made:
# GitHub, a Product Hunt page, an X account, a Reddit account, Wikidata.
SAME_AS: list = []


@app.context_processor
def _brand():
    """The identity block, so every page says the same thing about the app."""
    base = _public_base()
    return {"site_base": base,
            "site_name": "Orenth",
            "site_logo": base + url_for("static", filename="logo.png"),
            "same_as": SAME_AS,
            # One source for every page that counts the strategies: those
            # that decide the call, and apart from them those on trial.
            "strategy_count": len(strategies_mod.voting_names()),
            "daily_strategy_count": len(strategies_mod.daily_voting_names()),
            "trial_count": len(strategies_mod.TRIAL),
            "daily_trial_count": len([n for n in strategies_mod.TRIAL
                                      if n not in strategies_mod.INTRADAY_ONLY]),
            "trial_names": strategies_mod.TRIAL}


@app.context_processor
def _preview_urls():
    """Absolute https addresses for the link-preview tags.

    url_for(_external=True) builds from what the app sees, and behind a TLS
    terminator that is a plain http internal request. X and several chat
    clients silently drop a card whose image is not https, so these are built
    from the public address rather than the observed one. The query string is
    dropped because a scraper lands on ?next=... and that is not the address
    anyone should be sharing.
    """
    base = _public_base()
    return {"canonical_url": base + request.path,
            "preview_image": base + url_for("static", filename="preview.png")}


@app.route("/favicon.ico")
def favicon():
    """Serve a real icon file from the address everything asks for.

    Browsers request /favicon.ico whether or not a page links to one, and
    Google will not use a data: URI as the icon beside a search result: it
    fetches a file or shows a generic globe. This route existed as neither, so
    the request fell through to the sign-in redirect and the search result got
    the globe.
    """
    return send_from_directory(
        os.path.join(app.root_path, "static"), "favicon.ico",
        mimetype="image/x-icon", max_age=60 * 60 * 24 * 7)


# The installable app. A phone or a computer offers to install a site when it
# finds a manifest, a service worker and https. The worker keeps public files
# under a name that changes whenever one of them does, worked out once here at
# start-up from the files themselves.
ASSET_VERSION = webapp_mod.asset_version(app.root_path)
# Pages ask for the files the worker keeps with ?v= and this version, and the
# worker answers only those from its copy. A version from before the worker,
# put back by a rollback, asks for them plain, and gets its own from here.
app.jinja_env.globals["asset_version"] = ASSET_VERSION


@app.route("/manifest.webmanifest")
def manifest():
    """What a browser needs to install Orenth as an app."""
    body = webapp_mod.manifest(
        lambda name: url_for("static", filename=name),
        start=url_for("index", source="app"),
        shortcuts=[("Analyse", url_for("index")), ("Monitor", url_for("monitor")),
                   ("Journal", url_for("trades"))])
    return Response(json.dumps(body, indent=1), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.route("/sw.js")
def service_worker():
    """The service worker, from the site's root so it can cover every page.

    no-cache, so a browser checks for a new one on every visit rather than
    keeping an old one for a day. The same file for everybody: nothing in it
    depends on who asked. Every file it keeps is named with this version, as
    the pages ask for it.
    """
    def static(name):
        return url_for("static", filename=name, v=ASSET_VERSION)

    body = app.jinja_env.get_template("sw.js").render(
        version=ASSET_VERSION, offline=url_for("offline"),
        precache=[static(name) for name in webapp_mod.PRECACHE],
        static_prefix=app.static_url_path.rstrip("/") + "/",
        icon=static("icon-192.png"), badge=static("badge-96.png"),
        home=url_for("monitor"), config_url=url_for("push_config"),
        subscribe_url=url_for("push_subscribe"))
    return Response(body, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache",
                             "Service-Worker-Allowed": "/"})


@app.route("/offline")
def offline():
    """The page the service worker shows when there is no connection.

    Kept on the device, so it is the same for everyone and holds nothing
    about whoever is signed in: it stands alone rather than extending
    base.html, which carries the person's name and menu.
    """
    body = app.jinja_env.get_template("offline.html").render(
        light=webapp_mod.LIGHT, dark=webapp_mod.DARK)
    return Response(body, mimetype="text/html",
                    headers={"Cache-Control": "no-cache"})


@app.route("/robots.txt")
def robots():
    return Response(ROBOTS % _public_base(), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    base = _public_base()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = public_mod.verdicts(conn)
    finally:
        conn.close()
    # A strategy built for intraday prices never fires on the daily ones these
    # tests use, and its page would stay empty: not one to send a robot to.
    pages = PUBLIC_PAGES + [("/stocks", "daily", "0.8")] + [
        ("/stock/" + public_mod.slug(sym), "daily", "0.7")
        for sym, _ in public_mod.POPULAR] + [
        ("/backtests", "daily", "0.8"), ("/record", "daily", "0.8")] + [
        ("/backtest/" + public_mod.strategy_slug(name), "daily", "0.7")
        for name in strategies_mod.strategy_names()
        if public_mod.strategy_results(rows, name)]
    # The Learn pages once they have something measured; until then they ask
    # search engines to wait, so they are not sent there.
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        learned = learn_mod.measured_slugs(conn, _registered_setups())
    finally:
        conn.close()
    if learned:
        pages += [("/learn", "daily", "0.7")] + [
            ("/learn/" + slug, "daily", "0.6") for slug in learned]
    rows = "".join(
        "<url><loc>%s%s</loc><changefreq>%s</changefreq>"
        "<priority>%s</priority></url>" % (base, path, freq, priority)
        for path, freq, priority in pages)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '%s</urlset>' % rows, mimetype="application/xml")


@app.route("/privacy")
def privacy():
    """Required by Google's consent screen, and worth having regardless."""
    import serve
    from bot import monitoring
    from bot import offsite as offsite_mod
    # Copies of the backups hold everyone's data, so once they are kept off
    # this server the page names who holds them and for how long. Never the
    # bucket or its address.
    offsite = {"provider": offsite_mod.provider(), "days": offsite_mod.KEEP_DAYS,
               "months": offsite_mod.KEEP_MONTHS}
    # So does the copy serve.py takes each time a new version starts. The
    # page gives how many it keeps and, where serve.py also deletes them by
    # age (RELEASE_COPY_DAYS), after how many days; read from serve.py, so the
    # two cannot drift apart.
    release = {"keep": serve.KEEP_RELEASE_COPIES,
               "days": getattr(serve, "RELEASE_COPY_DAYS", 0)}
    # Who carries and runs this copy, named where it can be told: Cloudflare
    # adds CF-Connecting-IP to every request it carries (_client_ip reads
    # it), Render sets RENDER and RENDER_SERVICE_ID on its machines, and Fly
    # sets FLY_APP_NAME.
    hosting = {"proxy": "Cloudflare" if request.headers.get("CF-Connecting-IP") else "",
               "server": ("Render" if os.environ.get("RENDER")
                          or os.environ.get("RENDER_SERVICE_ID")
                          else "Fly.io" if os.environ.get("FLY_APP_NAME") else "")}
    return render_template("legal.html", page="privacy",
                           heading="Privacy", updated=LEGAL_UPDATED,
                           session_days=accounts_mod.SESSION_DAYS,
                           contact=_contact(), offsite=offsite, hosting=hosting,
                           release=release,
                           sampled=_sampled_words(monitoring.status()["sampled"]))


def _sampled_words(share: float) -> str:
    """How many page loads go to Sentry when nothing breaks, in words; empty
    when none do."""
    if share <= 0:
        return ""
    if share >= 1:
        return "all"
    if share >= 0.01:
        return "about %d in every 100" % round(share * 100)
    return "fewer than 1 in every 100"


@app.route("/terms")
def terms():
    return render_template("legal.html", page="terms",
                           heading="Terms of use", updated=LEGAL_UPDATED,
                           session_days=accounts_mod.SESSION_DAYS,
                           contact=_contact())


def _contact() -> str:
    """The address the legal pages give for questions, or empty.

    Unset, it used to print "set STOCKBOT_CONTACT to an email address" to
    every visitor, who can do nothing about it. Now the line is left out, and
    only whoever runs the server is told how to fill it.
    """
    return (os.environ.get("STOCKBOT_CONTACT") or "").strip()


@app.route("/export/<dataset>.<fmt>")
def export_data(dataset, fmt):
    """Hand back one dataset as a file. Your record, in a format you can read."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        built = export_mod.build(conn, dataset, fmt, user=uid())
    except ValueError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 404
    finally:
        conn.close()

    return Response(
        built["body"], mimetype=built["mimetype"],
        headers={"Content-Disposition":
                 'attachment; filename="%s"' % built["filename"]})


def _utc_clock(ts) -> str:
    """A time of day in UTC, for saying when a list's prices were read."""
    return time.strftime("%H:%M UTC", time.gmtime(float(ts)))


@app.route("/ideas")
def ideas():
    """What is worth looking at, given how you trade."""
    market = request.args.get("market") or "stocks"
    horizon = request.args.get("horizon") or "medium"
    risk = request.args.get("risk") or "balanced"
    cfg = _cfg()

    result = None
    busy = ""
    asked = (request.args.get("market") or request.args.get("horizon")
             or request.args.get("risk"))
    if g.demo and asked and horizon == "short":
        return _locked("Create an account to scan for short holds",
                       "The example scans daily bars. With an account you "
                       "can scan for holds of a few days on hourly bars.",
                       examples=False)
    if asked:
        try:
            # The scan runs in the background. The request that starts it
            # waits a few seconds for it; one joining a scan already running,
            # such as the page reloading itself, answers at once with how far
            # it has got.
            result = (_demo_shortlist(market, horizon, risk) if g.demo else
                      ideas_mod.find(market, horizon, cfg, risk,
                                     wait=ideas_mod.PAGE_WAIT, join_wait=0))
        except ideas_mod.Busy as exc:
            # Said on the Find page itself, with the form, so asking again is
            # one press away.
            busy = str(exc)
        except Exception as exc:
            return render_template("error.html", symbol="", message=_plainly(
                exc, "The scan could not be finished. Try again in a minute.")), 502
    if result is not None:
        result = dict(result)
        result.setdefault("passed_total", len(result["passed"]))
        result.setdefault("watch_total", len(result["watch"]))
        result.setdefault("watch_shown", len(result["watch"]))
        # Scans are shared between everyone asking the same question, so
        # this one may have run minutes ago. The page says so, with the clock
        # time, rather than presenting a stale list as this second's answer.
        if result.get("scanned_at"):
            age = max(0.0, time.time() - result["scanned_at"])
            result.update(age=age, checked_at=_utc_clock(result["scanned_at"]),
                          # Rounded up, so a list read fifty seconds ago does
                          # not say it was read "0 minutes ago".
                          age_minutes=int((age + 59) // 60) if age > 45 else 0)

    # Visitors included: an example that showed more than a free account would
    # make signing up look like a downgrade. The counts beside the shortened
    # lists are the full ones, from before anything was cut.
    if result is not None and not _pro():
        keep = billing_mod.free_limits()["find_results"]
        if len(result["passed"]) > keep or len(result["watch"]) > keep:
            result = dict(result, passed=result["passed"][:keep],
                          watch=result["watch"][:keep], limited=keep)

    return render_template("ideas.html", result=result, busy=busy,
                           market=market if market in ideas_mod.MARKETS else "stocks",
                           horizon=horizon if horizon in ideas_mod.HORIZONS else "medium",
                           risk=risk if risk in ideas_mod.RISKS else "balanced",
                           markets=ideas_mod.MARKETS, horizons=ideas_mod.HORIZONS,
                           risks=ideas_mod.RISKS), (503 if busy else 200)


# The example account's scans: the house settings, and reused for an hour,
# so visitors cannot make the server run a 150-instrument scan each.
DEMO_SCAN_TTL = 60 * 60.0


def _demo_shortlist(market: str, horizon: str, risk: str) -> Dict:
    cfg = config_mod.load(app.config.get("CFG_PATH"))
    return ideas_mod.find(market, horizon, cfg, risk, wait=ideas_mod.PAGE_WAIT,
                          ttl=DEMO_SCAN_TTL, join_wait=0)


SCREEN_FAILED = "The screen could not be finished. Try again in a minute."


@app.route("/screener", methods=["GET"])
def screener():
    """Find candidates, with the reason each one qualified."""
    preset_key = request.args.get("preset") or ""
    preset = screener_mod.PRESETS.get(preset_key)

    filters: Dict[str, float] = {}
    universe = request.args.get("universe") or "most_actives"
    symbols_raw = (request.args.get("symbols") or "").strip()

    if preset:
        filters = dict(preset["filters"])
        universe = preset["universe"]
    else:
        import math
        for key, spec in screener_mod.FILTERS.items():
            raw = request.args.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if not math.isfinite(value):
                continue
            # Percent fields are typed as whole percents: 15 is 15%, 0.5 is
            # half a percent and -3 is down 3%. Guessing from the size of the
            # number turned "up at most -3" into -300% and "up at least 1"
            # into 100%.
            if spec["unit"] == "pct":
                value = value / 100.0
            filters[key] = value

    # The boxes show percents the way they are typed, so running the same
    # screen again asks the same question.
    typed = {key: ("%.6f" % (value * 100 if screener_mod.FILTERS[key]["unit"] == "pct"
                             else value)).rstrip("0").rstrip(".")
             for key, value in filters.items()}

    result = None
    if g.demo and request.args.get("go") and not preset:
        return _locked("Create an account to run your own screen",
                       "The example can run the ready-made screens. With an "
                       "account you can set your own filters.", examples=False)
    if g.demo and preset:
        # A ready-made screen is the same answer for every visitor, so it is
        # run once and shared rather than once per visit. Only a screen that
        # read everything is shared: one the data source refused is shown to
        # the visitor who ran it, and the next visitor gets a fresh try rather
        # than that failure for a quarter of an hour.
        from bot import upstream as upstream_mod

        class _NotKept(Exception):
            def __init__(self, found):
                super().__init__("not kept")
                self.found = found

        def fresh():
            found = screener_mod.run(symbols=None, universe=universe,
                                     filters=filters, limit=40)
            if not found.complete:
                raise _NotKept(found)
            return found

        try:
            result = upstream_mod.cached(
                "demo-screen:" + preset_key, fresh, ttl=DEMO_TTL,
                # Longer than a screen can take, so a second visitor waits for
                # the first one's answer rather than running the screen again.
                wait=screener_mod.BUDGET + 30.0)
        except _NotKept as kept:
            result = kept.found
        except Exception as exc:
            return render_template("error.html", symbol="", message=_plainly(
                exc, SCREEN_FAILED)), 500
    elif request.args.get("go") or preset:
        symbols = [s.strip() for s in symbols_raw.replace(",", " ").split()
                   ] if symbols_raw else None
        try:
            result = screener_mod.run(symbols=symbols, universe=universe,
                                      filters=filters, limit=40)
        except Exception as exc:
            return render_template("error.html", symbol="", message=_plainly(
                exc, SCREEN_FAILED)), 500

    return render_template("screener.html", result=result, presets=screener_mod.PRESETS,
                           universes=screener_mod.UNIVERSES, filters=filters,
                           typed=typed, universe=universe, symbols_raw=symbols_raw,
                           preset_key=preset_key,
                           checked_at=_utc_clock(result.at) if result else "",
                           filter_specs=screener_mod.FILTERS)


@app.route("/portfolio")
def portfolio():
    """Exposure, concentration, correlation and what a shock would do."""
    cfg = _cfg()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        # What is held: a position closed in part counts only what is left.
        rows = db_mod.open_positions(conn, limit=100, user=uid())
        summary = db_mod.journal_summary(conn, user=uid(), costs=_costs())
    finally:
        conn.close()

    view = portfolio_mod.build(rows, cfg["account_size"], cost_bps={
        kind: config_mod.cost_bps_for(kind, cfg) for kind in ("equity", "crypto", "other")})
    # What the prices just read saw is kept, as the journal keeps it.
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        portfolio_mod.keep_reads(conn, view)
    finally:
        conn.close()
    shocks = {pct: portfolio_mod.what_if(view, pct)
              for pct in (-0.05, -0.10, -0.20)} if view.positions else {}
    return render_template("portfolio.html", view=view, cfg=cfg,
                           summary=summary, shocks=shocks)


@app.route("/monitor")
def monitor():
    """Alerts, watchlist, saved theses and the automated routines."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        alert_rows = db_mod.alert_list(conn, user=uid())
        watch_rows = db_mod.watchlist(conn, user=uid())
        thesis_rows = db_mod.thesis_list(conn, limit=20, user=uid())
        events = db_mod.event_list(conn, uid(), limit=15)
    finally:
        conn.close()

    described = [(r, alerts_mod.describe(r["kind"], r["threshold"], r["symbol"]))
                 for r in alert_rows]

    # Price each open thesis so the page can say how it is doing, and propose
    # the outcome the price supports. One quote per distinct symbol.
    judged = []
    bars_of = {}
    for row in thesis_rows:
        sym = row["symbol"]
        if sym not in bars_of:
            try:
                bars_of[sym] = market_mod.fetch_bars(sym, "1d")
            except Exception:
                # One unpriceable symbol must not take the whole page down.
                bars_of[sym] = None
        price, split = _thesis_price(row, bars_of[sym])
        stored = {"price": row["price"], "balance": row["balance"],
                  "created": row["created_ts"]}
        look = (thesis_mod.review(stored, price) if price else
                {"usable": False, "reason": "Could not price %s today." % sym})
        proposed = (thesis_mod.suggested_outcome(stored, price)
                    if price and not row["outcome"] else None)
        judged.append({"row": row, "now": price, "review": look,
                       "proposed": proposed,
                       "quoted": bars_of[sym].last_price if split != 1.0 else None,
                       "split_words": (market_mod.split_words(split)
                                       if split != 1.0 else "")})

    board = thesis_mod.scoreboard([dict(r) for r in thesis_rows])

    return render_template("monitor.html", alerts=described, watchlist=watch_rows,
                           theses=judged, board=board,
                           outcomes=thesis_mod.OUTCOMES, kinds=alerts_mod.KINDS,
                           workflows=workflows_mod.WORKFLOWS,
                           # This person's own last routine. A report holds
                           # their alerts, positions and watchlist.
                           last_run=(None if g.demo else
                                     app.config.get("LAST_WORKFLOW_BY_USER", {}).get(uid())),
                           events=events,
                           loop=({k: v for k, v in ALERTS.status().items()
                                  if k in ("running", "interval", "last_run")}
                                 if ALERTS is not None else None),
                           email_ready=codes_mod.email_ready(),
                           alert_email=bool(getattr(g.user, "alert_email", False)),
                           can_email=bool(g.user and g.user.email
                                          and not g.user.email.endswith(".invalid")
                                          and not g.user.is_local))


def _over_alert_cap(conn):
    """The pricing page if one more running alert would pass the free limit.

    Asked wherever an alert is switched on: adding one, resuming one, and
    re-arming one that fired. Asked only when adding, re-arming was a way
    round it.
    """
    if _pro():
        return None
    cap = billing_mod.free_limits()["alerts"]
    if len(db_mod.alert_list(conn, active_only=True, user=uid())) < cap:
        return None
    return _upgrade("A free account can run %d alert%s at a time. Pause or "
                    "delete one on the Monitor page first, or Pro has no limit."
                    % (cap, "" if cap == 1 else "s"))


def _alert_value(kind: str, raw: Optional[str]) -> Optional[float]:
    """The value typed into an alert form, as the alert stores it.

    A percent is typed as a whole number, 5 for 5%, and stored as 0.05. Always
    divided: guessing from the size read 0.5 as 50% and -5 as -500%.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    value = float(raw)
    if alerts_mod.KINDS.get(kind, {}).get("unit") == "percent":
        value = value / 100.0
    return value


@app.route("/monitor/alert/add", methods=["POST"])
def add_alert():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        refused = _over_alert_cap(conn)
        if refused is not None:
            return refused
        kind = request.form["kind"]
        threshold = _alert_value(kind, request.form.get("threshold"))
        # The same ticker an analysis would use: "btc" is Bitcoin, not the
        # fund listed as BTC, and " aapl" is AAPL. Picked from the
        # suggestions, or set from its analysis, with exact=1, BTC is the
        # fund, and the alert remembers that it means it exactly.
        symbol, exact = _symbol_from(request.form)
        symbol = market_mod.normalise_symbol(symbol, exact=exact)
        # Pro is promised as many alerts as it likes; the ceiling keeps any
        # other account's list in bounds.
        db_mod.alert_add(conn, symbol, kind, threshold,
                         request.form.get("interval") or "1d",
                         request.form.get("note", ""),
                         request.form.get("repeat") == "1", user=uid(),
                         cap=None if _pro() else db_mod.MAX_ALERTS, exact=exact)
        events_mod.bump(conn, "alert_add")
    except (KeyError, ValueError, DataError) as exc:
        message = str(exc)
        if isinstance(exc, KeyError):
            message = "The alert form was incomplete. Try again from the Monitor page."
        elif isinstance(exc, DataError):
            message = "Type a ticker, for example AAPL."
        elif "could not convert" in message:
            message = "The alert value must be a number."
        return render_template("error.html", message=message, symbol=""), 400
    finally:
        conn.close()

    # Setting an alert from an analysis should leave you on that analysis, with
    # something that says it worked.
    back = _safe_back(request.form.get("back"), "")
    if back:
        return redirect(back + ("alert_set=1" if back.endswith("?")
                                else "&alert_set=1"))
    return redirect(url_for("monitor"))


@app.route("/monitor/alert/<action>", methods=["POST"])
def alert_action(action):
    if action not in ("delete", "reset", "pause", "resume", "edit"):
        return render_template("error.html", message="Unknown action.", symbol=""), 404
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        alert_id = int(request.form["alert_id"])
        if action == "delete":
            db_mod.alert_delete(conn, alert_id, user=uid())
        elif action == "pause":
            db_mod.alert_set_active(conn, alert_id, False, user=uid())
        elif action == "resume":
            # Resuming counts as switching an alert on, so the free plan's
            # limit applies here as it does to adding one.
            refused = _over_alert_cap(conn)
            if refused is not None:
                return refused
            db_mod.alert_set_active(conn, alert_id, True, user=uid())
        elif action == "edit":
            row = conn.execute("SELECT kind FROM alerts WHERE id=? AND user_id=?",
                               (alert_id, uid())).fetchone()
            threshold = _alert_value(row["kind"] if row else "",
                                     request.form.get("threshold"))
            db_mod.alert_edit(conn, alert_id, threshold,
                              (request.form.get("note") or "").strip(), user=uid())
        else:
            # Re-arming switches a fired alert back on: the same limit.
            refused = _over_alert_cap(conn)
            if refused is not None:
                return refused
            db_mod.alert_reset(conn, alert_id, user=uid())
    except (KeyError, ValueError) as exc:
        message = str(exc)
        if "could not convert" in message:
            message = "The alert value must be a number."
        return render_template("error.html", message=message, symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("monitor"))


@app.route("/monitor/alerts/check", methods=["POST"])
def check_alerts():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        result = _check_and_tell(conn, user=uid())
    finally:
        conn.close()
    return redirect(url_for("monitor", checked=result["checked"],
                            fired=len(result["fired"])))


@app.route("/monitor/alert-email", methods=["POST"])
def alert_email():
    """Switch alert emails on or off for the signed-in person."""
    if g.demo or not codes_mod.email_ready():
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        accounts_mod.set_alert_email(conn, uid(), request.form.get("on") == "1")
    finally:
        conn.close()
    return redirect(url_for("monitor"))


# Notifications on phones and browsers: the device a person is on tells the
# server where to send, and bot/push.py sends there when an alert fires.
# Switching a device on or off, and testing it, are generous for a person
# and refused to a script doing it over and over.
PUSH_CHANGES_PER_HOUR = 60
PUSH_TESTS_PER_HOUR = 6


def _origin_of(url: str) -> str:
    """scheme://host[:port] of an address, as a browser writes an Origin."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return ""
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if not scheme or not host:
        return ""
    if ":" in host:
        host = "[%s]" % host
    default = {"http": 80, "https": 443}.get(scheme)
    return "%s://%s%s" % (scheme, host, "" if port in (None, default) else ":%d" % port)


def _push_post_refused():
    """Why a post to the notification endpoints is refused, or None.

    These decide what reaches someone's phone, so beyond the session cookie
    (SameSite=Lax, which another site's form never carries) the browser's own
    account of where the request came from is checked: an Origin other than
    this site's is refused, and so is a body that is not JSON, which a page
    elsewhere could only send after a check this server never answers. This
    site is its public address, or the address the request was sent to,
    which a page on another site cannot choose for its visitor's browser.
    """
    if getattr(g, "user", None) is None or g.demo:
        return jsonify({"error": "Not signed in."}), 401
    origin = request.headers.get("Origin")
    ours = {_origin_of(_public_base()), _origin_of(request.host_url)}
    if (origin is not None and _origin_of(origin) not in ours - {""}) \
            or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "That request came from another site."}), 403
    if (request.content_length or 0) > 16384:
        return jsonify({"error": "That is far more than a device's details."}), 413
    # The same cap for a body sent in chunks, which states no length up front.
    request.max_content_length = 16384
    if not isinstance(request.get_json(silent=True), dict):
        return jsonify({"error": "Send the details as JSON."}), 400
    return None


def _session_ref(token: Optional[str] = None, purpose: str = "push") -> str:
    """A hash naming this sign-in, kept on the devices it switches on.

    Signing out forgets them, so the next person to sign in on a shared phone
    is not sent the last one's alerts. A hash, so the table never holds a
    token that could be used to sign in.
    """
    import hashlib
    token = session.get(SESSION_KEY, "") if token is None else token
    if not token:
        return ""
    return hashlib.sha256(("%s|%s" % (purpose, token)).encode("utf-8")).hexdigest()[:32]


@app.context_processor
def _push_mark():
    """A mark that changes with each sign-in, for static/notify.js.

    It reminds the server of a device with notifications on once a visit,
    and has to know when a new sign-in has started, since signing out forgot
    the device. A hash of its own, so the page holds nothing the database
    or the session cookie does.
    """
    return {"push_mark": _session_ref(purpose="page")[:12]}


@app.route("/api/push/config")
def push_config():
    """What a page needs to switch notifications on for its device."""
    if g.demo:
        abort(404)
    keys = push_mod.vapid_keys()
    out = {"web": {"enabled": keys is not None,
                   "public_key": keys.public if keys else ""},
           "apns": {"enabled": push_mod.apns_ready()}}
    # Why it is off, for whoever runs this copy on their own machine only.
    if g.user.is_local and keys is None:
        out["web"]["note"] = push_mod.why_not()["web"]
    return jsonify(out)


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    """Send this person's alerts to the device this request came from.

    From a tap on "Turn on", the device is kept for this person, and moved
    to them if someone else had it. With "check", or "replaces" naming the
    address the device had before, it is only kept when it is already this
    person's: that is how a page or the service worker keeps a device up to
    date without bringing back one that was switched off, removed from
    another device, or forgotten when its sign-in ended. "known" in the
    answer says which.
    """
    refused = _push_post_refused()
    if refused is not None:
        return refused
    if _rate_limited("push", PUSH_CHANGES_PER_HOUR):
        return jsonify({"error": "Notifications were switched on and off a lot "
                                 "in the last hour. Wait a while, then try "
                                 "again."}), 429
    data = request.get_json(silent=True)
    kind = data.get("kind")
    check = data.get("check") is True
    old = data.get("replaces") if isinstance(data.get("replaces"), str) else ""
    label = push_mod.clean_label(data.get("label"), request.headers.get("User-Agent", ""))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if kind == "web":
            if push_mod.vapid_keys() is None:
                return jsonify({"error": "Browser notifications are off on this "
                                         "server."}), 409
            address, p256dh, auth = push_mod.clean_subscription(data.get("subscription"))
            extra = {"p256dh": p256dh, "auth": auth}
        elif kind == "apns":
            if not push_mod.apns_ready():
                return jsonify({"error": "Notifications to the iPhone app are not "
                                         "switched on here."}), 409
            address = push_mod.clean_token(data.get("token"))
            old = old.strip().lower()
            environment = data.get("environment")
            # Looked up only if it is text: a list or an object cannot be.
            extra = {"environment": environment if isinstance(environment, str)
                     and environment in push_mod.APNS_HOSTS else ""}
        else:
            return jsonify({"error": "kind must be web or apns."}), 400
        if (check or old) and not push_mod.owns(conn, uid(), kind, address) \
                and not (old and push_mod.owns(conn, uid(), kind, old)):
            return jsonify({"ok": True, "known": False})
        device = push_mod.register(conn, uid(), kind, address, label=label,
                                   session_ref=_session_ref(), **extra)
        if old and old != address:
            push_mod.forget(conn, uid(), kind, old)
    except push_mod.PushError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "known": True, "id": device})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    """Stop sending to the device named.

    Only this person's, unless "release" is set: then whoever had it, for a
    browser or phone where someone else is signed in now. Only the device
    itself knows its address, so naming it is proof enough.
    """
    refused = _push_post_refused()
    if refused is not None:
        return refused
    if _rate_limited("push", PUSH_CHANGES_PER_HOUR):
        return jsonify({"error": "Notifications were switched on and off a lot "
                                 "in the last hour. Wait a while, then try "
                                 "again."}), 429
    data = request.get_json(silent=True)
    kind, address = push_mod.address_of(data)
    if not kind:
        return jsonify({"error": "Name a web endpoint or an iPhone token."}), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if data.get("release") is True:
            removed = push_mod.release(conn, kind, address)
        else:
            removed = push_mod.forget(conn, uid(), kind, address)
    finally:
        conn.close()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/push/test", methods=["POST"])
def push_test():
    """Send "Notifications are on" to this person's devices, or to the one named.

    Sent while the request waits, so the page can say whether it went.
    """
    refused = _push_post_refused()
    if refused is not None:
        return refused
    if _rate_limited("push_test", PUSH_TESTS_PER_HOUR):
        return jsonify({"error": "Several tests were sent in the last hour. Wait "
                                 "a while, then try again."}), 429
    kind, address = push_mod.address_of(request.get_json(silent=True))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        # The configured address, as the alert loop uses, never one read
        # from this request's Host header.
        result = push_mod.send_test(conn, uid(), _public_base_offline(), kind, address)
    finally:
        conn.close()
    if not result["devices"]:
        return jsonify({"ok": False, "sent": 0, "failed": 0,
                        "error": "Notifications are not on for this device."}), 404
    return jsonify({"ok": result["sent"] > 0, "sent": result["sent"],
                    "failed": result["failed"] + result["dropped"],
                    "errors": [e["error"] for e in result["errors"]][:5]})


@app.route("/settings/notifications/remove", methods=["POST"])
def push_remove():
    """Stop notifications to one of this person's devices, from any device.

    For a phone that was lost or sold, which cannot be switched off from
    itself any more.
    """
    if g.demo:
        abort(404)
    try:
        device = int(request.form.get("device_id") or "")
    except ValueError:
        return render_template("error.html", symbol="",
                               message="No device was named."), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        push_mod.forget_ids(conn, uid(), [device])
    finally:
        conn.close()
    return redirect(url_for("settings") + "#notifications")


@app.route("/watch/<action>", methods=["POST"])
def watch(action):
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        symbol = request.form["symbol"]
        if action == "add":
            # The ticker an analysis would use, so the watchlist routine and
            # an alert on it look at the same instrument. With exact=1 it is
            # Yahoo's own, and kept as that: LINK for Interlink Electronics.
            _, exact = _symbol_from(request.form)
            db_mod.watch_add(conn, market_mod.normalise_symbol(symbol, exact=exact),
                             request.form.get("interval") or "1d",
                             request.form.get("note", ""), user=uid(), exact=exact)
            events_mod.bump(conn, "watch_add")
        elif action == "remove":
            # Exactly as stored, so an entry saved before tickers were
            # tidied up can still be removed.
            db_mod.watch_remove(conn, symbol, user=uid())
        else:
            return render_template("error.html", message="Unknown action.",
                                   symbol=""), 404
    except (KeyError, DataError):
        return render_template("error.html", message="No instrument given.",
                               symbol=""), 400
    except ValueError as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(_safe_back(request.form.get("back"), url_for("monitor")))


@app.route("/workflow/<name>", methods=["POST"])
def run_workflow(name):
    if name not in workflows_mod.WORKFLOWS:
        return render_template("error.html", message="No routine called %r." % name,
                               symbol=""), 404
    cfg = _cfg()
    # Alerts the way the loop checks them: a call worked out, the fire kept
    # in the person's history and emailed if they asked. Crossed stops are
    # left to the routine, which reports them in its own words.
    report = workflows_mod.run(
        name, cfg, app.config.get("DB_PATH"), user=uid(),
        check_alerts=lambda conn, user: _check_and_tell(conn, user=user, stops=False))
    # Kept per person. One shared slot showed whoever ran a routine last to
    # everybody's Monitor page, visitors included.
    app.config.setdefault("LAST_WORKFLOW_BY_USER", {})[uid()] = report
    # A workflow can change what every analysis would say, so drop the caches.
    with _CACHE_LOCK:
        _CACHE.clear()
        _RESEARCH_CACHE.clear()
    return redirect(url_for("monitor"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """View and edit the settings that drive every recommendation."""
    cfg_path = app.config.get("CFG_PATH")
    saved = error = None
    # On a deployment, settings are each person's own; the shared file is only
    # written for the server's own settings, and only by the operator.
    personal = not g.user.is_local
    operator = _is_operator()

    # A POST, not a link: restoring defaults used to be /settings?reset=1, so
    # anyone who opened that address, or any crawler that followed it, wiped
    # the settings.
    if request.method == "POST" and request.form.get("reset") == "1":
        try:
            if personal:
                conn = db_mod.connect(app.config.get("DB_PATH"))
                try:
                    accounts_mod.clear_settings(conn, g.user.id)
                finally:
                    conn.close()
            else:
                fresh = json.loads(json.dumps(config_mod.DEFAULTS))
                config_mod.save(fresh, cfg_path)
                _CACHE.clear()
                _RESEARCH_CACHE.clear()
            return redirect(url_for("settings", saved="1"))
        except Exception as exc:
            error = _plainly(exc, "The settings could not be put back just now. "
                                  "Try again in a minute.")

    if request.method == "POST" and request.form.get("reset") != "1":
        cfg = _cfg()
        numbers = ("account_size", "risk_per_trade_pct", "max_position_pct",
                   "cost_bps_equity", "cost_bps_crypto", "stop_atr_multiple",
                   "target_atr_multiple", "min_conviction", "strong_conviction",
                   "alert_check_minutes", "cost_bps_futures", "daily_loss_limit_r",
                   "max_trades_per_day")
        try:
            for key in numbers:
                if request.form.get(key) not in (None, ""):
                    cfg[key] = float(request.form[key])
            # The journal's daily rules: an empty time switches that rule off,
            # and the three are checked here, since a person's own settings
            # are not otherwise checked before they are saved.
            if request.form.get("last_entry_time") is not None:
                cfg["last_entry_time"] = request.form["last_entry_time"].strip()
            rules = ("cost_bps_futures", "daily_loss_limit_r", "max_trades_per_day",
                     "last_entry_time")
            config_mod.validate(dict(config_mod.DEFAULTS, **{k: cfg[k] for k in rules}))
            cfg["max_trades_per_day"] = int(cfg["max_trades_per_day"])
            raw_horizon = (request.form.get("horizon_bars") or "").strip()
            if raw_horizon:
                cfg["horizon_bars"] = ("auto" if raw_horizon.lower() == "auto"
                                       else int(float(raw_horizon)))
            if request.form.get("interval"):
                cfg["interval"] = request.form["interval"]
            for flag in ("allow_shorts", "require_positive_expectancy",
                         "use_llm_sentiment", "calm_candles"):
                if request.form.get(flag) is not None:
                    cfg[flag] = request.form[flag] == "1"

            if personal:
                conn = db_mod.connect(app.config.get("DB_PATH"))
                try:
                    accounts_mod.save_settings(conn, g.user.id, cfg)
                finally:
                    conn.close()
            if not personal or operator:
                # The server's own settings: the whole file on your own
                # machine, only the server-level keys for an operator online.
                shared = config_mod.load(cfg_path)
                if personal:
                    for key in ("alert_check_minutes", "use_llm_sentiment"):
                        shared[key] = cfg[key]
                else:
                    shared = cfg
                config_mod.save(shared, cfg_path)
                # Cached analyses were produced under the old settings.
                _CACHE.clear()
                _RESEARCH_CACHE.clear()
            if personal and not operator:
                return redirect(url_for("settings", saved="1"))
            # Apply a changed check interval now rather than at next start,
            # including switching checks back on after they were off.
            minutes = cfg.get("alert_check_minutes", 5)
            if not minutes:
                if ALERTS is not None:
                    ALERTS.stop()
            elif ALERTS is None:
                start_alert_loop(cfg)
            else:
                ALERTS.set_interval(float(minutes) * 60.0)
                if not ALERTS.running:
                    ALERTS.start()
            return redirect(url_for("settings", saved="1"))
        except ValueError:
            error = "Every numeric field needs a number."
        except RuntimeError as exc:
            error = str(exc)

    cfg = _cfg()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        export_counts = export_mod.counts(conn, user=uid())
        # Every device this person gets notifications on, so a lost phone
        # can be switched off from another one.
        push_devices = [] if g.demo else push_mod.devices(conn, uid())
    finally:
        conn.close()
    return render_template("settings.html", cfg=cfg, push_devices=push_devices,
                           personal=personal, operator=operator,
                           datasets=export_mod.DATASETS,
                           export_counts=export_counts,
                           scheduler=(ALERTS.status() if ALERTS else None),
                           saved=saved or request.args.get("saved") == "1",
                           error=error,
                           has_key=bool(os.environ.get("ANTHROPIC_API_KEY")),
                           ai_since=_ai_consent_since())


def _ai_consent_since() -> int:
    """When a signed-in person said yes to screenshots going to Anthropic, or 0.
    Only an account of one's own has a yes to show or take back."""
    user = getattr(g, "user", None)
    if user is None or getattr(g, "demo", False) or user.is_local:
        return 0
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return accounts_mod.ai_consent(conn, uid())
    finally:
        conn.close()


@app.route("/api/search")
def api_search():
    """Suggestions for a search box, from what has been typed so far."""
    return jsonify(market_mod.search((request.args.get("q") or "")[:40]))


@app.route("/api/analyse")
def api_analyse():
    """JSON, for anything you want to build on top."""
    symbol, exact = _symbol_from(request.args)
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    interval = request.args.get("interval") or "1d"
    # The same allowance as the pages, or the API is a way round it.
    if not _allowance_left(symbol, interval, exact):
        return jsonify({"error": "The free daily allowance is used up."}), 402
    cfg = _cfg()
    try:
        result = cached_analysis(symbol, interval, cfg,
                                 request.args.get("news", "1") != "0", exact=exact)
    except DataError as exc:
        wait = _source_wait(exc)
        if wait is not None:
            answer = jsonify({"error": str(exc), "retry_after": wait})
            answer.status_code = 503
            answer.headers["Retry-After"] = str(wait)
            return answer
        return jsonify({"error": str(exc)}), 404
    _spend_view(symbol, interval, exact)

    from bot import report as report_mod
    import json as _json
    return app.response_class(
        report_mod.to_json(result.bars, result.plan, result.signals,
                           result.composite, result.regime, result.calibration,
                           result.sentiment),
        mimetype="application/json")


# ---------------------------------------------------------------------------
# Pages anyone can read, sharing, invites and the weekly email
# ---------------------------------------------------------------------------

def _png(data: bytes, max_age: int):
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=%d" % max_age})


@app.route("/stocks")
def stocks_index():
    """Every popular stock and coin with today's call, for people and search."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = public_mod.verdicts(conn)
    finally:
        conn.close()
    return render_template("stocks.html", rows=rows, short=public_mod.short,
                           slug=public_mod.slug)


@app.route("/stock/<sym>")
def stock_page(sym):
    """One popular instrument's call, refreshed daily in the background."""
    symbol = public_mod.from_slug(sym)
    if symbol is None:
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        snap = public_mod.verdict(conn, symbol)
        others = public_mod.related(symbol, public_mod.verdicts(conn))
        history = public_mod.symbol_record(conn, symbol)
    finally:
        conn.close()
    base = _public_base()
    page = base + url_for("stock_page", sym=public_mod.slug(symbol))
    card = base + url_for("stock_card", sym=public_mod.slug(symbol))
    return render_template(
        "stock.html", snap=snap, symbol=symbol, name=public_mod.NAMES[symbol],
        short=public_mod.short, slug=public_mod.slug, counts=public_mod.counts_line,
        others=others, page_url=page, card_url=card, frozen=False,
        tested=public_mod.symbol_results(snap), label_of=public_mod.strategy_label,
        strategy_slug=public_mod.strategy_slug, history=history,
        how_words=public_mod.HOW_WORDS,
        **({"preview_image": card} if snap else {}))


def _house_method() -> dict:
    """The settings every public back-test runs on, said on the page."""
    cfg = config_mod.load(app.config.get("CFG_PATH"))
    return {"stop": cfg["stop_atr_multiple"], "target": cfg["target_atr_multiple"],
            "min": public_mod.MIN_TRADES, "stride": int(cfg["calibration_stride"]),
            "days": config_mod.horizon_for("1d", cfg),
            "equity": cfg["cost_bps_equity"] / 100.0,
            "crypto": cfg["cost_bps_crypto"] / 100.0}


@app.route("/backtests")
def backtests():
    """Every strategy that fires on daily bars, back-tested on the popular list."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = public_mod.verdicts(conn)
    finally:
        conn.close()
    table = [dict(public_mod.strategy_summary(public_mod.strategy_results(rows, s.name)),
                  strategy=s.name, family=s.family)
             for s in strategies_mod.REGISTRY]
    table = [t for t in table if t["n"]]
    tested = len(table)
    # Buying and shorting on every tested day rank among them, so each
    # strategy reads against what the market's own drift paid its side, not
    # against zero.
    for key, side in (("baseline", "buy"), ("baseline_short", "short")):
        base = public_mod.strategy_summary(public_mod.baseline_results(rows, key))
        if table and base["n"]:
            table.append(dict(base, strategy="", family="", baseline=side))
    table = sorted(table, key=lambda t: -t["kept"])
    return render_template("backtests.html", table=table, tested=tested,
                           method=_house_method(),
                           label_of=public_mod.strategy_label,
                           strategy_slug=public_mod.strategy_slug)


@app.route("/backtest/<name>")
def backtest_page(name):
    """Does one strategy work? Its record on every popular instrument."""
    strategy = public_mod.strategy_from_slug(name)
    if strategy is None:
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = public_mod.verdicts(conn)
    finally:
        conn.close()
    results = public_mod.strategy_results(rows, strategy)
    info = strategies_mod.BY_NAME[strategy]
    kin = [s.name for s in strategies_mod.REGISTRY
           if s.family == info.family and s.name != strategy]
    return render_template(
        "backtest.html", strategy=strategy, info=info,
        label=public_mod.strategy_label(strategy), results=results,
        intraday=strategy in strategies_mod.INTRADAY_ONLY,
        summary=public_mod.strategy_summary(results), kin=kin,
        method=_house_method(), label_of=public_mod.strategy_label,
        strategy_slug=public_mod.strategy_slug, short=public_mod.short,
        slug=public_mod.slug)


@app.route("/record")
def record():
    """How every past public call turned out, for anyone to check."""
    method = _house_method()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rec = public_mod.record(conn, costs={"equity": method["equity"] / 100.0,
                                             "crypto": method["crypto"] / 100.0})
        prints = public_mod.fingerprints(conn, limit=7)
    finally:
        conn.close()
    # Calls made before a stock split are scored on split-adjusted prices only
    # once the market data carries the split events, so the page says so
    # only then.
    splits = "splits" in {f.name for f in dataclasses.fields(market_mod.Bars)}
    return render_template("record.html", rec=rec, method=method,
                           popular_count=len(public_mod.POPULAR),
                           short=public_mod.short, slug=public_mod.slug,
                           how_words=public_mod.HOW_WORDS, splits=splits,
                           prints=prints)


@app.route("/record.csv")
def record_csv():
    """Every public call, left-out ones included, as a file anyone can check."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        body = public_mod.calls_csv(conn)
    finally:
        conn.close()
    return Response(body, mimetype="text/csv", headers={
        "Content-Disposition": 'attachment; filename="orenth-record.csv"',
        "Cache-Control": "public, max-age=900"})


@app.route("/record/fingerprints.csv")
def record_fingerprints():
    """Every day's fingerprint of the record, for anyone to keep a copy of."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        body = public_mod.fingerprints_csv(conn)
    finally:
        conn.close()
    return Response(body, mimetype="text/csv", headers={
        "Content-Disposition": 'attachment; filename="orenth-record-fingerprints.csv"',
        "Cache-Control": "public, max-age=900"})


# --- Learn: the price-action ideas from TJR's video, beside what was measured ---
#
# Every number on these pages comes from the Setup lab's store (bot/labstore.py)
# or the daily strategy test; bot/learn.py holds the words. Public, like the
# strategy tests, and kept out of search until something is measured.
from bot import learn as learn_mod  # noqa: E402
from bot import setups as setups_mod  # noqa: E402

PUBLIC_ENDPOINTS.update({"learn", "learn_topic", "learn_practice"})


def _registered_setups() -> list:
    """The setups that are strategies in this build: the daily test reads only those."""
    return [s.name for s in setups_mod.SETUPS if s.name in strategies_mod.BY_NAME]


def _learn_links() -> dict:
    """The journal and risk tools, linked from the Learn pages where this build has them."""
    return {key: url_for(endpoint) for key, endpoint in (
        ("size", "size_tool"), ("survival", "survival_tool"),
        ("breakdown", "journal_breakdown"), ("trades", "trades"))
        if endpoint in app.view_functions}


@app.route("/learn")
def learn():
    """Price action, tested: the course's ideas, the claims, and what was measured."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        page = learn_mod.hub(conn, _registered_setups())
    finally:
        conn.close()
    return render_template("learn.html", page=page, links=_learn_links(),
                           strategy_slug=public_mod.strategy_slug)


@app.route("/learn/<slug>")
def learn_topic(slug):
    """One idea: what it is, what the course teaches, Orenth's exact rule, the result."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        page = learn_mod.topic(conn, slug, _registered_setups())
    finally:
        conn.close()
    if page is None:
        abort(404)
    return render_template("learn_topic.html", page=page, links=_learn_links(),
                           strategy_slug=public_mod.strategy_slug)


@app.route("/learn/practice", methods=["GET", "POST"])
def learn_practice():
    """Call the next move on a past chart. The tally lives in this browser's
    session cookie only: nothing is written to the database."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        items = learn_mod.practice_items(conn)
    finally:
        conn.close()
    tally = session.get("practice_tally") or {"n": 0, "right": 0}
    if request.method == "POST":
        if not _from_this_site():
            abort(403)
        if request.form.get("reset"):
            for key in ("practice_tally", "practice_last", "practice_done"):
                session.pop(key, None)
            return redirect(url_for("learn_practice"), code=303)
        k = request.form.get("item", type=int)
        pick = request.form.get("pick", "")
        if not items or k is None or not 0 <= k < len(items) \
                or pick not in dict(learn_mod.PRACTICE_PICKS):
            abort(400)
        answer = learn_mod.practice_answer(items[k])
        done = list(session.get("practice_done") or [])
        if k not in done:
            # Answering the same chart again shows it again but is not counted.
            tally = {"n": int(tally.get("n", 0)) + 1,
                     "right": int(tally.get("right", 0)) + (pick == answer)}
            session["practice_tally"] = tally
            session["practice_done"] = (done + [k])[-200:]
        session["practice_last"] = {"item": k, "pick": pick, "answer": answer}
        return redirect(url_for("learn_practice", shown=k), code=303)

    item = reveal = None
    k = 0
    if items:
        shown = request.args.get("shown", type=int)
        last = session.get("practice_last") or {}
        if shown is not None and last.get("item") == shown and 0 <= shown < len(items):
            k, reveal = shown, last
        else:
            k = (request.args.get("n", type=int) or 0) % len(items)
        item = items[k]
    chart = learn_mod.practice_chart(item, bool(reveal)) if item else ""
    return render_template(
        "learn_practice.html", item=item, k=k, count=len(items), reveal=reveal,
        chart=chart, tally=tally, picks=learn_mod.PRACTICE_PICKS,
        ahead=learn_mod.PRACTICE_AHEAD,
        next_k=(k + 1) % len(items) if items else 0,
        name=learn_mod.symbol_name(item["symbol"]) if item else "",
        setup_label=(setups_mod.BY_NAME[item["setup"]].label
                     if item and item["setup"] in setups_mod.BY_NAME else ""),
        setup_topic=(setups_mod.BY_NAME[item["setup"]].learn
                     if item and item["setup"] in setups_mod.BY_NAME else ""),
        interval_words=learn_mod.interval_words(item["interval"]) if item else "")


def _lab_box_data(strategy):
    """The Setup lab's result for a strategy's test page (_lab_box.html), or
    None. A failure here must never take the strategy page down with it."""
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            return learn_mod.lab_box(conn, strategy)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        app.logger.exception("lab box for %s", strategy)
        return None


app.jinja_env.globals["lab_box_data"] = _lab_box_data


@app.route("/stock/<sym>/card.png")
def stock_card(sym):
    symbol = public_mod.from_slug(sym)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        snap = public_mod.verdict(conn, symbol) if symbol else None
    finally:
        conn.close()
    if snap is None:
        abort(404)
    return _png(public_mod.render_card(snap), 3600)


@app.route("/share", methods=["POST"])
def share():
    """Freeze what this person is looking at into a page they can pass on."""
    symbol, exact = _symbol_from(request.form)
    symbol = symbol.upper()[:20]
    interval = request.form.get("interval") or "1d"
    if not symbol or interval not in dict(INTERVALS):
        abort(400)
    if not _allowance_left(symbol, interval, exact):
        return _allowance_used()
    try:
        result = cached_analysis(symbol, interval, _cfg(), record=False, exact=exact)
    except DataError as exc:
        return _no_prices(exc, symbol, 404)
    _spend_view(symbol, interval, exact)
    snap = public_mod.snapshot(result, plain_mod.explain_plan(result.plan,
                                                              result.bars))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        sid = public_mod.create_share(conn, uid(), snap)
    except public_mod.ShareError as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 429
    finally:
        conn.close()
    return redirect(url_for("share_page", sid=sid), code=303)


@app.route("/s/<sid>")
def share_page(sid):
    """A shared call, exactly as it was when shared."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        snap = public_mod.get_share(conn, sid)
    finally:
        conn.close()
    if snap is None:
        abort(404)
    base = _public_base()
    card = base + url_for("share_card", sid=sid)
    symbol = snap["symbol"]
    return render_template(
        "stock.html", snap=snap, symbol=symbol,
        name=snap.get("name") or symbol, short=public_mod.short,
        slug=public_mod.slug, counts=public_mod.counts_line, others=[],
        page_url=base + url_for("share_page", sid=sid), card_url=card,
        frozen=True, preview_image=card,
        today_url=(url_for("stock_page", sym=public_mod.slug(symbol))
                   if symbol in public_mod.NAMES else ""))


@app.route("/s/<sid>.png")
def share_card(sid):
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        snap = public_mod.get_share(conn, sid)
    finally:
        conn.close()
    if snap is None:
        abort(404)
    # Frozen, so it can be cached for a long time.
    return _png(public_mod.render_card(snap), 86400 * 7)


@app.route("/join/<code>")
def join(code):
    """A friend's invite link. Remembered until this browser signs up."""
    if getattr(g, "user", None) is not None:
        return redirect(url_for("index"))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        known = invites_mod.inviter(conn, code) is not None
    finally:
        conn.close()
    if known:
        session["ref"] = code.strip().lower()
    return redirect(url_for("index"))


@app.route("/account/weekly-email", methods=["POST"])
def weekly_email():
    if g.demo or not digest_mod.can_receive(g.user):
        abort(404)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        digest_mod.set_wanted(conn, uid(), request.form.get("on") == "1")
    finally:
        conn.close()
    return redirect(url_for("account") + "#weekly")


@app.route("/email/stop", methods=["GET", "POST"])
def email_stop():
    """Stop the weekly email from a link in it.

    A GET only asks, because mail scanners open every link in a message and
    would otherwise unsubscribe people who never clicked. A POST stops it,
    which is also what an inbox's own unsubscribe button sends.
    """
    try:
        user_id = int(request.values.get("u") or 0)
    except ValueError:
        user_id = 0
    if not user_id or not digest_mod.check_token(app.secret_key, user_id,
                                                 request.values.get("t", "")):
        abort(404)
    done = False
    if request.method == "POST":
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            digest_mod.set_wanted(conn, user_id, False)
        finally:
            conn.close()
        done = True
    return render_template("email_stop.html", done=done, u=user_id,
                           t=request.values.get("t", ""))


def _public_snapshot(symbol: str, indexes: Optional[dict] = None,
                     exact: bool = False) -> dict:
    """One popular instrument, analysed with the shared settings.

    indexes holds the index bars a background run has fetched, by symbol,
    for setting the strategy tests beside holding the index; without it the
    snapshot leaves that comparison out, and an index the run could not
    fetch (None) keeps the one stored at the last check. The weekly email
    uses it for watched symbols too, with exact for an entry saved as
    Yahoo's own symbol (LINK for Interlink Electronics).
    """
    cfg = config_mod.load(app.config.get("CFG_PATH"))
    result = engine.analyse(symbol, cfg, interval="1d", with_news=False,
                            with_learning=True, record=False,
                            db_path=app.config.get("DB_PATH"), exact=exact)
    horizon = config_mod.horizon_for("1d", cfg)
    stop, target = cfg["stop_atr_multiple"], cfg["target_atr_multiple"]
    stride = cfg["calibration_stride"]
    snap = public_mod.snapshot(
        result, plain_mod.explain_plan(result.plan, result.bars),
        stop_atr=stop, target_atr=target, horizon=horizon)
    snap["tests"] = public_mod.tests(result.strategy_report)
    # Kept apart from the tests, whose three figures the previous version
    # reads: rolled back to, it still opens the strategy pages. So are the
    # sides, shorting every day and the index below, which it never reads.
    snap["spread"] = public_mod.spreads(result.strategy_report)
    snap["sides"] = public_mod.sides(result.strategy_report)
    # The same pretend trade on every day the strategies were tested on,
    # both ways, so the strategy pages can set each one against its side.
    snap["baseline"] = public_mod.buy_every_day(
        result.context, stop, target, horizon, stride, result.cost_bps)
    snap["baseline_short"] = public_mod.short_every_day(
        result.context, stop, target, horizon, stride, result.cost_bps)
    index = public_mod.index_symbol(symbol)
    held = None
    if indexes and indexes.get(index) is not None:
        try:
            held = public_mod.hold_index(result.context, indexes[index], stop,
                                         horizon, stride, result.cost_bps)
        except Exception:        # a comparison only: never at the cost of the call
            app.logger.warning("Could not set %s's strategy tests beside %s",
                               symbol, index, exc_info=True)
    snap["index"] = {"symbol": index, "hold": held} if held else None
    if indexes is not None and indexes.get(index) is None:
        # The run could not fetch the index. The comparison kept from the last
        # check stands until one can: written as None, one failed download
        # took holding the index off every strategy page for six hours.
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            snap["index"] = (public_mod.verdict(conn, symbol) or {}).get("index")
        finally:
            conn.close()
    return snap


def _index_bars(symbol: str, fetched: dict) -> dict:
    """Make sure fetched holds the daily bars of the index this symbol's
    strategy tests are set beside (public.index_symbol), and return it. Each
    index is fetched once per background run; one the source would not give
    is None for the rest of the run."""
    index = public_mod.index_symbol(symbol)
    if index not in fetched:
        try:
            fetched[index] = market_mod.fetch_bars(index, "1d")
        except Exception:                    # the pages go without it until the next run
            app.logger.warning("Could not fetch %s to set the strategy tests beside",
                               index, exc_info=True)
            fetched[index] = None
    return fetched


def _digest_look_up(symbol: str, exact: bool = False):
    """A symbol's call for the weekly email: today's public one if there is
    one, otherwise a fresh analysis. exact is for a watched listing whose
    ticker is also a coin's, which is never one of the public ones."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        stored = public_mod.verdict(conn, symbol)
    finally:
        conn.close()
    if stored and time.time() - stored["at"] < 2 * 86400:
        return stored
    return _public_snapshot(symbol, exact=exact)


def _watch_check(symbol: str, with_call: bool) -> dict:
    """A watched instrument's daily call and price, for "since yesterday".

    A popular instrument's call is its public daily check, so nothing more is
    analysed for it. Any other is analysed the way that check is, on daily
    bars with the standard settings, recorded in nobody's history and costing
    nobody's look, and only when with_call; otherwise only its price is read.
    """
    action, bars = None, None
    # Already Yahoo's own symbol: digest._first resolved each watchlist entry
    # to what it opens, so LINK here is Interlink Electronics and the coin
    # arrives as LINK-USD. Read as typed text it would turn into the coin.
    ticker = market_mod.normalise_symbol(symbol, exact=True)
    if with_call and ticker in public_mod.NAMES:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            snap = public_mod.verdict(conn, ticker)
        finally:
            conn.close()
        if snap and time.time() - snap["at"] < public_mod.MAX_AGE + 3600:
            action = snap.get("action") or None
    elif with_call:
        cfg = config_mod.load(app.config.get("CFG_PATH"))
        # No calendar: it adds warnings to a plan, never changes its call.
        result = engine.analyse(ticker, cfg, interval="1d", with_news=False,
                                with_learning=True, record=False,
                                db_path=app.config.get("DB_PATH"),
                                with_calendar=False, exact=True)
        action, bars = result.plan.action, result.bars
    if bars is None:
        bars = market_mod.fetch_bars(ticker, "1d", lookback=alerts_mod.LOOKBACK["1d"])
    return digest_mod.reading(bars, action)


def _full_plan():
    """full(person): whether the free plan's limits leave this account alone.

    The pages' own test (_pro), for a background job with no page: Pro, an
    invite week, the local account and the operator, or anyone while billing
    is off.
    """
    admins = {e.strip().lower()
              for e in (os.environ.get("STOCKBOT_ADMIN_EMAIL") or "").split(",")
              if e.strip()}
    return lambda person: billing_mod.is_pro(person,
                                             (person.email or "").lower() in admins)


def _refresh_popular_job():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    # SPY and Bitcoin, fetched once for the whole run rather than once per
    # stock or coin set beside them.
    indexes: dict = {}
    try:
        # The bars were fetched moments ago for the check, so scoring the
        # earlier calls reads them from the cache rather than asking again.
        done = public_mod.refresh(
            conn, lambda s: _public_snapshot(s, _index_bars(s, indexes)),
            bars=lambda s: market_mod.fetch_bars(s, "1d"))
        # Then what people watch, after the popular checks it reuses. Its own
        # failure must not undo the popular run's.
        try:
            watch = digest_mod.check_watched(conn, _watch_check, _full_plan())
        except Exception as exc:
            app.logger.exception("Could not check the watched instruments")
            watch = {"checked": 0, "errors": ["watched instruments: %s" % exc]}
        return {"checked": done["checked"] + watch["checked"],
                "errors": list(done["errors"]) + list(watch["errors"])}
    finally:
        conn.close()


def _weekly_email_job():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return digest_mod.run(conn, app.secret_key, _public_base_offline(),
                              _digest_look_up, full=_full_plan())
    finally:
        conn.close()


def _lab_job():
    """The Setup lab and the video's claims, once a day after New York's 17:00.

    Checked every half hour; it measures only when due (setuplab.due), on the
    house settings every public test runs on, and pauses between downloads.
    """
    from bot import setuplab as setuplab_mod
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        if not setuplab_mod.due(conn):
            return {"checked": 0, "skipped": 1}
        summary = setuplab_mod.run(conn, cfg=config_mod.load(app.config.get("CFG_PATH")))
        return {"checked": summary["checked"], "errors": summary["errors"]}
    finally:
        conn.close()


def _public_base_offline() -> str:
    """The public address outside a request, where request.url_root is not."""
    return ((os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip().rstrip("/")
            or "http://127.0.0.1:8000")


BACKGROUND = {}


def start_background_jobs():
    """The daily popular checks and the Monday email. Started by serve.py."""
    if BACKGROUND:
        return BACKGROUND
    BACKGROUND["popular"] = scheduler_mod.Scheduler(
        _refresh_popular_job, interval_seconds=3600, name="orenth-popular")
    BACKGROUND["weekly"] = scheduler_mod.Scheduler(
        _weekly_email_job, interval_seconds=1800, name="orenth-weekly-email")
    BACKGROUND["plans"] = scheduler_mod.Scheduler(
        _recheck_plans_job, interval_seconds=3600, name="orenth-plans")
    BACKGROUND["lab"] = scheduler_mod.Scheduler(_lab_job, interval_seconds=1800, name="orenth-lab")
    for job in BACKGROUND.values():
        job.start()
    # The first popular run soon after start rather than an hour later, but
    # not at the same moment as everything else waking up.
    soon = threading.Timer(120, BACKGROUND["popular"].check_now)
    soon.daemon = True
    soon.start()
    # Plans that lapsed while the server was down are read again within a
    # minute, before anyone notices their Pro has gone.
    plans = threading.Timer(60, BACKGROUND["plans"].check_now)
    plans.daemon = True
    plans.start()
    # The Setup lab looks ten minutes after start, so a lab that never ran,
    # or missed its evening while the server was down, does not wait half an
    # hour; it measures only when due.
    lab = threading.Timer(600, BACKGROUND["lab"].check_now)
    lab.daemon = True
    lab.start()
    return BACKGROUND


def _recheck_plans_job():
    """Read lapsing Pro plans from Stripe, in case a renewal's webhook was lost.

    And forget App Store notifications too old for Apple to be retrying, and
    Apple subscriptions no account owns (a deleted account's) once their time
    has run out.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        result = billing_mod.recheck(conn)
        try:
            apple_store_mod.prune_notifications(conn)
            apple_store_mod.prune_unowned(conn)
        except sqlite3.Error as exc:
            result.setdefault("errors", []).append("app store records: %s" % exc)
        return result
    finally:
        conn.close()


@app.errorhandler(404)
def not_found(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "No such address."}), 404
    return render_template("notfound.html"), 404


@app.errorhandler(413)
def too_large(_exc):
    # Typed fields have their own, much smaller, limit, and a padded note is
    # not an image.
    if request.endpoint != "analyse_image":
        return render_template("error.html", symbol="",
                               message="That is more text than this form takes. "
                                       "Shorten it and try again."), 413
    return render_template("error.html", symbol="",
                           message="That image is larger than 12 MB. A normal "
                                   "screenshot is well under that, so try "
                                   "cropping it to just the chart."), 413


@app.errorhandler(500)
def server_error(_exc):
    # Flask has already written the exception to the server log by the time
    # this runs. The visitor gets a plain sentence instead: an exception's text
    # can carry paths, queries and internal names, and helps nobody reading it.
    return render_template("error.html", symbol="",
                           message="Something broke on our side. Try again "
                                   "in a minute."), 500


# One scheduler for the process. Created on start, not at import, so importing
# web.py in a test or a script never spawns a thread.
ALERTS = None


# Today's call for a call alert, kept for half an hour per instrument so a
# loop every five minutes does not run a full analysis every five minutes.
CALL_TTL = 30 * 60.0
_CALLS: Dict = {}
_CALLS_LOCK = threading.Lock()


def _call_now(symbol: str, interval: str = "1d",
              now: Optional[float] = None,
              exact: bool = False) -> Optional[str]:
    """BUY, WAIT, AVOID or SHORT for an instrument, with the standard settings.

    A popular symbol's daily call is read from its public check when that is
    recent; anything else is analysed once and kept for CALL_TTL. Neither is
    used once the market's last close has settled since it was worked out:
    it read a day that was not over, and the loop's first check after a close
    is the one a change of call must be read on, since the loop then waits
    for the open. `now` is the present unless given. exact is for an alert
    saved for Yahoo's own symbol: LINK as Interlink Electronics.
    """
    now = time.time() if now is None else now
    key = (symbol.upper(), interval, bool(exact))

    def fresh(at: float, age: float) -> bool:
        return now - at < age and not sessions_mod.settled_between(symbol, at, now)

    with _CALLS_LOCK:
        hit = _CALLS.get(key)
        if hit and fresh(hit[0], CALL_TTL):
            return hit[1]
    action = None
    if interval == "1d" and public_mod.from_slug(symbol.lower()):
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            snap = public_mod.verdict(conn, symbol.upper())
        finally:
            conn.close()
        if snap and fresh(snap["at"], public_mod.MAX_AGE + 3600):
            action = snap.get("action") or None
    if action is None:
        cfg = config_mod.load(app.config.get("CFG_PATH"))
        result = engine.analyse(symbol, cfg, interval=interval, with_news=False,
                                with_learning=True, record=False,
                                db_path=app.config.get("DB_PATH"), exact=exact)
        action = (result.plan.action or "").upper() or None
    with _CALLS_LOCK:
        _CALLS[key] = (now, action)
        for old in [k for k, v in _CALLS.items() if now - v[0] > CALL_TTL]:
            _CALLS.pop(old, None)
    return action


def _alert_address(conn):
    """The address to email someone's alerts to, if they asked for that."""
    def address_of(user_id: int) -> Optional[str]:
        person = accounts_mod.get_user(conn, user_id)
        if person is None or not person.alert_email or not person.email \
                or person.email.endswith(".invalid") or person.is_local:
            return None
        return person.email
    return address_of


def _alert_limits(conn):
    """limit_of(user_id) for alerts.check, or None while billing is off.

    The free plan's limit is checked when an alert is switched on, but a paid
    plan can lapse with many running. This is how many each person's plan
    checks now, or None for all of them.
    """
    if not billing_mod.ready():
        return None
    cap = billing_mod.free_limits()["alerts"]
    admins = {e.strip().lower()
              for e in (os.environ.get("STOCKBOT_ADMIN_EMAIL") or "").split(",")
              if e.strip()}

    def limit_of(user_id: int) -> Optional[int]:
        person = accounts_mod.get_user(conn, user_id)
        if person is None:
            return None
        operator = (person.email or "").lower() in admins
        return None if billing_mod.is_pro(person, operator) else cap
    return limit_of


def _check_and_tell(conn, user: Optional[int] = None, stops: bool = True,
                    skip_closed: bool = False, now: Optional[float] = None) -> Dict:
    """Check alerts and open positions' stops, record and email what fired.

    stops=False leaves crossed stops to the caller: a routine reports those
    in its own words. skip_closed leaves what cannot have changed, a stock
    whose market has been closed since its last good check, for the open.
    """
    result = alerts_mod.check(conn, user=user, call_of=_call_now,
                              limit_of=_alert_limits(conn),
                              skip_closed=skip_closed, now=now)
    crossed = (alerts_mod.check_stops(conn, user=user, skip_closed=skip_closed, now=now)
               if stops else {"checked": 0, "fired": [], "errors": [], "skipped": 0})
    fired = list(result["fired"]) + list(crossed["fired"])
    if fired:
        # To the phones and browsers people switched on, from push_mod's own
        # thread: a push service that is slow or down must never hold up a
        # check or a page, and its failure is kept on each device instead.
        try:
            push_mod.deliver_later(app.config.get("DB_PATH"), fired,
                                   _public_base_offline())
        except Exception:
            app.logger.exception("Could not hand fired alerts to the notification sender")
    if fired and codes_mod.email_ready():
        alerts_mod.deliver(conn, fired, codes_mod.send_mail, _alert_address(conn))
    return {"checked": result["checked"] + crossed["checked"], "fired": fired,
            "errors": result["errors"] + crossed["errors"], "when": result["when"],
            "skipped": result.get("skipped", 0) + crossed.get("skipped", 0)}


def _check_alerts_job(now: Optional[float] = None):
    """What the background loop actually does, every interval.

    Unlike a check someone asks for, it leaves a stock whose market has been
    closed since its last good check until the market opens: nothing it reads
    can have changed. How many it left is counted, by the day and with
    nothing about whose they were, for the operator's usage page.
    """
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        result = _check_and_tell(conn, skip_closed=True, now=now)
        events_mod.bump(conn, "closed_skips", result["skipped"])
        return result
    finally:
        conn.close()


def start_alert_loop(cfg):
    """Begin background alert checking, unless it is switched off."""
    global ALERTS
    minutes = cfg.get("alert_check_minutes", 5)
    if not minutes:
        return None
    ALERTS = scheduler_mod.Scheduler(_check_alerts_job,
                                     interval_seconds=float(minutes) * 60.0,
                                     name="stockbot-alerts")
    ALERTS.start()
    return ALERTS


@app.route("/api/alerts/status")
def alert_status():
    """What has fired for the person asking, for the page to poll.

    Read from their alert history in the database, not from the loop's
    memory: a restart does not lose a fire, and a fire from "Check them all
    now" or a routine is announced like one from the loop. `after` is the id
    of the last fire a page has seen, and `cursor` in the reply is the one to
    send next time, so each is announced once. With no marker at all, a page
    is told only the last day's, rather than old news as if it were new.
    """
    me = uid()

    def number(name, kind):
        try:
            raw = request.args.get(name)
            return kind(raw) if raw not in (None, "") else None
        except ValueError:
            return None

    after, since = number("after", int), number("since", float)
    if after is None and not since:
        since = time.time() - 86400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        # The marker first: a fire recorded in between is then left for the
        # next poll, rather than counted as seen without being shown.
        cursor = db_mod.event_cursor(conn, me)
        rows = [r for r in db_mod.events_after(conn, me, after=after, since=since)
                if r["id"] <= cursor]
    finally:
        conn.close()

    if ALERTS is None:
        out = {"running": False, "enabled": False,
               "note": "Background checking is switched off in settings."}
    else:
        # The loop checks everybody's alerts in one pass, so its own list of
        # what fired is everybody's: that is for the operator only.
        full = ALERTS.status()
        out = {key: full[key] for key in ("running", "interval", "last_run", "next_run")}
        if _is_operator():
            out.update(full)
        out["enabled"] = True
    out["new_fires"] = [{"id": r["id"], "at": r["ts"], "kind": r["kind"],
                         "symbol": r["symbol"], "message": r["message"],
                         "as_of": r["as_of"]} for r in rows]
    out["cursor"] = cursor
    out["now"] = time.time()
    return jsonify(out)


@app.route("/api/alerts/check-now", methods=["POST"])
def alert_check_now():
    """Ask the background loop to run immediately."""
    if ALERTS is None:
        return jsonify({"ok": False,
                        "note": "Background checking is switched off."}), 409
    # It runs everybody's checks, so only the operator may start it early.
    # Anyone can check their own from the Monitor page.
    if not _is_operator():
        return jsonify({"ok": False, "note": "Check your own alerts from the "
                                             "Monitor page."}), 403
    ALERTS.check_now()
    return jsonify({"ok": True})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="web.py", description="Web interface for the bot.")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; 0.0.0.0 exposes it to your local network")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window on start")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--config", help="path to a config.json")
    p.add_argument("--db", help="path to the database file")
    args = p.parse_args(argv)

    app.config["CFG_PATH"] = args.config
    app.config["DB_PATH"] = args.db

    # Fail loudly when the port is taken. Otherwise a server left running from
    # an earlier session keeps answering, the new one dies quietly, and every
    # page you load is served by stale code.
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((args.host, args.port))
    except OSError:
        print()
        print("  Port %d is already in use." % args.port)
        print()
        print("  Orenth is probably still running in another window.")
        print("  Close that window, or start this one on a different port:")
        print()
        print("      Stock Bot.bat --port %d" % (args.port + 1))
        print()
        return 1
    finally:
        probe.close()

    url = "http://%s:%d" % ("127.0.0.1" if args.host == "0.0.0.0" else args.host,
                            args.port)
    print()
    print("  Orenth is running.")
    print()
    print("      %s" % url)
    print()
    if args.host == "0.0.0.0":
        print("  Also reachable from other devices on your network.")
        print()
    print("  Leave this window open while you use it. Press Ctrl+C to stop.")
    print()

    if not args.no_browser:
        # Give the server a moment to bind before the browser asks for a page.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # The reloader is off, so this runs once rather than once per process.
    started_cfg = config_mod.load(app.config.get("CFG_PATH"))
    loop = start_alert_loop(started_cfg)
    if loop is not None:
        print("  Checking alerts every %g minutes while this window is open."
              % started_cfg.get("alert_check_minutes", 5))
        print()

    try:
        app.run(host=args.host, port=args.port, debug=args.debug,
                use_reloader=False, threaded=True)
    finally:
        if loop is not None:
            loop.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        sys.exit(0)
