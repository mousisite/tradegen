"""The lock file the server installs from agrees with requirements.txt.

The Dockerfile installs requirements.lock with --require-hashes and
--no-deps, so a package added to requirements.txt and not to the lock is
simply missing on the server, and a pin changed in one file and not the
other installs something that was never tested. This catches both.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path):
    """name -> version for every 'name==version' requirement line."""
    found = {}
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?==([0-9A-Za-z.+!*]+)", line)
            if m:
                found[norm(m.group(1))] = m.group(3)
    return found


wanted = pins("requirements.txt")
locked = pins("requirements.lock")
check("requirements.txt pins every package exactly", len(wanted) >= 10, wanted)
missing = sorted(n for n in wanted if n not in locked)
check("every package in requirements.txt is in the lock", not missing, missing)
differ = sorted("%s %s/%s" % (n, wanted[n], locked[n])
                for n in wanted if n in locked and wanted[n] != locked[n])
check("at the same version", not differ, differ)

with open(os.path.join(ROOT, "requirements.lock"), encoding="utf-8") as fh:
    text = fh.read()
entries = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n", 1)[1])
unhashed = [e.split()[0] for e in entries
            if re.match(r"^[A-Za-z0-9]", e) and "--hash=sha256:" not in e]
check("every locked package carries its files' hashes", not unhashed, unhashed)
check("no local paths in the lock (the repository is public)",
      not re.search(r"[A-Za-z]:[\\/]|/home/|/Users/", text))
with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
    docker = fh.read()
check("the Dockerfile installs from the lock with hashes required",
      "--require-hashes" in docker and "requirements.lock" in docker)

print()
print("%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
