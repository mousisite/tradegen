import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import numpy as np
from bot.market import fetch_bars
from bot import strategies as S
from bot import indicators as ind

b = fetch_bars("BTC-USD", "5m")
ctx = S.build_context(b)
i = len(b) - 1
sess = b.session_id[i]
uniq = np.unique(b.session_id[:i + 1])
print("sessions total:", len(uniq), "current:", sess, "uniq[-1]:", uniq[-1], "uniq[-2]:", uniq[-2])

prior = b.session_id == uniq[-2] if uniq[-1] == sess else b.session_id == uniq[-1]
print("prior bar count:", int(prior.sum()))
ph, pl, pc = float(np.max(b.high[prior])), float(np.min(b.low[prior])), float(b.close[prior][-1])
print("prior H/L/C: %.2f %.2f %.2f" % (ph, pl, pc))
p = ind.pivot_points(ph, pl, pc)
for k, v in p.items():
    print("   %-6s %.2f" % (k, v))
price = float(b.close[i])
atr_v = ctx.at("atr", i)
print("price=%.2f atr=%.4f" % (price, atr_v))
print("dist in ATR = %.2f" % ((price - p["pivot"]) / atr_v))

nearest = min(((abs(price - v), k) for k, v in p.items()), key=lambda t: t[0])
print("nearest level:", nearest)
sig = S.pivot_levels(ctx, i)
print("signal:", sig.score, sig.reason)


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
