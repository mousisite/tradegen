"""New York time, trading days, sessions and the day's levels, checked exactly.

The clock change on both sides, in 2025 and 2026; which trading day an 18:30
bar belongs to; the session and window at their edges; the Asia range before
and after 03:00, and when it has too few bars; yesterday's levels (from the
last whole day), the opening gaps of a day and a week and their fills, a
contract roll, 4-hour bars built from hourly ones, and the date of a daily
bar whatever time it is stamped at. No network.
"""
import math
import os as _os
import sys as _sys
from datetime import date, timedelta

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness  # noqa: E402,F401

harness.isolate("clock")

import numpy as np  # noqa: E402

import scenarios as sc  # noqa: E402
from bot import clock, sessions  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


utc, ny = clock.utc, clock.ny


def futures(n_days, start=date(2026, 9, 13), step=300, offsets=None, kind="future"):
    """A future's bars for n trading days: a gentle wave, each day lifted by
    offsets[d] (so the day opens with a jump of the difference)."""
    per_day = int(23 * 3600 / step)
    ts = sc.futures_times(n_days * per_day, start=start, step=step)
    k = np.arange(len(ts))
    day = k // per_day
    base = 6000.0 + 2.0 * np.sin(k / 20.0)
    if offsets is not None:
        base = base + np.asarray(offsets, dtype=float)[day]
    close = base
    open_ = np.concatenate([[base[0]], base[:-1]])     # each bar opens at the close before
    first = np.r_[True, day[1:] != day[:-1]]
    open_[first] = close[first]          # except a day's first, which opens at its own level
    high = np.maximum(open_, close) + 0.25
    low = np.minimum(open_, close) - 0.25
    return sc.make(open_, high, low, close, ts, kind=kind, interval="%dm" % (step // 60)
                   if step < 3600 else "1h")


# ---------------------------------------------------------------------------
head("NEW YORK TIME ACROSS THE CLOCK CHANGES")
# ---------------------------------------------------------------------------
for year, spring, autumn in ((2025, 9, 2), (2026, 8, 1)):
    check("%d: 01:59 New York before the spring change is winter time" % year,
          clock.ny_offset(utc(year, 3, spring, 6, 59)) == -18000
          and clock.fmt_ny(utc(year, 3, spring, 6, 59)) == "01:59")
    check("%d: a minute later it is 03:00, summer time" % year,
          clock.ny_offset(utc(year, 3, spring, 7, 0)) == -14400
          and clock.fmt_ny(utc(year, 3, spring, 7, 0)) == "03:00")
    check("%d: 01:59 before the autumn change is still summer time" % year,
          clock.ny_offset(utc(year, 11, autumn, 5, 59)) == -14400
          and clock.fmt_ny(utc(year, 11, autumn, 5, 59)) == "01:59")
    check("%d: a minute later it is 01:00 again, winter time" % year,
          clock.ny_offset(utc(year, 11, autumn, 6, 0)) == -18000
          and clock.fmt_ny(utc(year, 11, autumn, 6, 0)) == "01:00")
check("the stock open is 09:30 New York in winter and in summer",
      clock.fmt_ny(utc(2026, 3, 6, 14, 30)) == "09:30"
      and clock.fmt_ny(utc(2026, 3, 9, 13, 30)) == "09:30")
check("the same as sessions.new_york_offset at noon on every day of two years",
      all(clock.ny_offset(sessions.us_session(d)[0] if sessions.us_session(d) else
                          utc(d.year, d.month, d.day, 17, 0))
          == sessions.new_york_offset(d)
          for d in (date(2025, 1, 1) + timedelta(days=k) for k in range(730))))
check("labels read in New York time", clock.label_ny(utc(2026, 11, 19, 15, 2)) == "19 Nov 10:02"
      and clock.label_ny(utc(2026, 11, 19, 15, 2), intraday=False) == "19 Nov")
check("ny() turns a New York wall-clock time back into the moment",
      ny(2026, 7, 1, 9, 30) == utc(2026, 7, 1, 13, 30)
      and ny(2026, 12, 1, 9, 30) == utc(2026, 12, 1, 14, 30))
check("a vector of times reads the same as one at a time",
      clock.offsets(np.array([utc(2026, 3, 8, 6, 59), utc(2026, 3, 8, 7, 0)])).tolist()
      == [-18000, -14400])

# ---------------------------------------------------------------------------
head("WHICH TRADING DAY, SESSION AND WINDOW")
# ---------------------------------------------------------------------------
evening = ny(2026, 11, 18, 18, 30)
check("an 18:30 bar is the next day's for a future and a coin",
      clock.trading_day(evening, "future") == date(2026, 11, 19)
      and clock.trading_day(evening, "coin") == date(2026, 11, 19))
check("and the same day's for a stock", clock.trading_day(evening, "stock") == date(2026, 11, 18))
check("17:59 is still the day it is", clock.trading_day(ny(2026, 11, 18, 17, 59), "future")
      == date(2026, 11, 18))
edges = [((9, 29, 59), "ny_pre", None), ((9, 30, 0), "ny_am", "open_drive"),
         ((9, 49, 59), "ny_am", "open_drive"), ((9, 50, 0), "ny_am", "macro"),
         ((10, 9, 59), "ny_am", "macro"), ((10, 10, 0), "ny_am", "late_morning"),
         ((10, 29, 59), "ny_am", "late_morning"), ((10, 30, 0), "ny_am", "after_cutoff"),
         ((2, 59, 59), "asia", None), ((3, 0, 0), "london", None),
         ((8, 29, 59), "london", None), ((8, 30, 0), "ny_pre", None),
         ((12, 0, 0), "ny_lunch", "after_cutoff"), ((15, 59, 59), "ny_pm", "after_cutoff"),
         ((16, 0, 0), "ny_late", None), ((17, 0, 0), "halt", None), ((18, 0, 0), "asia", None)]
wrong = []
for (h, m, s_), sess, win in edges:
    t = ny(2026, 11, 18, h, m) + s_
    if clock.session_of(t) != sess or clock.window_of(t) != win:
        wrong.append(("%02d:%02d:%02d" % (h, m, s_), clock.session_of(t), clock.window_of(t)))
check("sessions and windows at their edges (09:29:59, 09:30, 10:10, 10:30...)", not wrong, wrong)
check("the lab's windows cover every minute",
      all(clock.lab_window_of(ny(2026, 11, 18, 0, 0) + 60 * k) in
          [w[0] for w in clock.LAB_WINDOWS] for k in range(24 * 60)))
check("and split the morning the course's way",
      clock.lab_window_of(ny(2026, 11, 18, 9, 55)) == "macro"
      and clock.lab_window_of(ny(2026, 11, 18, 10, 45)) == "midday"
      and clock.lab_window_of(ny(2026, 11, 18, 13, 0)) == "afternoon")
check("a future, a coin, a stock and daily bars are told apart",
      clock.kind_of(sc.futures_series(300)) == "future"
      and clock.kind_of(sc.coin_series(300)) == "coin"
      and clock.kind_of(sc.stock_series(300)) == "stock"
      and clock.kind_of(sc.daily_series(300)) == "daily")

# ---------------------------------------------------------------------------
head("THE ASIA AND LONDON RANGES")
# ---------------------------------------------------------------------------
b = sc.futures_series(276 * 6, roll_day=None)
s = clock.series(b)
days = clock.day_table(b)
d = days[2]
idx = np.arange(d.first, d.last + 1)
mins = s["ck_min"][idx]
asia = idx[(mins >= 18 * 60) | (mins < 3 * 60)]
lon = idx[(mins >= 3 * 60) & (mins < 8 * 60 + 30)]
first_after = idx[(mins >= 3 * 60) & (mins < 18 * 60)][0]
check("the day has all its Asia bars (108 five-minute bars)",
      len(asia) == 108 and d.sessions["asia"].complete, len(asia))
check("Asia's range is not there before 03:00",
      np.all(np.isnan(s["ck_asia_hi"][asia])))
check("and from 03:00 it is the session's own high and low",
      s["ck_asia_hi"][first_after] == b.high[asia].max()
      and s["ck_asia_lo"][first_after] == b.low[asia].min()
      and clock.fmt_ny(int(b.timestamp[first_after])) == "03:00")
check("for the rest of the day", np.all(s["ck_asia_hi"][first_after:d.last + 1]
                                        == b.high[asia].max()))
check("the next day starts without it", math.isnan(s["ck_asia_hi"][days[3].first]))
lon_from = idx[(mins >= 8 * 60 + 30) & (mins < 18 * 60)][0]
check("London's range is there from 08:30",
      math.isnan(s["ck_lon_hi"][lon_from - 1]) and s["ck_lon_hi"][lon_from] == b.high[lon].max())
check("and the before-the-open range from 09:30",
      s["ck_pre_hi"][idx[(mins >= 9 * 60 + 30) & (mins < 18 * 60)][0]]
      == b.high[idx[(mins >= 8 * 60 + 30) & (mins < 9 * 60 + 30)]].max())

# An Asia session with 60% of its bars is no range; with 81% it is one.
for keep_share, wanted in ((0.6, False), (0.81, True)):
    drop = asia[:int(round(len(asia) * (1 - keep_share)))]
    keep = np.setdiff1d(np.arange(len(b.close)), drop)
    cut = sc.make(b.open[keep], b.high[keep], b.low[keep], b.close[keep], b.timestamp[keep],
                  kind="future")
    cs = clock.series(cut)
    cday = clock.day_table(cut)[2]
    got = not np.all(np.isnan(cs["ck_asia_hi"][cday.first:cday.last + 1]))
    check("an Asia session with %d%% of its bars %s a range"
          % (keep_share * 100, "is" if wanted else "is not"),
          got == wanted and cday.sessions["asia"].complete == wanted,
          (cday.sessions["asia"].bars, cday.sessions["asia"].expected))
    if not wanted:
        check("and New York takes Asia's range only when London's is incomplete: here "
              "it is London's", np.all(cs["ck_sess_src"][cday.first:cday.last + 1] != 2))

coin = sc.coin_series(288 * 4)
cs = clock.series(coin)
check("a coin has Asia and London ranges, from the 18:00 day",
      np.isfinite(cs["ck_asia_hi"]).any() and np.isfinite(cs["ck_lon_hi"]).any())
check("and no opening gaps: it never shuts", not np.isfinite(cs["ck_gap_lo"]).any()
      and not np.isfinite(cs["ck_wgap_lo"]).any())
stock = sc.stock_series(78 * 5)
ss = clock.series(stock)
check("a stock has none of them, its data being the day session only",
      not np.isfinite(ss["ck_asia_hi"]).any() and not np.isfinite(ss["ck_lon_hi"]).any()
      and not np.isfinite(ss["ck_pre_hi"]).any() and not np.any(ss["ck_sess_take"]))
check("but it has yesterday's levels and its opening gap",
      np.isfinite(ss["ck_pdh"]).any() and np.isfinite(ss["ck_gap_lo"]).any())

# ---------------------------------------------------------------------------
head("YESTERDAY'S HIGH AND LOW")
# ---------------------------------------------------------------------------
daily = sc.daily_series(60)
ds = clock.series(daily)
check("on daily bars they are the bar before's",
      ds["ck_pdh"][10] == daily.high[9] and ds["ck_pdl"][10] == daily.low[9]
      and ds["ck_pdc"][10] == daily.close[9] and math.isnan(ds["ck_pdh"][0]))
took = [i for i in range(1, 60) if ds["ck_pd_take"][i] == 1]
check("and taking yesterday's low is a low below the bar before's",
      took and all(daily.low[i] < daily.low[i - 1] for i in took))
check("a daily bar's date is its own", ds["ck_day"][0] == date(2021, 11, 22).toordinal())

stock = sc.stock_series(78 * 4)
days = clock.day_table(stock)
s = clock.series(stock)
check("on 5-minute bars, the previous whole day's high and low, all day",
      np.all(s["ck_pdh"][days[2].first:days[2].last + 1] == days[1].high)
      and np.all(s["ck_pdl"][days[2].first:days[2].last + 1] == days[1].low))
check("the first day has none", np.all(np.isnan(s["ck_pdh"][:days[0].last + 1])))
# A day with half its bars missing is skipped: the day after it reads the one before.
holes = np.arange(days[1].first + 10, days[1].first + 50)
keep = np.setdiff1d(np.arange(len(stock.close)), holes)
gappy = sc.make(stock.open[keep], stock.high[keep], stock.low[keep], stock.close[keep],
                stock.timestamp[keep])
gs = clock.series(gappy)
gdays = clock.day_table(gappy)
check("a day with half its bars missing is not complete", not gdays[1].complete)
check("so the day after it reads the last whole day's high and low",
      np.all(gs["ck_pdh"][gdays[2].first:gdays[2].last + 1] == gdays[0].high))
pd = [i for i in range(days[2].first, days[2].last + 1) if s["ck_pd_take"][i] != 0]
check("taking yesterday's high or low is marked once per side, at the first bar past it",
      len(pd) <= 2 and len(set(int(s["ck_pd_take"][i]) for i in pd)) == len(pd))
for i in pd:
    side = int(s["ck_pd_take"][i])
    lvl = days[1].low if side == 1 else days[1].high
    earlier = range(days[2].first, i)
    check("bar %d is the first past yesterday's %s" % (i, "low" if side == 1 else "high"),
          (stock.low[i] < lvl if side == 1 else stock.high[i] > lvl)
          and not any((stock.low[j] < lvl) if side == 1 else (stock.high[j] > lvl)
                      for j in earlier))
early = sc.stock_times(42 * 2, start=date(2026, 11, 26))
check("the day after Thanksgiving closes at 13:00: 42 five-minute bars make it whole",
      clock.day_table(sc.walk(early))[0].expected == 42
      and clock.day_table(sc.walk(early))[0].complete)

# ---------------------------------------------------------------------------
head("OPENING GAPS AND THEIR FILL")
# ---------------------------------------------------------------------------
offsets = [0.0, 0.5, -0.3, 6.0, 6.2, 5.9, 6.4, 6.1, 66.0, 66.4, 66.1]
b = futures(len(offsets), offsets=offsets)
days = clock.day_table(b)
s = clock.series(b)
d3 = days[3]
prev = float(b.close[d3.first - 1])
check("a future's day opens with a gap from the 17:00 close to the 18:00 open",
      s["ck_gap_lo"][d3.first] == prev and s["ck_gap_hi"][d3.first] == b.open[d3.first]
      and s["ck_gap_dir"][d3.first] == 1 and clock.fmt_ny(int(b.timestamp[d3.first])) == "18:00")
fill = int(d3.first + 30)
b.low[fill] = prev - 0.25
s = clock.series(b)
check("not filled until a bar trades back to that close",
      not np.any(s["ck_gap_filled"][d3.first:fill]))
check("then filled for the rest of the day",
      np.all(s["ck_gap_filled"][fill:d3.last + 1] == 1))
check("a contract roll's jump (60 points against a usual few) is flagged for that day",
      [d.day for d in days if d.roll] == [days[8].day]
      and np.all(s["ck_roll"][days[8].first:days[8].last + 1] == 1)
      and not np.any(s["ck_roll"][:days[8].first]), [d.day for d in days if d.roll])
check("the roll flag uses only the days before it",
      clock.day_table(clock.head(b, days[8].first + 1))[-1].roll)
monday = next(d for d in days[1:] if d.weekday == 0)
week_end = [d for d in days if d.week == monday.week][-1]
friday_close = float(b.close[monday.first - 1])
check("the week's opening gap, Friday's close to Sunday's 18:00 open, lasts all week",
      s["ck_wgap_lo"][monday.first] == min(friday_close, b.open[monday.first])
      and np.all(s["ck_wgap_hi"][monday.first:week_end.last + 1]
                 == max(friday_close, b.open[monday.first]))
      and clock.label_ny(int(b.timestamp[monday.first])).endswith("18:00")
      and date.fromordinal(int(s["ck_day"][monday.first])).weekday() == 0
      and week_end.weekday == 4)
check("and the next week has its own", s["ck_wgap_hi"][days[-1].first]
      != s["ck_wgap_hi"][week_end.last])
check("a stock's gap is the 09:30 open against the close before",
      clock.series(sc.stock_series(200))["ck_gap_lo"][78] in
      (sc.stock_series(200).close[77], sc.stock_series(200).open[78]))

# ---------------------------------------------------------------------------
head("THE WEEK BEFORE")
# ---------------------------------------------------------------------------
b = futures(12)                    # opens Sunday 13 September: a whole first week
days = clock.day_table(b)
s = clock.series(b)
wk2 = next(d for d in days if d.week != days[0].week)
first_week = [d for d in days if d.week == days[0].week]
check("last week's high and low are the whole previous week's",
      s["ck_pwh"][wk2.first] == max(d.high for d in first_week)
      and s["ck_pwl"][wk2.first] == min(d.low for d in first_week))
b = futures(12, start=date(2026, 9, 15))      # opens Tuesday evening: a part-week
s = clock.series(b)
days = clock.day_table(b)
wk2 = next(d for d in days if d.week != days[0].week)
check("a part-week at the start of the data is not a week's high and low",
      math.isnan(s["ck_pwh"][wk2.first]))

# ---------------------------------------------------------------------------
head("4-HOUR BARS FROM HOURLY ONES")
# ---------------------------------------------------------------------------
hourly = futures(5, step=3600)
four = clock.resample(hourly, 4)
starts = sorted(set(clock.fmt_ny(int(t)) for t in four.timestamp))
check("a future's buckets start at 18:00, 22:00, 02:00, 06:00, 10:00 and 14:00",
      starts == ["02:00", "06:00", "10:00", "14:00", "18:00", "22:00"], starts)
check("each is the hourly bars inside it",
      four.open[1] == hourly.open[4] and four.close[1] == hourly.close[7]
      and four.high[1] == hourly.high[4:8].max() and four.low[1] == hourly.low[4:8].min())
check("the last bucket of a day ends at the 17:00 pause and counts once 16:00's bar is in",
      clock.fmt_ny(int(four.timestamp[-1])) == "14:00" and len(four.close) == 6 * 5)
at_11 = [k for k in range(len(hourly.close)) if clock.fmt_ny(int(hourly.timestamp[k])) == "11:00"][2]
cut4 = clock.resample(clock.head(hourly, at_11 + 1), 4)
check("a bucket still forming is dropped (10:00 to 14:00 with only 10:00 and 11:00 in)",
      clock.fmt_ny(int(cut4.timestamp[-1])) == "06:00", clock.fmt_ny(int(cut4.timestamp[-1])))
st = sc.walk(sc.stock_times(7 * 4, step=3600), interval="1h")
st4 = clock.resample(st, 4)
check("a stock's buckets are 09:30 to 13:30 and 13:30 to the close",
      sorted(set(clock.fmt_ny(int(t)) for t in st4.timestamp)) == ["09:30", "13:30"]
      and len(st4.close) == 8)
check("with the afternoon dropped while it is still trading",
      len(clock.resample(clock.head(st, 7 * 3 + 5), 4).close) == 7)
check("4-hour bars carry the instrument and their own interval",
      four.symbol == hourly.symbol and four.interval == "4h" and clock.kind_of(four) == "future")

# ---------------------------------------------------------------------------
head("A DAILY BAR'S DATE, WHATEVER IT IS STAMPED AT")
# ---------------------------------------------------------------------------
summer = sc.make([1.0] * 3, [1.0] * 3, [1.0] * 3, [1.0] * 3,
                 [ny(2026, 7, 6, 0, 0), ny(2026, 7, 7, 0, 0), ny(2026, 7, 8, 0, 0)],
                 kind="future", interval="1d")
check("a future's day stamped at 00:00 New York in summer is that day, not the one before",
      [date.fromordinal(int(x)) for x in clock.series(summer)["ck_day"]]
      == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)])
btc = sc.make([1.0] * 2, [1.0] * 2, [1.0] * 2, [1.0] * 2,
              [utc(2026, 11, 18), utc(2026, 11, 19)], kind="coin", interval="1d")
check("a coin's day, stamped at midnight UTC, is the UTC date",
      [date.fromordinal(int(x)) for x in clock.series(btc)["ck_day"]]
      == [date(2026, 11, 18), date(2026, 11, 19)])


print()
print("%d failure(s)" % len(fails))
print("CLOCK OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
