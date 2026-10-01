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
from datetime import timedelta
import hmac
import json
import os
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
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)

from bot import accounts as accounts_mod
from bot import alerts as alerts_mod
from bot import auth as auth_mod
from bot import billing as billing_mod
from bot import catalysts as catalysts_mod
from bot import codes as codes_mod
from bot import charts as charts_mod
from bot import config as config_mod
from bot import database as db_mod
from bot import demo as demo_mod
from bot import engine
from bot import ideas as ideas_mod
from bot import export as export_mod
from bot import market as market_mod
from bot import plain as plain_mod
from bot import portfolio as portfolio_mod
from bot import scheduler as scheduler_mod
from bot import screener as screener_mod
from bot import thesis as thesis_mod
from bot import usage as usage_mod
from bot import workflows as workflows_mod
from bot.market import DataError, format_price
from bot.strategies import FAMILIES
from bot.vision import VisionError, read_chart

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# Chart screenshots are small. This cap stops a huge upload from tying up the
# single-process dev server.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

# Signed cookies need a key that survives a restart, or everyone is signed out
# every time the server starts. bot/auth.py keeps one next to the database.
app.secret_key = auth_mod.session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=accounts_mod.SESSION_DAYS),
)

# Behind a reverse proxy the app sees plain http on an internal hostname, so
# url_for(_external=True) would build an http:// callback that Google refuses,
# because the redirect address has to match the registered one exactly. These
# headers are what the proxy uses to say what the browser actually asked for.
# Only enabled deliberately, since trusting them when there is no proxy in
# front would let a client spoof its own scheme and host.
_public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()
# An https public address is an operator saying there is a TLS terminator in
# front, which is the only way these headers get set at all.
if (os.environ.get("STOCKBOT_BEHIND_PROXY", "").strip().lower()
        in ("1", "true", "yes")) or _public.startswith("https://"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# A session cookie sent over plain http can be read in transit. On a public
# deployment that is unacceptable, so the flag is set whenever the app knows it
# is served over https; on localhost it stays off or the cookie never arrives.
if _public.startswith("https://"):
    app.config["SESSION_COOKIE_SECURE"] = True


@app.route("/healthz")
def healthcheck():
    """Liveness, for whatever is watching the process.

    Deliberately cheap and unauthenticated: it touches the database to prove
    the process is actually able to serve, and returns nothing about anyone.
    """
    try:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"ok": False, "detail": str(exc)[:200]}), 503
    return jsonify({"ok": True, "service": "stockbot"})


# Pages reachable without being signed in. Everything else needs an account
# once Google sign-in is configured.
PUBLIC_ENDPOINTS = {"signin", "google_start", "google_callback", "signout",
                    "static", "healthcheck", "privacy", "terms",
                    "robots", "sitemap", "index", "favicon",
                    "code_start", "code_verify", "pricing",
                    "billing_webhook"}

SESSION_KEY = "stockbot_session"

# Bumped by hand when the legal pages change, so the date on them is
# the date they were actually revised.
LEGAL_UPDATED = "30 September 2026"


