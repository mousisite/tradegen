"""Drawing the numbers, in SVG, with no library.

Charts are the one obvious thing this was missing against every commercial
research tool. They are built here as plain SVG strings rather than by loading
a charting library, for three reasons: the pages currently load no third-party
JavaScript at all and the privacy policy says so; an SVG renders before any
script runs, so a chart is visible in the first paint; and a chart built from
the same numbers as the table beside it cannot drift out of step with it.

Every chart labels a real value at every tick. Nothing is drawn to a scale it
does not state, and a series that cannot be drawn honestly returns nothing
rather than something misleading.
"""
from __future__ import annotations

import bisect
import math
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple


def _nice_step(span: float, target_ticks: int = 4) -> float:
    """A tick interval a person would choose: 1, 2, 2.5 or 5 times a power of ten."""
    if span <= 0:
        return 1.0
    raw = span / max(target_ticks, 1)
    power = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * power:
            return multiple * power
    return 10 * power


def _money(value: float) -> str:
    """A figure at the scale a person would say it."""
    magnitude = abs(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if magnitude >= limit:
            return "%.1f%s" % (value / limit, suffix)
    if magnitude >= 1:
        return "%.0f" % value
    return "%.2f" % value


def _price(value: float, decimals: int = 0) -> str:
    """A price with the decimals its size usually needs, or decimals if more."""
    usual = 0 if abs(value) >= 1000 else 2 if abs(value) >= 1 else 4
    return format(value, ",.%df" % max(usual, decimals))


def _tick_decimals(step: float) -> int:
    """The decimals that write every tick of an axis exactly: 2 for 0.25."""
    for d in range(12):
        if abs(round(step, d) - step) < step * 1e-6:
            return d
    return 12


def _level_decimals(step: float, value: float) -> int:
    """The decimals that keep prices on this axis apart: to a tenth of a tick
    when the usual ones are coarser than a fifth of one, as on a currency
    pair, where an entry and a stop could both read 1.08, and never to one
    decimal, which nobody quotes a price in; else 0, the usual."""
    usual = 0 if abs(value) >= 1000 else 2 if abs(value) >= 1 else 4
    if 10.0 ** -usual <= step / 5 * 1.0001:
        return 0
    return max(2, int(math.ceil(-math.log10(step) - 1e-9)) + 1)


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def price_chart(closes: Sequence[float], labels: Sequence[str] = (),
                width: int = 720, height: int = 220,
                title: str = "") -> str:
    """A price line with a filled area, gridlines and labelled ends.

    Deliberately not a candlestick chart. At the width this renders, candles
    are illegible and imply a precision the eye cannot read; a line answers the
    question the research page is actually asking, which is what shape the last
    year looked like.
    """
    values = [float(v) for v in closes if v is not None and math.isfinite(float(v))]
    if len(values) < 3:
        return ""

    pad_left, pad_right, pad_top, pad_bottom = 56, 14, 16, 26
    plot_h = height - pad_top - pad_bottom

    low, high = min(values), max(values)
    if high == low:
        high, low = high * 1.01 + 0.01, low * 0.99 - 0.01
    step = _nice_step(high - low)
    axis_low = math.floor(low / step) * step
    axis_high = math.ceil(high / step) * step
    span = axis_high - axis_low or 1.0
    decimals = _tick_decimals(step)
    pad_left += 7 * (len(_price(axis_high, decimals)) - len(_price(axis_high)))
    plot_w = width - pad_left - pad_right

    def x_at(i):
        return pad_left + plot_w * (i / max(len(values) - 1, 1))

    def y_at(v):
        return pad_top + plot_h * (1 - (v - axis_low) / span)

    points = " ".join("%.1f,%.1f" % (x_at(i), y_at(v)) for i, v in enumerate(values))
    area = ("%s %.1f,%.1f %.1f,%.1f"
            % (points, x_at(len(values) - 1), pad_top + plot_h,
               pad_left, pad_top + plot_h))

    rising = values[-1] >= values[0]
    stroke = "var(--gain)" if rising else "var(--loss)"
    fill = "var(--gain-wash)" if rising else "var(--loss-wash)"

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="none" aria-label="%s">'
             % (width, height, _escape(title or "Price history"))]

    ticks = []
    value = axis_low
    while value <= axis_high + step * 0.001:
        ticks.append(value)
        value += step
    for tick in ticks:
        y = y_at(tick)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                     'stroke="var(--hairline)" stroke-width="1"/>'
                     % (pad_left, y, width - pad_right, y))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" '
                     'class="clab">%s</text>'
                     % (pad_left - 8, y + 4, _escape(_price(tick, decimals))))

    parts.append('<polygon points="%s" fill="%s" opacity="0.65"/>' % (area, fill))
    parts.append('<polyline points="%s" fill="none" stroke="%s" '
                 'stroke-width="2" stroke-linejoin="round" '
                 'stroke-linecap="round"/>' % (points, stroke))

    last_x, last_y = x_at(len(values) - 1), y_at(values[-1])
    parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s"/>'
                 % (last_x, last_y, stroke))

    if labels:
        first, last = _escape(labels[0]), _escape(labels[-1])
        parts.append('<text x="%d" y="%d" class="clab">%s</text>'
                     % (pad_left, height - 8, first))
        parts.append('<text x="%d" y="%d" text-anchor="end" class="clab">%s</text>'
                     % (width - pad_right, height - 8, last))

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# The trade, drawn on its own prices
# ---------------------------------------------------------------------------

