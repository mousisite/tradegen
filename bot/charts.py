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

import math
from typing import Dict, List, Optional, Sequence, Tuple


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


def _price(value: float) -> str:
    if abs(value) >= 1000:
        return "%,.0f".replace(",", ",") % value if False else format(value, ",.0f")
    if abs(value) >= 1:
        return "%.2f" % value
    return "%.4f" % value


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
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    low, high = min(values), max(values)
    if high == low:
        high, low = high * 1.01 + 0.01, low * 0.99 - 0.01
    step = _nice_step(high - low)
    axis_low = math.floor(low / step) * step
    axis_high = math.ceil(high / step) * step
    span = axis_high - axis_low or 1.0

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
                     % (pad_left - 8, y + 4, _escape(_price(tick))))

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
# Filed financials
# ---------------------------------------------------------------------------

def bar_chart(years: Sequence[int], values: Sequence[Optional[float]],
              width: int = 340, height: int = 170,
              title: str = "", money: bool = True) -> str:
    """One filed line item across the years, as bars.

    Bars rather than a line because these are separate annual measurements, not
    a continuous series, and a line between two audited figures implies values
    in between that were never reported.
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
    bar_w = max(6.0, slot * 0.62)
    zero_y = y_at(0.0)
    fmt = _money if money else (lambda v: "%.1f%%" % (v * 100))

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
                     'rx="2" fill="%s"><title>%d: %s</title></rect>'
                     % (x, y, bar_w, max(bar_h, 1.0), colour, year,
                        _escape(fmt(amount))))
        if index == 0 or index == len(pairs) - 1:
            parts.append('<text x="%.1f" y="%d" text-anchor="middle" '
                         'class="clab">%d</text>'
                         % (x + bar_w / 2, height - 6, year))

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