def auth_required() -> bool:
    """Whether this installation asks people to sign in.

    Default is "auto": required exactly when Google credentials are present, so
    running it on your own machine needs no login and nothing to configure, and
    putting it on a server with credentials protects it without a second step.
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
            or codes_mod.phone_ready())


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
                  "trades", "monitor", "strategies", "settings"}


@app.before_request
def _identify():
    """Attach the signed-in person, or the local account, to this request."""
    g.user = None
    g.demo = False
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
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not signed in."}), 401
    # A visitor who tried to do something. A POST cannot be replayed as a GET
    # after signing in, so they are brought back to the page they were on.
    if request.method != "GET":
        return redirect(url_for("signin", next=_referrer_path()))
    return redirect(url_for("signin", next=request.full_path))


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


def _upgrade(reason: str):
    """The pricing page, saying why they arrived at it."""
    return render_template("pricing.html", reason=reason,
                           limits=billing_mod.free_limits(),
                           price=billing_mod.price_label(), is_pro=False,
                           has_customer=False), 402


def _allowance_left(symbol: str, interval: str) -> bool:
    """Whether a free account has a look left today for this instrument."""
    if g.demo or _pro():
        return True
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return billing_mod.may_view(conn, g.user.id,
                                    "%s|%s" % (symbol.upper(), interval))
    finally:
        conn.close()


def _spend_view(symbol: str, interval: str) -> None:
    """Counted once the analysis is on screen, so a mistyped ticker costs nothing."""
    if g.demo or _pro():
        return
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        billing_mod.count_view(conn, g.user.id, "%s|%s" % (symbol.upper(), interval))
    finally:
        conn.close()


def _allowance_used():
    return _upgrade("You have looked at %d different instruments today, which "
                    "is the free allowance. It resets at midnight UTC, or Pro "
                    "has no limit." % billing_mod.free_limits()["analyses_per_day"])


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
            "phone_ready": codes_mod.phone_ready(),
            "google_ready": auth_mod.configured()}


def uid() -> int:
    """The id every personal query is filtered by."""
    user = getattr(g, "user", None)
    if user is None:
        abort(401)
    return user.id

# An analysis takes several seconds. Re-rendering the same page, or hitting
# refresh, should not re-run the whole pipeline and re-hammer the data source.
_CACHE: Dict[Tuple[str, str], Tuple[float, engine.Analysis]] = {}
# Research costs several network round trips, so it gets its own cache.
_RESEARCH_CACHE: Dict[Tuple, Tuple[float, object]] = {}
_CACHE_TTL = 90.0
# How long the example account's analyses are reused. Long, because visitors
# looking around should cost the server one analysis per instrument per
# quarter hour, however many of them there are.
DEMO_TTL = 15 * 60.0
_CACHE_LOCK = threading.Lock()

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


def cached_analysis(symbol: str, interval: str, cfg: Dict,
                    with_news: bool = True, record: bool = True,
                    ttl: Optional[float] = None) -> engine.Analysis:
    # The settings are part of the key. Position sizes, stops and the call
    # itself depend on them, and without this one person's ?shorts=1 or larger
    # account was served to the next person who asked for the same ticker.
    key = (symbol.upper(), interval, bool(with_news), _settings_key(cfg))
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < (ttl or _CACHE_TTL):
            return hit[1]

    # The cache key is deliberately not per user: an analysis of AAPL is the
    # same analysis whoever asked for it, and recomputing it per person would
    # be pure waste. Only the record of having run it belongs to someone, and
    # that is written inside engine.analyse before the result is cached.
    result = engine.analyse(symbol, cfg, interval=interval, with_news=with_news,
                            source="web", db_path=app.config.get("DB_PATH"),
                            user=uid(), record=record)
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), result)
        # Keep the cache from growing without bound in a long session.
        if len(_CACHE) > 40:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
    return result


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.template_filter("price")
def _price(value):
    return format_price(value) if value is not None else "-"


@app.template_filter("money")
def _money(value):
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "-"


@app.template_filter("pct")
def _pct(value, digits=0):
    try:
        return "%.*f%%" % (digits, float(value) * 100)
    except (TypeError, ValueError):
        return "-"


@app.template_filter("signed")
def _signed(value, digits=2):
    try:
        return "%+.*f" % (digits, float(value))
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
        return render_template("landing.html", example=_example())

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        runs = db_mod.recent_runs(conn, user=uid(), limit=12)
        open_trades = db_mod.list_trades(conn, status="open", limit=20,
                                         user=uid())
        summary = db_mod.journal_summary(conn, user=uid())
        counts = db_mod.stats(conn, user=uid())
    finally:
        conn.close()
    return render_template("index.html", runs=runs, open_trades=open_trades,
                           summary=summary, counts=counts,
                           has_key=bool(os.environ.get("ANTHROPIC_API_KEY")))


def _safe_back(raw, fallback):
    """A redirect target from a form field, restricted to this app.

    A bare "//host" or "https://host" in a redirect is an open redirect, so only
    a single-slash relative path is accepted.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    return raw


@app.route("/analyse")
def analyse():
    symbol = (request.args.get("symbol") or "").strip()
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


    if not _allowance_left(symbol, interval):
        return _allowance_used()

    if g.demo:
        # Address overrides would give every visitor their own cache entry.
        cfg = config_mod.load(app.config.get("CFG_PATH"))
    try:
        result = cached_analysis(symbol, interval, cfg, with_news,
                                 record=not g.demo,
                                 ttl=DEMO_TTL if g.demo else None)
    except DataError as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 404
    except Exception as exc:                        # keep the app usable
        return render_template("error.html", message=str(exc), symbol=symbol), 500
    _spend_view(symbol, interval)

    families = _family_rollup(result)
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
                           plain=plain_mod.explain_plan(result.plan, result.bars,
                                                        hold_text))


def _family_rollup(result: engine.Analysis):
    """Aggregate signals per family so the page shows structure, not a list."""
    rows = []
    for family in FAMILIES:
        group = [s for s in result.signals
                 if s.family == family and s.weight > 0 and abs(s.score) > 0.05]
        total = sum(1 for s in result.signals if s.family == family)
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
        summary = db_mod.journal_summary(conn, user=uid())
        live = _mark_to_market(open_trades)
    finally:
        conn.close()
    return render_template("trades.html", open_trades=open_trades,
                           closed=closed, summary=summary, live=live)


def _mark_one(trade):
    """Current price, open R and state for a single position."""
    from bot.market import fetch_bars
    bars = fetch_bars(trade["symbol"], trade["interval"] or "1d")
    price = bars.last_price
    direction = int(trade["direction"])
    entry, stop = float(trade["entry"]), float(trade["stop"])
    risk = abs(entry - stop)
    r_now = ((price - entry) * direction / risk) if risk > 0 else 0.0

    target = trade["target1"]
    status = "open"
    if (price <= stop) if direction > 0 else (price >= stop):
        status = "past stop"
    elif target and ((price >= float(target)) if direction > 0
                     else (price <= float(target))):
        status = "past target"
    return {"price": price, "r": r_now, "status": status}


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