# What one bar of each interval is called, for the trade chart's title.
_BAR_WORDS = {"1d": "daily", "1h": "hourly", "60m": "hourly", "30m": "30-minute",
              "15m": "15-minute", "5m": "5-minute", "2m": "2-minute",
              "1m": "1-minute"}


def bar_words(interval: str) -> str:
    """"daily", "hourly" or "15-minute": what a bar of this interval covers."""
    return _BAR_WORDS.get((interval or "1d").lower(), interval or "")


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def trade_chart(times: Sequence[int], opens: Sequence[float], highs: Sequence[float],
                lows: Sequence[float], closes: Sequence[float],
                volumes: Sequence[float] = (), levels: Optional[Dict[str, float]] = None,
                last: Optional[float] = None, as_of: str = "", day_label=None,
                width: int = 720, height: int = 300, bars: int = 90,
                interval: str = "1d", zones: Sequence = (), marks: Sequence = (),
                extra_levels: Optional[Dict[str, float]] = None, bands: Sequence = (),
                calm: bool = False) -> str:
    """Candles with the plan's prices drawn across them.

    For the analysis page, where the question is where today's price sits
    against the entry, the stop and the target. Candles here, unlike the
    research page's year-long line, because the last few months at this width
    leave each one wide enough to read, and the stop only means something
    against the size of a normal bar's range.

    levels maps a label ("Buy at", "Stop", "Target") to a price. A level far
    outside the drawn range is kept, and the range is widened to show it,
    because a target nobody can see on the chart reads as no target. Each
    candle carries its date and prices as a tooltip; on bars shorter than a
    day that is the date and time, in UTC unless day_label says otherwise.
    as_of is when the latest price was set, said in the title and on the dot
    that marks it. Nothing is drawn for a series too short or too broken to
    draw honestly.

    The analysis page's key-levels chart adds, all optional and drawn only
    when given:
      zones         [(start, end or None, top, bottom, label, dir)]: shaded
                    boxes, such as a gap price left open (dir +1 the rise's,
                    -1 the fall's), from its start time to its end or the
                    right edge
      marks         [(time, price, dir, label)]: a ring on a candle, such as
                    where an old high or low was taken
      extra_levels  {label: price}: thin dotted lines, labelled at the right
                    edge with the plan's, kept apart the same way. Unlike the
                    plan's prices, a level more than half the candles' range
                    beyond them is left off (the page's table lists it):
                    stretching the scale to reach it would flatten the candles
      bands         [(start, end, label)]: light shading behind the candles
                    for a stretch of time, such as the Asia session
      calm          hollow grey candles for a rise and ink ones for a fall,
                    for anyone who finds red and green stir them up
    Times here are bar start times, as in `times`.
    """
    n = min(len(times), len(opens), len(highs), len(lows), len(closes))
    rows = []
    for k in range(max(0, n - bars), n):
        o, h, l, c = (float(opens[k]), float(highs[k]), float(lows[k]), float(closes[k]))
        if all(math.isfinite(x) for x in (o, h, l, c)) and h >= l:
            v = float(volumes[k]) if k < len(volumes) and math.isfinite(float(volumes[k])) else 0.0
            rows.append((int(times[k]), o, h, l, c, v))
    if len(rows) < 10:
        return ""

    levels = {name: float(price) for name, price in (levels or {}).items()
              if price is not None and math.isfinite(float(price)) and float(price) > 0}
    low = min(min(r[3] for r in rows), *(levels.values() or [math.inf]))
    high = max(max(r[2] for r in rows), *(levels.values() or [-math.inf]))
    if last is not None and math.isfinite(last):
        low, high = min(low, last), max(high, last)

    # Context drawn only near the candles: half their range either side.
    candle_lo, candle_hi = min(r[3] for r in rows), max(r[2] for r in rows)
    reach = (candle_hi - candle_lo) * 0.5
    near_lo, near_hi = candle_lo - reach, candle_hi + reach
    extra = {str(name): float(price) for name, price in (extra_levels or {}).items()
             if _finite(price) and float(price) > 0
             and near_lo <= float(price) <= near_hi}
    boxes = []
    for zone in zones or ():
        start, end, top, bottom, label, side = zone
        if not (_finite(top) and _finite(bottom)):
            continue
        top, bottom = max(float(top), float(bottom)), min(float(top), float(bottom))
        if bottom > near_hi or top < near_lo:
            continue
        boxes.append((int(start), None if end is None else int(end),
                      min(top, near_hi), max(bottom, near_lo), str(label), int(side or 0)))
    for price in extra.values():
        low, high = min(low, price), max(high, price)
    for box in boxes:
        low, high = min(low, box[3]), max(high, box[2])
    if high <= low:
        high, low = high * 1.01 + 0.01, low * 0.99 - 0.01

    pad_left, pad_right, pad_top = 60, 86, 14
    vol_h = 44 if any(r[5] > 0 for r in rows) else 0
    gap = 10 if vol_h else 0
    pad_bottom = 24
    price_h = height - pad_top - pad_bottom - vol_h - gap

    step = _nice_step(high - low)
    axis_low = math.floor(low / step) * step
    axis_high = math.ceil(high / step) * step
    span = axis_high - axis_low or 1.0
    decimals = _tick_decimals(step)

    def said(v):
        return _price(v, _level_decimals(step, v))

    # Room at the edges for the decimals close prices need.
    pad_left += 7 * (len(_price(axis_high, decimals)) - len(_price(axis_high)))
    pad_right += 7 * max([len(said(v)) - len(_price(v)) for v in levels.values()] or [0])
    plot_w = width - pad_left - pad_right
    slot = plot_w / len(rows)
    body = max(1.0, slot * 0.62)

    def x_at(i):
        return pad_left + slot * (i + 0.5)

    def y_at(v):
        return pad_top + price_h * (1 - (v - axis_low) / span)

    title = "%s price%s" % (bar_words(interval).capitalize(),
                            ", as of " + as_of if as_of else "")
    parts = ['<svg class="chart trade" viewBox="0 0 %d %d" role="img" aria-label="%s">'
             % (width, height, _escape(title))]

    tick = axis_low
    while tick <= axis_high + step * 0.001:
        y = y_at(tick)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--hairline)" '
                     'stroke-width="1"/>' % (pad_left, y, width - pad_right, y))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="clab">%s</text>'
                     % (pad_left - 8, y + 4, _escape(_price(tick, decimals))))
        tick += step

    starts = [r[0] for r in rows]

    def x_edge(ts: int) -> float:
        """The left edge of the first candle starting at or after ts."""
        return pad_left + slot * bisect.bisect_left(starts, int(ts))

    # Sessions behind everything, then the gap boxes, then the candles.
    for start, end, label in bands or ():
        if not (_finite(start) and _finite(end)) or end <= starts[0] or start > starts[-1]:
            continue
        x0, x1 = x_edge(start), x_edge(end)
        if x1 - x0 < 1:
            continue
        parts.append('<g><title>%s</title><rect x="%.1f" y="%d" width="%.1f" height="%.1f" '
                     'fill="var(--sunken)"/>' % (_escape(label), x0, pad_top, x1 - x0, price_h))
        if x1 - x0 >= 7 * len(str(label)) + 6:
            parts.append('<text x="%.1f" y="%d" class="clab">%s</text>'
                         % (x0 + 3, pad_top + 11, _escape(label)))
        parts.append('</g>')
    for start, end, top, bottom, label, side in boxes:
        if (end is not None and end <= starts[0]) or start > starts[-1]:
            continue
        x0 = x_edge(start)
        x1 = pad_left + plot_w if end is None else x_edge(end)
        if x1 - x0 < 1:
            continue
        wash, edge = (("var(--gain-wash)", "var(--gain)") if side > 0 else
                      ("var(--loss-wash)", "var(--loss)") if side < 0 else
                      ("var(--sunken)", "var(--ink-3)"))
        parts.append('<g><title>%s</title><rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'fill="%s" stroke="%s" stroke-width="0.8" stroke-opacity="0.5"/></g>'
                     % (_escape(label), x0, y_at(top), x1 - x0,
                        max(1.0, y_at(bottom) - y_at(top)), wash, edge))

    # A daily candle is named by its date; a shorter one needs the time too,
    # or a day's worth of hourly candles all carry the same name.
    zone = ""
    day = day_label
    if day is None:
        intraday = bar_words(interval) != "daily"
        stamp = "%d %b %H:%M" if intraday else "%d %b"
        zone = " UTC" if intraday else ""

        def day(ts):
            return time.strftime(stamp, time.gmtime(int(ts))).lstrip("0")
    peak = max(r[5] for r in rows) or 1.0
    for i, (ts, o, h, l, c, v) in enumerate(rows):
        up = c >= o
        colour = "var(--gain)" if up else "var(--loss)"
        if calm:
            colour = "var(--ink-3)" if up else "var(--ink)"
        x = x_at(i)
        top, bottom = y_at(max(o, c)), y_at(min(o, c))
        tip = "%s%s  open %s  high %s  low %s  close %s" % (
            day(ts), zone, said(o), said(h), said(l), said(c))
        parts.append('<g><title>%s</title>' % _escape(tip.strip()))
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"/>'
                     % (x, y_at(h), x, y_at(l), colour))
        if calm and up:
            # Hollow: the outline in grey, the inside the card's own colour.
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                         'fill="var(--surface)" stroke="var(--ink-3)" stroke-width="1"/>'
                         % (x - body / 2, top, body, max(1.0, bottom - top)))
        elif calm:
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--ink)"/>'
                         % (x - body / 2, top, body, max(1.0, bottom - top)))
        else:
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x - body / 2, top, body, max(1.0, bottom - top),
                            colour if not up else "var(--gain)"))
        if vol_h:
            vh = vol_h * (v / peak)
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                         'fill="var(--ink-3)" opacity="0.35"/>'
                         % (x - body / 2, pad_top + price_h + gap + vol_h - vh, body, vh))
        parts.append('</g>')

    # Where an old high or low was taken: a ring on that candle, at the level.
    for ts, price, _side, label in marks or ():
        if not _finite(price):
            continue
        j = bisect.bisect_left(starts, int(ts))
        if j >= len(starts) or starts[j] != int(ts):
            continue
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="var(--surface)" '
                     'stroke="var(--ink)" stroke-width="1.5"><title>%s</title></circle>'
                     % (x_at(j), y_at(float(price)), _escape(label)))

    # The plan's prices, labelled at the right edge where the eye ends up,
    # with any other levels, dotted and quieter, among them.
    styles = {"Stop": ("var(--loss)", "4 3"), "Target": ("var(--gain)", "4 3")}
    placed = []
    lines = [(name, price, False) for name, price in levels.items()]
    lines += [(name, price, True) for name, price in extra.items()]
    for name, price, quiet in sorted(lines, key=lambda kv: -kv[1]):
        colour, dash = styles.get(name, ("var(--accent-text)", "6 3"))
        y = y_at(price)
        label_y = y + 4
        for other in placed:                 # keep labels from sitting on each other
            if abs(label_y - other) < 12:
                label_y = other + 12
        placed.append(label_y)
        if quiet:
            parts.append('<g><title>%s %s</title><line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                         'stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="1 3"/>'
                         '<text x="%d" y="%.1f" class="clab lvl">%s</text></g>'
                         % (_escape(name), _escape(said(price)), pad_left, y,
                            width - pad_right, y, width - pad_right + 6, label_y,
                            _escape(name)))
            continue
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.5" '
                     'stroke-dasharray="%s"/>' % (pad_left, y, width - pad_right, y, colour, dash))
        parts.append('<text x="%d" y="%.1f" class="clab lvl" style="fill:%s">%s %s</text>'
                     % (width - pad_right + 6, label_y, colour, _escape(name), _escape(said(price))))

    if last is not None and math.isfinite(last):
        now = "Now %s%s" % (said(last), ", as of " + as_of if as_of else "")
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="var(--ink)"><title>%s</title></circle>'
                     % (x_at(len(rows) - 1), y_at(last), _escape(now)))

    # The first and last candle's dates, short enough to sit apart on a
    # phone. When the latest price was set is in the title and on its dot.
    first_day, last_day = day(rows[0][0]), day(rows[-1][0])
    if first_day:
        parts.append('<text x="%d" y="%d" class="clab">%s</text>'
                     % (pad_left, height - 6, _escape(first_day)))
    if last_day:
        parts.append('<text x="%d" y="%d" text-anchor="end" class="clab">%s</text>'
                     % (width - pad_right, height - 6, _escape(last_day + zone)))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Filed financials
