"""When a market's price can move, and a call worked out before a close.

The NYSE calendar worked out by rule: weekends, holidays and the days they are
taken, Good Friday, the 13:00 closes and summer time. Then the hour after a
close, listings elsewhere read from their own data, coins and anything unknown
always checked, a data clock that disagrees, and a call worked out before a
close not used after it. No network: the analysis is stubbed."""
import calendar
import os as _os
import sys as _sys
from datetime import date, timedelta
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

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


import web  # noqa: E402
from bot import sessions  # noqa: E402

harness.isolated_web("sessions")
HOUR = 3600


def at(y, mo, d, h=0, mi=0):
    """A moment in UTC, as epoch seconds."""
    return calendar.timegm((y, mo, d, h, mi, 0))


def us_meta(day, start=None, end=None, sort="EQUITY"):
    """Yahoo's chart answer for a US listing on a trading day: its type, its
    time zone and that day's regular session in UTC."""
    if start is None:
        start, end = sessions.us_session(day)
    offset = sessions.new_york_offset(day)
    return {"instrumentType": sort, "exchangeTimezoneName": "America/New_York",
            "gmtoffset": offset,
            "currentTradingPeriod": {
                "regular": {"start": start, "end": end, "gmtoffset": offset},
                "post": {"start": end, "end": end + 4 * HOUR, "gmtoffset": offset}}}


def seen(symbol, meta, when):
    """A download at `when` that said this about the instrument."""
    sessions.note(symbol, SimpleNamespace(meta=meta), when)


def may(symbol, since, now):
    return sessions.can_change(symbol, since, now)


def us_day(symbol, day):
    """AAPL as read after the close of `day`, when the data still describes it."""
    seen(symbol, us_meta(day), sessions.us_session(day)[1] + 2 * HOUR)


head("THE NYSE'S HOLIDAYS, BY RULE")
published = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
           "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
}
for year, days in published.items():
    got = sorted(d.isoformat() for d in sessions.nyse_holidays(year))
    check("%d's holidays are the NYSE's published list" % year, got == days, got)
check("New Year's Day on a Saturday is not taken on the Friday: 31 Dec 2021 traded",
      sessions.us_session(date(2021, 12, 31)) is not None
      and date(2021, 12, 31) not in sessions.nyse_holidays(2021))
check("on a Sunday it is taken on the Monday: 2 Jan 2023",
      sessions.us_session(date(2023, 1, 2)) is None)
check("Juneteenth is a holiday from 2022, and not before",
      date(2022, 6, 20) in sessions.nyse_holidays(2022)
      and not any(n == "Juneteenth" for n in sessions.nyse_holidays(2021).values()))
for year, sunday in ((2008, date(2008, 3, 23)), (2019, date(2019, 4, 21)),
                     (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)),
                     (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25)),
                     (2285, date(2285, 3, 22))):
    check("Easter %d is %s, and Good Friday two days before" % (year, sunday),
          sessions.easter(year) == sunday
          and sessions.nyse_holidays(year).get(sunday - timedelta(days=2)) == "Good Friday",
          sessions.easter(year))
for year, days in ((2025, ["2025-07-03", "2025-11-28", "2025-12-24"]),
                   (2026, ["2026-11-27", "2026-12-24"]),
                   (2027, ["2027-11-26"])):
    got = [d.isoformat() for d in (date(year, 1, 1) + timedelta(days=k) for k in range(366))
           if d.year == year and sessions.early_close(d)]
    check("%d's 13:00 closes are %s" % (year, ", ".join(days)), got == days, got)
check("2 July 2026 is a full day: the next day is the holiday, not a half day",
      sessions.us_session(date(2026, 7, 2))[1] == at(2026, 7, 2, 20, 0))
# Yahoo's own answer for Monday 16 November 2026, from a live download.
check("a session lands where the data source puts it",
      sessions.us_session(date(2026, 11, 16)) == (1794839400, 1794862800),
      sessions.us_session(date(2026, 11, 16)))
