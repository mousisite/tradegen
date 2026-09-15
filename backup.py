"""Take a backup that is actually safe to restore.

The obvious way to back up a SQLite file is to copy it. That is wrong here. The
database runs in WAL mode, so at any moment some committed data lives in the
-wal file rather than the main one, and a plain copy taken while the server is
writing produces a file that may open fine and be missing the most recent
writes, or may not open at all. It is the kind of backup you discover is broken
on the day you need it.

SQLite's own backup API copies a consistent snapshot of a live database while
it is being written to, which is what this uses.

    python backup.py                     writes into backups/
    python backup.py --out D:\\safe       somewhere else
    python backup.py --keep 30           prune to the newest 30
    python backup.py --csv               also write the plain-text exports
    python backup.py --verify <file>     check a backup opens and is intact
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _size(path: str) -> str:
    n = os.path.getsize(path)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "bytes" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d bytes" % n


def take(out_dir: str, with_csv: bool = False) -> str:
    """Copy a consistent snapshot of the live database."""
    from bot import database as db_mod

    source_path = db_mod.default_path()
    if not os.path.exists(source_path):
        # A normal exception, not SystemExit. This is called from a scheduler
        # as well as from a command line, and a library that kills the process
        # is a library that cannot be scheduled.
        raise FileNotFoundError("There is no database at %s yet." % source_path)

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    target_path = os.path.join(out_dir, "stockbot-%s.db" % stamp)

    # The stamp is only accurate to the second, so two backups taken in the
    # same second would land on the same filename and one would silently
    # replace the other. Rare in normal use and disastrous when it happens,
    # because the count of backups you think you have would be wrong.
    suffix = 1
    while os.path.exists(target_path):
        target_path = os.path.join(out_dir, "stockbot-%s-%d.db" % (stamp, suffix))
        suffix += 1

    source = sqlite3.connect(source_path, timeout=30.0)
    target = sqlite3.connect(target_path)
    try:
        # The backup API, not a file copy: consistent even mid-write.
        source.backup(target)
    finally:
        target.close()
        source.close()

    print("  database  %s  (%s)" % (target_path, _size(target_path)))

    if with_csv:
        # A database is only readable by this program. The CSVs are readable by
        # anything, which matters if the worst case is that this app is gone.
        from bot import export as export_mod
        conn = db_mod.connect(target_path)
        try:
            for name in export_mod.DATASETS:
                built = export_mod.build(conn, name, "csv")
                path = os.path.join(out_dir, "stockbot-%s-%s.csv" % (name, stamp))
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(built["body"])
                print("  %-9s %s  (%d rows)" % (name, path, built["count"]))
        finally:
            conn.close()

    return target_path


def verify(path: str) -> bool:
    """Open a backup and check it is intact and has the tables it should."""
    if not os.path.exists(path):
        print("  no such file: %s" % path)
        return False
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                print("  integrity check failed: %s" % result)
                return False

            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"runs", "trades", "watchlist", "alerts", "theses",
                        "strategy_stats", "users"}
            missing = expected - names
            if missing:
                print("  tables missing: %s" % ", ".join(sorted(missing)))
                return False

            print("  integrity  ok")
            for table in ("users", "runs", "trades", "theses", "strategy_stats"):
                count = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                print("  %-15s %d rows" % (table, count))
            return True
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        print("  not a readable database: %s" % exc)
        return False


def prune(out_dir: str, keep: int) -> None:
    """Delete all but the newest few, so this can run on a timer."""
    if keep <= 0 or not os.path.isdir(out_dir):
        return
    found = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir)
         if f.startswith("stockbot-") and f.endswith(".db")),
        key=os.path.getmtime, reverse=True)
    for old in found[keep:]:
        try:
            os.remove(old)
            print("  removed old backup %s" % os.path.basename(old))
        except OSError as exc:
            print("  could not remove %s: %s" % (old, exc))


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=os.path.join(here, "backups"),
                        help="where to write backups")
    parser.add_argument("--keep", type=int, default=14,
                        help="how many to keep, 0 for all")
    parser.add_argument("--csv", action="store_true",
                        help="also write plain-text exports")
    parser.add_argument("--verify", metavar="FILE",
                        help="check an existing backup instead of taking one")
    args = parser.parse_args(argv)

    if args.verify:
        print()
        ok = verify(args.verify)
        print()
        return 0 if ok else 1

    print()
    print("  Backing up")
    try:
        path = take(args.out, with_csv=args.csv)
    except FileNotFoundError as exc:
        print("  %s" % exc)
        print("  Nothing to back up yet. Run the app once first.")
        print()
        return 1
    print()
    print("  Verifying the copy that was just written")
    ok = verify(path)
    if ok:
        prune(args.out, args.keep)
    print()
    print("  Done." if ok else "  The backup did not verify. Do not rely on it.")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
