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
                   render_template, request, session, url_for)

from bot import accounts as accounts_mod
from bot import alerts as alerts_mod
from bot import auth as auth_mod
from bot import catalysts as catalysts_mod
from bot import charts as charts_mod
from bot import config as config_mod
from bot import database as db_mod
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
                    "robots", "sitemap", "index"}

SESSION_KEY = "stockbot_session"

# Bumped by hand when the legal pages change, so the date on them is
# the date they were actually revised.
LEGAL_UPDATED = "11 September 2026"


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
    return auth_mod.configured()


@app.before_request
def _identify():
    """Attach the signed-in person, or the local account, to this request."""
    g.user = None
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
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not signed in."}), 401
    return redirect(url_for("signin", next=request.full_path))


@app.context_processor
def _expose_user():
    """Templates need to know who is signed in and whether signing in exists."""
    return {"current_user": getattr(g, "user", None),
            "auth_on": auth_required(),
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
_CACHE_LOCK = threading.Lock()

INTERVALS = [("5m", "5 minutes"), ("15m", "15 minutes"), ("30m", "30 minutes"),
             ("1h", "1 hour"), ("1d", "Daily")]


def cached_analysis(symbol: str, interval: str, cfg: Dict,
                    with_news: bool = True) -> engine.Analysis:
    key = (symbol.upper(), interval)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    # The cache key is deliberately not per user: an analysis of AAPL is the
    # same analysis whoever asked for it, and recomputing it per person would
    # be pure waste. Only the record of having run it belongs to someone, and
    # that is written inside engine.analyse before the result is cached.
    result = engine.analyse(symbol, cfg, interval=interval, with_news=with_news,
                            source="web", db_path=app.config.get("DB_PATH"),
                            user=uid())
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

@app.route("/")
def index():
    # A signed-out visitor gets the landing page here rather than a redirect to
    # /signin. Search engines are reluctant to index a login page, and a
    # redirect from the front door is the weakest thing a crawler can be
    # handed: the address everyone shares would have had nothing behind it.
    # It is also the first thing a person arriving from a link sees, and a bare
    # login form asks them to commit before they know what this is.
    if getattr(g, "user", None) is None:
        return render_template("signin.html", reason="",
                               why_not=auth_mod.why_not(), next_url="")

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

    cfg = config_mod.load(app.config.get("CFG_PATH"))
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

    try:
        result = cached_analysis(symbol, interval, cfg, with_news)
    except DataError as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 404
    except Exception as exc:                        # keep the app usable
        return render_template("error.html", message=str(exc), symbol=symbol), 500

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

        cfg = config_mod.load(app.config.get("CFG_PATH"))
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

    cfg = config_mod.load(app.config.get("CFG_PATH"))
    try:
        analysis = cached_analysis(symbol, interval, cfg,
                                   request.args.get("news", "1") != "0")
    except DataError as exc:
        return render_template("error.html", message=str(exc), symbol=symbol), 404

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
        cfg = config_mod.load(app.config.get("CFG_PATH"))
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
    return render_template("account.html", counts=counts, everyone=everyone,
                           datasets=export_mod.DATASETS)


@app.route("/account/delete", methods=["POST"])
def delete_account():
    """Remove this account and everything personal in it."""
    if request.form.get("confirm") != "delete":
        return render_template("error.html", symbol="",
                               message="Type delete to confirm."), 400
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        accounts_mod.delete_user(conn, uid())
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

    from bot import upstream as upstream_mod
    return render_template("usage.html", data=data, days=days, trend=trend,
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
    return (os.environ.get("STOCKBOT_CONTACT") or "").strip() or         "set STOCKBOT_CONTACT to an email address"


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
    cfg = config_mod.load(app.config.get("CFG_PATH"))

    result = None
    if request.args.get("market") or request.args.get("horizon"):
        try:
            result = ideas_mod.find(market, horizon, cfg)
        except Exception as exc:
            return render_template("error.html", symbol="",
                                   message="The scan failed: %s" % exc), 502

    return render_template("ideas.html", result=result,
                           market=market if market in ideas_mod.MARKETS else "stocks",
                           horizon=horizon if horizon in ideas_mod.HORIZONS else "medium",
                           markets=ideas_mod.MARKETS, horizons=ideas_mod.HORIZONS)


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
    if request.args.get("go") or preset:
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
    cfg = config_mod.load(app.config.get("CFG_PATH"))
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
    cfg = config_mod.load(app.config.get("CFG_PATH"))
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

    if request.args.get("reset") == "1":
        try:
            fresh = json.loads(json.dumps(config_mod.DEFAULTS))
            config_mod.save(fresh, cfg_path)
            _CACHE.clear()
            return redirect(url_for("settings", saved="1"))
        except Exception as exc:
            error = str(exc)

    if request.method == "POST":
        cfg = config_mod.load(cfg_path)
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

            config_mod.save(cfg, cfg_path)
            # Cached analyses were produced under the old settings.
            _CACHE.clear()
            _RESEARCH_CACHE.clear()
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

    cfg = config_mod.load(cfg_path)
    conn = db_mod.connect(app.config.get("DB_PATH"))
    try:
        export_counts = export_mod.counts(conn, user=uid())
    finally:
        conn.close()
    return render_template("settings.html", cfg=cfg,
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
    cfg = config_mod.load(app.config.get("CFG_PATH"))
    try:
        result = cached_analysis(symbol, interval, cfg,
                                 request.args.get("news", "1") != "0")
    except DataError as exc:
        return jsonify({"error": str(exc)}), 404

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