# ---------------------------------------------------------------------------

def bar_chart(years: Sequence[int], values: Sequence[Optional[float]],
              width: int = 340, height: int = 170,
              title: str = "", money: bool = True,
              label: Optional[Callable[[int], str]] = None,
              fmt: Optional[Callable[[float], str]] = None) -> str:
    """One filed line item across the years, as bars.

    Bars rather than a line because these are separate annual measurements, not
    a continuous series, and a line between two audited figures implies values
    in between that were never reported.

    label names each bar's key (a year by default) and fmt says each value
    (money, or a fraction as a percent with money=False). The operator's
    analyses-per-day chart passes both: its keys are days, given as epoch
    seconds, and its values are counts.
    """
    pairs = [(int(y), float(v)) for y, v in zip(years, values)
             if v is not None and math.isfinite(float(v))]
    if len(pairs) < 2:
        return ""

    pad_left, pad_right, pad_top, pad_bottom = 52, 8, 14, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    amounts = [v for _, v in pairs]
    high = max(amounts + [0.0])
    low = min(amounts + [0.0])
    step = _nice_step(high - low)
    axis_high = math.ceil(high / step) * step if high else step
    axis_low = math.floor(low / step) * step if low < 0 else 0.0
    span = (axis_high - axis_low) or 1.0

    def y_at(v):
        return pad_top + plot_h * (1 - (v - axis_low) / span)

    slot = plot_w / len(pairs)
    # Never wider than its slot: a year of days would be drawn over each other.
    bar_w = min(slot, max(6.0, slot * 0.62))
    zero_y = y_at(0.0)
    if fmt is None:
        fmt = _money if money else (lambda v: "%.1f%%" % (v * 100))
    if label is None:
        label = str

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="none" aria-label="%s">'
             % (width, height, _escape(title or "Filed figures by year"))]

    for tick in (axis_low, axis_high) if axis_low else (0.0, axis_high):
        y = y_at(tick)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                     'stroke="var(--hairline)"/>'
                     % (pad_left, y, width - pad_right, y))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="clab">%s</text>'
                     % (pad_left - 6, y + 4, _escape(fmt(tick))))

    for index, (year, amount) in enumerate(pairs):
        x = pad_left + slot * index + (slot - bar_w) / 2
        y = y_at(max(amount, 0.0))
        bar_h = abs(y_at(amount) - zero_y)
        colour = "var(--accent)" if amount >= 0 else "var(--loss)"
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'rx="2" fill="%s"><title>%s: %s</title></rect>'
                     % (x, y, bar_w, max(bar_h, 1.0), colour,
                        _escape(label(year)), _escape(fmt(amount))))
        if index == 0 or index == len(pairs) - 1:
            parts.append('<text x="%.1f" y="%d" text-anchor="middle" '
                         'class="clab">%s</text>'
                         % (x + bar_w / 2, height - 6, _escape(label(year))))

    parts.append("</svg>")
    return "".join(parts)


