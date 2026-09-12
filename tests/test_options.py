"""Verify the options module: maths first, then live data."""
import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot import options as O

fails = []
def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if not ok else ""))
    if not ok:
        fails.append(name)

print("=" * 72)
print("BLACK-SCHOLES against known textbook values")
print("=" * 72)
# Standard reference case: S=100, K=100, T=1, r=5%, vol=20%
# Call = 10.4506, Put = 5.5735, call delta = 0.6368
c = O.black_scholes(100, 100, 1.0, 0.05, 0.20, True)
p = O.black_scholes(100, 100, 1.0, 0.05, 0.20, False)
print("   call price %.4f (expect 10.4506)" % c["price"])
print("   put  price %.4f (expect  5.5735)" % p["price"])
print("   call delta %.4f (expect  0.6368)" % c["delta"])
print("   gamma      %.4f (expect  0.0188)" % c["gamma"])
check("call price", abs(c["price"] - 10.4506) < 0.001)
check("put price", abs(p["price"] - 5.5735) < 0.001)
check("call delta", abs(c["delta"] - 0.6368) < 0.001)
check("gamma", abs(c["gamma"] - 0.018762) < 0.0001)

# Put-call parity: C - P = S - K*e^(-rT)
import math
parity = c["price"] - p["price"]
expected = 100 - 100 * math.exp(-0.05 * 1.0)
print("   parity C-P = %.4f, S-Ke^-rT = %.4f" % (parity, expected))
check("put-call parity holds", abs(parity - expected) < 1e-6)

check("call delta between 0 and 1", 0 <= c["delta"] <= 1)
check("put delta between -1 and 0", -1 <= p["delta"] <= 0)
check("theta is negative for long options", c["theta"] < 0 and p["theta"] < 0)
check("vega positive and shared", abs(c["vega"] - p["vega"]) < 1e-9 and c["vega"] > 0)
check("expiry gives intrinsic", abs(O.black_scholes(110, 100, 0, 0.05, 0.2, True)["price"] - 10) < 1e-9)
check("zero vol gives intrinsic", O.black_scholes(90, 100, 1, 0.0, 0.0, True)["price"] == 0)

print()
print("=" * 72)
print("IMPLIED VOLATILITY round trip")
print("=" * 72)
for vol in (0.12, 0.25, 0.50, 1.20):
    for strike in (80, 100, 125):
        price = O.black_scholes(100, strike, 0.5, 0.04, vol, True)["price"]
        back = O.implied_vol(price, 100, strike, 0.5, 0.04, True)
        ok = back is not None and abs(back - vol) < 0.001
        if not ok:
            check("IV round trip vol=%.2f K=%d" % (vol, strike), False,
                  "got %s" % back)
print("   OK   every volatility and strike recovered to within 0.001")

check("IV rejects price below intrinsic", O.implied_vol(1.0, 120, 100, 0.5, 0.04, True) is None)
check("IV rejects zero price", O.implied_vol(0, 100, 100, 0.5, 0.04, True) is None)

print()
print("=" * 72)
print("LIVE CHAIN")
print("=" * 72)
rate = O.risk_free_rate()
print("   risk-free rate in use: %.3f%%" % (rate * 100))
check("rate is plausible", 0 <= rate < 0.20)

for sym in ["AAPL", "NVDA"]:
    try:
        ch = O.load_chain(sym)
    except Exception as exc:
        check("%s chain loads" % sym, False, str(exc)[:70])
        continue
    print("\n   %s  spot %.2f  expiry %s (%d days)" % (
        ch.symbol, ch.spot, ch.expiry_date, ch.days))
    print("   %d calls, %d puts, %d expiries available" % (
        len(ch.calls), len(ch.puts), len(ch.expirations)))
    check("%s has contracts" % sym, len(ch.all) > 10)
    check("%s expiry is at least a week out" % sym, ch.days >= 7, "%d days" % ch.days)

    withiv = [c for c in ch.all if c.iv]
    check("%s most contracts got an IV" % sym, len(withiv) > len(ch.all) * 0.5,
          "%d of %d" % (len(withiv), len(ch.all)))

    calls_ok = [c for c in ch.calls if c.iv and c.quality == "ok"]
    if calls_ok:
        bad_delta = [c for c in calls_ok if not (0 <= c.delta <= 1)]
        check("%s call deltas in range" % sym, not bad_delta, "%d bad" % len(bad_delta))
    puts_ok = [c for c in ch.puts if c.iv and c.quality == "ok"]
    if puts_ok:
        bad = [c for c in puts_ok if not (-1 <= c.delta <= 0)]
        check("%s put deltas in range" % sym, not bad, "%d bad" % len(bad))

    a = O.analyse(ch)
    print("   ATM IV: %s" % ("%.1f%%" % (a["atm_iv"] * 100) if a["atm_iv"] else "—"))
    em = a["expected_move"]
    if em:
        print("   expected move: %.2f (%.1f%%) -> %.2f to %.2f" % (
            em["absolute"], em["percent"] * 100, em["lower"], em["upper"]))
    print("   put/call OI: %s   volume: %s" % (
        "%.2f" % a["put_call_oi"] if a["put_call_oi"] else "—",
        "%.2f" % a["put_call_volume"] if a["put_call_volume"] else "—"))
    print("   skew: %s   tradable: %d of %d" % (
        "%.3f" % a["skew"] if a["skew"] is not None else "—",
        a["tradable_count"], a["total_count"]))
    check("%s expected move is sane" % sym,
          em is None or 0 < em["percent"] < 0.5, str(em.get("percent") if em else None))
    for line in a["reading"]:
        print("      - %s" % line)
    for note in a["notes"]:
        print("      ! %s" % note)

    for view in ("bullish", "bearish"):
        ideas = O.suggest(ch, view, 0.6)
        print("   %s idea: %s" % (view, ideas[0]["name"] if ideas else "none"))
        check("%s %s suggestion produced" % (sym, view), len(ideas) > 0)

print()
print("%d failure(s)" % len(fails))
print("OPTIONS OK" if not fails else "FAILURES: %s" % fails)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