check("no session at weekends", sessions.us_session(date(2026, 11, 14)) is None
      and sessions.us_session(date(2026, 11, 15)) is None)

head("A US STOCK THROUGH A WEEK (NEW YORK ON WINTER TIME)")
# Friday 13 November 2026: open 14:30 UTC, close 21:00, settled 22:00.
us_day("AAPL", date(2026, 11, 13))
check("during the session it is checked",
      may("AAPL", at(2026, 11, 13, 15, 0), at(2026, 11, 13, 15, 5)))
check("the check just after the close happens",
      may("AAPL", at(2026, 11, 13, 20, 58), at(2026, 11, 13, 21, 3)))
check("and the next ones while the close settles",
      may("AAPL", at(2026, 11, 13, 21, 3), at(2026, 11, 13, 21, 8))
      and may("AAPL", at(2026, 11, 13, 21, 58), at(2026, 11, 13, 22, 3)))
check("then, once one has looked after the hour, it waits",
      not may("AAPL", at(2026, 11, 13, 22, 3), at(2026, 11, 13, 22, 8)))
check("a loop slower than the hour still gets its look after the close",
      may("AAPL", at(2026, 11, 13, 20, 30), at(2026, 11, 13, 22, 30)))
check("Saturday and Sunday are left alone",
      not may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 14, 15, 0))
      and not may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 15, 23, 0)))
check("so is Monday before the open",
      not may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 16, 14, 29)))
check("and the open is checked",
      may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 16, 14, 30)))
check("a weekday night waits too, until the morning's open",
      not may("AAPL", at(2026, 11, 17, 22, 5), at(2026, 11, 18, 14, 25))
      and may("AAPL", at(2026, 11, 17, 22, 5), at(2026, 11, 18, 14, 35)))
check("a last check older than a week is never trusted to cover a closed market",
      may("AAPL", at(2026, 11, 1, 22, 5), at(2026, 11, 14, 15, 0)))

head("HOLIDAYS AND THE DAYS THEY ARE TAKEN (SUMMER TIME)")
# Thursday 2 July 2026: close 20:00 UTC, settled 21:00. Friday 3 July is
# Independence Day, taken on the Friday because the 4th is a Saturday.
us_day("AAPL", date(2026, 7, 2))
check("Independence Day taken on Friday 3 July 2026 is left alone",
      not may("AAPL", at(2026, 7, 2, 21, 5), at(2026, 7, 3, 15, 0)))
check("through the weekend after it",
      not may("AAPL", at(2026, 7, 2, 21, 5), at(2026, 7, 5, 22, 0)))
check("until Monday's open at 13:30 UTC",
      not may("AAPL", at(2026, 7, 2, 21, 5), at(2026, 7, 6, 13, 29))
      and may("AAPL", at(2026, 7, 2, 21, 5), at(2026, 7, 6, 13, 30)))
us_day("AAPL", date(2027, 6, 17))
check("Juneteenth taken on Friday 18 June 2027 is left alone",
      not may("AAPL", at(2027, 6, 17, 21, 5), at(2027, 6, 18, 15, 0)))
us_day("AAPL", date(2027, 12, 23))
check("Christmas taken on Friday 24 December 2027 is left alone",
      not may("AAPL", at(2027, 12, 23, 22, 5), at(2027, 12, 24, 15, 0)))
us_day("AAPL", date(2021, 12, 30))
check("31 December 2021, before a Saturday New Year's Day, is checked",
      may("AAPL", at(2021, 12, 30, 22, 5), at(2021, 12, 31, 15, 0)))
us_day("AAPL", date(2026, 11, 25))
check("Thanksgiving is left alone, and the day after it is checked",
      not may("AAPL", at(2026, 11, 25, 22, 5), at(2026, 11, 26, 16, 0))
      and may("AAPL", at(2026, 11, 25, 22, 5), at(2026, 11, 27, 14, 30)))

