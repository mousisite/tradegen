"""The course's liquidity model, found in prices: taken, confirmed, retested.

TJR's video teaches one chain above everything else (2:44:58 "potential,
confirmation, continuation"; 3:38:30-3:42:47; 4:02:52-4:11:09; 6:24:12-6:30:28):

1. an old high or low is taken (a swing point, yesterday's high or low, or an
   earlier session's range),
2. price then breaks its last swing the other way, or closes through a gap the
   move left (the confirmation),
3. it pulls back into a gap made after the take, or to the halfway line of the
   new move,
4. and a candle turns back in the new direction there.

series() adds the lq_* keys to structure.series() and clock.series(); events()
says what happened lately in plain words, for the analysis page.
As everywhere in this family of modules, the value at bar i uses bars 0..i
only. The sign convention is the strategies': +1 means the setup expects the
price to rise (lows were taken), -1 that it expects a fall.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import numpy as np

from . import clock, structure
from .market import format_price

# app choice: the confirmation must come within this many bars of the take,
# and the pullback within this many bars of the confirmation. Two hours and
# three hours on 5-minute bars; two and three weeks on daily ones.
SWEEP_TO_CONFIRM = {"intraday": 24, "daily": 10}
CONFIRM_TO_RETRACE = {"intraday": 36, "daily": 15}

# lq_take_src codes, by name.
SOURCES = ("", "swing", "prior_day", "session", "equal")
_SRC_SWING, _SRC_PRIOR, _SRC_SESSION, _SRC_EQUAL = 1, 2, 3, 4
# app choice: when one bar takes several kinds of level, the one named is the
# most widely watched: yesterday's, then a session's, then equal ones, then a swing.
_PRIORITY = (_SRC_PRIOR, _SRC_SESSION, _SRC_EQUAL, _SRC_SWING)

# lq_chain_via codes.
VIA = ("", "gap", "half")

EVENT_KEYS = ("lq_take", "lq_take_both", "lq_take_back", "lq_take_src", "lq_asf",
              "lq_chain", "lq_chain_conf", "lq_chain_via")
INDEX_KEYS = ("lq_asf_sweep_at", "lq_chain_sweep_at", "lq_chain_conf_at")
PRICE_KEYS = ("lq_take_lvl", "lq_asf_stop", "lq_chain_stop")
KEYS = EVENT_KEYS + INDEX_KEYS + PRICE_KEYS

NAN = float("nan")


def series(bars, k: int = structure.K) -> Dict[str, np.ndarray]:
    """structure.series, clock.series and the lq_* keys, in one dict.

    lq_take       +1 lows taken / -1 highs taken: the union of swing sweeps,
                  yesterday's high or low and a session's range (0 when one bar
                  took both sides: lq_take_both)
    lq_take_lvl   the level named (see _PRIORITY), lq_take_src its kind
                  (SOURCES), lq_take_back 1 when the close came back inside
                  every level the bar took
    lq_asf        +1/-1 a break of structure in the direction of a take within
                  SWEEP_TO_CONFIRM bars, the take's extreme not exceeded since;
                  lq_asf_stop that extreme, lq_asf_sweep_at the take's bar
    lq_chain      +1/-1 the four steps complete; lq_chain_stop the extreme of
                  the take, lq_chain_via 1 (a gap) or 2 (the halfway line),
                  lq_chain_sweep_at, lq_chain_conf_at the steps' bars;
                  lq_chain_conf marks each confirmation (step 2)
    """
    s = structure.series(bars, k)
    s.update(clock.series(bars))
    s.update(_chain(bars, s, clock.kind_of(bars) == "daily"))
    return s


def empty(n: int) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}
    for key in EVENT_KEYS:
        out[key] = np.zeros(n, dtype=np.int8)
    for key in INDEX_KEYS:
        out[key] = np.full(n, -1, dtype=np.int64)
    for key in PRICE_KEYS:
        out[key] = np.full(n, np.nan, dtype=float)
    return out


def _chain(bars, s: Dict[str, np.ndarray], daily: bool) -> Dict[str, np.ndarray]:
    n = len(bars.close)
    out = empty(n)
    if n == 0:
        return out
    O = np.asarray(bars.open, dtype=float).tolist()
    H = np.asarray(bars.high, dtype=float).tolist()
    L = np.asarray(bars.low, dtype=float).tolist()
    C = np.asarray(bars.close, dtype=float).tolist()
    sw, sw_both = s["st_sweep"].tolist(), s["st_sweep_both"].tolist()
    sw_lvl, sw_eq = s["st_sweep_lvl"].tolist(), s["st_sweep_eq"].tolist()
    sw_eq_lvl = s["st_sweep_eq_lvl"].tolist()
    pd_take, pd_both = s["ck_pd_take"].tolist(), s["ck_pd_both"].tolist()
    pdh, pdl = s["ck_pdh"].tolist(), s["ck_pdl"].tolist()
    se_take, se_both, se_lvl = (s["ck_sess_take"].tolist(), s["ck_sess_both"].tolist(),
                                s["ck_sess_lvl"].tolist())
    flip, ifvg = s["st_flip"].tolist(), s["st_ifvg"].tolist()
    up_top, up_bot, up_at = (s["st_fvg_up_top"].tolist(), s["st_fvg_up_bot"].tolist(),
                             s["st_fvg_up_at"].tolist())
    dn_top, dn_bot, dn_at = (s["st_fvg_dn_top"].tolist(), s["st_fvg_dn_bot"].tolist(),
                             s["st_fvg_dn_at"].tolist())
    to_confirm = SWEEP_TO_CONFIRM["daily" if daily else "intraday"]
    to_retrace = CONFIRM_TO_RETRACE["daily" if daily else "intraday"]

    take = [0] * n
    take_both = [0] * n
    take_back = [0] * n
    take_src = [0] * n
    take_lvl = [NAN] * n
    asf = [0] * n
    asf_stop = [NAN] * n
    asf_at = [-1] * n
    chain = [0] * n
    chain_stop = [NAN] * n
    chain_via = [0] * n
    chain_sweep = [-1] * n
    chain_conf = [0] * n
    chain_conf_at = [-1] * n

    # Sweep-then-flip, each way: [take bar, extreme, still whole].
    asf_state = {1: [-1, NAN, False], -1: [-1, NAN, False]}
    # The chain, each way: [step, take bar, extreme, confirmation bar,
    # leg extreme, its bar, leg fixed].
    ch = {1: [0, -1, NAN, -1, NAN, -1, False], -1: [0, -1, NAN, -1, NAN, -1, False]}

    for i in range(n):
        hi_i, lo_i, cl_i, op_i = H[i], L[i], C[i], O[i]

        # --- step 1: something was taken -------------------------------------
        # video 1:36:14 (swings), 2:05:49 (yesterday's), 2:02:39 (sessions).
        ups: List = []
        dns: List = []
        clash = bool(sw_both[i] or pd_both[i] or se_both[i])
        if sw[i]:
            side_list = ups if sw[i] == 1 else dns
            side_list.append((sw_lvl[i], _SRC_SWING))
            if sw_eq[i]:
                side_list.append((sw_eq_lvl[i], _SRC_EQUAL))
        if pd_take[i] == 1:
            ups.append((pdl[i], _SRC_PRIOR))
        elif pd_take[i] == -1:
            dns.append((pdh[i], _SRC_PRIOR))
        if se_take[i] == 1:
            ups.append((se_lvl[i], _SRC_SESSION))
        elif se_take[i] == -1:
            dns.append((se_lvl[i], _SRC_SESSION))
        d = 0
        if clash or (ups and dns):
            take_both[i] = 1
        elif ups or dns:
            d = 1 if ups else -1
            got = ups or dns
            named = min(got, key=lambda x: _PRIORITY.index(x[1]))
            take[i], take_lvl[i], take_src[i] = d, named[0], named[1]
            if d == 1:
                take_back[i] = 1 if cl_i > max(x[0] for x in got) else 0
            else:
                take_back[i] = 1 if cl_i < min(x[0] for x in got) else 0

        # --- a take, then a break of structure the other way --------------------
        # video 2:42:53-2:43:56: the break that counts follows a take, and
        # the take's extreme has held since.
        for side in (1, -1):
            st = asf_state[side]
            low_side = side == 1
            if d == side:
                st[0], st[1], st[2] = i, (lo_i if low_side else hi_i), True
            elif st[2] and ((lo_i < st[1]) if low_side else (hi_i > st[1])):
                st[2] = False
            if flip[i] == side and st[2] and i - st[0] <= to_confirm:
                asf[i], asf_stop[i], asf_at[i] = side, st[1], st[0]
                st[2] = False

        # --- the four steps, each way ---------------------------------------------
        # video 2:44:58 (potential, confirmation, continuation), 4:02:52-4:11:09
        # and 6:24:12-6:30:28 (taken, confirmed, continued; the stop past the
        # high or low that was taken).
        for side in (1, -1):
            c = ch[side]
            up = side == 1
            # Cancelled: past the time limits, the take's extreme broken, or
            # a break of structure against it.
            if c[0] == 1 and i - c[1] > to_confirm:
                c[0] = 0
            if c[0] == 2:
                broke = (lo_i < c[2]) if up else (hi_i > c[2])
                if broke or flip[i] == -side or i - c[3] > to_retrace:
                    c[0] = 0

            # Step 4: back into the zone, and a candle turning (state as of
            # the bar before, so this bar's own high cannot place the line).
            if c[0] == 2 and i > c[3]:
                via = 0
                if up:
                    turns = cl_i > op_i and lo_i > c[2]
                    if turns and up_at[i - 1] > c[1] and lo_i <= up_top[i - 1] \
                            and cl_i > up_bot[i - 1]:
                        via = 1
                    elif turns and c[6]:
                        half = (c[2] + c[4]) / 2.0
                        if lo_i <= half and cl_i > half:
                            via = 2
                else:
                    turns = cl_i < op_i and hi_i < c[2]
                    if turns and dn_at[i - 1] > c[1] and hi_i >= dn_bot[i - 1] \
                            and cl_i < dn_top[i - 1]:
                        via = 1
                    elif turns and c[6]:
                        half = (c[2] + c[4]) / 2.0
                        if hi_i >= half and cl_i < half:
                            via = 2
                if via:
                    chain[i], chain_stop[i], chain_via[i] = side, c[2], via
                    chain_sweep[i], chain_conf_at[i] = c[1], c[3]
                    c[0] = 0                     # one signal per take

            # Step 1 again: a take starts a chain, or a further take past the
            # extreme starts it over. app choice: a take that does not go past
            # the extreme leaves a chain in progress alone, and the new move's
            # leg is measured from the extreme, where the move began.
            moved = False
            extreme = lo_i if up else hi_i
            beyond = (extreme < c[2]) if up else (extreme > c[2])
            if d == side and (c[0] == 0 or beyond):
                c[:] = [1, i, extreme, -1, hi_i if up else lo_i, i, False]
            elif c[0] == 1 and beyond:
                # A lower low before the confirmation moves the extreme.
                c[2] = extreme
                c[4], c[5], c[6] = (hi_i if up else lo_i), i, False
                moved = True

            # Step 2: the confirmation, on a bar that did not go past the extreme.
            if c[0] == 1 and not moved and (flip[i] == side or ifvg[i] == side) \
                    and ((lo_i >= c[2]) if up else (hi_i <= c[2])):
                c[0], c[3] = 2, i
                chain_conf[i] = side

            # The new move's extreme, and the candle that fixes its halfway line.
            if c[0] in (1, 2) and c[5] != i:
                if up:
                    if hi_i > c[4]:
                        c[4], c[5], c[6] = hi_i, i, False
                    elif not c[6] and cl_i < op_i:
                        c[6] = True
                else:
                    if lo_i < c[4]:
                        c[4], c[5], c[6] = lo_i, i, False
                    elif not c[6] and cl_i > op_i:
                        c[6] = True

    out["lq_take"] = np.asarray(take, dtype=np.int8)
    out["lq_take_both"] = np.asarray(take_both, dtype=np.int8)
    out["lq_take_back"] = np.asarray(take_back, dtype=np.int8)
    out["lq_take_src"] = np.asarray(take_src, dtype=np.int8)
    out["lq_take_lvl"] = np.asarray(take_lvl, dtype=float)
    out["lq_asf"] = np.asarray(asf, dtype=np.int8)
    out["lq_asf_stop"] = np.asarray(asf_stop, dtype=float)
    out["lq_asf_sweep_at"] = np.asarray(asf_at, dtype=np.int64)
    out["lq_chain"] = np.asarray(chain, dtype=np.int8)
    out["lq_chain_stop"] = np.asarray(chain_stop, dtype=float)
    out["lq_chain_via"] = np.asarray(chain_via, dtype=np.int8)
    out["lq_chain_sweep_at"] = np.asarray(chain_sweep, dtype=np.int64)
    out["lq_chain_conf"] = np.asarray(chain_conf, dtype=np.int8)
    out["lq_chain_conf_at"] = np.asarray(chain_conf_at, dtype=np.int64)
    return out


# --- In words -----------------------------------------------------------------

def level_name(s: Dict[str, np.ndarray], i: int) -> str:
    """What the take at bar i took, as a person would say it."""
    d = int(s["lq_take"][i])
    src = SOURCES[int(s["lq_take_src"][i])] if d else ""
    side = "low" if d == 1 else "high"
    if src == "prior_day":
        return "yesterday's %s" % side
    if src == "session":
        which = clock.SESS_SOURCES[int(s["ck_sess_src"][i])] or "london"
        return "the %s session's %s" % ("London" if which == "london" else "Asia", side)
    if src == "equal":
        return "equal %ss" % side
    count = int(s["st_sweep_n"][i]) if "st_sweep_n" in s else 1
    if count > 1:
        return "%d earlier swing %ss" % (count, side)
    return "an earlier swing %s" % side


def _when(bars, s: Dict[str, np.ndarray], i: int, daily: bool) -> str:
    if daily:
        day = date.fromordinal(int(s["ck_day"][i]))
        return "on %d %s" % (day.day, clock._MONTHS[day.month - 1])
    stamp = clock.label_ny(int(bars.timestamp[i]))
    day, _, hm = stamp.rpartition(" ")
    return "on %s at %s New York time" % (day, hm)


def _at(s, key, i):
    v = float(s[key][i]) if i >= 0 else NAN
    return v


def events(bars, s: Dict[str, np.ndarray], since: Optional[int] = None,
           limit: int = 20) -> List[Dict]:
    """What price did lately, newest first, as facts in plain words.

    Each event is {"kind", "dir", "at" (the bar's start time, UTC seconds),
    "i" (its position), "price" (the level involved), "level_name", "close",
    "words"}. Kinds: "take", "flip", "break", "gap_touch", "inverse_gap",
    "half_touch", "sweep_flip", "chain", "gap_fill". `since` keeps only bars
    that started after that time. Nothing here forecasts: each sentence says
    what the price did and where.
    """
    n = len(bars.close)
    if n == 0:
        return []
    daily = clock.kind_of(bars) == "daily"
    ts = np.asarray(bars.timestamp, dtype=np.int64)
    first = 0 if since is None else int(np.searchsorted(ts, int(since), side="right"))

    def fp(v):
        return format_price(v)

    out: List[Dict] = []
    for i in range(n - 1, first - 1, -1):
        when = None
        close = float(bars.close[i])
        found: List[Dict] = []

        def add(kind, d, price, name, words):
            found.append({"kind": kind, "dir": int(d), "at": int(ts[i]), "i": i,
                          "price": float(price) if price == price else None,
                          "level_name": name, "close": close, "words": words})

        if "lq_chain" in s and s["lq_chain"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["lq_chain"][i])
            j = int(s["lq_chain_sweep_at"][i])
            name = level_name(s, j) if j >= 0 and s["lq_take"][j] == d else (
                "an earlier low" if d == 1 else "an earlier high")
            via = VIA[int(s["lq_chain_via"][i])]
            where = "a gap the new move left" if via == "gap" else "the halfway line of the new move"
            add("chain", d, s["lq_chain_stop"][i], name,
                "Took %s, broke its last swing %s, came back to %s and closed %s at %s %s. "
                "The course would put the stop past %s."
                % (name, "high" if d == 1 else "low", where, "up" if d == 1 else "down",
                   fp(close), when, fp(s["lq_chain_stop"][i])))
        if "lq_asf" in s and s["lq_asf"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["lq_asf"][i])
            j = int(s["lq_asf_sweep_at"][i])
            name = level_name(s, j) if j >= 0 else ""
            add("sweep_flip", d, s["lq_asf_stop"][i], name,
                "After taking %s, closed %s its last swing %s at %s %s."
                % (name, "above" if d == 1 else "below", "high" if d == 1 else "low",
                   fp(close), when))
        if s["st_flip"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["st_flip"][i])
            lvl = _at(s, "st_ref_hi" if d == 1 else "st_ref_lo", i - 1)
            add("flip", d, lvl, "the last swing %s" % ("high" if d == 1 else "low"),
                "Closed at %s, %s the last swing %s at %s, %s, so the trend on these "
                "bars turned %s." % (fp(close), "above" if d == 1 else "below",
                                     "high" if d == 1 else "low", fp(lvl), when,
                                     "up" if d == 1 else "down"))
        if s["st_ext"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["st_ext"][i])
            lvl = _at(s, "st_hi" if d == 1 else "st_lo", i)
            add("break", d, lvl, "the last swing %s" % ("high" if d == 1 else "low"),
                "Closed at %s, %s the last swing %s at %s, %s: the %s trend carried on."
                % (fp(close), "above" if d == 1 else "below", "high" if d == 1 else "low",
                   fp(lvl), when, "up" if d == 1 else "down"))
        if "lq_take" in s and s["lq_take"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["lq_take"][i])
            name = level_name(s, i)
            lvl = float(s["lq_take_lvl"][i])
            # lq_take_back is against every level the bar took; the words
            # are about the one they name.
            back = close > lvl if d == 1 else close < lvl
            several = name.startswith("equal") or name[:1].isdigit()
            it = "them" if several else "it"
            add("take", d, lvl, name,
                "Took %s %s %s and closed %s %s at %s."
                % (name, ("(the nearest at %s)" if several else "at %s") % fp(lvl), when,
                   ("back above" if d == 1 else "back below") if back else
                   ("below" if d == 1 else "above"), it, fp(close)))
        if s["st_ifvg"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["st_ifvg"][i])
            lvl = _at(s, "st_ifvg_lvl", i)
            add("inverse_gap", d, lvl, "a gap the %s left" % ("fall" if d == 1 else "rise"),
                "Closed at %s, %s the gap the %s left (its edge was %s), %s."
                % (fp(close), "above" if d == 1 else "below", "fall" if d == 1 else "rise",
                   fp(lvl), when))
        if s["st_fvg_touch"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["st_fvg_touch"][i])
            top = _at(s, "st_fvg_up_top" if d == 1 else "st_fvg_dn_top", i - 1)
            bot = _at(s, "st_fvg_up_bot" if d == 1 else "st_fvg_dn_bot", i - 1)
            add("gap_touch", d, top if d == 1 else bot, "a gap",
                "Came back into the gap between %s and %s that the %s left, %s, and "
                "closed at %s." % (fp(bot), fp(top), "rise" if d == 1 else "fall", when,
                                   fp(close)))
        if s["st_eq_touch"][i]:
            when = when or _when(bars, s, i, daily)
            d = int(s["st_eq_touch"][i])
            lvl = _at(s, "st_eq", i - 1)
            add("half_touch", d, lvl, "the halfway line",
                "Came back to the halfway line of the last %s at %s, %s, and closed at %s."
                % ("rise" if d == 1 else "fall", fp(lvl), when, fp(close)))
        if "ck_gap_filled" in s and s["ck_gap_filled"][i] and (
                i == 0 or not s["ck_gap_filled"][i - 1] or s["ck_day"][i] != s["ck_day"][i - 1]):
            when = when or _when(bars, s, i, daily)
            d = int(s["ck_gap_dir"][i])
            prev = float(s["ck_gap_lo"][i] if d == 1 else s["ck_gap_hi"][i])
            add("gap_fill", -d, prev, "the previous close",
                "Traded back to the previous close at %s %s, filling the gap the day "
                "opened with." % (fp(prev), when))
        out.extend(found)
        if len(out) >= limit:
            break
    return out[:limit]