def series_from_history(history: Dict, concept: str) -> Tuple[List[int], List[float]]:
    """Years and values for one filed concept, ready to chart."""
    rows = (history.get("table") or {}).get(concept) or []
    years, values = [], []
    for row in rows:
        if isinstance(row, dict) and row.get("value") is not None:
            years.append(int(row["fy"]))
            values.append(float(row["value"]))
    return years, values


def margin_series(history: Dict) -> Tuple[List[int], List[float]]:
    """Net margin per filed year, as a fraction.

    Refuses when the two lines are reported in different currencies, which
    happens for foreign issuers and would otherwise produce a margin wrong by
    the exchange rate.
    """
    if history.get("mixed_currency"):
        return [], []
    revenue = dict(zip(*series_from_history(history, "revenue")))
    income = dict(zip(*series_from_history(history, "net_income")))
    years = sorted(set(revenue) & set(income))
    out_years, out_values = [], []
    for year in years:
        if revenue[year]:
            out_years.append(year)
            out_values.append(income[year] / revenue[year])
    return out_years, out_values


def charts_for(history: Dict) -> Dict[str, str]:
    """The set of filed-financial charts worth drawing for one company.

    Each chart covers one line item, so it is internally consistent even when
    the filing as a whole mixes currencies. The title carries the unit, because
    two charts side by side invite a comparison the currencies do not support.
    """
    if not history or not history.get("available"):
        return {}

    units = {}
    for concept in ("revenue", "net_income", "operating_cashflow"):
        for row in (history.get("table") or {}).get(concept) or []:
            if row.get("unit"):
                units[concept] = row["unit"]
                break

    out = {}
    for key, concept, label in (
        ("revenue", "revenue", "Revenue"),
        ("net_income", "net_income", "Net income"),
        ("operating_cashflow", "operating_cashflow", "Operating cash flow"),
    ):
        years, values = series_from_history(history, concept)
        unit = units.get(concept, "")
        svg = bar_chart(years, values,
                        title="%s%s" % (label, (" in %s" % unit) if unit else ""))
        if svg:
            out[key] = svg
            out[key + "_unit"] = unit

    years, values = margin_series(history)
    svg = bar_chart(years, values, title="Net margin", money=False)
    if svg:
        out["net_margin"] = svg
    return out