head("GOOD FRIDAY")
us_day("AAPL", date(2026, 4, 2))
check("Good Friday, 3 April 2026, is left alone",
      not may("AAPL", at(2026, 4, 2, 21, 5), at(2026, 4, 3, 15, 0)))
check("and Monday's open is checked",
      may("AAPL", at(2026, 4, 2, 21, 5), at(2026, 4, 6, 13, 30)))

head("THE 13:00 CLOSES")
# The day after Thanksgiving 2026: open 14:30 UTC, close 18:00, settled 19:00.
us_day("AAPL", date(2026, 11, 27))
check("the hour after an early close is checked",
      may("AAPL", at(2026, 11, 27, 18, 30), at(2026, 11, 27, 18, 40)))
check("then it waits, at an hour that is mid-session on other days",
      not may("AAPL", at(2026, 11, 27, 19, 5), at(2026, 11, 27, 20, 0)))
us_day("AAPL", date(2026, 12, 24))
check("Christmas Eve 2026 closes at 13:00 and Christmas Day is left alone",
      not may("AAPL", at(2026, 12, 24, 19, 5), at(2026, 12, 25, 15, 0))
      and may("AAPL", at(2026, 12, 24, 19, 5), at(2026, 12, 28, 14, 30)))
friday = date(2026, 11, 27)
seen("AAPL", us_meta(friday, sessions.us_session(friday)[0], at(2026, 11, 27, 21, 0)),
     at(2026, 11, 27, 19, 5))
check("data that has the market trading until 16:00 that day is believed instead",
      may("AAPL", at(2026, 11, 27, 19, 5), at(2026, 11, 27, 20, 0)))

head("SUMMER TIME BEGINS AND ENDS")
check("New York is five hours behind on 6 March 2026 and four on 9 March",
      sessions.new_york_offset(date(2026, 3, 6)) == -5 * HOUR
      and sessions.new_york_offset(date(2026, 3, 9)) == -4 * HOUR)
check("four on 30 October 2026 and five again on 2 November",
      sessions.new_york_offset(date(2026, 10, 30)) == -4 * HOUR
      and sessions.new_york_offset(date(2026, 11, 2)) == -5 * HOUR)
us_day("AAPL", date(2026, 3, 6))
check("Friday 6 March still trades at 20:30 UTC, on winter time",
      may("AAPL", at(2026, 3, 6, 20, 25), at(2026, 3, 6, 20, 30)))
check("Monday 9 March opens at 13:30 UTC, an hour earlier",
      not may("AAPL", at(2026, 3, 6, 22, 5), at(2026, 3, 9, 13, 29))
      and may("AAPL", at(2026, 3, 6, 22, 5), at(2026, 3, 9, 13, 31)))
us_day("AAPL", date(2026, 3, 9))
check("and closes at 20:00 UTC, settled by 21:00",
      may("AAPL", at(2026, 3, 9, 20, 5), at(2026, 3, 9, 20, 30))
      and not may("AAPL", at(2026, 3, 9, 21, 5), at(2026, 3, 9, 22, 0)))
us_day("AAPL", date(2026, 10, 30))
check("after summer time ends, 13:35 UTC on Monday 2 November is before the open",
      not may("AAPL", at(2026, 10, 30, 21, 5), at(2026, 11, 2, 13, 35)))
check("which is at 14:30 UTC",
      may("AAPL", at(2026, 10, 30, 21, 5), at(2026, 11, 2, 14, 31)))
zone = sessions._zone()
if zone is None:
    print("   (no time zone database here, so the built-in rule is what ran)")
else:
    real_zone = sessions._zone
    try:
        sessions._zone = lambda: None
        day, differ = date(2007, 1, 1), []
        while day < date(2038, 1, 1):
            rule = sessions.new_york_offset(day)
            sessions._zone = real_zone
            if sessions.new_york_offset(day) != rule:
                differ.append(day)
            sessions._zone = lambda: None
            day += timedelta(days=1)
    finally:
        sessions._zone = real_zone
    check("the built-in rule agrees with the time zone database every day "
          "from 2007 to 2037", not differ, differ[:3])

