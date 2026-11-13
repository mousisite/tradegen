"""Alerts: conditions worth being told about.

Checked on demand, and by the server's background loop (web.start_alert_loop),
which keeps going when nobody has the page open. Nothing here starts a thread
or sends mail itself: the loop calls check() and check_stops(), and deliver()
emails what fired, through whatever sender it is handed.

Each alert states its condition in plain terms and, when it fires, records the
value that triggered it. An alert that says only "AAPL alert triggered" is
useless an hour later.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from . import indicators as ind
from .market import DataError, fetch_bars, format_price

KINDS: Dict[str, Dict] = {
    "price_above": {"label": "Price rises above", "needs_threshold": True,
                    "unit": "price"},
    "price_below": {"label": "Price falls below", "needs_threshold": True,
                    "unit": "price"},
    "pct_up": {"label": "Rises by at least", "needs_threshold": True,
               "unit": "percent"},
    "pct_down": {"label": "Falls by at least", "needs_threshold": True,
                 "unit": "percent"},
    "rsi_above": {"label": "RSI goes above", "needs_threshold": True,
                  "unit": "level"},
    "rsi_below": {"label": "RSI falls below", "needs_threshold": True,
                  "unit": "level"},
    "near_52w_high": {"label": "Comes within", "needs_threshold": True,
                      "unit": "percent", "suffix": "of its 52-week high"},
    "near_52w_low": {"label": "Comes within", "needs_threshold": True,
                     "unit": "percent", "suffix": "of its 52-week low"},
    "volume_spike": {"label": "Trades on more than", "needs_threshold": True,
                     "unit": "multiple", "suffix": "its normal volume"},
    "crosses_ma": {"label": "Closes above its 50-bar average",
                   "needs_threshold": False, "unit": ""},
    "loses_ma": {"label": "Closes below its 50-bar average",
                 "needs_threshold": False, "unit": ""},
    # Calls rather than prices: the app's own BUY / WAIT / AVOID for the
    # instrument, worked out with the standard settings.
    "call_changes": {"label": "Call changes (for example WAIT to BUY)",
                     "needs_threshold": False, "unit": "", "call": True},
    "call_buy": {"label": "Call turns BUY", "needs_threshold": False, "unit": "",
                 "call": True},
}

CALL_KINDS = tuple(k for k, spec in KINDS.items() if spec.get("call"))


@dataclass
class Fired:
    """One alert whose condition was met."""
    alert_id: int
    symbol: str
    kind: str
    threshold: Optional[float]
    value: float
    message: str
    note: str = ""
    user_id: Optional[int] = None
    as_of: str = ""
    trade_id: Optional[int] = None
    event_id: Optional[int] = None


def describe(kind: str, threshold: Optional[float], symbol: str) -> str:
    """Plain sentence for an alert, used everywhere it is displayed."""
    spec = KINDS.get(kind)
    if not spec:
        return "%s: unknown condition" % symbol
    if not spec["needs_threshold"]:
        return "%s %s" % (symbol, spec["label"].lower())
    unit = spec["unit"]
    if unit == "percent":
        shown = "%.1f%%" % (threshold * 100 if threshold and threshold < 1 else threshold or 0)
    elif unit == "multiple":
        shown = "%.1fx" % (threshold or 0)
    elif unit == "price":
        shown = format_price(threshold)
    else:
        shown = "%.0f" % (threshold or 0)
    text = "%s %s %s" % (symbol, spec["label"].lower(), shown)
    if spec.get("suffix"):
        text += " " + spec["suffix"]
    # "rsi" reads wrong in lower case; it is an abbreviation, not a word.
    return text.replace(" rsi ", " RSI ")


def as_of(bars) -> str:
    """When the price behind a message was set, said with the message.

    An alert that says "above 200" without saying when that was true is
    unreadable an hour later, and on a closed market it may be Friday's.
    """
    meta = getattr(bars, "meta", None) or {}
    when = meta.get("regularMarketTime")
    if when:
        stamp = time.strftime("%d %b %H:%M UTC", time.gmtime(int(when))).lstrip("0")
    else:
        stamp = getattr(bars, "last_bar_time", "") or ""
    if stamp and not getattr(bars, "market_open", True):
        stamp += ", market closed"
    return stamp


def _owner(row) -> Optional[int]:
    try:
        return int(row["user_id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _evaluate(row, bars, call: Optional[str] = None) -> Optional[Fired]:
    """Test one alert against fresh bars. Returns None when not triggered.

    call is the instrument's current BUY / WAIT / AVOID, for the call kinds.
    """
    kind = row["kind"]
    threshold = row["threshold"]
    symbol = row["symbol"]
    price = bars.last_price
    close = np.asarray(bars.close, dtype=float)

    def fire(value, message):
        return Fired(int(row["id"]), symbol, kind, threshold, value, message,
                     row["note"] or "", _owner(row), as_of(bars))

    if kind in CALL_KINDS:
        was = row["state"] if "state" in row.keys() else None
        # The first look only learns the call. A change needs a before.
        if not call or not was or call == was:
            return None
        if kind == "call_buy" and call != "BUY":
            return None
        return fire(price, "%s's call changed from %s to %s, at %s."
                    % (symbol, was, call, format_price(price)))

    if kind == "price_above" and price > threshold:
        return fire(price, "%s is %s, above the %s you set."
                    % (symbol, format_price(price), format_price(threshold)))

    if kind == "price_below" and price < threshold:
        return fire(price, "%s is %s, below the %s you set."
                    % (symbol, format_price(price), format_price(threshold)))

    if kind in ("pct_up", "pct_down"):
        # Measured against the previous session close, which is what "up today"
        # means to everybody except a chart with a different anchor.
        base = bars.prev_close
        if not base:
            return None
        move = (price - base) / base
        pct = threshold if threshold and threshold < 1 else (threshold or 0) / 100.0
        if kind == "pct_up" and move >= pct:
            return fire(move, "%s is up %.2f%% since the previous close."
                        % (symbol, move * 100))
        if kind == "pct_down" and move <= -pct:
            return fire(move, "%s is down %.2f%% since the previous close."
                        % (symbol, abs(move) * 100))
        return None

    if kind in ("rsi_above", "rsi_below"):
        rsi = ind.rsi(close, 14)
        if not len(rsi) or not np.isfinite(rsi[-1]):
            return None
        value = float(rsi[-1])
        if kind == "rsi_above" and value > threshold:
            return fire(value, "%s RSI is %.0f, above %.0f." % (symbol, value, threshold))
        if kind == "rsi_below" and value < threshold:
            return fire(value, "%s RSI is %.0f, below %.0f." % (symbol, value, threshold))
        return None

    if kind in ("near_52w_high", "near_52w_low"):
        if len(close) < 30:
            return None
        window = close[-252:] if len(close) >= 252 else close
        pct = threshold if threshold and threshold < 1 else (threshold or 0) / 100.0
        if kind == "near_52w_high":
            high = float(np.max(window))
            gap = (high - price) / high if high else 1.0
            if gap <= pct:
                return fire(gap, "%s at %s is within %.1f%% of its %s high of %s."
                            % (symbol, format_price(price), gap * 100,
                               "52-week" if len(close) >= 252 else "recent",
                               format_price(high)))
        else:
            low = float(np.min(window))
            gap = (price - low) / low if low else 1.0
            if gap <= pct:
                return fire(gap, "%s at %s is within %.1f%% of its %s low of %s."
                            % (symbol, format_price(price), gap * 100,
                               "52-week" if len(close) >= 252 else "recent",
                               format_price(low)))
        return None

    if kind == "volume_spike":
        rvol = ind.relative_volume(np.asarray(bars.volume, dtype=float), 20)
        if not len(rvol) or not np.isfinite(rvol[-1]):
            return None
        value = float(rvol[-1])
        if value >= threshold:
            return fire(value, "%s is trading %.1f times its normal volume."
                        % (symbol, value))
        return None

    if kind in ("crosses_ma", "loses_ma"):
        ma = ind.sma(close, 50)
        if len(ma) < 2 or not np.isfinite(ma[-1]) or not np.isfinite(ma[-2]):
            return None
        # A cross, not a state: the alert is about the moment it happened, so
        # it requires the previous bar to have been on the other side.
        was_above = close[-2] > ma[-2]
        now_above = close[-1] > ma[-1]
        if kind == "crosses_ma" and now_above and not was_above:
            return fire(float(close[-1]),
                        "%s closed at %s, back above its 50-bar average of %s."
                        % (symbol, format_price(close[-1]), format_price(ma[-1])))
        if kind == "loses_ma" and was_above and not now_above:
            return fire(float(close[-1]),
                        "%s closed at %s, losing its 50-bar average of %s."
                        % (symbol, format_price(close[-1]), format_price(ma[-1])))
        return None

    return None


def check(conn, only_active: bool = True, user=None, call_of=None) -> Dict:
    """Check alerts against fresh data and record anything that fired.

    With no user this checks every alert in the database, which is what the
    background loop wants. Pass one to check only that person's, which is what
    a page acting on someone's behalf must do.

    call_of(symbol, interval) returns today's call for the call kinds; without
    it those alerts are skipped. Every fire is also written to the person's
    alert history, and a check that fails is kept on the alert with its reason.
    """
    from . import database as db

    rows = [r for r in db.alert_list(conn, active_only=only_active, user=user)]
    if not rows:
        return {"checked": 0, "fired": [], "errors": [], "when": int(time.time())}

    calls: Dict = {}
    if call_of is not None:
        for key in {(r["symbol"], r["interval"] or "1d") for r in rows
                    if r["kind"] in CALL_KINDS}:
            try:
                calls[key] = call_of(*key)
            except Exception:
                calls[key] = None

    # One fetch per symbol and interval, shared by every alert on it.
    wanted = {(r["symbol"], r["interval"] or "1d") for r in rows}
    data, errors = {}, []

    def grab(key):
        symbol, interval = key
        try:
            return key, fetch_bars(symbol, interval)
        except (DataError, Exception) as exc:
            return key, exc

    with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
        for key, result in pool.map(grab, wanted):
            if isinstance(result, Exception):
                errors.append({"symbol": key[0], "error": str(result)[:120]})
            else:
                data[key] = result

    fired: List[Fired] = []
    failed = {e["symbol"]: e["error"] for e in errors}
    for row in rows:
        key = (row["symbol"], row["interval"] or "1d")
        bars = data.get(key)
        if bars is None:
            db.alert_failed(conn, int(row["id"]), "No price: %s" % failed.get(row["symbol"], "the data source did not answer"))
            continue
        is_call = row["kind"] in CALL_KINDS
        call = calls.get(key) if is_call else None
        if is_call and call is None:
            db.alert_failed(conn, int(row["id"]), "The call could not be worked out just now.")
            continue
        try:
            hit = _evaluate(row, bars, call)
        except Exception as exc:
            errors.append({"symbol": row["symbol"], "error": str(exc)[:120]})
            db.alert_failed(conn, int(row["id"]), str(exc)[:120])
            continue
        if hit:
            db.alert_fire(conn, hit.alert_id, hit.value, hit.message)
            if is_call:
                db.alert_checked(conn, int(row["id"]), state=call)
            if hit.user_id is not None:
                hit.event_id = db.event_add(conn, hit.user_id, hit.kind, hit.symbol,
                                            hit.message, hit.value, hit.as_of,
                                            alert_id=hit.alert_id)
            fired.append(hit)
        else:
            db.alert_checked(conn, int(row["id"]), state=call if is_call else None)

    return {"checked": len(rows), "fired": fired, "errors": errors,
            "when": int(time.time())}


def check_stops(conn, user=None) -> Dict:
    """Tell the owner, once, when an open journal position crosses its stop.

    A stop in the journal is a plan, not an order: nothing closes the trade.
    So the background loop watches every open position and says so the first
    time the price is at or past the stop, with the time of that price. It is
    written to the person's alert history like any alert, and never repeats
    for the same position.
    """
    from . import database as db

    sql, args = db._own("SELECT * FROM trades WHERE status='open' "
                        "AND breach_noticed IS NULL", [], user)
    rows = list(conn.execute(sql, args))
    if not rows:
        return {"checked": 0, "fired": [], "errors": [], "when": int(time.time())}

    data, errors = {}, []
    for symbol in sorted({r["symbol"] for r in rows}):
        try:
            data[symbol] = fetch_bars(symbol, "1d")
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:120]})

    fired: List[Fired] = []
    for row in rows:
        bars = data.get(row["symbol"])
        if bars is None:
            continue
        price, stop, side = float(bars.last_price), float(row["stop"]), int(row["direction"])
        if (price - stop) * side > 0:
            continue
        message = ("%s is at %s, past the stop of %s on your %s position. "
                   "The journal does not close it for you: close it, or set a "
                   "new stop on purpose." % (row["symbol"], format_price(price),
                                             format_price(stop),
                                             "long" if side > 0 else "short"))
        hit = Fired(0, row["symbol"], "stop_breach", stop, price, message, "",
                    _owner(row), as_of(bars), int(row["id"]))
        conn.execute("UPDATE trades SET breach_noticed=? WHERE id=?",
                     (int(time.time()), int(row["id"])))
        conn.commit()
        if hit.user_id is not None:
            hit.event_id = db.event_add(conn, hit.user_id, "stop_breach", hit.symbol,
                                        message, price, hit.as_of, trade_id=hit.trade_id)
        fired.append(hit)
    return {"checked": len(rows), "fired": fired, "errors": errors,
            "when": int(time.time())}


def deliver(conn, fired: List[Fired], send, address_of) -> Dict:
    """Email each person what fired for them this round, in one message.

    send(to, subject, body) is the mail function; address_of(user_id) returns
    an address for someone who asked for alert emails, or None. The outcome,
    sent or the error, is written against each event, so a failure is on
    record rather than silent.
    """
    from . import database as db

    by_user: Dict[int, List[Fired]] = {}
    for hit in fired:
        if hit.user_id is not None:
            by_user.setdefault(hit.user_id, []).append(hit)
    sent, failed = 0, 0
    for user_id, hits in by_user.items():
        to = address_of(user_id)
        if not to:
            continue
        events = [h.event_id for h in hits if h.event_id is not None]
        subject = ("Orenth: %s" % hits[0].message[:60] if len(hits) == 1
                   else "Orenth: %d alerts" % len(hits))
        body = "\n\n".join("%s\n(price as of %s)" % (h.message, h.as_of or "unknown")
                            for h in hits)
        body += ("\n\nSee them all on your Monitor page. You get these because "
                 "you asked for alert emails; switch them off on the Monitor page.\n")
        try:
            send(to, subject, body)
            db.event_emailed(conn, events)
            sent += 1
        except Exception as exc:
            db.event_emailed(conn, events, error=str(exc) or type(exc).__name__)
            failed += 1
    return {"sent": sent, "failed": failed}


def suggest_for(symbol: str, plan=None, bars=None) -> List[Dict]:
    """Alerts worth setting for an instrument, given a plan if there is one.

    Suggestions are concrete rather than generic: if a plan says wait for a
    price, the obvious alert is that exact price.
    """
    out: List[Dict] = []
    if plan is not None and getattr(plan, "entry", None):
        if plan.direction > 0:
            out.append({"kind": "price_below", "threshold": plan.entry,
                        "why": "Tells you when price reaches the entry the plan "
                               "is waiting for."})
        else:
            out.append({"kind": "price_above", "threshold": plan.entry,
                        "why": "Tells you when price reaches the short entry."})
        if getattr(plan, "stop", None):
            out.append({"kind": "price_below" if plan.direction > 0 else "price_above",
                        "threshold": plan.stop,
                        "why": "Tells you if the level that invalidates the idea "
                               "is breached."})

    if bars is not None:
        out.append({"kind": "pct_down", "threshold": 0.05,
                    "why": "A 5% single-day fall usually means news you have "
                           "not read yet."})
        out.append({"kind": "volume_spike", "threshold": 3.0,
                    "why": "Three times normal volume means something changed."})
        out.append({"kind": "near_52w_high", "threshold": 0.02,
                    "why": "Approaching the 52-week high, where breakouts and "
                           "rejections both happen."})
    return out