@app.route("/trades/close", methods=["POST"])
def close_trade():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        db_mod.close_trade(conn, int(request.form["trade_id"]),
                           float(request.form["price"]),
                           request.form.get("reason", "manual"),
                           request.form.get("notes", ""), user=uid())
    except (ValueError, KeyError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


@app.route("/trades/take", methods=["POST"])
def take_trade():
    """Open a paper position from a recorded plan."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        run_id = int(request.form["run_id"])
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or not row["entry"] or not row["stop"]:
            return render_template("error.html", symbol="",
                                   message="That analysis has no entry and stop "
                                           "to trade."), 400

        cfg = _cfg()
        direction = 1 if row["action"] in ("BUY", "WAIT") else -1
        entry, stop = float(row["entry"]), float(row["stop"])
        per_unit = abs(entry - stop)
        account = cfg["account_size"]
        qty_raw = (account * cfg["risk_per_trade_pct"] / 100.0 / per_unit
                   if per_unit > 0 else 0)
        cap = account * cfg["max_position_pct"] / 100.0
        if qty_raw * entry > cap:
            qty_raw = cap / entry
        qty = round(qty_raw, 6) if row["asset_class"] == "crypto" else float(int(qty_raw))
        if qty <= 0:
            return render_template("error.html", symbol=row["symbol"],
                                   message="Account too small for one unit at "
                                           "this stop distance."), 400

        db_mod.open_trade(conn, symbol=row["symbol"], interval=row["interval"],
                          direction=direction, entry=entry, stop=stop,
                          target1=row["target1"], target2=row["target2"],
                          quantity=qty, risk_amount=qty * per_unit,
                          run_id=run_id, account="paper",
                          strategy_note="from run #%d (%s)" % (run_id, row["action"]),
                          user=uid())
    except (ValueError, KeyError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("trades"))


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
        db_mod.open_trade(
            conn,
            symbol=request.form["symbol"].strip().upper(),
            interval=request.form.get("interval") or "1d",
            direction=direction, entry=entry, stop=stop,
            target1=float(target) if target else None,
            quantity=float(qty) if qty else None,
            risk_amount=(abs(entry - stop) * float(qty)) if qty else None,
            account="paper", strategy_note="entered by hand",
            notes=request.form.get("notes", ""), user=uid())
    except (KeyError, ValueError) as exc:
        message = str(exc)
        if isinstance(exc, KeyError):
            message = "Instrument, entry and stop are all required."
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


@app.route("/strategies")
def strategies():
    regime = request.args.get("regime", "all")
    rank_by = request.args.get("rank_by", "gross")
    symbol = request.args.get("symbol") or None
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = db_mod.strategy_leaderboard(conn, symbol=symbol, regime=regime,
                                           minimum=40, limit=40, rank_by=rank_by)
        seen = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM strategy_stats ORDER BY symbol")]
    finally:
        conn.close()
    return render_template("strategies.html", rows=rows, regime=regime,
                           rank_by=rank_by, symbol=symbol, seen=seen)


@app.route("/analyse/image", methods=["POST"])
def analyse_image():
    """Read a chart screenshot and analyse whatever instrument it shows.

    The image is only ever used to identify the instrument. Everything after
    that runs on live data, because a screenshot may already be minutes stale
    by the time it is uploaded.
    """
    upload = request.files.get("chart")
    if upload is None or not upload.filename:
        return render_template("error.html", symbol="",
                               message="No image was attached."), 400

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return render_template("error.html", symbol="", no_key=True,
                               message="Reading a chart image needs an Anthropic "
                                       "API key, and none is set."), 400

    tmp_dir = os.path.join(config_mod.project_root(), "data")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, "_upload.png")

    try:
        # Re-encode through Pillow rather than trusting the upload: it both
        # validates that the bytes really are an image and strips anything
        # unusual the original file carried.
        from PIL import Image
        img = Image.open(upload.stream)
        img.verify()
        upload.stream.seek(0)
        img = Image.open(upload.stream)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(tmp_path, format="PNG")
    except Exception:
        return render_template("error.html", symbol="",
                               message="That file could not be read as an image. "
                                       "PNG or JPEG works best."), 400

    try:
        read = read_chart(tmp_path)
    except VisionError as exc:
        return render_template("error.html", symbol="", message=str(exc)), 502
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not read.symbol:
        return render_template("error.html", symbol="",
                               message="No instrument could be identified in that "
                                       "image. Try a clearer screenshot, or just "
                                       "type the ticker."), 422

    interval = request.form.get("interval") or _interval_from(read.timeframe)
    return redirect(url_for("analyse", symbol=read.symbol, interval=interval,
                            fromimage="1"))


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
    symbol = (request.args.get("symbol") or "").strip()
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


    if not _allowance_left(symbol, interval):
        return _allowance_used()

    cfg = _cfg()
    try:
        analysis = cached_analysis(symbol, interval, cfg,
                                   request.args.get("news", "1") != "0",
                                   record=not g.demo,
                                   ttl=DEMO_TTL if g.demo else None)
    except DataError as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 404
    _spend_view(symbol, interval)

    key = (analysis.bars.symbol.upper(), interval, "research")
    now = time.time()
    with _CACHE_LOCK:
        hit = _RESEARCH_CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        found = hit[1]
    else:
        found = engine.research(analysis.bars.symbol, analysis.bars.last_price,
                                cfg, analysis=analysis,
                                with_options=request.args.get("options", "1") != "0")
        with _CACHE_LOCK:
            _RESEARCH_CACHE[key] = (time.time(), found)
            if len(_RESEARCH_CACHE) > 20:
                oldest = min(_RESEARCH_CACHE, key=lambda k: _RESEARCH_CACHE[k][0])
                _RESEARCH_CACHE.pop(oldest, None)

    # Drawn from the bars and filings already in hand, so no extra request.
    price_svg = charts_mod.price_chart(
        [float(v) for v in analysis.bars.close[-260:]],
        labels=("about a year ago", "now"),
        title="%s price history" % analysis.bars.symbol)
    filed_charts = charts_mod.charts_for(found.sec_history or {})

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        saved = db_mod.thesis_list(conn, analysis.bars.symbol, limit=6, user=uid())
        watched = any(r["symbol"] == analysis.bars.symbol
                      for r in db_mod.watchlist(conn, user=uid()))
    finally:
        conn.close()

    reviews = []
    for row in saved:
        reviews.append((row, thesis_mod.review(
            {"price": row["price"], "balance": row["balance"],
             "created": row["created_ts"]}, analysis.bars.last_price)))

    return render_template("research.html", r=found, a=analysis, cfg=cfg,
                           saved=reviews, watched=watched,
                           price_svg=price_svg, filed_charts=filed_charts,
                           plain=plain_mod.explain_company(found))


@app.route("/thesis/save", methods=["POST"])
def save_thesis():
    symbol = request.form.get("symbol", "").upper()
    interval = request.form.get("interval", "1d")
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        cfg = _cfg()
        analysis = cached_analysis(symbol, interval, cfg, False)
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
            t.falsifiers, t.all_sources, request.form.get("note", ""),
            user=uid())
    except Exception as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 400
    finally:
        conn.close()
    return redirect(url_for("research", symbol=symbol, interval=interval))


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
            price = market_mod.fetch_bars(row["symbol"], "1d").last_price
        except DataError as exc:
            return render_template("error.html", symbol=row["symbol"],
                                   message="Could not price %s to close the "
                                           "thesis: %s" % (row["symbol"], exc)), 502

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


@app.route("/signin")
def signin():
    """The page you land on when you are not signed in."""
    if getattr(g, "user", None) is not None:
        return redirect(url_for("index"))
    return render_template("signin.html",
                           reason=request.args.get("reason", ""),
                           why_not=auth_mod.why_not(),
                           next_url=_safe_back(request.args.get("next"), ""))


@app.route("/auth/google")
def google_start():
    """Begin the Google sign-in flow."""
    if not auth_mod.configured():
        return render_template("signin.html", reason=auth_mod.why_not(),
                               why_not=auth_mod.why_not(), next_url=""), 503

    state = auth_mod.new_state()
    verifier = auth_mod.new_verifier()
    # Held in the signed cookie, so the callback can prove it belongs to the
    # browser that started this and not to someone else's forged link.
    session["oauth_state"] = state
    session["oauth_verifier"] = verifier
    session["oauth_next"] = _safe_back(request.args.get("next"), "")

    try:
        url = auth_mod.authorization_url(_redirect_uri(), state, verifier)
    except auth_mod.AuthError as exc:
        return render_template("signin.html", reason=str(exc),
                               why_not="", next_url=""), 503
    return redirect(url)


@app.route("/auth/google/callback")
def google_callback():
    """Where Google sends the browser back."""
    expected = session.pop("oauth_state", "")
    verifier = session.pop("oauth_verifier", "")
    next_url = session.pop("oauth_next", "")

    if request.args.get("error"):
        return render_template(
            "signin.html", why_not="", next_url="",
            reason="Google did not complete the sign-in: %s"
                   % request.args.get("error")), 400

    state = request.args.get("state", "")
    # Constant time, because this is a secret being compared.
    if not expected or not hmac.compare_digest(str(expected), str(state)):
        return render_template(
            "signin.html", why_not="", next_url="",
            reason="That sign-in link did not match this browser session. "
                   "Start again from this page."), 400

    code = request.args.get("code", "")
    if not code:
        return render_template("signin.html", why_not="", next_url="",
                               reason="Google returned no sign-in code."), 400

    try:
        profile = auth_mod.sign_in(code, _redirect_uri(), verifier)
    except auth_mod.AuthError as exc:
        return render_template("signin.html", reason=str(exc), why_not="",
                               next_url=""), 400

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        user = accounts_mod.upsert_google_user(conn, profile)
        accounts_mod.purge_expired(conn)
        token = accounts_mod.start_session(
            conn, user.id, request.headers.get("User-Agent", ""))
    except ValueError as exc:
        return render_template("signin.html", reason=str(exc), why_not="",
                               next_url=""), 400
    finally:
        conn.close()

    session.permanent = True
    session[SESSION_KEY] = token
    return redirect(_safe_back(next_url, url_for("index")))


def _finish_sign_in(user, next_url: str):
    """Start a session for someone who just proved who they are."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        accounts_mod.purge_expired(conn)
        token = accounts_mod.start_session(
            conn, user.id, request.headers.get("User-Agent", ""))
    finally:
        conn.close()
    session.permanent = True
    session[SESSION_KEY] = token
    return redirect(_safe_back(next_url, url_for("index")))


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
                user = accounts_mod.upsert_email_user(conn, pending["to"])
            else:
                codes_mod.check_phone_code(conn, pending["to"],
                                           request.form.get("code", ""))
                user = accounts_mod.upsert_phone_user(conn, pending["to"])
        except (codes_mod.CodeError, ValueError) as exc:
            error = str(exc)
        else:
            session.pop("code_login", None)
            return _finish_sign_in(user, pending.get("next", ""))
        finally:
            conn.close()
    return render_template("code_verify.html", channel=channel,
                           to=pending["to"], error=error)


@app.route("/pricing")
def pricing():
    """Free and Pro, side by side. Only exists once billing is set up."""
    if not billing_mod.ready():
        abort(404)
    customer = ""
    if getattr(g, "user", None) is not None and not g.demo:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            customer = billing_mod.customer_of(conn, g.user.id)
        finally:
            conn.close()
    return render_template("pricing.html", reason=request.args.get("reason", ""),
                           limits=billing_mod.free_limits(),
                           price=billing_mod.price_label(),
                           is_pro=bool(getattr(g, "user", None)) and _pro(),
                           has_customer=bool(customer))


@app.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    """Send someone to Stripe's own page to pay."""
    if not billing_mod.ready() or g.demo:
        abort(404)
    if _pro():
        return redirect(url_for("account"))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        customer = billing_mod.customer_of(conn, g.user.id)
    finally:
        conn.close()
    try:
        url = billing_mod.checkout_url(g.user, _public_base(), customer)
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
            upgraded = billing_mod.confirm_checkout(conn, session_id, g.user.id)
        except billing_mod.BillingError:
            upgraded = False
        finally:
            conn.close()
    return render_template("billing_done.html", upgraded=upgraded)


@app.route("/billing/portal", methods=["POST"])
def billing_portal():
    """Stripe's own page for cancelling, changing card and receipts."""
    if not billing_mod.ready() or g.demo:
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
        event = json.loads(payload.decode("utf-8"))
    except ValueError:
        return jsonify({"error": "not json"}), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        outcome = billing_mod.handle(conn, event)
    finally:
        conn.close()
    return jsonify({"received": True, "outcome": outcome})


@app.route("/signout", methods=["GET", "POST"])
def signout():
    """End this session everywhere, not just in this browser."""
    token = session.pop(SESSION_KEY, "")
    if token:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            accounts_mod.end_session(conn, token)
        finally:
            conn.close()
    session.clear()
    return redirect(url_for("signin", reason="You are signed out."))


@app.route("/account", methods=["GET"])
def account():
    """What this account holds, and how to leave."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        counts = accounts_mod.owned_counts(conn, uid())
        everyone = accounts_mod.list_users(conn) if g.user.is_local else []
    finally:
        conn.close()
    plan = {"on": billing_mod.ready(), "pro": _pro(), "customer": ""}
    if plan["on"]:
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            plan["customer"] = billing_mod.customer_of(conn, uid())
            plan["used"] = billing_mod.views_today(conn, uid())
        finally:
            conn.close()
        plan["limit"] = billing_mod.free_limits()["analyses_per_day"]
    return render_template("account.html", counts=counts, everyone=everyone,
                           datasets=export_mod.DATASETS, plan=plan)


@app.route("/account/delete", methods=["POST"])
def delete_account():
    """Remove this account and everything personal in it."""
    if request.form.get("confirm") != "delete":
        return render_template("error.html", symbol="",
                               message="Type delete to confirm."), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        # Stop the subscription first, or a deleted account keeps being
        # charged. If Stripe cannot be reached, nothing is deleted.
        customer = billing_mod.customer_of(conn, uid())
        if customer and billing_mod.ready():
            billing_mod.close_customer(customer)
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
    return redirect(url_for("signin", reason="That account and everything in "
                                             "it has been deleted."))


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
        return render_template(
            "error.html", symbol="",
            message="This page covers activity across every account, so it is "
                    "limited to whoever runs this installation. Set "
                    "STOCKBOT_ADMIN_EMAIL to your address to see it."), 403

    try:
        days = max(1, min(365, int(request.args.get("days", "30"))))
    except ValueError:
        days = 30

    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        data = usage_mod.read(conn, days)
    finally:
        conn.close()

    trend = ""
    if len(data["daily"]) > 1:
        trend = charts_mod.bar_chart(
            [d["day"] for d in data["daily"]],
            [float(d["runs"]) for d in data["daily"]],
            width=720, height=180, title="Analyses per day", money=False)

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

    from bot import upstream as upstream_mod
    return render_template("usage.html", data=data, days=days, trend=trend,
                           storage=storage,
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
            "same_as": SAME_AS}


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


@app.route("/robots.txt")
def robots():
    return Response(ROBOTS % _public_base(), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    base = _public_base()
    rows = "".join(
        "<url><loc>%s%s</loc><changefreq>%s</changefreq>"
        "<priority>%s</priority></url>" % (base, path, freq, priority)
        for path, freq, priority in PUBLIC_PAGES)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '%s</urlset>' % rows, mimetype="application/xml")


@app.route("/privacy")
def privacy():
    """Required by Google's consent screen, and worth having regardless."""
    return render_template("legal.html", page="privacy",
                           heading="Privacy", updated=LEGAL_UPDATED,
                           session_days=accounts_mod.SESSION_DAYS,
                           contact=_contact())


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


@app.route("/ideas")
def ideas():
    """What is worth looking at, given how you trade."""
    market = request.args.get("market") or "stocks"
    horizon = request.args.get("horizon") or "medium"
    risk = request.args.get("risk") or "balanced"
    cfg = _cfg()

    result = None
    preparing = False
    asked = (request.args.get("market") or request.args.get("horizon")
             or request.args.get("risk"))
    if g.demo and asked:
        # The example reads a scan made in the background at most once an
        # hour, so visitors cannot make the server run 150-instrument scans.
        if horizon == "short":
            return _locked("Create an account to scan for short holds",
                           "The example scans daily bars. With an account you "
                           "can scan for holds of a few days on hourly bars.",
                           examples=False)
        result = _demo_shortlist(market, horizon, risk)
        preparing = result is None
        if result is not None and result.get("scanned_at"):
            result = dict(result, age=time.time() - result["scanned_at"])
    elif asked:
        try:
            result = ideas_mod.find(market, horizon, cfg, risk)
            # Scans are shared between everyone asking the same question, so
            # this one may have run minutes ago. The page says so rather than
            # presenting a stale list as this second's answer.
            if result.get("scanned_at"):
                result = dict(result, age=time.time() - result["scanned_at"])
        except Exception as exc:
            return render_template("error.html", symbol="",
                                   message="The scan failed: %s" % exc), 502

    # Visitors included: an example that showed more than a free account would
    # make signing up look like a downgrade.
    if result is not None and not _pro():
        keep = billing_mod.free_limits()["find_results"]
        total = len(result["passed"]) + len(result["watch"])
        if len(result["passed"]) > keep or len(result["watch"]) > keep:
            result = dict(result, passed=result["passed"][:keep],
                          watch=result["watch"][:keep], limited_total=total)

    return render_template("ideas.html", result=result, preparing=preparing,
                           market=market if market in ideas_mod.MARKETS else "stocks",
                           horizon=horizon if horizon in ideas_mod.HORIZONS else "medium",
                           risk=risk if risk in ideas_mod.RISKS else "balanced",
                           markets=ideas_mod.MARKETS, horizons=ideas_mod.HORIZONS,
                           risks=ideas_mod.RISKS)


# The example account's scans, one per market, refreshed at most hourly and
# only when a visitor actually asks for one.
DEMO_SCAN_TTL = 60 * 60.0
_demo_scans: Dict[str, Dict] = {}
_demo_scan_lock = threading.Lock()


def _refresh_demo_scan(market: str) -> None:
    try:
        cfg = config_mod.load(app.config.get("CFG_PATH"))
        scan = ideas_mod._scan(market, "1d", cfg, ideas_mod.MAX_SCANNED)
        with _demo_scan_lock:
            _demo_scans[market].update(scan=scan, at=time.time())
    except Exception:
        pass
    finally:
        with _demo_scan_lock:
            _demo_scans[market]["busy"] = False


def _demo_shortlist(market: str, horizon: str, risk: str) -> Optional[Dict]:
    market = market if market in ideas_mod.MARKETS else "stocks"
    horizon = horizon if horizon in ideas_mod.HORIZONS else "medium"
    risk = risk if risk in ideas_mod.RISKS else "balanced"
    with _demo_scan_lock:
        slot = _demo_scans.setdefault(market, {"scan": None, "at": 0.0,
                                               "busy": False})
        stale = slot["scan"] is None or time.time() - slot["at"] > DEMO_SCAN_TTL
        if stale and not slot["busy"]:
            slot["busy"] = True
            threading.Thread(target=_refresh_demo_scan, args=(market,),
                             daemon=True).start()
        scan = slot["scan"]
    if scan is None:
        return None
    return ideas_mod._shortlist(scan, horizon, risk)


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
        for key, spec in screener_mod.FILTERS.items():
            raw = request.args.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            # Percent fields are typed as whole numbers by people, not decimals.
            if spec["unit"] == "pct" and value > 1:
                value = value / 100.0
            filters[key] = value

    result = None
    if g.demo and request.args.get("go") and not preset:
        return _locked("Create an account to run your own screen",
                       "The example can run the ready-made screens. With an "
                       "account you can set your own filters.", examples=False)
    if g.demo and preset:
        # A ready-made screen is the same answer for every visitor, so it is
        # run once and shared rather than once per visit.
        from bot import upstream as upstream_mod
        try:
            result = upstream_mod.cached(
                "demo-screen:" + preset_key,
                lambda: screener_mod.run(symbols=None, universe=universe,
                                         filters=filters, limit=40),
                ttl=DEMO_TTL, wait=60.0)
        except Exception as exc:
            return render_template("error.html", message=str(exc), symbol=""), 500
    elif request.args.get("go") or preset:
        symbols = [s.strip() for s in symbols_raw.replace(",", " ").split()
                   ] if symbols_raw else None
        try:
            result = screener_mod.run(symbols=symbols, universe=universe,
                                      filters=filters, limit=40)
        except Exception as exc:
            return render_template("error.html", message=str(exc), symbol=""), 500

    return render_template("screener.html", result=result, presets=screener_mod.PRESETS,
                           universes=screener_mod.UNIVERSES, filters=filters,
                           universe=universe, symbols_raw=symbols_raw,
                           preset_key=preset_key,
                           filter_specs=screener_mod.FILTERS)


@app.route("/portfolio")
def portfolio():
    """Exposure, concentration, correlation and what a shock would do."""
    cfg = _cfg()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        rows = db_mod.list_trades(conn, status="open", limit=100, user=uid())
        summary = db_mod.journal_summary(conn, user=uid())
    finally:
        conn.close()

    view = portfolio_mod.build(rows, cfg["account_size"])
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
    finally:
        conn.close()

    described = [(r, alerts_mod.describe(r["kind"], r["threshold"], r["symbol"]))
                 for r in alert_rows]

    # Price each open thesis so the page can say how it is doing, and propose
    # the outcome the price supports. One quote per distinct symbol.
    judged = []
    prices = {}
    for row in thesis_rows:
        sym = row["symbol"]
        if sym not in prices:
            try:
                prices[sym] = market_mod.fetch_bars(sym, "1d").last_price
            except Exception:
                # One unpriceable symbol must not take the whole page down.
                prices[sym] = None
        price = prices[sym]
        stored = {"price": row["price"], "balance": row["balance"],
                  "created": row["created_ts"]}
        look = (thesis_mod.review(stored, price) if price else
                {"usable": False, "reason": "Could not price %s today." % sym})
        proposed = (thesis_mod.suggested_outcome(stored, price)
                    if price and not row["outcome"] else None)
        judged.append({"row": row, "now": price, "review": look,
                       "proposed": proposed})

    board = thesis_mod.scoreboard([dict(r) for r in thesis_rows])

    return render_template("monitor.html", alerts=described, watchlist=watch_rows,
                           theses=judged, board=board,
                           outcomes=thesis_mod.OUTCOMES, kinds=alerts_mod.KINDS,
                           workflows=workflows_mod.WORKFLOWS,
                           last_run=app.config.get("LAST_WORKFLOW"))


@app.route("/monitor/alert/add", methods=["POST"])
def add_alert():
    if not _pro():
        conn = db_mod.connect(app.config.get("DB_PATH"))
        try:
            running = len(db_mod.alert_list(conn, active_only=True, user=uid()))
        finally:
            conn.close()
        cap = billing_mod.free_limits()["alerts"]
        if running >= cap:
            return _upgrade("A free account can run %d alert%s at a time. "
                            "Remove one on the Monitor page, or Pro has no limit."
                            % (cap, "" if cap == 1 else "s"))
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        raw = (request.form.get("threshold") or "").strip()
        threshold = float(raw) if raw else None
        kind = request.form["kind"]
        # People type "5" meaning five percent, not five hundred percent.
        if threshold is not None and alerts_mod.KINDS[kind]["unit"] == "percent" \
                and threshold > 1:
            threshold = threshold / 100.0
        db_mod.alert_add(conn, request.form["symbol"], kind, threshold,
                         request.form.get("interval") or "1d",
                         request.form.get("note", ""),
                         request.form.get("repeat") == "1", user=uid())
    except (KeyError, ValueError) as exc:
        message = str(exc)
        if isinstance(exc, ValueError) and "could not convert" in message:
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
    if action not in ("delete", "reset"):
        return render_template("error.html", message="Unknown action.", symbol=""), 404
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        alert_id = int(request.form["alert_id"])
        if action == "delete":
            db_mod.alert_delete(conn, alert_id, user=uid())
        else:
            db_mod.alert_reset(conn, alert_id, user=uid())
    except (KeyError, ValueError) as exc:
        return render_template("error.html", message=str(exc), symbol=""), 400
    finally:
        conn.close()
    return redirect(url_for("monitor"))


@app.route("/monitor/alerts/check", methods=["POST"])
def check_alerts():
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        result = alerts_mod.check(conn, user=uid())
    finally:
        conn.close()
    app.config["LAST_ALERT_CHECK"] = result
    return redirect(url_for("monitor", checked=result["checked"],
                            fired=len(result["fired"])))


@app.route("/watch/<action>", methods=["POST"])
def watch(action):
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        symbol = request.form["symbol"]
        if action == "add":
            db_mod.watch_add(conn, symbol, request.form.get("interval") or "1d",
                             request.form.get("note", ""), user=uid())
        elif action == "remove":
            db_mod.watch_remove(conn, symbol, user=uid())
        else:
            return render_template("error.html", message="Unknown action.",
                                   symbol=""), 404
    except KeyError:
        return render_template("error.html", message="No instrument given.",
                               symbol=""), 400
    finally:
        conn.close()
    return redirect(_safe_back(request.form.get("back"), url_for("monitor")))


@app.route("/workflow/<name>", methods=["POST"])
def run_workflow(name):
    if name not in workflows_mod.WORKFLOWS:
        return render_template("error.html", message="No routine called %r." % name,
                               symbol=""), 404
    cfg = _cfg()
    report = workflows_mod.run(name, cfg, app.config.get("DB_PATH"),
                               user=uid())
    app.config["LAST_WORKFLOW"] = report
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
            error = str(exc)

    if request.method == "POST" and request.form.get("reset") != "1":
        cfg = _cfg()
        numbers = ("account_size", "risk_per_trade_pct", "max_position_pct",
                   "cost_bps_equity", "cost_bps_crypto", "stop_atr_multiple",
                   "target_atr_multiple", "min_conviction", "strong_conviction",
                   "alert_check_minutes")
        try:
            for key in numbers:
                if request.form.get(key) not in (None, ""):
                    cfg[key] = float(request.form[key])
            raw_horizon = (request.form.get("horizon_bars") or "").strip()
            if raw_horizon:
                cfg["horizon_bars"] = ("auto" if raw_horizon.lower() == "auto"
                                       else int(float(raw_horizon)))
            if request.form.get("interval"):
                cfg["interval"] = request.form["interval"]
            for flag in ("allow_shorts", "require_positive_expectancy",
                         "use_llm_sentiment"):
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
            # Apply a changed check interval now rather than at next start.
            if ALERTS is not None:
                minutes = cfg.get("alert_check_minutes", 5)
                if minutes:
                    ALERTS.set_interval(float(minutes) * 60.0)
                else:
                    ALERTS.stop()
            return redirect(url_for("settings", saved="1"))
        except ValueError:
            error = "Every numeric field needs a number."
        except RuntimeError as exc:
            error = str(exc)

    cfg = _cfg()
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        export_counts = export_mod.counts(conn, user=uid())
    finally:
        conn.close()
    return render_template("settings.html", cfg=cfg,
                           personal=personal, operator=operator,
                           datasets=export_mod.DATASETS,
                           export_counts=export_counts,
                           scheduler=(ALERTS.status() if ALERTS else None),
                           saved=saved or request.args.get("saved") == "1",
                           error=error,
                           has_key=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.route("/api/analyse")
def api_analyse():
    """JSON, for anything you want to build on top."""
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    interval = request.args.get("interval") or "1d"
    # The same allowance as the pages, or the API is a way round it.
    if not _allowance_left(symbol, interval):
        return jsonify({"error": "The free daily allowance is used up."}), 402
    cfg = _cfg()
    try:
        result = cached_analysis(symbol, interval, cfg,
                                 request.args.get("news", "1") != "0")
    except DataError as exc:
        return jsonify({"error": str(exc)}), 404
    _spend_view(symbol, interval)

    from bot import report as report_mod
    import json as _json
    return app.response_class(
        report_mod.to_json(result.bars, result.plan, result.signals,
                           result.composite, result.regime, result.calibration,
                           result.sentiment),
        mimetype="application/json")


@app.errorhandler(404)
def not_found(_exc):
    return render_template("error.html", symbol="",
                           message="No page at that address."), 404


@app.errorhandler(413)
def too_large(_exc):
    return render_template("error.html", symbol="",
                           message="That image is larger than 12 MB. A normal "
                                   "screenshot is well under that, so try "
                                   "cropping it to just the chart."), 413


@app.errorhandler(500)
def server_error(exc):
    return render_template("error.html", symbol="", message=str(exc)), 500


# One scheduler for the process. Created on start, not at import, so importing
# web.py in a test or a script never spawns a thread.
ALERTS = None


def _check_alerts_job():
    """What the background loop actually does, every interval."""
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        return alerts_mod.check(conn)
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
    """What the background checker has been doing, for the page to poll.

    `since` lets a page ask only for alerts that fired after it last looked, so
    it can announce them once rather than on every poll.
    """
    if ALERTS is None:
        return jsonify({"running": False, "enabled": False,
                        "note": "Background checking is switched off in settings."})
    try:
        since = float(request.args.get("since") or 0)
    except ValueError:
        since = 0.0
    out = ALERTS.status()
    out["enabled"] = True
    out["new_fires"] = ALERTS.unseen_fires(since)
    out["now"] = time.time()
    return jsonify(out)


@app.route("/api/alerts/check-now", methods=["POST"])
def alert_check_now():
    """Ask the background loop to run immediately."""
    if ALERTS is None:
        return jsonify({"ok": False,
                        "note": "Background checking is switched off."}), 409
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