head("A CLOCK THE DATA DISAGREES WITH")
friday = date(2026, 11, 13)
start, end = sessions.us_session(friday)
seen("AAPL", us_meta(friday, start - HOUR, end - HOUR), at(2026, 11, 13, 22, 5))
check("data that opens the market an hour before the calendar does is believed",
      may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 14, 15, 0)))
us_day("AAPL", friday)
check("until a later download agrees with the calendar again",
      not may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 14, 15, 0)))
bare = us_meta(friday)
del bare["currentTradingPeriod"]
seen("AAPL", bare, at(2026, 11, 13, 22, 5))
check("data that gives no hours is not taken as agreeing",
      may("AAPL", at(2026, 11, 13, 22, 5), at(2026, 11, 14, 15, 0)))

head("COINS, FUNDS AND ANYTHING NOT KNOWN ARE ALWAYS CHECKED")
saturday = (at(2026, 11, 14, 15, 0), at(2026, 11, 14, 15, 5))
seen("BTC-USD", {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC",
                 "currentTradingPeriod": {"regular": {"start": at(2026, 11, 14),
                                                      "end": at(2026, 11, 14, 23, 59)}}},
     saturday[0])
check("a coin is checked on a Saturday", may("BTC-USD", *saturday))
check("and every round of the week",
      all(may("BTC-USD", t, t + 300) for t in range(at(2026, 11, 9), at(2026, 11, 16), 1800)))
check("a coin never read yet is checked", may("ETH-USD", *saturday))
us_day("AAPL", friday)
seen("VFIAX", us_meta(friday, sort="MUTUALFUND"), at(2026, 11, 13, 22, 5))
check("a US mutual fund, whose price comes hours after the close, is checked",
      may("VFIAX", at(2026, 11, 13, 22, 5), saturday[0]))
seen("^GSPC", us_meta(friday, sort="INDEX"), at(2026, 11, 13, 22, 5))
check("so is an index", may("^GSPC", at(2026, 11, 13, 22, 5), saturday[0]))
check("a symbol whose data was never read is checked", may("ZZZZ", *saturday))
seen("VOD.L", {"instrumentType": "EQUITY", "exchangeTimezoneName": "Europe/London"},
     saturday[0])
check("a listing elsewhere whose data gives no hours is checked",
      may("VOD.L", saturday[0], saturday[1]))
check("so is anything with no good check yet",
      may("AAPL", None, saturday[0]) and may("AAPL", 0, saturday[0]))
check("or a clock that has gone back",
      may("AAPL", saturday[1], saturday[0]))

head("A LISTING ELSEWHERE, FROM ITS OWN DATA")
# London on Monday 16 November 2026, as Yahoo gave it: the regular session
# 08:00 to 16:30 UTC, then an after-market to 17:15.
london = {"instrumentType": "EQUITY", "exchangeTimezoneName": "Europe/London",
          "gmtoffset": 0, "currentTradingPeriod": {
              "regular": {"start": at(2026, 11, 16, 8), "end": at(2026, 11, 16, 16, 30),
                          "gmtoffset": 0},
              "post": {"start": at(2026, 11, 16, 16, 30), "end": at(2026, 11, 16, 17, 15),
                       "gmtoffset": 0}}}
seen("VOD.L", london, at(2026, 11, 16, 16, 0))
check("it is checked through the hour after its after-market ends",
      may("VOD.L", at(2026, 11, 16, 17, 40), at(2026, 11, 16, 18, 0)))
check("then left alone until midnight there",
      not may("VOD.L", at(2026, 11, 16, 18, 20), at(2026, 11, 16, 23, 55)))
check("when nothing says yet when the next session starts, so it is checked",
      may("VOD.L", at(2026, 11, 16, 18, 20), at(2026, 11, 17, 0, 5)))
tuesday = {"instrumentType": "EQUITY", "exchangeTimezoneName": "Europe/London",
           "gmtoffset": 0, "currentTradingPeriod": {
               "regular": {"start": at(2026, 11, 17, 8), "end": at(2026, 11, 17, 16, 30),
                           "gmtoffset": 0}}}
