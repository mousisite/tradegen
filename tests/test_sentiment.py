import sys
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from bot.sentiment import lexicon_score

cases = [
    ("Tesla beats earnings but cuts full-year guidance", "neg"),
    ("Company is not strong and misses estimates", "neg"),
    ("Nvidia surges on record data center growth", "pos"),
    ("SEC opens investigation into company accounting", "neg"),
    ("Stock closed unchanged on light volume", "flat"),
    ("Shares rise despite weak guidance", "pos"),
    ("Not a strong quarter", "neg"),
    ("Analyst upgrades stock to outperform, raises target", "pos"),
    ("Revenue misses but company raises guidance and announces buyback", "pos"),
    ("Bankruptcy fears mount as lawsuit expands", "neg"),
]
ok = True
for text, want in cases:
    s = lexicon_score(text)
    got = "pos" if s > 0.10 else ("neg" if s < -0.10 else "flat")
    mark = "OK " if got == want else "BAD"
    if got != want:
        ok = False
    print("%s %+.3f want=%-4s got=%-4s | %s" % (mark, s, want, got, text))
print("\nALL PASS" if ok else "\nSOME FAILED")


# --- exit code so a runner can tell pass from fail -------------------------
import sys as _exit_sys
_bad = bool(globals().get("fails")) or bool(globals().get("fail"))     or (globals().get("ok") is False)
_exit_sys.exit(1 if _bad else 0)
