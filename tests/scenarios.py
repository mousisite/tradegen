"""Made-up bars for the price-structure tests, and scenes with a known answer.

Shared by test_structure.py, test_clock.py and test_bias.py, and meant for any
suite that needs a sweep, a break of structure or a gap at a known bar (the
strategy tests). No network. Each scene's docstring says which bar shows what;
the tests assert exactly that.

Times are real ones: a stock's bars run 09:30 to its close on the days the
NYSE trades, a future's from 18:00 New York to 17:00 the next day, Sunday
evening to Friday, and a coin's round the clock.
"""
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from bot import clock, sessions  # noqa: E402
from bot.market import Bars  # noqa: E402

DAY = 86400
_ASSET = {"stock": "equity", "future": "other", "coin": "crypto"}
_SYMBOL = {"stock": "TEST", "future": "ES=F", "coin": "BTC-USD"}
_META = {"stock": {"instrumentType": "EQUITY", "exchangeTimezoneName": "America/New_York"},
         "future": {"instrumentType": "FUTURE", "exchangeTimezoneName": "America/New_York"},
         "coin": {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC"}}


def make(o, h, l, c, ts, kind="stock", interval="5m", symbol=None, volume=None):
    """Bars from columns, with the metadata market.fetch_bars would give."""
    o, h, l, c = (np.asarray(x, dtype=float) for x in (o, h, l, c))
    ts = np.asarray(ts, dtype=np.int64)
    n = len(c)
    gmt = 0 if kind == "coin" else -18000
    return Bars(symbol=symbol or _SYMBOL[kind], name="Test", asset_class=_ASSET[kind],
                currency="USD", exchange="TEST", interval=interval, timestamp=ts,
                open=o, high=h, low=l, close=c,
                volume=np.full(n, 1e5) if volume is None else np.asarray(volume, float),
                session_id=((ts + gmt) // DAY).astype(np.int64), gmt_offset=gmt,
                prev_close=float(c[-2]) if n > 1 else None, live_price=None,
                market_open=False, last_bar_time="", meta=dict(_META[kind]))


def rows(table, ts=None, kind="stock", interval="5m", symbol=None):
    """Bars from (open, high, low, close) rows, on a stock's 5-minute times."""
    o, h, l, c = zip(*table)
    if ts is None:
        ts = stock_times(len(table))
    return make(o, h, l, c, ts, kind=kind, interval=interval, symbol=symbol)


# --- times ---------------------------------------------------------------------

def stock_times(n, start=date(2026, 9, 1), step=300):
    """Start times of n bars of a US stock, 09:30 to each day's close."""
    out = []
    d = start
    while len(out) < n:
        hours = sessions.us_session(d)
        if hours:
            t = hours[0]
            while t < hours[1] and len(out) < n:
                out.append(t)
                t += step
        d += timedelta(days=1)
    return np.array(out, dtype=np.int64)


def futures_times(n, start=date(2026, 9, 13), step=300):
    """Start times of n bars of a future: each trading day runs from 18:00 New
    York to 17:00 the next day, Sunday evening to Friday, with the hour's pause
    at 17:00. `start` is the Sunday it opens on."""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() in (6, 0, 1, 2, 3):           # opens Sunday to Thursday evening
            nd = d + timedelta(days=1)
            t = clock.ny(d.year, d.month, d.day, 18, 0)
            end = clock.ny(nd.year, nd.month, nd.day, 17, 0)
            while t < end and len(out) < n:
                out.append(t)
                t += step
        d += timedelta(days=1)
    return np.array(out, dtype=np.int64)


def coin_times(n, start=None, step=300):
    """Start times of n bars of a coin, round the clock."""
    start = clock.ny(2026, 9, 13, 18, 0) if start is None else start
    return start + step * np.arange(n, dtype=np.int64)


def daily_times(n, start=date(2021, 11, 22)):
    """Daily bars of a stock, stamped at the 09:30 open as Yahoo stamps them."""
    out = []
    d = start
    while len(out) < n:
        hours = sessions.us_session(d)
        if hours:
            out.append(hours[0])
        d += timedelta(days=1)
    return np.array(out, dtype=np.int64)


# --- random walks with a little structure --------------------------------------------

def walk(ts, seed=1, kind="stock", interval="5m", price=100.0, vol=0.0015, jumps=None,
         symbol=None):
    """A random walk whose drift turns every few dozen bars, so it has swings,
    breaks, gaps and sweeps to find. Bars after a pause (a new day, a
    weekend) open with a small jump. jumps maps a bar position to a factor
    applied from that bar on (a futures roll)."""
    rng = np.random.RandomState(seed)
    ts = np.asarray(ts, dtype=np.int64)
    n = len(ts)
    legs = np.cumsum(rng.randint(20, 90, size=n // 20 + 2))
    leg_of = np.searchsorted(legs, np.arange(n), side="right")
    drift = np.where(leg_of % 2 == 0, 1.0, -1.0) * vol * 0.25
    rets = drift + rng.normal(0.0, vol, n)
    close = price * np.exp(np.cumsum(rets))
    prev = np.concatenate([[price], close[:-1]])
    step = np.diff(ts, prepend=ts[0])
    usual = np.median(step[1:]) if n > 1 else 0
    after_pause = step > usual * 1.5
    open_ = prev * np.exp(np.where(after_pause, rng.normal(0.0, vol * 2.0, n),
                                   rng.normal(0.0, vol * 0.15, n)))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, vol * 0.5, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, vol * 0.5, n)))
    for at, factor in sorted((jumps or {}).items()):
        open_[at:] *= factor
        high[at:] *= factor
        low[at:] *= factor
        close[at:] *= factor
    return make(open_, high, low, close, ts, kind=kind, interval=interval, symbol=symbol)


def daily_series(n=1250, seed=11):
    """A stock's daily bars, five years or so."""
    return walk(daily_times(n), seed=seed, interval="1d", vol=0.015)


def stock_series(n=4000, seed=12):
    """A stock's 5-minute bars, 09:30 to the close, through the November clock change."""
    return walk(stock_times(n), seed=seed, vol=0.0015)


def futures_series(n=17000, seed=13, roll_day=30):
    """A future's 5-minute bars round the clock, the daily pause and weekends,
    through the November clock change, with a contract roll's jump at the
    open of trading day `roll_day` (None for none)."""
    ts = futures_times(n)
    jumps = {}
    starts = np.flatnonzero(np.diff(ts) > 600) + 1
    if roll_day is not None and len(starts) >= roll_day:
        jumps[int(starts[roll_day - 1])] = 1.012
    return walk(ts, seed=seed, kind="future", vol=0.0008, price=6000.0, jumps=jumps)


def coin_series(n=17000, seed=14):
    return walk(coin_times(n), seed=seed, kind="coin", vol=0.0015, price=90000.0)


# --- scenes with a known answer (k = 2) --------------------------------------------------

def swing_scene():
    """A swing high of 10.9 at bar 2, known at bar 4 (k = 2 bars later)."""
    return rows([
        (10.0, 10.2, 9.8, 10.1),
        (10.1, 10.4, 10.0, 10.3),
        (10.3, 10.9, 10.2, 10.8),     # 2: the swing high
        (10.8, 10.85, 10.4, 10.5),
        (10.5, 10.6, 10.1, 10.2),     # 4: it is known here
        (10.2, 10.4, 10.0, 10.3),
    ])


def trend_scene():
    """The trend and its breaks.

    Swing lows 99.0 (bar 2, known at 4), 100.4 (9, known at 11), 101.5 (16,
    known at 18); swing highs 102.5 (6, known at 8), 104.0 (13, known at 15),
    103.0 (18, known at 20).
    11: the close of 102.7 above 102.5 sets the trend up (not a flip); the
        reference low is 100.4.
    18: the swing low 101.5 is confirmed and becomes the reference.
    19: a wick to 101.2, below 101.5, closing 101.6 above it: a sweep that
        came back (st_sweep +1, st_sweep_back 1), not a flip.
    20: a close of exactly 101.5: not a flip.
    21: a close of 101.1, below 101.5: the trend flips down; the next flip is
        measured from the latest swing high, 103.0 (not 104.0).
    23: a wick below 100.4 takes it and closes back above.
    24: a close of 103.1 above 103.0 flips the trend up; the reference low is
        the latest confirmed swing low, 101.5.
    25: a lower swing low, 100.2 (bar 23), is confirmed and replaces it.
    """
    return rows([
        (100.0, 100.5, 99.5, 100.2),
        (100.2, 100.8, 99.8, 100.6),
        (100.6, 101.0, 99.0, 99.4),       # 2: swing low 99.0
        (99.4, 100.2, 99.3, 100.0),
        (100.0, 101.5, 99.9, 101.3),      # 4: 99.0 known
        (101.3, 102.0, 101.1, 101.8),
        (101.8, 102.5, 101.6, 102.3),     # 6: swing high 102.5
        (102.3, 102.4, 101.2, 101.4),
        (101.4, 101.6, 100.6, 100.8),     # 8: 102.5 known
        (100.8, 101.0, 100.4, 100.7),     # 9: swing low 100.4
        (100.7, 101.9, 100.6, 101.7),
        (101.7, 102.8, 101.5, 102.7),     # 11: 100.4 known; close above 102.5: trend up
        (102.7, 103.5, 102.5, 103.3),
        (103.3, 104.0, 103.1, 103.8),     # 13: swing high 104.0
        (103.8, 103.9, 102.9, 103.0),
        (103.0, 103.2, 101.9, 102.1),     # 15: 104.0 known
        (102.1, 102.3, 101.5, 101.7),     # 16: swing low 101.5
        (101.7, 102.6, 101.6, 102.4),
        (102.4, 103.0, 102.2, 102.8),     # 18: 101.5 known; swing high 103.0
        (102.8, 102.9, 101.2, 101.6),     # 19: wick below 101.5, close above
        (101.6, 102.0, 101.3, 101.5),     # 20: close exactly 101.5; 103.0 known
        (101.5, 101.7, 100.9, 101.1),     # 21: close below 101.5: flip down
        (101.1, 101.4, 100.5, 100.7),
        (100.7, 101.0, 100.2, 100.5),     # 23: takes 100.4; swing low 100.2
        (100.5, 103.2, 100.4, 103.1),     # 24: close above 103.0: flip up
        (103.1, 103.6, 102.6, 103.4),     # 25: 100.2 known: the new reference
        (103.4, 103.8, 103.0, 103.6),
    ])


def gap_scene(middle="up", touching=False):
    """A three-bar gap up at bar 2, zone 10.2 to 10.4.

    middle="down" makes the middle candle a down one: then no gap.
    touching=True puts bar 2's low exactly on bar 0's high: then no gap.
    3: a wick to 10.15 through the gap, closing 10.5: the first touch, and the
       gap stays (st_fvg_touch +1).
    4: a close of 10.1, below the gap's bottom: the gap is gone.
    """
    mid = (10.1, 10.8, 10.1, 10.7) if middle == "up" else (10.7, 10.8, 10.1, 10.1)
    low2 = 10.2 if touching else 10.4
    return rows([
        (10.0, 10.2, 9.9, 10.1),
        mid,
        (10.7, 11.0, low2, 10.9),         # 2: the gap's third candle
        (10.9, 11.1, 10.15, 10.5),        # 3: wick through, close inside
        (10.5, 10.6, 10.0, 10.1),         # 4: close through the bottom
        (10.1, 10.3, 9.9, 10.2),
    ])


def gap_used_scene():
    """A gap up at bar 2 (10.2 to 10.4); a swing high of 11.3 forms after it
    (bar 3, known at 5); bar 7 closes above 11.3, so the gap is used up."""
    return rows([
        (10.0, 10.2, 9.9, 10.1),
        (10.1, 10.8, 10.1, 10.7),
        (10.7, 11.0, 10.4, 10.9),         # 2: gap 10.2-10.4
        (10.9, 11.3, 10.8, 11.2),         # 3: swing high 11.3
        (11.2, 11.25, 10.9, 11.0),
        (11.0, 11.1, 10.6, 10.7),         # 5: 11.3 known
        (10.7, 11.2, 10.6, 11.1),
        (11.1, 11.5, 11.0, 11.4),         # 7: close above 11.3: the gap ends
        (11.4, 11.6, 11.2, 11.5),
    ])


def stack_scene():
    """The trend turns up at bar 7 (close 10.95 above the swing high 10.9).
    Two gaps up in a stack: bar 7 (10.2 to 10.4) and bar 8 (10.6 to 10.9).
    10: a close of 10.55 below the upper gap's bottom (10.6): not an inverse gap.
    11: a close of 10.1 below the lower gap's bottom (10.2): an inverse gap
        (st_ifvg -1). The trend stays up: only a flip changes it."""
    return rows([
        (10.0, 10.3, 9.8, 10.2),
        (10.2, 10.6, 10.1, 10.5),
        (10.5, 10.9, 10.3, 10.4),         # 2: swing high 10.9
        (10.4, 10.5, 10.0, 10.1),
        (10.1, 10.4, 9.7, 9.9),           # 4: 10.9 known
        (9.9, 10.2, 9.6, 10.1),           # 5: swing low 9.6
        (10.1, 10.6, 10.0, 10.5),
        (10.5, 11.0, 10.4, 10.95),        # 7: 9.6 known; trend up; gap 10.2-10.4
        (10.95, 11.6, 10.9, 11.5),        # 8: gap 10.6-10.9, same stack
        (11.5, 11.8, 10.95, 11.7),
        (11.7, 11.75, 10.5, 10.55),       # 10: below 10.6 only
        (10.55, 10.6, 10.0, 10.1),        # 11: below 10.2: inverse gap
        (10.1, 10.4, 10.0, 10.3),
    ])


def half_scene():
    """The 50% line. Trend up from bar 7; the leg runs from the swing low 9.6
    (bar 5). Bar 8 makes the high, 11.4, with a down candle: not locked yet,
    since the lock needs a down candle after the high. Bar 9 is a down candle:
    the line locks at (9.6 + 11.4) / 2 = 10.5. Bar 10 dips to 10.4: the first
    touch (st_eq_touch +1); bar 11 dips again: no second touch. Bar 13
    confirms a newer swing low, 10.3 (bar 11), which becomes the leg's low."""
    return rows([
        (10.0, 10.3, 9.8, 10.2),
        (10.2, 10.6, 10.1, 10.5),
        (10.5, 10.9, 10.3, 10.4),         # 2: swing high 10.9
        (10.4, 10.5, 10.0, 10.1),
        (10.1, 10.4, 9.7, 9.9),
        (9.9, 10.2, 9.6, 10.1),           # 5: swing low 9.6
        (10.1, 10.6, 10.0, 10.5),
        (10.5, 11.0, 10.45, 10.95),       # 7: 9.6 known; trend up
        (11.35, 11.4, 10.9, 11.0),        # 8: the high, on a down candle
        (11.0, 11.05, 10.85, 10.9),       # 9: a down candle after it: locked at 10.5
        (10.9, 11.0, 10.4, 10.45),        # 10: dips to 10.4: first touch
        (10.45, 10.8, 10.3, 10.7),        # 11: dips again: no second touch
        (10.7, 11.0, 10.6, 10.9),
        (10.9, 11.3, 10.8, 11.2),         # 13: swing low 10.3 known
        (11.2, 11.3, 11.0, 11.1),
    ])


def equal_scene(apart=0.05):
    """Twenty quiet bars make ATR(14) exactly 1.0. Swing highs at 101.0 (bar
    21, known at 23) and 101.0 - apart (bar 25, known at 27): within 0.1 ATR
    they are equal highs, with st_eqh 101.0 from bar 27. Bar 28 trades to 101.2
    and takes both at once (st_sweep -1, two levels, equal); bar 29 trades
    higher again and takes nothing: a level is taken once."""
    quiet = [(100.0, 100.5, 99.5, 100.0)] * 20
    second = 101.0 - apart
    # Every bar's true range is exactly 1.0, so ATR stays 1.0 to bar 27.
    return rows(quiet + [
        (100.0, 100.6, 99.6, 100.4),
        (100.4, 101.0, 100.0, 100.8),     # 21: the first high
        (100.8, 100.9, 99.9, 100.2),
        (100.2, 100.5, 99.5, 99.8),       # 23: known
        (99.8, 100.4, 99.4, 100.2),
        (100.2, second, second - 1.0, second - 0.25),   # 25: the second high
        (second - 0.25, second - 0.05, second - 1.05, second - 0.85),
        (second - 0.85, 100.4, 99.4, 99.6),             # 27: known
        (99.6, 101.2, 99.6, 100.0),       # 28: takes both
        (100.0, 101.3, 99.9, 100.1),      # 29: nothing left to take
        (100.1, 100.4, 99.6, 99.9),
    ])


def chain_scene(broken=False):
    """The course's full model, rising.

    A fall: the trend turns down at bar 11. Bar 24 wicks to 99.8 below the
    swing low 100.2 (bar 19) and closes back above it: the take. Bar 26 closes
    at 101.7, above the swing high 101.4: the break of structure (a flip, and
    an inverse gap), with a new gap up (100.5 to 100.8); bar 27 adds one (101.0
    to 101.6). Bar 28 is a down candle: the move's halfway line is fixed. Bar
    29 dips into the gap; bar 30 dips in again and closes up: lq_chain +1 at
    bar 30, stop 99.8, through the gap. lq_asf +1 at bar 26.

    broken=True: bar 28 drops to 99.5 instead, below the take's low, so the
    chain is cancelled and never fires.
    """
    table = [
        (105.0, 105.5, 104.5, 105.2),
        (105.2, 106.0, 105.0, 105.8),
        (105.8, 106.5, 105.6, 106.0),     # 2: swing high 106.5
        (106.0, 106.2, 104.8, 105.0),
        (105.0, 105.3, 103.8, 104.0),
        (104.0, 104.3, 103.0, 103.2),
        (103.2, 103.5, 102.5, 102.7),     # 6: swing low 102.5
        (102.7, 103.9, 102.6, 103.7),
        (103.7, 104.4, 103.5, 104.2),     # 8: 102.5 known
        (104.2, 104.6, 103.9, 104.1),     # 9: swing high 104.6
        (104.1, 104.3, 103.2, 103.4),
        (103.4, 103.6, 102.2, 102.3),     # 11: below 102.5: trend down
        (102.3, 102.6, 101.6, 101.8),
        (101.8, 102.0, 101.0, 101.2),     # 13: swing low 101.0
        (101.2, 101.9, 101.1, 101.7),
        (101.7, 102.4, 101.5, 102.2),
        (102.2, 102.5, 101.9, 102.0),     # 16: swing high 102.5
        (102.0, 102.1, 101.4, 101.5),
        (101.5, 101.6, 100.7, 100.8),     # 18: takes 101.0
        (100.8, 101.0, 100.2, 100.4),     # 19: swing low 100.2
        (100.4, 100.9, 100.3, 100.8),
        (100.8, 101.3, 100.6, 101.2),
        (101.2, 101.4, 100.9, 101.0),     # 22: swing high 101.4
        (101.0, 101.1, 100.3, 100.4),
        (100.4, 100.5, 99.8, 100.3),      # 24: the take: wick below 100.2, close above
        (100.3, 101.0, 100.2, 100.9),
        (100.9, 101.8, 100.8, 101.7),     # 26: close above 101.4: the break
        (101.7, 102.3, 101.6, 102.2),     # 27: a second gap up
        (102.2, 102.25, 101.7, 101.8),    # 28: down candle: the line is fixed
        (101.8, 101.85, 101.3, 101.4),    # 29: into the gap, down
        (101.4, 101.9, 101.2, 101.8),     # 30: into the gap, closes up: the chain
        (101.8, 102.4, 101.7, 102.3),
    ]
    if broken:
        table[28] = (102.2, 102.25, 99.5, 99.7)
        table[29] = (99.7, 100.0, 99.6, 99.9)
        table[30] = (99.9, 100.1, 99.7, 100.0)
        table[31] = (100.0, 100.2, 99.8, 100.1)
    return rows(table)