seen("VOD.L", tuesday, at(2026, 11, 17, 0, 10))
seen("VOD.L", tuesday, at(2026, 11, 17, 0, 15))
check("once the data names the next session, an hour after it first said so",
      may("VOD.L", at(2026, 11, 17, 0, 15), at(2026, 11, 17, 0, 20))
      and not may("VOD.L", at(2026, 11, 17, 1, 15), at(2026, 11, 17, 7, 59)))
check("it waits for that session's open",
      may("VOD.L", at(2026, 11, 17, 1, 15), at(2026, 11, 17, 8, 0)))
# Seoul: Yahoo's regular session ends at 15:00 there, but the closing
# auction runs to 15:30, inside what it calls the after-market.
seoul = {"instrumentType": "EQUITY", "exchangeTimezoneName": "Asia/Seoul",
         "gmtoffset": 9 * HOUR, "currentTradingPeriod": {
             "regular": {"start": at(2026, 11, 17, 0), "end": at(2026, 11, 17, 6)},
             "post": {"start": at(2026, 11, 17, 6), "end": at(2026, 11, 17, 9)}}}
seen("005930.KS", seoul, at(2026, 11, 17, 5))
check("Seoul is still checked more than an hour after Yahoo's regular end",
      may("005930.KS", at(2026, 11, 17, 7, 30), at(2026, 11, 17, 8, 30)))
check("and left alone from an hour after its after-market until midnight there",
      not may("005930.KS", at(2026, 11, 17, 10, 5), at(2026, 11, 17, 14, 55))
      and may("005930.KS", at(2026, 11, 17, 10, 5), at(2026, 11, 17, 15, 5)))

head("A CALL IS NOT CARRIED ACROSS A CLOSE")
sessions._SEEN.clear()
us_day("AAPL", date(2026, 11, 13))
seen("BTC-USD", {"instrumentType": "CRYPTOCURRENCY"}, at(2026, 11, 13, 22))
ANALYSED = []


def analyse(symbol, cfg, **_kw):
    ANALYSED.append(symbol)
    return SimpleNamespace(plan=SimpleNamespace(action="BUY"))


real = (web.engine.analyse, web.public_mod.verdict)
web.engine.analyse = analyse
# The public check for AAPL was written mid-session, from a day not over.
web.public_mod.verdict = lambda conn, symbol: (
    {"symbol": symbol, "at": at(2026, 11, 13, 18, 0), "action": "WAIT"}
    if symbol == "AAPL" else None)
try:
    web._CALLS.clear()
    before_close = web._call_now("AAPL", "1d", now=at(2026, 11, 13, 21, 50))
    check("while the close settles, the day's public call is used as before",
          before_close == "WAIT" and not ANALYSED, (before_close, ANALYSED))
    # Fifteen minutes on: inside the half hour a call is kept, but the
    # close has settled in between.
    after = web._call_now("AAPL", "1d", now=at(2026, 11, 13, 22, 5))
    check("once it has settled, neither that nor the call kept from before is used",
          after == "BUY" and ANALYSED == ["AAPL"], (after, ANALYSED))
    again = web._call_now("AAPL", "1d", now=at(2026, 11, 13, 22, 10))
    check("and the call worked out after the close is kept as usual",
          again == "BUY" and ANALYSED == ["AAPL"], ANALYSED)
    del ANALYSED[:]
    web._call_now("BTC-USD", "1d", now=at(2026, 11, 14, 3, 0))
    web._call_now("BTC-USD", "1d", now=at(2026, 11, 14, 3, 10))
    check("a coin's call is kept for its half hour, as before",
          ANALYSED == ["BTC-USD"], ANALYSED)
finally:
    web.engine.analyse, web.public_mod.verdict = real
    web._CALLS.clear()

print()
print("%d failure(s)" % len(fails))
print("SESSIONS OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
